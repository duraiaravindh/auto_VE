"""Audio envelope, silence detection and the keep/trim decision for every pause."""
import re

import numpy as np

from common import run, snap, tool
import subprocess

RATE = 16000
HOP = 160  # 10 ms


def envelope(source):
    """RMS level in dB per 10 ms."""
    raw = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", source, "-vn", "-ac", "1", "-ar", str(RATE),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, dtype=np.float32)
    n = len(pcm) // HOP
    rms = np.sqrt(np.mean(pcm[:n * HOP].reshape(n, HOP) ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def detect_silences(source, noise_db, min_seconds):
    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", source, "-af",
              f"silencedetect=noise={noise_db}dB:d={min_seconds}", "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return list(zip(starts, ends))


def decide(silences, policy, duration):
    """One record per pause. Protected story beats and short pauses are never touched."""
    out = []
    for s, e in silences:
        length = e - s
        record = {"start": round(s, 3), "end": round(e, 3), "length": round(length, 3)}
        protected = next((p["why"] for p in policy["protected"] if p["from"] <= s <= p["to"]), None)
        if e >= duration - 0.02:
            record.update(action="tail", why="after the last word")
        elif protected:
            record.update(action="keep", why=f"protected: {protected}")
        elif length < policy["min_trim_length"]:
            record.update(action="keep", why=f"natural, under {policy['min_trim_length']}s")
        else:
            record.update(action="trim", why=f"tightened, {policy['keep_after_trim']}s kept")
        out.append(record)
    return out


def insert_pauses(ranges, inserts, fps):
    """Adds a pause inside a line. Each insert plays a quiet stretch of the take ("from"-"to") at "at",
    so the pause keeps real room tone and real movement."""
    for item in inserts:
        at = snap(item["at"], fps)
        i = next(k for k, (a, b) in enumerate(ranges) if a < at < b)
        a, b = ranges[i]
        ranges[i:i + 1] = [(a, at), (snap(item["from"], fps), snap(item["to"], fps)), (at, b)]
    return ranges


def kept_ranges(pauses, policy, duration, fps, end_hold):
    """Source ranges to keep, on frame boundaries so audio, video and captions cannot drift."""
    half = policy["keep_after_trim"] / 2
    ranges, pos = [], 0.0
    tail = next((p["start"] for p in pauses if p["action"] == "tail"), duration)
    for p in pauses:
        if p["action"] == "trim":
            ranges.append((pos, snap(p["start"] + half, fps)))
            pos = snap(p["end"] - half, fps)
    ranges.append((pos, snap(min(tail + end_hold, duration), fps)))
    return ranges
