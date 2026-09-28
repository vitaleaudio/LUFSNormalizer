"""
LUFSNormalizer batch orchestration engine.

Supports both sequential and parallel processing modes.
"""

import logging
import csv
import os
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

from .processor import process_single_file
from .. import VERSION

logger = logging.getLogger('lufs_normalizer')


class LUFSNormalizer:
    """
    Professional LUFS batch normalizer with broadcast-grade features.

    Supports sequential and parallel (ProcessPoolExecutor) processing,
    LRA measurement, BWF metadata injection, and detailed CSV reporting.
    """

    def __init__(self, config_path=None):
        self.config_path = config_path
        self.progress_callback = None
        self.result_callback = None
        self.stop_requested = False
        self.results = []
        self.errors = []
        self.skipped_files = []
        self.skipped_silent = []
        self.dry_run_results = []

    def set_progress_callback(self, callback):
        """Set callback for progress updates: callback(current, total, filename)"""
        self.progress_callback = callback

    def set_result_callback(self, callback):
        """Set callback for result updates: callback(filename, status, details)"""
        self.result_callback = callback

    def request_stop(self):
        """Request graceful stop of batch processing."""
        self.stop_requested = True
        if hasattr(self, '_stop_event') and self._stop_event is not None:
            self._stop_event.set()

    def _find_audio_files(self, input_dir, recursive=False):
        """Find WAV and AIFF files in input directory, optionally recursing into subdirs."""
        input_path = Path(input_dir)
        glob_fn = input_path.rglob if recursive else input_path.glob
        seen = set()
        files = []
        for pattern in ('*.wav', '*.WAV', '*.aiff', '*.AIFF', '*.aif', '*.AIF'):
            for f in glob_fn(pattern):
                resolved = f.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(f)
        return sorted(files)

    def _setup_output_dirs(self, output_dir, target_lufs, use_batch_folders, strict_lufs_matching=True):
        """Create output directory structure. Returns (batch_path, normalized_path, needs_limiting_path, logs_path)."""
        output_path = Path(output_dir)

        if use_batch_folders:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            mode = 'strict' if strict_lufs_matching else 'drift'
            batch_name = f"batch_{timestamp}_{int(target_lufs)}LUFS_{mode}"
            batch_path = output_path / batch_name
            normalized_path = batch_path / 'normalized'
            needs_limiting_path = batch_path / 'needs_limiting'
            logs_path = batch_path / 'logs'
        else:
            batch_path = output_path
            normalized_path = output_path
            needs_limiting_path = output_path / 'needs_limiting'
            logs_path = output_path

        normalized_path.mkdir(parents=True, exist_ok=True)
        return batch_path, normalized_path, needs_limiting_path, logs_path

    def _setup_logging(self, logs_path, generate_log):
        """Configure logging handlers. Returns log_file path or None."""
        log_file = logs_path / 'processing.log' if generate_log else None

        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        if generate_log:
            logs_path.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        return log_file

    def _process_result(self, result, idx, total_files, report_name=None):
        """Handle a single file result from process_single_file().

        report_name, if given, replaces the filename in CSV-bound records.
        """
        record_key = {'success': 'result', 'needs_limiting': 'skipped'}.get(result['type'])
        if report_name is not None and record_key:
            result[record_key]['filename'] = report_name

        # Replay log messages
        for level, msg in result.get('log_messages', []):
            prefixed = f"[{idx}/{total_files}] {msg}"
            if level == 'error':
                logger.error(prefixed)
            elif level == 'warning':
                logger.warning(prefixed)
            else:
                logger.info(prefixed)

        filename = result['filename']
        rtype = result['type']

        if rtype == 'success':
            self.results.append(result['result'])
            status = result['result']['status']
            if status == 'OK_UNDERSHOOT':
                final_lufs = result['result']['final_lufs']
                target = result['result']['target_lufs']
                if self.result_callback:
                    self.result_callback(filename, 'SUCCESS_UNDERSHOOT',
                                         f'LUFS: {final_lufs:.1f} (target was {target})')
            else:
                gain = result['result']['gain_applied_db']
                peak = result['result']['true_peak_dBTP']
                if self.result_callback:
                    self.result_callback(filename, 'SUCCESS',
                                         f'Gain: {gain:+.1f}dB | Peak: {peak:.1f}dBTP')

        elif rtype == 'needs_limiting':
            self.skipped_files.append(result['skipped'])
            peak = result['skipped']['predicted_peak_dBTP']
            if self.result_callback:
                self.result_callback(filename, 'NEEDS_LIMITING',
                                     f'Would peak at {peak:.1f}dBTP')

        elif rtype == 'skipped':
            self.skipped_silent.append(result['error'])
            if self.result_callback:
                self.result_callback(filename, 'SKIPPED', 'Too quiet/silent')

        elif rtype == 'blocked':
            self.errors.append(result['error'])
            error_msg = result['error']['error']
            if self.result_callback:
                self.result_callback(filename, 'BLOCKED', error_msg)

        elif rtype == 'dry_run':
            self.dry_run_results.append(result['result'])
            status = result['result']['predicted_status']
            gain = result['result']['gain_needed_db']
            peak = result['result']['predicted_peak_dBTP']
            if self.result_callback:
                self.result_callback(filename, f'DRY_RUN_{status}',
                                     f'Gain: {gain:+.1f}dB | Peak: {peak:.1f}dBTP')

        elif rtype == 'error':
            self.errors.append(result['error'])
            error_msg = result['error']['error']
            if self.result_callback:
                self.result_callback(filename, 'FAILED', error_msg)

    def _write_reports(self, logs_path, generate_csv):
        """Write CSV reports for results and skipped files. Returns csv_path or None."""
        csv_path = None
        if generate_csv and self.results:
            logs_path.mkdir(parents=True, exist_ok=True)
            csv_path = logs_path / 'normalization_report.csv'
            with open(csv_path, 'w', newline='') as f:
                fieldnames = ['filename', 'status', 'reason', 'sample_rate', 'bit_depth',
                              'original_lufs', 'target_lufs', 'final_lufs', 'gain_applied_db',
                              'true_peak_dBTP', 'lra_lu']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.results)

        if generate_csv and self.skipped_files:
            logs_path.mkdir(parents=True, exist_ok=True)
            skipped_csv = logs_path / 'needs_limiting_report.csv'
            with open(skipped_csv, 'w', newline='') as f:
                fieldnames = ['filename', 'original_lufs', 'predicted_peak_dBTP',
                              'gain_needed_db', 'lra_lu', 'reason']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.skipped_files)

        if generate_csv and self.dry_run_results:
            logs_path.mkdir(parents=True, exist_ok=True)
            dry_csv = logs_path / 'dry_run_report.csv'
            with open(dry_csv, 'w', newline='') as f:
                fieldnames = ['filename', 'original_lufs', 'target_lufs',
                              'gain_needed_db', 'predicted_peak_dBTP',
                              'predicted_status', 'lra_lu', 'reason']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.dry_run_results)
            if csv_path is None:
                csv_path = dry_csv

        return csv_path

    def _log_summary(self, total_files, normalized_path, needs_limiting_path):
        """Log the final batch summary."""
        success_count = len(self.results)
        skipped_count = len(self.skipped_files)
        silent_count = len(self.skipped_silent)
        error_count = len(self.errors)

        logger.info("=" * 70)
        logger.info("BATCH COMPLETE")
        logger.info(f"Processed: {success_count}/{total_files} files")

        if skipped_count > 0:
            logger.warning(f"NEEDS LIMITING: {skipped_count} files exceeded peak ceiling")
            logger.warning(f"  -> Copied to: {needs_limiting_path}")
            logger.warning(f"  -> Apply a limiter in your DAW, then re-process")

        if silent_count > 0:
            logger.info(f"Skipped: {silent_count} silent/too-quiet files")

        if error_count > 0:
            logger.error(f"Errors: {error_count} files failed")

        logger.info(f"Output: {normalized_path}")
        logger.info("=" * 70)

    def normalize_batch(self, input_dir, output_dir, target_lufs=-23.0, peak_ceiling=-1.0,
                        bit_depth='preserve', sample_rate='preserve',
                        use_batch_folders=True, generate_log=True, generate_csv=True,
                        strict_lufs_matching=True, embed_bwf=False, recursive=False,
                        dry_run=False):
        """
        Normalize a batch of audio files sequentially.

        When recursive=True, subdirectories are scanned and the output mirrors
        the input folder hierarchy under normalized_path.
        When dry_run=True, files are measured but no output is written; a
        dry_run_report.csv is produced showing predicted gain and peak values.

        Returns:
            tuple: (success_count, total_count, log_path, csv_path, output_path)
        """
        self.stop_requested = False
        self.results = []
        self.errors = []
        self.skipped_files = []
        self.skipped_silent = []
        self.dry_run_results = []

        input_path = Path(input_dir)
        audio_files = self._find_audio_files(input_dir, recursive=recursive)
        if not audio_files:
            logger.warning("No WAV or AIFF files found in input directory")
            return 0, 0, None, None, None

        total_files = len(audio_files)
        batch_path, normalized_path, needs_limiting_path, logs_path = \
            self._setup_output_dirs(output_dir, target_lufs, use_batch_folders, strict_lufs_matching)
        log_file = self._setup_logging(logs_path, generate_log)

        # Log header
        logger.info("=" * 70)
        logger.info(f"LUFS NORMALIZER v{VERSION} - BATCH PROCESSING")
        logger.info("=" * 70)
        logger.info(f"Target LUFS: {target_lufs}")
        logger.info(f"Peak Ceiling: {peak_ceiling} dBTP")
        logger.info(f"Bit Depth: {bit_depth}")
        logger.info(f"Sample Rate: {sample_rate}")
        logger.info(f"BWF Metadata: {'Yes' if embed_bwf else 'No'}")
        logger.info(f"Recursive: {'Yes' if recursive else 'No'}")
        if dry_run:
            logger.info("*** DRY RUN — measuring only, no output files will be written ***")
        logger.info(f"Files to process: {total_files}")
        logger.info("-" * 70)

        # Process each file sequentially
        for idx, audio_path in enumerate(audio_files, 1):
            if self.stop_requested:
                logger.info("Processing stopped by user")
                break

            if self.progress_callback:
                self.progress_callback(idx, total_files, audio_path.name)

            # Mirror subdirectory structure when running recursively
            if recursive:
                rel_parent = audio_path.relative_to(input_path).parent
                file_normalized_path = normalized_path / rel_parent
                file_needs_limiting_path = needs_limiting_path / rel_parent
            else:
                file_normalized_path = normalized_path
                file_needs_limiting_path = needs_limiting_path

            result = process_single_file(
                audio_path=str(audio_path),
                target_lufs=target_lufs,
                peak_ceiling=peak_ceiling,
                strict_lufs_matching=strict_lufs_matching,
                bit_depth=bit_depth,
                sample_rate=sample_rate,
                normalized_path=str(file_normalized_path),
                needs_limiting_path=str(file_needs_limiting_path),
                embed_bwf=embed_bwf,
                rng_seed=idx,
                dry_run=dry_run,
            )
            report_name = audio_path.relative_to(input_path).as_posix() if recursive else None
            self._process_result(result, idx, total_files, report_name)

        csv_path = self._write_reports(logs_path, generate_csv)
        self._log_summary(total_files, normalized_path, needs_limiting_path)

        success_count = len(self.results)
        return (success_count, total_files,
                str(log_file) if log_file else None,
                str(csv_path) if csv_path else None,
                str(normalized_path))

    def normalize_batch_parallel(self, input_dir, output_dir, target_lufs=-23.0,
                                  peak_ceiling=-1.0, bit_depth='preserve',
                                  sample_rate='preserve', use_batch_folders=True,
                                  generate_log=True, generate_csv=True,
                                  strict_lufs_matching=True, embed_bwf=False,
                                  max_workers=None, recursive=False, dry_run=False):
        """
        Normalize a batch of audio files in parallel using ProcessPoolExecutor.

        Args:
            max_workers: Number of parallel workers (default: CPU count)
            recursive: Scan subdirectories and mirror hierarchy in output
            dry_run: Measure only — no output written, dry_run_report.csv produced
            (all other args same as normalize_batch)

        Returns:
            tuple: (success_count, total_count, log_path, csv_path, output_path)
        """
        self.stop_requested = False
        self._stop_event = multiprocessing.Event()
        self.results = []
        self.errors = []
        self.skipped_files = []
        self.skipped_silent = []
        self.dry_run_results = []

        input_path = Path(input_dir)
        audio_files = self._find_audio_files(input_dir, recursive=recursive)
        if not audio_files:
            logger.warning("No WAV or AIFF files found in input directory")
            return 0, 0, None, None, None

        total_files = len(audio_files)
        if max_workers is None:
            max_workers = os.cpu_count() or 4

        batch_path, normalized_path, needs_limiting_path, logs_path = \
            self._setup_output_dirs(output_dir, target_lufs, use_batch_folders, strict_lufs_matching)
        log_file = self._setup_logging(logs_path, generate_log)

        logger.info("=" * 70)
        logger.info(f"LUFS NORMALIZER v{VERSION} - PARALLEL BATCH PROCESSING")
        logger.info("=" * 70)
        logger.info(f"Target LUFS: {target_lufs}")
        logger.info(f"Peak Ceiling: {peak_ceiling} dBTP")
        logger.info(f"Workers: {max_workers}")
        logger.info(f"BWF Metadata: {'Yes' if embed_bwf else 'No'}")
        logger.info(f"Recursive: {'Yes' if recursive else 'No'}")
        if dry_run:
            logger.info("*** DRY RUN — measuring only, no output files will be written ***")
        logger.info(f"Files to process: {total_files}")
        logger.info("-" * 70)

        completed = 0
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_info = {}
            for idx, audio_path in enumerate(audio_files, 1):
                if recursive:
                    rel_parent = audio_path.relative_to(input_path).parent
                    file_normalized_path = normalized_path / rel_parent
                    file_needs_limiting_path = needs_limiting_path / rel_parent
                else:
                    file_normalized_path = normalized_path
                    file_needs_limiting_path = needs_limiting_path

                future = executor.submit(
                    process_single_file,
                    audio_path=str(audio_path),
                    target_lufs=target_lufs,
                    peak_ceiling=peak_ceiling,
                    strict_lufs_matching=strict_lufs_matching,
                    bit_depth=bit_depth,
                    sample_rate=sample_rate,
                    normalized_path=str(file_normalized_path),
                    needs_limiting_path=str(file_needs_limiting_path),
                    embed_bwf=embed_bwf,
                    rng_seed=idx,
                    dry_run=dry_run,
                )
                future_to_info[future] = (idx, audio_path)

            # Collect results as they complete
            for future in as_completed(future_to_info):
                if self.stop_requested:
                    # Cancel remaining futures
                    for f in future_to_info:
                        f.cancel()
                    logger.info("Processing stopped by user")
                    break

                idx, audio_path = future_to_info[future]
                completed += 1

                if self.progress_callback:
                    self.progress_callback(completed, total_files, audio_path.name)

                try:
                    result = future.result()
                    report_name = (audio_path.relative_to(input_path).as_posix()
                                   if recursive else None)
                    self._process_result(result, completed, total_files, report_name)
                except Exception as e:
                    logger.error(f"[{completed}/{total_files}] Worker error for {audio_path.name}: {e}")
                    self.errors.append({
                        'filename': audio_path.name,
                        'error': str(e),
                        'status': 'FAILED',
                        'reason': 'worker_exception'
                    })
                    if self.result_callback:
                        self.result_callback(audio_path.name, 'FAILED', str(e))

        csv_path = self._write_reports(logs_path, generate_csv)
        self._log_summary(total_files, normalized_path, needs_limiting_path)

        self._stop_event = None
        success_count = len(self.results)
        return (success_count, total_files,
                str(log_file) if log_file else None,
                str(csv_path) if csv_path else None,
                str(normalized_path))
