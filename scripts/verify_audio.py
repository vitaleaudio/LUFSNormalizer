#!/usr/bin/env python3
"""
LUFS & True Peak Verification Tool v3.1.3
Verify normalized files meet broadcast specifications

Uses SOXR for True Peak measurement (same as normalizer)

Usage:
    python verify_audio.py <audio_file>
    python verify_audio.py <folder>
"""

import sys
import soundfile as sf
import numpy as np
from pathlib import Path

from lufs_normalizer.core.measurement import measure_true_peak, measure_lra

try:
    import pyloudnorm as pyln
except ImportError:
    print("Error: pyloudnorm required. Install with: pip install pyloudnorm")
    sys.exit(1)


def verify_file(filepath):
    """Verify a single audio file"""
    path = Path(filepath)

    if not path.exists():
        print(f"File not found: {filepath}")
        return

    try:
        data, rate = sf.read(str(path))
        info = sf.info(str(path))

        # Measure LUFS
        meter = pyln.Meter(rate)
        if data.ndim == 1:
            lufs = meter.integrated_loudness(data.reshape(-1, 1))
        else:
            lufs = meter.integrated_loudness(data)

        # Measure True Peak
        true_peak = measure_true_peak(data, rate)

        # Measure LRA
        lra = measure_lra(data, rate)

        # Sample Peak for comparison
        sample_peak = 20 * np.log10(np.max(np.abs(data))) if np.max(np.abs(data)) > 0 else -100

        # Display results
        print(f"\n{'='*60}")
        print(f"FILE: {path.name}")
        print(f"{'='*60}")
        print(f"Duration:     {len(data)/rate:.2f} seconds")
        print(f"Sample Rate:  {rate} Hz")
        print(f"Channels:     {data.shape[1] if data.ndim > 1 else 1}")
        print(f"Format:       {info.subtype}")
        print(f"{'─'*60}")
        print(f"LUFS:         {lufs:.1f} LUFS")
        print(f"LRA:          {lra:.1f} LU" if lra is not None else "LRA:          N/A")
        print(f"Sample Peak:  {sample_peak:.2f} dBFS")
        print(f"True Peak:    {true_peak:.2f} dBTP")
        print(f"{'─'*60}")

        # Broadcast compliance check
        print("BROADCAST COMPLIANCE:")

        # EBU R128
        if -24 <= lufs <= -22:
            print(f"  EBU R128 (-23 LUFS +/-1)   OK")
        else:
            print(f"  EBU R128 (-23 LUFS +/-1)   FAIL (Current: {lufs:.1f})")

        if true_peak <= -1.0:
            print(f"  True Peak <= -1 dBTP       OK")
        else:
            print(f"  True Peak <= -1 dBTP       FAIL ({true_peak:.2f})")

        # Streaming platforms
        if -15 <= lufs <= -13:
            print(f"  Streaming (-14 LUFS +/-1)  OK")

        # Game audio
        if -19 <= lufs <= -17:
            print(f"  Game Audio (-18 LUFS +/-1) OK")

        print(f"{'='*60}\n")

    except Exception as e:
        print(f"Error reading {filepath}: {e}")


def verify_folder(folder_path):
    """Verify all audio files in a folder"""
    path = Path(folder_path)

    audio_files = (list(path.glob('*.wav')) + list(path.glob('*.WAV')) +
                   list(path.glob('*.aiff')) + list(path.glob('*.AIFF')) +
                   list(path.glob('*.aif')) + list(path.glob('*.AIF')))

    if not audio_files:
        print(f"No audio files found in: {folder_path}")
        return

    print(f"\nVerifying {len(audio_files)} files in: {folder_path}")

    for audio_file in sorted(audio_files):
        verify_file(str(audio_file))


def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_audio.py <file_or_folder>")
        print("\nExamples:")
        print("  python verify_audio.py audio.wav")
        print("  python verify_audio.py ./normalized_folder/")
        return

    target = sys.argv[1]
    path = Path(target)

    if path.is_dir():
        verify_folder(target)
    elif path.is_file():
        verify_file(target)
    else:
        print(f"Not found: {target}")


if __name__ == '__main__':
    main()
