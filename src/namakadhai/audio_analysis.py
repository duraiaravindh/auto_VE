"""Read-only local audio-level and quiet-interval analysis using FFmpeg."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from namakadhai.media_review import (
    MediaReviewError,
    _resolve_ffmpeg,
    preflight_media,
)

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"
DEFAULT_SILENCE_THRESHOLD_DB = -35.0
DEFAULT_MINIMUM_SILENCE_SECONDS = 1.0
SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(
    r"silence_end:\s*(-?\d+(?:\.\d+)?).*?silence_duration:\s*(-?\d+(?:\.\d+)?)"
)
VOLUME_RE = re.compile(
    r"(?P<name>mean_volume|max_volume):\s*(?P<value>-?inf|-?\d+(?:\.\d+)?)\s*dB",
    re.IGNORECASE,
)


class AudioAnalysisError(RuntimeError):
    """Base error for local audio analysis."""


class AudioAnalysisConfigurationError(AudioAnalysisError):
    """The audio analysis settings are invalid."""


class AudioAnalysisToolError(AudioAnalysisError):
    """FFmpeg could not analyze the input audio."""


class AudioAnalysisOutputExistsError(AudioAnalysisError):
    """An audio report already exists and will not be overwritten."""


@dataclass(frozen=True)
class QuietInterval:
    start_seconds: float
    end_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class AudioLevels:
    mean_volume_db: float | None
    max_volume_db: float | None


@dataclass(frozen=True)
class AudioAnalysisResult:
    report_path: Path
    has_audio: bool
    threshold_db: float
    minimum_duration_seconds: float
    quiet_intervals: tuple[QuietInterval, ...]
    levels: AudioLevels | None
    warnings: tuple[str, ...]


def _load_audio_settings(
    settings_path: str | Path | None,
) -> tuple[float, float]:
    config_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AudioAnalysisConfigurationError(
            "Could not read project settings."
        ) from exc
    except json.JSONDecodeError as exc:
        raise AudioAnalysisConfigurationError(
            "Project settings contain invalid JSON."
        ) from exc

    try:
        audio_settings = settings.get("analysis", {}).get("audio", {})
        threshold_db = audio_settings.get(
            "silence_threshold_db", DEFAULT_SILENCE_THRESHOLD_DB
        )
        minimum_duration = audio_settings.get(
            "minimum_silence_duration_seconds",
            DEFAULT_MINIMUM_SILENCE_SECONDS,
        )
        threshold_db = float(threshold_db)
        minimum_duration = float(minimum_duration)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AudioAnalysisConfigurationError(
            "analysis.audio silence settings must be numeric."
        ) from exc

    if not math.isfinite(threshold_db) or threshold_db > 0:
        raise AudioAnalysisConfigurationError(
            "silence_threshold_db must be a finite value at or below 0."
        )
    if not math.isfinite(minimum_duration) or minimum_duration <= 0:
        raise AudioAnalysisConfigurationError(
            "minimum_silence_duration_seconds must be a positive finite value."
        )
    return threshold_db, minimum_duration


def _parse_audio_analysis(
    stderr: str,
    *,
    duration_seconds: float | None,
) -> tuple[tuple[QuietInterval, ...], AudioLevels]:
    intervals: list[QuietInterval] = []
    open_silence: float | None = None
    for line in stderr.splitlines():
        start_match = SILENCE_START_RE.search(line)
        end_match = SILENCE_END_RE.search(line)
        if start_match:
            try:
                open_silence = max(0.0, float(start_match.group(1)))
            except ValueError:
                continue
        if end_match:
            try:
                end_time = float(end_match.group(1))
                silence_duration = float(end_match.group(2))
            except ValueError:
                open_silence = None
                continue
            start_time = (
                open_silence
                if open_silence is not None
                else max(0.0, end_time - silence_duration)
            )
            if end_time >= start_time and silence_duration >= 0:
                intervals.append(
                    QuietInterval(
                        start_seconds=start_time,
                        end_seconds=end_time,
                        duration_seconds=silence_duration,
                    )
                )
            open_silence = None

    if open_silence is not None and duration_seconds is not None:
        end_time = max(open_silence, duration_seconds)
        intervals.append(
            QuietInterval(
                start_seconds=open_silence,
                end_seconds=end_time,
                duration_seconds=end_time - open_silence,
            )
        )

    levels: dict[str, float | None] = {
        "mean_volume": None,
        "max_volume": None,
    }
    for match in VOLUME_RE.finditer(stderr):
        name = match.group("name").lower()
        raw_value = match.group("value").lower()
        levels[name] = (
            None if "inf" in raw_value else float(raw_value)
        )
    return tuple(intervals), AudioLevels(
        mean_volume_db=levels["mean_volume"],
        max_volume_db=levels["max_volume"],
    )


def _build_ffmpeg_command(
    executable: Path,
    media_path: Path,
    *,
    threshold_db: float,
    minimum_duration_seconds: float,
) -> list[str]:
    filter_chain = (
        f"silencedetect=noise={threshold_db:.2f}dB:"
        f"d={minimum_duration_seconds:.3f},volumedetect"
    )
    return [
        str(executable),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-i",
        str(media_path.resolve()),
        "-vn",
        "-af",
        filter_chain,
        "-f",
        "null",
        "NUL",
    ]


def _render_report(
    *,
    file_name: str,
    duration_seconds: float | None,
    threshold_db: float,
    minimum_duration_seconds: float,
    intervals: tuple[QuietInterval, ...],
    levels: AudioLevels | None,
    warnings: tuple[str, ...],
) -> str:
    duration = (
        f"{duration_seconds:.3f} seconds"
        if duration_seconds is not None
        else "unavailable"
    )
    lines = [
        "# Audio analysis report",
        "",
        f"- **File:** `{file_name}`",
        f"- **Video duration:** {duration}",
        f"- **Quiet threshold:** {threshold_db:.2f} dBFS",
        f"- **Minimum interval duration:** {minimum_duration_seconds:.3f} seconds",
        "",
        "This is a signal-level analysis only. Quiet speech and expressive pauses "
        "can be misclassified; no speech, story, emotion, or intent was analyzed. "
        "Review every interval before considering any edit. Nothing was removed "
        "or changed.",
        "",
        "## Audio levels",
        "",
    ]
    if levels is None:
        lines.append("No audio stream was detected; audio levels are unavailable.")
    else:
        mean = (
            f"{levels.mean_volume_db:.2f} dBFS"
            if levels.mean_volume_db is not None
            else "Unavailable"
        )
        peak = (
            f"{levels.max_volume_db:.2f} dBFS"
            if levels.max_volume_db is not None
            else "Unavailable"
        )
        lines.extend((f"- Mean volume: {mean}", f"- Peak volume: {peak}"))
    lines.extend(("", "## Candidate quiet intervals", ""))
    if intervals:
        lines.append("| Start (s) | End (s) | Duration (s) | Review note |")
        lines.append("|---:|---:|---:|---|")
        for interval in intervals:
            lines.append(
                f"| {interval.start_seconds:.3f} | {interval.end_seconds:.3f} "
                f"| {interval.duration_seconds:.3f} | Review manually; not a cut recommendation |"
            )
    else:
        lines.append("No quiet intervals met the configured threshold and duration.")
    lines.extend(("", "## Warnings", ""))
    lines.extend(
        f"- {warning}" for warning in warnings
    ) if warnings else lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def analyze_audio(
    media_path: str | Path,
    *,
    settings_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> AudioAnalysisResult:
    """Detect candidate quiet intervals and loudness levels without editing media."""
    metadata, review_paths = preflight_media(
        media_path,
        settings_path=settings_path,
    )
    threshold_db, minimum_duration = _load_audio_settings(settings_path)
    output_report_path = (
        Path(report_path)
        if report_path is not None
        else review_paths.project_directory / "audio_analysis.md"
    )
    if output_report_path.exists():
        raise AudioAnalysisOutputExistsError(
            "Audio analysis report already exists; refusing to overwrite "
            f"{output_report_path.name}."
        )

    intervals: tuple[QuietInterval, ...] = ()
    levels: AudioLevels | None = None
    warnings: list[str] = []
    has_audio = metadata.audio is not None

    if has_audio:
        try:
            executable = _resolve_ffmpeg(settings_path)
        except MediaReviewError as exc:
            raise AudioAnalysisToolError(str(exc)) from exc
        command = _build_ffmpeg_command(
            executable,
            Path(media_path),
            threshold_db=threshold_db,
            minimum_duration_seconds=minimum_duration,
        )
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
            raise AudioAnalysisToolError(
                f"FFmpeg timed out while analyzing {metadata.file_name}."
            ) from exc
        except OSError as exc:
            raise AudioAnalysisToolError(
                "Could not start the configured project-local FFmpeg executable."
            ) from exc

        if result.returncode != 0:
            detail = result.stderr.strip()
            detail = detail.replace(
                str(Path(media_path).resolve()), metadata.file_name
            )
            detail = detail.replace(str(executable), "ffmpeg")
            raise AudioAnalysisToolError(
                f"FFmpeg audio analysis failed for {metadata.file_name} "
                f"(exit code {result.returncode})."
                + (f" {detail}" if detail else "")
            )
        intervals, levels = _parse_audio_analysis(
            result.stderr,
            duration_seconds=metadata.duration_seconds,
        )
        if not intervals:
            warnings.append(
                "No intervals met the configured quiet threshold and minimum duration."
            )
    else:
        warnings.append("No audio stream was detected.")

    report = _render_report(
        file_name=metadata.file_name,
        duration_seconds=metadata.duration_seconds,
        threshold_db=threshold_db,
        minimum_duration_seconds=minimum_duration,
        intervals=intervals,
        levels=levels,
        warnings=tuple(warnings),
    )
    try:
        output_report_path.parent.mkdir(parents=True, exist_ok=True)
        with output_report_path.open("x", encoding="utf-8", newline="") as report_file:
            report_file.write(report)
    except FileExistsError as exc:
        raise AudioAnalysisOutputExistsError(
            "Audio analysis report appeared during analysis; it was not overwritten."
        ) from exc
    except OSError as exc:
        raise AudioAnalysisError("Could not write the audio analysis report.") from exc

    return AudioAnalysisResult(
        report_path=output_report_path,
        has_audio=has_audio,
        threshold_db=threshold_db,
        minimum_duration_seconds=minimum_duration,
        quiet_intervals=intervals,
        levels=levels,
        warnings=tuple(warnings),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only local audio quiet-interval and level analysis."
    )
    parser.add_argument("video", type=Path, help="Path to an existing video file.")
    arguments = parser.parse_args(argv)
    try:
        result = analyze_audio(arguments.video)
    except (AudioAnalysisError, MediaReviewError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Audio report: {result.report_path}")
    print(f"Audio stream found: {'yes' if result.has_audio else 'no'}")
    print(f"Candidate quiet intervals: {len(result.quiet_intervals)}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
