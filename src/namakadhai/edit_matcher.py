"""Find which parts of an original recording survive in an edited export.

An editor such as OpenReel cuts and rearranges the recording but keeps the
voice audio. Matching the edited audio back to the original gives a piecewise
mapping (edited time -> original time), so captions timed once on the original
can be moved onto the edit, and words from removed parts dropped.

Runs in the alignment environment (needs numpy).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 8000
HOP = 80  # 10 ms frames
FRAME = HOP / SAMPLE_RATE


class EditMatchError(RuntimeError):
    """The edited video could not be matched to the original."""


@dataclass(frozen=True)
class Segment:
    """Edited [edit_start, edit_end) plays original [edit_start + offset, ...)."""

    edit_start: float
    edit_end: float
    offset: float

    def to_original(self, edit_time: float) -> float:
        return edit_time + self.offset


def _envelope(media: Path, ffmpeg: Path) -> np.ndarray:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(media),
         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True, check=False, timeout=1800, shell=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise EditMatchError(f"Could not decode audio from {media.name}.")
    samples = np.frombuffer(result.stdout[: len(result.stdout) // 4 * 4], np.float32)
    frames = len(samples) // HOP
    if frames < 200:
        raise EditMatchError(f"{media.name} is too short to match.")
    power = (samples[: frames * HOP].reshape(frames, HOP).astype(np.float64) ** 2).mean(axis=1)
    return np.log10(power + 1e-8)


def match_envelopes(
    original: np.ndarray,
    edited: np.ndarray,
    *,
    window_frames: int = 150,
    hop_frames: int = 50,
    min_quality: float = 0.85,
) -> list[Segment]:
    """Piecewise offsets from 10 ms log-energy envelopes (pure numpy, testable)."""
    if len(edited) < window_frames or len(original) < window_frames:
        raise EditMatchError("Audio is too short to match.")
    size = 1 << int(np.ceil(np.log2(len(original) + window_frames)))
    spectrum = np.fft.rfft(original, size)
    sums = np.concatenate([[0.0], np.cumsum(original)])
    squares = np.concatenate([[0.0], np.cumsum(original ** 2)])
    positions = len(original) - window_frames + 1
    mean = (sums[window_frames : window_frames + positions] - sums[:positions]) / window_frames
    var = (squares[window_frames : window_frames + positions] - squares[:positions]) / window_frames - mean ** 2
    scale = window_frames * np.sqrt(np.maximum(var, 1e-9))

    matches: list[tuple[int, int]] = []  # (edited start frame, offset frames)
    starts = list(range(0, len(edited) - window_frames + 1, hop_frames))
    if starts[-1] != len(edited) - window_frames:
        starts.append(len(edited) - window_frames)
    for start in starts:
        window = edited[start : start + window_frames]
        spread = window.std()
        if spread < 0.05:  # flat audio (silence or a held tone) can't be placed
            continue
        normalized = (window - window.mean()) / spread
        correlation = np.fft.irfft(spectrum * np.conj(np.fft.rfft(normalized, size)), size)[:positions]
        quality = correlation / scale
        best = int(np.argmax(quality))
        if quality[best] >= min_quality:
            matches.append((start, best - start))

    if not matches:
        raise EditMatchError("No part of the edited audio matches the original recording.")
    # Group consecutive windows that agree on the offset.
    groups: list[list[tuple[int, int]]] = [[matches[0]]]
    for start, offset in matches[1:]:
        previous_start, previous_offset = groups[-1][-1]
        if abs(offset - previous_offset) <= 1 and start - previous_start <= hop_frames * 2:
            groups[-1].append((start, offset))
        else:
            groups.append([(start, offset)])
    groups = _drop_outliers(groups)
    spans = []
    for group in groups:
        offset = int(round(np.median([item[1] for item in group])))
        spans.append([group[0][0], group[-1][0] + window_frames, offset])
    # Where two spans overlap or leave a gap, place the cut at the frame where
    # the edited audio stops agreeing with the first offset and starts agreeing
    # with the second.
    for left, right in zip(spans, spans[1:]):
        low, high = min(left[1], right[0]), max(left[1], right[0])
        low = max(low - window_frames // 2, left[0] + 1)
        high = min(high + window_frames // 2, right[1] - 1)
        frames = np.arange(low, high)
        valid = frames[(frames + left[2] >= 0) & (frames + left[2] < len(original))
                       & (frames + right[2] >= 0) & (frames + right[2] < len(original))]
        if len(valid) == 0:
            cut = (low + high) // 2
        else:
            error_left = np.abs(edited[valid] - original[valid + left[2]])
            error_right = np.abs(edited[valid] - original[valid + right[2]])
            totals = np.concatenate([[0.0], np.cumsum(error_left)]) + (
                error_right.sum() - np.concatenate([[0.0], np.cumsum(error_right)])
            )
            cut = int(valid[0] + int(np.argmin(totals)))
        left[1], right[0] = cut, cut
    return [
        Segment(round(start * FRAME, 3), round(end * FRAME, 3), round(offset * FRAME, 3))
        for start, end, offset in spans
        if end > start
    ]


def _drop_outliers(groups: list[list[tuple[int, int]]], *, max_windows: int = 2) -> list[list[tuple[int, int]]]:
    """Drop tiny matches that jump backwards while their neighbours run in order.

    A window straddling a cut can latch onto similar-sounding speech elsewhere.
    Real edits that re-use earlier footage last longer than a couple of windows.
    """
    kept = list(groups)
    changed = True
    while changed:
        changed = False
        for index in range(1, len(kept) - 1):
            group, before, after = kept[index], kept[index - 1], kept[index + 1]
            if len(group) > max_windows:
                continue
            original_start = group[0][0] + group[0][1]
            before_end = before[-1][0] + before[-1][1]
            after_start = after[0][0] + after[0][1]
            if original_start < before_end and after_start >= before_end:
                del kept[index]
                changed = True
                break
    return kept


def match_edit(original: Path, edited: Path, *, ffmpeg: Path) -> list[Segment]:
    return match_envelopes(_envelope(original, ffmpeg), _envelope(edited, ffmpeg))


def map_words(
    words: list[tuple[float, float]], segments: list[Segment]
) -> list[tuple[float, float] | None]:
    """Move (start, end) times from the original onto the edit; None if cut out."""
    mapped: list[tuple[float, float] | None] = []
    for start, end in words:
        found = None
        for segment in segments:
            original_start = segment.to_original(segment.edit_start)
            original_end = segment.to_original(segment.edit_end)
            if original_start <= start < original_end:
                found = (start - segment.offset, min(end, original_end) - segment.offset)
                break
        mapped.append(found)
    return mapped
