"""Checks text_moments.json against the real timeline and writes the data for the Reel5 composition.

Run from the AI Video Editor folder. Stops on any rule failure.
"""
import json
import sys

sys.path.insert(0, "edited/v2/engine")
import captions

EDIT = "edited/v2/edits/namma-kadhai-v13"
REMOTION = "video_auto/project/remotion"
END_SECONDS = 1.9  # starts as the sign-off begins, after "share pannunga"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    timeline, moments = load(f"{EDIT}/timeline.json"), load("video_auto/project/text_moments.json")
    edit, effects = load(f"{EDIT}/edit.json"), load(f"{EDIT}/effects.json")
    phrases, shots = timeline["phrases"], timeline["shots"]
    total = timeline["output_seconds"]
    calm = [(captions.to_output_time(x["from"], shots), captions.to_output_time(x["to"], shots)) for x in edit["pause_policy"]["protected"]]
    spoken = " ".join(p["text"] for p in phrases).casefold()
    bad, out = [], []
    for m in moments:
        a = [p for p in phrases if p["text"] == m["from"]]
        b = [p for p in phrases if p["text"] == m["to"]]
        if len(a) != 1 or len(b) != 1:
            bad.append(f"phrase not unique or missing: {m['from']} / {m['to']}")
            continue
        start, end = a[0]["out_start"], b[0]["out_end"]
        if any(x < end and start < y for x, y in calm):
            bad.append(f"in protected section: {m['text']} {start:.2f}-{end:.2f}")
        if end > total - END_SECONDS:
            bad.append(f"overlaps the end card: {m['text']}")
        for words in (m["text"], m.get("kicker", "")):
            if words.casefold() not in spoken:
                bad.append(f"not spoken: {words}")
        out.append(dict(m, start=start, end=end))
    out.sort(key=lambda m: m["start"])
    for x, y in zip(out, out[1:]):
        if y["start"] < x["end"] - 1e-6:
            bad.append(f"overlap: {x['text']} and {y['text']}")
    if bad:
        raise SystemExit("\n".join(bad))

    data = {"total": total, "endSeconds": END_SECONDS, "moments": out,
            "hook": effects["hook"]["text"], "endLine": "Neenga yaarukkaavadhu support-aa irundhurukkeengala?",
            "phrases": [{"text": p["text"], "start": p["out_start"], "end": p["out_end"]} for p in phrases]}
    with open(f"{REMOTION}/src/data5.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)

    # sound follows the visible changes: soft air on cards, one hit on 200%, none on plain headlines
    effects["sound"]["cues"] = [{"at": max(m["start"] - 0.08, 0), "kind": "air"} for m in out if m["type"] != "head"]
    effects["sound"]["cues"] += [{"at": m["start"], "kind": "hit"} for m in out if m["text"].startswith("200%")]
    with open(f"{EDIT}/effects.json", "w", encoding="utf-8") as f:
        json.dump(effects, f, ensure_ascii=False, indent=1)
    print(f"ok: {len(out)} text moments")
    for m in out:
        print(f"  {m['type']:6}{m['start']:6.2f}-{m['end']:6.2f}  {m.get('kicker', '')} | {m['text']}")


if __name__ == "__main__":
    main()
