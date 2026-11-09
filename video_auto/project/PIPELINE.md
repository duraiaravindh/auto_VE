# Cinematic edit pipeline

Windows, Python and FFmpeg, local only, no paid services. Raw footage is only ever read.

| Step | Output | Status |
|---|---|---|
| 1. Analyse references | `analysis/reference_analysis.md`, `style_guide.json`, `reference_contact_sheet.jpg` | Done; style guide needs approval |
| 2. Analyse raw footage | `footage_inventory.json`, `footage_contact_sheet.jpg`, `transcript.srt`, `shot_candidates.json` | Waiting for footage; script not built |
| 3. Edit decision list | `project/edit_decision_list.json`, validated | Not built |
| 4. 720p proxies and rough cut | `proxies/`, `previews/` | Not built |
| 5. Preview for approval | `previews/`, with shots, timeline, audio plan and grade | Not built |
| 6. Final after your explicit approval | `exports/` | Not built |

To repeat step 1: `python video_auto/project/analyze_references.py` from the `AI Video Editor` folder.

Rendering can reuse the engine in `edited/v2/engine`, which already handles cropping, B-roll, zoom, grade, captions, loudness and QC.

## Story Reels (Remotion)
Each story has `edited/v2/edits/<story>-v1/`, `video_auto/project/<story>_beats.json` and a composition in `remotion/src/Root.tsx`. All stories share `remotion/src/Story.tsx`. From the `AI Video Editor` folder, in order:
1. `python edited/v2/engine/nk.py plan edited/v2/edits/<story>-v1/edit.json`
2. `python video_auto/project/build_story.py <story>` (rule checks; writes `<story>.json` and the sound cues)
3. `python edited/v2/engine/nk.py base edited/v2/edits/<story>-v1/edit.json` (only if timing or sound changed), then repeat step 2 for the true length
4. `python video_auto/project/extract_frames.py <story>` (temporary images; Remotion's video decoder crashes on this machine)
5. In `video_auto/project/remotion`: `npx remotion bundle src/index.ts --out-dir <folder>`, then `bash ../render_chunks.sh <folder> <Composition> <frames> ../../previews/<story>-v1 ../../previews/<story>-v1/<story>-v1-base.mp4`

Delete the old `parts` folder before re-rendering, or finished chunks are reused. Delete `remotion/public/seq` afterwards.
