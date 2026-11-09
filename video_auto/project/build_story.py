"""Checks a story's beats against the real timeline and writes the data for its composition.

Run from the AI Video Editor folder, after `nk.py plan`:  python video_auto/project/build_story.py ramayana
Stops on any rule failure. Before the base exists it uses the planned length; run it again after `nk.py base`.
Also writes the sound cues into effects.json, so the next `nk.py base` places them.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, "edited/v2/engine")
sys.path.insert(0, "video_auto/project")
import captions
from analyze_references import FP

STORY = sys.argv[1]
EDIT = f"edited/v2/edits/{STORY}-v1"
REMOTION = "video_auto/project/remotion"
CLIP_SECONDS = 10.0
# brightness, contrast, saturation, warmth as multipliers: applied to all footage, never to text
GRADES = {"diwali2": {"brightness": 0.93, "contrast": 1.08, "saturation": 0.94, "warmth": 0.05}}
COLLAGE = [] if STORY != "deepavali" else ["morning-1", "dress-4.5", "crackers-4", "doorstep-3", "terrace-4", "morning-5", "dress-1", "crackers-8", "doorstep-7", "terrace-7", "dress-7", "crackers-1"]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def clip_seconds(name):
    path = f"{REMOTION}/public/broll/{name}.mp4"
    if not os.path.exists(path):
        return CLIP_SECONDS
    return float(subprocess.run([FP, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                capture_output=True, text=True, check=True).stdout.strip())


def main():
    timeline, beats = load(f"{EDIT}/timeline.json"), load(f"video_auto/project/{STORY}_beats.json")
    edit, effects = load(f"{EDIT}/edit.json"), load(f"{EDIT}/effects.json")
    phrases, shots = timeline["phrases"], timeline["shots"]
    # the base decides the length: the source can end a frame or two before the plan's last cut
    base = f"video_auto/previews/{edit['name']}/{edit['name']}-base.mp4"
    total = timeline["output_seconds"]
    if os.path.exists(base):
        total = int(subprocess.run([FP, "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries", "stream=nb_read_frames",
                                    "-of", "csv=p=0", base], capture_output=True, text=True, check=True).stdout.strip()) / 30
    calm = [(captions.to_output_time(x["from"], shots), captions.to_output_time(x["to"], shots)) for x in edit["pause_policy"]["protected"]]
    spoken = " ".join(p["text"] for p in phrases).casefold()
    bad, out = [], []

    for i, b in enumerate(beats):
        hits = [p for p in phrases if p["text"] == b["from"]]
        if len(hits) != 1:
            bad.append(f"phrase not unique or missing: {b['from']}")
            continue
        # whole frames, so Python and the browser can never round a beat boundary differently
        out.append(dict(b, start=0.0 if i == 0 else round(hits[0]["out_start"] * 30) / 30))
    if bad:
        raise SystemExit("\n".join(bad))
    for a, b in zip(out, out[1:] + [{"start": total}]):
        a["end"] = b["start"]

    for b in out:
        if b.get("secondFrom"):
            hits = [p for p in phrases if p["text"] == b["secondFrom"]]
            if len(hits) != 1:
                bad.append(f"phrase not unique or missing: {b['secondFrom']}")
            else:
                b["secondAt"] = round(hits[0]["out_start"] * 30) / 30
        # "approved" marks text the user asked for by name, even though it is not spoken in the take
        for words in () if b.get("approved") else (b.get("kicker", ""), *b.get("text", "").split("|"), *b.get("text2", "").split("|")):
            for word in words.replace("?", "").split():
                if word.casefold().strip(",.") not in spoken:
                    bad.append(f"not spoken: {word} ({b['from']})")
        if b["type"] != "head" or b.get("text") or b.get("sparks"):
            if any(x < b["end"] - 1 / 30 and b["start"] < y for x, y in calm):  # a frame of rounding is allowed
                bad.append(f"graphics in the protected section: {b['from']} {b['start']:.2f}-{b['end']:.2f}")
        if b.get("clip") and not b.get("clipFrames") and b.get("clipStart", 0) + (b["end"] - b["start"]) > clip_seconds(b["clip"]):
            bad.append(f"runs off the end of {b['clip']}: {b['from']}")
        if b.get("clip2") and b["clip2Start"] + (b["end"] - b.get("secondAt", b["start"])) > clip_seconds(b["clip2"]):
            bad.append(f"runs off the end of {b['clip2']}: {b['from']}")
        for name in (b.get("clip"), b.get("clip2")):
            if name and not os.path.exists(f"{REMOTION}/public/broll/{name}.mp4"):
                bad.append(f"missing clip: {name}")
        if b.get("under") and not os.path.exists(f"{REMOTION}/public/broll/{b['under'].rsplit('-', 1)[0]}.mp4"):
            bad.append(f"no clip for still: {b['under']}")
    for name in COLLAGE:
        if not os.path.exists(f"{REMOTION}/public/stills/{name}.jpg"):
            bad.append(f"missing still: {name}")
    if bad:
        raise SystemExit("\n".join(bad))

    data = {"total": total, "splitLift": 420, "collage": COLLAGE, "beats": out, "subPlate": {"diwali2": True, "ramayana5": 0.5}.get(STORY, False), "grade": GRADES.get(STORY),
            "phrases": [{"text": p["text"], "start": p["out_start"], "end": p["out_end"]} for p in phrases]}
    with open(f"{REMOTION}/src/{STORY}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)

    effects["sound"]["cues"] = []
    for b in out:
        sounds = b.get("sfx") or []
        for item in [sounds] if isinstance(sounds, str) else sounds:
            kind, _, after = item.partition("@")
            effects["sound"]["cues"].append({"at": max(b["start"] + float(after or 0), 0), "kind": kind})
    effects["sound"]["cues"] += [{"at": b["secondAt"], "kind": b["sfx2"]} for b in out if b.get("sfx2")]
    with open(f"{EDIT}/effects.json", "w", encoding="utf-8") as f:
        json.dump(effects, f, ensure_ascii=False, indent=1)
    lengths = sorted(b["end"] - b["start"] for b in out)
    print(f"ok: {len(out)} beats over {total:.2f}s, median {lengths[len(lengths) // 2]:.2f}s, longest {lengths[-1]:.2f}s")
    for b in out:
        print(f"  {b['start']:6.2f}-{b['end']:6.2f} {b['type']:8}{b.get('clip', ''):9} {b.get('kicker', '')} | {b.get('text', '')}")


if __name__ == "__main__":
    main()
