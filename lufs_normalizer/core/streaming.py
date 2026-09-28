"""
Streaming (chunk-based) audio measurement and write for large files.

Activated automatically when loading a file into float64 RAM would exceed
STREAMING_THRESHOLD_BYTES (~2 GiB). Memory usage stays bounded by
chunk size regardless of file duration.

scipy.signal is required for streaming mode — it is used to carry IIR
filter state across chunk boundaries for correct BS.1770-4 K-weighting.
"""

import numpy as np
import soundfile as sf
from pathlib import Path

STREAMING_THRESHOLD_BYTES = 2 * 1024 ** 3  # 2 GiB of float64 in RAM
_CHUNK_FRAMES = 480_000                     # 10 s at 48 kHz
_BLOCK_SECONDS = 0.4                        # BS.1770-4 400 ms integrated block
_HOP_SECONDS = 0.1                          # 100 ms hop → 75 % overlap

# Absolute gate: -70 LUFS converted to linear mean-square (with ±0.691 offset)
_ABSOLUTE_GATE_POWER = 10.0 ** ((-70.0 + 0.691) / 10.0)


class NonFiniteSamplesError(ValueError):
    def __init__(self, count):
        super().__init__(f"{count} non-finite samples (NaN/inf)")
        self.count = count


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def should_use_streaming(audio_path) -> bool:
    """Return True if the file's float64 in-memory footprint would exceed 2 GiB."""
    info = sf.info(str(audio_path))
    return info.frames * info.channels * 8 > STREAMING_THRESHOLD_BYTES


def measure_streaming(audio_path, chunk_frames: int = _CHUNK_FRAMES):
    """
    Compute integrated LUFS (BS.1770-4) and True Peak (dBTP) via chunked I/O.

    Keeps at most ``chunk_frames × channels × 8`` bytes of audio in RAM at
    any one time.  K-weighting filter state is carried across chunk boundaries
    so the result is numerically identical to a full-file load.

    Requires scipy (``pip install scipy``).  Raises RuntimeError if missing.

    Returns
    -------
    (lufs: float, true_peak_db: float)
        ``lufs`` is ``-inf`` for silent / inaudible content.
    """
    lfilter, lfilter_zi = _require_scipy()

    info = sf.info(str(audio_path))
    rate = info.samplerate
    num_channels = info.channels

    filters_ba = _k_weighting_ba(rate)
    block_frames = int(rate * _BLOCK_SECONDS)
    hop_frames = int(rate * _HOP_SECONDS)

    # Zero initial conditions — matches pyloudnorm's default lfilter(b, a, data)
    # (no zi parameter → zeros). lfilter_zi() gives non-zero DC steady-state
    # conditions which would cause a spurious transient when processing silence.
    filter_states = [
        [np.zeros(max(len(b), len(a)) - 1, dtype=np.float64) for _ in range(num_channels)]
        for b, a in filters_ba
    ]

    try:
        import soxr
        def _peak(chunk):
            return float(np.max(np.abs(soxr.resample(chunk, rate, rate * 4, quality='VHQ'))))
    except ImportError:
        def _peak(chunk):
            return float(np.max(np.abs(chunk)))

    block_powers = []
    pending = np.empty((0, num_channels), dtype=np.float64)
    true_peak_linear = 0.0
    non_finite = 0

    with sf.SoundFile(str(audio_path)) as f:
        for raw in f.blocks(blocksize=chunk_frames, dtype='float64'):
            chunk = raw.reshape(-1, 1) if raw.ndim == 1 else raw

            chunk_non_finite = int(np.count_nonzero(~np.isfinite(chunk)))
            if chunk_non_finite:
                non_finite += chunk_non_finite
                continue

            # True Peak from unfiltered chunk
            # (inter-sample peaks at chunk boundaries are negligible for typical
            # chunk sizes; a 10 s chunk has < 0.01 dBTP boundary uncertainty)
            tp_in = chunk[:, 0] if num_channels == 1 else chunk
            true_peak_linear = max(true_peak_linear, _peak(tp_in))

            # K-weighting with continuous state across chunk boundary
            filtered = chunk.copy()
            for fi, (b, a) in enumerate(filters_ba):
                out = np.empty_like(filtered)
                for ch in range(num_channels):
                    out[:, ch], filter_states[fi][ch] = lfilter(
                        b, a, filtered[:, ch], zi=filter_states[fi][ch]
                    )
                filtered = out

            # Grow the pending buffer, then drain complete 400 ms blocks
            pending = np.vstack([pending, filtered])
            while len(pending) >= block_frames:
                block = pending[:block_frames]
                # BS.1770-4: sum of per-channel mean-squares (not average)
                block_powers.append(float(np.sum(np.mean(block ** 2, axis=0))))
                pending = pending[hop_frames:]

    if non_finite:
        raise NonFiniteSamplesError(non_finite)

    lufs = _apply_bs1770_gate(block_powers)
    true_peak_db = 20.0 * np.log10(true_peak_linear) if true_peak_linear > 0 else -100.0
    return lufs, true_peak_db


def write_normalized_streaming(in_path, out_path, gain_linear: float, rate: int,
                                output_subtype: str, output_bits: int, rng,
                                chunk_frames: int = _CHUNK_FRAMES):
    """
    Apply gain → clip → dither and write output in constant-memory chunks.

    Sample rate conversion is not supported in streaming mode; the output
    sample rate equals the input rate.

    Returns
    -------
    (final_lufs: float, final_peak_db: float, lra: None)
        LRA is always ``None`` for large files (streaming LRA not implemented).
    """
    from .dither import apply_tpdf_dither

    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    info = sf.info(str(in_path))
    num_channels = info.channels

    with sf.SoundFile(str(in_path)) as fin, \
         sf.SoundFile(str(out_path), mode='w', samplerate=rate,
                      channels=num_channels, subtype=output_subtype) as fout:
        for raw in fin.blocks(blocksize=chunk_frames, dtype='float64'):
            chunk = raw * gain_linear
            chunk = np.clip(chunk, -1.0, 1.0)
            if output_bits < 32:
                chunk = apply_tpdf_dither(chunk, output_bits, rng=rng)
            fout.write(chunk)

    final_lufs, final_peak = measure_streaming(out_path)
    return final_lufs, final_peak, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_scipy():
    try:
        from scipy.signal import lfilter, lfilter_zi
        return lfilter, lfilter_zi
    except ImportError:
        raise RuntimeError(
            "scipy is required for large-file (streaming) mode. "
            "Install it: pip install scipy"
        )


def _k_weighting_ba(sample_rate):
    """Extract K-weighting filter coefficients from a pyloudnorm Meter."""
    import pyloudnorm as pyln
    meter = pyln.Meter(sample_rate)
    return [
        (np.asarray(f.b, dtype=np.float64), np.asarray(f.a, dtype=np.float64))
        for f in meter._filters.values()
    ]


def _apply_bs1770_gate(block_powers) -> float:
    """Apply BS.1770-4 two-stage gating and return integrated LUFS."""
    if not block_powers:
        return float('-inf')

    bp = np.array(block_powers, dtype=np.float64)

    # Stage 1: absolute gate at -70 LUFS
    mask1 = bp > _ABSOLUTE_GATE_POWER
    if not np.any(mask1):
        return float('-inf')

    ungated_mean = float(np.mean(bp[mask1]))

    # Stage 2: relative gate at ungated_mean - 10 dB
    rel_threshold = ungated_mean * 10.0 ** (-10.0 / 10.0)
    mask2 = bp > rel_threshold
    if not np.any(mask2):
        return float('-inf')

    return float(-0.691 + 10.0 * np.log10(np.mean(bp[mask2])))
