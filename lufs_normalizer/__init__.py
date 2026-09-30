"""
LUFS Normalizer v3.1.4
Professional broadcast-grade audio normalization

Author: Mario Vitale
"""

import re
from pathlib import Path

VERSION = "3.1.4"


def get_output_filename(original_name, target_lufs):
    """
    Generate output filename with smart LUFS suffix handling.

    - If file has existing LUFS suffix (e.g., _-18LUFS), replace it
    - If file has _normalized suffix, replace with LUFS suffix
    - Otherwise, append LUFS suffix

    Examples:
        audio.wav -> audio_-23LUFS.wav
        audio_-18LUFS.wav -> audio_-23LUFS.wav
        audio_normalized.wav -> audio_-23LUFS.wav
    """
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix

    lufs_pattern = r'_-?\d+(\.\d+)?LUFS$'
    normalized_pattern = r'_normalized$'

    stem = re.sub(lufs_pattern, '', stem, flags=re.IGNORECASE)
    stem = re.sub(normalized_pattern, '', stem, flags=re.IGNORECASE)

    lufs_str = f"{int(target_lufs)}" if target_lufs == int(target_lufs) else f"{target_lufs}"
    new_name = f"{stem}_{lufs_str}LUFS{suffix}"

    return new_name
