"""Face track from sampled frames (Windows built-in detector), cached per edit folder."""
import glob
import json
import os
import shutil
import subprocess

from common import run

SAMPLE_FPS = 2


def track(source, cache_dir):
    """[{t, box:[x,y,w,h]|None}] at SAMPLE_FPS, in source pixels."""
    path = os.path.join(cache_dir, "faces.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    frames = os.path.join(cache_dir, "face_frames")
    os.makedirs(frames, exist_ok=True)
    run("ffmpeg", "-v", "error", "-y", "-i", source, "-vf", f"fps={SAMPLE_FPS}", os.path.join(frames, "f%05d.png"))
    script = os.path.join(os.path.dirname(__file__), "face_detect.ps1")
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                          os.path.abspath(frames)], capture_output=True, text=True, check=True).stdout
    result = []
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        index = int(row["file"][1:6]) - 1
        # largest box is the speaker; posters behind can produce small false faces
        box = max(row["faces"], key=lambda b: b[2] * b[3]) if row["faces"] else None
        result.append({"t": (index + 0.5) / SAMPLE_FPS, "box": box})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    shutil.rmtree(frames)
    return result
