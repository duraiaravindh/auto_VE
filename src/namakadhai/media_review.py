"""Read-only media preflight and bounded FFmpeg visual contact sheets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from namakadhai.probe import (
    DEFAULT_SETTINGS_PATH,
    ProbeError,
    VideoMetadata,
    probe_video,
)

SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".m4v", ".avi"})
DEFAULT_MAX_FRAMES = 12
MAX_CONTACT_SHEET_FRAMES = 24
DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_THUMBNAIL_WIDTH = 320
MAX_THUMBNAIL_WIDTH = 640
DEFAULT_COLUMNS = 3


class MediaReviewError(RuntimeError):
    """Base error for media preflight and visual review."""


class UnsupportedMediaError(MediaReviewError):
    """The input file extension is not supported for this workflow."""


class MediaToolError(MediaReviewError):
    """A configured local media executable is missing or failed."""


class ReviewOutputExistsError(MediaReviewError):
    """A requested output already exists and will not be overwritten."""


@dataclass(frozen=True)
class ReviewPaths:
    project_directory: Path
    report: Path
    contact_sheet: Path


@dataclass(frozen=True)
class MediaReviewResult:
    metadata: VideoMetadata
    paths: ReviewPaths
    warnings: tuple[str, ...]


def _load_settings(settings_path: str | Path | None) -> tuple[dict, Path]:
    config_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MediaReviewError("Could not read project settings.") from exc
    except json.JSONDecodeError as exc:
        raise MediaReviewError("Project settings contain invalid JSON.") from exc
    if not isinstance(settings, dict) or not isinstance(settings.get("paths"), dict):
        raise MediaReviewError("Project settings must contain a paths object.")
    return settings, config_path.resolve().parent.parent


def _safe_project_name(source: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip(" .-_")
    stem = stem[:48].rstrip(" .-_") or "recording"
    digest = hashlib.sha256(str(source.resolve()).casefold().encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{digest}"


def _review_paths(
    source: Path,
    settings_path: str | Path | None,
) -> ReviewPaths:
    settings, root = _load_settings(settings_path)
    try:
        projects_value = settings["paths"]["projects"]
    except (KeyError, TypeError) as exc:
        raise MediaReviewError("Settings must define paths.projects.") from exc
    if not isinstance(projects_value, str) or not projects_value:
        raise MediaReviewError("paths.projects must be a non-empty path.")
    projects = Path(projects_value)
    if not projects.is_absolute():
        projects = root / projects
    project = projects / _safe_project_name(source)
    visual_review = project / "visual_review"
    return ReviewPaths(
        project_directory=project,
        report=project / "media_review.md",
        contact_sheet=visual_review / "contact_sheet.jpg",
    )


def _resolve_ffmpeg(settings_path: str | Path | None) -> Path:
    settings, root = _load_settings(settings_path)
    try:
        configured = settings["paths"]["ffmpeg_executable"]
    except (KeyError, TypeError) as exc:
        raise MediaToolError("Settings must define paths.ffmpeg_executable.") from exc
    if not isinstance(configured, str) or not configured.strip():
        raise MediaToolError("paths.ffmpeg_executable must be a non-empty path.")
    executable = Path(configured)
    if not executable.is_absolute():
        executable = root / executable
    executable = executable.resolve()
    if not executable.is_file():
        raise MediaToolError(
            "The configured project-local FFmpeg executable was not found."
        )
    return executable


def preflight_media(
    media_path: str | Path,
    *,
    settings_path: str | Path | None = None,
) -> tuple[VideoMetadata, ReviewPaths]:
    """Validate and probe one supported video without changing the input."""
    source = Path(media_path).expanduser()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Video file does not exist: {source.name}")
    if source.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_VIDEO_EXTENSIONS))
        raise UnsupportedMediaError(
            f"Unsupported video extension '{source.suffix}'. Supported: {allowed}."
        )

    try:
        metadata = probe_video(source, settings_path=settings_path)
    except ProbeError as exc:
        raise MediaReviewError(f"Video preflight failed: {exc}") from exc
    return metadata, _review_paths(source, settings_path)


def build_contact_sheet_command(
    ffmpeg_executable: str | Path,
    source: str | Path,
    output: str | Path,
    *,
    duration_seconds: float,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_frames: int = DEFAULT_MAX_FRAMES,
    thumbnail_width: int = DEFAULT_THUMBNAIL_WIDTH,
    columns: int = DEFAULT_COLUMNS,
) -> list[str]:
    """Build a single bounded FFmpeg command for an interval contact sheet."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be a positive finite number.")
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a positive finite number.")
    if isinstance(max_frames, bool) or not 1 <= max_frames <= MAX_CONTACT_SHEET_FRAMES:
        raise ValueError(
            f"max_frames must be between 1 and {MAX_CONTACT_SHEET_FRAMES}."
        )
    if (
        isinstance(thumbnail_width, bool)
        or not 16 <= thumbnail_width <= MAX_THUMBNAIL_WIDTH
    ):
        raise ValueError(
            f"thumbnail_width must be between 16 and {MAX_THUMBNAIL_WIDTH} pixels."
        )
    if isinstance(columns, bool) or not 1 <= columns <= MAX_CONTACT_SHEET_FRAMES:
        raise ValueError(
            f"columns must be between 1 and {MAX_CONTACT_SHEET_FRAMES}."
        )

    frame_count = min(max_frames, max(1, math.ceil(duration_seconds / interval_seconds)))
    rows = math.ceil(frame_count / columns)
    filter_graph = (
        f"fps=1/{interval_seconds:.6f},"
        f"scale={thumbnail_width}:-2:force_original_aspect_ratio=decrease,"
        f"tile={columns}x{rows}:nb_frames={frame_count}:padding=8:margin=8"
    )
    return [
        str(ffmpeg_executable),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(Path(source).resolve()),
        "-vf",
        filter_graph,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output),
    ]


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unavailable"
    total_seconds = round(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _report_text(
    metadata: VideoMetadata,
    paths: ReviewPaths,
    contact_sheet_created: bool,
    warnings: tuple[str, ...],
) -> str:
    video = metadata.video
    resolution = (
        f"{video.width} x {video.height}"
        if video.width is not None and video.height is not None
        else "Unavailable"
    )
    frame_rate = (
        f"{video.frame_rate:.3f} fps"
        if video.frame_rate is not None
        else "Unavailable"
    )
    if metadata.audio is None:
        audio = "No audio stream detected"
    else:
        audio = (
            f"{metadata.audio.codec or 'Unknown codec'}, "
            f"{metadata.audio.sample_rate_hz or 'Unknown'} Hz, "
            f"{metadata.audio.channels or 'Unknown'} channel(s)"
        )

    sheet_location = (
        str(paths.contact_sheet.relative_to(paths.project_directory))
        if contact_sheet_created
        else "Not generated"
    )
    warning_lines = (
        "\n".join(f"- {warning}" for warning in warnings)
        if warnings
        else "- None reported by preflight."
    )
    duration = (
        f"{_format_duration(metadata.duration_seconds)} "
        f"({metadata.duration_seconds:.3f} seconds)"
        if metadata.duration_seconds is not None
        else "Unavailable"
    )
    return (
        "# Media review report\n\n"
        f"- **File:** `{metadata.file_name}`\n"
        f"- **File size:** {metadata.file_size_bytes:,} bytes\n"
        f"- **Duration:** {duration}\n"
        f"- **Container:** {metadata.format_name or 'Unavailable'}\n"
        f"- **Video:** {video.codec or 'Unknown codec'}, {resolution}, {frame_rate}\n"
        f"- **Audio:** {audio}\n"
        f"- **Contact sheet:** `{sheet_location}`\n\n"
        "## Manual review suggestions\n\n"
        "- Review the visual contact sheet for framing, continuity, and possible "
        "B-roll opportunities.\n"
        "- Preserve expressive pauses and natural pacing when planning edits.\n"
        "- These are generic suggestions, not AI findings. No speech or story "
        "analysis was performed.\n\n"
        "## Warnings\n\n"
        f"{warning_lines}\n"
    )


def _write_report(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="") as report_file:
            report_file.write(content)
    except FileExistsError as exc:
        raise ReviewOutputExistsError(
            f"Review report already exists; refusing to overwrite {path.name}."
        ) from exc
    except OSError as exc:
        raise MediaReviewError("Could not write the media review report.") from exc


def prepare_media_review(
    media_path: str | Path,
    *,
    settings_path: str | Path | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_frames: int = DEFAULT_MAX_FRAMES,
    thumbnail_width: int = DEFAULT_THUMBNAIL_WIDTH,
    columns: int = DEFAULT_COLUMNS,
) -> MediaReviewResult:
    """Probe media, attempt one bounded contact sheet, and write a review report."""
    metadata, paths = preflight_media(media_path, settings_path=settings_path)
    warnings: list[str] = []
    contact_sheet_created = False

    if paths.report.exists():
        raise ReviewOutputExistsError(
            f"Review report already exists; refusing to overwrite {paths.report.name}."
        )
    if paths.contact_sheet.exists():
        raise ReviewOutputExistsError(
            "Contact sheet already exists; refusing to overwrite contact_sheet.jpg."
        )

    try:
        ffmpeg = _resolve_ffmpeg(settings_path)
        paths.contact_sheet.parent.mkdir(parents=True, exist_ok=True)
        command = build_contact_sheet_command(
            ffmpeg,
            media_path,
            paths.contact_sheet,
            duration_seconds=metadata.duration_seconds or 0,
            interval_seconds=interval_seconds,
            max_frames=max_frames,
            thumbnail_width=thumbnail_width,
            columns=columns,
        )
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
            shell=False,
        )
        if (
            result.returncode != 0
            or not paths.contact_sheet.is_file()
            or paths.contact_sheet.stat().st_size == 0
        ):
            detail = result.stderr.strip()
            if detail:
                detail = detail.replace(str(Path(media_path).resolve()), Path(media_path).name)
                detail = detail.replace(str(ffmpeg), "ffmpeg")
            raise MediaToolError(
                "FFmpeg failed to generate the contact sheet."
                + (f" {detail}" if detail else "")
            )
        contact_sheet_created = True
    except (MediaToolError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        warnings.append(str(exc))
        if paths.contact_sheet.exists():
            paths.contact_sheet.unlink()

    report = _report_text(
        metadata,
        paths,
        contact_sheet_created,
        tuple(warnings),
    )
    _write_report(paths.report, report)
    return MediaReviewResult(
        metadata=metadata,
        paths=paths,
        warnings=tuple(warnings),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only video preflight and visual contact-sheet generation."
    )
    parser.add_argument("video", type=Path, help="Path to an existing video file.")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between representative frames (default: {DEFAULT_INTERVAL_SECONDS:g}).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
        help=f"Maximum contact-sheet frames (default: {DEFAULT_MAX_FRAMES}).",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_THUMBNAIL_WIDTH,
        help=f"Thumbnail width in pixels (default: {DEFAULT_THUMBNAIL_WIDTH}).",
    )
    parser.add_argument(
        "--columns",
        type=int,
        default=DEFAULT_COLUMNS,
        help=f"Contact-sheet columns (default: {DEFAULT_COLUMNS}).",
    )
    arguments = parser.parse_args(argv)
    try:
        result = prepare_media_review(
            arguments.video,
            interval_seconds=arguments.interval,
            max_frames=arguments.max_frames,
            thumbnail_width=arguments.width,
            columns=arguments.columns,
        )
    except (MediaReviewError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Review report: {result.paths.report}")
    if result.paths.contact_sheet.is_file():
        print(f"Contact sheet: {result.paths.contact_sheet}")
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(
        "Metadata: "
        f"{result.metadata.file_name}, "
        f"{_format_duration(result.metadata.duration_seconds)}, "
        f"{result.metadata.video.width}x{result.metadata.video.height}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
