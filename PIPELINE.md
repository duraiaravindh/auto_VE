# Cinematic edit pipeline

Windows, Python and FFmpeg, local only, no paid services. Raw footage is only ever read.

| Step | Output | Status |
|---|---|---|
| 1. Analyse references | `analysis/reference_analysis.md`, `style_guide.json`, `reference_contact_sheet.jpg` | Done; style guide needs approval |
| 2. Analyse raw footage | `footage_inventory.json`, `footage_contact_sheet.jpg`, `transcript.srt`, `shot_candidates.json` | Waiting for footage; script not built |
| 3. Edit decision list | `project/edit_decision_list.json`, validated | Not built |
| 4. 720p proxies and rough cut | `proxies/`, `previews/` | Not built |
| 5. Preview for approval | `previews/`, with shots, timeline, audio plan and grade | Not built |
| 6. Final after your explicit approval | `5_exports/` | Not built |

To repeat step 1: `python engine/analyze_references.py` from the `AI Video Editor` folder.

Rendering can reuse the engine in `engine`, which already handles cropping, B-roll, zoom, grade, captions, loudness and QC.

## Story Reels (Remotion)
Each story has `stories/<story>-v1/`, `stories/<story>-v1/beats.json` and a composition in `remotion/src/Root.tsx`. All stories share `remotion/src/Story.tsx`. From the `AI Video Editor` folder, in order:
1. `python engine/nk.py plan stories/<story>-v1/edit.json`
2. `python engine/build_story.py <story>` (rule checks; writes `<story>.json` and the sound cues)
3. `python engine/nk.py base stories/<story>-v1/edit.json` (only if timing or sound changed), then repeat step 2 for the true length
4. `python engine/extract_frames.py <story>` (temporary images; Remotion's video decoder crashes on this machine)
5. In `remotion`: `npx remotion bundle src/index.ts --out-dir <folder>`, then `bash ../engine/render_chunks.sh <folder> <Composition> <frames> ../4_previews/<story>-v1 ../4_previews/<story>-v1/<story>-v1-base.mp4`

Delete the old `parts` folder before re-rendering, or finished chunks are reused. Delete `remotion/public/seq` afterwards.
