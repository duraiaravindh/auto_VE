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

from namakadhai.auto_edit import TAMIL_RANGE, transliterate_tamil
from namakadhai.subtitle_import import parse_srt

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
