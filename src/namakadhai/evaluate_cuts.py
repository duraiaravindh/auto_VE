"""Measure automatic cuts against mistakes you marked by hand.

Marks file (CSV, one mistake per line; times as seconds or m:ss.s):

    start,end,label
    0:12.4,0:15.0,retake
    0:31.0,0:31.6,filler

A pre-ticked cut counts as **correct** if at least half of it lies inside a
marked mistake, otherwise as a **false cut** (it removes good speech). A marked
mistake is **caught** if a pre-ticked cut covers at least half of it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


def parse_time(value: str) -> float:
    value = value.strip()
    if ":" in value:
        minutes, seconds = value.split(":", 1)
        return int(minutes) * 60 + float(seconds)
    return float(value)


def load_marks(path: Path) -> list[tuple[float, float, str]]:
    marks = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            start, end = parse_time(row["start"]), parse_time(row["end"])
            if end <= start:
                raise ValueError(f"Mark ends before it starts: {row}")
            marks.append((start, end, (row.get("label") or "").strip()))
    return marks


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def evaluate(cuts: list[dict[str, Any]], marks: list[tuple[float, float, str]]) -> dict[str, Any]:
    """``cuts``: pre-ticked suggestions with ``proposed_cut_seconds`` and ``kind``.

    Shortened pauses remove only silence, so they are counted separately and
    never as false cuts.
    """
    pause_cuts = [cut for cut in cuts if cut.get("kind") == "pause"]
    cuts = [cut for cut in cuts if cut.get("kind") != "pause"]
    ranges = [tuple(map(float, cut["proposed_cut_seconds"])) for cut in cuts]
    correct, false_cuts = [], []
    for cut, span in zip(cuts, ranges):
        inside = sum(_overlap(span, (start, end)) for start, end, _ in marks)
        (correct if inside >= 0.5 * (span[1] - span[0]) else false_cuts).append(
            {"kind": cut.get("kind"), "start": span[0], "end": span[1],
             "evidence": cut.get("evidence")}
        )
    caught, missed = [], []
    for start, end, label in marks:
        covered = sum(_overlap((start, end), span) for span in ranges)
        (caught if covered >= 0.5 * (end - start) else missed).append(
            {"label": label, "start": start, "end": end}
        )
    return {
        "marked_mistakes": len(marks), "caught": caught, "missed": missed,
        "pre_ticked_cuts": len(cuts), "correct_cuts": correct, "false_cuts": false_cuts,
        "pauses_shortened": len(pause_cuts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score auto cuts against hand-marked mistakes.")
    parser.add_argument("plan", type=Path, help="story_edit.json produced by auto mode")
    parser.add_argument("marks", type=Path, help="CSV of mistakes you marked (start,end,label)")
    arguments = parser.parse_args(argv)
    plan = json.loads(arguments.plan.read_text(encoding="utf-8"))
    decisions: dict[str, str] = {}
    for line in (arguments.plan.parent / "approvals.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            decisions[event["item_id"]] = event["status"]
    ticked = [c for c in plan["candidates"] if decisions.get(c["candidate_id"]) == "APPROVED"]
    result = evaluate(ticked, load_marks(arguments.marks))
    print(f"Marked mistakes: {result['marked_mistakes']}, caught {len(result['caught'])}, "
          f"missed {len(result['missed'])}")
    print(f"Pre-ticked cuts: {result['pre_ticked_cuts']}, correct {len(result['correct_cuts'])}, "
          f"FALSE CUTS {len(result['false_cuts'])}; pauses shortened {result['pauses_shortened']}")
    for item in result["false_cuts"]:
        print(f"  false cut {item['kind']} {item['start']:.2f}-{item['end']:.2f}s {item['evidence'] or ''}")
    for item in result["missed"]:
        print(f"  missed {item['label']} {item['start']:.2f}-{item['end']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
