"""Resolves scenes.json against the real timeline, checks the rules, and writes Remotion's data.

Run from the AI Video Editor folder after `nk.py plan`. Stops on any rule failure.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, "engine")
import captions

EDIT = "stories/namma-kadhai-v13"
REMOTION = "remotion"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    timeline, scenes = load(f"{EDIT}/timeline.json"), load("stories/namma-kadhai-v13/scenes.json")
    edit, effects = load(f"{EDIT}/edit.json"), load(f"{EDIT}/effects.json")
    phrases, shots = timeline["phrases"], timeline["shots"]
    calm = [(captions.to_output_time(x["from"], shots), captions.to_output_time(x["to"], shots)) for x in edit["pause_policy"]["protected"]]
    spoken = " ".join(p["text"] for p in phrases).casefold()
    bad, resolved = [], []
    for s in scenes:
        a = [p for p in phrases if p["text"] == s["from"]]
        b = [p for p in phrases if p["text"] == s["to"]]
        if len(a) != 1 or len(b) != 1:
            bad.append(f"{s['id']}: phrase not unique or missing")
            continue
        start, end = a[0]["out_start"], b[0]["out_end"]
        if end <= start:
            bad.append(f"{s['id']}: negative length")
        if any(x < end and start < y for x, y in calm):
            bad.append(f"{s['id']}: in protected section ({start:.2f}-{end:.2f})")
        for label in s["labels"]:
            if label.casefold() not in spoken:
                bad.append(f"{s['id']}: not spoken: {label}")
        resolved.append(dict(s, start=start, end=end))
    resolved.sort(key=lambda s: s["start"])
    for x, y in zip(resolved, resolved[1:]):
        if y["start"] < x["end"] - 1e-6:
            bad.append(f"overlap: {x['id']} and {y['id']}")
    if bad:
        raise SystemExit("\n".join(bad))

    total = timeline["output_seconds"]
    data = {"total": total, "calm": calm, "scenes": resolved,
            "hook": effects["hook"]["text"], "hookWord": effects["hook"]["key_word"],
            "endLine": "Neenga yaarukkaavadhu support-aa irundhurukkeengala?", "endCall": "Comment-la sollunga",
            "phrases": [{"text": p["text"], "marked": p["marked"], "start": p["out_start"], "end": p["out_end"]} for p in phrases]}
    os.makedirs(f"{REMOTION}/src", exist_ok=True)
    with open(f"{REMOTION}/src/data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)

    effects["sound"]["cues"] = [{"at": max(s["start"] - 0.2, 0), "kind": "whoosh"} for s in resolved if s["id"] != "meter"]
    effects["sound"]["cues"] += [{"at": s["start"], "kind": "hit"} for s in resolved if s["id"] == "meter"]
    effects["look"]["grain"] = False  # Remotion re-encodes; grain only adds size
    with open(f"{EDIT}/effects.json", "w", encoding="utf-8") as f:
        json.dump(effects, f, ensure_ascii=False, indent=1)

    os.makedirs(f"{REMOTION}/public/fonts", exist_ok=True)
    for name in ("Anton-Regular.ttf", "KaushanScript-Regular.ttf", "Poppins-Bold.ttf"):
        shutil.copyfile(f"fonts/{name}", f"{REMOTION}/public/fonts/{name}")
    print(f"ok: {len(resolved)} scenes, {sum(s['end'] - s['start'] for s in resolved):.1f}s of {total:.1f}s")
    for s in resolved:
        print(f"  {s['id']:9} {s['start']:6.2f}-{s['end']:6.2f} {s['layout']}")


if __name__ == "__main__":
    main()
