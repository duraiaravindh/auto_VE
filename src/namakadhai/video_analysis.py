"""Deterministic visual signal analysis; results are flags, not story judgments."""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from namakadhai.media_review import _resolve_ffmpeg
from namakadhai.probe import VideoMetadata

BLACK_RE = re.compile(
    r"black_start:(?P<start>\d+(?:\.\d+)?)\s+black_end:(?P<end>\d+(?:\.\d+)?)\s+black_duration:(?P<duration>\d+(?:\.\d+)?)"
)
FREEZE_START_RE = re.compile(r"freeze_start:\s*(?P<value>\d+(?:\.\d+)?)")
FREEZE_END_RE = re.compile(
    r"freeze_end:\s*(?P<end>\d+(?:\.\d+)?)\s*\|\s*freeze_duration:\s*(?P<duration>\d+(?:\.\d+)?)"
)
BRIGHTNESS_RE = re.compile(r"lavfi\.signalstats\.YAVG=(?P<value>\d+(?:\.\d+)?)")
SCENE_RE = re.compile(r"Parsed_showinfo[^\n]*pts_time:(?P<value>\d+(?:\.\d+)?)")


class VideoAnalysisError(RuntimeError):
    """Visual analysis failed without modifying the input."""


def _parse(stderr: str, stdout: str, duration: float) -> dict[str, Any]:
    black = [
        {
            "start_seconds": float(match.group("start")),
            "end_seconds": float(match.group("end")),
            "duration_seconds": float(match.group("duration")),
        }
        for match in BLACK_RE.finditer(stderr)
    ]
    freeze_starts = [float(match.group("value")) for match in FREEZE_START_RE.finditer(stderr)]
    freeze_ends = [
        (float(match.group("end")), float(match.group("duration")))
        for match in FREEZE_END_RE.finditer(stderr)
    ]
    frozen: list[dict[str, float]] = []
    for index, (end, measured) in enumerate(freeze_ends):
        start = freeze_starts[index] if index < len(freeze_starts) else max(0.0, end - measured)
        frozen.append(
            {"start_seconds": start, "end_seconds": end, "duration_seconds": measured}
        )
    if len(freeze_starts) > len(freeze_ends):
        for start in freeze_starts[len(freeze_ends) :]:
            if duration > start:
                frozen.append(
                    {
                        "start_seconds": start,
                        "end_seconds": duration,
                        "duration_seconds": duration - start,
                        "open_ended": True,
                    }
                )
    brightness = [float(match.group("value")) for match in BRIGHTNESS_RE.finditer(stdout)]
    scene_times = sorted(
        {
            round(value, 3)
            for match in SCENE_RE.finditer(stderr)
            if (value := float(match.group("value"))) > 0.001
        }
    )
    average = sum(brightness) / len(brightness) if brightness else None
    warnings: list[str] = []
    if black:
        warnings.append(f"Detected {len(black)} possible black-frame interval(s); review visually.")
    if frozen:
        warnings.append(f"Detected {len(frozen)} possible frozen interval(s); review visually.")
    if average is not None and average < 45:
        warnings.append("Sampled frames appear unusually dark; verify exposure before grading.")
    if average is not None and average > 210:
        warnings.append("Sampled frames appear unusually bright; verify exposure before grading.")
    return {
        "scene_change_times_seconds": scene_times,
        "black_intervals": black,
        "frozen_intervals": frozen,
        "brightness": {
            "sample_count": len(brightness),
            "average_y": round(average, 3) if average is not None else None,
            "minimum_y": min(brightness) if brightness else None,
            "maximum_y": max(brightness) if brightness else None,
            "scale": "limited/full-range luma signal reported by FFmpeg signalstats (0-255)",
        },
        "warnings": warnings,
        "interpretation": (
            "Signal-level candidates only. Scene changes do not reveal narrative structure, "
            "and black/freeze flags require human review."
        ),
    }


def analyze_video_signals(
    source: str | Path,
    metadata: VideoMetadata,
    output_path: str | Path,
    *,
    settings_path: str | Path | None = None,
) -> dict[str, Any]:
    video = Path(source).resolve()
    output = Path(output_path).resolve()
    if output.exists():
        raise VideoAnalysisError(f"Refusing to overwrite visual analysis: {output.name}")
    duration = metadata.duration_seconds
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise VideoAnalysisError("Video duration is unavailable for visual analysis.")
    ffmpeg = _resolve_ffmpeg(settings_path)
    # Three bounded branches: full-rate fault flags, sparse brightness samples, scene candidates.
    graph = (
        "[0:v]split=3[fault][light][scene];"
        "[fault]blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=2[faultout];"
        "[light]fps=1/5,signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG:file=-[lightout];"
        # The first frame guarantees a packet even when no scene candidate crosses
        # the threshold. It is removed from the parsed candidate list.
        "[scene]select='eq(n,0)+gt(scene,0.30)',showinfo[sceneout]"
    )
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(video),
        "-filter_complex",
        graph,
        "-map",
        "[faultout]",
        "-map",
        "[lightout]",
        "-map",
        "[sceneout]",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(60.0, min(600.0, duration * 4)),
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoAnalysisError("FFmpeg could not complete visual signal analysis.") from exc
    if result.returncode != 0:
        raise VideoAnalysisError(
            f"FFmpeg visual analysis failed (exit {result.returncode}): {result.stderr[-500:]}"
        )
    payload = {
        "version": 1,
        "source_file": video.name,
        "duration_seconds": duration,
        "analysis": _parse(result.stderr, result.stdout, duration),
        "ffmpeg_operation": "allowlisted visual signal filters",
    }
    try:
        with output.open("x", encoding="utf-8", newline="") as report:
            json.dump(payload, report, ensure_ascii=False, indent=2)
            report.write("\n")
    except OSError as exc:
        raise VideoAnalysisError("Could not write visual analysis report.") from exc
    return {"report_path": str(output), **payload["analysis"]}

