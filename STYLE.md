# STYLE.md: Namma Kadhai Reels

Status: approved standard, adapted to each story.
Standard example: `exports/deepavali-v1.mp4`. It replaces the older v12 look.
References studied: reel1, reel2, reel4, reel5. Use them for style only; never re-upload them.

## How this style works
The **core** keeps every Reel recognisably Namma Kadhai. The **story layer** changes with each story, so the Reels don't all feel the same.

| Stays the same (core) | Changes with the story |
|---|---|
| Subtitle font and yellow Anton for "NAMMA KADHAI" | Headline fonts, colours and subtitle tint, chosen by mood |
| Clear, readable text | Which words become headlines |
| Your words only, never rewritten | Pictures and layouts for each line |
| Voice first, -14 LUFS | Accent colour, if the story calls for it |
| A strong first second | Drawn effects, such as sparks for Deepavali |
| One quiet moment on your face | Pace, and where the quiet moment falls |
| Comment question and sign-off | Music mood and sound effects |

## For each new story
1. Read the story and find its hook, its turn and its payoff.
2. Pick a mood from the guide below, or suggest a new one if none fits.
3. Show a short plan: mood, picture for each line, headlines, quiet moment and anything new.
4. Make a preview. Render a final only after your approval.
5. If a new idea gets approved, add it to the log at the end of this file.

## Story guide
These are starting points, not fixed rules. Only Festival and memory has been tested in a finished Reel.

| Story type | Pace | Visual feel | Quiet moment | Sound |
|---|---|---|---|---|
| Festival and memory | Fast, about 2 s per picture, then slowing | Warm B-roll, collage recap, drawn sparks | When joy turns to loss | Warm music with hits on big words |
| Friendship and relationships | Medium, about 3 s | More of your face, framed photos, handwritten words | The line that hits hardest | Soft music with few effects |
| Struggle and comeback | Slow start, faster at the comeback | Colour drained at first, returning at the turn | Just before the comeback | Sparse music that builds |
| Lesson or advice | Crisp, about 2-3 s | Grid paper, cream cards, numbered points | Before the main lesson | Light music with clean whooshes |
| Funny story | Quick, timed to the punchlines | Big reaction words and punch-in zooms | Short pause before the punchline | Playful effects, used on the jokes |

## Format
- 1080x1920 vertical, 30 fps, H.264 and AAC, yuv420p with BT.709 colour
- Loudness about -14 LUFS, true peak at or under -0.9 dB
- Keep each Reel only as long as its story needs

## Words on screen
- Use your SRT word for word. Never translate, correct or rewrite the Tanglish.
- If there is no SRT, transcribe a draft and list any uncertain lines for you to check.
- Headlines and handwritten words must come from what you actually say. The only exceptions are text you specifically approve.

## Subtitles
- Crimson Text, size 56, at most 2 lines
- Gold `#F7D94C` with a tight dark shadow over video
- Dark ink `#2A2112` on grid paper and cream cards
- Inside Instagram's safe area: ends at about y 1440 and stays left of x 960
- Sits under the frame on grid paper, and on a dark strip at the seam in split screen
- Short phrases, timed to real voice gaps

## Headlines
- Font, colour and entrance follow the mood of each moment (see below)
- Starts at about y 214, with margins kept clear of the frame edges
- Clear of your head, so shots with a headline stay near 100% zoom
- Pick the words that carry the feeling, not every line
- Check contrast at phone size; add a darker edge if the picture behind is bright

## Mood type
| Mood | Headline font | Colour | Entrance | Subtitle tint |
|---|---|---|---|---|
| Divine, sacred | Cinzel | Cream-to-gold gradient, dark edge | Slow rise | Soft gold |
| Old memories | Alfa Slab One | Cream with a retro orange shadow | Pop | Warm cream |
| Silence, stillness | Cormorant, widely spaced | Ivory | Slow fade | Ivory |
| Modern, big, technology | Montserrat ExtraBold | White with a blue glow | Pop | Cool white |
| Shock, peak moment | Anton | Yellow-to-orange fire gradient | Pop | Warm yellow |
| Emotional question | Playfair Display italic | Deep maroon on cream | Pop | Dark ink |
| Your brand | Anton | Yellow `#F5C533` | Pop | Gold |

Sacramento and Kaushan Script suit short handwritten lines above a headline. Add a new mood when a story needs one.

## Layouts
Mix these so the picture keeps changing:
- **Your face:** full frame, with a quick settle on the cut
- **Full-screen B-roll:** slow push in or out, darkened behind the text
- **Grid paper:** white-framed picture that lands with a slight tilt, with another photo behind it
- **Split screen:** picture above, your face below
- **Collage:** tilted wall of story moments for a recap
- **Cream card:** stacked capitals for the comment question or a big statement

## Motion and transitions
- Mostly hard cuts, with a warm light sweep at big story changes
- Drawn effects only when they suit the story
- Zoom never above 120%

## Quiet moment
- Choose one for each story: your face, subtitles only, no headlines, B-roll or sound effects
- Keep its natural pauses
- Tighten other pauses over about 0.35 s

## B-roll
- Your face is the hero. Use no B-roll faces that resemble a real person, especially adults in close-up
- Match each clip to the line being spoken
- Keep family visuals to 2-3 shots, and reuse one consistent family shot rather than changing faces
- Prefer faceless visuals: backs of heads, objects and places
- Remove the clip's own audio; your voice continues over every clip
- Use only clips you own or may use, and check whether AI-generated clips need a label

## Sound
- Voice first, with linear loudness correction
- Music about 15 dB below the voice, dropping further in the quiet moment
- Whooshes on big layout changes, hits on key words, faint air on smaller changes
- Use generated music or a track you may use

## Look
- Check each raw clip's colour first. HDR (HLG) footage must be converted to BT.709 before any grade, or skin and posters wash out
- Contrast 1.12, saturation 1.08, gamma 1.04, warmth 0.04, with a centred vignette and no grain
- Poster wall darkened up to 42%
- Flip the footage if it was recorded mirrored

## Output
- Show previews before a full render
- Run full QC, then save the final to `exports/`
- Never overwrite raw footage or earlier exports

## Ideas not used yet
Seen in the references. Use your own images and B-roll, and suggest these when a story suits them.

- Big number cards for list-style stories (reel2, reel4)
- Highlight line through a caption word (reel4)
- Phone-screen mockup for chats and messages (reel1)
- Rounded video box with a label (reel2, reel4)
- Self-drawing lines or charts (reel2, reel5)
- Word-by-word captions (reel2; needs word-level timing)

## Style log
Approved additions from each story.

| Reel | What it added |
|---|---|
| Deepavali (`deepavali-v1`) | Became the standard: fireworks cold open, drawn gold sparks, collage recap, colour draining on the emotional turn, split screen with subtitles on the seam |
| Ramayana (`ramayana-v2`) | Old-TV look for the past, fly-through from TV to theatre, one picture splitting into old and new, gold dust, soft original flute and tanpura music, a held pause before a key word, the end card's clean sign-off line |
| Ramayana (`ramayana5-v1`) | Warm AI illustrations only, no film or TV footage, every shot full screen (no split screens), one warm amber tone with off-tone shots warmed to match, text cards of at most 3 words, a soft fading subtitle strip on face shots, an inserted pause shown as a held frame with a slow push, end card with only the sign-off line, cover with face and big text |
