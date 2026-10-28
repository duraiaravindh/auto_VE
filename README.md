# Nama Kadhai captions

Burn word-timed captions from an SRT into a video. Local and free; the
original video is never changed.

## Caption Studio (browser)

    .\studio.ps1          # opens http://127.0.0.1:8770

1. Upload the video (vertical or horizontal) and the SRT.
2. Optional: **Time captions to my voice** to see the real timing; fix any
   caption (red = check) with -0.1 / +0.1 / start here, or shift all.
3. Pick a style preset, a font (bundled + every font installed on Windows),
   size, colours, box, animation; drag the caption to where it should sit.
4. **Burn captions & render**. The output keeps the video's size and length.

## Use (command line)

In PowerShell, in this folder:

    powershell -ExecutionPolicy Bypass -File .\caption.ps1 myvideo.mp4 myvideo.srt

The result is saved to `projects\captioned\<video>-<date-time>\<video>_captioned.mp4`,
same size and length as the input. The folder also holds the cleaned SRT,
the word timings (`captions.json`), the subtitle file (`captions.ass`) and
`alignment_report.json`.

What it does:

1. Cleans the SRT: removes cue numbers/timestamps that ended up as text, and
   writes Tamil-script words in English letters (the aligner reads only a-z).
2. Times every word against your voice (MMS forced alignment). The SRT's own
   timestamps are ignored, so they don't need to be right.
3. Splits long lines into short captions and burns them in with the style.

What it reports:

- **Check these spots**: words that didn't match the speech well.
- **SRT lines not heard in the video**: lines skipped (often a different cut).
- **Speech with no caption from X to Y**: the SRT is missing lines there.

SRT tips: English letters (Tanglish), made from this video's own audio, and
complete to the end.

If it stops right after step 1 with no message, the PC is low on memory:
close Chrome or restart, then run it again.

## Style

Edit `config\caption_style.json` (font, colours, outline, shadow, bold/italic,
`pop`/`fade` animation, word highlight, `font_scale`, `margin_y_scale`).
For a one-off look, copy it and pass `-Style path\to\copy.json`.
Fonts must be in `fonts\` (Poppins Bold, Varela Round are included).

## Setup (one-time)

    uv venv --python 3.11 .venv-align
    uv pip install --python .venv-align\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1
    uv pip install --python .venv-align\Scripts\python.exe "numpy<2"

FFmpeg goes in `tools\ffmpeg-9.0.2-full_build\` (paths in `config\settings.json`).
The speech model downloads to `models\torch\` on first run. Its licence is
CC-BY-NC 4.0 (non-commercial).

Everything that was removed (app, auto cuts, OpenReel/Remotion, renders) is
kept on the git branch `backup-before-caption-only`.
