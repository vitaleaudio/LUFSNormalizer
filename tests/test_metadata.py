"""
Tests for core.metadata: BWF BEXT and iXML chunk injection.

Covers:
  - BEXT chunk binary layout (size, encoded loudness fields)
  - Non-finite loudness handling (Round 1 fix #2)
  - Re-injection idempotency (a second call replaces, doesn't append)
  - Stream-copy preservation of large `data` chunks
  - iXML XML-escaping for non-finite values
"""

import struct
import numpy as np
import soundfile as sf

from lufs_normalizer.core.metadata import (
    _build_bext_chunk,
    _read_riff_chunks,
    inject_bext_chunk,
    inject_ixml_chunk,
    build_ixml_for_normalization,
)


SR = 48000


def _read_bext(wav_path):
    """Return raw bext chunk bytes from a WAV, or None if absent."""
    _, chunks = _read_riff_chunks(wav_path)
    for cid, payload in chunks:
        if cid == b'bext':
            return payload if isinstance(payload, bytes) else None
    return None


def _read_ixml(wav_path):
    _, chunks = _read_riff_chunks(wav_path)
    for cid, payload in chunks:
        if cid == b'iXML':
            return payload if isinstance(payload, bytes) else None
    return None


def _decode_loudness_fields(bext_bytes):
    """Extract the three int16 loudness fields (LV, LR, MaxTP) from a BEXT chunk."""
    # Layout: desc(256) + orig(32) + origref(32) + date(10) + time(8) +
    #         time_ref_low(4) + time_ref_high(4) + version(2) + umid(64) =
    #         412 bytes before loudness fields
    base = 256 + 32 + 32 + 10 + 8 + 4 + 4 + 2 + 64
    lv, lr, mtp = struct.unpack('<hhh', bext_bytes[base:base + 6])
    return lv, lr, mtp


class TestBuildBEXTChunk:
    def test_chunk_is_602_bytes(self):
        """BEXT v2 fixed-size structure is 602 bytes."""
        chunk = _build_bext_chunk(loudness_value=-23.0, loudness_range=8.2,
                                   max_true_peak=-1.5)
        assert len(chunk) == 602

    def test_loudness_fields_encoded_as_int16_x100(self):
        chunk = _build_bext_chunk(loudness_value=-23.0, loudness_range=8.2,
                                   max_true_peak=-1.5)
        lv, lr, mtp = _decode_loudness_fields(chunk)
        assert lv == -2300
        assert lr == 820
        assert mtp == -150

    def test_neg_inf_loudness_writes_zero(self):
        """Round 1 fix #2: -inf must not crash int() — writes 0 ('not measured')."""
        chunk = _build_bext_chunk(loudness_value=float('-inf'),
                                   loudness_range=float('-inf'),
                                   max_true_peak=float('-inf'))
        lv, lr, mtp = _decode_loudness_fields(chunk)
        assert lv == 0 and lr == 0 and mtp == 0

    def test_nan_loudness_writes_zero(self):
        """NaN must not crash int() — writes 0."""
        chunk = _build_bext_chunk(loudness_value=float('nan'),
                                   loudness_range=float('nan'),
                                   max_true_peak=-1.0)
        lv, lr, mtp = _decode_loudness_fields(chunk)
        assert lv == 0 and lr == 0 and mtp == -100

    def test_extreme_value_clamps_to_int16_range(self):
        """Out-of-range loudness clamps rather than wrapping."""
        chunk = _build_bext_chunk(loudness_value=999.0, max_true_peak=-999.0)
        lv, _, mtp = _decode_loudness_fields(chunk)
        assert lv == 32767  # clamped from 99900
        assert mtp == -32768  # clamped from -99900

    def test_none_writes_zero(self):
        chunk = _build_bext_chunk(loudness_value=None, loudness_range=None,
                                   max_true_peak=None)
        lv, lr, mtp = _decode_loudness_fields(chunk)
        assert lv == 0 and lr == 0 and mtp == 0

    def test_description_truncated_to_256_chars(self):
        long = 'A' * 500
        chunk = _build_bext_chunk(description=long)
        assert chunk[:256] == b'A' * 256


class TestBEXTInjection:
    def _make_wav(self, path, channels=2, samples=SR):
        data = np.zeros((samples, channels), dtype=np.float32) if channels > 1 \
               else np.zeros(samples, dtype=np.float32)
        sf.write(str(path), data, SR, subtype='PCM_24')

    def test_injection_round_trip(self, tmp_path):
        wav = tmp_path / 'a.wav'
        self._make_wav(wav)
        ok = inject_bext_chunk(str(wav), {
            'description': 'Test deliverable',
            'loudness_value': -23.0,
            'loudness_range': 7.5,
            'max_true_peak': -1.0,
        })
        assert ok
        bext = _read_bext(str(wav))
        assert bext is not None
        assert bext[:16].rstrip(b'\x00') == b'Test deliverable'
        lv, lr, mtp = _decode_loudness_fields(bext)
        assert lv == -2300 and lr == 750 and mtp == -100

    def test_double_injection_replaces_existing_chunk(self, tmp_path):
        """A second injection must not duplicate the bext chunk."""
        wav = tmp_path / 'b.wav'
        self._make_wav(wav)
        inject_bext_chunk(str(wav), {'loudness_value': -23.0, 'max_true_peak': -1.0})
        inject_bext_chunk(str(wav), {'loudness_value': -16.0, 'max_true_peak': -2.0})

        _, chunks = _read_riff_chunks(str(wav))
        bext_chunks = [c for c in chunks if c[0] == b'bext']
        assert len(bext_chunks) == 1, f"expected 1 bext chunk, got {len(bext_chunks)}"
        lv, _, mtp = _decode_loudness_fields(bext_chunks[0][1])
        assert lv == -1600 and mtp == -200  # second value won

    def test_audio_data_preserved_after_injection(self, tmp_path):
        """Audio samples must be byte-identical after BWF injection."""
        wav = tmp_path / 'c.wav'
        rng = np.random.default_rng(0)
        data = (rng.standard_normal((SR * 2, 2)) * 0.1).astype(np.float32)
        sf.write(str(wav), data, SR, subtype='PCM_24')

        before, _ = sf.read(str(wav))
        inject_bext_chunk(str(wav), {'loudness_value': -23.0, 'max_true_peak': -1.0})
        after, _ = sf.read(str(wav))

        np.testing.assert_array_equal(before, after)


class TestiXMLInjection:
    def _make_wav(self, path):
        data = np.zeros((SR, 2), dtype=np.float32)
        sf.write(str(path), data, SR, subtype='PCM_24')

    def test_ixml_round_trip(self, tmp_path):
        wav = tmp_path / 'a.wav'
        self._make_wav(wav)
        xml = build_ixml_for_normalization(-23.0, -23.01, 8.2, -1.82, '3.1.3')
        assert inject_ixml_chunk(str(wav), xml)
        ixml = _read_ixml(str(wav))
        assert ixml is not None
        assert b'<TARGET_LUFS>-23.0</TARGET_LUFS>' in ixml
        assert b'<FINAL_LUFS>-23.01</FINAL_LUFS>' in ixml

    def test_double_injection_replaces(self, tmp_path):
        wav = tmp_path / 'b.wav'
        self._make_wav(wav)
        inject_ixml_chunk(str(wav), '<a>1</a>')
        inject_ixml_chunk(str(wav), '<a>2</a>')
        _, chunks = _read_riff_chunks(str(wav))
        ixml_chunks = [c for c in chunks if c[0] == b'iXML']
        assert len(ixml_chunks) == 1
        assert b'<a>2</a>' in ixml_chunks[0][1]


class TestBuildIXML:
    def test_finite_values(self):
        xml = build_ixml_for_normalization(-23.0, -23.01, 8.2, -1.82, '3.1.3')
        assert '<FINAL_LUFS>-23.01</FINAL_LUFS>' in xml
        assert '<LRA_LU>8.2</LRA_LU>' in xml
        assert '<TRUE_PEAK_DBTP>-1.82</TRUE_PEAK_DBTP>' in xml

    def test_neg_inf_renders_as_na(self):
        """Round 1 fix #2: non-finite values render 'N/A', not '-inf'."""
        xml = build_ixml_for_normalization(-23.0, float('-inf'),
                                            float('nan'), -1.0, '3.1.3')
        assert '<FINAL_LUFS>N/A</FINAL_LUFS>' in xml
        assert '<LRA_LU>N/A</LRA_LU>' in xml
        assert '-inf' not in xml
        assert 'nan' not in xml.lower()

    def test_none_lra_renders_as_na(self):
        """LRA can legitimately be None (file too short)."""
        xml = build_ixml_for_normalization(-23.0, -23.0, None, -1.0, '3.1.3')
        assert '<LRA_LU>N/A</LRA_LU>' in xml
