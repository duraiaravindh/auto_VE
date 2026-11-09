"""Namma Kadhai editing engine.

  python edited/v2/engine/nk.py plan   edited/v2/edits/<name>/edit.json   # analysis + timeline, no video
  python edited/v2/engine/nk.py render edited/v2/edits/<name>/edit.json --approved
  python edited/v2/engine/nk.py qc     edited/v2/edits/<name>/edit.json
  python edited/v2/engine/nk.py preview edited/v2/edits/<name>/edit.json 0:5 24:5    # short review clips
  python edited/v2/engine/nk.py length edited/v2/edits/<name>/edit.json              # true frame count, no video
  python edited/v2/engine/nk.py still  edited/v2/edits/<name>/edit.json 0.5 96.5   # test PNGs at output times

All creative decisions live in edit.json. Raw footage is only ever read.
"""
import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import audio
import captions
import effects
import face
import framing
import pauses
import render
import qc
from common import ROOT, clock, parse_srt, run


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(edit_path):
    edit = load(edit_path)
    folder = os.path.dirname(edit_path)
    cache = os.path.join(folder, "cache")
    os.makedirs(cache, exist_ok=True)
    source = glob.glob(edit["source"])[0]
    cues = parse_srt(edit["srt"])
    captions.validate_text(cues, edit["phrases"])

    info = json.loads(run("ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                          "-show_entries", "format=duration", "-of", "json", source).stdout)
    src_w, src_h = info["streams"][0]["width"], info["streams"][0]["height"]
    duration = float(info["format"]["duration"])
    fps = edit["fps"]
    policy = edit["pause_policy"]

    env = pauses.envelope(source)
    silences = pauses.detect_silences(source, policy["noise_db"], policy["min_detect"])
    decisions = pauses.decide(silences, policy, duration)
    ranges = pauses.kept_ranges(decisions, policy, duration, fps, policy["end_hold"])
    ranges = pauses.insert_pauses(ranges, edit.get("pause_inserts", ()), fps)
    shots = framing.build_shots(ranges, cues, edit["zoom_by_cue"], fps, edit.get("zoom_splits", ()))
    layer = effects.load(folder)
    framing.check_max_zoom(shots, layer["limits"]["max_zoom"] if layer else 1.20)
    faces = face.track(source, cache)
    if edit.get("hflip"):
        # the footage is flipped before cropping, so face positions must be mirrored to match
        faces = [dict(fr, box=[src_w - fr["box"][0] - fr["box"][2], *fr["box"][1:]] if fr["box"] else fr["box"]) for fr in faces]
    if layer:
        layer["_protected"] = policy["protected"]
    protected = [(x["from"], x["to"]) for x in policy["protected"]]
    effects.check_snap_cap(shots, layer, protected, layer["limits"]["max_zoom"] if layer else 1.20)
    framing.place_crops(shots, faces, src_w, src_h, 1080, 1920,
                        caption_top=edit["caption_style"]["top"])

    phrases = captions.time_phrases(cues, edit["phrases"], env, silences)
    captions.place_on_output(phrases, shots, edit["caption_style"])
    font = "edited/v2/fonts/Poppins-Bold.ttf"
    for p in phrases:
        p["lines"] = captions.fit_lines(p["text"], edit["caption_style"], font)
    captions.check_lines(phrases)

    captions.write_ass(phrases, edit["caption_style"], os.path.join(folder, "captions.ass").replace("\\", "/"))
    total = sum(s["out"] - s["in"] for s in shots)
    if layer:
        edit["_fx_results"] = effects.apply(os.path.join(folder, "captions.ass"), phrases, total, layer, font,
                                            shots, faces, edit["caption_style"])
        edit["_mask"] = effects.build_wall_mask(layer, shots, faces, folder)
    edit["_layer"], edit["_protected"] = layer, protected
    return edit, folder, source, cues, decisions, shots, phrases, total, duration


def write_plan(folder, decisions, shots, phrases, total, duration, edit):
    with open(os.path.join(folder, "timeline.json"), "w", encoding="utf-8") as f:
        json.dump({"source_seconds": duration, "output_seconds": total, "pauses": decisions,
                   "shots": shots, "phrases": phrases}, f, indent=1, ensure_ascii=False)
    with open(os.path.join(folder, "edl.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["shot", "source_in", "source_out", "output_in", "zoom", "crop_w", "crop_h", "crop_x", "crop_y", "flags"])
        pos = 0.0
        for i, s in enumerate(shots, 1):
            w.writerow([i, f"{s['in']:.3f}", f"{s['out']:.3f}", f"{pos:.3f}", s["zoom"], *s["crop"], "; ".join(s["flags"])])
            pos += s["out"] - s["in"]
    lines = [f"# Timeline: {edit['name']}", f"Source {duration:.2f}s, output {total:.2f}s.", "",
             "## Pauses", "| Source | Length | Decision | Why |", "|---|---|---|---|"]
    lines += [f"| {p['start']:.2f}-{p['end']:.2f} | {p['length']:.2f} | {p['action']} | {p['why']} |" for p in decisions]
    lines += ["", "## Shots", "| # | Source in-out | Output in | Zoom | Crop w,h,x,y | Face | Flags |", "|---|---|---|---|---|---|---|"]
    pos = 0.0
    for i, s in enumerate(shots, 1):
        lines.append(f"| {i} | {s['in']:.3f}-{s['out']:.3f} | {clock(pos)} | {round(s['zoom'] * 100)}% | {s['crop']} | "
                     f"{s['face_found']}/{s['face_samples']} | {'; '.join(s['flags'])} |")
        pos += s["out"] - s["in"]
    lines += ["", "## Captions", "| Cue | Output | Source | Text | Lines | Boundary | Chars/s |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {p['cue']} | {p['out_start']:.2f}-{p['out_end']:.2f} | {p['start']:.2f}-{p['end']:.2f} | {p['marked']} | "
              f"{len(p['lines'])} | {p['boundary']} | {p['cps']} |" for p in phrases]
    with open(os.path.join(folder, "TIMELINE.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def make_graph(edit, shots, ass, af, review=False):
    layer = edit.get("_layer")
    limit = layer["limits"]["max_zoom"] if layer else 1.20
    filt = (lambda s: effects.snap_filter(s, layer, edit["_protected"])
            + effects.zoom_filter(s, shots.index(s), layer, edit["_protected"], limit)) if layer else None
    return render.graph(shots, ass, af, edit["fps"], review=review, exact=edit.get("exact_trims", False),
                        pre=effects.pre_filters(layer), shot_filter=filt, mask=edit.get("_mask"),
                        broll=effects.broll_clips(layer), hflip=edit.get("hflip", False),
                        bed=(layer or {}).get("_bed"), source_filter=edit.get("source_filter", ""))


def print_ass_checks(ass, cues, edit):
    results = qc.check_ass(ass, cues, edit["caption_style"], "edited/v2/fonts/Poppins-Bold.ttf") + edit.get("_fx_results", [])
    for r in results:
        print("PASS" if r["pass"] else "FAIL", r["check"], "-", r["detail"])
    if not all(r["pass"] for r in results):
        raise SystemExit("ASS checks failed")


def still(edit, folder, source, shots, at, out):
    """One PNG at an output time with captions and effects, no video written."""
    pos = 0.0
    for s in shots:
        length = s["out"] - s["in"]
        if at < pos + length:
            w, h, x, y = s["crop"]
            ass = os.path.join(folder, "captions.ass").replace(chr(92), "/")
            mask = edit.get("_mask")
            wall = f"[vb];movie={mask},format=rgba[mk];[vb][mk]overlay=eof_action=repeat" if mask else ""
            run("ffmpeg", "-v", "error", "-y", "-ss", f"{s['in'] + at - pos:.3f}", "-i", source, "-frames:v", "1", "-vf",
                f"{'hflip,' if edit.get('hflip') else ''}crop={w}:{h}:{x}:{y},scale=1080:1920:flags=lanczos,setsar=1,setpts={at}/TB,unsharp=5:5:0.35{wall},"
                f"{effects.pre_filters(edit.get('_layer'))}ass={ass}:fontsdir=edited/v2/fonts", out)
            return
        pos += length
    raise SystemExit(f"{at}s is past the end")


def main():
    command, edit_path = sys.argv[1], os.path.abspath(sys.argv[2])
    os.chdir(ROOT)
    edit_path = os.path.relpath(edit_path, ROOT)
    edit, folder, source, cues, decisions, shots, phrases, total, duration = build(edit_path)
    ass = os.path.join(folder, "captions.ass").replace("\\", "/")
    review_dir = edit["review_dir"]

    if command == "plan":
        write_plan(folder, decisions, shots, phrases, total, duration, edit)
        m = audio.measure(source, edit["audio"])
        with open(os.path.join(folder, "audio_measure.json"), "w", encoding="utf-8") as f:
            json.dump({"chain": audio.chain(edit["audio"]), "first_pass": m, "second_pass_type": audio.verify_linear(source, edit["audio"], m), "source_peak": audio.clipping_report(source)}, f, indent=1)
        final_graph = make_graph(edit, shots, ass, audio.final_filter(edit["audio"], m))
        render.validate_graph(source, final_graph, os.path.join(folder, "filter_graph.txt"))
        print(framing.check_max_zoom(shots))
        print_ass_checks(ass, cues, edit)
        print(f"plan ok: {len(shots)} shots, {len(phrases)} captions, {total:.2f}s of {duration:.2f}s. Graph validated (no video written).")
    elif command == "still":
        os.makedirs(os.path.join(folder, "test_frames"), exist_ok=True)
        for at in sys.argv[3:]:
            still(edit, folder, source, shots, float(at), os.path.join(folder, "test_frames", f"t{at}.png"))
    elif command == "preview":
        # args: start:seconds ...  Review settings, cut from the finished graph
        os.makedirs(os.path.join(review_dir, "previews"), exist_ok=True)
        m = audio.measure(source, edit["audio"])
        # --clean leaves out the timecode, shot data and safe-zone guides
        g = make_graph(edit, shots, ass, audio.final_filter(edit["audio"], m), review="--clean" not in sys.argv)
        for item in (x for x in sys.argv[3:] if x != "--clean"):
            start, seconds = item.split(":")
            render.preview(source, g, os.path.join(folder, "filter_graph_review.txt"),
                           os.path.join(review_dir, "previews", f"preview-{start}s.mp4"), start, seconds)
    elif command == "length":
        m = audio.measure(source, edit["audio"])
        g = make_graph(edit, shots, ass, audio.final_filter(edit["audio"], m))
        frames, seconds = render.measure_length(source, g, os.path.join(folder, "filter_graph.txt"))
        print(f"graph output {frames} frames = {seconds:.3f}s; timeline {round(total * edit['fps'])} frames = {total:.3f}s")
    elif command == "render":
        if "--approved" not in sys.argv:
            raise SystemExit("Render needs --approved, after the timeline has been reviewed.")
        os.makedirs(review_dir, exist_ok=True)
        m = audio.measure(source, edit["audio"])
        af = audio.final_filter(edit["audio"], m)
        captions.write_srt(phrases, os.path.join(review_dir, f"{edit['name']}.srt"))
        # optional "final_encode" in edit.json: e.g. a bitrate cap and no review copy when disk is short
        settings = edit.get("final_encode", {})
        render.encode(source, make_graph(edit, shots, ass, af), os.path.join(folder, "filter_graph.txt"),
                      os.path.join(review_dir, f"{edit['name']}-final.mp4"), crf=settings.get("crf", "17"),
                      preset=settings.get("preset", "slow"), maxrate=settings.get("maxrate"))
        if not settings.get("skip_review"):
            render.encode(source, make_graph(edit, shots, ass, af, review=True),
                          os.path.join(folder, "filter_graph_review.txt"),
                          os.path.join(review_dir, f"{edit['name']}-review.mp4"), crf="23", preset="veryfast")
        print("rendered; run qc next")
    elif command == "base":
        # speaker edit with sound and no burned text, for compositing in Remotion
        os.makedirs(review_dir, exist_ok=True)
        with open(ass, encoding="utf-8") as f:
            header = f.read().split("Dialogue:", 1)[0]
        blank = os.path.join(folder, "blank.ass").replace("\\", "/")
        with open(blank, "w", encoding="utf-8") as f:
            f.write(header)
        m = audio.measure(source, edit["audio"])
        write_plan(folder, decisions, shots, phrases, total, duration, edit)
        out = os.path.join(review_dir, f"{edit['name']}-base.mp4")
        if os.path.exists(out):
            os.remove(out)  # generated intermediate, always rebuilt
        render.encode(source, make_graph(edit, shots, blank, audio.final_filter(edit["audio"], m)),
                      os.path.join(folder, "filter_graph.txt"), out, crf="18", preset="veryfast")
        print("base written", out)
    elif command == "qc":
        results = qc.check(os.path.join(review_dir, f"{edit['name']}-final.mp4"), total, edit["fps"], phrases, cues,
                           os.path.join(review_dir, "contact_sheet.jpg"), os.path.join(review_dir, "technical_report.json"))
        print_ass_checks(ass, cues, edit)
        for r in results:
            print("PASS" if r["pass"] else "FAIL", r["check"], "-", r["detail"])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
