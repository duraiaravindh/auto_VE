#!/usr/bin/env bash
# Renders a Remotion composition in chunks, so low memory can only cost one chunk.
# Finished chunks are kept, so a re-run resumes. Stops on the first failure. Sound comes from the base.
# CRF can be set in the environment (default 20).
# Usage, from the Remotion folder: render_chunks.sh <bundle> <composition> <frames> <review folder> <base.mp4>
set -u
BUNDLE=$1; COMP=$2; FRAMES=$3; OUT=$4; BASE=$5
STEP=150
FF=$(ls /c/Users/durai/AppData/Local/Microsoft/WinGet/Packages/*/*/bin/ffmpeg.exe | head -1)
PARTS="$OUT/parts"; mkdir -p "$PARTS"; : > "$PARTS/list.txt"
for ((a = 0; a < FRAMES; a += STEP)); do
  b=$((a + STEP - 1)); [ "$b" -ge "$FRAMES" ] && b=$((FRAMES - 1))
  part="$PARTS/p$(printf %05d "$a").mp4"
  echo "file 'p$(printf %05d "$a").mp4'" >> "$PARTS/list.txt"
  [ -s "$part" ] && { echo "have $a-$b"; continue; }
  ok=0
  for try in 1 2 3 4 5; do
    if timeout 400 npx remotion render "$BUNDLE" "$COMP" "$part.tmp.mp4" --frames="$a-$b" --crf="${CRF:-20}" --pixel-format=yuv420p --concurrency=1         --timeout=120000 --muted --log=error > "$PARTS/log-$a.txt" 2>&1; then ok=1; break; fi
    echo "retry $try for $a-$b"; rm -f "$part.tmp.mp4"
  done
  [ "$ok" = 1 ] || { echo "FAILED at frames $a-$b; see $PARTS/log-$a.txt"; exit 1; }
  mv "$part.tmp.mp4" "$part"; echo "done $a-$b"
done
"$FF" -v error -y -f concat -safe 0 -i "$PARTS/list.txt" -c copy "$OUT/video-only.mp4" || exit 1
"$FF" -v error -y -i "$OUT/video-only.mp4" -i "$BASE" -map 0:v -map 1:a -c copy -shortest -movflags +faststart "$OUT/preview.mp4" || exit 1
echo "preview written"
