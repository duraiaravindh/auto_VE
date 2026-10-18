#!/usr/bin/env python3
"""Nama Kadhai caption engine.

transcript (JSON or SRT)  ->  readable caption chunks  ->  styled ASS  ->  burned-in MP4

Design rules (from the project brief):
  * warm cream text, muted-gold emphasis, rounded sans font, dark outline
  * natural sentence/clause chunks, max two lines, never word-by-word karaoke
  * separate layout profiles for landscape (16:9) and vertical (9:16)
  * the original recording is never modified

Everything is sized from the OUTPUT resolution (font, outline, margins are
fractions of frame height), so captions look the same on 480p previews and
1080p/4K finals.

Usage:
  python captions.py check  transcript.json --profile landscape
  python captions.py render video.mp4 transcript.json --profile landscape
  python captions.py render video.mp4 transcript.json --profile vertical
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

CREAM = "F5E8D0"
GOLD = "F3C96B"
OUTLINE = "1B1B1B"

SENTENCE_END = (".", "?", "!", "…")
CLAUSE_END = (",", ";", ":", "—", "–")
MAX_WORD_DURATION_SECONDS = 2.5


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    text: str
    start: float
    end: float
    hl: bool = False


@dataclass
class Profile:
    """Layout profile. All sizes are fractions of the output frame height/width."""
    name: str
    out_w: int
    out_h: int
    font: str = "Varela Round"
    bold: bool = False
    font_frac: float = 0.062        # font size / frame height
    outline_frac: float = 0.0085    # outline width / frame height
    shadow_frac: float = 0.0035
    margin_bottom_frac: float = 0.075
    margin_side_frac: float = 0.10
    max_chars: int = 32             # per line
    max_lines: int = 2
    max_chunk_dur: float = 4.5
    min_dur: float = 0.9
    max_gap: float = 0.6            # silence that forces a new chunk
    min_clause_chars: int = 14      # split at a comma only if the chunk is already this long
    lead: float = 0.06              # show caption slightly before the first word
    hold: float = 0.25              # keep it slightly after the last word
    fade_in_ms: int = 90
    fade_out_ms: int = 120

    @property
    def font_px(self) -> int:
        return max(12, round(self.out_h * self.font_frac))


def make_profile(name: str, out_w: int, out_h: int, **overrides) -> Profile:
    if name == "landscape":
        p = Profile("landscape", out_w, out_h)
    elif name == "vertical":
        p = Profile(
            "vertical", out_w, out_h,
            font_frac=0.036,            # 1920 tall -> ~69 px
            outline_frac=0.0048,
            shadow_frac=0.002,
            margin_bottom_frac=0.20,    # stays above Shorts/Reels UI
            margin_side_frac=0.08,
            max_chars=20,
            max_chunk_dur=3.8,
            min_clause_chars=10,
        )
    else:
        raise ValueError(f"unknown profile {name!r}")
    return replace(p, **overrides) if overrides else p


# --------------------------------------------------------------------------- #
# Transcript loading
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> list[tuple[str, bool]]:
    """Split text into (token, highlighted). Highlight with *word* or *two words*."""
    out, on = [], False
    for raw in text.split():
        starts, ends = raw.startswith("*"), raw.endswith("*") and len(raw) > 1
        if starts:
            on = True
        tok = raw.strip("*")
        if tok:
            out.append((tok, on))
        if ends or (starts and raw.endswith("*")):
            on = False
    return out


def _spread(tokens: list[str], start: float, end: float) -> list[tuple[float, float]]:
    """Give each token a time slice, weighted by length (+ pause after punctuation)."""
    weights = []
    for t in tokens:
        w = len(t.strip(".,;:!?…—–")) + 1.0
        if t.endswith(SENTENCE_END):
            w += 4
        elif t.endswith(CLAUSE_END):
            w += 2
        weights.append(w)
    total = sum(weights) or 1.0
    span, cur, out = max(end - start, 0.01), start, []
    for w in weights:
        d = span * w / total
        out.append((cur, cur + d))
        cur += d
    return out


def _segment_words(seg: dict) -> list[Word]:
    text = seg.get("text", "")
    toks = _tokenize(text) if text else []
    words = seg.get("words") or []
    if words and not toks:                      # words only (raw Whisper output)
        return [Word(w["word"].strip(), float(w["start"]), float(w["end"]),
                     bool(w.get("hl", False))) for w in words if w["word"].strip()]
    if words and len(words) == len(toks):       # edited text, same word count: keep timings
        return [Word(t, float(w["start"]), float(w["end"]), hl)
                for (t, hl), w in zip(toks, words)]
    if words:                                   # edited text, different count: respread
        start, end = float(words[0]["start"]), float(words[-1]["end"])
    else:
        start, end = float(seg["start"]), float(seg["end"])
    times = _spread([t for t, _ in toks], start, end)
    return [Word(t, s, e, hl) for (t, hl), (s, e) in zip(toks, times)]


def _parse_srt_time(s: str) -> float:
    h, m, rest = s.strip().split(":")
    sec, ms = rest.replace(",", ".").split(".")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")[:3]) / 1000


def load_transcript(path: Path, extra_highlights: list[str] | None = None) -> list[Word]:
    path = Path(path)
    raw = path.read_text(encoding="utf-8-sig")
    highlights = {h.lower() for h in (extra_highlights or [])}
    words: list[Word] = []
    if path.suffix.lower() == ".srt":
        for block in re.split(r"\n\s*\n", raw.strip()):
            lines = [l for l in block.splitlines() if l.strip()]
            if len(lines) < 2:
                continue
            t_line = next((l for l in lines if "-->" in l), None)
            if not t_line:
                continue
            a, b = t_line.split("-->")
            text = " ".join(lines[lines.index(t_line) + 1:])
            words += _segment_words({"text": text, "start": _parse_srt_time(a),
                                     "end": _parse_srt_time(b)})
    else:
        data = json.loads(raw)
        highlights |= {h.lower() for h in data.get("highlights", [])}
        for seg in data["segments"]:
            words += _segment_words(seg)
    for w in words:
        if w.text.strip(".,;:!?…\"'()").lower() in highlights:
            w.hl = True
    words.sort(key=lambda w: w.start)
    return words


# --------------------------------------------------------------------------- #
# Chunking and line breaking
# --------------------------------------------------------------------------- #
def _len(ws: list[Word]) -> int:
    return len(" ".join(w.text for w in ws))


def break_lines(words: list[Word], max_chars: int, max_lines: int = 2) -> list[list[Word]] | None:
    """Balanced line break. Returns None if the words cannot fit."""
    if _len(words) <= max_chars:
        return [words]
    if max_lines < 2 or len(words) < 2:
        return None
    best = None
    for i in range(1, len(words)):
        a, b = words[:i], words[i:]
        la, lb = _len(a), _len(b)
        if la > max_chars or lb > max_chars:
            continue
        cost = abs(la - lb)
        if words[i - 1].text.endswith(CLAUSE_END):
            cost -= 6                       # prefer breaking at a comma
        if min(la, lb) < 0.3 * max_chars:
            cost += 8                       # avoid stubby lines
        if len(b) == 1:
            cost += 10                      # avoid a lone orphan word
        if best is None or cost < best[0]:
            best = (cost, [a, b])
    return best[1] if best else None


def make_chunks(words: list[Word], prof: Profile) -> list[list[Word]]:
    def fits(ws):
        return break_lines(ws, prof.max_chars, prof.max_lines) is not None

    chunks: list[list[Word]] = []
    cur: list[Word] = []

    def flush():
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = []

    for w in words:
        if cur and (not fits(cur + [w])
                    or (w.end - cur[0].start) > prof.max_chunk_dur
                    or (w.start - cur[-1].end) > prof.max_gap):
            flush()
        cur.append(w)
        if w.text.endswith(SENTENCE_END) or (
                w.text.endswith(CLAUSE_END) and _len(cur) >= prof.min_clause_chars):
            flush()
    flush()

    # merge tiny trailing chunks ("...amazhaa." alone) back into the previous one
    merged: list[list[Word]] = []
    for ch in chunks:
        if (merged and len(ch) <= 2 and _len(ch) < 12
                and not merged[-1][-1].text.endswith(SENTENCE_END)
                and (ch[0].start - merged[-1][-1].end) < 0.5
                and fits(merged[-1] + ch)
                and (ch[-1].end - merged[-1][0].start) <= prof.max_chunk_dur):
            merged[-1] = merged[-1] + ch
        else:
            merged.append(ch)
    return merged


@dataclass
class Event:
    start: float
    end: float
    lines: list[list[Word]]


def build_events(chunks: list[list[Word]], prof: Profile) -> list[Event]:
    # start times first: show slightly before the first word, but never before the
    # previous chunk's last word has finished (no overlaps, even for rapid speech)
    starts = []
    for i, ch in enumerate(chunks):
        s = max(0.0, ch[0].start - prof.lead)
        if i > 0:
            s = max(s, chunks[i - 1][-1].end)
        starts.append(s)
    events = []
    for i, ch in enumerate(chunks):
        e = ch[-1].end + prof.hold
        if e - starts[i] < prof.min_dur:
            e = starts[i] + prof.min_dur
        if i + 1 < len(chunks):
            gap_end = starts[i + 1] - 0.02
            e = min(e, gap_end if gap_end > ch[-1].end else starts[i + 1])
        e = max(e, ch[-1].end)              # never cut off speech
        lines = break_lines(ch, prof.max_chars, prof.max_lines) or [ch]
        events.append(Event(starts[i], e, lines))
    return events


# --------------------------------------------------------------------------- #
# ASS output
# --------------------------------------------------------------------------- #
def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _rgb(hexstr: str) -> tuple[int, int, int]:
    return int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16)


def ass_style_color(hexstr: str, alpha: int = 0) -> str:
    r, g, b = _rgb(hexstr)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_inline_color(hexstr: str) -> str:
    r, g, b = _rgb(hexstr)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _line_text(line: list[Word]) -> str:
    out, hl_on = [], False
    for i, w in enumerate(line):
        if w.hl and not hl_on:
            out.append("{\\c%s}" % ass_inline_color(GOLD))
            hl_on = True
        elif not w.hl and hl_on:
            out.append("{\\c%s}" % ass_inline_color(CREAM))
            hl_on = False
        out.append(_esc(w.text) + (" " if i < len(line) - 1 else ""))
    if hl_on:
        out.append("{\\c%s}" % ass_inline_color(CREAM))
    return "".join(out)


def build_ass(events: list[Event], prof: Profile) -> str:
    side = round(prof.out_w * prof.margin_side_frac)
    bottom = round(prof.out_h * prof.margin_bottom_frac)
    outline = max(1.5, round(prof.out_h * prof.outline_frac, 1))
    shadow = max(0.5, round(prof.out_h * prof.shadow_frac, 1))
    header = f"""[Script Info]
; Nama Kadhai captions - profile: {prof.name}
ScriptType: v4.00+
PlayResX: {prof.out_w}
PlayResY: {prof.out_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{prof.font},{prof.font_px},{ass_style_color(CREAM)},{ass_style_color(CREAM)},{ass_style_color(OUTLINE)},{ass_style_color('000000', 0x70)},{-1 if prof.bold else 0},0,0,0,100,100,0,0,1,{outline},{shadow},2,{side},{side},{bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for ev in events:
        text = "\\N".join(_line_text(l) for l in ev.lines)
        fade = "{\\fad(%d,%d)}" % (prof.fade_in_ms, prof.fade_out_ms)
        rows.append(f"Dialogue: 0,{ass_time(ev.start)},{ass_time(ev.end)},Default,,0,0,0,,{fade}{text}")
    return header + "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- #
# Probing / rendering
# --------------------------------------------------------------------------- #
def probe(video: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return {"w": d["streams"][0]["width"], "h": d["streams"][0]["height"],
            "duration": float(d["format"]["duration"])}


def output_size(profile: str, src_w: int, src_h: int, scale_to: int | None) -> tuple[int, int]:
    if profile == "vertical":
        return 1080, 1920
    if scale_to and scale_to != src_h:
        w = round(src_w * scale_to / src_h / 2) * 2
        return w, scale_to
    return src_w, src_h


def video_filter(profile: str, src_w: int, src_h: int, out_w: int, out_h: int, crop_x: float) -> str:
    parts = []
    if profile == "vertical":
        # centre-crop (or crop_x-shifted) 9:16 window from a landscape source
        parts.append(f"crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))*{crop_x}':0")
        parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
    elif (out_w, out_h) != (src_w, src_h):
        parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
    parts.append("ass=captions.ass:fontsdir=fonts")
    return ",".join(parts)


def render(video: Path, ass_text: str, out: Path, flt: str, fonts_dir: Path | None,
           crf: int, max_seconds: float | None) -> list[str]:
    """Burn captions. Returns the font names libass actually selected."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "captions.ass").write_text(ass_text, encoding="utf-8")
        (td / "fonts").mkdir()
        if fonts_dir and fonts_dir.is_dir():
            for f in list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf")):
                shutil.copy(f, td / "fonts" / f.name)
        cmd = ["ffmpeg", "-n", "-hide_banner", "-v", "info", "-i", str(video.resolve())]
        if max_seconds:
            cmd += ["-t", str(max_seconds)]
        cmd += ["-vf", flt, "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(out.resolve())]
        r = subprocess.run(cmd, cwd=td, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("ffmpeg failed:\n" + r.stderr[-2000:])
        return sorted({m.group(1) for m in re.finditer(r"fontselect: \(([^,]+),", r.stderr)})


def validate_output_paths(video: Path, out: Path) -> Path:
    """Prevent rendering over the source or replacing existing output files."""
    video = Path(video)
    out = Path(out)
    if video.resolve() == out.resolve():
        raise ValueError("refusing to overwrite the source video")
    ass_path = out.with_suffix(".ass")
    if out.exists() or ass_path.exists():
        raise FileExistsError(
            f"refusing to overwrite existing caption output: {out if out.exists() else ass_path}"
        )
    return ass_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def print_report(events: list[Event], prof: Profile) -> None:
    print(f"profile={prof.name}  frame={prof.out_w}x{prof.out_h}  font={prof.font} {prof.font_px}px  "
          f"max {prof.max_chars} chars x {prof.max_lines} lines")
    warn = 0
    for i, ev in enumerate(events, 1):
        txt = " / ".join(" ".join(("*" + w.text + "*") if w.hl else w.text for w in l) for l in ev.lines)
        longest = max(_len(l) for l in ev.lines)
        flags = []
        if longest > prof.max_chars:
            flags.append("line too long")
            warn += 1
        long_words = [
            (word, word.end - word.start)
            for line in ev.lines
            for word in line
            if word.end - word.start > MAX_WORD_DURATION_SECONDS
        ]
        if long_words:
            details = ", ".join(
                f"{word.text!r} ({duration:.2f}s)" for word, duration in long_words
            )
            flags.append(f"word timing >{MAX_WORD_DURATION_SECONDS:.1f}s: {details}")
            warn += len(long_words)
        flag = f"  <-- {'; '.join(flags)}" if flags else ""
        print(f"{i:3d}  {ev.start:7.2f}-{ev.end:7.2f}  ({ev.end - ev.start:3.1f}s)  {txt}{flag}")
    print(f"{len(events)} captions, {warn} warning(s)")


def build(args, w: int, h: int) -> tuple[Profile, list[Event]]:
    prof = make_profile(args.profile, w, h, **({"font": args.font} if args.font else {}))
    words = load_transcript(args.transcript, args.highlight)
    if not words:
        sys.exit("transcript has no words")
    return prof, build_events(make_chunks(words, prof), prof)


def main() -> None:
    configure_console_encoding()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--profile", choices=["landscape", "vertical"], default="landscape")
        p.add_argument("--highlight", nargs="*", default=[], help="extra words to highlight in gold")
        p.add_argument("--font", help="override font family name")

    c = sub.add_parser("check", help="show the caption plan without rendering")
    c.add_argument("transcript", type=Path)
    c.add_argument("--size", default=None, help="WxH to plan for (default 1920x1080 / 1080x1920)")
    common(c)

    r = sub.add_parser("render", help="burn captions into a new MP4")
    r.add_argument("video", type=Path)
    r.add_argument("transcript", type=Path)
    r.add_argument("--out", type=Path)
    r.add_argument("--fonts-dir", type=Path, default=Path(__file__).parent / "fonts")
    r.add_argument("--scale-to", type=int, help="landscape only: output height, e.g. 1080")
    r.add_argument("--crop-x", type=float, default=0.5, help="vertical only: 0=left .. 1=right crop window")
    r.add_argument("--crf", type=int, default=18)
    r.add_argument("--max-seconds", type=float, help="render only the first N seconds (quick preview)")
    common(r)

    args = ap.parse_args()

    if args.cmd == "check":
        if args.size:
            w, h = map(int, args.size.lower().split("x"))
        else:
            w, h = (1080, 1920) if args.profile == "vertical" else (1920, 1080)
        prof, events = build(args, w, h)
        print_report(events, prof)
        return

    if not args.video.exists():
        sys.exit(f"video not found: {args.video}")
    info = probe(args.video)
    out_w, out_h = output_size(args.profile, info["w"], info["h"], args.scale_to)
    prof, events = build(args, out_w, out_h)
    print_report(events, prof)

    out = args.out or args.video.with_name(f"{args.video.stem}_captioned_{args.profile}.mp4")
    try:
        ass_path = validate_output_paths(args.video, out)
    except (OSError, ValueError) as e:
        ap.error(str(e))
    ass_text = build_ass(events, prof)
    try:
        with ass_path.open("x", encoding="utf-8") as ass_file:
            ass_file.write(ass_text)
    except OSError as e:
        ap.error(f"could not write ASS sidecar: {e}")
    flt = video_filter(args.profile, info["w"], info["h"], out_w, out_h, args.crop_x)
    fonts = render(args.video, ass_text, out, flt, args.fonts_dir, args.crf, args.max_seconds)
    print(f"\nwrote {out}  ({out_w}x{out_h})\nwrote {out.with_suffix('.ass')}  (editable in Aegisub)")
    if fonts:
        print("fonts used by renderer:", ", ".join(fonts))
        if prof.font not in fonts:
            print(f"WARNING: requested '{prof.font}' but renderer used {fonts}. Check fonts/ folder.")


if __name__ == "__main__":
    main()
