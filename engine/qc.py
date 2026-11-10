"""Post-render checks. Reports only what a program can verify; never claims a watch or listen."""
import json
import re

import captions

from common import run


def check_ass(ass_path, cues, style, font, top=220, bottom=1440, right=960):
    """Checks the .ass as it will be burned, effects included. Needs no video."""
    with open(ass_path, encoding="utf-8") as f:
        rows = f.read().splitlines()
    tag = re.compile(r"\{[^}]*\}")
    results = []

    def add(name, ok, detail):
        results.append({"check": name, "pass": bool(ok), "detail": str(detail)})

    caps = [r.split(",,0,0,0,,", 1)[1] for r in rows if r.startswith("Dialogue:") and r.split(",")[3] == "Cap"]
    words = " ".join(tag.sub("", c).replace("\\N", " ") for c in caps)
    add("Cap text matches SRT word for word", words == " ".join(c["text"] for c in cues), f"{len(caps)} captions")
    over = [c for c in caps if sum(len(captions.fit_lines(x, style, font))
                                   for x in tag.sub("", c).split("\\N")) > 2]
    add("captions at most 2 lines", not over, over or "all fit")

    for r in rows:
        if not r.startswith("Dialogue:") or "\\p1}" not in r or r.split(",")[3] == "Shape":
            continue
        xs_ys = [int(n) for n in re.findall(r"-?\d+", r.split("\\p1}", 1)[1].split("{", 1)[0])]
        xs, ys = xs_ys[0::2], xs_ys[1::2]
        add(f"{r.split(',')[3]} block inside safe zone", min(ys) >= top and max(ys) <= bottom and max(xs) <= right,
            f"x {min(xs)}-{max(xs)}, y {min(ys)}-{max(ys)}")
    return results


def check(video, expected_seconds, fps, phrases, cues, sheet_path, report_path):
    probe = json.loads(run("ffprobe", "-v", "error", "-show_entries",
                           "format=duration,size:stream=codec_name,codec_type,width,height,r_frame_rate,duration,pix_fmt",
                           "-of", "json", video).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    a = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    results = []

    def add(name, ok, detail):
        results.append({"check": name, "pass": bool(ok), "detail": str(detail)})

    add("size 1080x1920", (v["width"], v["height"]) == (1080, 1920), f"{v['width']}x{v['height']}")
    add("h264 + aac", v["codec_name"] == "h264" and a["codec_name"] == "aac", f"{v['codec_name']}/{a['codec_name']}")
    add("frame rate", v["r_frame_rate"] == f"{fps}/1", v["r_frame_rate"])
    add("duration matches timeline", abs(float(v["duration"]) - expected_seconds) <= 2 / fps,
        f"video {v['duration']}s, timeline {expected_seconds:.3f}s")
    add("A/V drift under 2 frames", abs(float(v["duration"]) - float(a["duration"])) <= 2 / fps,
        f"video {v['duration']}s, audio {a['duration']}s")

    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", video, "-af", "ebur128=peak=true:framelog=quiet",
              "-vf", "blackdetect=d=0.1,freezedetect=n=0.001:d=3", "-f", "null", "-").stderr
    lufs = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])
    peak = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", err)[-1])
    add("loudness -14 LUFS +/-1", abs(lufs + 14) <= 1, lufs)
    add("true peak at or under -0.9 dB", peak <= -0.9, peak)
    add("no black frames", "black_start" not in err, err.count("black_start"))
    add("no frozen picture over 3 s", "freeze_start" not in err, err.count("freeze_start"))

    joined = " ".join(p["text"] for p in phrases)
    add("captions identical to SRT", joined == " ".join(c["text"] for c in cues), f"{len(phrases)} phrases")
    overlaps = [p["text"] for p, q in zip(phrases, phrases[1:]) if p["out_end"] > q["out_start"] + 0.001]
    add("no overlapping captions", not overlaps, overlaps)

    run("ffmpeg", "-v", "error", "-y", "-i", video, "-vf", "fps=1/4,scale=216:-1,tile=8x4", "-frames:v", "1", sheet_path)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"video": video, "probe": probe, "lufs": lufs, "true_peak": peak, "checks": results,
                   "not_verified": ["voice quality by ear", "cut feel in motion", "emotional pacing",
                                    "caption sync by ear", "frames between samples"]}, f, indent=1)
    return results
