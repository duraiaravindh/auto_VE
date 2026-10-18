"""Local CPU-first Whisper transcription and subtitle file generation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from namakadhai.probe import PROJECT_ROOT

DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
SUPPORTED_MULTILINGUAL_MODELS = frozenset(
    {
        "tiny",
        "base",
        "small",
        "medium",
        "large",
        "large-v1",
        "large-v2",
        "large-v3",
        "large-v3-turbo",
        "turbo",
    }
)
OUTPUT_FILENAMES = (
    "transcript_raw.txt",
    "transcript_review.txt",
    "subtitles.srt",
    "subtitles.vtt",
)


class TranscriptionError(RuntimeError):
    """Base error for transcription and output generation."""


class TranscriptionConfigurationError(TranscriptionError):
    """The transcription settings are invalid or incomplete."""


class TranscriptionDependencyError(TranscriptionError):
    """Whisper or one of its runtime dependencies is unavailable."""


class ModelApprovalRequiredError(TranscriptionError):
    """The selected local Whisper checkpoint is missing."""


class TranscriptionMediaError(TranscriptionError):
    """FFmpeg could not extract usable audio from the selected media."""


class TranscriptionOutputError(TranscriptionError):
    """Whisper returned invalid transcription data."""


class TranscriptionInferenceError(TranscriptionError):
    """Whisper could not load or run the local model."""


class OutputExistsError(TranscriptionError):
    """One or more transcription outputs already exist."""


@dataclass(frozen=True)
class TranscriptionConfig:
    model: str = "base"
    device: str = "cpu"
    language: str | None = "ta"
    task: str = "transcribe"
    word_timestamps: bool = False
    beam_size: int = 5
    download_model_on_first_use: bool = False
    model_directory: Path = PROJECT_ROOT / "models" / "whisper"
    projects_directory: Path = PROJECT_ROOT / "projects"
    temp_directory: Path = PROJECT_ROOT / "temp"
    ffmpeg_executable: Path = (
        PROJECT_ROOT
        / "tools"
        / "ffmpeg-9.0.2-full_build"
        / "bin"
        / "ffmpeg.exe"
    )

    def validate(self) -> None:
        if self.model not in SUPPORTED_MULTILINGUAL_MODELS:
            raise TranscriptionConfigurationError(
                "Choose a supported multilingual Whisper model; English-only "
                "models do not support Tamil transcription."
            )
        if self.device != "cpu":
            raise TranscriptionConfigurationError(
                "This phase supports CPU inference only; set device to 'cpu'."
            )
        if self.task != "transcribe":
            raise TranscriptionConfigurationError(
                "Only task='transcribe' is supported; translation can change "
                "the spoken language and is not Tanglish transliteration."
            )
        if self.language is not None and (
            not isinstance(self.language, str) or not self.language.strip()
        ):
            raise TranscriptionConfigurationError(
                "language must be a language code such as 'ta', or null for "
                "Whisper language detection."
            )
        if not isinstance(self.word_timestamps, bool):
            raise TranscriptionConfigurationError(
                "word_timestamps must be a boolean."
            )
        if (
            isinstance(self.beam_size, bool)
            or not isinstance(self.beam_size, int)
            or self.beam_size < 1
        ):
            raise TranscriptionConfigurationError("beam_size must be a positive integer.")
        if not isinstance(self.download_model_on_first_use, bool):
            raise TranscriptionConfigurationError(
                "download_model_on_first_use must be a boolean."
            )
        if self.download_model_on_first_use:
            raise TranscriptionConfigurationError(
                "Automatic model downloads are disabled. Download a model only "
                "through a separately approved step."
            )


@dataclass(frozen=True)
class TranscriptionOutputs:
    project_directory: Path
    raw_transcript: Path
    review_transcript: Path
    subtitles_srt: Path
    subtitles_vtt: Path


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    outputs: TranscriptionOutputs


class WhisperModel(Protocol):
    def transcribe(self, audio: str, **options: Any) -> dict[str, Any]: ...


ModelLoader = Callable[[str, Path, str], WhisperModel]


def load_transcription_config(
    settings_path: str | Path | None = None,
) -> TranscriptionConfig:
    """Load transcription and local executable paths from project settings."""
    config_path = (
        Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    )
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TranscriptionConfigurationError(
            "Cannot read project settings."
        ) from exc
    except json.JSONDecodeError as exc:
        raise TranscriptionConfigurationError(
            "Project settings contain invalid JSON."
        ) from exc

    root = config_path.resolve().parent.parent
    try:
        transcription_settings = settings["transcription"]
        path_settings = settings["paths"]
        model = transcription_settings["model"]
        device = transcription_settings["device"]
        language = transcription_settings["language"]
        task = transcription_settings.get("task", "transcribe")
        word_timestamps = transcription_settings.get("word_timestamps", False)
        beam_size = transcription_settings.get("beam_size", 5)
        download_on_first_use = transcription_settings["download_model_on_first_use"]
        model_directory = root / path_settings["models"] / "whisper"
        projects_directory = root / path_settings["projects"]
        temp_directory = root / path_settings["temp"]
        ffmpeg_executable = root / path_settings["ffmpeg_executable"]
    except (KeyError, TypeError) as exc:
        raise TranscriptionConfigurationError(
            "Settings must define transcription options and local model, "
            "projects, temp, and FFmpeg paths."
        ) from exc

    if not isinstance(language, (str, type(None))):
        raise TranscriptionConfigurationError(
            "transcription.language must be a language code or null."
        )
    if not all(
        isinstance(value, str) and value
        for value in (model, device, task, path_settings["models"],
                      path_settings["projects"], path_settings["temp"],
                      path_settings["ffmpeg_executable"])
    ):
        raise TranscriptionConfigurationError(
            "Model, device, task, and configured paths must be non-empty strings."
        )

    config = TranscriptionConfig(
        model=model,
        device=device,
        language=language,
        task=task,
        word_timestamps=word_timestamps,
        beam_size=beam_size,
        download_model_on_first_use=download_on_first_use,
        model_directory=model_directory,
        projects_directory=projects_directory,
        temp_directory=temp_directory,
        ffmpeg_executable=ffmpeg_executable,
    )
    config.validate()
    return config


def _load_local_whisper_model(
    model_name: str,
    checkpoint: Path,
    device: str,
) -> WhisperModel:
    try:
        import whisper
    except ImportError as exc:
        raise TranscriptionDependencyError(
            "OpenAI Whisper or one of its dependencies is missing from this "
            "project's .venv. Install the approved requirements before "
            "transcribing."
        ) from exc

    try:
        return whisper.load_model(str(checkpoint), device=device)
    except Exception as exc:
        raise TranscriptionInferenceError(
            f"Could not load the local '{model_name}' Whisper checkpoint."
        ) from exc


def _safe_project_name(source: Path) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip(" .-_")
    if not readable:
        readable = "recording"
    readable = readable[:48].rstrip(" .-_") or "recording"
    source_identity = str(source.resolve()).casefold().encode("utf-8")
    suffix = hashlib.sha256(source_identity).hexdigest()[:10]
    return f"{readable}-{suffix}"


def _format_timestamp(seconds: float, separator: str) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise TranscriptionOutputError(
            "Whisper returned an invalid subtitle timestamp."
        )
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return (
        f"{hours:02}:{minutes:02}:{whole_seconds:02}"
        f"{separator}{milliseconds:03}"
    )


def _wrap_subtitle_text(text: str, line_width: int = 42) -> str:
    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        lines.extend(
            textwrap.wrap(
                paragraph,
                width=line_width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    return "\n".join(lines)


def format_subtitles(segments: list[dict[str, Any]]) -> tuple[str, str]:
    """Format Whisper segments as SRT and WebVTT without rewriting words."""
    srt_blocks: list[str] = []
    vtt_blocks: list[str] = []
    subtitle_index = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise TranscriptionOutputError(
                "Whisper returned a subtitle segment with an invalid structure."
            )
        text = segment.get("text")
        if not isinstance(text, str):
            raise TranscriptionOutputError(
                "Whisper returned a subtitle segment without text."
            )
        text = _wrap_subtitle_text(text)
        if not text:
            continue
        try:
            start = float(segment["start"])
            end = float(segment["end"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise TranscriptionOutputError(
                "Whisper returned a subtitle segment without valid timestamps."
            ) from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise TranscriptionOutputError(
                "Whisper returned an invalid subtitle time range."
            )

        subtitle_index += 1
        srt_blocks.append(
            f"{subtitle_index}\n"
            f"{_format_timestamp(start, ',')} --> {_format_timestamp(end, ',')}\n"
            f"{text}"
        )
        vtt_blocks.append(
            f"{_format_timestamp(start, '.')} --> {_format_timestamp(end, '.')}\n"
            f"{text}"
        )

    srt = "\n\n".join(srt_blocks)
    vtt = "WEBVTT\n\n" + "\n\n".join(vtt_blocks)
    return srt, vtt


def _prepare_outputs(
    source: Path,
    config: TranscriptionConfig,
    project_directory: str | Path | None,
) -> TranscriptionOutputs:
    project = (
        Path(project_directory)
        if project_directory is not None
        else config.projects_directory / _safe_project_name(source)
    )
    outputs = TranscriptionOutputs(
        project_directory=project,
        raw_transcript=project / OUTPUT_FILENAMES[0],
        review_transcript=project / OUTPUT_FILENAMES[1],
        subtitles_srt=project / OUTPUT_FILENAMES[2],
        subtitles_vtt=project / OUTPUT_FILENAMES[3],
    )
    existing = [path.name for path in (
        outputs.raw_transcript,
        outputs.review_transcript,
        outputs.subtitles_srt,
        outputs.subtitles_vtt,
    ) if path.exists()]
    if existing:
        raise OutputExistsError(
            "Transcription output already exists; refusing to overwrite: "
            + ", ".join(existing)
        )
    return outputs


def _extract_audio(
    source: Path,
    config: TranscriptionConfig,
) -> Path:
    if not config.ffmpeg_executable.is_file():
        raise TranscriptionConfigurationError(
            "The configured project-local FFmpeg executable was not found."
        )
    try:
        config.temp_directory.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix="namakadhai-audio-",
            suffix=".wav",
            dir=config.temp_directory,
        )
        os.close(file_descriptor)
    except OSError as exc:
        raise TranscriptionMediaError(
            "Could not prepare a temporary audio file under the project temp directory."
        ) from exc

    audio_path = Path(temp_name)
    command = [
        str(config.ffmpeg_executable),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source.resolve()),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(audio_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        audio_path.unlink(missing_ok=True)
        raise TranscriptionMediaError(
            f"FFmpeg timed out while extracting audio from {source.name}."
        ) from exc
    except OSError as exc:
        audio_path.unlink(missing_ok=True)
        raise TranscriptionMediaError(
            "Could not start the configured project-local FFmpeg executable."
        ) from exc

    if result.returncode != 0 or not audio_path.is_file() or audio_path.stat().st_size == 0:
        audio_path.unlink(missing_ok=True)
        detail = result.stderr.strip()
        if detail:
            detail = detail.replace(str(source.resolve()), source.name)
            detail = detail.replace(str(audio_path.resolve()), audio_path.name)
            detail = detail.replace(str(config.ffmpeg_executable.resolve()), "ffmpeg")
        message = f"FFmpeg could not extract audio from {source.name}."
        if detail:
            message = f"{message} {detail}"
        raise TranscriptionMediaError(message)
    return audio_path


def _write_outputs(
    outputs: TranscriptionOutputs,
    text: str,
    srt: str,
    vtt: str,
) -> None:
    contents = {
        outputs.raw_transcript: text,
        outputs.review_transcript: text,
        outputs.subtitles_srt: srt,
        outputs.subtitles_vtt: vtt,
    }
    created: list[Path] = []
    outputs.project_directory.mkdir(parents=True, exist_ok=True)
    try:
        for path, content in contents.items():
            with path.open("x", encoding="utf-8", newline="") as output_file:
                created.append(path)
                output_file.write(content)
    except FileExistsError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise OutputExistsError(
            "A transcription output appeared during processing; no existing "
            "file was overwritten."
        ) from exc
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise TranscriptionError(
            "Could not write all transcription outputs to the project folder."
        ) from exc


def transcribe_media(
    media_path: str | Path,
    *,
    config: TranscriptionConfig | None = None,
    settings_path: str | Path | None = None,
    project_directory: str | Path | None = None,
    model_loader: ModelLoader | None = None,
) -> TranscriptionResult:
    """Transcribe one local media file and write editable transcript/subtitles.

    FFmpeg extracts a temporary mono 16 kHz WAV using the configured local
    executable. Whisper is only given an existing local model checkpoint path,
    so its model loader cannot fetch weights as a side effect.
    """
    source = Path(media_path).expanduser()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Media file does not exist: {source.name}")
    active_config = config or load_transcription_config(settings_path)
    active_config.validate()
    checkpoint = active_config.model_directory / f"{active_config.model}.pt"
    if not checkpoint.is_file():
        raise ModelApprovalRequiredError(
            f"No local weights were found for Whisper '{active_config.model}'. "
            "Model download requires your explicit approval; no download was started."
        )
    if not active_config.ffmpeg_executable.is_file():
        raise TranscriptionConfigurationError(
            "The configured project-local FFmpeg executable was not found."
        )

    outputs = _prepare_outputs(source, active_config, project_directory)
    loader = model_loader or _load_local_whisper_model
    try:
        model = loader(active_config.model, checkpoint, active_config.device)
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionInferenceError(
            f"Could not load the local '{active_config.model}' Whisper checkpoint."
        ) from exc

    audio_path = _extract_audio(source, active_config)
    try:
        options: dict[str, Any] = {
            "task": active_config.task,
            "fp16": False,
            "word_timestamps": active_config.word_timestamps,
            "beam_size": active_config.beam_size,
            "verbose": False,
        }
        if active_config.language is not None:
            options["language"] = active_config.language
        try:
            transcription = model.transcribe(str(audio_path), **options)
        except Exception as exc:
            raise TranscriptionInferenceError(
                f"Whisper inference failed for {source.name}."
            ) from exc
    finally:
        audio_path.unlink(missing_ok=True)

    if not isinstance(transcription, dict):
        raise TranscriptionOutputError("Whisper returned an invalid result.")
    text = transcription.get("text")
    segments = transcription.get("segments", [])
    if not isinstance(text, str) or not isinstance(segments, list):
        raise TranscriptionOutputError(
            "Whisper returned invalid transcript text or subtitle segments."
        )

    srt, vtt = format_subtitles(segments)
    _write_outputs(outputs, text, srt, vtt)
    return TranscriptionResult(text=text, outputs=outputs)
