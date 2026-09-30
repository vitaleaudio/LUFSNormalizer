# LUFS Normalizer

Professional batch audio normalization for broadcast, game audio and streaming. Normalizes WAV and AIFF files to industry-standard LUFS targets while respecting True Peak limits.

**Version 3.1.4** | Author: Mario Vitale

## Download

Get the latest Windows build from the [releases page](https://github.com/vitaleaudio/LUFSNormalizer/releases/latest). The download contains two executables: `LUFSNormalizer_v3.1.4.exe` (GUI) and `LUFSNormalizer_v3.1.4_CLI.exe` (command line). Both exes are unsigned, so Windows SmartScreen will warn on first launch.

## Features

- **LUFS normalization** per ITU-R BS.1770-4 (via pyloudnorm)
- **True Peak measurement** (dBTP) with 4x oversampling via SOXR (scipy fallback)
- **Loudness Range (LRA)** measurement per EBU R128 s1
- **TPDF dithering** for bit depth reduction (16-bit, 24-bit)
- **Sample rate conversion** via SOXR (VHQ quality, downsampling only)
- **Parallel batch processing** using ProcessPoolExecutor
- **BWF BEXT + iXML metadata** injection for WAV files
- **Watch folder mode** with automatic processing of new files
- **10 built-in presets** covering broadcast, streaming, game, film and music
- **Strict LUFS and Drift** peak handling modes
- **PySide6 dark-themed GUI** with preset manager and real-time log
- **CLI** for scripting and headless operation
- **Single-file exe** build via PyInstaller

---

## Presets

| Key | Name | LUFS | Peak (dBTP) | Standard |
|---|---|---|---|---|
| `broadcast_us` | Broadcast (US) | -24.0 | -2.0 | ATSC A/85 |
| `broadcast_eu` | Broadcast (EU) | -23.0 | -1.0 | EBU R128 |
| `streaming` | Streaming | -14.0 | -1.0 | Spotify / YouTube / Amazon |
| `podcast` | Podcast | -16.0 | -1.0 | Apple Podcasts |
| `game_console` | Game (Console) | -24.0 | -1.0 | ASWG-R001 Home |
| `game_mobile` | Game (Mobile) | -18.0 | -1.0 | ASWG-R001 Portable |
| `film` | Film / Cinema | -24.0 | -2.0 | SMPTE RP 200 |
| `music_dynamic` | Music (Dynamic) | -14.0 | -1.0 | Streaming optimized |
| `music_loud` | Music (Loud) | -9.0 | -1.0 | Contemporary pop/EDM |
| `reference_cinema` | Cinema Dialog Ref | -27.0 | -2.0 | Netflix 5.1 |

The GUI displays up to 5 favorite presets as quick-select buttons. Use the Preset Manager to add, remove and drag-reorder favorites.

---

## Peak Handling Modes

### Strict LUFS (default)

Files that would exceed the peak ceiling after normalization are **skipped**. The original file is copied to a `needs_limiting/` folder so you can apply a limiter in your DAW and re-process. Every normalized file is guaranteed to hit the exact target LUFS.

### Drift Mode

Gain is reduced to keep the True Peak at or below the ceiling. The final LUFS may undershoot the target. Files are never skipped. The CSV report marks these as `OK_UNDERSHOOT` with reason `peak_limited`.

---

## Supported Formats

- **Input:** `.wav`, `.WAV`, `.aiff`, `.AIFF`, `.aif`, `.AIF`
- **Output:** Same format as input (WAV stays WAV, AIFF stays AIFF)
- **Bit depth:** Preserve, 16-bit, 24-bit or 32-bit (TPDF dither applied when reducing)
- **Sample rate:** Preserve, 44100 Hz or 48000 Hz (downsampling only, requires SOXR)

### Large-file streaming mode

Files whose float64 in-memory footprint would exceed **2 GiB** (~46 minutes stereo 48 kHz) are automatically processed in constant-memory chunks. Memory usage stays bounded regardless of file duration.

- Requires `scipy` (`pip install scipy`)
- **Limitation:** sample rate conversion is not supported in streaming mode - normalize rate separately or omit the `--rate` flag
- LRA is reported as empty for files processed in streaming mode

---

## Output Folder Structure

With batch folders enabled (default):

```
output/
  batch_20260327_143000_-23LUFS/
    normalized/
      audio_-23LUFS.wav
      speech_-23LUFS.wav
    needs_limiting/
      loud_track.wav
    logs/
      processing.log
      normalization_report.csv
      needs_limiting_report.csv
```

With `--no-batch-folders` (flat mode):

```
output/
  audio_-23LUFS.wav
  speech_-23LUFS.wav
  needs_limiting/
    loud_track.wav
  processing.log
  normalization_report.csv
```

Output filenames replace any existing `_-XXLUFS` or `_normalized` suffix with the new target. For example, `audio_-18LUFS.wav` normalized to -23 LUFS becomes `audio_-23LUFS.wav`.

---

## CSV Report Schema

### normalization_report.csv

| Column | Description |
|---|---|
| `filename` | Input filename (path relative to the input folder with forward slashes in `--recursive` mode, e.g. `a/tone.wav`) |
| `status` | `OK` or `OK_UNDERSHOOT` |
| `reason` | `ok` or `peak_limited` |
| `sample_rate` | Output sample rate in Hz |
| `bit_depth` | Output bit depth (16, 24 or 32) |
| `original_lufs` | Measured input loudness |
| `target_lufs` | Requested target |
| `final_lufs` | Measured output loudness |
| `gain_applied_db` | Gain applied in dB |
| `true_peak_dBTP` | Output True Peak in dBTP |
| `lra_lu` | Loudness Range in LU (empty if file shorter than 3 seconds) |

### needs_limiting_report.csv

Generated in Strict mode when files are skipped.

| Column | Description |
|---|---|
| `filename` | Input filename (path relative to the input folder with forward slashes in `--recursive` mode, e.g. `a/tone.wav`) |
| `original_lufs` | Measured input loudness |
| `predicted_peak_dBTP` | Peak that would result from normalization |
| `gain_needed_db` | Gain that would be required |
| `lra_lu` | Loudness Range in LU (empty if file shorter than 3 seconds) |
| `reason` | `would_exceed_peak_ceiling` |

---

## BWF / iXML Metadata

When enabled (`--bwf` on CLI, or the "Embed BWF metadata" checkbox in the GUI), WAV output files receive two additional RIFF chunks. AIFF files are unaffected.

### BEXT chunk (EBU Tech 3285 v2)

| Field | Value |
|---|---|
| Description | `Normalized to -23.0 LUFS by LUFS Normalizer v3.1.4` |
| Originator | `LUFS Normalizer` |
| OriginatorReference | `LN302` |
| OriginationDate | Processing date (yyyy-mm-dd) |
| OriginationTime | Processing time (hh:mm:ss) |
| LoudnessValue | Final LUFS (int16, value x 100) |
| LoudnessRange | LRA in LU (int16, value x 100) |
| MaxTruePeakLevel | dBTP (int16, value x 100) |

### iXML chunk

```xml
<BWFXML>
  <IXML_VERSION>1.52</IXML_VERSION>
  <PROJECT>LUFS Normalizer</PROJECT>
  <NOTE>Normalized to -23.0 LUFS by LUFS Normalizer v3.1.4</NOTE>
  <USER>
    <TARGET_LUFS>-23.0</TARGET_LUFS>
    <FINAL_LUFS>-23.01</FINAL_LUFS>
    <LRA_LU>8.2</LRA_LU>
    <TRUE_PEAK_DBTP>-1.82</TRUE_PEAK_DBTP>
  </USER>
</BWFXML>
```

BWF BEXT follows EBU Tech 3285 v2 and the iXML chunk follows the iXML spec.

---

## CLI Usage

The CLI ships as a separate executable, `LUFSNormalizer_v3.1.4_CLI.exe`, in the same download as the GUI exe. Run the CLI commands in this README against that file, not the GUI exe. `LUFSNormalizer_v3.1.4.exe` always opens the GUI and ignores command-line arguments. When using the downloaded exe, replace `python -m lufs_normalizer` in the examples below with `LUFSNormalizer_v3.1.4_CLI.exe`.

```
LUFSNormalizer_v3.1.4_CLI.exe input_dir output_dir [options]
python -m lufs_normalizer input_dir output_dir [options]
```

### Options

| Flag | Description | Default |
|---|---|---|
| `-t`, `--target` | Target LUFS | -23.0 |
| `-p`, `--peak` | Peak ceiling in dBTP | -1.0 |
| `-b`, `--bits` | Output bit depth (`preserve`, `16`, `24`, `32`) | `preserve` |
| `-r`, `--rate` | Output sample rate (`preserve`, `44100`, `48000`) | `preserve` |
| `--no-batch-folders` | Flat output (no timestamped subdirectory) | off |
| `--no-log` | Skip log file generation | off |
| `--no-csv` | Skip CSV report generation | off |
| `--drift` | Drift mode (reduce gain to protect peak) | off (strict) |
| `--bwf` | Embed BWF BEXT + iXML in output WAV files | off |
| `--parallel` | Enable parallel processing | off |
| `--workers N` | Number of parallel workers | CPU count |
| `--recursive` | Scan subdirectories; mirror folder hierarchy in output | off |
| `--watch` | Watch folder mode (monitor for new files) | off |

### Examples

```bash
# EBU R128 broadcast normalization
python -m lufs_normalizer input/ output/ -t -23 -p -1

# Parallel processing with 8 workers and BWF metadata
python -m lufs_normalizer input/ output/ -t -24 -p -2 --parallel --workers 8 --bwf

# Drift mode (never skip files)
python -m lufs_normalizer input/ output/ -t -14 --drift

# Watch folder mode
python -m lufs_normalizer --watch input/ output/ -t -24 --bwf

# Convert to 48 kHz / 24-bit, flat output
python -m lufs_normalizer input/ output/ -t -16 -b 24 -r 48000 --no-batch-folders
```

---

## GUI Usage

Launch with no arguments:

```bash
python -m lufs_normalizer
```

Or via the shim script:

```bash
python normalize_gui_modern.py
```

### Batch Processing tab

1. Select input and output folders
2. Choose a preset or set LUFS / peak values manually
3. Configure bit depth, sample rate and options (BWF, parallel, strict/drift)
4. Click **Start Processing**

The LUFS spinner supports Up/Down arrow keys (1.0 step) and Shift+Up/Down (0.1 step).

Settings are saved to `config.json` next to the application and restored on launch.

### Watch Folder tab

1. Set a watch folder and output folder
2. Select a processing profile (any of the 10 presets)
3. Click **Start Watch**

New `.wav` and `.aiff` files dropped into the watch folder are automatically detected, waited on until the write completes, then processed. The panel shows a real-time activity log. Requires the `watchdog` package.

---

## Building the Exe

### Using the build script

```
build.bat
```

This installs build dependencies, generates the application icon via `create_icon.py` and runs PyInstaller to produce single-file exes. The distribution is written to `dist/LUFSNormalizer_v3.1.4/` with both exes (GUI and CLI), `config.json`, `verify_audio.py` and icon files.

### Manual build

```bash
pip install pyinstaller
pyinstaller LUFSNormalizer_v3.1.4.spec
```

The spec file bundles `config.json`, the `lufs_normalizer` package and hidden imports for PySide6, soundfile, pyloudnorm, soxr, numpy and watchdog.

Place `config.json` next to the exe for default settings. The exe creates and updates this file to persist user preferences.

---

## Running from Source

### Requirements

- Python 3.9+
- Windows (primary target). CI also runs on Ubuntu. macOS is untested.

### Install

```bash
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose | Required |
|---|---|---|
| `soundfile` | Audio file I/O (WAV, AIFF via libsndfile) | Yes |
| `pyloudnorm` | LUFS measurement (BS.1770-4) | Yes |
| `numpy` | Array processing | Yes |
| `PySide6` | GUI framework | Yes (GUI mode) |
| `soxr` | Resampling and True Peak oversampling | Recommended |
| `watchdog` | Watch folder file monitoring | Optional |
| `scipy` | Fallback True Peak oversampling if soxr absent; **required for large-file streaming mode** | Optional |
| `Pillow` | Icon generation at build time | Build only |

### Run

```bash
# GUI
python -m lufs_normalizer

# CLI
python -m lufs_normalizer input/ output/ -t -23

# Direct script
python normalize_gui_modern.py
```

---

## Keyboard Shortcuts (GUI)

| Key | Action |
|---|---|
| Up / Down | Adjust LUFS target by 1.0 |
| Shift + Up / Down | Adjust LUFS target by 0.1 |

---

## Testing

```bash
pip install -r requirements-test.txt
pytest
```

`requirements-test.txt` contains only the packages needed to run the suite (no PySide6 or Pillow). CI runs the same command on Ubuntu and Windows across Python 3.9 / 3.11 / 3.13 via GitHub Actions (`.github/workflows/test.yml`).

The suite (150+ tests) generates audio fixtures on the fly - no binary
test files in the repo. It covers BS.1770 measurement against reference
values, TPDF dither distribution, BWF/iXML round-trips, every branch of
`process_single_file`, parallel-vs-sequential consistency, the watch
folder pipeline (including pre-existing file scan-on-start) and the
large-file streaming path (chunked K-weighting and write).

## Known Issues

- With `--recursive` and the output folder equal to or inside the input folder, files previously written to `needs_limiting/` are picked up as new inputs on the next run. Use a separate output folder.
- The CLI exits with code 0 even when files are diverted to `needs_limiting/` or fail.
- Failed files (for example files containing NaN or inf samples) are reported only in the console output and `processing.log`. They do not appear in the CSV reports.

## Credits

Product design, specification and QA by Mario Vitale. Implementation built with Claude (Anthropic) under his direction.

**Libraries:** [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm), [soundfile](https://github.com/bastibe/python-soundfile), [soxr](https://github.com/dofuuz/python-soxr), [PySide6](https://doc.qt.io/qtforpython-6/)

## License

PolyForm Noncommercial License 1.0.0

Required Notice: Copyright Mario Vitale (https://github.com/vitaleaudio/LUFSNormalizer)
