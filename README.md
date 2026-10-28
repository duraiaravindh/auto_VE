# Nama Kadhai video editor

Local, free editing for Nama Kadhai videos: edit in OpenReel, then word-synced
captions, Remotion title/outro cards and approval-gated landscape and vertical
renders. Runs offline; the original recording is never modified.

## Reference-style dynamic vertical video (no UI)

Create the complete 1080x1920 edit from a video and reviewed SRT:

    .\dynamic.ps1 test001.mp4 subtitles.srt

Each run creates a new folder under `projects\dynamic\` containing 1080x1920
vertical and 1920x1080 landscape review MP4s, short speech-activity-aligned
captions, ASS subtitles, source-bound creative timelines, generated-asset
catalogue, and validation reports. Use `-VerticalOnly` when only the reference-style
vertical output is needed. The source video and SRT are never overwritten.

The automatic edit trims silent handles, uses quick presenter cuts, split-screen,
circular picture-in-picture, locally generated animated topic cards, sparse
dialogue-triggered reaction marks, restrained flash/sweep transitions, procedural
pop/whoosh/impact sounds, a full-screen closing card, bottom-positioned bold italic
yellow captions with a dark outline,
and a normalized voice mix.
No stock media is downloaded. Supply an explicitly approved, hash-bound asset
catalogue with `-Assets path\to\assets.json` to add licensed B-roll. Background
Music is absent unless approved local files and provenance are provided. The three
built-in sound effects are synthesized locally, deliberately quiet, and recorded
in the same hash-bound asset catalogue as the graphics.

Caption timing in this command is estimated from the SRT plus measured speech
activity. It is more accurate than reusing paragraph timestamps, but is not
phoneme-level forced alignment; the creator must watch the complete render before
publishing. The timeline and render report deliberately retain
`final_human_review: REQUIRED`.

Optional free Pexels discovery is metadata-only and never downloads footage:

    $env:PEXELS_API_KEY = "your-key"
    $env:PYTHONPATH = "$PWD\src"
    python -m namakadhai.pexels_client "Tamil festival television" --out pexels-results.json

Review the Pexels licence and each candidate yourself, download only the selected
clips, then add their paths, hashes, provenance, and approval to an asset catalogue.

## Setup (Windows)
    # Python 3.11+; FFmpeg/FFprobe go in tools/ (see config/settings.json).
    # Fonts (Poppins, Varela Round; SIL OFL) are bundled in fonts/.

## Editing in OpenReel, finishing in Nama Kadhai

OpenReel (free, MIT) is the editor: timeline, trim/split/reorder, B-roll, music,
effects, undo. Nama Kadhai finishes the export: word-synced captions, Remotion
title/outro cards (free for individuals), and the approval-gated renders.

1. `.\openreel.ps1` opens OpenReel at `http://localhost:5173`. Edit your raw
   recording and export an MP4 **without captions**.
2. Finish it:

       .\caption.ps1 edited.mp4 captions.srt -Original raw_recording.mp4 `
           -Intro "Remote Sandai" -IntroSubtitle "Nama Kadhai" `
           -Outro "Nama Kadhai" -OutroSubtitle "Comment-la sollunga"

   or, in the app, put the export in **Video file path**, the SRT in **Captions
   file** and the raw recording in **Original recording**.

How the captions stay in sync after editing: the SRT is word-timed on the
**original** recording (where the text matches exactly), then the edited
export's audio is matched back to the original to find which parts were kept
(about a second for a minute of audio). Every word moves with its part; lines
you cut are skipped and listed. This needs the voice audio left as recorded:
added music is fine at a normal level, speed changes are not supported.

One-time setup, outside OneDrive (both install tens of thousands of files):

    git clone --depth 1 https://github.com/Augani/openreel-video.git $env:USERPROFILE\dev\openreel-video
    cd $env:USERPROFILE\dev\openreel-video
    npx -y pnpm@11.7.0 install --filter "@openreel/web..."
    npx -y pnpm@11.7.0 build:wasm
    cd <this project>
    $env:PYTHONPATH="src"; python -m namakadhai.remotion_cards setup   # Remotion + headless Chrome (~300 MB)

The card designs are in `remotion/src` (TitleCard, Outro); preview them with
`npm run studio` inside `%USERPROFILE%\dev\namakadhai-remotion`. Remotion is
free for individuals and companies of up to three people; larger companies
need a Remotion company licence.

## Captioning a video (one step, word-synced)

    .\caption.ps1 path\to\video.mp4 path\to\captions.srt

Output goes to a new folder `projects\captioned\<video>-<date-time>\`:
`<video>_captioned.mp4`, `captions.json` (per-word timings), `captions.ass`,
`alignment_report.json`. Nothing existing is overwritten.

- The SRT's **text and line grouping** are used; its timestamps are ignored and
  replaced by word timings measured from the audio (MMS forced alignment).
- Text must be **romanized** (Tanglish/English letters) and match what is actually
  said. Tamil script is not supported by the aligner.
- Style comes from `config/nama_kadhai_nostalgic.json` (Poppins Bold, bottom,
  pop-in, gold word highlight). Layout is picked from the video's orientation;
  override with `-Layout vertical|landscape`.
- Words listed under "Check these spots" had low confidence: usually the SRT
  says something slightly different there.

One-time setup of the alignment environment (about 1.5 GB with the model):

    uv venv --python 3.11 .venv-align
    uv pip install --python .venv-align\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1
    uv pip install --python .venv-align\Scripts\python.exe "numpy<2"

The model downloads to `models\torch\` on first run.

## Workflow (command line)
1. **Plan edits for review** (signal-level analysis only; does not change media)
   Set the package source path, then generate a review-only edit decision list:

       $env:PYTHONPATH = "$PWD\src"
       .\.venv\Scripts\python.exe -m namakadhai.edit_decision recording.mp4

   The EDL is saved under the recording's project folder in `edit_review\edit_decision_list.md`.
   It lists quiet intervals as candidate cuts with padding, highlights low/near-clipping
   audio levels for listening review, and includes approve/reject fields. It never applies
   cuts; quiet speech and expressive pauses must be checked by a person.

2. **Approve cuts and render a technical preview** (all decisions stay local)
   Create a versioned plan and approval log in a new timestamped directory under the
   recording's `edit_review` folder:

       .\.venv\Scripts\python.exe -m namakadhai.postproduction analyze recording.mp4

   Review `edit_plan.md` and `edit_plan.json`. Silence candidates are low-confidence
   suggestions only. Approve or reject each candidate by its ID; candidates still
   pending are never applied:

       .\.venv\Scripts\python.exe -m namakadhai.postproduction review "<edit_plan.json>" silence-<id> APPROVED --note "Listened and approved"

   Inspect the exact FFmpeg command without rendering or creating an output:

       .\.venv\Scripts\python.exe -m namakadhai.postproduction dry-run "<edit_plan.json>" --out ".\technical_preview.mp4"

   Rendering requires the separate `render` command. It creates a new MP4 plus a
   validation report and refuses to overwrite any existing destination:

       .\.venv\Scripts\python.exe -m namakadhai.postproduction render "<edit_plan.json>" --out ".\technical_preview.mp4"

   Voice audio is loudness-normalized and limited conservatively; this is not
   denoising. Optional, explicit picture adjustments are available with
   `--video-correction --brightness 0.01 --contrast 1.02 --saturation 1.0` and
   `--white-balance --white-balance-red -0.01 --white-balance-blue 0.01`.
   Both are off unless requested.

   Optional music must be provided as a local file and is mixed under the original
   voice. Supply its provenance with `--music-source-url`, `--music-provider`,
   `--music-license`, `--music-attribution`, and `--music-evidence`. The
   `--confirm-music-license` flag records your confirmation that the supplied license
   permits the intended use, including monetization; it is not independent legal
   verification. Without complete confirmation, the provenance report marks the
   track UNVERIFIED and warns not to publish.

   Music ducking is optional and off by default. When using local music, enable
   FFmpeg side-chain compression with `--music-ducking`; adjust
   `--music-ducking-threshold-db`, `--music-ducking-ratio`,
   `--music-ducking-attack-ms`, and `--music-ducking-release-ms` as needed.
   Both render workflows measure the final mix with FFmpeg `ebur128=peak=true`
   and record integrated LUFS and true peak in the report. Configurable warning
   thresholds are `--loudness-warning-tolerance-lu` and
   `--true-peak-warning-dbtp` in this standalone post-production CLI. In the
   story-editor profile, configure them under `audio.validation`. Output duration
   uses frame-rate tolerance; reported audio/video stream start and duration
   differences are checked for sync. These checks flag issues but do not certify
   a mix for broadcast.

   The source video is hash-checked and never edited. Approval records are append-only
   and bound to the exact plan contents. A changed source or plan invalidates review.
   LosslessCut remains useful for manual visual/audio review, but this workflow does
   not automate story-aware cuts or noise removal.

## End-to-end story editor

The Nama Kadhai editor uses the existing Python/FFmpeg stack. It writes a
source-hash-bound plan, records human decisions in an append-only log, and renders
separate 1920×1080 landscape and 1080×1920 vertical previews. The local browser UI
supports project analysis, suggestion review, manually reviewed captions,
preview playback, final approval, and a manual publishing package. It does not
transcribe, download models, change story order, or modify the source video.

### Start the local browser editor (Windows PowerShell)

From the project directory, set the source path and start the local-only server:

    $env:PYTHONPATH = "$PWD\src"
    python -m namakadhai.ui

The editor opens at `http://127.0.0.1:8765`. It binds to localhost only; press
Ctrl+C in the terminal to stop it. Core editing uses the bundled FFmpeg tools and
Python standard library. Enter a local video path, review the proposed changes,
and render both previews from the Previews tab. Final approval and package creation
remain explicit actions; the app records each preview review only after playback
reaches the end, binds that review to the rendered file hash, and requires a
non-empty approval note. The CLI finalizer also requires the matching review
receipts written by the application; a note alone is not enough. Nothing is
published automatically.

Render-job records are stored under `projects\app\render_jobs`. If the server
restarts while a render is queued or running, that attempt is marked interrupted
and cannot be finalized. The Previews panel offers an explicit retry for failed,
cancelled, or interrupted attempts; each retry uses a fresh attempt directory and
preserves earlier outputs. On restart, successful job output and report hashes
are rechecked before the job remains marked successful.

Story-edit audio is loudness-measured and normalized in two passes after voice
and optional music are mixed, then resampled and validated at 48 kHz. Silence
cut candidates are snapped inward to the source frame grid. Their crossfades
are capped at the retained silence padding; hard cuts use short audio fades.
Caption cues crossing a removed source interval are omitted rather than shown
twice and are reported for manual correction.

If port 8765 is already in use, start it on another local port, for example:

    python -m namakadhai.ui --port 18765 --no-browser

Transcription stays disabled. To add captions, supply manually timed Tanglish JSON
that you have checked against the source. The UI will not infer or correct dialogue.

Set the package path once in the VS Code PowerShell terminal:

    $env:PYTHONPATH = "$PWD\src"

Create a story plan. The profile defaults to `config\nama_kadhai_nostalgic.json`.
Titles and captions are always supplied by you; dialogue is never invented:

    & .\.venv\Scripts\python.exe -m namakadhai.story_editor plan "C:\path\to\recording.mp4" --title "A story from home"

To include reviewed Tanglish captions, prepare a JSON file and pass it using
`--captions`. Set the top-level `reviewed` value to `true` only after checking each
line against your transcript/audio. `text` is used as written; lines marked
`"uncertain": true` are omitted from both exports.

    {
      "version": 1,
      "reviewed": true,
      "captions": [
        {
          "start": 5.7,
          "end": 7.8,
          "text": "Your exact reviewed Tanglish words",
          "highlight": ["one"],
          "uncertain": false
        }
      ]
    }

For local background music, add `--music "C:\path\to\licensed-track.mp3"` and
provide its provenance fields (`--music-provider`, `--music-license`,
`--music-evidence`, and attribution/source URL if applicable). The optional
`--confirm-music-license` records your confirmation only; it is not legal
verification. Unverified tracks are marked as such and should not be published.
The track is not downloaded by the workflow.

Music ducking is available but disabled by default. To try it, edit the profile's
`audio.ducking.enabled` to `true` before creating a plan. Its side-chain threshold,
ratio, attack, and release remain configurable there. FFmpeg uses the normalized
voice as a level detector; this is not speech recognition or a word-level gate.
Every render now runs an EBU R128 measurement pass and records integrated LUFS,
true peak, stream durations, and any configured-threshold warnings in each render
report. Duration is checked against the expected timeline within 1.5 output frames
plus two audio samples; an audio/video stream-duration mismatch beyond that frame
tolerance fails validation. These measurements are technical checks, not broadcast
certification.

The `plan` command prints the new plan and report paths. Review the Markdown plan,
then use its candidate IDs and the special item IDs below to record decisions:

    & .\.venv\Scripts\python.exe -m namakadhai.story_editor review "<story_edit.json>" "<silence-candidate-id>" APPROVED --note "Reviewed the source"
    & .\.venv\Scripts\python.exe -m namakadhai.story_editor review "<story_edit.json>" captions APPROVED --note "Checked against reviewed transcript"
    & .\.venv\Scripts\python.exe -m namakadhai.story_editor review "<story_edit.json>" title APPROVED --note "Title is accurate"
    & .\.venv\Scripts\python.exe -m namakadhai.story_editor review "<story_edit.json>" music APPROVED --note "Checked local track provenance"
    & .\.venv\Scripts\python.exe -m namakadhai.story_editor render "<story_edit.json>"

Only approve IDs for items present in that plan. Pending/rejected cuts remain intact;
captions, title, and music are omitted unless separately approved. Approved silence
cuts receive a brief dissolve and voice crossfade; no other transitions are added.
The renderer records exact settings, FFmpeg commands/logs, source and asset hashes,
provenance, and FFprobe validation in each format's render report. Existing outputs
are never overwritten. A new plan creates a fresh output folder.

After watching both `review_landscape.mp4` and `review_vertical.mp4` and checking
their render reports, explicitly approve the reviewed previews for export:

    & .\.venv\Scripts\python.exe -m namakadhai.story_editor finalize "<story_edit.json>" --note "Reviewed both formats"

Finalization requires every planned creative decision to be either approved or
explicitly rejected, and verifies that the source, reviewed assets, approvals, and
render reports still match. It creates `final_landscape.mp4`,
`final_vertical.mp4`, and `final_approval.json` in that plan's `exports` folder.
The MP4s are hard links to the already-rendered review previews: finalization
does not re-encode them. Existing final outputs are never overwritten.

### Optional Ollama suggestions

`suggest` contacts only an already-running Ollama service on `127.0.0.1`, and only
uses a model that `/api/tags` reports as installed. It never starts Ollama, pulls a
model, or records approvals. Its schema-checked output is a review-only suggestion
file. It can only refer to candidate/item IDs in the plan:

    & .\.venv\Scripts\python.exe -m namakadhai.story_editor suggest "<story_edit.json>" --model "qwen2.5-coder-8k:7b"

This is optional and not part of rendering. A 7B model file is several gigabytes;
on an 8 GB machine, keep Ollama stopped unless you deliberately choose to try it.
Without reviewed captions, the planner has no dialogue/story text and cannot infer
the story from silence measurements.

### Current milestone boundaries

**Implemented:** a local browser review interface; review-only silence candidates,
append-only approvals, stale-source and stale-caption/music detection, exact
reviewed captions with selective gold emphasis and gentle cue/title fades, short
dissolves at approved cuts, a subtle configurable warm profile, blurred-fill
landscape output to preserve portrait-source framing, configurable vertical crop,
optional locally supplied music under voice with opt-in side-chain ducking, EBU
R128 integrated-loudness/true-peak measurement, frame-aware duration and
audio/video stream-duration checks, dual exports, and synthetic end-to-end tests.

**Not yet implemented:** an LLM-driven planner integrated into the browser, B-roll
insertion and its asset browser, sound-effects selection, noise-removal
enhancement, story-order changes, face-aware vertical reframing, and MCP controls.
Ollama suggestions are available only as an optional CLI workflow. The remaining
features need separate asset/provenance and approval workflows before use; no
synthetic visual or dialogue is silently substituted. Voice processing currently
uses loudness normalization and a limiter, not denoising. Music ducking is disabled
in the default profile. Measured levels outside configured thresholds produce
warnings; they do not assert that every mix is broadcast-ready. Synthetic renders
validate mechanics and format dimensions, not professional appearance on real
footage; review the actual preview before judging the profile.

Remotion was not added: ASS/FFmpeg covers the current caption/title animation and
format-render needs without another Node/React rendering stack. Revisit it only if
the actual preview shows a motion-graphics requirement FFmpeg cannot meet; adding it
would require separate runtime/resource and licensing evaluation.

See [FEATURE_STATUS.md](./FEATURE_STATUS.md) for the requirement-by-requirement
status matrix, evidence, dependencies, and prioritized acceptance work.

## What "properly" means here
- Every caption word is timed against the audio (MMS forced alignment). A caption
  appears 0.15 s before its first word and the word being spoken turns gold.
- Sizes are fractions of frame height, so small previews and 4K finals look alike.
  Captions sit at the bottom: 7.5% margin in landscape, 10% in vertical.
- Each SRT line becomes one caption of at most two lines; captions never overlap.
- Portrait footage in landscape uses a blurred, darkened fill behind the
  aspect-fit source image. Vertical output centre-crops (adjustable crop position).

## Tuning
Caption font, colours, outline, animation and word highlight are in
`config/nama_kadhai_nostalgic.json` (`caption`). Size and margins per layout are
`LAYOUTS` in `src/namakadhai/caption_renderer.py`. Card designs are in `remotion/src`.

## Tests
    $env:PYTHONPATH = "$PWD\src"
    python -m unittest discover -s tests -v
