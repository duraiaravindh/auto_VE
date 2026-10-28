"""Import reviewed SRT text into the story-caption schema without changing the SRT."""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

STANDARD_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$")
LEGACY_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{3})$")
SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
SILENCE_END = re.compile(
    r"silence_end:\s*(-?\d+(?:\.\d+)?).*?silence_duration:\s*(-?\d+(?:\.\d+)?)"
)
EMBEDDED_TIMING = re.compile(r"^\s*(.+?)\s*-->\s*(.+?)\s*$")


class SubtitleImportError(RuntimeError):
    """The supplied subtitle file could not be imported safely."""


def parse_srt_time(value: str) -> float:
    value = value.strip()
    standard = STANDARD_TIME.fullmatch(value)
    if standard:
        hours, minutes, seconds, milliseconds = map(int, standard.groups())
        if minutes >= 60 or seconds >= 60:
            raise SubtitleImportError(f"Invalid SRT time: {value}")
        return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000
    legacy = LEGACY_TIME.fullmatch(value)
    if legacy:
        minutes, seconds, milliseconds = map(int, legacy.groups())
        if seconds >= 60:
            raise SubtitleImportError(f"Invalid legacy subtitle time: {value}")
        return minutes * 60 + seconds + milliseconds / 1000
    raise SubtitleImportError(f"Unsupported subtitle time: {value}")


def _unwrap_nested_srt(cues: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Recover an SRT accidentally exported as numbered/timed subtitle text.

    Some caption tools turn each original SRT entry into three new cues: one
    containing the original index, one containing the original timing line,
    and one containing the dialogue. Recovery is deliberately strict so normal
    dialogue containing a number or arrow can never be silently rewritten.
    """
    if len(cues) < 3 or len(cues) % 3:
        return None
    recovered: list[dict[str, Any]] = []
    previous_end = 0.0
    for offset in range(0, len(cues), 3):
        number_text = str(cues[offset]["text"]).strip()
        timing_text = str(cues[offset + 1]["text"]).strip()
        dialogue = str(cues[offset + 2]["text"]).strip()
        expected_number = offset // 3 + 1
        timing = EMBEDDED_TIMING.fullmatch(timing_text)
        if number_text != str(expected_number) or timing is None or not dialogue:
            return None
        try:
            start = parse_srt_time(timing.group(1))
            end = parse_srt_time(timing.group(2))
        except SubtitleImportError:
            return None
        if end <= start or start < previous_end - 0.001:
            return None
        recovered.append(
            {
                "start": start,
                "end": end,
                "text": dialogue,
                "source_format": "nested_srt_recovered",
            }
        )
        previous_end = end
    return recovered


def parse_srt(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        content = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SubtitleImportError(f"Could not read subtitle file: {source.name}") from exc
    cues: list[dict[str, Any]] = []
    previous_end = 0.0
    for block_number, block in enumerate(re.split(r"\r?\n\s*\r?\n", content.strip()), start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise SubtitleImportError(f"Subtitle block {block_number} is incomplete.")
        timing_index = 1 if "-->" in lines[1] else 0
        if "-->" not in lines[timing_index]:
            raise SubtitleImportError(f"Subtitle block {block_number} has no timing line.")
        left, right = lines[timing_index].split("-->", 1)
        start = parse_srt_time(left)
        end = parse_srt_time(right)
        text = " ".join(line.strip() for line in lines[timing_index + 1 :] if line.strip())
        if not text or end <= start or start < previous_end - 0.001:
            raise SubtitleImportError(f"Subtitle block {block_number} has invalid text or timing.")
        cues.append({"start": start, "end": end, "text": text})
        previous_end = end
    if not cues:
        raise SubtitleImportError("Subtitle file contains no cues.")
    return _unwrap_nested_srt(cues) or cues


def detect_speech_pauses(
    media_path: str | Path,
    ffmpeg_path: str | Path,
    *,
    threshold_db: float = -32.0,
    minimum_seconds: float = 0.12,
) -> list[tuple[float, float]]:
    """Return merged signal-level pauses without claiming speech recognition."""
    command = [
        str(Path(ffmpeg_path).resolve()), "-hide_banner", "-nostdin", "-i",
        str(Path(media_path).resolve()), "-af",
        f"silencedetect=noise={threshold_db:g}dB:d={minimum_seconds:g}",
        "-f", "null", "NUL",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=300, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubtitleImportError("FFmpeg could not analyze speech pauses.") from exc
    if result.returncode != 0:
        raise SubtitleImportError("FFmpeg pause analysis failed.")
    starts = [float(match.group(1)) for match in SILENCE_START.finditer(result.stderr)]
    ends = [float(match.group(1)) for match in SILENCE_END.finditer(result.stderr)]
    intervals = [(start, end) for start, end in zip(starts, ends) if end > start]
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start - merged[-1][1] <= 0.08:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def align_cues_to_speech_pauses(
    cues: list[dict[str, Any]],
    pauses: list[tuple[float, float]],
    *,
    video_duration_seconds: float,
    maximum_snap_seconds: float = 0.72,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Warp the overall timeline, then snap cue boundaries to nearby pauses."""
    if not cues:
        raise SubtitleImportError("No subtitle cues were supplied for alignment.")
    original_end = float(cues[-1]["end"])
    if original_end <= 0:
        raise SubtitleImportError("Subtitle timeline is invalid.")
    early_pauses = [end for start, end in pauses if start <= 0.5 and end <= 1.5]
    speech_start = max(early_pauses, default=0.0)
    speech_end = max(speech_start + 0.5, video_duration_seconds - 0.2)
    usable_span = speech_end - speech_start
    targets = [
        speech_start + (float(cue["end"]) / original_end) * usable_span
        for cue in cues[:-1]
    ]
    internal_pauses = [
        (start, end) for start, end in pauses
        if end > speech_start and start < speech_end and end - start >= 0.12
    ]
    used: set[int] = set()
    boundaries: list[tuple[float, float, bool]] = []
    previous_next_start = speech_start
    for target in targets:
        choices: list[tuple[float, int, float, float]] = []
        for index, (start, end) in enumerate(internal_pauses):
            if index in used or start <= previous_next_start + 0.25:
                continue
            midpoint = (start + end) / 2
            difference = abs(midpoint - target)
            if difference <= maximum_snap_seconds:
                choices.append((difference - min(0.6, end - start) * 0.08, index, start, end))
        if choices:
            _, index, pause_start, pause_end = min(choices)
            cue_end = pause_start + min(0.06, (pause_end - pause_start) / 3)
            next_start = pause_end - min(0.03, (pause_end - pause_start) / 4)
            if cue_end - previous_next_start >= 0.35:
                used.add(index)
                boundaries.append((cue_end, next_start, True))
                previous_next_start = next_start
                continue
        boundary = max(previous_next_start + 0.35, target)
        boundaries.append((boundary, boundary, False))
        previous_next_start = boundary

    aligned: list[dict[str, Any]] = []
    cue_start = speech_start
    for index, cue in enumerate(cues):
        if index < len(boundaries):
            cue_end, next_start, snapped = boundaries[index]
        else:
            cue_end, next_start, snapped = speech_end, speech_end, False
        cue_end = min(speech_end, max(cue_start + 0.25, cue_end))
        aligned.append(
            {
                "start": round(cue_start, 3), "end": round(cue_end, 3),
                "text": cue["text"], "highlight": [], "uncertain": False,
                "alignment": "speech_pause" if snapped else "timeline_warp",
            }
        )
        cue_start = min(speech_end, max(cue_end, next_start))
    report = {
        "method": "global_timeline_warp_plus_nearby_signal_pause_snapping",
        "speech_start_seconds": speech_start,
        "speech_end_seconds": speech_end,
        "detected_pause_count": len(pauses),
        "snapped_boundary_count": sum(boundary[2] for boundary in boundaries),
        "unsnapped_boundary_count": sum(not boundary[2] for boundary in boundaries),
        "maximum_snap_seconds": maximum_snap_seconds,
        "text_preserved_exactly": True,
        "warning": "Signal-pause alignment is not phoneme-level forced alignment; review lip/speech sync.",
    }
    return aligned, report


def import_pause_aligned_srt(
    source_path: str | Path,
    media_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path,
    video_duration_seconds: float,
) -> dict[str, Any]:
    output = Path(output_path).resolve()
    if output.exists():
        raise SubtitleImportError(f"Refusing to overwrite caption import: {output.name}")
    cues = parse_srt(source_path)
    pauses = detect_speech_pauses(media_path, ffmpeg_path)
    aligned, report = align_cues_to_speech_pauses(
        cues, pauses, video_duration_seconds=video_duration_seconds
    )
    payload = {
        "version": 1, "reviewed": True,
        "source": {
            "file": str(Path(source_path).resolve()),
            "media": str(Path(media_path).resolve()),
            "alignment": report,
        },
        "captions": aligned,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        with staged.open("x", encoding="utf-8", newline="") as caption_file:
            json.dump(payload, caption_file, ensure_ascii=False, indent=2)
            caption_file.write("\n")
        staged.replace(output)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise SubtitleImportError("Could not write the pause-aligned caption revision.") from exc
    return payload


def import_reviewed_srt(
    source_path: str | Path,
    output_path: str | Path,
    *,
    video_duration_seconds: float,
    fit_to_video: bool = False,
    tail_seconds: float = 0.25,
) -> dict[str, Any]:
    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if output.exists():
        raise SubtitleImportError(f"Refusing to overwrite caption import: {output.name}")
    if not math.isfinite(video_duration_seconds) or video_duration_seconds <= 0:
        raise SubtitleImportError("Video duration must be positive and finite.")
    cues = parse_srt(source)
    original_end = float(cues[-1]["end"])
    timing_scale = 1.0
    if original_end > video_duration_seconds:
        if not fit_to_video:
            raise SubtitleImportError(
                f"Subtitles end at {original_end:.3f}s, after the {video_duration_seconds:.3f}s video."
            )
        target_end = max(0.001, video_duration_seconds - max(0.0, tail_seconds))
        timing_scale = target_end / original_end
    imported = []
    for cue in cues:
        imported.append(
            {
                "start": round(float(cue["start"]) * timing_scale, 3),
                "end": round(float(cue["end"]) * timing_scale, 3),
                "text": cue["text"],
                "highlight": [],
                "uncertain": False,
            }
        )
    payload = {
        "version": 1,
        "reviewed": True,
        "source": {
            "file": str(source),
            "timing_format": "SRT or legacy MM:SS:milliseconds",
            "original_last_end_seconds": original_end,
            "video_duration_seconds": video_duration_seconds,
            "timing_scale": timing_scale,
            "fit_to_video": timing_scale != 1.0,
            "text_preserved_exactly": True,
        },
        "captions": imported,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        with staged.open("x", encoding="utf-8", newline="") as caption_file:
            json.dump(payload, caption_file, ensure_ascii=False, indent=2)
            caption_file.write("\n")
        staged.replace(output)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise SubtitleImportError("Could not write the derived caption revision.") from exc
    return payload



FRAME_SECONDS = 0.01
VOWEL_GROUP = re.compile(r"[aeiouy]+")
CAPTION_TOKEN = re.compile(r"\S+")
CAPTION_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "in", "is", "it", "of", "on", "or", "so", "the", "to", "was", "we", "with",
        "aa", "aana", "adhu", "andha", "dhaan", "dhan", "enna", "indha", "la", "le",
        "na", "naa", "nu", "oru", "pa", "paarunga", "um", "vandhu",
    }
)


def measure_speech_envelope(
    media_path: str | Path,
    ffmpeg_path: str | Path,
    *,
    sample_rate: int = 16000,
) -> list[float]:
    """Return the mono signal level in dBFS for each 10 ms frame."""
    command = [
        str(Path(ffmpeg_path).resolve()), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(Path(media_path).resolve()), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "-",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, check=False, timeout=300, shell=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubtitleImportError("FFmpeg could not decode the audio for alignment.") from exc
    if result.returncode != 0 or not result.stdout:
        raise SubtitleImportError("FFmpeg audio decoding for alignment failed.")
    samples = array.array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    hop = max(1, round(sample_rate * FRAME_SECONDS))
    scale = float(hop * 32768 * 32768)
    levels = []
    for offset in range(0, len(samples) - hop + 1, hop):
        energy = sum(value * value for value in samples[offset : offset + hop])
        levels.append(10 * math.log10(energy / scale + 1e-12))
    if not levels:
        raise SubtitleImportError("The media contains no decodable audio.")
    return levels


def detect_speech_activity(
    levels: list[float],
    *,
    minimum_pause_seconds: float = 0.15,
    minimum_speech_seconds: float = 0.08,
    isolated_burst_seconds: float = 0.35,
    isolation_seconds: float = 0.8,
) -> list[bool]:
    """Classify 10 ms frames as speech using an adaptive floor/peak threshold."""
    ordered = sorted(levels)
    floor = ordered[int(len(ordered) * 0.10)]
    peak = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    threshold = floor + 0.5 * (peak - floor)
    radius = 2
    smoothed = []
    for index in range(len(levels)):
        window = levels[max(0, index - radius) : index + radius + 1]
        smoothed.append(sum(window) / len(window))
    active = [value >= threshold for value in smoothed]

    def runs(value: bool) -> list[tuple[int, int]]:
        found, start = [], None
        for index, flag in enumerate(active + [not value]):
            if flag == value and start is None:
                start = index
            elif flag != value and start is not None:
                found.append((start, index))
                start = None
        return found

    for start, end in runs(False):
        short = (end - start) * FRAME_SECONDS < minimum_pause_seconds
        if start > 0 and end < len(active) and short:
            active[start:end] = [True] * (end - start)
    for start, end in runs(True):
        if (end - start) * FRAME_SECONDS < minimum_speech_seconds:
            active[start:end] = [False] * (end - start)
    # Short bursts far from any other speech are clicks or handling noise.
    speech_runs = runs(True)
    for index, (start, end) in enumerate(speech_runs):
        gap_before = start - speech_runs[index - 1][1] if index > 0 else math.inf
        gap_after = speech_runs[index + 1][0] - end if index + 1 < len(speech_runs) else math.inf
        if (
            (end - start) * FRAME_SECONDS < isolated_burst_seconds
            and min(gap_before, gap_after) * FRAME_SECONDS > isolation_seconds
        ):
            active[start:end] = [False] * (end - start)
    return active


def _spoken_weight(text: str) -> float:
    """Approximate spoken length of romanized text from vowel groups and digits."""
    total = 0.0
    for word in text.split():
        letters = re.sub(r"[^a-z0-9]", "", word.casefold())
        digits = sum(character.isdigit() for character in letters)
        total += max(1, len(VOWEL_GROUP.findall(letters)) + 2 * digits) + 0.25
    return max(total, 1.0)


def _short_caption_groups(
    words: list[str], *, maximum_words: int, maximum_characters: int
) -> list[list[str]]:
    """Group words into compact social-video captions without changing order."""
    groups: list[list[str]] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join((*current, word))
        if current and (
            len(current) >= maximum_words or len(candidate) > maximum_characters
        ):
            groups.append(current)
            current = []
        current.append(word)
        if re.search(r"[.!?…]$", word):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _caption_highlight(words: list[str]) -> list[str]:
    """Choose one readable emphasis word; this is styling, not text rewriting."""
    candidates = []
    for word in words:
        normalized = re.sub(r"[^A-Za-z0-9]", "", word).casefold()
        if normalized and normalized not in CAPTION_STOP_WORDS:
            candidates.append((len(normalized), word))
    return [max(candidates)[1]] if candidates else []


def make_short_word_timed_captions(
    cues: list[dict[str, Any]],
    active: list[bool],
    *,
    video_duration_seconds: float,
    maximum_words: int = 5,
    maximum_characters: int = 30,
    minimum_duration_seconds: float = 0.32,
    hold_seconds: float = 0.12,
    highlight_every: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split aligned paragraph cues into short cues with measured speech timing.

    Word positions are estimated over speech-active 10 ms frames inside each
    already aligned cue. This is a deterministic fallback when a forced aligner
    is unavailable; the report labels the result honestly for creator review.
    """
    if maximum_words < 1 or maximum_words > 8:
        raise SubtitleImportError("maximum_words must be between 1 and 8.")
    if maximum_characters < 8 or maximum_characters > 48:
        raise SubtitleImportError("maximum_characters must be between 8 and 48.")
    output: list[dict[str, Any]] = []
    estimated_words = 0
    for source_index, cue in enumerate(cues):
        words = CAPTION_TOKEN.findall(str(cue["text"]))
        if not words:
            continue
        cue_start = max(0.0, float(cue["start"]))
        cue_end = min(video_duration_seconds, float(cue["end"]))
        if cue_end <= cue_start:
            continue
        first_frame = max(0, math.floor(cue_start / FRAME_SECONDS))
        last_frame = min(len(active), math.ceil(cue_end / FRAME_SECONDS))
        speech_frames = [
            frame for frame in range(first_frame, last_frame)
            if active[frame]
        ]
        if not speech_frames:
            speech_frames = list(range(first_frame, max(first_frame + 1, last_frame)))

        weights = [_spoken_weight(word) for word in words]
        cumulative = [0.0]
        for weight in weights:
            cumulative.append(cumulative[-1] + weight)

        def boundary(weight: float) -> float:
            fraction = 0.0 if cumulative[-1] <= 0 else weight / cumulative[-1]
            position = min(
                len(speech_frames) - 1,
                max(0, round(fraction * (len(speech_frames) - 1))),
            )
            return min(cue_end, max(cue_start, speech_frames[position] * FRAME_SECONDS))

        word_entries = []
        for index, word in enumerate(words):
            start = boundary(cumulative[index])
            end = boundary(cumulative[index + 1])
            if index + 1 == len(words):
                end = max(end, min(cue_end, end + hold_seconds))
            end = min(cue_end, max(start + 0.01, end))
            word_entries.append(
                {"text": word, "start": round(start, 3), "end": round(end, 3),
                 "confidence": None}
            )
        estimated_words += len(word_entries)

        groups = _short_caption_groups(
            words, maximum_words=maximum_words,
            maximum_characters=maximum_characters,
        )
        cursor = 0
        for group in groups:
            group_words = word_entries[cursor : cursor + len(group)]
            cursor += len(group)
            start = group_words[0]["start"]
            end = min(cue_end, max(group_words[-1]["end"] + hold_seconds, start + minimum_duration_seconds))
            if output and start < output[-1]["end"]:
                output[-1]["end"] = round(max(output[-1]["start"] + 0.05, start), 3)
            highlight = (
                _caption_highlight(group)
                if highlight_every > 0 and len(output) % highlight_every == 1
                else []
            )
            output.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": " ".join(group),
                    "highlight": highlight,
                    "uncertain": False,
                    "alignment": "speech_activity_word_estimate",
                    "source_cue_index": source_index,
                    "words": group_words,
                }
            )
    for previous, current in zip(output, output[1:]):
        if previous["end"] > current["start"]:
            previous["end"] = current["start"]
        previous["words"][-1]["end"] = min(previous["words"][-1]["end"], previous["end"])
    if not output:
        raise SubtitleImportError("No short captions could be created from the aligned text.")
    return output, {
        "method": "speech_activity_weighted_word_estimate",
        "caption_count": len(output),
        "estimated_word_count": estimated_words,
        "maximum_words": maximum_words,
        "maximum_characters": maximum_characters,
        "review_required": True,
        "warning": "Word timing is estimated from signal-level speech activity; review against speech.",
    }


def align_cues_to_speech_activity(
    cues: list[dict[str, Any]],
    active: list[bool],
    *,
    video_duration_seconds: float,
    levels: list[float] | None = None,
    search_seconds: float = 2.0,
    minimum_cue_seconds: float = 0.5,
    hold_seconds: float = 0.2,
    lead_seconds: float = 0.08,
    merge_gap_seconds: float = 0.3,
    maximum_syllables_per_second: float = 12.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Place cue text on measured speech time and put cue changes in real pauses.

    Each cue's share of the speech is estimated from its spoken length, so long
    silences (intros, breaths) never consume caption time. A dynamic program
    then chooses one boundary per cue change, preferring detected pauses close
    to the estimate and keeping every cue at least ``minimum_cue_seconds`` long.
    """
    if not cues:
        raise SubtitleImportError("No subtitle cues were supplied for alignment.")
    speech_frames = [index for index, flag in enumerate(active) if flag]
    if not speech_frames:
        raise SubtitleImportError("No speech was detected in the media audio.")
    first, last = speech_frames[0], speech_frames[-1] + 1
    speech_start = max(0.0, first * FRAME_SECONDS - lead_seconds)
    speech_end = min(video_duration_seconds, last * FRAME_SECONDS + 0.35)

    cumulative = [0]
    for flag in active:
        cumulative.append(cumulative[-1] + flag)
    total_speech = cumulative[-1]

    def time_at_speech(amount: float) -> float:
        low, high = first, last
        while low < high:
            middle = (low + high) // 2
            if cumulative[middle] < amount:
                low = middle + 1
            else:
                high = middle
        return low * FRAME_SECONDS

    pauses: list[tuple[float, float]] = []
    pause_start_frame = None
    for index in range(first, last + 1):
        flag = active[index] if index < last else True
        if not flag and pause_start_frame is None:
            pause_start_frame = index
        elif flag and pause_start_frame is not None:
            pauses.append((pause_start_frame * FRAME_SECONDS, index * FRAME_SECONDS))
            pause_start_frame = None

    if levels:
        ordered = sorted(levels)
        floor = ordered[int(len(ordered) * 0.10)]
        span = max(1.0, ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] - floor)
        loudness = []
        for index in range(len(levels)):
            window = levels[max(0, index - 3) : index + 4]
            loudness.append(min(1.0, max(0.0, (sum(window) / len(window) - floor) / span)))
    else:
        loudness = [1.0 if flag else 0.0 for flag in active]

    weights = [_spoken_weight(str(cue["text"])) for cue in cues]
    total_weight = sum(weights)
    targets, running = [], 0.0
    for weight in weights[:-1]:
        running += weight
        targets.append(time_at_speech(total_speech * running / total_weight))

    # Each candidate is (start, end, cost): a detected pause, or a point in speech.
    candidate_sets: list[list[tuple[float, float, float]]] = []
    for target in targets:
        options = []
        for pause_start, pause_end in pauses:
            if pause_end < target - search_seconds or pause_start > target + search_seconds:
                continue
            distance = max(0.0, pause_start - target, target - pause_end)
            length_bonus = 2.0 * min(pause_end - pause_start, 0.5) / 0.5
            options.append((pause_start, pause_end, (distance / 0.6) ** 2 - length_bonus))
        for step in range(-20, 21):
            point = target + step * 0.03
            frame = min(len(loudness) - 1, max(0, round(point / FRAME_SECONDS)))
            # Inside speech, the quietest moment between words is the best cut.
            options.append(
                (point, point, 1.0 + 3.0 * loudness[frame] + (abs(point - target) / 0.35) ** 2)
            )
        candidate_sets.append(options)

    # Nobody speaks faster than ~12 syllables per second; keep cues readable.
    minimum_durations = [
        max(minimum_cue_seconds, weight / maximum_syllables_per_second) for weight in weights
    ]
    best: list[list[float]] = []
    back: list[list[int]] = []
    for k, options in enumerate(candidate_sets):
        row, pointers = [], []
        for option_start, _, cost in options:
            if k == 0:
                feasible = option_start - speech_start >= minimum_durations[0]
                row.append(cost if feasible else math.inf)
                pointers.append(-1)
                continue
            choice, choice_cost = -1, math.inf
            for j, (_, previous_end, _) in enumerate(candidate_sets[k - 1]):
                if (
                    option_start - previous_end >= minimum_durations[k]
                    and best[k - 1][j] < choice_cost
                ):
                    choice, choice_cost = j, best[k - 1][j]
            row.append(choice_cost + cost)
            pointers.append(choice)
        best.append(row)
        back.append(pointers)

    chosen: list[tuple[float, float, bool]] = []
    if candidate_sets:
        final_costs = [
            cost if speech_end - candidate_sets[-1][j][1] >= minimum_durations[-1] else math.inf
            for j, cost in enumerate(best[-1])
        ]
        index = min(range(len(final_costs)), key=final_costs.__getitem__)
        if math.isinf(final_costs[index]):
            raise SubtitleImportError("Too many cues for the detected speech duration.")
        for k in range(len(candidate_sets) - 1, -1, -1):
            option_start, option_end, _ = candidate_sets[k][index]
            chosen.append((option_start, option_end, option_end > option_start))
            index = back[k][index]
        chosen.reverse()

    aligned: list[dict[str, Any]] = []
    cue_start = speech_start
    for index, cue in enumerate(cues):
        if index < len(chosen):
            pause_start, pause_end, in_pause = chosen[index]
            if in_pause:
                cue_end = min(pause_start + hold_seconds, pause_end)
                next_start = max(pause_end - lead_seconds, cue_end)
                if next_start - cue_end < merge_gap_seconds:
                    cue_end = next_start
            else:
                cue_end = next_start = pause_start
        else:
            cue_end, next_start, in_pause = speech_end, speech_end, True
        aligned.append(
            {
                "start": round(cue_start, 3), "end": round(cue_end, 3),
                "text": cue["text"], "highlight": list(cue.get("highlight", [])),
                "uncertain": False,
                "alignment": "speech_pause" if in_pause else "speech_estimate",
            }
        )
        cue_start = next_start
    report = {
        "method": "spoken_length_on_measured_speech_time_with_pause_dynamic_programming",
        "speech_start_seconds": round(speech_start, 3),
        "speech_end_seconds": round(speech_end, 3),
        "measured_speech_seconds": round(total_speech * FRAME_SECONDS, 3),
        "detected_pause_count": len(pauses),
        "pause_boundary_count": sum(item[2] for item in chosen),
        "estimated_boundary_count": sum(not item[2] for item in chosen),
        "text_preserved_exactly": True,
        "warning": "Signal-level alignment, not phoneme-level forced alignment; review sync.",
    }
    return aligned, report


def import_speech_aligned_srt(
    source_path: str | Path,
    media_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path,
    video_duration_seconds: float,
) -> dict[str, Any]:
    """Import SRT text, re-timing every cue against the media's measured speech."""
    output = Path(output_path).resolve()
    if output.exists():
        raise SubtitleImportError(f"Refusing to overwrite caption import: {output.name}")
    if not math.isfinite(video_duration_seconds) or video_duration_seconds <= 0:
        raise SubtitleImportError("Video duration must be positive and finite.")
    cues = parse_srt(source_path)
    levels = measure_speech_envelope(media_path, ffmpeg_path)
    aligned, report = align_cues_to_speech_activity(
        cues, detect_speech_activity(levels),
        video_duration_seconds=video_duration_seconds, levels=levels,
    )
    payload = {
        "version": 1, "reviewed": True,
        "source": {
            "file": str(Path(source_path).resolve()),
            "media": str(Path(media_path).resolve()),
            "video_duration_seconds": video_duration_seconds,
            "alignment": report,
        },
        "captions": aligned,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        with staged.open("x", encoding="utf-8", newline="") as caption_file:
            json.dump(payload, caption_file, ensure_ascii=False, indent=2)
            caption_file.write("\n")
        staged.replace(output)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise SubtitleImportError("Could not write the speech-aligned caption revision.") from exc
    return payload


def main(argv: list[str] | None = None) -> int:
    from namakadhai.media_review import MediaReviewError, _resolve_ffmpeg, preflight_media

    parser = argparse.ArgumentParser(
        description="Import SRT text and align its timing to the video's speech."
    )
    parser.add_argument("srt", type=Path, help="Reviewed SRT; text is kept exactly.")
    parser.add_argument("video", type=Path, help="Source video; it is only read.")
    parser.add_argument("output", type=Path, help="New caption JSON (never overwritten).")
    arguments = parser.parse_args(argv)
    try:
        metadata, _ = preflight_media(arguments.video)
        if metadata.duration_seconds is None:
            raise SubtitleImportError("FFprobe did not report the video duration.")
        payload = import_speech_aligned_srt(
            arguments.srt, arguments.video, arguments.output,
            ffmpeg_path=_resolve_ffmpeg(None),
            video_duration_seconds=metadata.duration_seconds,
        )
    except (SubtitleImportError, MediaReviewError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    report = payload["source"]["alignment"]
    print(f"Captions: {arguments.output.resolve()}")
    print(
        f"Speech {report['speech_start_seconds']:.2f}s-{report['speech_end_seconds']:.2f}s; "
        f"{report['pause_boundary_count']} cue changes in pauses, "
        f"{report['estimated_boundary_count']} estimated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
