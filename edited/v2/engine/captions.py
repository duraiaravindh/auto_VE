"""Phrase timing from the SRT + audio dips, exact-text validation, ASS and SRT output.

No aligner: the SRT gives each line's window, and phrase boundaries inside it are placed
at the nearest real dip in the voice. Every boundary records how it was placed so the
weak ones can be reviewed by a human.
"""
import re

import numpy as np
from PIL import ImageFont

from common import clock


def plain(phrase):
    return phrase.replace("*", "")


def validate_text(cues, phrases_by_cue):
    """Stops the build if any phrase list differs from the SRT by a single character."""
    bad = []
    for c in cues:
        joined = " ".join(plain(p) for p in phrases_by_cue[str(c["id"])])
        if joined != c["text"]:
            bad.append(f"cue {c['id']}:\n  srt: {c['text']}\n  got: {joined}")
    if bad:
        raise SystemExit("Caption text differs from the SRT:\n" + "\n".join(bad))


def syllables(text):
    # digits are said aloud as words ("200%" ~ "irunooru percent"), so they weigh more
    return len(re.findall(r"[aeiouAEIOU]+", text)) + 2 * len(re.findall(r"\d", text)) + (2 if "%" in text else 0)


def _voice_gaps(env, lo, hi, level, drop=12, min_frames=6):
    """Real gaps (60 ms+ well below the line's level) between two times, as (start, end)."""
    if hi <= lo:
        return []
    quiet = env[int(lo * 100):int(hi * 100)] < level - drop
    gaps, run = [], None
    for i, q in enumerate(list(quiet) + [False]):
        if q and run is None:
            run = i
        elif not q and run is not None:
            if i - run >= min_frames:
                gaps.append((lo + run / 100, lo + i / 100))
            run = None
    return gaps


def time_phrases(cues, phrases_by_cue, env, silences):
    """The SRT window is the anchor (its gaps are often breaths, which stay uncaptioned).
    An edge only moves when it falls inside detected silence. Inside a line, a boundary
    snaps to a real gap near the syllable estimate; otherwise it stays an estimate and
    is marked for review."""
    result = []
    for c in cues:
        phrases = phrases_by_cue[str(c["id"])]
        start, end = c["start"], c["end"]
        for s, e in silences:
            if s <= start < e:
                start = e
            if s < end <= e:
                end = s
        level = np.percentile(env[int(start * 100):max(int(end * 100), int(start * 100) + 1)], 75)
        weights = [syllables(plain(p)) for p in phrases]
        bounds, methods, previous = [], [], start
        for i in range(len(phrases) - 1):
            estimate = start + (end - start) * sum(weights[:i + 1]) / sum(weights)
            near = _voice_gaps(env, max(previous + 0.3, estimate - 0.35), min(end - 0.25, estimate + 0.35), level)
            if near:
                gap = min(near, key=lambda g: abs((g[0] + g[1]) / 2 - estimate))
                bounds.append(gap)
                methods.append("gap")
                previous = gap[1]
            else:
                bounds.append((estimate, estimate))
                methods.append("estimate")
                previous = estimate
        starts = [start] + [g[1] for g in bounds]
        ends = [g[0] for g in bounds] + [end]
        for i, p in enumerate(phrases):
            text = plain(p)
            duration = ends[i] - starts[i]
            result.append({
                "cue": c["id"], "text": text, "marked": p,
                "start": round(starts[i], 2), "end": round(ends[i], 2),
                "boundary": methods[i] if i < len(methods) else "srt",
                "cps": round(len(text) / max(duration, 0.01), 1),
            })
    return result


def line_width(text, size, font_path):
    """Rendered width of one line at an ASS font size (libass: size = ascent+descent)."""
    probe = ImageFont.truetype(font_path, 100)
    ascent, descent = probe.getmetrics()
    return ImageFont.truetype(font_path, round(size * 100 / (ascent + descent))).getlength(text)


def fit_lines(text, style, font_path):
    """Greedy wrap measured with the real font, using libass sizing (size = ascent+descent)."""
    probe = ImageFont.truetype(font_path, 100)
    ascent, descent = probe.getmetrics()
    px = style["size"] * 100 / (ascent + descent)
    font = ImageFont.truetype(font_path, round(px))
    room = 1080 - style["margin_l"] - style["margin_r"] - 2 * style["outline"]
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= room:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def to_output_time(t, shots):
    """Source time -> output time. Inside a removed pause it lands on the next shot."""
    out = 0.0
    for s in shots:
        if t < s["in"]:
            return out
        if t <= s["out"]:
            return out + t - s["in"]
        out += s["out"] - s["in"]
    return out


def check_lines(phrases, limit=2):
    too_long = [p["text"] for p in phrases if len(p["lines"]) > limit]
    if too_long:
        raise SystemExit(f"Captions over {limit} lines; split them in edit.json:\n" + "\n".join(too_long))


def place_on_output(phrases, shots, style):
    """Output on/off times. A caption stays up across a small gap to avoid flicker,
    but clears during a longer pause so the moment has a clean frame."""
    for p in phrases:
        p["out_start"] = to_output_time(p["start"], shots)
        p["out_end"] = to_output_time(p["end"], shots)
    for a, b in zip(phrases, phrases[1:]):
        gap = b["out_start"] - a["out_end"]
        a["out_end"] += gap if gap <= style["bridge_gap"] else style["hold_after"]
    phrases[-1]["out_end"] = to_output_time(shots[-1]["out"], shots)
    for p in phrases:
        p["out_start"], p["out_end"] = round(p["out_start"], 2), round(p["out_end"], 2)
    return phrases


def _ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(phrases, style, path):
    s = style
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{s['font']},{s['size']},{s['text']},{s['text']},{s['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,"
        f"{s['outline']},{s['shadow']},8,{s['margin_l']},{s['margin_r']},{s['top']},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    rows = []
    for p in phrases:
        parts = p["marked"].split("*")
        text = "".join(f"{{\\c{s['highlight']}}}{x}{{\\c{s['text']}}}" if i % 2 else x for i, x in enumerate(parts))
        rows.append(f"Dialogue: 0,{_ass_time(p['out_start'])},{_ass_time(p['out_end'])},Cap,,0,0,0,,{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(rows) + "\n")


def write_srt(phrases, path):
    def stamp(t):
        return clock(t).replace(".", ",")
    with open(path, "w", encoding="utf-8") as f:
        for i, p in enumerate(phrases, 1):
            f.write(f"{i}\n00:{stamp(p['out_start'])} --> 00:{stamp(p['out_end'])}\n{p['text']}\n\n")
