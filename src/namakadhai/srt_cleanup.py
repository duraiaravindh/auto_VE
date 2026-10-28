"""Repair common problems in transcription-tool SRT files before captioning.

Fixes what the tools in use actually produce:
- an SRT pasted inside another SRT (cue numbers and "00:00:01,000 --> ..."
  lines appearing as caption text);
- Tamil-script words, which the word aligner cannot read (written in English
  letters instead, so the word still gets a caption);
- empty cues.
The original file is never changed; a cleaned copy is written.
"""

from __future__ import annotations

import re
from pathlib import Path

from namakadhai.subtitle_import import parse_srt

TAMIL_VOWELS = {
    "அ": "a", "ஆ": "aa", "இ": "i", "ஈ": "ee", "உ": "u", "ஊ": "oo",
    "எ": "e", "ஏ": "ae", "ஐ": "ai", "ஒ": "o", "ஓ": "oa", "ஔ": "au",
}
TAMIL_CONSONANTS = {
    "க": "k", "ங": "ng", "ச": "ch", "ஞ": "nj", "ட": "t", "ண": "n",
    "த": "th", "ந": "n", "ப": "p", "ம": "m", "ய": "y", "ர": "r",
    "ல": "l", "வ": "v", "ழ": "zh", "ள": "l", "ற": "r", "ன": "n",
    "ஜ": "j", "ஷ": "sh", "ஸ": "s", "ஹ": "h",
}
VOWEL_SIGNS = {
    "ா": "aa", "ி": "i", "ீ": "ee", "ு": "u", "ூ": "oo", "ெ": "e",
    "ே": "ae", "ை": "ai", "ொ": "o", "ோ": "oa", "ௌ": "au",
}
TAMIL_MARKS = {"ஂ": "m", "ஃ": "akh"}
TAMIL_RANGE = re.compile(r"[\u0B80-\u0BFF]")

def transliterate_tamil(text: str) -> str:
    """Produce a deterministic, review-required Tanglish draft without a network model."""
    result: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in TAMIL_VOWELS:
            result.append(TAMIL_VOWELS[char])
        elif char in TAMIL_CONSONANTS:
            base = TAMIL_CONSONANTS[char]
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if next_char == "்":
                result.append(base)
                index += 1
            elif next_char in VOWEL_SIGNS:
                result.append(base + VOWEL_SIGNS[next_char])
                index += 1
            else:
                result.append(base + "a")
        elif char in TAMIL_MARKS:
            result.append(TAMIL_MARKS[char])
        elif char in VOWEL_SIGNS or char == "்":
            # Invalid/standalone combining marks are preserved for verification.
            result.append(char)
        else:
            result.append(char)
        index += 1
    return "".join(result)


TIMING_LINE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?[,.:]\d{3}\s*-->")
NUMBER_ONLY = re.compile(r"^\s*\d+\s*$")
TAMIL_WORD = re.compile(r"[஀-௿]+")


def clean_srt(source: Path, destination: Path) -> dict:
    """Write a cleaned SRT; return what was changed."""
    cues = parse_srt(source)
    kept, removed, transliterated = [], 0, []
    for cue in cues:
        text = " ".join(
            line for line in str(cue["text"]).split("\n")
            if not TIMING_LINE.match(line) and not NUMBER_ONLY.match(line)
        ).strip()
        text = TIMING_LINE.sub("", text).strip()
        if not text or NUMBER_ONLY.match(text) or "-->" in text:
            removed += 1
            continue
        if TAMIL_RANGE.search(text):
            def swap(match: re.Match) -> str:
                latin = transliterate_tamil(match.group(0))
                transliterated.append(f"{match.group(0)} -> {latin}")
                return latin
            text = TAMIL_WORD.sub(swap, text)
        kept.append(text)
    if not kept:
        raise ValueError("The subtitle file has no caption text.")
    with destination.open("x", encoding="utf-8") as handle:
        for index, text in enumerate(kept, 1):
            # Placeholder times; the aligner times every word from the audio.
            minutes, seconds = divmod(index, 60)
            stamp = f"00:{minutes:02d}:{seconds:02d}"
            handle.write(f"{index}\n{stamp},000 --> {stamp},900\n{text}\n\n")
    return {
        "lines": len(kept),
        "words": sum(len(text.split()) for text in kept),
        "junk_lines_removed": removed,
        "tamil_words_transliterated": transliterated,
    }
