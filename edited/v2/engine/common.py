"""Shared helpers: tool lookup, process running, SRT parsing, frame snapping."""
import glob
import os
import re
import shutil
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    hits = glob.glob(os.path.expanduser(
        f"~/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/*/bin/{name}.exe"))
    if hits:
        return hits[0]
    raise SystemExit(f"{name} not found; install FFmpeg")


def run(*args):
    args = [tool(a) if i == 0 and a in ("ffmpeg", "ffprobe") else a for i, a in enumerate(args)]
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise SystemExit(f"{args[0]} failed:\n{done.stderr[-1500:]}")
    return done


def parse_srt(path):
    """Cues as dicts. text keeps wording exactly; a line break becomes one space."""
    with open(path, encoding="utf-8") as f:
        blocks = re.split(r"\n\s*\n", f.read().strip())
    cues = []
    for block in blocks:
        lines = block.strip().splitlines()
        h, m, s, ms, h2, m2, s2, ms2 = map(int, re.findall(r"\d+", lines[1]))
        cues.append({
            "id": int(lines[0]),
            "start": h * 3600 + m * 60 + s + ms / 1000,
            "end": h2 * 3600 + m2 * 60 + s2 + ms2 / 1000,
            "text": " ".join(lines[2:]),
        })
    return cues


def snap(t, fps):
    return round(t * fps) / fps


def clock(t):
    return f"{int(t // 60):02d}:{t % 60:06.3f}"
