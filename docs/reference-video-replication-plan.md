# Reference-inspired dynamic vertical video plan

## Objective

Create a repeatable, local-first editing mode that turns `test001.mp4` plus
`subtitles.srt` into a polished 9:16 talking-head story with the same *editing
grammar* as `reference.mp4`: fast pacing, short kinetic captions, relevant
B-roll, selective split-screen/PiP layouts, restrained emphasis transitions,
and a finished social-audio mix.

This plan reproduces the style and structure, not the reference video's
copyrighted movie clips, music, branding, or exact graphic assets.

## What the reference actually does

### Measured media facts

| Property | Reference |
|---|---:|
| Duration | 84.05 s |
| Frame | 480 x 854, vertical 9:16 |
| Frame rate | 30 fps, constant |
| Video/audio | H.264 + AAC stereo |
| High-confidence visual changes | 35 |
| Mean detected shot interval | 2.33 s |
| Median detected shot interval | 1.93 s |
| Longest detected interval | 6.93 s |
| Integrated loudness | -14.2 LUFS |
| True peak | -0.7 dBFS |
| Silence at -35 dB for 250 ms+ | none detected |

The cut count includes frames inside the two bright transition sequences, so it
should be treated as a pacing measurement rather than an exact manual edit list.

### Editing grammar

1. **Immediate hook:** the presenter starts full-frame, looking at camera. There
   is no logo intro before the first spoken idea.
2. **Rapid visual proof:** after about one second, supporting footage appears.
   The opening uses a circular presenter cutout over a centred B-roll clip.
3. **Presenter remains the anchor:** the video repeatedly returns to a clear
   medium close-up. B-roll illustrates a phrase; it does not replace the story.
4. **Three B-roll layouts:**
   - full-frame or centred vertical insert;
   - top-half B-roll with the presenter in the bottom half;
   - full-frame B-roll for an emotional or recognisable moment.
5. **Short Tanglish captions:** most caption events are a few words, not an
   entire SRT sentence. White text carries the sentence; one important word is
   occasionally larger and warm yellow/cream.
6. **Kinetic emphasis:** large keywords, an occasional emoji/icon, light scale
   pops, and simple position changes create motion. The reference does not make
   every word bounce.
7. **Mostly hard cuts:** ordinary idea changes use cuts. A bright light sweep
   near 7 s and a vertical smear/white flash near 78 s mark major emphasis.
8. **Continuous energy:** speech and a quiet audio bed leave no long silent
   gaps. The loudness is typical of social video, with a fairly compressed
   3.6 LU loudness range.
9. **Dark lower gradient:** many shots have a subtle bottom vignette/gradient so
   white captions remain readable without a large caption box.

## What `test001` needs

`test001.mp4` is 70.31 s, 478 x 850 at 30 fps with 48 kHz stereo AAC.
`subtitles.srt` covers 0.00-70.20 s in 15 long blocks averaging about 4.7 s.
Those blocks are suitable as transcript paragraphs, but not as final on-screen
caption events.

The first output should therefore:

- keep the exact reviewed Tanglish wording;
- align words to speech, then split the 15 SRT blocks into short 1-5 word cues;
- never place a cue only from the original SRT boundary if the spoken word is
  later or earlier;
- create the dynamic edit without requiring the UI;
- use only local or explicitly approved assets;
- render 1080 x 1920 at 30 fps, AAC 48 kHz;
- target -14 to -16 LUFS and no more than -1 dBTP true peak.

## Existing capability versus required work

| Area | Already in the application | Required for this style |
|---|---|---|
| Safe projects | Source hashing, separate outputs, revision/approval records | Reuse unchanged |
| Captions | ASS/libass, pop/fade, highlighting, reviewed inputs | Word-aligned cue splitting, per-token scale/color templates, safe-area collision checks |
| Render | FFmpeg graph, H.264/AAC, vertical/landscape, validation | Multi-layer timeline: presenter, B-roll, PiP mask, gradients, stickers and SFX |
| Transitions | Hard cut or one global fade | Allowlisted per-event transitions: hard cut, light sweep, short blur, smear/flash |
| B-roll | Not integrated | Asset catalogue, placement plan, crop/layout templates, provenance and approval |
| Framing | Fixed crop/blurred fill | Optional face-aware crop and presenter cutout tracking |
| Audio | Loudness normalization, optional music and ducking | Reference-style social target, small transition SFX, event-level fades |
| Planning | Silence/transcript suggestions and optional local Ollama | Structured visual beat planner that never downloads or approves assets |
| QA | Duration, streams, sync, loudness, hashes | Layer bounds, caption safe areas, missing assets, flash duration and transition density |

## Proposed implementation

### 1. Add a creative timeline, not more ad-hoc filter strings

Introduce a versioned `creative_timeline.json` which the renderer validates.
Every operation must be an allowlisted template with numeric limits.

Each timeline event should contain:

- output start/end;
- source clip and source in/out;
- layout: `presenter_full`, `broll_full`, `split_top`, or `circle_pip`;
- crop/scale coordinates for each layer;
- caption cue IDs and highlighted word IDs;
- transition in/out and duration;
- optional SFX/music event;
- asset ID, source URL, license, attribution, local hash, and approval status;
- confidence/reason and creator approval status.

Suggested modules:

- `creative_timeline.py`: schema, validation, revision hashes;
- `asset_catalog.py`: local assets and license/provenance records;
- `composition.py`: FFmpeg layer graph and layout templates;
- `motion_templates.py`: captions, light sweep, blur and flash templates;
- `visual_planner.py`: transcript-to-beat suggestions only;
- `reframe.py`: optional CPU face tracking and smoothed crop paths;
- existing `story_editor.py`: workflow, approvals, audio and final rendering;
- existing `caption_renderer.py`: ASS generation and subtitle sidecars.

### 2. Fix caption timing before adding visual effects

Use this sequence:

1. Parse `subtitles.srt` as approved text, not trusted timing.
2. Obtain word timestamps from local `faster-whisper` or the existing forced
   aligner. The model must already exist locally; the app must not download it.
3. Match the approved Tanglish words to measured speech words. Preserve the SRT
   spelling in the output.
4. Flag unmatched/low-confidence words instead of inventing timings.
5. Split on word gaps and semantic punctuation into 1-5 word events, normally
   0.65-2.2 s each.
6. Apply 40-80 ms visual lead-in and a short tail, while preventing overlap.
7. Re-map word times after cuts and transitions.

Caption templates for the first version:

- `sentence_small`: white Poppins/Varela, lower third, light shadow;
- `keyword_gold`: one large cream/gold keyword with a 120-180 ms scale pop;
- `keyword_stack`: small context line above one large keyword;
- `quote_card`: centred handwritten-style quote only for a deliberate beat;
- optional approved emoji/sticker beside a keyword.

No cue may cover the face or the bottom platform controls. Use separate safe
areas for plain presenter shots, split-screen shots, and full-screen B-roll.

### 3. Build four deterministic visual layouts

1. **Presenter full:** subtle 102-106% push-in over 2-4 s; bottom readability
   gradient; captions in lower-middle safe area.
2. **Split top:** B-roll in the top 45-52%; presenter in the lower region;
   caption placed near the seam or above the bottom UI zone.
3. **B-roll full:** approved asset fills frame; presenter is hidden; captions
   remain visible.
4. **Circle PiP:** B-roll centred, black/graded background, presenter face/torso
   inside a circular mask near the lower centre. Start with a manually defined
   crop; add tracking only after it is tested.

### 4. Add a small transition library

The reference style needs event transitions, not a fade on every cut.

| Transition | Use | Duration |
|---|---|---:|
| Hard cut | Same topic, normal B-roll swaps | 0 s |
| Soft scale/blur | Return to presenter or minor emphasis | 0.15-0.25 s |
| Light sweep | First major reveal/chapter | 0.35-0.55 s |
| Vertical smear + flash | Strong payoff or final chapter | 0.25-0.45 s |

Implementation can use the bundled FFmpeg filters already present: `overlay`,
`xfade`, `zoompan`, `gblur`, `tmix`, `drawtext`, `colorkey/chromakey`, and
generated gradient/light mattes. Keep roughly 80-90% of changes as hard cuts and
limit strong flash transitions to about 1-3 per minute. Provide a reduced-flash
mode and reject long or repeated full-white frames.

### 5. Add B-roll as an approved asset workflow

The planner extracts concrete visual phrases from the reviewed transcript, for
example `festival`, `family watching television`, `commercial break`, or
`changing channels`. It produces search terms and suggested time ranges.

It must not download or insert results automatically. The user selects a local
file or approves a search result. The app then stores the asset hash, provider,
URL, license/attribution text, and selected source range.

Placement rules:

- illustrate a noun, memory, action, or emotional beat;
- avoid decorative clips that contradict the narration;
- prefer 1.0-4.0 s per insert;
- do not replace the presenter's important facial reaction;
- change visual mode at natural phrase boundaries, not mid-word;
- never use copyrighted movie/TV extracts merely because the reference does.

### 6. Audio and SFX

- Normalize the final mix to -14 to -16 LUFS, true peak at or below -1 dBTP.
- Keep dialogue central and use the existing side-chain music ducking.
- Use a low music bed only when its license is recorded and approved.
- Support optional local `whoosh`, `pop`, and `soft hit` SFX at transition or
  keyword events; never place them on every caption.
- Keep SFX roughly 12-20 dB below dialogue and validate the final mix again.

### 7. Optional local intelligence

- **faster-whisper, CPU int8:** word timestamps and transcription draft; local
  model only, no silent download.
- **Existing forced aligner:** preferred when exact reviewed Tanglish text must
  be preserved and the local model is available.
- **Ollama:** optional story/visual beat suggestions using a strict JSON schema;
  no shell commands, asset downloads, or approval authority.
- **MediaPipe or OpenCV:** optional CPU face box tracking for crop/PiP paths.
  Smooth movement and fall back to a manual crop if confidence is poor.

The renderer and all manual editing paths must still work when these optional
components are unavailable.

## Free tools and optional APIs

| Tool/API | Role | Cost/constraint |
|---|---|---|
| FFmpeg/FFprobe | Decode, composite, transitions, audio, encode, QA | Local open-source tool; already bundled |
| ASS/libass | High-quality burned captions | Included through FFmpeg |
| faster-whisper/CTranslate2 | Local word timestamps | Open source; model download/storage is separate and explicit |
| OpenCV or MediaPipe | Optional CPU face tracking and crop paths | Open source; add only after dependency approval |
| Ollama | Optional local beat/B-roll suggestions | Local; existing installation; model must already be installed |
| Remotion | Complex title/outro/graphic templates | Free only when the user/team qualifies under its current license; keep FFmpeg fallbacks |
| Pexels API | Optional stock photo/video search | Free API key with current default limits; every selected asset still needs provenance/approval |

Do not treat Freesound API as a generally free commercial SFX API: its official
terms state that free API use is for non-commercial purposes. For a monetized
channel, use user-supplied cleared SFX or obtain separate permission.

## Build order and acceptance gates

### Milestone 1 - caption-sync proof on `test001`

- Generate word-aligned captions from `subtitles.srt`.
- Render a captions-only 1080 x 1920 preview.
- Produce an alignment report with every uncertain word/time.
- Acceptance: creator checks the complete 70 s video; no cue starts before its
  phrase, lingers into the next phrase, or exceeds two lines.

### Milestone 2 - visual template proof

- Add `presenter_full`, `split_top`, `broll_full`, and `circle_pip`.
- Use three to five explicitly approved local placeholder assets.
- Acceptance: no stretched footage, face clipping, caption collision, or black
  gap; all asset licenses and hashes appear in the report.

### Milestone 3 - motion and transitions

- Add keyword animation, push-in, light sweep, blur, and smear/flash templates.
- Acceptance: most edits remain cuts; no transition hides speech or creates an
  unintended full-white/black frame; reduced-flash mode passes.

### Milestone 4 - audio finish

- Mix approved music/SFX with ducking and two-pass loudness normalization.
- Acceptance: -14 to -16 LUFS, <= -1 dBTP, 48 kHz stereo, no clipping, and
  measured A/V duration difference within the existing frame-aware tolerance.

### Milestone 5 - reference-style first pass

- Create a complete reviewed timeline for `test001`.
- Target a median visual change interval near 2-3 s only where the narration
  supports it; do not force a cut quota.
- Render a low-resolution draft, revise the timeline, then render 1080 x 1920.
- Acceptance: creator approves captions, B-roll relevance, transitions, audio,
  and final file. Only then may the publishing package be created.

### Milestone 6 - automation

- Generate caption grouping, visual beats, B-roll search terms, layout choices,
  and transition suggestions automatically.
- Keep every external asset and low-confidence transcript item pending until a
  human approves it.
- Add synthetic tests for every template and one end-to-end render test.

## Definition of done for this style

- `test001.mp4` is never modified and its hash still matches.
- Captions visually match the spoken words for the full video.
- The render contains purposeful B-roll, not arbitrary stock footage.
- Strong transitions occur only at selected emphasis beats.
- No copyright-unclear reference clips are copied.
- The output is 1080 x 1920, 30 fps, H.264/AAC 48 kHz and technically validated.
- Final audio meets the selected loudness/peak limits.
- Every asset, timeline revision, preview, report and approval is hash-bound.
- The application works locally without requiring a paid cloud AI API.

