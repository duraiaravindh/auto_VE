"""Read-only video metadata analysis using a project-local FFprobe executable."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_FFPROBE_PATH = (
    Path("tools")
    / "ffmpeg-9.0.2-full_build"
    / "bin"
    / "ffprobe.exe"
)
DEFAULT_TIMEOUT_SECONDS = 30.0


class ProbeError(RuntimeError):
    """Base error for an FFprobe analysis failure."""


class ProbeConfigurationError(ProbeError):
    """The FFprobe executable could not be resolved from project settings."""


class ProbeExecutionError(ProbeError):
    """FFprobe could not run or reported an error."""


class ProbeTimeoutError(ProbeExecutionError):
    """FFprobe exceeded the configured time limit."""


class ProbeOutputError(ProbeError):
    """FFprobe output was malformed or did not describe a video stream."""


@dataclass(frozen=True)
class VideoStreamMetadata:
    codec: str | None
    width: int | None
    height: int | None
    frame_rate: float | None
    duration_seconds: float | None = None
    start_time_seconds: float | None = None


@dataclass(frozen=True)
class AudioStreamMetadata:
    codec: str | None
    sample_rate_hz: int | None
    channels: int | None
    duration_seconds: float | None = None
    start_time_seconds: float | None = None


@dataclass(frozen=True)
class VideoMetadata:
    file_name: str
    file_size_bytes: int
    duration_seconds: float | None
    format_name: str | None
    format_long_name: str | None
    video: VideoStreamMetadata
    audio: AudioStreamMetadata | None

    def to_dict(self) -> dict[str, Any]:
        """Return report data without exposing the source's absolute path."""
        return asdict(self)


def _resolve_ffprobe(
    ffprobe_path: str | Path | None,
    settings_path: str | Path | None,
) -> Path:
    config_file = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    project_root = config_file.resolve().parent.parent

    if ffprobe_path is None:
        try:
            settings = json.loads(config_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProbeConfigurationError(
                "Cannot read project settings; configure paths.ffprobe_executable "
                "or provide ffprobe_path."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProbeConfigurationError(
                "Project settings contain invalid JSON."
            ) from exc

        try:
            configured_path = settings["paths"]["ffprobe_executable"]
        except (KeyError, TypeError) as exc:
            configured_path = str(DEFAULT_FFPROBE_PATH)

        if not isinstance(configured_path, str) or not configured_path.strip():
            raise ProbeConfigurationError(
                "paths.ffprobe_executable must be a non-empty path."
            )
        candidate = Path(configured_path)
    else:
        candidate = Path(ffprobe_path)

    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _parse_rate(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return rate if rate > 0 else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_duration(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sanitized_detail(stderr: str, source: Path, executable: Path) -> str:
    detail = stderr.strip()
    if not detail:
        return ""
    for sensitive_path in (str(source), str(executable)):
        detail = detail.replace(sensitive_path, source.name)
    return detail


def probe_video(
    video_path: str | Path,
    *,
    ffprobe_path: str | Path | None = None,
    settings_path: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> VideoMetadata:
    """Read technical metadata from a video without modifying the source file.

    The FFprobe executable is resolved from ``ffprobe_path`` or the project's
    ``paths.ffprobe_executable`` setting. Executable lookup never uses PATH.
    """
    source = Path(video_path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Video file does not exist: {source.name}")
    if not source.is_file():
        raise ProbeError(f"Video input is not a regular file: {source.name}")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero.")

    executable = _resolve_ffprobe(ffprobe_path, settings_path)
    command = [
        str(executable),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source.resolve()),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeTimeoutError(
            f"FFprobe timed out while analyzing {source.name} "
            f"after {timeout:g} seconds."
        ) from exc
    except OSError as exc:
        raise ProbeExecutionError(
            "Could not start the configured FFprobe executable. "
            "Check paths.ffprobe_executable in config/settings.json."
        ) from exc

    if result.returncode != 0:
        detail = _sanitized_detail(result.stderr, source.resolve(), executable)
        message = f"FFprobe failed to analyze {source.name} (exit code {result.returncode})."
        if detail:
            message = f"{message} {detail}"
        raise ProbeExecutionError(message)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeOutputError("FFprobe returned malformed JSON.") from exc

    if not isinstance(payload, dict):
        raise ProbeOutputError("FFprobe JSON must contain an object.")

    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise ProbeOutputError("FFprobe JSON contains an invalid streams list.")

    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise ProbeOutputError(f"No video stream was found in {source.name}.")

    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    format_data = payload.get("format")
    if not isinstance(format_data, dict):
        format_data = {}

    duration = _optional_duration(format_data.get("duration"))

    frame_rate = _parse_rate(
        video_stream.get("avg_frame_rate")
    ) or _parse_rate(video_stream.get("r_frame_rate"))

    audio = None
    if audio_stream is not None:
        audio = AudioStreamMetadata(
            codec=audio_stream.get("codec_name"),
            sample_rate_hz=_optional_int(audio_stream.get("sample_rate")),
            channels=_optional_int(audio_stream.get("channels")),
            duration_seconds=_optional_duration(audio_stream.get("duration")),
            start_time_seconds=_optional_float(audio_stream.get("start_time")),
        )

    return VideoMetadata(
        file_name=source.name,
        file_size_bytes=source.stat().st_size,
        duration_seconds=duration,
        format_name=format_data.get("format_name"),
        format_long_name=format_data.get("format_long_name"),
        video=VideoStreamMetadata(
            codec=video_stream.get("codec_name"),
            width=_optional_int(video_stream.get("width")),
            height=_optional_int(video_stream.get("height")),
            frame_rate=frame_rate,
            duration_seconds=_optional_duration(video_stream.get("duration")),
            start_time_seconds=_optional_float(video_stream.get("start_time")),
        ),
        audio=audio,
    )
