"""
Regression tests for v3.1.3:
- needs_limiting/ mirrors the recursive subfolder structure, so same-named
  files from different subfolders no longer overwrite each other.
- Files containing NaN or inf samples fail with reason non_finite_samples and
  produce no output, while all-zero files are still skipped as silent.
"""

import csv

import numpy as np
import pytest
import soundfile as sf

import lufs_normalizer.core.streaming as streaming_mod
from lufs_normalizer.core.engine import LUFSNormalizer
from lufs_normalizer.core.processor import process_single_file


SR = 48000
DURATION = 4.0


def _write_peaky(path, quiet_amplitude):
    """Quiet sine with a near-full-scale spike: any loud target needs limiting."""
    n = int(DURATION * SR)
    t = np.arange(n) / SR
    data = quiet_amplitude * np.sin(2 * np.pi * 1000 * t)
    spike_n = int(0.01 * SR)
    data[n // 2:n // 2 + spike_n] = 0.95 * np.sin(2 * np.pi * 1000 * np.arange(spike_n) / SR)
    sf.write(str(path), np.column_stack([data, data]), SR, subtype='PCM_24')


def _write_float(path, bad_value=None, bad_count=100):
    """Float32 WAV: a quiet sine with `bad_count` samples per channel set to bad_value,
    or all zeros when bad_value is None."""
    n = int(DURATION * SR)
    if bad_value is None:
        data = np.zeros(n)
    else:
        data = 0.1 * np.sin(2 * np.pi * 1000 * np.arange(n) / SR)
        data[n // 2:n // 2 + bad_count] = bad_value
    sf.write(str(path), np.column_stack([data, data]).astype(np.float32), SR, subtype='FLOAT')


def _write_good(path):
    n = int(DURATION * SR)
    data = 0.1 * np.sin(2 * np.pi * 1000 * np.arange(n) / SR)
    sf.write(str(path), np.column_stack([data, data]), SR, subtype='PCM_24')


# ---------------------------------------------------------------------------
# Bug A: needs_limiting/ collisions in recursive strict mode
# ---------------------------------------------------------------------------

class TestNeedsLimitingMirrorsSubfolders:
    @pytest.fixture
    def same_named_input(self, tmp_path):
        root = tmp_path / 'in'
        (root / 'a').mkdir(parents=True)
        (root / 'b').mkdir(parents=True)
        _write_peaky(root / 'a' / 'tone.wav', quiet_amplitude=0.005)
        _write_peaky(root / 'b' / 'tone.wav', quiet_amplitude=0.02)
        return root

    def _assert_both_preserved(self, root, out):
        nl = out / 'needs_limiting'
        for sub in ('a', 'b'):
            copied = nl / sub / 'tone.wav'
            assert copied.is_file(), f"missing {copied}"
            assert copied.read_bytes() == (root / sub / 'tone.wav').read_bytes()
        assert not (nl / 'tone.wav').exists()

    def test_sequential(self, same_named_input, tmp_path):
        out = tmp_path / 'out'
        normalizer = LUFSNormalizer()
        normalizer.normalize_batch(
            input_dir=str(same_named_input), output_dir=str(out),
            target_lufs=-9.0, peak_ceiling=-1.0, strict_lufs_matching=True,
            recursive=True, use_batch_folders=False,
        )
        assert len(normalizer.skipped_files) == 2
        self._assert_both_preserved(same_named_input, out)

    def test_parallel(self, same_named_input, tmp_path):
        out = tmp_path / 'out'
        normalizer = LUFSNormalizer()
        normalizer.normalize_batch_parallel(
            input_dir=str(same_named_input), output_dir=str(out),
            target_lufs=-9.0, peak_ceiling=-1.0, strict_lufs_matching=True,
            recursive=True, use_batch_folders=False, max_workers=2,
        )
        assert len(normalizer.skipped_files) == 2
        self._assert_both_preserved(same_named_input, out)


def _csv_filenames(path):
    with open(path, newline='') as f:
        return sorted(row['filename'] for row in csv.DictReader(f))


class TestReportFilenames:
    @pytest.fixture
    def same_named_input(self, tmp_path):
        root = tmp_path / 'in'
        (root / 'a').mkdir(parents=True)
        (root / 'b').mkdir(parents=True)
        _write_peaky(root / 'a' / 'tone.wav', quiet_amplitude=0.005)
        _write_peaky(root / 'b' / 'tone.wav', quiet_amplitude=0.02)
        return root

    @pytest.mark.parametrize('parallel', [False, True], ids=['sequential', 'parallel'])
    def test_recursive_strict_needs_limiting_rows_distinct(self, same_named_input, tmp_path,
                                                           parallel):
        out = tmp_path / 'out'
        normalizer = LUFSNormalizer()
        kwargs = dict(input_dir=str(same_named_input), output_dir=str(out),
                      target_lufs=-9.0, peak_ceiling=-1.0, strict_lufs_matching=True,
                      recursive=True, use_batch_folders=False)
        if parallel:
            normalizer.normalize_batch_parallel(max_workers=2, **kwargs)
        else:
            normalizer.normalize_batch(**kwargs)
        assert _csv_filenames(out / 'needs_limiting_report.csv') == ['a/tone.wav', 'b/tone.wav']

    def test_recursive_drift_normalization_rows_distinct(self, same_named_input, tmp_path):
        out = tmp_path / 'out'
        LUFSNormalizer().normalize_batch(
            input_dir=str(same_named_input), output_dir=str(out),
            target_lufs=-9.0, peak_ceiling=-1.0, strict_lufs_matching=False,
            recursive=True, use_batch_folders=False,
        )
        assert _csv_filenames(out / 'normalization_report.csv') == ['a/tone.wav', 'b/tone.wav']

    def test_non_recursive_keeps_bare_filenames(self, tmp_path):
        root = tmp_path / 'in'
        root.mkdir()
        _write_peaky(root / 'tone.wav', quiet_amplitude=0.005)
        _write_good(root / 'good.wav')
        out = tmp_path / 'out'
        LUFSNormalizer().normalize_batch(
            input_dir=str(root), output_dir=str(out),
            target_lufs=-9.0, peak_ceiling=-1.0, strict_lufs_matching=True,
            recursive=False, use_batch_folders=False,
        )
        assert _csv_filenames(out / 'needs_limiting_report.csv') == ['tone.wav']
        assert _csv_filenames(out / 'normalization_report.csv') == ['good.wav']


# ---------------------------------------------------------------------------
# Bug B: non-finite samples
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'drift'])
class TestNonFiniteSamples:
    @pytest.fixture
    def mixed_input(self, tmp_path):
        root = tmp_path / 'in'
        root.mkdir()
        _write_float(root / 'nan.wav', bad_value=np.nan)
        _write_float(root / 'inf.wav', bad_value=np.inf)
        _write_float(root / 'zero.wav', bad_value=None)
        _write_good(root / 'good.wav')
        return root

    def test_nan_and_inf_fail_zero_skipped_good_continues(self, mixed_input, tmp_path, strict):
        out = tmp_path / 'out'
        normalizer = LUFSNormalizer()
        success, total, *_ = normalizer.normalize_batch(
            input_dir=str(mixed_input), output_dir=str(out),
            target_lufs=-23.0, peak_ceiling=-1.0, strict_lufs_matching=strict,
            use_batch_folders=False,
        )
        assert total == 4
        assert success == 1
        assert [r['filename'] for r in normalizer.results] == ['good.wav']

        errors = {e['filename']: e for e in normalizer.errors}
        assert set(errors) == {'nan.wav', 'inf.wav'}
        for e in errors.values():
            assert e['status'] == 'FAILED'
            assert e['reason'] == 'non_finite_samples'
            assert '200 non-finite samples' in e['error']

        assert [s['filename'] for s in normalizer.skipped_silent] == ['zero.wav']
        assert normalizer.skipped_files == []

        written = sorted(p.name for p in out.rglob('*.wav'))
        assert written == ['good_-23LUFS.wav']
        assert not (out / 'needs_limiting').exists()


class TestNonFiniteProcessorPaths:
    @pytest.mark.parametrize('bad_value', [np.nan, np.inf, -np.inf], ids=['nan', 'inf', 'neg_inf'])
    @pytest.mark.parametrize('streaming', [False, True], ids=['standard', 'streaming'])
    def test_failed_result_no_output(self, tmp_path, monkeypatch, bad_value, streaming):
        if streaming:
            monkeypatch.setattr(streaming_mod, 'STREAMING_THRESHOLD_BYTES', 1)
        src = tmp_path / 'bad.wav'
        _write_float(src, bad_value=bad_value, bad_count=7)
        norm, nl = tmp_path / 'n', tmp_path / 'nl'

        res = process_single_file(
            str(src), target_lufs=-23.0, peak_ceiling=-1.0,
            strict_lufs_matching=True, bit_depth='preserve', sample_rate='preserve',
            normalized_path=str(norm), needs_limiting_path=str(nl),
        )
        assert res['type'] == 'error'
        assert res['output_file'] is None
        assert res['error']['status'] == 'FAILED'
        assert res['error']['reason'] == 'non_finite_samples'
        assert '14 non-finite samples' in res['error']['error']
        assert not norm.exists() or not any(norm.iterdir())
        assert not nl.exists()

    def test_streaming_counts_across_chunks(self, tmp_path):
        src = tmp_path / 'bad.wav'
        n = int(DURATION * SR)
        data = np.zeros((n, 2), dtype=np.float32)
        data[10, 0] = np.nan
        data[n - 10, 1] = np.inf
        sf.write(str(src), data, SR, subtype='FLOAT')
        with pytest.raises(streaming_mod.NonFiniteSamplesError) as exc:
            streaming_mod.measure_streaming(src, chunk_frames=SR)
        assert exc.value.count == 2

    def test_all_zero_still_skipped(self, tmp_path):
        src = tmp_path / 'zero.wav'
        _write_float(src, bad_value=None)
        res = process_single_file(
            str(src), target_lufs=-23.0, peak_ceiling=-1.0,
            strict_lufs_matching=True, bit_depth='preserve', sample_rate='preserve',
            normalized_path=str(tmp_path / 'n'), needs_limiting_path=str(tmp_path / 'nl'),
        )
        assert res['type'] == 'skipped'
        assert res['error']['reason'] == 'too_quiet'
