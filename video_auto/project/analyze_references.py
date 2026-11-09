"""Measures reference videos with FFmpeg and writes analysis files. Originals are only read.

Run from the AI Video Editor folder:  python video_auto/project/analyze_references.py
Writes: video_auto/analysis/reference_measurements.json, reference_contact_sheet.jpg
Frames are small and handled one at a time, to stay light on memory.
"""
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile

from PIL import Image, ImageDraw

ROOT = "video_auto"
ANALYSIS = f"{ROOT}/analysis"
FRAMES_PER_ROW, THUMB_W, THUMB_H = 12, 150, 267


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    hits = glob.glob(os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*\*\bin\{name}.exe"))
    if not hits:
        raise SystemExit(f"{name} not found")
    return hits[0]


FF, FP = tool("ffmpeg"), tool("ffprobe")


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def analyse(path):
    info = json.loads(run(FP, "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration,size",
                          "-of", "json", path).stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    duration = float(info["format"]["duration"])
    num, den = video["r_frame_rate"].split("/")

    scene = run(FF, "-hide_banner", "-i", path, "-an", "-vf", "scale=320:-2,select='gt(scene,0.3)',showinfo", "-f", "null", "-").stderr
    cuts = [round(float(x), 3) for x in re.findall(r"pts_time:([\d.]+)", scene)]
    shots = [round(b - a, 3) for a, b in zip([0.0] + cuts, cuts + [duration])]

    stats = run(FF, "-hide_banner", "-i", path, "-an", "-vf",
                "fps=1,scale=180:-2,signalstats,metadata=print:file=-", "-f", "null", "-").stdout
    picture = {}
    for key in ("YAVG", "SATAVG", "UAVG", "VAVG"):
        values = [float(x) for x in re.findall(rf"lavfi\.signalstats\.{key}=([\d.]+)", stats)]
        picture[key] = round(statistics.mean(values), 1) if values else None

    black = run(FF, "-hide_banner", "-i", path, "-an", "-vf", "scale=180:-2,blackdetect=d=0.1", "-f", "null", "-").stderr
    sound = {}
    if audio:
        err = run(FF, "-hide_banner", "-nostats", "-i", path, "-vn", "-af",
                  "silencedetect=n=-35dB:d=0.25,ebur128=peak=true:framelog=quiet", "-f", "null", "-").stderr
        gaps = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", err)]
        sound = {"integrated_lufs": float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1]),
                 "lra_lu": float(re.findall(r"LRA:\s+(-?[\d.]+) LU", err)[-1]),
                 "true_peak_db": float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", err)[-1]),
                 "silences_over_0_25s": len(gaps), "longest_silence_s": max(gaps, default=0)}

    return {"file": os.path.basename(path), "width": video["width"], "height": video["height"],
            "fps": round(int(num) / int(den), 3), "duration_s": round(duration, 3),
            "aspect_ratio": round(video["width"] / video["height"], 4), "has_audio": bool(audio),
            "scene_changes": len(cuts), "scene_change_times_s": cuts,
            "shot_length_s": {"mean": round(statistics.mean(shots), 2), "median": round(statistics.median(shots), 2),
                              "min": min(shots), "max": max(shots)},
            "black_segments": black.count("black_start"), "picture_means_8bit": picture, "audio": sound}


def contact_sheet(paths, durations, out):
    sheet = Image.new("RGB", (FRAMES_PER_ROW * (THUMB_W + 4), len(paths) * (THUMB_H + 24)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    with tempfile.TemporaryDirectory() as temp:
        frame = os.path.join(temp, "f.jpg")
        for row, (path, duration) in enumerate(zip(paths, durations)):
            for col in range(FRAMES_PER_ROW):
                at = duration * (col + 0.5) / FRAMES_PER_ROW
                run(FF, "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={THUMB_W}:{THUMB_H}", frame)
                x, y = col * (THUMB_W + 4), row * (THUMB_H + 24)
                with Image.open(frame) as image:
                    sheet.paste(image, (x, y + 20))
                draw.text((x + 2, y + 4), f"{os.path.basename(path)} {at:.1f}s", fill=(255, 210, 60))
    sheet.save(out, quality=86)


def main():
    os.makedirs(ANALYSIS, exist_ok=True)
    paths = sorted(p for p in glob.glob(f"{ROOT}/references/*") + glob.glob("references/*") if p.lower().endswith((".mp4", ".mov", ".mkv", ".webm")))
    seen, unique = set(), []
    for p in paths:
        if os.path.basename(p) not in seen:
            seen.add(os.path.basename(p))
            unique.append(p)
    results = [analyse(p) for p in unique]
    with open(f"{ANALYSIS}/reference_measurements.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    contact_sheet(unique, [r["duration_s"] for r in results], f"{ANALYSIS}/reference_contact_sheet.jpg")
    for r in results:
        print(json.dumps({k: v for k, v in r.items() if k != "scene_change_times_s"}))


if __name__ == "__main__":
    main()
