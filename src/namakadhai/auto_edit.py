"""Local faster-whisper transcription and conservative automatic edit planning."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol


class AutoEditError(RuntimeError):
    """Automatic planning could not complete safely."""


class LocalModelRequiredError(AutoEditError):
    """The approved local model is not installed yet."""


class TranscriptionProvider(Protocol):
    provider_id: str

    def status(self) -> dict[str, Any]: ...

    def transcribe(self, media: Path, output_directory: Path) -> dict[str, Any]: ...


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
WORD_CLEAN = re.compile(r"[^\w\u0B80-\u0BFF]+", re.UNICODE)
DEFAULT_FILLERS = frozenset(
    {
        "um", "uh", "erm", "hmm", "mmm", "ah", "அம்", "உம்", "ஹ்ம்ம்",
        "அதாவது", "வந்து", "enna", "vandhu", "athavathu",
    }
)


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


def _atomic_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise AutoEditError(f"Refusing to overwrite automatic-edit artifact: {path.name}")
    staged = path.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        with staged.open("x", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        staged.replace(path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise AutoEditError(f"Could not write automatic-edit artifact: {path.name}") from exc


def _model_ready(model_directory: Path) -> bool:
    return model_directory.is_dir() and (model_directory / "config.json").is_file() and any(
        model_directory.glob("model*.bin")
    )


@dataclass(frozen=True)
class FasterWhisperProvider:
    model_name: str
    model_directory: Path
    language: str = "ta"
    confidence_threshold: float = 0.35
    provider_id: str = "faster-whisper"

    def status(self) -> dict[str, Any]:
        dependency = importlib.util.find_spec("faster_whisper") is not None
        model = _model_ready(self.model_directory)
        return {
            "enabled": True,
            "ready": dependency and model,
            "provider": self.provider_id,
            "model": self.model_name,
            "dependency_installed": dependency,
            "model_installed": model,
            "model_directory": str(self.model_directory),
            "downloads_allowed": False,
            "word_timestamps": True,
            "device": "cpu",
            "compute_type": "int8",
            "caption_mode": "tanglish_draft",
        }

    def _load(self):
        status = self.status()
        if not status["dependency_installed"]:
            raise LocalModelRequiredError(
                "faster-whisper is not installed. Run the approved installation command first."
            )
        if not status["model_installed"]:
            raise LocalModelRequiredError(
                f"The local '{self.model_name}' model is missing from {self.model_directory}. "
                "The application will not download it automatically."
            )
        try:
            from faster_whisper import WhisperModel

            return WhisperModel(
                str(self.model_directory),
                device="cpu",
                compute_type="int8",
                local_files_only=True,
            )
        except Exception as exc:
            raise AutoEditError(f"Could not load local faster-whisper model '{self.model_name}'.") from exc

    def transcribe(self, media: Path, output_directory: Path) -> dict[str, Any]:
        model = self._load()
        output_directory.mkdir(parents=True, exist_ok=False)
        try:
            segments_iter, info = model.transcribe(
                str(media.resolve()),
                language=self.language,
                task="transcribe",
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            segments = list(segments_iter)
        except Exception as exc:
            try:
                output_directory.rmdir()
            except OSError:
                pass
            raise AutoEditError(f"Local faster-whisper inference failed for {media.name}.") from exc

        normalized_segments: list[dict[str, Any]] = []
        all_words: list[dict[str, Any]] = []
        for segment_index, segment in enumerate(segments, start=1):
            words: list[dict[str, Any]] = []
            for word in getattr(segment, "words", None) or ():
                original = str(getattr(word, "word", "")).strip()
                if not original:
                    continue
                start = float(getattr(word, "start"))
                end = float(getattr(word, "end"))
                probability = float(getattr(word, "probability", 0.0))
                if not all(math.isfinite(value) for value in (start, end, probability)) or end <= start:
                    continue
                tanglish = transliterate_tamil(original) if TAMIL_RANGE.search(original) else original
                item = {
                    "original": original,
                    "text": tanglish,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "confidence": round(max(0.0, min(1.0, probability)), 4),
                    "verify": probability < self.confidence_threshold,
                }
                words.append(item)
                all_words.append(item)
            original_text = str(getattr(segment, "text", "")).strip()
            normalized_segments.append(
                {
                    "id": segment_index,
                    "start": round(float(getattr(segment, "start")), 3),
                    "end": round(float(getattr(segment, "end")), 3),
                    "original_text": original_text,
                    "tanglish_draft": transliterate_tamil(original_text),
                    "words": words,
                }
            )
        transcript = {
            "version": 1,
            "provider": self.provider_id,
            "model": self.model_name,
            "language": getattr(info, "language", self.language),
            "language_probability": getattr(info, "language_probability", None),
            "caption_mode": "tanglish_draft",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_file": media.name,
            "segments": normalized_segments,
            "review_required": True,
            "word_count": len(all_words),
            "low_confidence_word_count": sum(bool(item["verify"]) for item in all_words),
        }
        captions = make_caption_cues(all_words, confidence_threshold=self.confidence_threshold)
        caption_payload = {
            "version": 1,
            "reviewed": True,
            "review_actor": "auto",
            "review_required_before_publish": True,
            "caption_mode": "tanglish_draft",
            "captions": captions,
        }
        transcript_path = output_directory / "transcript.json"
        captions_path = output_directory / "captions.json"
        report_path = output_directory / "transcription_report.json"
        _atomic_json(transcript_path, transcript)
        _atomic_json(captions_path, caption_payload)
        _atomic_json(
            report_path,
            {
                "version": 1,
                "provider": self.status(),
                "source_file": media.name,
                "word_count": len(all_words),
                "caption_count": len(captions),
                "low_confidence_word_count": transcript["low_confidence_word_count"],
                "omitted_uncertain_caption_count": sum(bool(cue["uncertain"]) for cue in captions),
                "warnings": [
                    "Tanglish is deterministic transliteration, not a verified transcript.",
                    "Cues containing low-confidence words are marked uncertain and omitted from renders until corrected.",
                ],
            },
        )
        return {
            "transcript_path": str(transcript_path),
            "captions_path": str(captions_path),
            "report_path": str(report_path),
            "word_count": len(all_words),
            "caption_count": len(captions),
            "low_confidence_word_count": transcript["low_confidence_word_count"],
        }


def make_caption_cues(
    words: list[dict[str, Any]],
    *,
    confidence_threshold: float = 0.35,
    maximum_duration_seconds: float = 3.2,
    maximum_characters: int = 48,
    pause_seconds: float = 0.55,
) -> list[dict[str, Any]]:
    """Group real word timings into readable cues without inventing timing."""
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        uncertain_words = [item["text"] for item in current if item.get("verify")]
        cues.append(
            {
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": " ".join(str(item["text"]) for item in current),
                "highlight": [],
                "uncertain": bool(uncertain_words),
                "verification_required_words": uncertain_words,
                "words": [
                    {
                        "text": item["text"],
                        "original": item.get("original", item["text"]),
                        "start": item["start"],
                        "end": item["end"],
                        "confidence": item.get("confidence"),
                    }
                    for item in current
                ],
            }
        )
        current.clear()

    for word in words:
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            candidate_text = " ".join(str(item["text"]) for item in (*current, word))
            candidate_duration = float(word["end"]) - float(current[0]["start"])
            if gap >= pause_seconds or candidate_duration > maximum_duration_seconds or len(candidate_text) > maximum_characters:
                flush()
        current.append(word)
        if re.search(r"[.!?…]$", str(word["text"])):
            flush()
    flush()
    return cues


def _normalized_word(value: str) -> str:
    return WORD_CLEAN.sub("", value).casefold()


def suggest_transcript_cuts(
    transcript_path: str | Path,
    *,
    duration_seconds: float,
    fillers: Iterable[str] = DEFAULT_FILLERS,
) -> list[dict[str, Any]]:
    """Suggest short filler/repetition cuts; never infer semantic mistakes."""
    try:
        transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoEditError("Could not read the automatic transcript.") from exc
    words = [word for segment in transcript.get("segments", []) for word in segment.get("words", [])]
    filler_set = {_normalized_word(item) for item in fillers}
    candidates: list[dict[str, Any]] = []

    def add(kind: str, word: dict[str, Any], reason: str) -> None:
        start = max(0.0, float(word["start"]) - 0.02)
        end = min(duration_seconds, float(word["end"]) + 0.02)
        if end <= start or end - start > 1.2:
            return
        identity = hashlib.sha256(f"{kind}:{start:.3f}:{end:.3f}".encode()).hexdigest()[:12]
        candidates.append(
            {
                "candidate_id": f"auto-{kind}-{identity}",
                "source_start_seconds": start,
                "source_end_seconds": end,
                "proposed_cut_seconds": [start, end],
                "reason": reason,
                "evidence": {"word": word.get("text"), "original": word.get("original"), "confidence": word.get("confidence")},
                "suggestion_source": "automatic_transcript_rule",
                "transition_kind": "cut",
            }
        )

    previous: dict[str, Any] | None = None
    for word in words:
        normalized = _normalized_word(str(word.get("text", "")))
        original_normalized = _normalized_word(str(word.get("original", "")))
        if normalized in filler_set or original_normalized in filler_set:
            add("filler", word, "Short configured filler-word candidate; verify meaning before publication.")
        if previous is not None:
            previous_normalized = _normalized_word(str(previous.get("text", "")))
            gap = float(word["start"]) - float(previous["end"])
            if normalized and normalized == previous_normalized and gap <= 0.45:
                add("repeat", previous, "Adjacent repeated-word candidate; may be an intentional emphasis.")
        previous = word
    candidates.sort(key=lambda item: item["proposed_cut_seconds"])
    non_overlapping: list[dict[str, Any]] = []
    for item in candidates:
        if non_overlapping and item["proposed_cut_seconds"][0] < non_overlapping[-1]["proposed_cut_seconds"][1]:
            continue
        non_overlapping.append(item)
    return non_overlapping

