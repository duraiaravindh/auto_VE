"""Word-level forced alignment of reviewed caption text to the spoken audio.

Uses the MMS forced-alignment model (torchaudio ``MMS_FA``), which aligns
romanized text in 1,100+ languages, so Tanglish captions are aligned directly
against the speech. Runs in the separate ``.venv-align`` environment because it
needs PyTorch; the rest of the application stays dependency-free.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from namakadhai.subtitle_import import SubtitleImportError, parse_srt

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # the model emits one frame per 20 ms
DIGIT_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _spoken_tokens(word: str) -> list[str]:
    """Lowercase a-z tokens the model can align; digits are spelled out."""
    tokens = []
    for part in re.findall(r"[A-Za-z']+|\d", word):
        if part.isdigit():
            tokens.append(DIGIT_WORDS[part])
        else:
            letters = re.sub(r"[^a-z]", "", part.casefold())
            if letters:
                tokens.append(letters)
    return tokens


def _decode_audio(media_path: Path, ffmpeg_path: Path):
    import torch

    result = subprocess.run(
        [
            str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(media_path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "f32le", "-",
        ],
        capture_output=True, check=False, timeout=600, shell=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise SubtitleImportError("FFmpeg could not decode the audio for alignment.")
    data = bytearray(result.stdout[: len(result.stdout) // 4 * 4])
    return torch.frombuffer(data, dtype=torch.float32).unsqueeze(0)


def _chunked_emission(model, waveform, *, chunk_seconds: float = 30.0, context_seconds: float = 2.0):
    """Model emissions for long audio, computed in overlapping windows.

    The model's attention cost grows with the square of the input length, so a
    whole multi-minute recording would exhaust memory. Each window gets extra
    context on both sides; only its centre frames are kept.
    """
    import torch

    total = waveform.size(1)
    chunk = round(chunk_seconds * SAMPLE_RATE / FRAME_SAMPLES) * FRAME_SAMPLES
    context = round(context_seconds * SAMPLE_RATE / FRAME_SAMPLES) * FRAME_SAMPLES
    total_frames = total // FRAME_SAMPLES
    pieces = []
    with torch.inference_mode():
        for start in range(0, total, chunk):
            left = max(0, start - context)
            right = min(total, start + chunk + context)
            output, _ = model(waveform[:, left:right])
            first = (start - left) // FRAME_SAMPLES
            wanted = min(chunk, total - start) // FRAME_SAMPLES
            piece = output[0, first : first + wanted]
            if piece.size(0) < wanted:  # the model drops a frame at the very end
                piece = torch.cat([piece, piece[-1:].expand(wanted - piece.size(0), -1)])
            pieces.append(piece)
    return torch.cat(pieces)[:total_frames]


def align_words(
    media_path: str | Path,
    words: list[str],
    *,
    ffmpeg_path: str | Path,
    phrase_starts: set[int] | None = None,
) -> list[tuple[float, float, float] | None]:
    """Return (start, end, score) per input word, or None for unalignable words.

    A wildcard ``*`` token is placed at both ends and before each index in
    ``phrase_starts`` so breaths, fillers and words missing from the text are
    absorbed there instead of stretching the neighbouring words.
    """
    return _align_on_emission(
        load_emission(media_path, ffmpeg_path=ffmpeg_path), words, phrase_starts=phrase_starts
    )


def load_emission(media_path: str | Path, *, ffmpeg_path: str | Path):
    """Run the speech model once; alignments against it are then cheap."""
    from torchaudio.pipelines import MMS_FA

    os.environ.setdefault("TORCH_HOME", str(PROJECT_ROOT / "models" / "torch"))
    waveform = _decode_audio(Path(media_path).resolve(), Path(ffmpeg_path).resolve())
    return _chunked_emission(MMS_FA.get_model(), waveform)


def _align_on_emission(
    emission, words: list[str], *, phrase_starts: set[int] | None = None
) -> list[tuple[float, float, float] | None]:
    import torch
    from torchaudio.pipelines import MMS_FA

    tokenizer = MMS_FA.get_tokenizer()
    aligner = MMS_FA.get_aligner()
    token_groups = [_spoken_tokens(word) for word in words]
    if not any(token_groups):
        raise SubtitleImportError("Caption text has no alignable words.")
    phrase_starts = phrase_starts or set()
    flat: list[str] = ["*"]
    positions: list[int] = []
    for index, group in enumerate(token_groups):
        if index in phrase_starts and index > 0:
            flat.append("*")
        positions.append(len(flat))
        flat.extend(group)
    flat.append("*")
    with torch.inference_mode():
        spans = aligner(emission, tokenizer(flat))
    seconds_per_frame = FRAME_SAMPLES / SAMPLE_RATE

    timings: list[tuple[float, float, float] | None] = []
    for group, cursor in zip(token_groups, positions):
        if not group:
            timings.append(None)
            continue
        word_spans = [span for token_spans in spans[cursor : cursor + len(group)] for span in token_spans]
        start = word_spans[0].start * seconds_per_frame
        end = word_spans[-1].end * seconds_per_frame
        score = sum(span.score * len(span) for span in word_spans) / sum(len(span) for span in word_spans)
        timings.append((start, end, float(score)))
    return timings


def select_spoken_cues(
    cue_count: int,
    score_for: Callable[[list[int]], list[float]],
    *,
    threshold: float = 0.3,
) -> list[int]:
    """Indices of caption lines that are really in the audio.

    Text that was cut from the video still gets squeezed into the audio by the
    aligner, with near-zero confidence, and drags neighbouring lines with it.
    So drop only the single worst line per round and re-align, then try each
    dropped line again in case it was only pulled down by a neighbour.
    ``score_for(kept)`` returns the mean word confidence of each kept line.
    """
    kept = list(range(cue_count))
    dropped: list[int] = []
    while kept:
        scores = score_for(kept)
        worst = min(range(len(kept)), key=scores.__getitem__)
        if scores[worst] >= threshold:
            break
        dropped.append(kept.pop(worst))
    for index in sorted(dropped):
        trial = sorted(kept + [index])
        if min(score_for(trial)) >= threshold:
            kept = trial
    return kept


def build_aligned_captions(
    cues: list[dict[str, Any]],
    timings: list[tuple[float, float, float] | None],
    *,
    video_duration_seconds: float,
    lead_seconds: float = 0.15,
    hold_seconds: float = 0.4,
    low_score: float = 0.35,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Turn per-word timings into cues that start on the first spoken word."""
    words_per_cue = [str(cue["text"]).split() for cue in cues]
    cursor = 0
    spoken: list[list[tuple[str, tuple[float, float, float]]]] = []
    present: list[dict[str, Any]] = []
    not_in_audio: list[str] = []
    for cue, cue_words in zip(cues, words_per_cue):
        pairs = []
        for word in cue_words:
            timing = timings[cursor]
            cursor += 1
            if timing is not None:
                pairs.append((word, timing))
        if not pairs:
            not_in_audio.append(str(cue["text"]))
            continue
        spoken.append(pairs)
        present.append(cue)
    if not spoken:
        raise SubtitleImportError("None of the caption lines were found in the audio.")
    cues = present

    aligned: list[dict[str, Any]] = []
    low_confidence: list[str] = []
    for index, (cue, pairs) in enumerate(zip(cues, spoken)):
        first_word_start = pairs[0][1][0]
        last_word_end = pairs[-1][1][1]
        start = max(0.0, first_word_start - lead_seconds)
        if aligned:
            start = max(start, aligned[-1]["end"])
        if index + 1 < len(spoken):
            next_start = max(start + 0.05, spoken[index + 1][0][1][0] - lead_seconds)
            end = min(next_start, last_word_end + hold_seconds)
        else:
            end = min(video_duration_seconds, last_word_end + hold_seconds)
        end = max(end, start + 0.05)
        word_entries = []
        for word, (word_start, word_end, score) in pairs:
            word_start = min(max(word_start, start), end)
            word_end = min(max(word_end, word_start + 0.01), end)
            if word_end <= word_start:
                continue
            word_entries.append(
                {"text": word, "start": round(word_start, 3), "end": round(word_end, 3),
                 "confidence": round(min(1.0, max(0.0, score)), 3)}
            )
            if score < low_score:
                low_confidence.append(f"{word} @ {word_start:.2f}s")
        aligned.append(
            {
                "start": round(start, 3), "end": round(end, 3), "text": cue["text"],
                "highlight": list(cue.get("highlight", [])), "uncertain": False,
                "alignment": "forced_word_alignment", "words": word_entries,
            }
        )
    scores = [timing[2] for timing in timings if timing is not None]
    report = {
        "method": "mms_fa_ctc_forced_alignment",
        "aligned_word_count": len(scores),
        "skipped_word_count": sum(timing is None for timing in timings),
        "mean_word_confidence": round(sum(scores) / len(scores), 3),
        "low_confidence_words": low_confidence,
        "not_in_audio": not_in_audio,
        "text_preserved_exactly": True,
    }
    return aligned, report


def import_forced_aligned_srt(
    source_path: str | Path,
    media_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path,
    video_duration_seconds: float,
    edited_path: str | Path | None = None,
) -> dict[str, Any]:
    """Time SRT words on ``media_path``.

    With ``edited_path`` (an export from OpenReel or any editor), the words are
    timed on the original recording, where the SRT matches exactly, then moved
    onto the edit by matching its audio back to the original. Words from parts
    that were cut out are dropped. ``video_duration_seconds`` is then the
    edited video's duration.
    """
    output = Path(output_path).resolve()
    if output.exists():
        raise SubtitleImportError(f"Refusing to overwrite caption import: {output.name}")
    if not math.isfinite(video_duration_seconds) or video_duration_seconds <= 0:
        raise SubtitleImportError("Video duration must be positive and finite.")
    cues = parse_srt(source_path)
    emission = load_emission(media_path, ffmpeg_path=ffmpeg_path)
    cue_words = [str(cue["text"]).split() for cue in cues]

    def align_subset(kept: list[int]) -> list[tuple[float, float, float] | None]:
        words: list[str] = []
        phrase_starts: set[int] = set()
        for index in kept:
            phrase_starts.add(len(words))
            words.extend(cue_words[index])
        return _align_on_emission(emission, words, phrase_starts=phrase_starts)

    def cue_scores(kept: list[int]) -> list[float]:
        timings, cursor, scores = align_subset(kept), 0, []
        for index in kept:
            found = [t[2] for t in timings[cursor : cursor + len(cue_words[index])] if t]
            cursor += len(cue_words[index])
            scores.append(sum(found) / len(found) if found else 0.0)
        return scores

    kept = select_spoken_cues(len(cues), cue_scores)
    kept_timings = iter(align_subset(kept))
    timings: list[tuple[float, float, float] | None] = []
    for index, words_of_cue in enumerate(cue_words):
        for _ in words_of_cue:
            timings.append(next(kept_timings) if index in kept else None)
    edit_report = None
    if edited_path is not None:
        from namakadhai.edit_matcher import EditMatchError, map_words, match_edit

        try:
            segments = match_edit(
                Path(media_path).resolve(), Path(edited_path).resolve(),
                ffmpeg=Path(ffmpeg_path).resolve(),
            )
        except EditMatchError as exc:
            raise SubtitleImportError(str(exc)) from exc
        moved = map_words([(t[0], t[1]) if t else (-1.0, -1.0) for t in timings], segments)
        edited_cues, edited_timings, cut_lines = [], [], []
        cursor = 0
        for cue, words_of_cue in zip(cues, cue_words):
            kept_words = []
            for word in words_of_cue:
                timing, place = timings[cursor], moved[cursor]
                cursor += 1
                if timing is not None and place is not None:
                    kept_words.append((word, (place[0], place[1], timing[2])))
            if not kept_words:
                cut_lines.append(str(cue["text"]))
                continue
            edited_cues.append({**cue, "text": " ".join(word for word, _ in kept_words)})
            edited_timings.extend(timing for _, timing in kept_words)
        if not edited_cues:
            raise SubtitleImportError("No caption words fall inside the edited video.")
        cues, timings = edited_cues, edited_timings
        covered = sum(segment.edit_end - segment.edit_start for segment in segments)
        edit_report = {
            "edited_media": str(Path(edited_path).resolve()),
            "segments": [segment.__dict__ for segment in segments],
            "matched_seconds": round(covered, 3),
            "lines_cut_in_edit": cut_lines,
        }
    aligned, report = build_aligned_captions(
        cues, timings, video_duration_seconds=video_duration_seconds
    )
    if edit_report is not None:
        report["edit"] = edit_report
    payload = {
        "version": 1, "reviewed": True,
        "source": {
            "file": str(Path(source_path).resolve()),
            "media": str(Path(edited_path or media_path).resolve()),
            "original_media": str(Path(media_path).resolve()),
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
        raise SubtitleImportError("Could not write the forced-aligned caption revision.") from exc
    return payload


def main(argv: list[str] | None = None) -> int:
    from namakadhai.media_review import MediaReviewError, _resolve_ffmpeg, preflight_media

    parser = argparse.ArgumentParser(
        description="Align SRT caption text word-by-word to the video's speech (MMS_FA)."
    )
    parser.add_argument("srt", type=Path, help="Reviewed SRT; text is kept exactly.")
    parser.add_argument("video", type=Path, help="Source video; it is only read.")
    parser.add_argument("output", type=Path, help="New caption JSON (never overwritten).")
    parser.add_argument(
        "--edited", type=Path,
        help="Edited export (e.g. from OpenReel) of VIDEO; captions are produced for it.",
    )
    arguments = parser.parse_args(argv)
    try:
        metadata, _ = preflight_media(arguments.edited or arguments.video)
        if metadata.duration_seconds is None:
            raise SubtitleImportError("FFprobe did not report the video duration.")
        payload = import_forced_aligned_srt(
            arguments.srt, arguments.video, arguments.output,
            ffmpeg_path=_resolve_ffmpeg(None),
            video_duration_seconds=metadata.duration_seconds,
            edited_path=arguments.edited,
        )
    except (SubtitleImportError, MediaReviewError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    report = payload["source"]["alignment"]
    print(f"Captions: {arguments.output.resolve()}")
    print(
        f"Aligned {report['aligned_word_count']} words "
        f"(mean confidence {report['mean_word_confidence']:.2f}); "
        f"{len(report['low_confidence_words'])} low-confidence words."
    )
    if report["not_in_audio"]:
        print("Not in the audio (skipped): " + " | ".join(report["not_in_audio"]))
    if "edit" in report:
        edit = report["edit"]
        print(f"Matched {len(edit['segments'])} kept part(s) of the original in the edit.")
        if edit["lines_cut_in_edit"]:
            print("Cut in the edit (no caption): " + " | ".join(edit["lines_cut_in_edit"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
