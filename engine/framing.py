"""Static per-shot crops centred on the median face. No moving camera, ever."""
import statistics

from common import snap


def build_shots(ranges, cues, zoom_by_cue, fps, zoom_splits=()):
    """Split kept ranges where the framing changes, midway through the gap between cues.
    zoom_splits add a hard punch-in inside a cue: from "at" until that cue's framing ends."""
    zooms = [zoom_by_cue[str(c["id"])] for c in cues]
    switches = [snap((cues[i - 1]["end"] + cues[i]["start"]) / 2, fps)
                for i in range(1, len(cues)) if zooms[i] != zooms[i - 1]]
    switches = sorted(set(switches + [snap(split["at"], fps) for split in zoom_splits]))

    def zoom_at(t):
        z = zooms[0]
        for i in range(1, len(cues)):
            if t >= (cues[i - 1]["end"] + cues[i]["start"]) / 2:
                z = zooms[i]
        for split in zoom_splits:
            cue = next(c for c in cues if c["start"] <= split["at"] <= c["end"])
            i = cues.index(cue)
            limit = (cue["end"] + cues[i + 1]["start"]) / 2 if i + 1 < len(cues) else cue["end"]
            if split["at"] <= t < limit:
                z = split["zoom"]
        return z

    shots = []
    for a, b in ranges:
        points = [a] + [s for s in switches if a < s < b] + [b]
        for x, y in zip(points, points[1:]):
            zoom = zoom_at((x + y) / 2)
            if shots and shots[-1]["out"] == x and shots[-1]["zoom"] == zoom:
                shots[-1]["out"] = y  # same framing, continuous: one shot, no crop jump
            else:
                shots.append({"in": x, "out": y, "zoom": zoom})
    return shots


def check_max_zoom(shots, limit=1.20):
    over = [f"{s['in']:.2f}-{s['out']:.2f} at {s['zoom']}" for s in shots if s["zoom"] > limit]
    if over:
        raise SystemExit(f"Zoom over {limit}: " + "; ".join(over))
    return f"max zoom {max(s['zoom'] for s in shots)} <= {limit} across {len(shots)} shots"


def place_crops(shots, faces, src_w, src_h, out_w, out_h, face_y=0.40, caption_top=1215):
    """Adds crop rectangle + face checks to each shot; flags anything out of bounds."""
    for shot in shots:
        z = shot["zoom"]
        crop_w, crop_h = src_w / z, src_h / z
        boxes = [f["box"] for f in faces if shot["in"] <= f["t"] <= shot["out"] and f["box"]]
        samples = [f for f in faces if shot["in"] <= f["t"] <= shot["out"]]
        shot["face_samples"] = len(samples)
        shot["face_found"] = len(boxes)
        if boxes:
            cx = statistics.median(b[0] + b[2] / 2 for b in boxes)
            cy = statistics.median(b[1] + b[3] / 2 for b in boxes)
        else:
            cx, cy = src_w / 2, src_h * face_y
        x = min(max(cx - crop_w / 2, 0), src_w - crop_w)
        y = min(max(cy - crop_h * face_y, 0), src_h - crop_h)
        # FFmpeg crops to whole pixels; keep the numbers honest
        shot["crop"] = [round(crop_w), round(crop_h), round(x), round(y)]
        scale = out_h / crop_h
        flags = []
        for b in boxes:
            if b[0] < x or b[0] + b[2] > x + crop_w or b[1] < y or b[1] + b[3] > y + crop_h:
                flags.append("face box touches crop edge")
            if (b[1] + b[3] - y) * scale > caption_top:
                flags.append("chin reaches caption zone")
        if samples and len(boxes) < len(samples):
            flags.append(f"no face in {len(samples) - len(boxes)}/{len(samples)} samples")
        shot["flags"] = sorted(set(flags))
    return shots
