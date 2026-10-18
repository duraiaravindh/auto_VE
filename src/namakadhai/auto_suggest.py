"""Suggest retake, repeat and filler cuts from timed words (no model, no network).

Works on any word list with timings: a Whisper transcript or a word-timed SRT.
Every suggestion carries a confidence; only "high" ones are pre-ticked, and all
of them stay reviewable. Pause shortening comes from the existing silence
candidates (they keep padding on both sides, so pauses shrink, not vanish).
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

PURE_FILLERS = frozenset({"um", "umm", "uh", "uhh", "erm", "hmm", "hmmm", "mmm", "ah", "aah"})
# Tamil/Tanglish words that are often fillers but also real words: never pre-ticked.
SOMETIMES_FILLERS = frozenset({"vandhu", "athavathu", "adhavadhu", "enna", "apram", "so"})


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


def _norm(text: str) -> str:
    """Spelling-tolerant key: Tanglish spellings of one sound collapse together."""
    value = re.sub(r"[^a-z0-9஀-௿]", "", text.casefold())
    for long, short in (("aa", "a"), ("ee", "i"), ("oo", "u"), ("dh", "d"), ("th", "t"), ("zh", "l")):
        value = value.replace(long, short)
    return re.sub(r"(.)\1+", r"\1", value)


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio() if a and b else 0.0


def _same_words(first: list[Word], second: list[Word]) -> bool:
    """Same word sequence, tolerating Tanglish spelling differences per word."""
    return len(first) == len(second) and all(
        _similar(_norm(a.text), _norm(b.text)) >= 0.8 for a, b in zip(first, second)
    )


def _candidate(kind: str, start: float, end: float, confidence: str, reason: str,
               evidence: dict[str, Any]) -> dict[str, Any]:
    identity = hashlib.sha256(f"{kind}:{start:.3f}:{end:.3f}".encode()).hexdigest()[:12]
    return {
        "candidate_id": f"auto-{kind}-{identity}",
        "kind": kind,
        "source_start_seconds": round(start, 3),
        "source_end_seconds": round(end, 3),
        "proposed_cut_seconds": [round(start, 3), round(end, 3)],
        "confidence": confidence,
        "default_decision": "APPROVED" if confidence == "high" else "REJECTED",
        "reason": reason,
        "evidence": evidence,
        "suggestion_source": "auto_suggest",
        "transition_kind": "cut",
    }


def find_retakes(lines: list[list[Word]], *, lookahead: int = 3) -> list[dict[str, Any]]:
    """An earlier line re-spoken by one of the next few lines is a retake; keep the last."""
    found = []
    for index, line in enumerate(lines):
        if not line:
            continue
        key = _norm(" ".join(word.text for word in line))
        if len(key) < 6:
            continue
        for later in lines[index + 1 : index + 1 + lookahead]:
            if not later:
                continue
            later_key = _norm(" ".join(word.text for word in later))
            whole = _similar(key, later_key)
            # A false start is the beginning of the next line.
            prefix = _similar(key, later_key[: len(key)]) if len(key) < len(later_key) else 0.0
            score = max(whole, prefix)
            gap = later[0].start - line[-1].end
            if score >= 0.75 and gap < 6.0:
                # Parallel phrasing ("oru padam ..." / "innoru padam ...") differs by a
                # whole word; only the same words, or a word-for-word false start,
                # are confident retakes.
                same = _same_words(line, later[: len(line)])
                confidence = "high" if same and gap < 3.0 else "medium"
                found.append(_candidate(
                    "retake", line[0].start, later[0].start, confidence,
                    "Said again right after; keeping the later take.",
                    {"earlier": " ".join(w.text for w in line),
                     "later": " ".join(w.text for w in later),
                     "similarity": round(score, 2), "gap_seconds": round(gap, 2)},
                ))
                break
    return found


def find_repeats(words: list[Word], *, max_gap: float = 0.6) -> list[dict[str, Any]]:
    """Back-to-back repeated words or phrases; single words are often deliberate in Tamil."""
    found = []
    keys = [_norm(word.text) for word in words]
    index = 0
    while index < len(words):
        for size in (3, 2, 1):
            first = keys[index : index + size]
            second = keys[index + size : index + 2 * size]
            if len(second) < size or not all(first) or first != second:
                continue
            gap = words[index + size].start - words[index + size - 1].end
            if gap > max_gap:
                continue
            confidence = {3: "high", 2: "medium", 1: "low"}[size]
            phrase = " ".join(word.text for word in words[index : index + size])
            found.append(_candidate(
                "repeat", words[index].start, words[index + size].start, confidence,
                "Repeated back to back" + (
                    "; single-word doubling is often deliberate in Tamil." if size == 1 else "."
                ),
                {"phrase": phrase, "words": size, "gap_seconds": round(gap, 2)},
            ))
            index += size
            break
        else:
            index += 1
    return found


def find_fillers(words: list[Word], *, extra: Iterable[str] = ()) -> list[dict[str, Any]]:
    found = []
    sometimes = {_norm(item) for item in (*SOMETIMES_FILLERS, *extra)}
    for word in words:
        key = re.sub(r"[^a-z]", "", word.text.casefold())
        if key in PURE_FILLERS:
            confidence = "high"
        elif _norm(word.text) in sometimes:
            confidence = "low"
        else:
            continue
        if word.end - word.start > 1.2:
            continue
        found.append(_candidate(
            "filler", word.start, word.end, confidence,
            "Filler sound." if confidence == "high" else
            "Often a filler, but also a real word: check before cutting.",
            {"word": word.text},
        ))
    return found


def find_pauses(
    words: list[Word], *, minimum: float = 1.0, keep_after: float = 0.25, keep_before: float = 0.2,
) -> list[dict[str, Any]]:
    """Long gaps between spoken words, shortened to a natural ~0.45 s (never removed).

    Uses word timings rather than a loudness threshold, so room noise can't
    hide a pause.
    """
    found = []
    for previous, following in zip(words, words[1:]):
        gap = following.start - previous.end
        if gap < minimum:
            continue
        found.append(_candidate(
            "pause", previous.end + keep_after, following.start - keep_before,
            "high" if gap >= 1.2 else "medium",
            f"Long pause ({gap:.1f}s) shortened to a natural gap.",
            {"after": previous.text, "before": following.text, "gap_seconds": round(gap, 2)},
        ))
    return found


def suggest_cuts(lines: list[list[Word]], *, extra_fillers: Iterable[str] = ()) -> list[dict[str, Any]]:
    """All suggestions, non-overlapping, preferring retakes > repeats > fillers > pauses."""
    words = [word for line in lines for word in line]
    ordered = (find_retakes(lines) + find_repeats(words)
               + find_fillers(words, extra=extra_fillers) + find_pauses(words))
    kept: list[dict[str, Any]] = []
    for item in ordered:
        start, end = item["proposed_cut_seconds"]
        if end - start < 0.05:
            continue
        if any(start < other["proposed_cut_seconds"][1] and end > other["proposed_cut_seconds"][0]
               for other in kept):
            continue
        kept.append(item)
    return sorted(kept, key=lambda item: item["proposed_cut_seconds"][0])
