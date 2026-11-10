"""Extracts the exact frames each beat shows, so Remotion composites images and needs no video decoder.

Run from the AI Video Editor folder, after build_story.py:  python engine/extract_frames.py ramayana
The images are temporary; delete remotion/public/seq after the render. Clips and footage are only read.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "engine")
from analyze_references import FF

REMOTION = "remotion"
STORY = sys.argv[1]
BASE = f"4_previews/{STORY}-v1/{STORY}-v1-base.mp4"
SEQ = f"{REMOTION}/public/seq"
FPS = 30
LUMA = (0.2126, 0.7152, 0.0722)


def picture_chain(grade):
    """Filters that turn video into the JPEG the browser reads: full range BT.601, whatever the source."""
    if not grade:
        return "format=yuvj420p"
    s, gain = grade["saturation"], grade["brightness"]
    tint = (1 + grade["warmth"] / 2, 1, 1 - grade["warmth"] / 2)
    # brightness, warmth and saturation in one matrix, then contrast around mid grey, all in RGB
    names = "rgb"
    cells = [f"{names[i]}{names[j]}={gain * tint[i] * (LUMA[j] * (1 - s) + (s if i == j else 0)):.5f}" for i in range(3) for j in range(3)]
    curve = f"clip((val-128)*{grade['contrast']}+128,0,255)"
    return ("scale=in_color_matrix=bt709:in_range=tv,format=gbrp,colorchannelmixer=" + ":".join(cells)
            + f",lutrgb=r='{curve}':g='{curve}':b='{curve}',scale=out_color_matrix=bt470bg:out_range=pc,format=yuvj420p")


def main():
    with open(f"{REMOTION}/src/{STORY}.json", encoding="utf-8") as f:
        data = json.load(f)
    shutil.rmtree(SEQ, ignore_errors=True)
    os.makedirs(f"{SEQ}/base")
    chain = picture_chain(data.get("grade"))

    # speaker frames: only where the speaker can be seen, named by their frame number in the Reel
    ranges = [(round(b["start"] * FPS), round(b["end"] * FPS)) for b in data["beats"] if b["type"] in ("head", "split")]
    pick = "+".join(f"between(n,{a},{z - 1})" for a, z in ranges)
    subprocess.run([FF, "-v", "error", "-y", "-i", BASE, "-an", "-vf", f"select='{pick}',{chain}", "-fps_mode", "passthrough",
                    "-frame_pts", "1", "-q:v", "2", f"{SEQ}/base/%05d.jpg"], check=True)
    want = sum(z - a for a, z in ranges)
    have = len(glob.glob(f"{SEQ}/base/*.jpg"))
    if want != have:
        raise SystemExit(f"speaker frames: wanted {want}, got {have}")

    # picture frames: each beat's own stretch of its clip, resampled to 30 fps, numbered from 0
    def pictures(name, clip, start, count):
        folder = f"{SEQ}/{name}"
        os.makedirs(folder)
        subprocess.run([FF, "-v", "error", "-y", "-ss", str(start), "-i", f"{REMOTION}/public/broll/{clip}.mp4", "-an",
                        "-vf", f"fps={FPS},{chain}", "-frames:v", str(count), "-start_number", "0", "-q:v", "2", f"{folder}/%05d.jpg"], check=True)
        got = len(glob.glob(f"{folder}/*.jpg"))
        if got != count:
            raise SystemExit(f"{name} ({clip}): wanted {count} frames, got {got}")

    for i, b in enumerate(data["beats"]):
        if b.get("under"):
            clip, at = b["under"].rsplit("-", 1)
            still = f"{REMOTION}/public/stills/{b['under']}.jpg"
            if not os.path.exists(still):
                subprocess.run([FF, "-v", "error", "-y", "-ss", at, "-i", f"{REMOTION}/public/broll/{clip}.mp4", "-frames:v", "1",
                                "-vf", "scale=480:-2,format=yuvj420p", "-q:v", "3", still], check=True)
        if not b.get("clip"):
            continue
        first = round(b["end"] * FPS) - round(b["start"] * FPS)
        # clipFrames holds the last good frame, when the clip cuts to something else too soon
        first = min(first, b.get("clipFrames", first))
        pictures(f"b{i}", b["clip"], b["clipStart"], first)
        b["seq"], b["frames"] = f"b{i}", first
        if b.get("clip2"):
            second = round(b["end"] * FPS) - round(b["secondAt"] * FPS)
            pictures(f"b{i}x", b["clip2"], b["clip2Start"], second)
            b["seq2"], b["frames2"] = f"b{i}x", second

    with open(f"{REMOTION}/src/{STORY}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    size = sum(os.path.getsize(x) for x in glob.glob(f"{SEQ}/**/*.jpg", recursive=True))
    print(f"ok: {have} speaker frames and {sum(b.get('frames', 0) for b in data['beats'])} picture frames, {size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
