"""
Single-file audio processing function.

This module contains process_single_file() as a standalone, module-level function
so it can be serialized and used with ProcessPoolExecutor for parallel processing.
"""

import soundfile as sf
import pyloudnorm as pyln
import numpy as np
import shutil
import logging
from pathlib import Path

from .measurement import measure_true_peak, measure_lra
from .dither import apply_tpdf_dither
from .metadata import inject_bext_chunk, inject_ixml_chunk, build_ixml_for_normalization
from .streaming import (should_use_streaming, measure_streaming, write_normalized_streaming,
                        NonFiniteSamplesError)
from .. import get_output_filename, VERSION


def process_single_file(audio_path, target_lufs, peak_ceiling, strict_lufs_matching,
                        bit_depth, sample_rate, normalized_path, needs_limiting_path,
                        embed_bwf=False, rng_seed=None, dry_run=False):
    """
    Process a single audio file for LUFS normalization.

    This is a standalone function (not a method) so it can be pickled
    for use with ProcessPoolExecutor.

    Args:
        audio_path: Path to input audio file
        target_lufs: Target integrated loudness in LUFS
        peak_ceiling: Maximum True Peak in dBTP
        strict_lufs_matching: If True, skip files exceeding peak
        bit_depth: Output bit depth ('preserve', '16', '24', '32')
        sample_rate: Output sample rate ('preserve', '44100 Hz', '48000 Hz')
        normalized_path: Path to normalized output directory
        needs_limiting_path: Path to needs_limiting output directory
        embed_bwf: If True, embed BWF BEXT + iXML metadata in output WAV
        rng_seed: Optional seed for deterministic TPDF dithering

    Returns:
        dict with keys:
            type: 'success' | 'skipped' | 'needs_limiting' | 'blocked' | 'error'
            filename: str
            result: dict (for success — CSV fields)
            skipped: dict (for needs_limiting — skipped report fields)
            error: dict (for error tracking)
            output_file: str (path to output file, if any)
            log_messages: list of (level, message) tuples
    """
    audio_path = Path(audio_path)
    normalized_path = Path(normalized_path)
    needs_limiting_path = Path(needs_limiting_path)

    log_messages = []
    rng = np.random.default_rng(rng_seed) if rng_seed is not None else np.random.default_rng()

    def log(level, msg):
        log_messages.append((level, msg))

    def non_finite_failure(count):
        msg = f"{count} non-finite samples (NaN/inf)"
        log('error', f"FAILED: {audio_path.name} | {msg}")
        return {
            'type': 'error',
            'filename': audio_path.name,
            'error': {
                'filename': audio_path.name,
                'error': msg,
                'status': 'FAILED',
                'reason': 'non_finite_samples'
            },
            'output_file': None,
            'log_messages': log_messages,
        }

    try:
        # Read file metadata upfront (no audio data loaded yet)
        _info = sf.info(str(audio_path))
        rate = _info.samplerate
        original_format = _info.subtype
        channels = _info.channels

        # Reject >2 channels. ITU-R BS.1770-4 requires per-channel weights (Ls/Rs at
        # +1.5 dB for 5.1) that pyloudnorm's default Meter does not apply without
        # explicit channel-layout configuration. Rather than silently report wrong
        # LUFS for surround content, block it with a clear error.
        if channels > 2:
            log('error', f"BLOCKED: {audio_path.name} | {channels}-channel surround not supported "
                         f"(mono/stereo only — multi-channel BS.1770 weighting not implemented)")
            return {
                'type': 'blocked',
                'filename': audio_path.name,
                'error': {
                    'filename': audio_path.name,
                    'error': f'{channels}-channel audio not supported (mono/stereo only)',
                    'status': 'BLOCKED',
                    'reason': 'multichannel_unsupported'
                },
                'output_file': None,
                'log_messages': log_messages,
            }

        # Choose measurement strategy based on estimated float64 footprint.
        # Files whose in-memory representation would exceed ~2 GiB are measured
        # and written in constant-memory chunks to prevent OOM.
        _use_streaming = should_use_streaming(audio_path)

        if _use_streaming:
            log('info', f"  Large file detected — using streaming mode (chunked I/O)")
            try:
                original_lufs, _input_true_peak_db = measure_streaming(audio_path)
            except NonFiniteSamplesError as _e:
                return non_finite_failure(_e.count)
            except RuntimeError as _e:
                log('error', f"BLOCKED: {audio_path.name} | {_e}")
                return {
                    'type': 'blocked',
                    'filename': audio_path.name,
                    'error': {
                        'filename': audio_path.name,
                        'error': str(_e),
                        'status': 'BLOCKED',
                        'reason': 'streaming_requires_scipy'
                    },
                    'output_file': None,
                    'log_messages': log_messages,
                }
            lra_lu = None
            data = None
        else:
            # Standard path: load full file into RAM
            data, rate = sf.read(str(audio_path))

            non_finite = int(np.count_nonzero(~np.isfinite(data)))
            if non_finite:
                return non_finite_failure(non_finite)

            # Measure original LUFS
            meter = pyln.Meter(rate)
            if data.ndim == 1:
                original_lufs = meter.integrated_loudness(data.reshape(-1, 1))
            else:
                original_lufs = meter.integrated_loudness(data)

            # Measure LRA
            lra_lu = measure_lra(data, rate)

        # Check for silence
        if original_lufs == float('-inf') or original_lufs < -70:
            log('warning', f"SKIPPED: {audio_path.name} | Too quiet/silent")
            return {
                'type': 'skipped',
                'filename': audio_path.name,
                'error': {
                    'filename': audio_path.name,
                    'error': 'Too quiet/silent',
                    'status': 'SKIPPED',
                    'reason': 'too_quiet'
                },
                'output_file': None,
                'log_messages': log_messages,
            }

        # Calculate gain needed
        gain_db = target_lufs - original_lufs
        gain_linear = 10 ** (gain_db / 20)

        # Predict post-normalization True Peak.
        # Streaming mode: gain is a linear scalar, so TP_out = TP_in + gain_db (dB).
        # Standard mode: apply gain to buffer and oversample directly.
        if _use_streaming:
            predicted_peak = _input_true_peak_db + gain_db
        else:
            test_normalized = data * gain_linear
            predicted_peak = measure_true_peak(test_normalized, rate)

        # Dry run: report what would happen without writing anything
        if dry_run:
            if predicted_peak > peak_ceiling:
                if strict_lufs_matching:
                    predicted_status = 'NEEDS_LIMITING'
                    reason = 'would_exceed_peak_ceiling'
                else:
                    # Drift: gain capped so peak stays at ceiling
                    orig_peak = _input_true_peak_db if _use_streaming else measure_true_peak(data, rate)
                    capped_gain = min(gain_db, peak_ceiling - orig_peak)
                    predicted_peak = orig_peak + capped_gain
                    gain_db = capped_gain
                    predicted_status = 'OK_UNDERSHOOT'
                    reason = 'peak_limited'
            else:
                predicted_status = 'OK'
                reason = 'ok'

            log('info', f"DRY RUN: {audio_path.name} | "
                f"Gain: {gain_db:+.1f}dB | Predicted Peak: {predicted_peak:.1f}dBTP | "
                f"{predicted_status}")
            return {
                'type': 'dry_run',
                'filename': audio_path.name,
                'result': {
                    'filename': audio_path.name,
                    'original_lufs': round(original_lufs, 2),
                    'target_lufs': target_lufs,
                    'gain_needed_db': round(gain_db, 2),
                    'predicted_peak_dBTP': round(predicted_peak, 2),
                    'predicted_status': predicted_status,
                    'lra_lu': lra_lu if lra_lu is not None else '',
                    'reason': reason,
                },
                'output_file': None,
                'log_messages': log_messages,
            }

        # Check if file would exceed peak ceiling
        if predicted_peak > peak_ceiling:
            if strict_lufs_matching:
                # STRICT LUFS MODE: Skip file, copy to needs_limiting/
                needs_limiting_path.mkdir(parents=True, exist_ok=True)
                dest_file = needs_limiting_path / audio_path.name
                shutil.copy2(str(audio_path), str(dest_file))

                log('error', f"NEEDS LIMITING: {audio_path.name} | "
                    f"Would peak at {predicted_peak:.1f}dBTP (ceiling: {peak_ceiling}dBTP) | "
                    f"Copied to needs_limiting/")

                return {
                    'type': 'needs_limiting',
                    'filename': audio_path.name,
                    'skipped': {
                        'filename': audio_path.name,
                        'original_lufs': round(original_lufs, 2),
                        'predicted_peak_dBTP': round(predicted_peak, 2),
                        'gain_needed_db': round(gain_db, 2),
                        'lra_lu': lra_lu if lra_lu is not None else '',
                        'reason': 'would_exceed_peak_ceiling'
                    },
                    'output_file': str(dest_file),
                    'log_messages': log_messages,
                }
            else:
                # DRIFT MODE: Reduce gain to protect peak ceiling
                original_peak = _input_true_peak_db if _use_streaming else measure_true_peak(data, rate)
                headroom = peak_ceiling - original_peak
                max_safe_gain_db = headroom
                actual_gain_db = min(gain_db, max_safe_gain_db)
                actual_gain_linear = 10 ** (actual_gain_db / 20)

                if not _use_streaming:
                    normalized_data = data * actual_gain_linear

                log('warning', f"PEAK LIMITED: {audio_path.name} | "
                    f"Gain reduced from {gain_db:+.1f}dB to {actual_gain_db:+.1f}dB to protect peak")

                gain_db = actual_gain_db
                gain_linear = actual_gain_linear
        else:
            if not _use_streaming:
                normalized_data = data * gain_linear

        # Sample rate conversion (downsampling only; not supported in streaming mode)
        output_rate = rate
        if sample_rate != 'preserve':
            target_rate = int(sample_rate.split()[0])

            if target_rate > rate:
                log('error', f"BLOCKED: {audio_path.name} | Cannot upsample {rate}Hz to {target_rate}Hz")
                return {
                    'type': 'blocked',
                    'filename': audio_path.name,
                    'error': {
                        'filename': audio_path.name,
                        'error': f'Cannot upsample {rate}Hz to {target_rate}Hz',
                        'status': 'BLOCKED',
                        'reason': 'upsample_blocked'
                    },
                    'output_file': None,
                    'log_messages': log_messages,
                }
            elif target_rate < rate:
                if _use_streaming:
                    log('error', f"BLOCKED: {audio_path.name} | "
                        f"Sample rate conversion not supported for large files (streaming mode). "
                        f"Convert rate separately or process without --rate flag.")
                    return {
                        'type': 'blocked',
                        'filename': audio_path.name,
                        'error': {
                            'filename': audio_path.name,
                            'error': f'SRC not supported in streaming mode',
                            'status': 'BLOCKED',
                            'reason': 'src_not_supported_in_streaming_mode'
                        },
                        'output_file': None,
                        'log_messages': log_messages,
                    }
                try:
                    import soxr
                    normalized_data = soxr.resample(
                        normalized_data, rate, target_rate, quality='VHQ'
                    )
                    output_rate = target_rate
                    log('info', f"  Resampled: {rate}Hz -> {target_rate}Hz (SOXR VHQ)")
                except ImportError:
                    try:
                        from scipy import signal as _scipy_signal
                        n_out = int(len(normalized_data) * target_rate / rate)
                        if normalized_data.ndim == 1:
                            normalized_data = _scipy_signal.resample(normalized_data, n_out)
                        else:
                            resampled_channels = [
                                _scipy_signal.resample(normalized_data[:, ch], n_out)
                                for ch in range(normalized_data.shape[1])
                            ]
                            normalized_data = np.column_stack(resampled_channels)
                        output_rate = target_rate
                        log('info', f"  Resampled: {rate}Hz -> {target_rate}Hz (scipy, SOXR not installed)")
                    except ImportError:
                        log('error', f"BLOCKED: {audio_path.name} | "
                            f"Sample rate conversion requires SOXR or scipy "
                            f"(pip install soxr  or  pip install scipy)")
                        return {
                            'type': 'blocked',
                            'filename': audio_path.name,
                            'error': {
                                'filename': audio_path.name,
                                'error': 'SRC requires soxr or scipy — neither installed',
                                'status': 'BLOCKED',
                                'reason': 'src_missing_dependency'
                            },
                            'output_file': None,
                            'log_messages': log_messages,
                        }

        # Determine output bit depth
        if bit_depth == 'preserve':
            if 'PCM_16' in original_format:
                output_subtype = 'PCM_16'
                output_bits = 16
            elif 'PCM_24' in original_format:
                output_subtype = 'PCM_24'
                output_bits = 24
            elif 'PCM_32' in original_format or 'FLOAT' in original_format:
                output_subtype = 'PCM_32'
                output_bits = 32
            else:
                output_subtype = 'PCM_24'
                output_bits = 24
        elif bit_depth == '16':
            output_subtype = 'PCM_16'
            output_bits = 16
        elif bit_depth == '24':
            output_subtype = 'PCM_24'
            output_bits = 24
        else:
            output_subtype = 'PCM_32'
            output_bits = 32

        # Export with smart filename
        output_filename = get_output_filename(audio_path.name, target_lufs)
        output_file = normalized_path / output_filename

        # Prevent overwriting the source file (e.g. flat output mode with same dir)
        if output_file.resolve() == audio_path.resolve():
            output_file = normalized_path / (Path(output_filename).stem + '_norm' + Path(output_filename).suffix)

        normalized_path.mkdir(parents=True, exist_ok=True)

        if _use_streaming:
            # Large file: apply gain → clip → dither → write in chunks, then
            # re-measure the output file with the same streaming approach.
            final_lufs, final_true_peak, output_lra = write_normalized_streaming(
                audio_path, output_file, gain_linear,
                output_rate, output_subtype, output_bits, rng,
            )
        else:
            # Safety clip to full-scale BEFORE dither. Clipping after dither re-introduces
            # the correlated quantization error that dither exists to mask.
            normalized_data = np.clip(normalized_data, -1.0, 1.0)

            # Apply TPDF dithering for bit depth reduction. Dither adds ~1 LSB of noise;
            # samples right at ±1.0 may round to the PCM extreme during sf.write's
            # quantization, which is harmless and is the expected behavior.
            if output_bits < 32:
                normalized_data = apply_tpdf_dither(normalized_data, output_bits, rng=rng)

            output_format = 'AIFF' if output_file.suffix.lower() in ('.aif', '.aiff') else 'WAV'
            sf.write(str(output_file), normalized_data, output_rate, subtype=output_subtype,
                     format=output_format)

            # Measure final values from the written file, not the in-memory float buffer.
            # This reflects actual post-quantization loudness / peak that consumers will hear.
            written_data, written_rate = sf.read(str(output_file))
            final_true_peak = measure_true_peak(written_data, written_rate)
            final_meter = pyln.Meter(written_rate)
            if written_data.ndim == 1:
                final_lufs = final_meter.integrated_loudness(written_data.reshape(-1, 1))
            else:
                final_lufs = final_meter.integrated_loudness(written_data)
            output_lra = measure_lra(written_data, written_rate)

        # Post-processing peak safety net (strict mode only). The pre-SRC ceiling
        # check used the true peak at the ORIGINAL rate/bit-depth; sample-rate
        # conversion AND bit-depth dithering/quantization can each raise the
        # measured true peak. If the written file's true peak now exceeds the
        # ceiling, discard the processed output and copy the ORIGINAL source into
        # needs_limiting/ — same raw-source contract as the pre-SRC path, so
        # everything in needs_limiting/ is an unprocessed file ready to be
        # manually limited and reprocessed. src_converted distinguishes the two
        # causes; the 0.05 dBTP tolerance absorbs measurement noise.
        src_converted = output_rate != rate
        if strict_lufs_matching and final_true_peak > peak_ceiling + 0.05:
            reason = 'exceeded_post_src' if src_converted else 'exceeded_post_dither'
            cause = (f"{rate}->{output_rate}Hz conversion" if src_converted
                     else "bit-depth dither/quantization")
            needs_limiting_path.mkdir(parents=True, exist_ok=True)
            nl_dest = needs_limiting_path / audio_path.name
            shutil.copy2(str(audio_path), str(nl_dest))
            output_file.unlink(missing_ok=True)  # discard the over-ceiling processed output

            log('error', f"NEEDS LIMITING (post-processing): {audio_path.name} | "
                f"Measured {final_true_peak:.1f}dBTP after {cause} "
                f"(ceiling: {peak_ceiling}dBTP) | Source copied to needs_limiting/")

            return {
                'type': 'needs_limiting',
                'filename': audio_path.name,
                'skipped': {
                    'filename': audio_path.name,
                    'original_lufs': round(original_lufs, 2),
                    'predicted_peak_dBTP': round(final_true_peak, 2),
                    'gain_needed_db': round(gain_db, 2),
                    'lra_lu': output_lra if output_lra is not None else '',
                    'reason': reason,
                },
                'output_file': str(nl_dest),
                'log_messages': log_messages,
            }

        # Embed BWF metadata if requested (WAV only)
        if embed_bwf and output_file.suffix.lower() == '.wav':
            bext_meta = {
                'description': f"Normalized to {target_lufs} LUFS by LUFS Normalizer v{VERSION}",
                'originator': 'LUFS Normalizer',
                'originator_reference': f'LN{VERSION.replace(".", "")}',
                'loudness_value': final_lufs,
                'loudness_range': output_lra,
                'max_true_peak': final_true_peak,
            }
            inject_bext_chunk(str(output_file), bext_meta)

            ixml = build_ixml_for_normalization(target_lufs, round(final_lufs, 2),
                                                 output_lra, round(final_true_peak, 2), VERSION)
            inject_ixml_chunk(str(output_file), ixml)

        # Determine status
        lufs_undershoot = abs(final_lufs - target_lufs) > 0.5
        # Drift mode always ships the file (no relocation), but if SRC or dither
        # pushed the final measured true peak over the ceiling despite the pre-SRC
        # gain cap, report it honestly instead of OK/OK_UNDERSHOOT. Same +0.05 dBTP
        # tolerance and src_converted split as the strict-mode safety net above.
        # (Strict mode never reaches here over-ceiling — it returned/relocated.)
        drift_peak_exceeded = (not strict_lufs_matching
                               and final_true_peak > peak_ceiling + 0.05)
        if drift_peak_exceeded:
            if src_converted:
                status = 'OK_PEAK_EXCEEDED_POST_SRC'
                reason = 'exceeded_post_src'
            else:
                status = 'OK_PEAK_EXCEEDED_POST_DITHER'
                reason = 'exceeded_post_dither'
            log('warning', f"PEAK EXCEEDED (drift): {audio_path.name} | "
                f"Measured {final_true_peak:.1f}dBTP > ceiling {peak_ceiling}dBTP "
                f"after {'SRC' if src_converted else 'dither/quantization'} | "
                f"Shipped as-is (drift mode)")
        elif lufs_undershoot and not strict_lufs_matching:
            status = 'OK_UNDERSHOOT'
            reason = 'peak_limited'
            log('info', f"SUCCESS (UNDERSHOOT): {audio_path.name} | "
                f"Gain: {gain_db:+.1f}dB | Peak: {final_true_peak:.1f}dBTP | "
                f"LUFS: {final_lufs:.1f} (target: {target_lufs})")
        else:
            status = 'OK'
            reason = 'ok'
            log('info', f"SUCCESS: {audio_path.name} | "
                f"Gain: {gain_db:+.1f}dB | Peak: {final_true_peak:.1f}dBTP | "
                f"LUFS: {final_lufs:.1f}")

        return {
            'type': 'success',
            'filename': audio_path.name,
            'result': {
                'filename': audio_path.name,
                'status': status,
                'reason': reason,
                'sample_rate': output_rate,
                'bit_depth': output_bits,
                'original_lufs': round(original_lufs, 2),
                'target_lufs': target_lufs,
                'final_lufs': round(final_lufs, 2),
                'gain_applied_db': round(gain_db, 2),
                'true_peak_dBTP': round(final_true_peak, 2),
                'lra_lu': output_lra if output_lra is not None else '',
            },
            'output_file': str(output_file),
            'log_messages': log_messages,
        }

    except Exception as e:
        log('error', f"FAILED: {audio_path.name} | {str(e)}")
        return {
            'type': 'error',
            'filename': audio_path.name,
            'error': {
                'filename': audio_path.name,
                'error': str(e),
                'status': 'FAILED',
                'reason': 'exception'
            },
            'output_file': None,
            'log_messages': log_messages,
        }
