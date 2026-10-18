# Nama Kadhai Video Editor — Work Overview

**Prepared:** 2026-10-17  
**Purpose:** Handoff context for continuing this project with Claude or another coding assistant.

## Project in brief

Nama Kadhai is an offline-first local video editing and review application built
with Python, FFmpeg/FFprobe, and a browser UI. It creates a source-hash-bound
edit plan, stores review decisions, and renders separate landscape and vertical
video previews. Final approval and creation of a local publishing package are
explicit gated actions; the application does not publish or upload content.

The project already contains a substantial renderer and workflow. Continue the
existing implementation; do not rebuild it or replace the renderer.

## Current state

- The local browser app can open a project, show analysis and render state, play
  the two previews, and expose approval and package controls.
- A real-media project for `test001.mp4` exists:
  `test001-end-to-end-validation-20261016-170129-ce6463fc`.
- Its current conservative previews are rendered and validated:
  - Landscape: 1920×1080, 70.314667 seconds.
  - Vertical: 1080×1920, 70.314667 seconds.
- The current preview set was reopened in the browser. Both players loaded
  metadata and briefly advanced during a playback smoke test. This was not a
  complete watch-through and is **not creator review**.
- Both preview-review records are currently empty. The automated playback
  review created during testing was cleared and the project event log records
  that it was automated, not creator approval.
- Project state is `PREVIEW_READY`; no finalization or publishing package
  exists. The final approval and package controls were verified disabled until
  the current previews are watched through and the required approval is given.
- Render-job recovery and retry are implemented and regression-tested, but
  interruption/retry has only been simulated in tests—not tested by stopping a
  real FFmpeg render.
- The latest code changes add application-written, plan/output-hash-bound
  preview-review receipts. The CLI finalizer now requires those receipts for
  both current preview files, in addition to its previous decision, report,
  hash, and note checks.
- The story renderer now uses a loudness measurement pass and a measured
  normalization pass after optional music mixing, forces/validates 48 kHz audio,
  duplicates the voice pad safely for sidechain ducking, and explicitly
  normalizes voice/music formats before mixing.
- The separate legacy `postproduction` CLI now normalizes the final mix in
  explicit dynamic single-pass mode and validates 48 kHz output. It has not yet
  been migrated to the story renderer's measured two-pass workflow.
- Silence-cut boundaries are snapped inward to the source frame grid; video
  segments use a common CFR/timebase; silence crossfades are capped at the
  retained-silence padding; hard-cut joins receive short audio fades. Caption
  cues crossing a removed interval are omitted rather than duplicated.
- Windows output publishing falls back to a no-overwrite rename when the
  filesystem does not support hard links. Source integrity hashing was reduced
  to an initial and a final full-file verification in the render paths.
- No fresh real-media project import, creator review, finalization, or real
  package generation has been completed. Therefore, the end-to-end acceptance
  workflow has **not passed**.

## Source and integrity

- Input: `test001.mp4` (portrait H.264/AAC).
- Verified SHA-256:
  `BA12A226B9DD2079D0C8B31CD3F8F4A3621C68508BE1D458427FF3099F219033`
- The hash matched the project record during the latest verification. Existing
  previews and render reports were also checked and remain validated.
- Never modify, replace, or overwrite the original recording. Preserve existing
  output files and diagnostics.

## Implemented workflow and recovery

- Local project records store the source hash, metadata, analysis, active plan,
  render outputs, preview-review hashes, finalization, and package state.
- Edit decisions are recorded against plan revisions. Changes invalidate
  reviews tied to stale previews.
- Render attempts persist their state, revision, requested formats, output
  locations, identifiers, and progress.
- Queued/running attempts found on server restart are marked interrupted.
- Explicit retries use unique attempt-specific output directories, avoid
  overwriting valid outputs, and verify output/report hashes before success.
- Interrupted, failed, or otherwise non-succeeded render attempts cannot be
  finalized.
- Publishing packages are local only and are bound to approved output/report
  hashes.

## Validation completed

Latest validation:

- `python -m unittest discover -s tests` — **112 tests passed**.
- `python -m compileall -q src tests` — passed.
- Inline JavaScript from `src/namakadhai/ui_assets/index.html`, checked with
  `node --check` — passed.
- A disposable synthetic dual-format FFmpeg render with ducking enabled,
  two-pass loudness normalization, approval review receipts, and finalization
  integrity checks passed.
- Browser smoke test: existing project opened; both preview players loaded
  metadata and briefly played; approval and package buttons were disabled.
- Health endpoint reported the offline core enabled and transcription disabled.
- Original source hash remained unchanged.

The repository's checked-in `.venv` has a stale Python base path. Tests were
run with system CPython 3.12 and `PYTHONPATH` set to `src`. No dependencies,
models, or external assets were installed or downloaded. The workspace was
reported as not being a Git repository.

## Important files

- `src/namakadhai/ui.py` — local HTTP UI, render job manager, persistence,
  restart recovery, and retry endpoint.
- `src/namakadhai/application.py` — project lifecycle, render-attempt
  association, preview review, and finalization gates.
- `src/namakadhai/story_editor.py` — planning, rendering, and finalization.
- `src/namakadhai/publishing.py` — local package generation and integrity
  checks.
- `src/namakadhai/ui_assets/index.html` — browser application.
- `tests/test_ui.py`, `tests/test_application.py`,
  `tests/test_story_editor.py`, `tests/test_publishing.py` — relevant automated
  coverage, including render recovery and approval-integrity checks.
- `README.md` — startup and workflow instructions.
- `FEATURE_STATUS.md` — detailed requirement/acceptance matrix.
- `projects/app/test001-end-to-end-validation-20261016-170129-ce6463fc/` —
  existing real-media project record, events, and analysis.

The latest implementation work changed the UI, application coordinator,
renderer, publishing code, their related tests, and the README/feature matrix.
The existing project's event log also contains entries from prior UI
evaluation and the later clearing of an automated review; retain the audit
history.

## Safety and product constraints

1. Do not finalize the current project or create its publishing package without
   the creator's explicit approval.
2. Do not record a preview review on the creator's behalf. Automated playback,
   test scripts, or short playback probes are not creator approval.
3. Do not run a real render interruption test on the existing project. Use a
   disposable project/source copy if testing real process interruption.
4. Do not use transcription or Whisper; do not download models, packages, or
   external assets without permission.
5. Do not invent dialogue, captions, music, or story content.
6. Keep the current conservative previews unless the creator requests a
   revision. Successful rendering does not establish professional creative
   quality.
7. Keep the app offline-capable and mindful of low-memory hardware.

## Remaining engineering gaps

- The main renderer still builds one graph containing all retained segments.
  Intermediate segment-group rendering is needed before claiming comfortable
  support for long recordings with hundreds of cuts on low-memory hardware.
- Silence detection still uses the configured fixed dBFS threshold. Do not
  substitute an automatic noise-floor heuristic until it has been validated
  against quiet speech and representative noisy recordings.

## Recommended next steps

1. Reconfirm whether the local server is running before treating a browser
   connection failure as an application bug. The server was stopped after the
   latest verification; the browser tab may remain open but need not be live.
2. If continuing the acceptance test, reopen the existing project and verify
   the current review state and disabled approval/package gates. Do not
   complete playback on the creator's behalf.
3. Have the creator personally watch both previews. Only after explicit
   approval should finalization be attempted; package creation should follow
   only if separately authorized by the applicable workflow.
4. Test a real interrupted render and retry on a disposable project. Preserve
   partial diagnostics and confirm that previous outputs remain untouched.
5. Plan bounded intermediate segment-group rendering for long, heavily-cut
   videos before adding a synthetic load test.
6. Consider fresh UI import-to-preview acceptance after the above gates. Report
   each observed step and blocker without overstating completion.

## Startup / test commands (Windows PowerShell)

From the workspace root:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m namakadhai.ui
```

The default address is `http://127.0.0.1:8765`. To use port 18765 without
opening a browser automatically:

```powershell
python -m namakadhai.ui --port 18765 --no-browser
```

Focused handoff validation commands:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

Use the system Python 3.12 executable if the checked-in `.venv` is still
unusable, and set `PYTHONPATH` to the workspace `src` directory.
