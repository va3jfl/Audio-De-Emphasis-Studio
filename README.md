<p align="center">
  <h1 align="center">Audio De-Emphasis Studio</h1>
  <p align="center">
    A cleaner desktop app for softening harsh high-frequency audio, peak-normalizing, and exporting polished WAV files.
  </p>
  <p align="center">
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue">
    <img alt="UI" src="https://img.shields.io/badge/UI-CustomTkinter-lightgrey">
    <img alt="Audio" src="https://img.shields.io/badge/Audio-pydub%20%2B%20SciPy-green">
  </p>
</p>

---

## What it does

Audio De-Emphasis Studio is a modernized version of the original Tkinter audio processor I worked on. It keeps the same core idea:

1. Load an audio file or a folder of audio files.
2. Measure high-frequency energy using spectral analysis.
3. Apply a de-emphasis / smoothing filter only when the file is overly bright.
4. Peak-normalize the result.
5. Export a processed `.wav` file.
6. Show a before/after spectrum plot.

The new version improves the desktop experience with a cleaner interface, app-style controls, live feedback, a progress bar, a log panel, batch processing, and a working cancel button.

---

## Features

- Modern dark desktop UI using CustomTkinter
- Single-file and batch-folder processing
- Non-blocking worker thread so the interface does not freeze while processing
- Live status log instead of ugly console-only feedback
- Progress bar for batch jobs
- Before/after spectrum plot
- Cancel button for long batch jobs
- Preset profiles plus manual slider control
- WAV export with `_processed.wav` suffix
- Supports common input formats handled by FFmpeg / pydub

---

## Profiles

| Profile | Purpose |
|---|---|
| Clean Balanced | Safe default for general cleanup |
| Broadcast Smooth | Stronger smoothing with a polished output level |
| Bright Tamer | More aggressive high-frequency control |
| Gentle Archive | Light touch for older or delicate recordings |
| Original Aggressive-ish | Close to the behavior of the older aggressive mode |
| Manual | Use the sliders freely |

---

## Controls

| Control | What it changes |
|---|---|
| Target Peak | Final peak normalization level in dBFS |
| Aggressiveness | How strongly the processor reacts once excess high-frequency energy is detected |
| HF Balance Threshold | How much high-frequency energy is allowed before filtering starts |
| HF Cutoff | Frequency region used to judge brightness / harshness |
| Max De-Emphasis | Upper limit of the smoothing filter strength |

---

## Install

### 1. Install Python

Use Python 3.10 or newer.

### 2. Install FFmpeg

`pydub` uses FFmpeg to read formats such as MP3, FLAC, OGG, OPUS, M4A, and AAC.

Windows options:

- Install FFmpeg from a trusted Windows build.
- Add the `bin` folder containing `ffmpeg.exe` to your system PATH.

Linux example:

```bash
sudo apt install ffmpeg
```

macOS example:

```bash
brew install ffmpeg
```

### 3. Install Python packages

```bash
pip install -r requirements.txt
```

---

## Run

```bash
python proc.py
```

Then:

1. Choose an audio file or choose a batch folder.
2. Choose an output folder.
3. Pick a profile.
4. Fine-tune the sliders if needed.
5. Click **Process File** or **Process Batch**.

Processed files are saved as:

```text
original_filename_processed.wav
```

---

## Build a Windows EXE

Optional:

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name AudioDeEmphasisStudio audio_deemphasis_studio.py
```

The EXE will appear in:

```text
dist\AudioDeEmphasisStudio.exe
```

FFmpeg still needs to be available on the system PATH, unless you bundle it yourself.

---

## Notes

- The processor internally converts audio to 16-bit samples before filtering, matching the spirit of the original code path.
- The filter is applied independently per channel, so stereo files are handled more cleanly than the original flat interleaved sample array.
- The app exports WAV files by default for predictable output quality.
- The cancel button stops the next file in a batch; the currently active file may finish its current processing step first.

---

## Project files

```text
proc.py   # Main application
requirements.txt             # Python dependencies
README.md                    # Project documentation
```
