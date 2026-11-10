"""Optional layer on top of the finished captions.ass; timing is never changed here.

Runs inside every build, straight after write_ass, because each command rewrites the file.
Loaded only when effects.json sits beside edit.json. All times are output time.
"""
import json
import os
import re

import numpy as np
from PIL import Image

import captions
import render
import soundbed

TAG =re.compile(r"\{[^}]*\}")


def load(folder):
    path = os.path.join(folder, "effects.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _break_lines(styled, lines):
    """Writes the measured wrap as explicit \\N, skipping tags. A caption animating from
    92% could otherwise fit fewer lines and reflow as it grows."""
    out, seen, breaks, inside = [], 0, set(), False
    position = 0
    for line in lines[:-1]:
        position += len(line)
        breaks.add(position)  # index of the space that ends this line
        position += 1
    for ch in styled:
        if ch == "{":
            inside = True
        if inside:
            out.append(ch)
            inside = ch != "}"
            continue
        out.append("\\N" if seen in breaks and ch == " " else ch)
        seen += 1
    return "".join(out)


def _extra_style(item):
    s = item["style"]
    return (f"Style: {item['style_name']},Poppins,{s['size']},&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,-1,0,0,0,"
            f"100,100,0,0,1,0,0,8,{s['margin_l']},{s['margin_r']},{s['margin_v']},1")


def _block(item, lines, font):
    """One rectangle behind all lines, sized from the measured text. Drawn in frame pixels."""
    s = item["style"]
    pad = s["box_padding"]
    width = max(captions.line_width(x, s["size"], font) for x in lines)
    mid = (s["margin_l"] + 1080 - s["margin_r"]) / 2
    x0, x1 = round(mid - width / 2 - pad), round(mid + width / 2 + pad)
    y0, y1 = s["margin_v"] - pad, s["margin_v"] + len(lines) * s["size"] + pad
    return f"m {x0} {y0} l {x1} {y0} l {x1} {y1} l {x0} {y1}"


def _text_style(item, base):
    s = item["style"]
    return {"size": s["size"], "margin_l": s["margin_l"], "margin_r": s["margin_r"], "outline": s["box_padding"]}


def apply_v2(ass_path, phrases, total, effects, font):
    with open(ass_path, encoding="utf-8") as f:
        rows = f.read().splitlines()
    pop = effects.get("caption_pop", {})
    index = 0
    for i, row in enumerate(rows):
        if not row.startswith("Dialogue: 0,"):
            continue
        p = phrases[index]
        index += 1
        head, text = row.split(",,0,0,0,,", 1)
        text = _break_lines(text, p["lines"])
        if pop.get("enabled") and p["out_end"] - p["out_start"] >= pop["min_seconds"]:
            s = pop["start_scale"]
            text = (f"{{\\fad({pop['fade_ms']},0)\\fscx{s}\\fscy{s}\\t(0,{pop['ms']},\\fscx100\\fscy100)}}" + text)
        rows[i] = f"{head},,0,0,0,,{text}"

    extras, styles = [], []
    for key, name in (("hook", "Hook"), ("end_text", "EndText")):
        item = effects.get(key)
        if not item:
            continue
        item = dict(item, style_name=name)
        start = item["start"] if "start" in item else total - item["from_end"]
        end = item["end"] if isinstance(item.get("end"), (int, float)) else total
        lines = captions.fit_lines(item["text"], _text_style(item, None), font)
        styles.append(_extra_style(item))
        when = f"{captions._ass_time(start)},{captions._ass_time(end)},{name},,0,0,0,,"
        fade = f"\\fad({item['fade_in_ms']},{item['fade_out_ms']})"
        alpha = f"{round(255 * (1 - item['style']['box_opacity'])):02X}"
        extras.append(f"Dialogue: 1,{when}{{\\an7\\pos(0,0)\\1c&H000000&\\1a&H{alpha}&{fade}\\p1}}{_block(item, lines, font)}{{\\p0}}")
        extras.append(f"Dialogue: 2,{when}{{{fade}}}" + "\\N".join(lines))
    at = next(i for i, r in enumerate(rows) if r.startswith("Style: Cap,"))
    rows[at + 1:at + 1] = styles
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows + extras) + "\n")


def apply(ass_path, phrases, total, effects, font, shots=None, faces=None, style=None):
    if effects.get("style") == "v3":
        return apply_v3(ass_path, phrases, total, effects, font, shots, faces, style)
    apply_v2(ass_path, phrases, total, effects, font)
    return []


def pre_filters(effects):
    """Picture treatment, placed before ass so captions stay clean. Each part is a switch."""
    if not effects:
        return ""
    look, out = effects.get("look", {}), []
    if look.get("grade"):
        out.append(f"eq=contrast={look.get('contrast', 1.05)}:saturation={look.get('saturation', 1.03)}:gamma={look.get('gamma', 1.0)},"
                   f"colorbalance=rm={look.get('warm', 0.02)}:gm=0.0:bm={-look.get('warm', 0.02)}")
    if look.get("vignette"):
        out.append(f"vignette=angle={look.get('vignette_angle', 'PI/6')}:x0=w/2:y0=h*{look.get('vignette_y', 0.5)}")
    if look.get("grain"):
        out.append(f"noise=alls={look['grain_strength']}:allf=t")
    return "".join(x + "," for x in out)


def build_wall_mask(effects, shots, faces, folder):
    """A soft dark PNG with an opening around the head, centred on the median face across the edit."""
    wall = (effects or {}).get("wall_mask", {})
    if not wall.get("enabled"):
        return None
    xs, ys = [], []
    for sh in shots:
        k = 1920 / sh["crop"][1]
        for fr in faces:
            if fr["box"] and sh["in"] <= fr["t"] <= sh["out"]:
                xs.append((fr["box"][0] + fr["box"][2] / 2 - sh["crop"][2]) * k)
                ys.append((fr["box"][1] + fr["box"][3] / 2 - sh["crop"][3]) * k)
    cx, cy = float(np.median(xs)), float(np.median(ys))
    yy, xx = np.mgrid[0:1920, 0:1080]
    d = np.hypot((xx - cx) / wall["rx"], (yy - cy) / wall["ry"])
    around = np.clip((d - 1) / wall["feather"], 0, 1)
    above = np.clip((wall["bottom"] - yy) / wall["bottom_fade"], 0, 1)
    ramp = around * around * (3 - 2 * around) * above * above * (3 - 2 * above)
    rgba = np.zeros((1920, 1080, 4), dtype=np.uint8)
    rgba[..., 3] = np.round(ramp * wall["opacity"] * 255).astype(np.uint8)
    path = os.path.join(folder, "wall_mask.png").replace(chr(92), "/")
    Image.fromarray(rgba, "RGBA").save(path)
    return path


def snap_filter(shot, effects, protected):
    """Upscale-then-crop settle at the start of a shot. Off by default, never in protected time."""
    snap = (effects or {}).get("snap_zoom", {})
    if not snap.get("enabled") or any(a < shot["out"] and shot["in"] < b for a, b in protected):
        return ""
    a, d = snap["extra"], snap["seconds"]
    z = f"(1+{a}*pow(max(1-t/{d},0),2))"
    return f",scale=w='2*trunc(540*{z})':h='2*trunc(960*{z})':eval=frame:flags=lanczos,crop=1080:1920,setsar=1"


def zoom_amount(shot, effects, protected, limit):
    """How far this shot's slow zoom may travel: 0 in protected time, never past the zoom cap."""
    z = (effects or {}).get("motion_zoom", {})
    if not z.get("enabled") or any(a < shot["out"] and shot["in"] < b for a, b in protected):
        return 0
    return max(min(z["amount"], limit / shot["zoom"] - 1), 0)


def zoom_filter(shot, index, effects, protected, limit):
    """Alternating slow push-in and pull-out, one direction per shot."""
    amount = zoom_amount(shot, effects, protected, limit)
    if amount <= 0:
        return ""
    return "," + render.zoom_scale(amount, "in" if index % 2 == 0 else "out", 0, shot["out"] - shot["in"]) + ",setsar=1"


def _zoom_check(fx, add, shots):
    if not fx.get("motion_zoom", {}).get("enabled"):
        return
    limit = fx["limits"]["max_zoom"]
    protected = [(x["from"], x["to"]) for x in fx["_protected"]]
    moving = [s for s in shots if zoom_amount(s, fx, protected, limit) > 0]
    peak = max(s["zoom"] * (1 + zoom_amount(s, fx, protected, limit)) for s in moving)
    inside = [s["in"] for s in moving if any(a < s["out"] and s["in"] < b for a, b in protected)]
    add("slow zoom under the cap, none in protected sections", peak <= limit + 1e-6 and not inside,
        f"{len(moving)} of {len(shots)} shots, peak {peak * 100:.1f}%")


def check_snap_cap(shots, effects, protected, limit):
    snap = (effects or {}).get("snap_zoom", {})
    if not snap.get("enabled"):
        return
    for s in shots:
        if snap_filter(s, effects, protected) and s["zoom"] * (1 + snap["extra"]) > limit:
            raise SystemExit(f"Snap zoom over {limit} on shot at {s['in']:.2f}")


def _shape(w, h):
    return f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"


def _rounded(w, h, r):
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def _highlight_runs(marked, lines):
    """[(line index, text before run, run text)] for each *highlighted* stretch."""
    flags, plain, on = [], [], False
    for ch in marked:
        if ch == "*":
            on = not on
        else:
            plain.append(ch)
            flags.append(on)
    runs, pos = [], 0
    for li, line in enumerate(lines):
        i = 0
        while i < len(line):
            if flags[pos + i]:
                j = i
                while j < len(line) and flags[pos + j]:
                    j += 1
                runs.append((li, line[:i], line[i:j].rstrip()))
                i = j
            else:
                i += 1
        pos += len(line) + 1
    return runs


def _fit(reel, text, family, width, ceiling):
    """Largest size at which text fits width, using the font's measured libass calibration."""
    f = reel["fonts"][family]
    return next(x for x in range(ceiling, 20, -1) if captions.line_width(text, x * f["k"], f["file"]) <= width)


def _font_tag(reel, family):
    return f"\\fn{family}\\b{reel['fonts'][family].get('bold', 0)}"


def _face_top(shots, faces, a, b):
    """Highest face-box top in frame pixels between two output times; None if no face was found."""
    tops = []
    for fr in faces:
        if fr["box"]:
            for s in shots:
                if s["in"] <= fr["t"] <= s["out"] and a <= captions.to_output_time(fr["t"], shots) <= b:
                    tops.append((fr["box"][1] - s["crop"][3]) * 1920 / s["crop"][1])
    return min(tops) if tops else None


def _window(phrases, item):
    found = [[p for p in phrases if p["text"] == item[k]] for k in ("from", "to")]
    if any(len(x) != 1 for x in found):
        raise SystemExit(f"Needs one match for each phrase: {item['from']} / {item['to']}")
    return found[0][0]["out_start"], found[1][0]["out_end"]


def _reel_layer(reel, events, add, phrases, shots, faces, calm, spoken, mid, yellow, total, t):
    """Reference-style layer: animated cards, headlines and cut transitions.
    Picture only: drawn under the captions, and it changes neither timing nor audio."""
    said = spoken.casefold()
    in_calm = lambda a, b: any(x < b and a < y for x, y in calm)
    bad, notes, spans = [], [], []

    # full-screen cards that slide in, hold and slide out inside their phrase window
    c = reel["cards_cfg"]
    for card in reel["cards"]:
        a, b = _window(phrases, card)
        spans.append((a, b))
        words = " ".join(x["text"] for x in card["lines"])
        if words.casefold() not in said:
            bad.append(f"not spoken: {words}")
        if in_calm(a, b):
            bad.append(f"in protected section: {words}")
        sizes = [_fit(reel, x["text"], x["font"], c["max_width"], x["max_size"]) for x in card["lines"]]
        height = sum(sizes)
        if c["center_y"] - height / 2 < 220 or c["center_y"] + height / 2 > c["bottom_limit"]:
            bad.append(f"card outside safe area: {words}")
        sec = c["slide_ms"] / 1000
        phases = [(a, a + sec, 1080, 0), (a + sec, b - sec, 0, 0), (b - sec, b, 0, -1080)]
        for x0, x1, dx0, dx1 in phases:
            motion = (lambda x, y: f"\\move({x + dx0},{y},{x + dx1},{y})") if dx0 != dx1 else (lambda x, y: f"\\pos({x},{y})")
            events.append(f"Dialogue: 0,{t(x0)},{t(x1)},Shape,,0,0,0,,{{\\an7{motion(0, 0)}\\1c{c['background']}\\p1}}{_shape(1080, 1920)}{{\\p0}}")
            y = c["center_y"] - height / 2
            for line, size in zip(card["lines"], sizes):
                events.append(f"Dialogue: 0,{t(x0)},{t(x1)},Card,,0,0,0,,{{\\q2\\an5{motion(mid, round(y + size / 2))}{_font_tag(reel, line['font'])}\\fs{size}"
                              f"\\c{yellow if line['color'] == 'yellow' else '&H00FFFFFF&'}\\bord0\\shad0}}{line['text']}")
                y += size
        notes.append(f"{words} {a:.2f}-{b:.2f} {sizes}")
    add("cards: spoken words, outside protected sections, inside safe area", not bad, bad or "; ".join(notes))

    # headlines above the head, sized to clear each moment's own face box
    bad, notes = [], []
    h = reel["headlines_cfg"]
    for item in reel["headlines"]:
        a, b = _window(phrases, item)
        if item["text"].casefold() not in said:
            bad.append(f"not spoken: {item['text']}")
        if in_calm(a, b):
            bad.append(f"in protected section: {item['text']}")
        if any(x < b and a < y for x, y in spans):
            bad.append(f"overlaps a card: {item['text']}")
        top = _face_top(shots, faces, a, b)
        room = (top - h["face_margin"] - h["top"]) if top is not None else h["max_size"]
        size = min(_fit(reel, item["text"], h["font"], h["max_width"] / h["peak"], h["max_size"]), round(room))
        if size < h["min_size"]:
            bad.append(f"too small above head: {item['text']} ({size})")
        pk = round(h["peak"] * 100)
        events.append(f"Dialogue: 1,{t(a)},{t(b)},Card,,0,0,0,,{{\\q2\\an8\\pos({mid},{h['top']}){_font_tag(reel, h['font'])}\\fs{size}\\c{yellow}\\bord{h['outline']}\\3c&H00000000&\\shad0"
                      f"\\fscx{h['start_scale']}\\fscy{h['start_scale']}\\t(0,{h['ms'] * 2 // 3},\\fscx{pk}\\fscy{pk})\\t({h['ms'] * 2 // 3},{h['ms']},\\fscx100\\fscy100)\\fad(0,{h['fade_out_ms']})}}{item['text']}")
        notes.append(f"{item['text']} {size}")
    add("headlines: spoken words, clear of faces, cards and protected sections", not bad, bad or "; ".join(notes))

    # transitions sit on real cuts and hide them; they never change the cut itself
    bad, notes = [], []
    cuts, pos = [], 0.0
    for s in shots[:-1]:
        pos += s["out"] - s["in"]
        cuts.append(pos)
    x = reel["transitions_cfg"]
    half = x["swipe_ms"] / 2000
    for item in reel["transitions"]:
        cut = min(cuts, key=lambda c: abs(c - item["at"]))
        if abs(cut - item["at"]) > 0.05:
            bad.append(f"no cut near {item['at']}")
        a, b = (cut - half, cut + half) if item["type"] == "swipe" else (cut, cut + x["flash_ms"] / 1000)
        if in_calm(a - x["calm_margin"], b + x["calm_margin"]):
            bad.append(f"at a protected section: {cut:.2f}")
        if any(p < b and a < q for p, q in spans):
            bad.append(f"overlaps a card: {cut:.2f}")
        if item["type"] == "swipe":
            lead, w = x["lead_w"], 1300
            events.append(f"Dialogue: 0,{t(a)},{t(b)},Shape,,0,0,0,,{{\\an7\\move({-w - lead},0,1080,0)\\1c{x['color']}\\p1}}{_shape(w, 1920)}{{\\p0}}")
            events.append(f"Dialogue: 0,{t(a)},{t(b)},Shape,,0,0,0,,{{\\an7\\move({-lead},0,{1080 + w},0)\\1c{yellow}\\p1}}{_shape(lead, 1920)}{{\\p0}}")
        else:
            events.append(f"Dialogue: 0,{t(a)},{t(b)},Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H00FFFFFF&\\1a&H{x['flash_alpha']}&\\fad(0,{x['flash_ms']})\\p1}}{_shape(1080, 1920)}{{\\p0}}")
        notes.append(f"{item['type']} {cut:.2f}")
    add("transitions: on real cuts, away from cards and protected sections", not bad, bad or "; ".join(notes))


def _end_banner(reel, from_end, events, add, shots, faces, mid, total, t):
    e = reel["end_banner"]
    if e.get("enabled") is False:
        return
    start = total - from_end
    upper = e["line1"] if isinstance(e["line1"], list) else [e["line1"]]
    small = min(_fit(reel, x, "Poppins", e["width"] - 2 * e["pad"], e["line1_max"]) for x in upper)
    big = _fit(reel, e["line2"], "Anton", e["width"] - 2 * e["pad"], e["line2_max"])
    tall = small * len(upper)
    y0, y1 = e["top"], e["top"] + 2 * e["pad"] + tall + big
    x0, x1 = round(mid - e["width"] / 2), round(mid + e["width"] / 2)
    top = _face_top(shots, faces, start, total)
    add("end banner inside safe zone and clear of face",
        y0 >= 220 and x1 <= 960 and (top is None or y1 <= top), f"x {x0}-{x1}, y {y0}-{y1}, face top {top if top is None else round(top)}, sizes {small}/{big}")
    d, m = e["slide_px"], e["slide_ms"]
    dark = e["text"]
    events.append(f"Dialogue: 3,{t(start)},{t(total)},Shape,,0,0,0,,{{\\an7\\move({x0},{y0 - d},{x0},{y0},0,{m})\\1c{e['color']}\\p1}}{_rounded(x1 - x0, y1 - y0, e['radius'])}{{\\p0}}")
    events.append(f"Dialogue: 4,{t(start)},{t(total)},Card,,0,0,0,,{{\\q2\\an8\\move({mid},{y0 + e['pad'] - d},{mid},{y0 + e['pad']},0,{m}){_font_tag(reel, 'Poppins')}\\fs{small}\\c{dark}\\bord0\\shad0}}{chr(92).join(['', 'N']).join(upper)}")
    events.append(f"Dialogue: 4,{t(start)},{t(total)},Card,,0,0,0,,{{\\q2\\an8\\move({mid},{y0 + e['pad'] + tall - d},{mid},{y0 + e['pad'] + tall},0,{m}){_font_tag(reel, 'Anton')}\\fs{big}\\c{dark}\\bord0\\shad0}}{e['line2']}")


def broll_clips(effects):
    return (effects or {}).get("broll", {}).get("clips")


def _broll_checks(fx, add, calm, total):
    clips = broll_clips(fx)
    if not clips:
        return
    bad, spans = [], []
    for b in clips:
        name = os.path.basename(b["file"])
        if not os.path.exists(b["file"]):
            bad.append(f"missing {name}")
        if b["clip_start"] + b["end"] - b["start"] > fx["broll"]["clip_seconds"]:
            bad.append(f"runs past the end of {name}")
        if any(x < b["end"] and b["start"] < y for x, y in calm):
            bad.append(f"in protected section: {b['start']:.2f}")
        if any(x < b["end"] and b["start"] < y for x, y in spans) or b["end"] > total:
            bad.append(f"overlap or past end: {b['start']:.2f}")
        spans.append((b["start"], b["end"]))
    seconds = sum(y - x for x, y in spans)
    add("B-roll: clips exist, no overlap, outside protected sections", not bad, bad or f"{len(clips)} cutaways, {seconds:.1f}s of {total:.1f}s")


def _main_top(fx, p):
    """Top of a caption in the main band. With main_bottom, every caption ends on the same line."""
    return fx["main_bottom"] - len(p["lines"]) * p["fs"] if "main_bottom" in fx else fx["main_top"]


def main_limit(fx):
    """Lowest a word card may reach: just above the main caption band."""
    return fx["main_top"] - fx["plate"]["pad_y"]


def apply_v3(ass_path, phrases, total, fx, font, shots, faces, style):
    t = captions._ass_time
    with open(ass_path, encoding="utf-8") as f:
        rows = f.read().splitlines()
    first = next(i for i, r in enumerate(rows) if r.startswith("Dialogue:"))
    head, events, results = rows[:first], [], []

    def add(name, ok, detail):
        results.append({"check": name, "pass": bool(ok), "detail": str(detail)})

    size, l, r = style["size"], style["margin_l"], style["margin_r"]
    # measured against rendered frames: libass draws Poppins narrower than the Pillow estimate
    k = fx["width_calibration"]

    def measure(text, at_size):
        return captions.line_width(text, at_size * k, font)

    def wrap(text, st):
        return captions.fit_lines(text, dict(st, size=st["size"] * k), font)

    mid = (l + 1080 - r) / 2
    motion, marker, kinetic = fx["caption_motion"], fx["marker"], fx["kinetic"]
    yellow = style["highlight"]
    # each phrase takes the largest size that fits at its 108% peak: one line up to max_one,
    # two lines up to max_two (so the block still ends inside the band), never below the base size
    room = 1080 - l - r - 2 * style["outline"]
    # the plate scales with the text, so plate and overshoot must both stay inside the margins
    half = (1080 - l - r) / 2
    fit_room = 2 * (half / motion["peak"] - fx["plate"]["pad_x"])
    tight = dict(style, margin_r=round(1080 - l - 2 * style["outline"] - fit_room))
    over, sizes = [], {}
    for p in phrases:
        ceiling = kinetic["size"] if kinetic["phrase"] == p["text"] else fx["max_one_line"]
        chosen = None
        for candidate in range(ceiling, size - 1, -1):
            lines = wrap(p["text"], dict(tight, size=candidate))
            # the band above the head only has room for one line
            two_ok = len(lines) == 2 and candidate <= fx["max_two_lines"] and p["text"] not in fx["upper_phrases"]
            # fit_lines cannot break a long word that lands alone on a later line, so measure every line
            fits = all(measure(x, candidate) <= fit_room for x in lines if x)
            if "" not in lines and fits and (len(lines) == 1 or two_ok):
                chosen = candidate
                break
        if chosen is None:
            over.append(p["text"])
            chosen, lines = size, wrap(p["text"], dict(tight, size=size))
        p["fs"], p["lines"] = chosen, lines
        sizes[chosen] = sizes.get(chosen, 0) + 1
    add("captions fit 2 lines at 108%", not over, over or f"sizes used {min(sizes)}-{max(sizes)}: {dict(sorted(sizes.items()))}")

    # optional full-screen word cards: picture only, voice and captions carry on above them.
    # Drawn first so same-layer plates, captions and the progress bar sit on top.
    spoken = " ".join(p["text"] for p in phrases)
    calm = [(captions.to_output_time(x["from"], shots), captions.to_output_time(x["to"], shots)) for x in fx["_protected"]]
    card_cfg, card_notes, card_bad = fx.get("word_cards"), [], []
    for c in (card_cfg["cards"] if card_cfg else []):
        starts = [p for p in phrases if p["text"] == c["from"]]
        ends = [p for p in phrases if p["text"] == c["to"]]
        if len(starts) != 1 or len(ends) != 1:
            raise SystemExit(f"Word card needs one match for each phrase: {c['from']} / {c['to']}")
        a, b = starts[0]["out_start"], ends[0]["out_end"]
        card_lines = c["lines"]
        card_fs = next(x for x in range(card_cfg["max_size"], 40, -1)
                       if max(measure(w, x) for w in card_lines) <= card_cfg["max_width"])
        height = len(card_lines) * card_fs
        cy = card_cfg["center_y"]
        half_w = max(measure(w, card_fs) for w in card_lines) / 2
        if " ".join(card_lines) not in spoken:
            card_bad.append(f"not spoken: {' '.join(card_lines)}")
        if any(x < b and a < y for x, y in calm):
            card_bad.append(f"in protected section: {' '.join(card_lines)}")
        if mid + half_w > 960 or cy - height / 2 < 220 or cy + height / 2 + card_cfg["rule_gap"] > main_limit(fx):
            card_bad.append(f"outside safe area: {' '.join(card_lines)}")
        fade = f"\\fad(0,{card_cfg['fade_out_ms']})"
        grow = f"\\fscx{card_cfg['start_scale']}\\fscy{card_cfg['start_scale']}\\t(0,{card_cfg['pop_ms']},\\fscx100\\fscy100)"
        events.append(f"Dialogue: 0,{t(a)},{t(b)},Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c{card_cfg['background']}{fade}\\p1}}{_shape(1080, 1920)}{{\\p0}}")
        events.append(f"Dialogue: 0,{t(a)},{t(b)},Card,,0,0,0,,{{\\q2\\an5\\pos({mid},{cy})\\fs{card_fs}\\c{yellow}{grow}{fade}}}" + "\\N".join(card_lines))
        rule_w = round(2 * half_w)
        events.append(f"Dialogue: 0,{t(a)},{t(b)},Shape,,0,0,0,,{{\\an7\\pos({round(mid - half_w)},{round(cy + height / 2 + card_cfg['rule_gap'])})\\1c&H00FFFFFF&\\fscx0"
                      f"\\t(0,{card_cfg['rule_ms']},\\fscx100){fade}\\p1}}{_shape(rule_w, card_cfg['rule_h'])}{{\\p0}}")
        card_notes.append(f"{' '.join(card_lines)} {a:.2f}-{b:.2f} size {card_fs}")
    if card_cfg:
        add("word cards: spoken words, outside protected sections, inside safe area", not card_bad, card_bad or "; ".join(card_notes))

    reel = fx.get("reel_fx")
    if reel:
        _reel_layer(reel, events, add, phrases, shots, faces, calm, spoken, mid, yellow, total, t)

    plate, pill = fx["plate"], fx["pill"]
    plate_alpha =f"{round(255 * (1 - plate['opacity'])):02X}"
    widest = 0
    for p in phrases:
        fs = p["fs"]
        runs = _highlight_runs(p["marked"], p["lines"]) if pill["enabled"] else []
        parts = p["marked"].split("*")
        on = f"{{\\c{pill['text']}\\bord0\\shad0}}" if runs else f"{{\\c{yellow}}}"
        off = f"{{\\c{style['text']}\\bord{style['outline']}\\shad{style['shadow']}}}" if runs else f"{{\\c{style['text']}}}"
        styled = "".join(f"{on}{x}{off}" if i % 2 else x for i, x in enumerate(parts))
        styled = _break_lines(styled, p["lines"])
        a, b = p["out_start"], p["out_end"]
        upper = p["text"] in fx["upper_phrases"]
        top = fx["upper_top"] if upper else _main_top(fx, p)
        animated = b - a >= motion["min_seconds"]
        is_kinetic = kinetic["phrase"] == p["text"]
        width = max(measure(x, fs) for x in p["lines"])
        pw, ph = round(width + 2 * plate["pad_x"]), len(p["lines"]) * fs + 2 * plate["pad_y"]
        widest = max(widest, mid + pw / 2 * motion["peak"])
        lo, pk, ms = motion["start_scale"], round(motion["peak"] * 100), motion["ms"]
        # a separate shape scales about its own anchor, so a caption with a pill slides rigidly instead
        scale = (f"\\fscx{lo}\\fscy{lo}\\t(0,{ms * 2 // 3},\\fscx{pk}\\fscy{pk})\\t({ms * 2 // 3},{ms},\\fscx100\\fscy100)"
                 if animated and not runs else "")
        fade = f"\\fad(0,{motion['fade_out_ms']})"

        def place(x, y, sliding, dx, dy):
            if sliding:
                return f"\\move({x},{y + motion['slide']},{x},{y},0,{ms})"
            return f"\\pos({x + dx},{y + dy})"

        def emit(t0, t1, name, sliding, dx=0, dy=0, fading=True):
            f_tag = fade if fading else ""
            s_tag = scale if sliding else ""
            over_clip = fx.get("plate_on_broll") and any(x["start"] < b and a < x["end"] for x in broll_clips(fx) or ())
            if plate["enabled"] or over_clip:
                events.append(f"Dialogue: 0,{t(t0)},{t(t1)},Shape,,0,0,0,,{{\\an8{place(mid, top - plate['pad_y'], sliding, dx, dy)}"
                              f"\\1c&H000000&\\1a&H{plate_alpha}&{s_tag}{f_tag}\\p1}}{_rounded(pw, ph, plate['radius'])}{{\\p0}}")
            for li, before, run in runs:
                line = p["lines"][li]
                x0 = mid - measure(line, fs) / 2 + measure(before, fs) - pill["pad_x"]
                rw = round(measure(run, fs) + 2 * pill["pad_x"])
                y0 = round(top + (li + pill["top"]) * fs)
                rh = round((pill["bottom"] - pill["top"]) * fs)
                events.append(f"Dialogue: 1,{t(t0)},{t(t1)},Shape,,0,0,0,,{{\\an7{place(round(x0), y0, sliding, dx, dy)}"
                              f"\\1c{yellow}{f_tag}\\p1}}{_rounded(rw, rh, pill['radius'])}{{\\p0}}")
            events.append(f"Dialogue: 2,{t(t0)},{t(t1)},{name},,0,0,0,,{{\\q2\\an8\\fs{fs}{place(mid, top, sliding, dx, dy)}{s_tag}{f_tag}}}{styled}")

        if is_kinetic and animated:
            at = a + ms / 1000
            emit(a, at, "Cap", True, fading=False)
            for dx, dy in kinetic["offsets"]:
                emit(at, at + 1 / 30, "CapK", False, dx, dy, fading=False)
                at += 1 / 30
            emit(at, b, "CapK", False)
        else:
            emit(a, b, "Cap", animated)

    # progress bar: hidden through the calm section, resumes at its true position
    bar = fx["progress"]
    if bar["enabled"]:
        spans, pos = [], 0.0
        for x, y in bar["hide"] + [[total, total]]:
            if x > pos:
                spans.append((pos, x))
            pos = y
        for x, y in spans:
            ms = round((y - x) * 1000)
            events.append(f"Dialogue: 0,{t(x)},{t(y)},Shape,,0,0,0,,{{\\an7\\pos(0,{bar['y']})\\1c{yellow}\\1a&H40&"
                          f"\\fscx{100 * x / total:.3f}\\t(0,{ms},\\fscx{100 * y / total:.3f})\\p1}}{_shape(1080, bar['h'])}{{\\p0}}")

    # hook banner: two balanced lines, as large as fits above the speaker's face box
    hook_events, hook_results = len(events), len(results)
    h = fx["hook"]; hs = h["style"]
    pad = hs["box_padding"]
    words = h["text"].split()

    def balanced(at_size):
        splits = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]
        return min(splits, key=lambda x: max(measure(x[0], at_size), measure(x[1], at_size)))

    face_tops = []
    for fr in faces:
        if fr["box"]:
            for sh in shots:
                if sh["in"] <= fr["t"] <= sh["out"] and h["start"] - 0.3 <= captions.to_output_time(fr["t"], shots) <= h["end"] + 0.3:
                    face_tops.append((fr["box"][1] - sh["crop"][3]) * 1920 / sh["crop"][1])
    limit = min(face_tops) - h["face_margin"]
    room_hook = 1080 - hs["margin_l"] - hs["margin_r"] - 2 * pad
    hook_size = None
    for candidate in range(h["max_size"], 40, -1):
        pair = balanced(candidate)
        if max(measure(x, candidate) for x in pair) <= room_hook and hs["margin_v"] + 2 * candidate + pad <= limit:
            hook_size = candidate
            break
    hs = dict(hs, size=hook_size)
    lines = list(balanced(hook_size))
    width = max(measure(x, hook_size) for x in lines)
    hmid = (hs["margin_l"] + 1080 - hs["margin_r"]) / 2
    x0, x1 = round(hmid - width / 2 - pad), round(hmid + width / 2 + pad)
    y0, y1 = hs["margin_v"] - pad, hs["margin_v"] + len(lines) * hook_size + pad
    alpha = f"{round(255 * (1 - hs['box_opacity'])):02X}"
    add("hook clear of face box", y1 <= limit + h["face_margin"], f"size {hook_size}, block bottom {y1}, face box top {min(face_tops):.0f}")
    when = f"{t(h['start'])},{t(h['end'])},Hook,,0,0,0,,"
    d = h["slide_px"]; m = h["slide_ms"]; fade = f"\\fad(0,{h['fade_out_ms']})"
    events.append(f"Dialogue: 3,{when}{{\\an7\\move(0,{-d},0,0,0,{m})\\1c&H000000&\\1a&H{alpha}&{fade}\\p1}}"
                  f"m {x0} {y0} l {x1} {y0} l {x1} {y1} l {x0} {y1}{{\\p0}}")
    events.append(f"Dialogue: 3,{t(h['start'])},{t(h['end'])},Shape,,0,0,0,,{{\\an7\\pos({x0},{y1})\\1c{yellow}\\fscx0"
                  f"\\t({m},{m + h['wipe_ms']},\\fscx100){fade}\\p1}}{_shape(x1 - x0, 6)}{{\\p0}}")
    hook_text = "\\N".join(lines).replace(h["key_word"], f"{{\\c{yellow}}}{h['key_word']}{{\\c&H00FFFFFF&}}")
    events.append(f"Dialogue: 4,{when}{{\\q2\\an8\\fs{hook_size}\\move({hmid},{hs['margin_v'] - d},{hmid},{hs['margin_v']},0,{m}){fade}}}" + hook_text)

    if h.get("enabled") is False:
        del events[hook_events:], results[hook_results:]

    # end card
    end_events, end_results = len(events), len(results)
    e = fx["end_text"]; es = e["style"]
    lines = wrap(e["text"], _text_style(e, None))
    add("end text fits 2 lines", len(lines) <= 2, lines)
    start = total - e["from_end"]
    pad = es["box_padding"]
    width = max(measure(x, es["size"]) for x in lines)
    emid = (es["margin_l"] + 1080 - es["margin_r"]) / 2
    x0, x1 = round(emid - width / 2 - pad), round(emid + width / 2 + pad)
    y0, y1 = es["margin_v"] - pad, es["margin_v"] + len(lines) * es["size"] + pad
    alpha = f"{round(255 * (1 - es['box_opacity'])):02X}"
    pop = "\\fscx85\\fscy85\\t(0,140,\\fscx100\\fscy100)"
    events.append(f"Dialogue: 3,{t(start)},{t(total)},EndText,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H000000&\\1a&H{alpha}&\\fad(100,0)\\p1}}"
                  f"m {x0} {y0} l {x1} {y0} l {x1} {y1} l {x0} {y1}{{\\p0}}")
    events.append(f"Dialogue: 4,{t(start)},{t(total)},EndText,,0,0,0,,{{\\q2\\an8\\pos({emid},{es['margin_v']}){pop}\\fad(100,0)}}" + "\\N".join(lines))
    tag_y = y1 + 14
    tag_t = start + e["tag_delay"]
    events.append(f"Dialogue: 3,{t(tag_t)},{t(total)},EndText,,0,0,0,,{{\\q2\\an8\\pos({emid},{tag_y})\\fs{e['tag_size']}\\c{yellow}\\bord4\\3c&H000000&\\fad(120,0)}}Namma Kadhai")
    ax, ay = x1 - 74, tag_y + 8
    arrow = "m 0 14 l 36 14 l 36 0 l 66 24 l 36 48 l 36 34 l 0 34"
    events.append(f"Dialogue: 3,{t(tag_t)},{t(total)},Shape,,0,0,0,,{{\\an7\\pos({ax},{ay})\\1c{yellow}\\fad(120,0)\\p1}}{arrow}{{\\p0}}")
    bottom = ay + 48
    add("end card inside safe zone", y0 >= 220 and bottom <= 1440 and max(x1, ax + 66) <= 960, f"x {x0}-{max(x1, ax + 66)}, y {y0}-{bottom}")
    if reel:
        # the banner replaces the small end block, tag and arrow
        del events[end_events:], results[end_results:]
        _end_banner(reel, e["from_end"], events, add, shots, faces, mid, total, t)

    # face clearance for every caption, using each phrase's own frames
    def face_range(p):
        tops, bots = [], []
        for s in shots:
            if s["in"] <= p["start"] < s["out"] or (p["start"] < s["in"] and s["in"] < p["end"]):
                k = 1920 / s["crop"][1]
                for fr in faces:
                    if p["start"] <= fr["t"] <= p["end"] and s["in"] <= fr["t"] <= s["out"] and fr["box"]:
                        bx = fr["box"]
                        tops.append((bx[1] - s["crop"][3]) * k)
                        bots.append((bx[1] + bx[3] - s["crop"][3]) * k)
        return tops, bots

    worst_chin, bad = 0, []
    for p in phrases:
        upper = p["text"] in fx["upper_phrases"]
        fs = p["fs"]
        tops, bots = face_range(p)
        if upper:
            cap_bottom = fx["upper_top"] + len(p["lines"]) * fs + fx["plate"]["pad_y"]
            if cap_bottom > fx["upper_bottom"] or (tops and cap_bottom + fx["hair_margin"] > min(tops)):
                bad.append(f"{p['text']} (bottom {cap_bottom}, face top {min(tops) if tops else 'n/a':.0f})")
        else:
            cap_top = _main_top(fx, p)
            cap_bottom = cap_top + len(p["lines"]) * fs + fx["plate"]["pad_y"]
            if bots:
                worst_chin = max(worst_chin, max(bots))
            if cap_bottom > fx.get("caption_bottom_limit", 1440) or (bots and max(bots) > cap_top - fx["plate"]["pad_y"]):
                bad.append(f"{p['text']} (bottom {cap_bottom})")
    add("captions clear of face boxes, inside y 220-1440", not bad, bad or f"chin box max {worst_chin:.0f}, main top {fx['main_top']}")

    add("caption plates inside x<=960", widest <= 960, f"right edge {widest:.0f}")
    _broll_checks(fx, add, calm, total)
    _zoom_check(fx, add, shots)
    fx["_bed"] = soundbed.build(fx, shots, total, os.path.dirname(ass_path), add)

    protected_output = [(captions.to_output_time(x["from"], shots), captions.to_output_time(x["to"], shots)) for x in fx["_protected"]]
    in_calm = [p["text"] for p in phrases if p["text"] in fx["upper_phrases"] and any(a <= p["out_start"] <= b for a, b in protected_output)]
    add("no upper-band jump in protected sections", not in_calm, in_calm or "none")

    styles = [
        f"Style: CapK,{style['font']},{size},{style['text']},{style['text']},{style['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},8,{l},{r},{fx['main_top']},1",
        "Style: Shape,Poppins,20,&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        _v3_text_style("Hook", hs),
        _v3_text_style("EndText", es),
    ]
    at = next(i for i, x in enumerate(head) if x.startswith("Style: Cap,"))
    if card_cfg or reel:
        styles.append(_v3_text_style("Card", {"size": card_cfg["max_size"] if card_cfg else 100, "margin_l": l, "margin_r": r, "margin_v": 0}))
    head[at + 1:at + 1] = styles
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + events) + "\n")
    return results


def _v3_text_style(name, s):
    return (f"Style: {name},Poppins,{s['size']},&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,-1,0,0,0,"
            f"100,100,0,0,1,0,0,8,{s['margin_l']},{s['margin_r']},{s['margin_v']},1")
