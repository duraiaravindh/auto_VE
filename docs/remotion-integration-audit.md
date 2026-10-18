# Remotion / OpenReel integration audit

**Date:** 2026-10-18
**Scope:** audit only. Nothing was installed, downloaded or changed in the application.

## Summary

The integration plan assumes this project is an **OpenReel (React/TypeScript)
editor**. It is not. Nama Kadhai is a **Python + FFmpeg** application with a
single-file HTML/JavaScript browser UI. There is no OpenReel code, no React, no
TypeScript and no `package.json` in this repository or its sibling folders.

Because of that, the plan's integration steps (embedding the Remotion Player in
the React app, routing AI proposals through OpenReel editing actions) have nothing
to attach to. Adopting OpenReel would mean replacing the current editor, not
extending it.

For the features the plan targets first (animated titles and captions), the
existing ASS/libass + FFmpeg pipeline already delivers them: Poppins Bold, pop-in
entrance, a word-by-word highlight timed by forced alignment, and a title card.
Remotion would add a second rendering stack without a capability the current
videos need.

**Recommendation:** do not add OpenReel or Remotion now. Revisit Remotion only for
a specific motion graphic that ASS can't do (see "When Remotion would be worth
it").

## Findings

### 1. Languages, runtimes and package managers

| Item | Found |
|---|---|
| Application language | Python 3.11 (standard library only; `requirements.txt` is empty) |
| UI | One file, `src/namakadhai/ui_assets/index.html`: plain HTML, CSS and inline JavaScript. No framework, no build step |
| Media engine | FFmpeg/FFprobe 9.0.2, bundled in `tools/` |
| Word alignment | Separate `.venv-align` (Python 3.11, torch 2.5.1 and torchaudio 2.5.1 CPU builds), called as a subprocess |
| React / TypeScript / `package.json` | None |
| Node.js / npm | Installed on the computer (Node 22.15.0, npm 10.9.2) but not used by this project |
| OpenReel | Not present in this repository or the sibling folders under `Desktop\Personal` |

### 2. Timeline and editing actions

There is no multi-track NLE timeline. Editing is plan-based:

- **Edit plan:** `story_edit.json`, bound to the source video's SHA-256 hash. It
  contains cut candidates, captions, title and music (`story_editor.py`).
- **Decisions:** appended to `approvals.jsonl`. Every cut starts as pending and
  only approved cuts are applied.
- **Applying cuts:** retained ranges are computed from approved cuts.
  `_map_captions` moves caption and word timings onto the edited timeline.
- **Revisions:** a caption revision creates a new plan revision, and reviews tied
  to an older plan become invalid.

### 3. Preview, text animation, graphics and export

| Area | Current implementation |
|---|---|
| Preview | The browser plays the rendered MP4 previews. No live compositing |
| Captions | ASS subtitles rendered by libass inside FFmpeg (`caption_renderer.py`): Poppins Bold, bottom placement, pop-in entrance, gold word highlight |
| Caption timing | Word-level forced alignment (MMS_FA) from an SRT (`forced_align.py`) |
| Title card | ASS overlay with a fade (`build_ass`, `title_text`) |
| Graphics | None beyond captions and the title card |
| Export | Landscape and vertical renders, two-pass loudness normalisation, validated render reports, hash-bound preview reviews, a gated finalize step, and a local publishing package |

### 4. AI features and network use

| Feature | Runs where | Network |
|---|---|---|
| Caption word alignment (MMS_FA) | Local CPU | One-time model download (about 1.2 GB) to `models/torch/`, already done |
| Transcription (faster-whisper) | Local | Configured, but the package and model are not installed |
| Cut suggestions | Local Ollama at `127.0.0.1:11434` | This computer only |
| Silence and audio analysis | FFmpeg | None |

No cloud APIs. Footage is never uploaded.

### 5. Persistence, undo, autosave and crash recovery

- **Project records:** `project.json` per project, plus `events.jsonl` (an event
  log) and a list of plan revisions.
- **Render jobs:** persisted to disk. Jobs still queued or running when the
  server restarts are marked as interrupted. Retries write to new attempt
  folders and never overwrite.
- **Undo/redo:** not present. Plan revisions and the decision log play this role.
- **Autosave:** server-side state is written on every action. Unsaved caption
  edits in the browser exist only in that page's memory.
- **Original media:** never modified. Its hash is checked when a project is
  created and when rendering.

### 6. Dependencies and licences

| Component | Licence | Note |
|---|---|---|
| FFmpeg full build | GPL | Fine for local use. Redistributing the editor with it carries GPL obligations |
| Poppins, Varela Round | SIL OFL 1.1 | Free, including commercial use |
| PyTorch, torchaudio | BSD | |
| **MMS_FA model weights** | **CC-BY-NC 4.0 (non-commercial)** | **Needs a decision:** if the channel is monetised or the videos are commercial work, check whether this licence permits that use. If not, switch to a commercially licensed aligner (for example Whisper word timestamps, MIT) |
| Remotion (if added) | Remotion licence | Free for individuals and companies of up to three people. Building and distributing an automated editing product may need a company licence. Not added |

### 7. Would Remotion add capabilities?

| Need | Covered today? | Remotion adds |
|---|---|---|
| Animated captions with word highlight | Yes (ASS) | Nothing essential |
| Title card | Yes (ASS fade) | Richer animation (springs, multiple layers) |
| Lower thirds, shapes, images, emoji, progress bars, animated layouts | No | **Yes**: this is where Remotion is strong |
| Live in-browser preview of graphics | No (renders MP4 first) | Yes, via the Player, but only in a React UI, which this project doesn't have |

Costs of adding Remotion:
- **Software stack:** a second build and render stack (Node, React and
  TypeScript bundling).
- **Large download:** rendering needs a headless Chromium download.
- **Speed:** frame-by-frame browser rendering is much slower than libass, and
  the output would need an extra FFmpeg compositing pass.
- **Memory:** this computer has about 14 GB of RAM, more than the plan's 8 GB
  target. Remotion's peak memory use while rendering is still unmeasured.

## When Remotion would be worth it

If you want a specific graphic ASS can't do (an animated lower third with an
image, an animated progress bar, animated emoji or stickers), the smallest safe
step is:

1. A separate `remotion/` folder that renders **one** composition to a
   transparent overlay file. It would take typed, validated props, run as a
   cancellable subprocess, and leave the Python app unchanged.
2. FFmpeg composites that overlay during the existing render. Story plan, review
   and export gates stay as they are.
3. Measure render time and peak RAM on a 1080p, 70-second clip before going
   further.

This needs your approval first. It installs npm packages and downloads Chromium,
and it needs the licence checked.

## Open questions

1. Do you have an OpenReel checkout somewhere else? If so, share its location.
   The plan in the pasted text only applies to that codebase.
2. Is there a specific graphic you want that the current captions and title
   can't do? That decides whether Remotion is needed at all.
3. Is the channel monetised or commercial? This decides whether the MMS_FA
   caption aligner's non-commercial licence is acceptable.
