"""AIFF inputs with .aif, .AIF and .aiff extensions are written back as AIFF."""

import numpy as np
import pytest
import soundfile as sf

import lufs_normalizer.core.streaming as streaming_mod
from lufs_normalizer.core.processor import process_single_file


SR = 48000


@pytest.mark.parametrize('streaming', [False, True], ids=['standard', 'streaming'])
@pytest.mark.parametrize('suffix', ['.aif', '.AIF', '.aiff'])
def test_aiff_suffixes_write_aiff_output(tmp_path, monkeypatch, suffix, streaming):
    if streaming:
        monkeypatch.setattr(streaming_mod, 'STREAMING_THRESHOLD_BYTES', 1)

    n = SR * 4
    tone = 0.1 * np.sin(2 * np.pi * 1000 * np.arange(n) / SR)
    src = tmp_path / f'tone{suffix}'
    sf.write(str(src), np.column_stack([tone, tone]), SR, subtype='PCM_24', format='AIFF')

    res = process_single_file(
        str(src), target_lufs=-23.0, peak_ceiling=-1.0,
        strict_lufs_matching=True, bit_depth='preserve', sample_rate='preserve',
        normalized_path=str(tmp_path / 'out'), needs_limiting_path=str(tmp_path / 'nl'),
    )

    assert res['type'] == 'success', res
    out = tmp_path / 'out' / f'tone_-23LUFS{suffix}'
    assert res['output_file'] == str(out)
    assert out.is_file()
    assert sf.info(str(out)).format == 'AIFF'
