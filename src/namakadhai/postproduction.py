"""Approval-gated local edit planning and technical preview rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from namakadhai.audio_analysis import (
    AudioAnalysisError,
    AudioAnalysisResult,
    analyze_audio,
)
from namakadhai.media_review import (
    MediaReviewError,
    _load_settings,
    _resolve_ffmpeg,
    _safe_project_name,
    preflight_media,
)
from namakadhai.probe import (
    ProbeError,
    VideoMetadata,
    _resolve_ffprobe,
    probe_video,
)

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"
PLAN_VERSION = 1
APPROVAL_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED"})
REQUIRED_FILTERS = frozenset({"trim", "setpts", "null", "fps", "settb"})
MUSIC_FILTERS = frozenset({"volume", "afade", "amix"})
VIDEO_FILTERS = frozenset({"eq"})
WHITE_BALANCE_FILTERS = frozenset({"colorbalance"})
MAX_CANDIDATE_CUT_SECONDS = 120.0


class PostProductionError(RuntimeError):
    """Base error for approval-gated post-production operations."""


class EditPlanError(PostProductionError):
    """The edit plan or its approvals are invalid or stale."""


class EditPlanOutputExistsError(PostProductionError):
    """A requested edit plan or approval output already exists."""


class RenderValidationError(PostProductionError):
    """A rendered preview failed validation."""


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    size_bytes: int
    sha256: str
    metadata: VideoMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "duration_seconds": self.metadata.duration_seconds,
            "video": self.metadata.to_dict()["video"],
            "audio": self.metadata.to_dict()["audio"],
        }


@dataclass(frozen=True)
class CutCandidate:
    candidate_id: str
    source_start: float
    source_end: float
    cut_start: float
    cut_end: float
    retained_ranges: tuple[tuple[float, float], ...]
    detection_method: str
    evidence: str
    confidence: str
    confidence_explanation: str
    limitations: str
    status: str = "PENDING"
    reviewer_notes: str = ""

    @property
    def trim_duration(self) -> float:
        return self.cut_end - self.cut_start

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_start_seconds": self.source_start,
            "source_end_seconds": self.source_end,
            "proposed_cut_seconds": [self.cut_start, self.cut_end],
            "proposed_retained_source_ranges_seconds": [
                list(item) for item in self.retained_ranges
            ],
            "proposed_trim_duration_seconds": self.trim_duration,
            "safety_padding_seconds": {
                "before": self.cut_start - self.source_start,
                "after": self.source_end - self.cut_end,
            },
            "detection_method": self.detection_method,
            "measured_evidence": self.evidence,
            "confidence_category": self.confidence,
            "confidence_explanation": self.confidence_explanation,
            "limitations_and_review_notes": self.limitations,
            "status": self.status,
            "reviewer_notes": self.reviewer_notes,
        }


@dataclass(frozen=True)
class EditPlan:
    plan_path: Path
    report_path: Path
    approvals_path: Path
    snapshot: SourceSnapshot
    candidates: tuple[CutCandidate, ...]


@dataclass(frozen=True)
class MusicMetadata:
    path: Path
    sha256: str
    duration_seconds: float | None
    codec: str | None
    sample_rate_hz: int | None
    channels: int | None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PostProductionError(f"Could not read source file {path.name}.") from exc
    return digest.hexdigest()


def _snapshot_source(
    video_path: str | Path,
    *,
    settings_path: str | Path | None,
) -> SourceSnapshot:
    source = Path(video_path).expanduser().resolve()
    metadata, _ = preflight_media(source, settings_path=settings_path)
    return SourceSnapshot(
        path=source,
        size_bytes=source.stat().st_size,
        sha256=_sha256_file(source),
        metadata=metadata,
    )


def _assert_source_unchanged(snapshot: SourceSnapshot) -> None:
    try:
        current_size = snapshot.path.stat().st_size
    except OSError as exc:
        raise PostProductionError("Source file is no longer available.") from exc
    if current_size != snapshot.size_bytes or _sha256_file(snapshot.path) != snapshot.sha256:
        raise PostProductionError(
            "Source file changed during processing; refusing to continue."
        )


def _candidate_id(start: float, end: float) -> str:
    token = hashlib.sha256(f"{start:.6f}:{end:.6f}".encode("ascii")).hexdigest()[:12]
    return f"silence-{token}"


def _make_candidates(
    analysis: AudioAnalysisResult,
    *,
    duration_seconds: float | None,
    padding_before_seconds: float,
    padding_after_seconds: float,
    minimum_cut_seconds: float,
    frame_rate_hz: float | None = None,
) -> tuple[CutCandidate, ...]:
    if (
        not math.isfinite(padding_before_seconds)
        or not math.isfinite(padding_after_seconds)
        or padding_before_seconds < 0
        or padding_after_seconds < 0
    ):
        raise ValueError("Safety padding values must be finite and non-negative.")
    if not math.isfinite(minimum_cut_seconds) or minimum_cut_seconds <= 0:
        raise ValueError("minimum_cut_seconds must be positive and finite.")
    if duration_seconds is not None and (
        not math.isfinite(duration_seconds) or duration_seconds <= 0
    ):
        raise ValueError("duration_seconds must be positive and finite when provided.")
    if frame_rate_hz is not None and (
        not math.isfinite(frame_rate_hz) or frame_rate_hz <= 0
    ):
        raise ValueError("frame_rate_hz must be positive and finite when provided.")

    candidates: list[CutCandidate] = []
    for interval in analysis.quiet_intervals:
        source_start = interval.start_seconds
        source_end = interval.end_seconds
        if (
            not math.isfinite(source_start)
            or not math.isfinite(source_end)
            or not math.isfinite(interval.duration_seconds)
            or source_start < 0
            or source_end <= source_start
            or interval.duration_seconds < 0
        ):
            continue
        cut_start = source_start + padding_before_seconds
        cut_end = source_end - padding_after_seconds
        if frame_rate_hz is not None:
            cut_start = math.ceil(cut_start * frame_rate_hz - 1e-9) / frame_rate_hz
            cut_end = math.floor(cut_end * frame_rate_hz + 1e-9) / frame_rate_hz
        if cut_end - cut_start < minimum_cut_seconds:
            continue
        if duration_seconds is not None and (
            source_start < 0 or source_end > duration_seconds or cut_end > duration_seconds
        ):
            continue
        retained: list[tuple[float, float]] = []
        if cut_start > source_start:
            retained.append((source_start, cut_start))
        if source_end > cut_end:
            retained.append((cut_end, source_end))
        candidates.append(
            CutCandidate(
                candidate_id=_candidate_id(source_start, source_end),
                source_start=source_start,
                source_end=source_end,
                cut_start=cut_start,
                cut_end=cut_end,
                retained_ranges=tuple(retained),
                detection_method="FFmpeg silencedetect",
                evidence=(
                    f"Below {analysis.threshold_db:.2f} dBFS for "
                    f"{interval.duration_seconds:.3f} seconds."
                ),
                confidence="LOW",
                confidence_explanation=(
                    "Signal-level silence is not evidence that a pause is unwanted."
                ),
                limitations=(
                    "Quiet speech, breaths, and expressive pauses may be detected. "
                    "Listen to the source around this interval before deciding."
                ),
            )
        )
    return tuple(candidates)


def _plan_directory(snapshot: SourceSnapshot, projects_directory: Path) -> Path:
    recording_project = projects_directory / _safe_project_name(snapshot.path)
    edit_review = recording_project / "edit_review"
    run_name = datetime.now(timezone.utc).strftime("plan-%Y%m%dT%H%M%S-%fZ")
    return edit_review / run_name


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    staged_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with staged_path.open("x", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        try:
            os.link(staged_path, path)
        except FileExistsError as exc:
            raise EditPlanOutputExistsError(
                f"Refusing to overwrite existing file {path.name}."
            ) from exc
        except OSError as exc:
            raise PostProductionError(f"Could not safely publish {path.name}.") from exc
    except OSError as exc:
        raise PostProductionError(f"Could not write {path.name}.") from exc
    finally:
        staged_path.unlink(missing_ok=True)


def _render_plan_markdown(
    snapshot: SourceSnapshot,
    candidates: tuple[CutCandidate, ...],
    analysis: AudioAnalysisResult,
) -> str:
    duration = snapshot.metadata.duration_seconds
    lines = [
        "# Approval-gated edit plan",
        "",
        f"- **Source:** `{snapshot.path.name}`",
        f"- **Duration:** {duration:.3f} seconds" if duration is not None else "- **Duration:** unavailable",
        f"- **Source SHA-256:** `{snapshot.sha256}`",
        f"- **Silence threshold:** {analysis.threshold_db:.2f} dBFS",
        f"- **Minimum detected interval:** {analysis.minimum_duration_seconds:.3f} seconds",
        "",
        "Review-only proposals. Nothing has been cut or rendered. Candidate times refer "
        "to the original source timeline. Silence detection cannot determine whether "
        "speech or a pause is creatively expendable.",
        "",
        "## Candidates",
        "",
    ]
    if not candidates:
        lines.append("No valid cuts are proposed.")
    else:
        lines.extend(
            (
                "| ID | Detected source span (s) | Proposed cut (s) | Trim (s) | Confidence | Status |",
                "|---|---:|---:|---:|---|---|",
            )
        )
        for candidate in candidates:
            lines.append(
                f"| `{candidate.candidate_id}` | "
                f"{candidate.source_start:.3f}–{candidate.source_end:.3f} | "
                f"{candidate.cut_start:.3f}–{candidate.cut_end:.3f} | "
                f"{candidate.trim_duration:.3f} | LOW — signal only | PENDING |"
            )
            lines.extend(
                (
                    "",
                    f"- Evidence: {candidate.evidence}",
                    f"- Padding: {candidate.cut_start - candidate.source_start:.3f}s before, "
                    f"{candidate.source_end - candidate.cut_end:.3f}s after.",
                    f"- Limitation: {candidate.limitations}",
                )
            )
    lines.extend(
        (
            "",
            "## Review workflow",
            "",
            "Use the `review` command to append an APPROVED or REJECTED decision. "
            "Unreviewed candidates remain PENDING and are never applied.",
            "",
        )
    )
    return "\n".join(lines)


def analyze_for_edit_plan(
    video_path: str | Path,
    *,
    settings_path: str | Path | None = None,
    padding_before_seconds: float = 0.25,
    padding_after_seconds: float = 0.25,
    minimum_cut_seconds: float = 0.25,
) -> EditPlan:
    """Analyze source audio and write a hash-bound, review-only edit plan."""
    snapshot = _snapshot_source(video_path, settings_path=settings_path)
    settings, root = _load_settings(settings_path)
    projects_value = settings["paths"]["projects"]
    projects = Path(projects_value)
    if not projects.is_absolute():
        projects = root / projects
    run_directory = _plan_directory(snapshot, projects)
    plan_path = run_directory / "edit_plan.json"
    report_path = run_directory / "edit_plan.md"
    approvals_path = run_directory / "approvals.jsonl"
    if run_directory.exists() or any(path.exists() for path in (plan_path, report_path, approvals_path)):
        raise EditPlanOutputExistsError("Edit-plan output directory already exists.")

    run_directory.mkdir(parents=True, exist_ok=False)
    analysis_path = run_directory / "audio_analysis.md"
    try:
        analysis = analyze_audio(
            snapshot.path,
            settings_path=settings_path,
            report_path=analysis_path,
        )
        _assert_source_unchanged(snapshot)
        candidates = _make_candidates(
            analysis,
            duration_seconds=snapshot.metadata.duration_seconds,
            padding_before_seconds=padding_before_seconds,
            padding_after_seconds=padding_after_seconds,
            minimum_cut_seconds=minimum_cut_seconds,
            frame_rate_hz=snapshot.metadata.video.frame_rate,
        )
        payload = {
            "version": PLAN_VERSION,
            "source": snapshot.to_dict(),
            "analysis": {
                "method": "FFmpeg silencedetect and volumedetect",
                "threshold_dbfs": analysis.threshold_db,
                "minimum_interval_seconds": analysis.minimum_duration_seconds,
                "padding_before_seconds": padding_before_seconds,
                "padding_after_seconds": padding_after_seconds,
                "minimum_cut_seconds": minimum_cut_seconds,
                "source_frame_rate_hz": snapshot.metadata.video.frame_rate,
            },
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        _atomic_create_json(plan_path, payload)
        with report_path.open("x", encoding="utf-8", newline="") as report:
            report.write(_render_plan_markdown(snapshot, candidates, analysis))
        with approvals_path.open("x", encoding="utf-8", newline=""):
            pass
        _assert_source_unchanged(snapshot)
    except Exception:
        for generated_path in (analysis_path, plan_path, report_path, approvals_path):
            try:
                generated_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            run_directory.rmdir()
        except OSError:
            pass
        raise
    return EditPlan(plan_path, report_path, approvals_path, snapshot, candidates)


def load_plan(path: str | Path) -> tuple[dict[str, Any], Path]:
    plan_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EditPlanError(f"Could not read edit plan {plan_path.name}.") from exc
    except json.JSONDecodeError as exc:
        raise EditPlanError("Edit plan contains invalid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("version") != PLAN_VERSION:
        raise EditPlanError("Unsupported edit plan schema version.")
    source = payload.get("source")
    candidates = payload.get("candidates")
    if not isinstance(source, dict) or not isinstance(candidates, list):
        raise EditPlanError("Edit plan is missing source or candidate data.")
    return payload, plan_path


def _plan_sha256(plan_path: Path) -> str:
    return hashlib.sha256(plan_path.read_bytes()).hexdigest()


def record_candidate_decision(
    plan_path: str | Path,
    candidate_id: str,
    status: str,
    *,
    note: str = "",
) -> Path:
    """Append a human decision without mutating the original plan."""
    status = status.upper()
    if status not in {"APPROVED", "REJECTED", "PENDING"}:
        raise EditPlanError("Decision status must be APPROVED, REJECTED, or PENDING.")
    payload, plan = load_plan(plan_path)
    candidates = payload["candidates"]
    if not any(
        isinstance(item, dict) and item.get("candidate_id") == candidate_id
        for item in candidates
    ):
        raise EditPlanError(f"Unknown candidate ID: {candidate_id}")
    approvals_path = plan.parent / "approvals.jsonl"
    event = {
        "plan_sha256": _plan_sha256(plan),
        "candidate_id": candidate_id,
        "status": status,
        "reviewer_notes": note,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with approvals_path.open("a", encoding="utf-8", newline="") as approvals:
            approvals.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise EditPlanError("Could not append the candidate decision.") from exc
    return approvals_path


def _load_approvals(plan_path: Path) -> dict[str, dict[str, str]]:
    approvals_path = plan_path.parent / "approvals.jsonl"
    latest: dict[str, dict[str, str]] = {}
    if not approvals_path.exists():
        return latest
    try:
        lines = approvals_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EditPlanError("Could not read edit-plan approval history.") from exc
    for number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EditPlanError(f"Approval history line {number} is invalid JSON.") from exc
        if not isinstance(event, dict):
            raise EditPlanError(f"Approval history line {number} is invalid.")
        candidate_id = event.get("candidate_id")
        status = event.get("status")
        if (
            not isinstance(candidate_id, str)
            or not isinstance(status, str)
            or status not in APPROVAL_STATUSES
        ):
            raise EditPlanError(f"Approval history line {number} has invalid fields.")
        latest[candidate_id] = event
    current_digest = _plan_sha256(plan_path)
    if any(event.get("plan_sha256") != current_digest for event in latest.values()):
        raise EditPlanError(
            "Approval history is stale: edit plan contents changed after review."
        )
    return latest


def _validate_plan_source(payload: dict[str, Any]) -> tuple[Path, float]:
    source = payload["source"]
    source_path = Path(str(source.get("path", ""))).expanduser().resolve()
    if not source_path.is_file():
        raise EditPlanError("The source recorded in the plan no longer exists.")
    actual_size = source_path.stat().st_size
    actual_hash = _sha256_file(source_path)
    if actual_size != source.get("size_bytes") or actual_hash != source.get("sha256"):
        raise EditPlanError("Edit plan is stale: source size or SHA-256 has changed.")
    try:
        duration = float(source["duration_seconds"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise EditPlanError("Edit plan source duration is unavailable.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise EditPlanError("Edit plan source duration is invalid.")
    return source_path, duration


def validate_approved_cuts(
    payload: dict[str, Any],
    approvals: dict[str, dict[str, str]],
) -> tuple[tuple[float, float], ...]:
    """Validate approved cut ranges against original-source bounds and conflicts."""
    _, duration = _validate_plan_source(payload)
    plan_candidates = payload["candidates"]
    candidate_ids: set[str] = set()
    for item in plan_candidates:
        if not isinstance(item, dict):
            raise EditPlanError("Edit plan contains an invalid candidate.")
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise EditPlanError("Edit plan contains a candidate with an invalid ID.")
        if candidate_id in candidate_ids:
            raise EditPlanError(f"Edit plan contains duplicate candidate ID {candidate_id}.")
        candidate_ids.add(candidate_id)
    if any(candidate_id not in candidate_ids for candidate_id in approvals):
        raise EditPlanError("Approval history contains an unknown candidate ID.")

    approved: list[tuple[float, float]] = []
    for candidate in plan_candidates:
        if not isinstance(candidate, dict):
            raise EditPlanError("Edit plan contains an invalid candidate.")
        candidate_id = candidate.get("candidate_id")
        status = approvals.get(candidate_id, {}).get("status", "PENDING")
        if not isinstance(status, str) or status not in APPROVAL_STATUSES:
            raise EditPlanError(f"Candidate {candidate_id} has invalid approval status.")
        if status != "APPROVED":
            continue
        cut = candidate.get("proposed_cut_seconds")
        if not isinstance(cut, list) or len(cut) != 2:
            raise EditPlanError(f"Candidate {candidate_id} has invalid cut bounds.")
        try:
            start, end = float(cut[0]), float(cut[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise EditPlanError(f"Candidate {candidate_id} has nonnumeric cut bounds.") from exc
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end > duration
            or end <= start
            or end - start > MAX_CANDIDATE_CUT_SECONDS
        ):
            raise EditPlanError(f"Candidate {candidate_id} has invalid or out-of-bounds cut.")
        try:
            source_start = float(candidate.get("source_start_seconds", -1))
            source_end = float(candidate.get("source_end_seconds", -1))
        except (TypeError, ValueError, OverflowError) as exc:
            raise EditPlanError(
                f"Candidate {candidate_id} has invalid detected source bounds."
            ) from exc
        if (
            not math.isfinite(source_start)
            or not math.isfinite(source_end)
            or source_start < 0
            or source_end <= source_start
        ):
            raise EditPlanError(
                f"Candidate {candidate_id} has invalid detected source bounds."
            )
        if start < source_start or end > source_end:
            raise EditPlanError(
                f"Candidate {candidate_id} cut extends beyond its detected source interval."
            )
        approved.append((start, end))

    approved.sort()
    for previous, current in zip(approved, approved[1:]):
        if current[0] < previous[1]:
            raise EditPlanError("Approved cuts overlap or conflict.")
    return tuple(approved)


def _retained_ranges(
    cuts: tuple[tuple[float, float], ...],
    duration: float,
) -> tuple[tuple[float, float], ...]:
    retained: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor:
            retained.append((cursor, start))
        cursor = end
    if cursor < duration:
        retained.append((cursor, duration))
    if not retained:
        raise EditPlanError("Approved cuts would remove the entire video.")
    return tuple(retained)


def _run_ffmpeg(
    command: list[str],
    *,
    timeout: float = 3600,
) -> subprocess.CompletedProcess[str]:
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
        raise PostProductionError("FFmpeg timed out during post-production.") from exc
    except OSError as exc:
        raise PostProductionError("Could not start the configured FFmpeg executable.") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-2000:]
        raise PostProductionError(
            "FFmpeg post-production failed."
            + (f" {detail}" if detail else "")
        )
    return result


def _available_filters(
    ffmpeg: Path,
    required: set[str] | frozenset[str],
) -> None:
    result = _run_ffmpeg(
        [str(ffmpeg), "-hide_banner", "-filters"],
        timeout=30,
    )
    available = {
        line.split()[1]
        for line in result.stdout.splitlines()
        if len(line.split()) >= 2
        and len(line.split()[0]) == 2
        and all(flag in ".TSC." for flag in line.split()[0])
    }
    missing = sorted(set(required) - available)
    if missing:
        raise PostProductionError(
            "Configured FFmpeg lacks required filters: " + ", ".join(missing)
        )


def _probe_music(
    path: str | Path,
    *,
    settings_path: str | Path | None,
) -> MusicMetadata:
    music_path = Path(path).expanduser().resolve()
    if not music_path.is_file():
        raise PostProductionError(f"Music file does not exist: {music_path.name}")
    executable = _resolve_ffprobe(None, settings_path)
    command = [
        str(executable),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(music_path),
    ]
    result = _run_ffmpeg(command, timeout=60)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PostProductionError("FFprobe returned invalid music metadata.") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise PostProductionError("FFprobe did not return music stream metadata.")
    audio = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ),
        None,
    )
    if audio is None:
        raise PostProductionError("Music input contains no readable audio stream.")
    try:
        duration = float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError, AttributeError):
        duration = None
    return MusicMetadata(
        path=music_path,
        sha256=_sha256_file(music_path),
        duration_seconds=duration if duration is not None and math.isfinite(duration) else None,
        codec=audio.get("codec_name"),
        sample_rate_hz=_int_or_none(audio.get("sample_rate")),
        channels=_int_or_none(audio.get("channels")),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _resolve_render_output(source: Path, output: str | Path | None) -> Path:
    resolved = (
        Path(output).expanduser().resolve()
        if output is not None
        else source.with_name(f"{source.stem}_postproduction_preview.mp4")
    )
    if resolved == source:
        raise PostProductionError("Refusing to write output over the source video.")
    if resolved.suffix.lower() != ".mp4":
        raise PostProductionError("Preview output must use the .mp4 extension.")
    return resolved


def _validate_render_destinations(output: Path, music: MusicMetadata | None) -> tuple[Path, Path | None]:
    render_report = output.with_suffix(".render-report.json")
    provenance = output.with_suffix(".music-provenance.json") if music is not None else None
    for path in (output, render_report, provenance):
        if path is not None and path.exists():
            raise PostProductionError(
                f"Refusing to overwrite existing output: {path.name}"
            )
    return render_report, provenance


def _build_filter_graph(
    retained: tuple[tuple[float, float], ...],
    *,
    has_source_audio: bool,
    audio_target_lufs: float,
    true_peak_dbtp: float,
    music: MusicMetadata | None,
    music_gain_db: float,
    fade_in_seconds: float,
    fade_out_seconds: float,
    video_correction: tuple[float, float, float] | None,
    white_balance: tuple[float, float, float] | None = None,
    music_ducking_enabled: bool = False,
    music_ducking_threshold_db: float = -30.0,
    music_ducking_ratio: float = 8.0,
    music_ducking_attack_ms: float = 20.0,
    music_ducking_release_ms: float = 350.0,
    frame_rate_hz: float = 30.0,
) -> tuple[str, list[str]]:
    if not -30 <= audio_target_lufs <= -14:
        raise ValueError("audio_target_lufs must be between -30 and -14 LUFS.")
    if not -9 <= true_peak_dbtp <= -1:
        raise ValueError("true_peak_dbtp must be between -9 and -1 dBTP.")
    if not -36 <= music_gain_db <= -6:
        raise ValueError("music_gain_db must be between -36 and -6 dB.")
    if not math.isfinite(frame_rate_hz) or frame_rate_hz <= 0:
        raise ValueError("frame_rate_hz must be positive and finite.")
    if (
        not math.isfinite(fade_in_seconds)
        or not math.isfinite(fade_out_seconds)
        or fade_in_seconds < 0
        or fade_out_seconds < 0
    ):
        raise ValueError("Music fade durations must be finite and non-negative.")
    if video_correction is not None:
        brightness, contrast, saturation = video_correction
        if not -0.1 <= brightness <= 0.1:
            raise ValueError("brightness correction must be between -0.1 and 0.1.")
        if not 0.8 <= contrast <= 1.2:
            raise ValueError("contrast correction must be between 0.8 and 1.2.")
        if not 0.8 <= saturation <= 1.2:
            raise ValueError("saturation correction must be between 0.8 and 1.2.")
    if white_balance is not None:
        if len(white_balance) != 3 or any(
            not math.isfinite(value) or not -0.1 <= value <= 0.1
            for value in white_balance
        ):
            raise ValueError(
                "White-balance RGB adjustments must be finite and between -0.1 and 0.1."
            )
    if (
        not isinstance(music_ducking_enabled, bool)
        or not math.isfinite(music_ducking_threshold_db)
        or not -60 <= music_ducking_threshold_db <= -6
        or not math.isfinite(music_ducking_ratio)
        or not 1 <= music_ducking_ratio <= 20
        or not math.isfinite(music_ducking_attack_ms)
        or not 1 <= music_ducking_attack_ms <= 2000
        or not math.isfinite(music_ducking_release_ms)
        or not 1 <= music_ducking_release_ms <= 9000
    ):
        raise ValueError("Music ducking settings are outside supported bounds.")

    filters: list[str] = []
    graph: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    duration = sum(end - start for start, end in retained)
    for index, (start, end) in enumerate(retained):
        video_label = f"v{index}"
        vf = (
            f"[0:v:0]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
            f"fps={frame_rate_hz:.6f},settb=AVTB"
        )
        filters.extend(("fps", "settb"))
        if video_correction is not None:
            brightness, contrast, saturation = video_correction
            vf += (
                f",eq=brightness={brightness:.4f}:contrast={contrast:.4f}:"
                f"saturation={saturation:.4f}"
            )
            filters.append("eq")
        if white_balance is not None:
            red, green, blue = white_balance
            vf += (
                f",colorbalance=rs={red:.4f}:gs={green:.4f}:bs={blue:.4f}:"
                f"rm={red:.4f}:gm={green:.4f}:bm={blue:.4f}:"
                f"rh={red:.4f}:gh={green:.4f}:bh={blue:.4f}"
            )
            filters.append("colorbalance")
        graph.append(f"{vf}[{video_label}]")
        video_labels.append(f"[{video_label}]")

        if has_source_audio:
            audio_label = f"a{index}"
            audio_filters = [
                f"[0:a:0]atrim=start={start:.6f}:end={end:.6f}",
                "asetpts=PTS-STARTPTS",
                "aformat=sample_rates=48000:channel_layouts=stereo",
                "aresample=48000",
            ]
            if len(retained) > 1:
                segment_duration = end - start
                fade_duration = min(0.01, segment_duration / 2)
                if index > 0:
                    audio_filters.append(f"afade=t=in:st=0:d={fade_duration:.4f}")
                if index < len(retained) - 1:
                    audio_filters.append(
                        f"afade=t=out:st={max(0.0, segment_duration-fade_duration):.6f}:"
                        f"d={fade_duration:.4f}"
                    )
                filters.append("afade")
            graph.append(f"{','.join(audio_filters)}[{audio_label}]")
            audio_labels.append(f"[{audio_label}]")
            filters.extend(("aformat", "aresample"))

    if len(retained) == 1:
        graph.append(f"{video_labels[0]}null[vout]")
    else:
        graph.append(
            "".join(video_labels)
            + f"concat=n={len(retained)}:v=1:a=0[vout]"
        )
        filters.extend(("concat",))

    if has_source_audio:
        if len(audio_labels) == 1:
            graph.append(f"{audio_labels[0]}anull[voice]")
        else:
            graph.append(
                "".join(audio_labels)
                + f"concat=n={len(retained)}:v=0:a=1[voice]"
            )
            filters.append("concat")
        filters.append("anull")

    if music is not None:
        if not has_source_audio:
            raise PostProductionError(
                "Music mixing requires a source voice audio stream; music will not replace it."
            )
        duration = sum(end - start for start, end in retained)
        music_filters = [
            f"atrim=duration={duration:.6f}",
            "asetpts=PTS-STARTPTS",
            "aformat=sample_rates=48000:channel_layouts=stereo",
            "aresample=48000",
            f"volume={music_gain_db:.2f}dB",
        ]
        if fade_in_seconds > 0:
            music_filters.append(f"afade=t=in:st=0:d={fade_in_seconds:.3f}")
        if fade_out_seconds > 0:
            fade_start = max(0.0, duration - fade_out_seconds)
            music_filters.append(
                f"afade=t=out:st={fade_start:.3f}:d={min(fade_out_seconds, duration):.3f}"
            )
        graph.append(f"[1:a:0]{','.join(music_filters)}[music]")
        music_label = "[music]"
        if music_ducking_enabled:
            threshold = 10 ** (music_ducking_threshold_db / 20)
            graph.append(
                "[voice]asplit=2[voice_mix][voice_sc];"
                f"[music][voice_sc]sidechaincompress=threshold={threshold:.8f}:"
                f"ratio={music_ducking_ratio:.3f}:attack={music_ducking_attack_ms:.3f}:"
                f"release={music_ducking_release_ms:.3f}:makeup=1[music_ducked]"
            )
            music_label = "[music_ducked]"
            voice_label = "[voice_mix]"
            filters.extend(("sidechaincompress", "asplit"))
        else:
            voice_label = "[voice]"
        graph.append(
            f"{voice_label}{music_label}amix=inputs=2:duration=first:dropout_transition=2:"
            "weights='1 1':normalize=0[mix_raw]"
        )
        filters.extend(("atrim", "aformat", "aresample", "volume", "afade", "amix"))
        audio_input = "[mix_raw]"
    elif has_source_audio:
        audio_input = "[voice]"
    else:
        audio_input = ""
    if audio_input:
        graph.append(
            f"{audio_input}loudnorm=I={audio_target_lufs:.2f}:"
            f"TP={true_peak_dbtp:.2f}:LRA=11:linear=false:print_format=json,"
            "alimiter=limit=0.95:attack=5:release=50,aresample=48000,"
            f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[aout]"
        )
        filters.extend(("loudnorm", "alimiter", "aresample", "atrim", "asetpts"))
    return ";".join(graph), filters


def _build_render_command(
    ffmpeg: Path,
    source: Path,
    temp_output: Path,
    *,
    retained: tuple[tuple[float, float], ...],
    has_source_audio: bool,
    audio_target_lufs: float,
    true_peak_dbtp: float,
    music: MusicMetadata | None,
    music_gain_db: float,
    fade_in_seconds: float,
    fade_out_seconds: float,
    video_correction: tuple[float, float, float] | None,
    white_balance: tuple[float, float, float] | None = None,
    music_ducking_enabled: bool = False,
    music_ducking_threshold_db: float = -30.0,
    music_ducking_ratio: float = 8.0,
    music_ducking_attack_ms: float = 20.0,
    music_ducking_release_ms: float = 350.0,
    frame_rate_hz: float = 30.0,
) -> tuple[list[str], list[str]]:
    graph, filters = _build_filter_graph(
        retained,
        has_source_audio=has_source_audio,
        audio_target_lufs=audio_target_lufs,
        true_peak_dbtp=true_peak_dbtp,
        music=music,
        music_gain_db=music_gain_db,
        fade_in_seconds=fade_in_seconds,
        fade_out_seconds=fade_out_seconds,
        video_correction=video_correction,
        white_balance=white_balance,
        music_ducking_enabled=music_ducking_enabled,
        music_ducking_threshold_db=music_ducking_threshold_db,
        music_ducking_ratio=music_ducking_ratio,
        music_ducking_attack_ms=music_ducking_attack_ms,
        music_ducking_release_ms=music_ducking_release_ms,
        frame_rate_hz=frame_rate_hz,
    )
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(source),
    ]
    if music is not None:
        command.extend(("-stream_loop", "-1", "-i", str(music.path)))
    command.extend(
        (
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
        )
    )
    if has_source_audio or music is not None:
        command.extend(("-map", "[aout]"))
    command.extend(
        (
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
        )
    )
    if has_source_audio or music is not None:
        command.extend(("-c:a", "aac", "-b:a", "192k", "-ar", "48000"))
    command.extend(("-movflags", "+faststart", str(temp_output)))
    return command, filters


def _atomic_publish(temp_output: Path, output: Path) -> None:
    try:
        os.link(temp_output, output)
    except FileExistsError as exc:
        raise PostProductionError(
            f"Output appeared during rendering; refusing to overwrite {output.name}."
        ) from exc
    except OSError as exc:
        if os.name != "nt":
            raise PostProductionError(
                "Could not safely publish the rendered preview without overwriting."
            ) from exc
        try:
            os.rename(temp_output, output)
        except FileExistsError as collision:
            raise PostProductionError(
                f"Output appeared during rendering; refusing to overwrite {output.name}."
            ) from collision
        except OSError as rename_error:
            raise PostProductionError(
                "Could not safely publish the rendered preview without overwriting."
            ) from rename_error
    temp_output.unlink(missing_ok=True)


def _link_output_without_overwrite(staged: Path, destination: Path) -> None:
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise PostProductionError(
            f"Output appeared during rendering; refusing to overwrite {destination.name}."
        ) from exc
    except OSError as exc:
        if os.name != "nt":
            raise PostProductionError(
                f"Could not safely publish {destination.name} without overwriting."
            ) from exc
        try:
            os.rename(staged, destination)
        except FileExistsError as collision:
            raise PostProductionError(
                f"Output appeared during rendering; refusing to overwrite {destination.name}."
            ) from collision
        except OSError as rename_error:
            raise PostProductionError(
                f"Could not safely publish {destination.name} without overwriting."
            ) from rename_error


def _validate_rendered_output(
    output: Path,
    *,
    settings_path: str | Path | None,
    expected_duration: float,
    expect_audio: bool,
) -> VideoMetadata:
    if not output.is_file() or output.stat().st_size == 0:
        raise RenderValidationError("FFmpeg did not produce a non-empty preview.")
    metadata = probe_video(output, settings_path=settings_path)
    if metadata.video.width is None or metadata.video.height is None:
        raise RenderValidationError("Rendered preview has no valid video dimensions.")
    if expect_audio and metadata.audio is None:
        raise RenderValidationError("Rendered preview is missing its audio stream.")
    if expect_audio and metadata.audio is not None and metadata.audio.sample_rate_hz != 48_000:
        raise RenderValidationError("Rendered preview audio was not normalized to 48 kHz.")
    if not expect_audio and metadata.audio is not None:
        raise RenderValidationError("Rendered preview unexpectedly contains audio.")
    _validate_output_timeline(metadata, expected_duration)
    return metadata


def _validate_output_timeline(
    metadata: VideoMetadata,
    expected_duration: float,
) -> dict[str, Any]:
    if not math.isfinite(expected_duration) or expected_duration < 0:
        raise RenderValidationError("Expected output duration must be finite and non-negative.")
    frame_rate = metadata.video.frame_rate
    if frame_rate is None or not math.isfinite(frame_rate) or frame_rate <= 0:
        raise RenderValidationError(
            "Rendered preview has no usable frame rate for duration validation."
        )
    sample_rate = metadata.audio.sample_rate_hz if metadata.audio is not None else None
    frame_tolerance = 1.5 / frame_rate
    sample_tolerance = 2 / sample_rate if sample_rate is not None and sample_rate > 0 else 0.0
    duration_tolerance = frame_tolerance + sample_tolerance
    if (
        metadata.duration_seconds is None
        or not math.isfinite(metadata.duration_seconds)
        or abs(metadata.duration_seconds - expected_duration) > duration_tolerance
    ):
        raise RenderValidationError(
            "Rendered preview duration differs from the approved plan by more than "
            f"{duration_tolerance:.4f} seconds."
        )

    sync_tolerance = max(frame_tolerance, sample_tolerance)
    video_duration = metadata.video.duration_seconds
    audio_duration = metadata.audio.duration_seconds if metadata.audio is not None else None
    duration_difference = None
    warnings: list[str] = []
    if video_duration is not None and audio_duration is not None:
        duration_difference = abs(video_duration - audio_duration)
        if duration_difference > sync_tolerance:
            raise RenderValidationError(
                "Rendered preview audio/video stream durations differ by "
                f"{duration_difference:.4f} seconds."
            )
    elif metadata.audio is not None:
        warnings.append("FFprobe did not report both audio and video stream durations.")
    video_start = metadata.video.start_time_seconds
    audio_start = metadata.audio.start_time_seconds if metadata.audio is not None else None
    start_offset = None
    if video_start is not None and audio_start is not None:
        start_offset = audio_start - video_start
        if abs(start_offset) > sync_tolerance:
            raise RenderValidationError(
                "Rendered preview audio/video stream start times differ by "
                f"{abs(start_offset):.4f} seconds."
            )
    elif metadata.audio is not None:
        warnings.append("FFprobe did not report both audio and video stream start times.")
    return {
        "expected_duration_seconds": expected_duration,
        "container_duration_seconds": metadata.duration_seconds,
        "duration_difference_seconds": abs(
            metadata.duration_seconds - expected_duration
        ),
        "duration_tolerance_seconds": duration_tolerance,
        "video_stream_duration_seconds": video_duration,
        "audio_stream_duration_seconds": audio_duration,
        "audio_video_duration_difference_seconds": duration_difference,
        "video_stream_start_time_seconds": video_start,
        "audio_stream_start_time_seconds": audio_start,
        "audio_video_start_offset_seconds": start_offset,
        "audio_video_sync_tolerance_seconds": sync_tolerance,
        "warnings": warnings,
    }


def _measure_audio_quality(
    path: Path,
    *,
    ffmpeg: Path,
    target_lufs: float,
    loudness_warning_tolerance_lu: float,
    true_peak_warning_dbtp: float,
) -> dict[str, Any]:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        "ebur128=peak=true:framelog=quiet",
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
            timeout=3600,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderValidationError("Audio loudness measurement timed out.") from exc
    except OSError as exc:
        raise RenderValidationError(
            "Could not start FFmpeg audio loudness measurement."
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-1500:]
        raise RenderValidationError(
            "FFmpeg could not measure rendered audio."
            + (f" {detail}" if detail else "")
        )

    integrated_match = re.search(
        r"^\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS\s*$",
        result.stderr,
        re.IGNORECASE | re.MULTILINE,
    )
    peak_match = re.search(
        r"^\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dBFS\s*$",
        result.stderr,
        re.IGNORECASE | re.MULTILINE,
    )

    def parsed_measurement(match: re.Match[str] | None) -> float | None:
        if match is None:
            return None
        value = float(match.group(1))
        return value if math.isfinite(value) else None

    integrated_loudness = parsed_measurement(integrated_match)
    true_peak = parsed_measurement(peak_match)
    if integrated_match is None or peak_match is None:
        raise RenderValidationError(
            "FFmpeg loudness output did not contain integrated LUFS and true peak."
        )

    warnings: list[str] = []
    lower_loudness = target_lufs - loudness_warning_tolerance_lu
    upper_loudness = target_lufs + loudness_warning_tolerance_lu
    if integrated_loudness is None:
        warnings.append("Integrated loudness is unavailable, likely because the audio is silent.")
    elif not lower_loudness <= integrated_loudness <= upper_loudness:
        warnings.append(
            f"Integrated loudness {integrated_loudness:.1f} LUFS is outside the "
            f"configured warning range [{lower_loudness:.1f}, {upper_loudness:.1f}] LUFS."
        )
    if true_peak is None:
        warnings.append("True peak is unavailable, likely because the audio is silent.")
    elif true_peak > true_peak_warning_dbtp:
        warnings.append(
            f"True peak {true_peak:.1f} dBTP exceeds the configured warning ceiling "
            f"of {true_peak_warning_dbtp:.1f} dBTP."
        )
    return {
        "measurement_filter": "ebur128=peak=true",
        "integrated_loudness_lufs": integrated_loudness,
        "true_peak_dbtp": true_peak,
        "warning_thresholds": {
            "target_loudness_lufs": target_lufs,
            "loudness_warning_tolerance_lu": loudness_warning_tolerance_lu,
            "true_peak_warning_dbtp": true_peak_warning_dbtp,
        },
        "warnings": warnings,
    }


def _music_provenance(
    music: MusicMetadata,
    *,
    source_url: str,
    provider: str,
    license_name: str,
    attribution: str,
    evidence: str,
    user_confirmed: bool,
) -> dict[str, Any]:
    complete_confirmation = bool(
        user_confirmed and license_name.strip() and evidence.strip()
    )
    return {
        "track_filename": music.path.name,
        "track_sha256": music.sha256,
        "duration_seconds": music.duration_seconds,
        "audio_properties": {
            "codec": music.codec,
            "sample_rate_hz": music.sample_rate_hz,
            "channels": music.channels,
        },
        "source_url": source_url or None,
        "provider": provider or None,
        "license_name_or_identifier": license_name or None,
        "attribution_requirements": attribution or None,
        "license_evidence_reference": evidence or None,
        "user_confirmed_permitted_for_intended_youtube_social_and_monetized_use": user_confirmed,
        "status": "USER_CONFIRMED" if complete_confirmation else "UNVERIFIED",
        "warnings": [
            (
                "User confirmation records the user's statement only; it is not "
                "independent legal verification and does not prevent platform claims."
                if complete_confirmation
                else "Licence information or explicit user confirmation is missing. "
                "Use only as a private test preview; do not publish."
            )
        ],
    }


def _required_render_filters(
    *,
    retained_range_count: int,
    has_source_audio: bool,
    has_music: bool,
    has_fades: bool,
    video_correction: bool,
    white_balance: bool,
    music_ducking: bool = False,
) -> set[str]:
    required = set(REQUIRED_FILTERS)
    if retained_range_count > 1:
        required.add("concat")
    if has_source_audio:
        required.update(
            {"atrim", "asetpts", "aformat", "aresample", "loudnorm", "alimiter"}
        )
        if retained_range_count > 1:
            required.add("afade")
    if has_music:
        required.update({"atrim", "volume", "amix", "alimiter"})
        if has_fades:
            required.add("afade")
        if music_ducking:
            required.add("sidechaincompress")
    if video_correction:
        required.update(VIDEO_FILTERS)
    if white_balance:
        required.update(WHITE_BALANCE_FILTERS)
    return required


def render_approved_preview(
    plan_path: str | Path,
    output_path: str | Path | None = None,
    *,
    settings_path: str | Path | None = None,
    dry_run: bool = False,
    audio_target_lufs: float = -20.0,
    true_peak_dbtp: float = -2.0,
    loudness_warning_tolerance_lu: float = 3.0,
    true_peak_warning_dbtp: float = -1.0,
    music_ducking_enabled: bool = False,
    music_ducking_threshold_db: float = -30.0,
    music_ducking_ratio: float = 8.0,
    music_ducking_attack_ms: float = 20.0,
    music_ducking_release_ms: float = 350.0,
    video_correction: tuple[float, float, float] | None = None,
    white_balance: tuple[float, float, float] | None = None,
    music_path: str | Path | None = None,
    music_gain_db: float = -24.0,
    music_fade_in_seconds: float = 1.0,
    music_fade_out_seconds: float = 2.0,
    music_source_url: str = "",
    music_provider: str = "",
    music_license: str = "",
    music_attribution: str = "",
    music_evidence: str = "",
    confirm_music_license: bool = False,
) -> dict[str, Any]:
    """Dry-run or explicitly render a new, validated preview from approved cuts."""
    payload, plan = load_plan(plan_path)
    source, duration = _validate_plan_source(payload)
    approvals = _load_approvals(plan)
    cuts = validate_approved_cuts(payload, approvals)
    retained = _retained_ranges(cuts, duration)
    expected_duration = sum(end - start for start, end in retained)
    if expected_duration <= 0:
        raise EditPlanError("Approved cuts leave no video to render.")
    if (
        not math.isfinite(loudness_warning_tolerance_lu)
        or not 0 <= loudness_warning_tolerance_lu <= 12
        or not math.isfinite(true_peak_warning_dbtp)
        or not -9 <= true_peak_warning_dbtp <= 0
    ):
        raise ValueError("Audio validation thresholds are outside supported bounds.")

    snapshot = _snapshot_source(source, settings_path=settings_path)
    if snapshot.sha256 != payload["source"]["sha256"]:
        raise EditPlanError("Edit plan is stale: source hash has changed.")
    metadata = snapshot.metadata
    if music_path is not None and metadata.audio is None:
        raise PostProductionError("Cannot mix music without preserving source voice audio.")
    music = (
        _probe_music(music_path, settings_path=settings_path)
        if music_path is not None
        else None
    )
    output = _resolve_render_output(source, output_path)
    render_report, provenance_path = _validate_render_destinations(output, music)

    executable = _resolve_ffmpeg(settings_path)
    required_filters = _required_render_filters(
        retained_range_count=len(retained),
        has_source_audio=metadata.audio is not None,
        has_music=music is not None,
        has_fades=music_fade_in_seconds > 0 or music_fade_out_seconds > 0,
        video_correction=video_correction is not None,
        white_balance=white_balance is not None,
        music_ducking=music is not None and music_ducking_enabled,
    )
    _available_filters(executable, required_filters)

    temp_output = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.partial.mp4")
    command, filters = _build_render_command(
        executable,
        source,
        temp_output,
        retained=retained,
        has_source_audio=metadata.audio is not None,
        audio_target_lufs=audio_target_lufs,
        true_peak_dbtp=true_peak_dbtp,
        music=music,
        music_gain_db=music_gain_db,
        fade_in_seconds=music_fade_in_seconds,
        fade_out_seconds=music_fade_out_seconds,
        video_correction=video_correction,
        white_balance=white_balance,
        music_ducking_enabled=music_ducking_enabled,
        music_ducking_threshold_db=music_ducking_threshold_db,
        music_ducking_ratio=music_ducking_ratio,
        music_ducking_attack_ms=music_ducking_attack_ms,
        music_ducking_release_ms=music_ducking_release_ms,
        frame_rate_hz=metadata.video.frame_rate or 30.0,
    )
    if dry_run:
        if temp_output.exists():
            raise PostProductionError("Temporary dry-run path unexpectedly exists.")
        return {
            "dry_run": True,
            "source": str(source),
            "source_sha256": snapshot.sha256,
            "approved_cut_ranges_seconds": [list(item) for item in cuts],
            "retained_source_ranges_seconds": [list(item) for item in retained],
            "expected_duration_seconds": expected_duration,
            "filters": filters,
            "command": command,
            "output": str(output),
            "music_status": (
                "UNVERIFIED"
                if music is not None
                and not (
                    confirm_music_license and music_license.strip() and music_evidence.strip()
                )
                else "USER_CONFIRMED" if music is not None else None
            ),
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    staged_report = output.with_name(f".{render_report.name}.{uuid.uuid4().hex}.partial")
    staged_provenance = (
        output.with_name(f".{provenance_path.name}.{uuid.uuid4().hex}.partial")
        if provenance_path is not None
        else None
    )
    try:
        _run_ffmpeg(command)
        rendered_metadata = _validate_rendered_output(
            temp_output,
            settings_path=settings_path,
            expected_duration=expected_duration,
            expect_audio=metadata.audio is not None or music is not None,
        )
        timeline_validation = _validate_output_timeline(
            rendered_metadata,
            expected_duration,
        )
        audio_quality = (
            _measure_audio_quality(
                temp_output,
                ffmpeg=executable,
                target_lufs=audio_target_lufs,
                loudness_warning_tolerance_lu=loudness_warning_tolerance_lu,
                true_peak_warning_dbtp=true_peak_warning_dbtp,
            )
            if rendered_metadata.audio is not None
            else {
                "measurement_filter": None,
                "integrated_loudness_lufs": None,
                "true_peak_dbtp": None,
                "warning_thresholds": {
                    "target_loudness_lufs": audio_target_lufs,
                    "loudness_warning_tolerance_lu": loudness_warning_tolerance_lu,
                    "true_peak_warning_dbtp": true_peak_warning_dbtp,
                },
                "warnings": ["Output has no audio stream to measure."],
            }
        )
        audio_quality["warnings"].extend(timeline_validation["warnings"])
        if music is not None and _sha256_file(music.path) != music.sha256:
            raise PostProductionError("Music file changed during processing.")
        _assert_source_unchanged(snapshot)
        report = {
            "version": 1,
            "source": snapshot.to_dict(),
            "edit_plan": str(plan),
            "approved_cut_ranges_seconds": [list(item) for item in cuts],
            "retained_source_ranges_seconds": [list(item) for item in retained],
            "expected_duration_seconds": expected_duration,
            "output": str(output),
            "output_metadata": rendered_metadata.to_dict(),
            "output_sha256": _sha256_file(temp_output),
            "timeline_validation": timeline_validation,
            "audio_quality": audio_quality,
            "selected_filters": filters,
            "music_ducking": {
                "enabled": music is not None and music_ducking_enabled,
                "threshold_db": music_ducking_threshold_db,
                "ratio": music_ducking_ratio,
                "attack_ms": music_ducking_attack_ms,
                "release_ms": music_ducking_release_ms,
            },
            "audio_target_lufs": audio_target_lufs,
            "true_peak_dbtp": true_peak_dbtp,
            "loudness_warning_tolerance_lu": loudness_warning_tolerance_lu,
            "true_peak_warning_dbtp": true_peak_warning_dbtp,
            "video_correction": (
                {
                    "brightness": video_correction[0],
                    "contrast": video_correction[1],
                    "saturation": video_correction[2],
                }
                if video_correction is not None
                else None
            ),
            "white_balance_rgb": list(white_balance) if white_balance is not None else None,
            "music_provenance": (
                str(provenance_path) if provenance_path is not None else None
            ),
            "validated": True,
        }
        _atomic_create_json(staged_report, report)
        if music is not None and staged_provenance is not None:
            _atomic_create_json(
                staged_provenance,
                _music_provenance(
                    music,
                    source_url=music_source_url,
                    provider=music_provider,
                    license_name=music_license,
                    attribution=music_attribution,
                    evidence=music_evidence,
                    user_confirmed=confirm_music_license,
                ),
            )

        published_sidecars: list[Path] = []
        try:
            _link_output_without_overwrite(staged_report, render_report)
            published_sidecars.append(render_report)
            if (
                music is not None
                and staged_provenance is not None
                and provenance_path is not None
            ):
                _link_output_without_overwrite(staged_provenance, provenance_path)
                published_sidecars.append(provenance_path)
            _atomic_publish(temp_output, output)
        except Exception:
            for sidecar in published_sidecars:
                sidecar.unlink(missing_ok=True)
            raise
    except Exception:
        temp_output.unlink(missing_ok=True)
        staged_report.unlink(missing_ok=True)
        if staged_provenance is not None:
            staged_provenance.unlink(missing_ok=True)
        raise
    temp_output.unlink(missing_ok=True)
    staged_report.unlink(missing_ok=True)
    if staged_provenance is not None:
        staged_provenance.unlink(missing_ok=True)
    return {
        "dry_run": False,
        "output": str(output),
        "render_report": str(render_report),
        "music_provenance": str(provenance_path) if provenance_path is not None else None,
        "metadata": rendered_metadata.to_dict(),
    }


def _parse_video_correction(args: argparse.Namespace) -> tuple[float, float, float] | None:
    if not args.video_correction:
        return None
    return args.brightness, args.contrast, args.saturation


def _parse_white_balance(args: argparse.Namespace) -> tuple[float, float, float] | None:
    if not args.white_balance:
        return None
    return args.white_balance_red, args.white_balance_green, args.white_balance_blue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Approval-gated local edit planning and technical preview rendering."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Create a review-only edit plan.")
    analyze_parser.add_argument("video", type=Path)
    analyze_parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    analyze_parser.add_argument("--padding-before", type=float, default=0.25)
    analyze_parser.add_argument("--padding-after", type=float, default=0.25)
    analyze_parser.add_argument("--minimum-cut", type=float, default=0.25)

    review_parser = subparsers.add_parser("review", help="Record a cut approval decision.")
    review_parser.add_argument("plan", type=Path)
    review_parser.add_argument("candidate_id")
    review_parser.add_argument("status", choices=("APPROVED", "REJECTED", "PENDING"))
    review_parser.add_argument("--note", default="")

    for command in ("dry-run", "render"):
        render_parser = subparsers.add_parser(
            command,
            help="Show or render the explicitly approved technical preview.",
        )
        render_parser.add_argument("plan", type=Path)
        render_parser.add_argument("--out", type=Path)
        render_parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
        render_parser.add_argument("--audio-target-lufs", type=float, default=-20.0)
        render_parser.add_argument("--true-peak-dbtp", type=float, default=-2.0)
        render_parser.add_argument("--loudness-warning-tolerance-lu", type=float, default=3.0)
        render_parser.add_argument("--true-peak-warning-dbtp", type=float, default=-1.0)
        render_parser.add_argument("--video-correction", action="store_true")
        render_parser.add_argument("--brightness", type=float, default=0.0)
        render_parser.add_argument("--contrast", type=float, default=1.0)
        render_parser.add_argument("--saturation", type=float, default=1.0)
        render_parser.add_argument("--white-balance", action="store_true")
        render_parser.add_argument("--white-balance-red", type=float, default=0.0)
        render_parser.add_argument("--white-balance-green", type=float, default=0.0)
        render_parser.add_argument("--white-balance-blue", type=float, default=0.0)
        render_parser.add_argument("--music", type=Path)
        render_parser.add_argument("--music-gain-db", type=float, default=-24.0)
        render_parser.add_argument(
            "--music-ducking",
            action="store_true",
            help="Duck music under voice using FFmpeg side-chain compression.",
        )
        render_parser.add_argument("--music-ducking-threshold-db", type=float, default=-30.0)
        render_parser.add_argument("--music-ducking-ratio", type=float, default=8.0)
        render_parser.add_argument("--music-ducking-attack-ms", type=float, default=20.0)
        render_parser.add_argument("--music-ducking-release-ms", type=float, default=350.0)
        render_parser.add_argument("--music-fade-in", type=float, default=1.0)
        render_parser.add_argument("--music-fade-out", type=float, default=2.0)
        render_parser.add_argument("--music-source-url", default="")
        render_parser.add_argument("--music-provider", default="")
        render_parser.add_argument("--music-license", default="")
        render_parser.add_argument("--music-attribution", default="")
        render_parser.add_argument("--music-evidence", default="")
        render_parser.add_argument(
            "--confirm-music-license",
            action="store_true",
            help="Confirm the supplied licence permits intended social/YouTube use, including monetization.",
        )

    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            result = analyze_for_edit_plan(
                args.video,
                settings_path=args.settings,
                padding_before_seconds=args.padding_before,
                padding_after_seconds=args.padding_after,
                minimum_cut_seconds=args.minimum_cut,
            )
            print(f"Edit plan: {result.plan_path}")
            print(f"Review report: {result.report_path}")
            print(f"Candidates: {len(result.candidates)}")
            print("No media changes were applied.")
            return 0
        if args.command == "review":
            path = record_candidate_decision(
                args.plan,
                args.candidate_id,
                args.status,
                note=args.note,
            )
            print(f"Approval recorded in: {path}")
            return 0

        result = render_approved_preview(
            args.plan,
            args.out,
            settings_path=args.settings,
            dry_run=args.command == "dry-run",
            audio_target_lufs=args.audio_target_lufs,
            true_peak_dbtp=args.true_peak_dbtp,
            loudness_warning_tolerance_lu=args.loudness_warning_tolerance_lu,
            true_peak_warning_dbtp=args.true_peak_warning_dbtp,
            video_correction=_parse_video_correction(args),
            white_balance=_parse_white_balance(args),
            music_path=args.music,
            music_gain_db=args.music_gain_db,
            music_ducking_enabled=args.music_ducking,
            music_ducking_threshold_db=args.music_ducking_threshold_db,
            music_ducking_ratio=args.music_ducking_ratio,
            music_ducking_attack_ms=args.music_ducking_attack_ms,
            music_ducking_release_ms=args.music_ducking_release_ms,
            music_fade_in_seconds=args.music_fade_in,
            music_fade_out_seconds=args.music_fade_out,
            music_source_url=args.music_source_url,
            music_provider=args.music_provider,
            music_license=args.music_license,
            music_attribution=args.music_attribution,
            music_evidence=args.music_evidence,
            confirm_music_license=args.confirm_music_license,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        AudioAnalysisError,
        EditPlanError,
        EditPlanOutputExistsError,
        MediaReviewError,
        PostProductionError,
        ProbeError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
