"""Approval-gated local story-edit planning and dual-format rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from namakadhai.audio_analysis import AudioAnalysisError, analyze_audio
from namakadhai.caption_renderer import (
    CaptionCue,
    CaptionError,
    CaptionInputError,
    CaptionStyle,
    _escape_filter_path,
    build_ass,
    load_caption_cues,
    render_threads,
)
from namakadhai.media_review import (
    MediaReviewError,
    _load_settings,
    _resolve_ffmpeg,
    _safe_project_name,
)
from namakadhai.postproduction import (
    APPROVAL_STATUSES,
    EditPlanError,
    MusicMetadata,
    PostProductionError,
    RenderValidationError,
    SourceSnapshot,
    _assert_source_unchanged,
    _make_candidates,
    _measure_audio_quality,
    _probe_music,
    _retained_ranges,
    _sha256_file,
    _snapshot_source,
    _validate_output_timeline,
)
from namakadhai.probe import ProbeError, VideoMetadata, probe_video

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"
DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "nama_kadhai_nostalgic.json"
)
PROJECT_VERSION = 1
OLLAMA_URL = "http://127.0.0.1:11434"
TRANSITION_DURATION_MAX_SECONDS = 0.6
OUTPUT_FORMATS = ("landscape", "vertical")
MAX_SILENCE_CROSSFADE_SECONDS = 0.25
HARD_CUT_AUDIO_FADE_SECONDS = 0.01


class StoryEditorError(RuntimeError):
    """Base error for story-edit plans and renders."""


class StoryPlanError(StoryEditorError):
    """A plan or its approval history is invalid or stale."""


class StoryRenderError(StoryEditorError):
    """A story-edit render failed or did not validate."""


@dataclass(frozen=True)
class CreativeProfile:
    profile_id: str
    name: str
    caption_style: CaptionStyle
    warmth: float
    saturation: float
    contrast: float
    audio_target_lufs: float
    true_peak_dbtp: float
    music_gain_db: float
    music_fade_in_seconds: float
    music_fade_out_seconds: float
    transition_kind: str
    transition_duration_seconds: float
    formats: dict[str, dict[str, float | int | str]]
    music_ducking_enabled: bool = False
    music_ducking_threshold_db: float = -30.0
    music_ducking_ratio: float = 8.0
    music_ducking_attack_ms: float = 20.0
    music_ducking_release_ms: float = 350.0
    loudness_warning_tolerance_lu: float = 3.0
    true_peak_warning_dbtp: float = -1.0


def _read_json(path: Path, *, error_type: type[Exception], label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise error_type(f"Could not read {label}: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise error_type(f"{label.capitalize()} contains invalid JSON.") from exc


def load_creative_profile(path: str | Path | None = None) -> tuple[CreativeProfile, str]:
    profile_path = Path(path or DEFAULT_PROFILE_PATH).expanduser().resolve()
    payload = _read_json(
        profile_path,
        error_type=StoryPlanError,
        label="creative profile",
    )
    try:
        caption = payload["caption"]
        picture = payload["picture"]
        audio = payload["audio"]
        if not isinstance(audio, dict):
            raise StoryPlanError("Creative profile audio settings must be a JSON object.")
        ducking = audio.get("ducking", {})
        validation = audio.get("validation", {})
        if not isinstance(ducking, dict) or not isinstance(validation, dict):
            raise StoryPlanError("Creative profile audio settings must be JSON objects.")
        transition = payload["transition"]
        formats = payload["formats"]
        profile = CreativeProfile(
            profile_id=payload["profile_id"],
            name=payload["name"],
            caption_style=CaptionStyle(
                font_name=caption["font_name"],
                text_color=caption["text_color"],
                highlight_color=caption["highlight_color"],
                outline_color=caption["outline_color"],
                outline_width=float(caption["outline_width"]),
                shadow_depth=float(caption["shadow_depth"]),
                fade_in_ms=int(caption["fade_in_ms"]),
                fade_out_ms=int(caption["fade_out_ms"]),
                animation=str(caption.get("animation", "pop")),
                entrance_ms=int(caption.get("entrance_ms", 160)),
                bold=bool(caption.get("bold", False)),
                word_highlight=bool(caption.get("word_highlight", False)),
            ),
            warmth=float(picture["warmth"]),
            saturation=float(picture["saturation"]),
            contrast=float(picture["contrast"]),
            audio_target_lufs=float(audio["target_lufs"]),
            true_peak_dbtp=float(audio["true_peak_dbtp"]),
            music_gain_db=float(audio["music_gain_db"]),
            music_fade_in_seconds=float(audio["music_fade_in_seconds"]),
            music_fade_out_seconds=float(audio["music_fade_out_seconds"]),
            transition_kind=transition["kind"],
            transition_duration_seconds=float(transition["duration_seconds"]),
            formats=formats,
            music_ducking_enabled=ducking.get("enabled", False),
            music_ducking_threshold_db=float(ducking.get("threshold_db", -30.0)),
            music_ducking_ratio=float(ducking.get("ratio", 8.0)),
            music_ducking_attack_ms=float(ducking.get("attack_ms", 20.0)),
            music_ducking_release_ms=float(ducking.get("release_ms", 350.0)),
            loudness_warning_tolerance_lu=float(
                validation.get("loudness_warning_tolerance_lu", 3.0)
            ),
            true_peak_warning_dbtp=float(
                validation.get("true_peak_warning_dbtp", -1.0)
            ),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StoryPlanError("Creative profile has missing or invalid values.") from exc

    numeric_values = (
        profile.warmth,
        profile.saturation,
        profile.contrast,
        profile.audio_target_lufs,
        profile.true_peak_dbtp,
        profile.music_gain_db,
        profile.music_fade_in_seconds,
        profile.music_fade_out_seconds,
        profile.transition_duration_seconds,
        profile.music_ducking_threshold_db,
        profile.music_ducking_ratio,
        profile.music_ducking_attack_ms,
        profile.music_ducking_release_ms,
        profile.loudness_warning_tolerance_lu,
        profile.true_peak_warning_dbtp,
    )
    if not all(math.isfinite(value) for value in numeric_values):
        raise StoryPlanError("Creative profile numeric settings must be finite.")
    if not -0.04 <= profile.warmth <= 0.04:
        raise StoryPlanError("Profile warmth must be between -0.04 and 0.04.")
    if not 0.7 <= profile.saturation <= 1.3 or not 0.8 <= profile.contrast <= 1.2:
        raise StoryPlanError("Profile saturation or contrast is outside safe bounds.")
    if not -30 <= profile.audio_target_lufs <= -14:
        raise StoryPlanError("Profile audio target must be between -30 and -14 LUFS.")
    if not -9 <= profile.true_peak_dbtp <= -1:
        raise StoryPlanError("Profile true peak must be between -9 and -1 dBTP.")
    if not -36 <= profile.music_gain_db <= -6:
        raise StoryPlanError("Profile music gain must be between -36 and -6 dB.")
    if (
        profile.music_fade_in_seconds < 0
        or profile.music_fade_out_seconds < 0
        or not 0 <= profile.transition_duration_seconds <= TRANSITION_DURATION_MAX_SECONDS
    ):
        raise StoryPlanError("Profile fade or transition duration is outside safe bounds.")
    if (
        not isinstance(profile.music_ducking_enabled, bool)
        or not -60 <= profile.music_ducking_threshold_db <= -6
        or not 1 <= profile.music_ducking_ratio <= 20
        or not 1 <= profile.music_ducking_attack_ms <= 2000
        or not 1 <= profile.music_ducking_release_ms <= 9000
        or not 0 <= profile.loudness_warning_tolerance_lu <= 12
        or not -9 <= profile.true_peak_warning_dbtp <= 0
    ):
        raise StoryPlanError("Profile ducking or audio-validation settings are outside safe bounds.")
    if profile.transition_kind not in {"fade", "cut"}:
        raise StoryPlanError("Supported transition kinds are 'fade' and 'cut'.")
    for name in OUTPUT_FORMATS:
        try:
            width = formats[name]["width"]
            height = formats[name]["height"]
            crop_x = float(formats[name]["crop_x"])
            fit_mode = formats[name].get("fit_mode", "crop")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StoryPlanError(f"Profile is missing valid {name} format settings.") from exc
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
            or not math.isfinite(crop_x)
            or not 0 <= crop_x <= 1
        ):
            raise StoryPlanError(f"Profile has invalid {name} dimensions or crop.")
        if fit_mode not in {"crop", "blurred_fill"}:
            raise StoryPlanError(f"Profile has invalid {name} fit mode.")
    return profile, hashlib.sha256(profile_path.read_bytes()).hexdigest()


def _caption_asset(
    path: str | Path | None,
    duration: float,
) -> dict[str, Any] | None:
    if path is None:
        return None
    caption_path = Path(path).expanduser().resolve()
    payload = _read_json(
        caption_path,
        error_type=CaptionInputError,
        label="caption input",
    )
    if not isinstance(payload, dict) or payload.get("reviewed") is not True:
        raise CaptionInputError(
            "Caption input must have top-level reviewed: true before it can be added "
            "to a story-edit plan."
        )
    cues = load_caption_cues(caption_path)
    for index, cue in enumerate(cues, start=1):
        if cue.end_seconds > duration:
            raise CaptionInputError(f"Caption {index} ends after the video duration.")
    return {
        "path": str(caption_path),
        "sha256": _sha256_file(caption_path),
        "cue_count": len(cues),
        "reviewed": True,
        "uncertain_cue_count": sum(cue.uncertain for cue in cues),
        "preserve_exact_text": True,
    }


def _make_plan_directory(snapshot: SourceSnapshot, projects: Path) -> Path:
    project_dir = projects / _safe_project_name(snapshot.path)
    run_id = datetime.now(timezone.utc).strftime("story-edit-%Y%m%dT%H%M%S-%fZ")
    return project_dir / "story_edits" / run_id


def create_story_plan(
    video_path: str | Path,
    *,
    settings_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    captions_path: str | Path | None = None,
    title: str | None = None,
    music_path: str | Path | None = None,
    music_source_url: str = "",
    music_provider: str = "",
    music_license: str = "",
    music_attribution: str = "",
    music_evidence: str = "",
    confirm_music_license: bool = False,
) -> Path:
    snapshot = _snapshot_source(video_path, settings_path=settings_path)
    settings, root = _load_settings(settings_path)
    projects = Path(settings["paths"]["projects"])
    if not projects.is_absolute():
        projects = root / projects
    profile, profile_hash = load_creative_profile(profile_path)
    duration = snapshot.metadata.duration_seconds
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise StoryPlanError("Source duration is missing or invalid.")
    if title is not None and not title.strip():
        raise StoryPlanError("A supplied title must not be empty.")
    caption = _caption_asset(captions_path, duration)
    music: dict[str, Any] | None = None
    if music_path is not None:
        track = _probe_music(music_path, settings_path=settings_path)
        music = {
            "path": str(track.path),
            "sha256": track.sha256,
            "duration_seconds": track.duration_seconds,
            "audio_properties": {
                "codec": track.codec,
                "sample_rate_hz": track.sample_rate_hz,
                "channels": track.channels,
            },
            "source_url": music_source_url or None,
            "provider": music_provider or None,
            "license_name_or_identifier": music_license or None,
            "attribution_requirements": music_attribution or None,
            "license_evidence_reference": music_evidence or None,
            "user_confirmed_permitted_for_intended_use": confirm_music_license,
        }
        if snapshot.metadata.audio is None:
            raise StoryPlanError("Background music cannot be used without source voice audio.")

    run_dir = _make_plan_directory(snapshot, projects)
    plan_path = run_dir / "story_edit.json"
    report_path = run_dir / "story_edit.md"
    approvals_path = run_dir / "approvals.jsonl"
    analysis_path = run_dir / "audio_analysis.md"
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        analysis = analyze_audio(
            snapshot.path,
            settings_path=settings_path,
            report_path=analysis_path,
        )
        candidates = _make_candidates(
            analysis,
            duration_seconds=duration,
            padding_before_seconds=0.25,
            padding_after_seconds=0.25,
            minimum_cut_seconds=0.5,
            frame_rate_hz=snapshot.metadata.video.frame_rate,
        )
        profile_payload = {
            "profile_id": profile.profile_id,
            "name": profile.name,
            "caption_style": asdict(profile.caption_style),
            "warmth": profile.warmth,
            "saturation": profile.saturation,
            "contrast": profile.contrast,
            "audio_target_lufs": profile.audio_target_lufs,
            "true_peak_dbtp": profile.true_peak_dbtp,
            "music_gain_db": profile.music_gain_db,
            "music_fade_in_seconds": profile.music_fade_in_seconds,
            "music_fade_out_seconds": profile.music_fade_out_seconds,
            "transition_kind": profile.transition_kind,
            "transition_duration_seconds": profile.transition_duration_seconds,
            "music_ducking_enabled": profile.music_ducking_enabled,
            "music_ducking_threshold_db": profile.music_ducking_threshold_db,
            "music_ducking_ratio": profile.music_ducking_ratio,
            "music_ducking_attack_ms": profile.music_ducking_attack_ms,
            "music_ducking_release_ms": profile.music_ducking_release_ms,
            "loudness_warning_tolerance_lu": profile.loudness_warning_tolerance_lu,
            "true_peak_warning_dbtp": profile.true_peak_warning_dbtp,
            "formats": profile.formats,
        }
        payload = {
            "version": PROJECT_VERSION,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": snapshot.to_dict(),
            "profile": profile_payload,
            "profile_sha256": profile_hash,
            "title": {
                "text": title,
                "status": "PENDING",
                "source": "user_supplied",
            } if title is not None else None,
            "captions": caption,
            "music": music,
            "candidates": [candidate.to_dict() for candidate in candidates],
            "limitations": [
                "Silence candidates are signal-level suggestions, not story judgments.",
                "No dialogue was transcribed, rewritten, or inferred.",
                "Uncertain caption cues are omitted from exports.",
                "No B-roll or sound effects are generated or inserted.",
            ],
        }
        _write_json_exclusive(plan_path, payload)
        with report_path.open("x", encoding="utf-8", newline="") as report:
            report.write(_render_plan_report(payload, analysis))
        with approvals_path.open("x", encoding="utf-8", newline=""):
            pass
        _assert_source_unchanged(snapshot)
    except Exception:
        for generated in (plan_path, report_path, approvals_path, analysis_path):
            try:
                generated.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise
    return plan_path


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
    except FileExistsError as exc:
        raise StoryPlanError(f"Refusing to overwrite {path.name}.") from exc
    except OSError as exc:
        raise StoryPlanError(f"Could not write {path.name}.") from exc


def _render_plan_report(payload: dict[str, Any], analysis: Any) -> str:
    source = payload["source"]
    lines = [
        "# Nama Kadhai story edit plan",
        "",
        f"- **Source:** `{Path(source['path']).name}`",
        f"- **Duration:** {source['duration_seconds']:.3f}s",
        f"- **Source SHA-256:** `{source['sha256']}`",
        f"- **Profile:** {payload['profile']['name']}",
        f"- **Approved cuts:** none; every suggestion starts PENDING",
        "",
        "This plan never edits the source. Review suggestions before rendering. "
        "Pending or rejected cuts are retained unchanged.",
        "",
        "## Suggested silence cuts",
        "",
    ]
    if not payload["candidates"]:
        lines.append("No silence-based cut candidates were found.")
    else:
        lines.extend(
            (
                "| Candidate | Source interval | Proposed cut | Trim | Status |",
                "|---|---:|---:|---:|---|",
            )
        )
        for item in payload["candidates"]:
            start, end = item["proposed_cut_seconds"]
            lines.append(
                f"| `{item['candidate_id']}` | "
                f"{item['source_start_seconds']:.3f}–{item['source_end_seconds']:.3f}s | "
                f"{start:.3f}–{end:.3f}s | {end - start:.3f}s | PENDING |"
            )
    lines.extend(("", "## Reviewable items", ""))
    if payload["captions"]:
        lines.append(
            f"- Caption file: `{Path(payload['captions']['path']).name}` "
            f"({payload['captions']['cue_count']} cues, reviewed at plan time; "
            "separate `captions` approval required)."
        )
    if payload["title"]:
        lines.append("- User-supplied title: pending separate `title` approval.")
    if payload["music"]:
        lines.append(
            "- Local music: pending separate `music` approval. Check the license and "
            "attribution before approving."
        )
    if not any((payload["captions"], payload["title"], payload["music"])):
        lines.append("No caption, title, or music assets were supplied.")
    lines.extend(("", "## Analysis", ""))
    if analysis.levels is None:
        audio_summary = "no audio stream detected."
    else:
        mean = (
            f"{analysis.levels.mean_volume_db:.2f} dBFS"
            if analysis.levels.mean_volume_db is not None
            else "unavailable"
        )
        peak = (
            f"{analysis.levels.max_volume_db:.2f} dBFS"
            if analysis.levels.max_volume_db is not None
            else "unavailable"
        )
        audio_summary = f"{mean} mean, {peak} peak."
    lines.append(f"Audio: {audio_summary}")
    lines.extend(("", "Story order is preserved. No B-roll or sound effects are added.", ""))
    return "\n".join(lines)


def _load_story_plan(path: str | Path) -> tuple[dict[str, Any], Path]:
    plan_path = Path(path).expanduser().resolve()
    payload = _read_json(plan_path, error_type=StoryPlanError, label="story edit plan")
    if (
        not isinstance(payload, dict)
        or payload.get("version") != PROJECT_VERSION
        or not isinstance(payload.get("source"), dict)
        or not isinstance(payload.get("candidates"), list)
    ):
        raise StoryPlanError("Story edit plan has an unsupported schema.")
    source = payload["source"]
    source_path = Path(str(source.get("path", ""))).expanduser().resolve()
    if not source_path.is_file():
        raise StoryPlanError("Source recorded in story plan is unavailable.")
    if (
        source_path.stat().st_size != source.get("size_bytes")
        or _sha256_file(source_path) != source.get("sha256")
    ):
        raise StoryPlanError("Story plan is stale: source size or hash changed.")
    try:
        duration = float(source["duration_seconds"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StoryPlanError("Story plan source duration is missing or invalid.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise StoryPlanError("Story plan source duration is missing or invalid.")
    for candidate in payload["candidates"]:
        if not isinstance(candidate, dict):
            raise StoryPlanError("Story plan contains an invalid cut candidate.")
        identifier = candidate.get("candidate_id")
        try:
            source_start = float(candidate["source_start_seconds"])
            source_end = float(candidate["source_end_seconds"])
            cut = candidate["proposed_cut_seconds"]
            cut_start, cut_end = map(float, cut)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StoryPlanError("Story plan contains invalid candidate timing data.") from exc
        if (
            not isinstance(identifier, str)
            or not identifier
            or not all(
                math.isfinite(value)
                for value in (source_start, source_end, cut_start, cut_end)
            )
            or source_start < 0
            or source_end > duration
            or source_end <= source_start
            or cut_start < source_start
            or cut_end > source_end
            or cut_end <= cut_start
        ):
            raise StoryPlanError("Story plan contains out-of-bounds candidate timing data.")
    _decision_ids(payload)
    return payload, plan_path


def _decision_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for candidate in payload["candidates"]:
        if not isinstance(candidate, dict):
            raise StoryPlanError("Story plan contains an invalid cut candidate.")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise StoryPlanError("Story plan candidate has an invalid ID.")
        if candidate_id in ids:
            raise StoryPlanError(f"Story plan has duplicate candidate ID {candidate_id}.")
        ids.add(candidate_id)
    if payload.get("captions") is not None:
        ids.add("captions")
    if payload.get("title") is not None:
        ids.add("title")
    if payload.get("music") is not None:
        ids.add("music")
    return ids


def record_story_decision(
    plan_path: str | Path,
    item_id: str,
    status: str,
    *,
    note: str = "",
    actor: str = "creator",
) -> Path:
    if actor not in {"creator", "auto"}:
        raise StoryPlanError("Decision actor must be creator or auto.")
    if not isinstance(status, str):
        raise StoryPlanError("Decision must be APPROVED, REJECTED, or PENDING.")
    status = status.upper()
    if status not in {"APPROVED", "REJECTED", "PENDING"}:
        raise StoryPlanError("Decision must be APPROVED, REJECTED, or PENDING.")
    payload, plan = _load_story_plan(plan_path)
    if not isinstance(item_id, str):
        raise StoryPlanError("Story-plan item ID must be a string.")
    if item_id not in _decision_ids(payload):
        raise StoryPlanError(f"Unknown story-plan item: {item_id}")
    event = {
        "plan_sha256": _sha256_file(plan),
        "item_id": item_id,
        "status": status,
        "reviewer_notes": note,
        "actor": actor,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with (plan.parent / "approvals.jsonl").open(
            "a", encoding="utf-8", newline=""
        ) as approvals:
            approvals.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise StoryPlanError("Could not append story-plan decision.") from exc
    return plan.parent / "approvals.jsonl"


def _read_story_decisions(plan: Path, payload: dict[str, Any]) -> dict[str, str]:
    history = plan.parent / "approvals.jsonl"
    try:
        lines = history.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StoryPlanError("Could not read story-plan approval history.") from exc
    digest = _sha256_file(plan)
    decisions: dict[str, str] = {}
    allowed = _decision_ids(payload)
    for index, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoryPlanError(f"Approval history line {index} is invalid JSON.") from exc
        item_id = event.get("item_id") if isinstance(event, dict) else None
        status = event.get("status") if isinstance(event, dict) else None
        if (
            not isinstance(event, dict)
            or event.get("plan_sha256") != digest
            or not isinstance(item_id, str)
            or item_id not in allowed
            or not isinstance(status, str)
            or status not in APPROVAL_STATUSES
        ):
            raise StoryPlanError(f"Approval history line {index} is invalid or stale.")
        decisions[item_id] = status
    return decisions


def _get_profile(payload: dict[str, Any]) -> CreativeProfile:
    raw = payload["profile"]
    try:
        caption = raw["caption_style"]
        profile = CreativeProfile(
            profile_id=raw["profile_id"],
            name=raw["name"],
            caption_style=CaptionStyle(**caption),
            warmth=float(raw["warmth"]),
            saturation=float(raw["saturation"]),
            contrast=float(raw["contrast"]),
            audio_target_lufs=float(raw["audio_target_lufs"]),
            true_peak_dbtp=float(raw["true_peak_dbtp"]),
            music_gain_db=float(raw["music_gain_db"]),
            music_fade_in_seconds=float(raw["music_fade_in_seconds"]),
            music_fade_out_seconds=float(raw["music_fade_out_seconds"]),
            transition_kind=raw["transition_kind"],
            transition_duration_seconds=float(raw["transition_duration_seconds"]),
            formats=raw["formats"],
            music_ducking_enabled=raw.get("music_ducking_enabled", False),
            music_ducking_threshold_db=float(
                raw.get("music_ducking_threshold_db", -30.0)
            ),
            music_ducking_ratio=float(raw.get("music_ducking_ratio", 8.0)),
            music_ducking_attack_ms=float(raw.get("music_ducking_attack_ms", 20.0)),
            music_ducking_release_ms=float(
                raw.get("music_ducking_release_ms", 350.0)
            ),
            loudness_warning_tolerance_lu=float(
                raw.get("loudness_warning_tolerance_lu", 3.0)
            ),
            true_peak_warning_dbtp=float(raw.get("true_peak_warning_dbtp", -1.0)),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StoryPlanError("Story plan contains an invalid creative profile.") from exc
    values = (
        profile.warmth,
        profile.saturation,
        profile.contrast,
        profile.audio_target_lufs,
        profile.true_peak_dbtp,
        profile.music_gain_db,
        profile.music_fade_in_seconds,
        profile.music_fade_out_seconds,
        profile.transition_duration_seconds,
        profile.music_ducking_threshold_db,
        profile.music_ducking_ratio,
        profile.music_ducking_attack_ms,
        profile.music_ducking_release_ms,
        profile.loudness_warning_tolerance_lu,
        profile.true_peak_warning_dbtp,
    )
    if (
        not all(math.isfinite(value) for value in values)
        or not -0.04 <= profile.warmth <= 0.04
        or not 0.7 <= profile.saturation <= 1.3
        or not 0.8 <= profile.contrast <= 1.2
        or not -30 <= profile.audio_target_lufs <= -14
        or not -9 <= profile.true_peak_dbtp <= -1
        or not -36 <= profile.music_gain_db <= -6
        or not isinstance(profile.music_ducking_enabled, bool)
        or not -60 <= profile.music_ducking_threshold_db <= -6
        or not 1 <= profile.music_ducking_ratio <= 20
        or not 1 <= profile.music_ducking_attack_ms <= 2000
        or not 1 <= profile.music_ducking_release_ms <= 9000
        or not 0 <= profile.loudness_warning_tolerance_lu <= 12
        or not -9 <= profile.true_peak_warning_dbtp <= 0
        or profile.music_fade_in_seconds < 0
        or profile.music_fade_out_seconds < 0
        or not 0 <= profile.transition_duration_seconds <= TRANSITION_DURATION_MAX_SECONDS
        or profile.transition_kind not in {"fade", "cut"}
        or not isinstance(profile.caption_style.font_name, str)
        or not profile.caption_style.font_name.strip()
    ):
        raise StoryPlanError("Story plan creative settings are outside supported limits.")
    for name in OUTPUT_FORMATS:
        try:
            layout = profile.formats[name]
            width = layout["width"]
            height = layout["height"]
            crop_x = float(layout["crop_x"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StoryPlanError(f"Story plan has invalid {name} format settings.") from exc
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
            or not math.isfinite(crop_x)
            or not 0 <= crop_x <= 1
        ):
            raise StoryPlanError(f"Story plan has invalid {name} format settings.")
    return profile


def _approved_cuts(
    payload: dict[str, Any],
    decisions: dict[str, str],
    duration: float,
) -> tuple[tuple[float, float], ...]:
    approved: list[tuple[float, float]] = []
    for candidate in payload["candidates"]:
        if not isinstance(candidate, dict):
            raise StoryPlanError("Story plan contains an invalid cut candidate.")
        candidate_id = candidate.get("candidate_id")
        if decisions.get(candidate_id) != "APPROVED":
            continue
        try:
            start, end = map(float, candidate["proposed_cut_seconds"])
            source_start = float(candidate["source_start_seconds"])
            source_end = float(candidate["source_end_seconds"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StoryPlanError(f"Cut candidate {candidate_id} has invalid bounds.") from exc
        if (
            not all(math.isfinite(value) for value in (start, end, source_start, source_end))
            or source_start < 0
            or source_end > duration
            or source_end <= source_start
            or start < source_start
            or end > source_end
            or end <= start
        ):
            raise StoryPlanError(f"Approved cut candidate {candidate_id} is out of bounds.")
        approved.append((start, end))
    approved.sort()
    for left, right in zip(approved, approved[1:]):
        if right[0] < left[1]:
            raise StoryPlanError("Approved story cuts overlap.")
    return tuple(approved)


def _map_captions(
    cues: tuple[CaptionCue, ...],
    retained: tuple[tuple[float, float], ...],
    transition_duration: float,
) -> tuple[CaptionCue, ...]:
    mapped: list[CaptionCue] = []
    elapsed = 0.0
    for index, (range_start, range_end) in enumerate(retained):
        length = range_end - range_start
        segment_offset = elapsed
        for cue in cues:
            if cue.uncertain:
                continue
            if cue.words:
                retained_words = tuple(
                    word
                    for word in cue.words
                    if word.start_seconds >= range_start and word.end_seconds <= range_end
                )
                if not retained_words:
                    continue
                # Keep the cue's lead-in/hold where the cut allows, and move the
                # word timings onto the output timeline with the cue.
                start = max(range_start, min(cue.start_seconds, retained_words[0].start_seconds))
                end = min(range_end, max(cue.end_seconds, retained_words[-1].end_seconds))
                if len(retained_words) < len(cue.words):
                    start = max(start, retained_words[0].start_seconds - 0.15)
                    end = min(end, retained_words[-1].end_seconds + 0.4)
                shift = segment_offset - range_start
                mapped.append(
                    CaptionCue(
                        start_seconds=start + shift,
                        end_seconds=end + shift,
                        text=" ".join(word.text for word in retained_words),
                        highlights=cue.highlights,
                        words=tuple(
                            replace(
                                word,
                                start_seconds=word.start_seconds + shift,
                                end_seconds=word.end_seconds + shift,
                            )
                            for word in retained_words
                        ),
                    )
                )
                continue
            intersections = sum(
                min(cue.end_seconds, end) > max(cue.start_seconds, start)
                for start, end in retained
            )
            if intersections > 1:
                continue
            start = max(cue.start_seconds, range_start)
            end = min(cue.end_seconds, range_end)
            if end <= start:
                continue
            mapped.append(
                CaptionCue(
                    start_seconds=segment_offset + start - range_start,
                    end_seconds=segment_offset + end - range_start,
                    text=cue.text,
                    highlights=cue.highlights,
                )
            )
        elapsed += length
        if index < len(retained) - 1:
            elapsed -= transition_duration
    mapped.sort(key=lambda cue: (cue.start_seconds, cue.end_seconds))
    return tuple(mapped)


def _build_render_graph(
    retained: tuple[tuple[float, float], ...],
    *,
    profile: CreativeProfile,
    layout: dict[str, float | int | str],
    source_audio: bool,
    music: MusicMetadata | None,
    ass_path: Path | None,
    output_duration: float,
    music_ducking_enabled: bool = False,
    music_ducking_threshold_db: float = -30.0,
    music_ducking_ratio: float = 8.0,
    music_ducking_attack_ms: float = 20.0,
    music_ducking_release_ms: float = 350.0,
    frame_rate_hz: float = 30.0,
    transition_duration_seconds: float | None = None,
    loudnorm_measurements: dict[str, float] | None = None,
    include_video: bool = True,
) -> tuple[str, set[str]]:
    width = int(layout["width"])
    height = int(layout["height"])
    crop_x = float(layout["crop_x"])
    fit_mode = str(layout.get("fit_mode", "crop"))
    transition = (
        min(profile.transition_duration_seconds, MAX_SILENCE_CROSSFADE_SECONDS)
        if transition_duration_seconds is None
        else transition_duration_seconds
    )
    use_fade = profile.transition_kind == "fade" and transition > 0
    if not math.isfinite(frame_rate_hz) or frame_rate_hz <= 0:
        raise StoryPlanError("Render frame rate must be positive and finite.")
    if len(retained) > 1 and use_fade:
        if any(end - start <= transition for start, end in retained):
            raise StoryPlanError(
                "At least one retained segment is shorter than its transition; "
                "choose a shorter transition or reject that cut."
            )

    graph: list[str] = []
    required = {
        "trim",
        "setpts",
        "scale",
        "crop",
        "setsar",
        "colorbalance",
        "eq",
        "format",
        "fps",
        "settb",
    }
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (start, end) in enumerate(retained):
        video_label = f"v{index}"
        if include_video and fit_mode == "blurred_fill":
            background_label = f"bg{index}"
            foreground_label = f"fg{index}"
            background_source = f"bgsrc{index}"
            foreground_source = f"fgsrc{index}"
            graph.append(
                f"[0:v:0]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
                f"fps={frame_rate_hz:.6f},settb=AVTB,"
                f"split=2[{background_source}][{foreground_source}]"
            )
            graph.append(
                f"[{background_source}]scale={width}:{height}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:(iw-ow)*0.5000:(ih-oh)*0.5000,"
                f"boxblur=24:2,eq=brightness=-0.3000:saturation=0.7200"
                f"[{background_label}]"
            )
            graph.append(
                f"[{foreground_source}]scale={width}:{height}:"
                "force_original_aspect_ratio=decrease:force_divisible_by=2"
                f"[{foreground_label}]"
            )
            graph.append(
                f"[{background_label}][{foreground_label}]"
                "overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,"
                f"colorbalance=rs={profile.warmth:.4f}:gs=0:bs={-profile.warmth:.4f}:"
                f"rm={profile.warmth:.4f}:gm=0:bm={-profile.warmth:.4f},"
                f"eq=contrast={profile.contrast:.4f}:saturation={profile.saturation:.4f}"
                f"[{video_label}]"
            )
            required.update({"split", "boxblur", "overlay"})
        elif include_video:
            graph.append(
                f"[0:v:0]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
                f"fps={frame_rate_hz:.6f},settb=AVTB,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:(iw-ow)*{crop_x:.4f}:(ih-oh)/2,setsar=1,"
                f"colorbalance=rs={profile.warmth:.4f}:gs=0:bs={-profile.warmth:.4f}:"
                f"rm={profile.warmth:.4f}:gm=0:bm={-profile.warmth:.4f},"
                f"eq=contrast={profile.contrast:.4f}:saturation={profile.saturation:.4f}"
                f"[{video_label}]"
            )
        if include_video:
            video_labels.append(f"[{video_label}]")
        if source_audio:
            audio_label = f"a{index}"
            audio_filters = [
                f"[0:a:0]atrim=start={start:.6f}:end={end:.6f}",
                "asetpts=PTS-STARTPTS",
                "aformat=sample_rates=48000:channel_layouts=stereo",
                "aresample=48000",
            ]
            if not use_fade and len(retained) > 1:
                segment_duration = end - start
                fade_duration = min(
                    HARD_CUT_AUDIO_FADE_SECONDS,
                    segment_duration / 2,
                )
                if index > 0:
                    audio_filters.append(
                        f"afade=t=in:st=0:d={fade_duration:.4f}"
                    )
                if index < len(retained) - 1:
                    fade_start = max(0.0, segment_duration - fade_duration)
                    audio_filters.append(
                        f"afade=t=out:st={fade_start:.6f}:d={fade_duration:.4f}"
                    )
            graph.append(f"{','.join(audio_filters)}[{audio_label}]")
            audio_labels.append(f"[{audio_label}]")
            required.update({"atrim", "asetpts", "aformat", "aresample"})
            if not use_fade and len(retained) > 1:
                required.add("afade")

    if include_video:
        if len(retained) == 1:
            video_chain = video_labels[0]
        elif not use_fade:
            graph.append(
                "".join(video_labels)
                + f"concat=n={len(video_labels)}:v=1:a=0[video_joined]"
            )
            video_chain = "[video_joined]"
            required.add("concat")
        else:
            video_chain = video_labels[0]
            previous_duration = retained[0][1] - retained[0][0]
            for index in range(1, len(retained)):
                offset = previous_duration - transition
                next_label = f"vx{index}"
                graph.append(
                    f"{video_chain}{video_labels[index]}"
                    f"xfade=transition=fade:duration={transition:.4f}:"
                    f"offset={offset:.6f}[{next_label}]"
                )
                video_chain = f"[{next_label}]"
                previous_duration += retained[index][1] - retained[index][0] - transition
            required.add("xfade")

        # Some phone recordings have an audio stream that is one or two frames
        # longer than the video stream. Hold the final decoded frame to the exact
        # approved timeline instead of trimming speech or relaxing sync checks.
        graph.append(
            f"{video_chain}tpad=stop_mode=clone:stop_duration=1.000000,"
            f"trim=duration={output_duration:.6f},setpts=PTS-STARTPTS[video_timeline]"
        )
        video_chain = "[video_timeline]"
        required.add("tpad")

        if ass_path is not None:
            font_directory = Path(__file__).resolve().parents[2] / "fonts"
            graph.append(
                f"{video_chain}ass=filename='{_escape_filter_path(ass_path)}':"
                f"fontsdir='{_escape_filter_path(font_directory)}'[vout]"
            )
            required.add("ass")
        else:
            graph.append(f"{video_chain}null[vout]")
            required.add("null")

    if source_audio:
        if len(audio_labels) == 1:
            graph.append(f"{audio_labels[0]}anull[voice]")
            required.add("anull")
        elif not use_fade:
            graph.append(
                "".join(audio_labels)
                + f"concat=n={len(audio_labels)}:v=0:a=1[voice]"
            )
            required.add("concat")
        else:
            audio_chain = audio_labels[0]
            for index in range(1, len(audio_labels)):
                next_audio = f"ax{index}"
                graph.append(
                    f"{audio_chain}{audio_labels[index]}"
                    f"acrossfade=d={transition:.4f}:c1=tri:c2=tri[{next_audio}]"
                )
                audio_chain = f"[{next_audio}]"
            graph.append(f"{audio_chain}anull[voice]")
            required.update({"acrossfade", "anull"})

    if music is not None:
        if not source_audio:
            raise StoryPlanError("Music requires an original voice audio stream.")
        music_filters = [
            f"atrim=duration={output_duration:.6f}",
            "asetpts=PTS-STARTPTS",
            "aformat=sample_rates=48000:channel_layouts=stereo",
            "aresample=48000",
            f"volume={profile.music_gain_db:.2f}dB",
        ]
        if profile.music_fade_in_seconds > 0:
            music_filters.append(
                f"afade=t=in:st=0:d={min(profile.music_fade_in_seconds, output_duration):.3f}"
            )
        if profile.music_fade_out_seconds > 0:
            fade_length = min(profile.music_fade_out_seconds, output_duration)
            music_filters.append(
                f"afade=t=out:st={max(0.0, output_duration - fade_length):.3f}:"
                f"d={fade_length:.3f}"
            )
        graph.append(f"[1:a:0]{','.join(music_filters)}[music]")
        music_input = "[music]"
        if music_ducking_enabled:
            threshold = 10 ** (music_ducking_threshold_db / 20)
            graph.append(
                "[voice]asplit=2[voice_mix][voice_sc];"
                f"[music][voice_sc]sidechaincompress=threshold={threshold:.8f}:"
                f"ratio={music_ducking_ratio:.3f}:attack={music_ducking_attack_ms:.3f}:"
                f"release={music_ducking_release_ms:.3f}:makeup=1[music_ducked]"
            )
            music_input = "[music_ducked]"
            required.update({"sidechaincompress", "asplit"})
            voice_input = "[voice_mix]"
        else:
            voice_input = "[voice]"
        graph.append(
            f"{voice_input}{music_input}amix=inputs=2:duration=first:dropout_transition=2:"
            "weights='1 1':normalize=0[mix_raw]"
        )
        required.update({"atrim", "asetpts", "aformat", "aresample", "volume", "amix"})
        if profile.music_fade_in_seconds > 0 or profile.music_fade_out_seconds > 0:
            required.add("afade")
        audio_input = "[mix_raw]"
    elif source_audio:
        audio_input = "[voice]"
    else:
        audio_input = ""

    if audio_input:
        loudnorm = (
            f"loudnorm=I={profile.audio_target_lufs:.2f}:"
            f"TP={profile.true_peak_dbtp:.2f}:LRA=11"
        )
        if loudnorm_measurements is None:
            loudnorm += ":linear=false:print_format=json"
        else:
            loudnorm += (
                f":measured_I={loudnorm_measurements['input_i']:.6f}"
                f":measured_TP={loudnorm_measurements['input_tp']:.6f}"
                f":measured_LRA={loudnorm_measurements['input_lra']:.6f}"
                f":measured_thresh={loudnorm_measurements['input_thresh']:.6f}"
                ":linear=true:print_format=json"
            )
        graph.append(
            f"{audio_input}{loudnorm},alimiter=limit=0.95:attack=5:release=50,"
            f"aresample=48000,atrim=duration={output_duration:.6f},"
            "asetpts=PTS-STARTPTS[aout]"
        )
        required.update({"loudnorm", "alimiter", "aresample", "atrim", "asetpts"})
    return ";".join(graph), required


def _build_ffmpeg_command(
    executable: Path,
    source: Path,
    output: Path,
    *,
    graph: str,
    source_audio: bool,
    music: MusicMetadata | None,
    audio_only: bool = False,
) -> list[str]:
    command = [
        str(executable),
        "-hide_banner",
        "-loglevel",
        "info" if audio_only else "error",
        "-nostdin",
        "-filter_complex_threads",
        str(render_threads()),
        "-threads",
        str(render_threads()),
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
        )
    )
    if audio_only:
        command.extend(("-map", "[aout]", "-vn", "-f", "null", os.devnull))
        return command
    command.extend(("-map", "[vout]"))
    if source_audio:
        command.extend(("-map", "[aout]"))
    command.extend(
        (
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-threads:v",
            str(render_threads()),
            "-pix_fmt",
            "yuv420p",
        )
    )
    if source_audio:
        command.extend(("-c:a", "aac", "-b:a", "192k", "-ar", "48000"))
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def _parse_loudnorm_measurements(log: str) -> dict[str, float]:
    decoder = json.JSONDecoder()
    parsed: dict[str, Any] | None = None
    for index, character in enumerate(log):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(log[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "input_i" in value:
            parsed = value
    if parsed is None:
        raise StoryRenderError("FFmpeg did not report first-pass loudness measurements.")
    result: dict[str, float] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh"):
        try:
            value = float(parsed[key])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StoryRenderError("FFmpeg returned incomplete loudness measurements.") from exc
        if not math.isfinite(value):
            raise StoryRenderError("FFmpeg returned non-finite loudness measurements.")
        result[key] = value
    return result


def _measure_story_loudness(
    ffmpeg: Path,
    source: Path,
    *,
    retained: tuple[tuple[float, float], ...],
    profile: CreativeProfile,
    frame_rate_hz: float,
    output_duration: float,
    music: MusicMetadata | None,
    transition_duration_seconds: float,
) -> dict[str, float]:
    graph, _ = _build_render_graph(
        retained,
        profile=profile,
        layout=profile.formats["landscape"],
        source_audio=True,
        music=music,
        ass_path=None,
        output_duration=output_duration,
        music_ducking_enabled=profile.music_ducking_enabled,
        music_ducking_threshold_db=profile.music_ducking_threshold_db,
        music_ducking_ratio=profile.music_ducking_ratio,
        music_ducking_attack_ms=profile.music_ducking_attack_ms,
        music_ducking_release_ms=profile.music_ducking_release_ms,
        frame_rate_hz=frame_rate_hz,
        transition_duration_seconds=transition_duration_seconds,
        include_video=False,
    )
    command = _build_ffmpeg_command(
        ffmpeg,
        source,
        Path(os.devnull),
        graph=graph,
        source_audio=True,
        music=music,
        audio_only=True,
    )
    return _parse_loudnorm_measurements(_run_render(command))


def _run_render(command: list[str]) -> str:
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
        raise StoryRenderError("FFmpeg timed out while rendering story edit.") from exc
    except OSError as exc:
        raise StoryRenderError("Could not start configured FFmpeg.") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-2500:]
        raise StoryRenderError(
            "FFmpeg story render failed." + (f" {detail}" if detail else "")
        )
    return result.stderr


def _write_staged_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="") as output:
            output.write(text)
    except OSError as exc:
        raise StoryRenderError(f"Could not stage {path.name}.") from exc


def record_story_preview_review(
    plan_path: str | Path,
    output_format: str,
    preview_path: str | Path,
) -> dict[str, Any]:
    if output_format not in OUTPUT_FORMATS:
        raise StoryPlanError("Preview format must be landscape or vertical.")
    plan = Path(plan_path).expanduser().resolve()
    if not plan.is_file():
        raise StoryPlanError("Current story plan is missing.")
    preview = Path(preview_path).expanduser().resolve()
    if preview.name != f"review_{output_format}.mp4" or not preview.is_file():
        raise StoryPlanError("The current rendered preview is missing.")
    try:
        preview.parent.relative_to(plan.parent.resolve())
    except ValueError as exc:
        raise StoryPlanError("Preview review files must stay inside the plan folder.") from exc

    receipt_path = preview.parent / "preview_reviews.json"
    if receipt_path.is_file():
        receipt = _read_json(
            receipt_path,
            error_type=StoryPlanError,
            label="preview review receipt",
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("version") != 1
            or receipt.get("plan_sha256") != _sha256_file(plan)
            or not isinstance(receipt.get("reviews"), dict)
        ):
            raise StoryPlanError("Preview review receipt does not match the current plan.")
    else:
        receipt = {
            "version": 1,
            "plan_sha256": _sha256_file(plan),
            "reviews": {},
        }
    review = {
        "output_sha256": _sha256_file(preview),
        "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    receipt["reviews"][output_format] = review
    staged = receipt_path.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        _write_staged_text(staged, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        os.replace(staged, receipt_path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise StoryRenderError("Could not persist the preview review receipt.") from exc
    return review


def _publish_staged(staged: Path, destination: Path) -> None:
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise StoryRenderError(
            f"Output appeared during rendering; refusing to overwrite {destination.name}."
        ) from exc
    except OSError as exc:
        if os.name != "nt":
            raise StoryRenderError(
                f"Could not safely publish {destination.name}."
            ) from exc
        try:
            os.rename(staged, destination)
        except FileExistsError as collision:
            raise StoryRenderError(
                f"Output appeared during rendering; refusing to overwrite {destination.name}."
            ) from collision
        except OSError as rename_error:
            raise StoryRenderError(
                f"Could not safely publish {destination.name}."
            ) from rename_error


def _validate_rendered(
    path: Path,
    *,
    settings_path: str | Path | None,
    expected_duration: float,
    width: int,
    height: int,
    expect_audio: bool,
) -> VideoMetadata:
    if not path.is_file() or path.stat().st_size == 0:
        raise StoryRenderError(f"FFmpeg did not produce {path.name}.")
    try:
        metadata = probe_video(path, settings_path=settings_path)
    except ProbeError as exc:
        raise StoryRenderError(f"Could not validate {path.name}: {exc}") from exc
    if (metadata.video.width, metadata.video.height) != (width, height):
        raise StoryRenderError(
            f"{path.name} dimensions do not match the selected format."
        )
    if expect_audio and metadata.audio is None:
        raise StoryRenderError(f"{path.name} is missing the voice/music audio stream.")
    if expect_audio and metadata.audio is not None and metadata.audio.sample_rate_hz != 48_000:
        raise StoryRenderError(f"{path.name} audio was not rendered at 48 kHz.")
    if not expect_audio and metadata.audio is not None:
        raise StoryRenderError(f"{path.name} unexpectedly contains an audio stream.")
    _validate_timeline(metadata, expected_duration, label=path.name)
    return metadata


def _validate_timeline(
    metadata: VideoMetadata,
    expected_duration: float,
    *,
    label: str,
) -> dict[str, Any]:
    try:
        return _validate_output_timeline(metadata, expected_duration)
    except RenderValidationError as exc:
        raise StoryRenderError(f"{label}: {exc}") from exc


def render_story_edit(
    plan_path: str | Path,
    *,
    settings_path: str | Path | None = None,
    output_directory: str | Path | None = None,
) -> dict[str, Any]:
    payload, plan = _load_story_plan(plan_path)
    decisions = _read_story_decisions(plan, payload)
    source = Path(payload["source"]["path"]).resolve()
    duration = float(payload["source"]["duration_seconds"])
    cuts = _approved_cuts(payload, decisions, duration)
    retained = _retained_ranges(cuts, duration)
    removed_segments = _removed_segments(payload, decisions, _read_story_decision_actors(plan))
    profile = _get_profile(payload)
    transition_duration = (
        min(profile.transition_duration_seconds, MAX_SILENCE_CROSSFADE_SECONDS)
        if len(retained) > 1 and profile.transition_kind == "fade"
        else 0.0
    )
    source_frame_rate = payload["source"].get("video", {}).get("frame_rate")
    frame_rate_hz = float(source_frame_rate) if source_frame_rate else 30.0
    effective_transition = transition_duration
    if any(end - start <= effective_transition for start, end in retained):
        effective_transition = 0.0
        if profile.transition_kind == "fade" and len(retained) > 1:
            raise StoryPlanError(
                "Approved cuts leave a segment too short for the selected transition."
            )
    output_duration = sum(end - start for start, end in retained) - (
        max(0, len(retained) - 1) * effective_transition
    )
    if output_duration <= 0:
        raise StoryPlanError("Approved cuts leave no video to render.")

    snapshot = _snapshot_source(source, settings_path=settings_path)
    if snapshot.sha256 != payload["source"]["sha256"]:
        raise StoryPlanError("Story plan is stale: source hash has changed.")

    caption_asset = payload.get("captions")
    approved_cues: tuple[CaptionCue, ...] = ()
    caption_status = "NOT_SUPPLIED"
    if caption_asset is not None:
        caption_path = Path(caption_asset["path"]).resolve()
        if not caption_path.is_file() or _sha256_file(caption_path) != caption_asset["sha256"]:
            raise StoryPlanError("Story plan is stale: caption input changed.")
        if decisions.get("captions") == "APPROVED":
            if not payload["captions"].get("reviewed") is True:
                raise StoryPlanError("Captions were not marked reviewed.")
            approved_cues = load_caption_cues(caption_path)
            caption_status = "APPROVED"
        else:
            caption_status = "PENDING_OR_REJECTED_OMITTED"
    title_text = None
    if payload.get("title") is not None:
        if decisions.get("title") == "APPROVED":
            title_text = payload["title"]["text"]
    approved_music = None
    music_payload = payload.get("music")
    if music_payload is not None and decisions.get("music") == "APPROVED":
        music_path = Path(music_payload["path"]).resolve()
        if not music_path.is_file() or _sha256_file(music_path) != music_payload["sha256"]:
            raise StoryPlanError("Story plan is stale: music file changed.")
        approved_music = _probe_music(music_path, settings_path=settings_path)
    if approved_music is not None and snapshot.metadata.audio is None:
        raise StoryPlanError("Music mixing requires the original voice audio.")

    ffmpeg = _resolve_ffmpeg(settings_path)
    staged_outputs: list[tuple[Path, Path]] = []
    staged_files: list[Path] = []
    output_records: list[dict[str, Any]] = []
    published: list[Path] = []
    exports = (
        Path(output_directory).expanduser().resolve()
        if output_directory is not None
        else plan.parent / "exports"
    )
    try:
        exports.relative_to(plan.parent.resolve())
    except ValueError as exc:
        raise StoryPlanError("Render output directory must stay inside the plan folder.") from exc
    exports.mkdir(parents=True, exist_ok=True)
    destinations: list[Path] = []
    for format_name in OUTPUT_FORMATS:
        destinations.append(exports / f"review_{format_name}.mp4")
        destinations.append(exports / f"review_{format_name}.render-report.json")
        if approved_cues or title_text:
            destinations.append(exports / f"review_{format_name}.ass")
    if approved_music is not None:
        destinations.append(exports / "music_provenance.json")
    existing = next((item for item in destinations if item.exists()), None)
    if existing is not None:
        raise StoryRenderError(f"Refusing to overwrite existing output: {existing.name}")

    loudnorm_measurements = (
        _measure_story_loudness(
            ffmpeg,
            source,
            retained=retained,
            profile=profile,
            frame_rate_hz=frame_rate_hz,
            output_duration=output_duration,
            music=approved_music,
            transition_duration_seconds=effective_transition,
        )
        if snapshot.metadata.audio is not None
        else None
    )
    if approved_music is not None and _sha256_file(approved_music.path) != approved_music.sha256:
        raise StoryPlanError("Approved music changed during loudness measurement.")

    try:
        for format_name in OUTPUT_FORMATS:
            layout = profile.formats[format_name]
            width = int(layout["width"])
            height = int(layout["height"])
            output = exports / f"review_{format_name}.mp4"
            staged_video = exports / f".{uuid.uuid4().hex}.partial.mp4"
            staged_files.append(staged_video)
            mapped_cues = _map_captions(
                approved_cues,
                retained,
                effective_transition,
            )
            if mapped_cues or title_text:
                ass_content = build_ass(
                    mapped_cues,
                    width=width,
                    height=height,
                    layout=format_name,
                    style=profile.caption_style,
                    title_text=title_text,
                    title_duration_seconds=min(3.0, output_duration),
                )
                ass_destination = exports / f"review_{format_name}.ass"
                staged_ass = exports / f".{uuid.uuid4().hex}.partial.ass"
                staged_files.append(staged_ass)
                _write_staged_text(staged_ass, ass_content)
                staged_outputs.append((staged_ass, ass_destination))
            else:
                ass_destination = None

            graph, required = _build_render_graph(
                retained,
                profile=profile,
                layout=layout,
                source_audio=snapshot.metadata.audio is not None,
                music=approved_music,
                ass_path=(
                    exports / f"review_{format_name}.ass"
                    if ass_destination is not None
                    else None
                ),
                output_duration=output_duration,
                music_ducking_enabled=profile.music_ducking_enabled,
                music_ducking_threshold_db=profile.music_ducking_threshold_db,
                music_ducking_ratio=profile.music_ducking_ratio,
                music_ducking_attack_ms=profile.music_ducking_attack_ms,
                music_ducking_release_ms=profile.music_ducking_release_ms,
                frame_rate_hz=frame_rate_hz,
                transition_duration_seconds=effective_transition,
                loudnorm_measurements=loudnorm_measurements,
            )
            # Point the ASS filter at the staged sibling until the final output is published.
            if ass_destination is not None:
                stage_path = staged_outputs[-1][0]
                graph = graph.replace(
                    _escape_filter_path(ass_destination),
                    _escape_filter_path(stage_path),
                )
            _available_ffmpeg_filters(ffmpeg, required)
            command = _build_ffmpeg_command(
                ffmpeg,
                source,
                staged_video,
                graph=graph,
                source_audio=snapshot.metadata.audio is not None,
                music=approved_music,
            )
            log = _run_render(command)
            if approved_music is not None and _sha256_file(approved_music.path) != approved_music.sha256:
                raise StoryPlanError("Approved music changed during rendering.")
            if caption_asset is not None and _sha256_file(
                Path(caption_asset["path"]).resolve()
            ) != caption_asset["sha256"]:
                raise StoryPlanError("Approved captions changed during rendering.")
            metadata = _validate_rendered(
                staged_video,
                settings_path=settings_path,
                expected_duration=output_duration,
                width=width,
                height=height,
                expect_audio=snapshot.metadata.audio is not None,
            )
            timeline_validation = _validate_timeline(
                metadata,
                output_duration,
                label=output.name,
            )
            audio_quality = (
                _measure_audio_quality(
                    staged_video,
                    ffmpeg=ffmpeg,
                    target_lufs=profile.audio_target_lufs,
                    loudness_warning_tolerance_lu=profile.loudness_warning_tolerance_lu,
                    true_peak_warning_dbtp=profile.true_peak_warning_dbtp,
                )
                if metadata.audio is not None
                else {
                    "measurement_filter": None,
                    "integrated_loudness_lufs": None,
                    "true_peak_dbtp": None,
                    "warning_thresholds": {
                        "target_loudness_lufs": profile.audio_target_lufs,
                        "loudness_warning_tolerance_lu": profile.loudness_warning_tolerance_lu,
                        "true_peak_warning_dbtp": profile.true_peak_warning_dbtp,
                    },
                    "warnings": ["Output has no audio stream to measure."],
                }
            )
            audio_quality["warnings"].extend(timeline_validation["warnings"])
            output_metadata = metadata.to_dict()
            output_metadata["file_name"] = output.name
            output_sha256 = _sha256_file(staged_video)
            report_destination = exports / f"review_{format_name}.render-report.json"
            staged_report = exports / f".{uuid.uuid4().hex}.partial.json"
            staged_files.append(staged_report)
            _write_staged_text(
                staged_report,
                json.dumps(
                    {
                        "version": 1,
                        "source": payload["source"],
                        "source_sha256": snapshot.sha256,
                        "plan_sha256": _sha256_file(plan),
                        "profile_sha256": payload["profile_sha256"],
                        "profile": profile.name,
                        "creative_settings": asdict(profile),
                        "format": format_name,
                        "dimensions": [width, height],
                        "approved_cut_ranges_seconds": [list(item) for item in cuts],
                        "removed_segments": removed_segments,
                        "retained_source_ranges_seconds": [
                            list(item) for item in retained
                        ],
                        "transition": {
                            "kind": profile.transition_kind,
                            "duration_seconds": effective_transition,
                            "maximum_silence_crossfade_seconds": MAX_SILENCE_CROSSFADE_SECONDS,
                        },
                        "captions": {
                            "status": caption_status,
                            "rendered_cue_count": len(mapped_cues),
                            "uncertain_cues_omitted": (
                                sum(cue.uncertain for cue in approved_cues)
                            ),
                            "cut_spanning_cues_omitted": sum(
                                not cue.uncertain
                                and sum(
                                    min(cue.end_seconds, end)
                                    > max(cue.start_seconds, start)
                                    for start, end in retained
                                )
                                > 1
                                for cue in approved_cues
                            ),
                            "asset": (
                                {
                                    "filename": Path(caption_asset["path"]).name,
                                    "sha256": caption_asset["sha256"],
                                    "reviewed": caption_asset["reviewed"],
                                }
                                if caption_asset is not None
                                else None
                            ),
                        },
                        "title": {
                            "status": "APPROVED" if title_text else "OMITTED",
                            "text": title_text,
                            "source": "user_supplied" if title_text else None,
                        },
                        "music": {
                            "approval": decisions.get("music", "NOT_SUPPLIED"),
                            "used_in_render": approved_music is not None,
                            "provenance": _music_report(music_payload, approved_music),
                        } if music_payload is not None else None,
                        "audio_processing": {
                            "normalization": {
                                "method": (
                                    "FFmpeg loudnorm two-pass"
                                    if loudnorm_measurements is not None
                                    else "not applied (no source audio)"
                                ),
                                "sample_rate_hz": (
                                    48000 if loudnorm_measurements is not None else None
                                ),
                                "first_pass_measurements": loudnorm_measurements,
                                "second_pass_linear_requested": loudnorm_measurements is not None,
                            },
                            "music_ducking": {
                                "enabled": profile.music_ducking_enabled
                                and approved_music is not None,
                                "configured_enabled": profile.music_ducking_enabled,
                                "threshold_db": profile.music_ducking_threshold_db,
                                "ratio": profile.music_ducking_ratio,
                                "attack_ms": profile.music_ducking_attack_ms,
                                "release_ms": profile.music_ducking_release_ms,
                            }
                        },
                        "audio_quality": audio_quality,
                        "timeline_validation": timeline_validation,
                        "output_metadata": output_metadata,
                        "output_sha256": output_sha256,
                        "ffmpeg_command": command,
                        "ffmpeg_stderr": log,
                        "validated": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            staged_outputs.extend(
                (
                    (staged_video, output),
                    (staged_report, report_destination),
                )
            )
            output_records.append(
                {
                    "format": format_name,
                    "path": str(output),
                    "dimensions": [width, height],
                    "duration_seconds": metadata.duration_seconds,
                    "captions": caption_status,
                    "render_report": str(report_destination),
                }
            )

        if approved_music is not None and music_payload is not None:
            provenance_destination = exports / "music_provenance.json"
            provenance_stage = exports / f".{uuid.uuid4().hex}.partial.json"
            staged_files.append(provenance_stage)
            _write_staged_text(
                provenance_stage,
                json.dumps(_music_report(music_payload, approved_music), indent=2)
                + "\n",
            )
            staged_outputs.append((provenance_stage, provenance_destination))

        _assert_source_unchanged(snapshot)
        if approved_music is not None and _sha256_file(approved_music.path) != approved_music.sha256:
            raise StoryPlanError("Approved music changed before output publication.")
        if caption_asset is not None and _sha256_file(
            Path(caption_asset["path"]).resolve()
        ) != caption_asset["sha256"]:
            raise StoryPlanError("Approved captions changed before output publication.")
        for staged, destination in staged_outputs:
            _publish_staged(staged, destination)
            published.append(destination)
    except Exception as render_error:
        cleanup_errors: list[Path] = []
        for destination in published:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(destination)
        for staged in staged_files:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(staged)
        if cleanup_errors:
            names = ", ".join(path.name for path in cleanup_errors)
            raise StoryRenderError(
                f"Render failed and could not clean all partial outputs: {names}"
            ) from render_error
        raise
    else:
        cleanup_errors = []
        for staged in staged_files:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(staged)
        if cleanup_errors:
            names = ", ".join(path.name for path in cleanup_errors)
            raise StoryRenderError(
                f"Render completed, but temporary files could not be removed: {names}"
            )

    return {
        "plan": str(plan),
        "exports_directory": str(exports),
        "outputs": output_records,
        "source_sha256": snapshot.sha256,
        "approved_cuts": [list(item) for item in cuts],
        "removed_segments": removed_segments,
        "caption_status": caption_status,
        "music_status": "APPROVED" if approved_music else (
            "PENDING_OR_REJECTED_OMITTED" if music_payload else "NOT_SUPPLIED"
        ),
    }


def approve_story_exports(
    plan_path: str | Path,
    *,
    note: str,
    settings_path: str | Path | None = None,
    output_directory: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(note, str) or not note.strip():
        raise StoryPlanError("Final export approval requires a non-empty review note.")
    payload, plan = _load_story_plan(plan_path)
    decisions = _read_story_decisions(plan, payload)
    unresolved = [
        item_id
        for item_id in _decision_ids(payload)
        if item_id not in decisions or decisions[item_id] == "PENDING"
    ]
    if unresolved:
        raise StoryPlanError(
            "Review or explicitly reject all planned creative items before finalizing: "
            + ", ".join(sorted(unresolved))
        )
    source = payload["source"]
    source_hash = source["sha256"]
    source_path = Path(source["path"]).resolve()
    if _sha256_file(source_path) != source_hash:
        raise StoryPlanError("Cannot finalize: source changed after planning.")
    cuts = _approved_cuts(
        payload,
        decisions,
        float(source["duration_seconds"]),
    )
    exports = (
        Path(output_directory).expanduser().resolve()
        if output_directory is not None
        else plan.parent / "exports"
    )
    try:
        exports.relative_to(plan.parent.resolve())
    except ValueError as exc:
        raise StoryPlanError("Final export directory must stay inside the plan folder.") from exc
    final_approval_path = exports / "final_approval.json"
    final_paths = {
        format_name: exports / f"final_{format_name}.mp4"
        for format_name in OUTPUT_FORMATS
    }
    collisions = [
        path
        for path in (*final_paths.values(), final_approval_path)
        if path.exists()
    ]
    if collisions:
        raise StoryRenderError(
            "Refusing to overwrite finalized output: "
            + ", ".join(path.name for path in collisions)
        )

    receipt_path = exports / "preview_reviews.json"
    if not receipt_path.is_file():
        raise StoryPlanError("Both current previews must be reviewed in the application.")
    receipt = _read_json(
        receipt_path,
        error_type=StoryPlanError,
        label="preview review receipt",
    )
    if (
        not isinstance(receipt, dict)
        or receipt.get("version") != 1
        or receipt.get("plan_sha256") != _sha256_file(plan)
        or not isinstance(receipt.get("reviews"), dict)
    ):
        raise StoryPlanError("Both current previews must be reviewed in the application.")

    profile = _get_profile(payload)
    preview_records: dict[str, dict[str, Any]] = {}
    for format_name in OUTPUT_FORMATS:
        preview = exports / f"review_{format_name}.mp4"
        report_path = exports / f"review_{format_name}.render-report.json"
        if not preview.is_file() or not report_path.is_file():
            raise StoryPlanError(
                f"Both review previews must exist before finalizing; missing {format_name}."
            )
        review = receipt["reviews"].get(format_name)
        if (
            not isinstance(review, dict)
            or review.get("output_sha256") != _sha256_file(preview)
            or not isinstance(review.get("reviewed_at_utc"), str)
            or not review["reviewed_at_utc"]
        ):
            raise StoryPlanError(
                f"Review the current {format_name} preview in the application before finalizing."
            )
        report = _read_json(
            report_path,
            error_type=StoryPlanError,
            label=f"{format_name} render report",
        )
        expected_captions = (
            "APPROVED"
            if decisions.get("captions") == "APPROVED"
            else (
                "PENDING_OR_REJECTED_OMITTED"
                if payload.get("captions") is not None
                else "NOT_SUPPLIED"
            )
        )
        expected_title = "APPROVED" if decisions.get("title") == "APPROVED" else "OMITTED"
        expected_music_approval = (
            decisions.get("music", "NOT_SUPPLIED")
            if payload.get("music") is not None
            else "NOT_SUPPLIED"
        )
        report_music = report.get("music") if isinstance(report, dict) else None
        if (
            not isinstance(report, dict)
            or report.get("validated") is not True
            or report.get("plan_sha256") != _sha256_file(plan)
            or report.get("profile_sha256") != payload.get("profile_sha256")
            or report.get("source_sha256") != source_hash
            or report.get("format") != format_name
            or report.get("output_sha256") != _sha256_file(preview)
            or report.get("approved_cut_ranges_seconds") != [list(item) for item in cuts]
            or not isinstance(report.get("captions"), dict)
            or report.get("captions", {}).get("status") != expected_captions
            or not isinstance(report.get("title"), dict)
            or report.get("title", {}).get("status") != expected_title
            or report.get("title", {}).get("text")
            != (
                payload["title"]["text"]
                if expected_title == "APPROVED" and payload.get("title")
                else None
            )
            or (
                payload.get("music") is not None
                and (
                    not isinstance(report_music, dict)
                    or report_music.get("approval") != expected_music_approval
                    or report_music.get("used_in_render")
                    != (decisions.get("music") == "APPROVED")
                    or not isinstance(report_music.get("provenance"), dict)
                    or report_music.get("provenance", {}).get("sha256")
                    != payload["music"]["sha256"]
                )
            )
            or (payload.get("music") is None and report.get("music") is not None)
        ):
            raise StoryPlanError(
                f"{format_name} preview report does not match current approvals."
            )
        layout = profile.formats[format_name]
        metadata = _validate_rendered(
            preview,
            settings_path=settings_path,
            expected_duration=float(source["duration_seconds"])
            - sum(end - start for start, end in cuts)
            - (
                max(0, len(_retained_ranges(cuts, float(source["duration_seconds"]))) - 1)
                * (
                    min(
                        profile.transition_duration_seconds,
                        MAX_SILENCE_CROSSFADE_SECONDS,
                    )
                    if profile.transition_kind == "fade"
                    else 0
                )
            ),
            width=int(layout["width"]),
            height=int(layout["height"]),
            expect_audio=source.get("audio") is not None,
        )
        preview_records[format_name] = {
            "preview_path": str(preview),
            "preview_sha256": _sha256_file(preview),
            "report_path": str(report_path),
            "report_sha256": _sha256_file(report_path),
            "metadata": metadata.to_dict(),
            "final_path": str(final_paths[format_name]),
        }

    if payload.get("captions") is not None:
        caption_path = Path(payload["captions"]["path"]).resolve()
        if not caption_path.is_file() or _sha256_file(caption_path) != payload["captions"]["sha256"]:
            raise StoryPlanError("Cannot finalize: reviewed caption file changed.")
    if payload.get("music") is not None and decisions.get("music") == "APPROVED":
        music_path = Path(payload["music"]["path"]).resolve()
        if not music_path.is_file() or _sha256_file(music_path) != payload["music"]["sha256"]:
            raise StoryPlanError("Cannot finalize: approved music asset changed.")

    approval = {
        "version": 1,
        "approved_at_utc": datetime.now(timezone.utc).isoformat(),
        "reviewer_note": note.strip(),
        "plan_path": str(plan),
        "plan_sha256": _sha256_file(plan),
        "source_sha256": source_hash,
        "decisions": decisions,
        "decision_actors": _read_story_decision_actors(plan),
        "cuts_applied_seconds": [list(item) for item in cuts],
        "outputs": preview_records,
        "preview_reviews": receipt["reviews"],
        "export_action": "Hard-linked the reviewed final-resolution preview bytes; no re-encode.",
    }
    staged_paths: list[Path] = []
    published: list[Path] = []
    try:
        for format_name in OUTPUT_FORMATS:
            preview = Path(preview_records[format_name]["preview_path"])
            final = final_paths[format_name]
            try:
                os.link(preview, final)
            except FileExistsError as exc:
                raise StoryRenderError(
                    f"Final output appeared during approval: {final.name}"
                ) from exc
            except OSError as exc:
                if os.name != "nt":
                    raise StoryRenderError(
                        f"Could not safely finalize {final.name} without overwriting."
                    ) from exc
                staged_final = final.with_name(f".{uuid.uuid4().hex}.partial.mp4")
                staged_paths.append(staged_final)
                try:
                    with preview.open("rb") as source_file, staged_final.open("xb") as staged_file:
                        shutil.copyfileobj(source_file, staged_file)
                    os.rename(staged_final, final)
                except FileExistsError as collision:
                    raise StoryRenderError(
                        f"Final output appeared during approval: {final.name}"
                    ) from collision
                except OSError as rename_error:
                    raise StoryRenderError(
                        f"Could not safely finalize {final.name} without overwriting."
                    ) from rename_error
            published.append(final)
            if _sha256_file(final) != preview_records[format_name]["preview_sha256"]:
                raise StoryRenderError(
                    f"Final export {final.name} differs from the reviewed preview."
                )

        staged_approval = exports / f".{uuid.uuid4().hex}.partial.json"
        staged_paths.append(staged_approval)
        _write_staged_text(
            staged_approval,
            json.dumps(approval, ensure_ascii=False, indent=2) + "\n",
        )
        _publish_staged(staged_approval, final_approval_path)
        published.append(final_approval_path)
        if _sha256_file(source_path) != source_hash:
            raise StoryPlanError("Source changed while final exports were being approved.")
    except Exception:
        cleanup_errors: list[Path] = []
        for path in published:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(path)
        for path in staged_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(path)
        if cleanup_errors:
            names = ", ".join(path.name for path in cleanup_errors)
            raise StoryRenderError(
                f"Finalization failed and cleanup was incomplete: {names}"
            )
        raise
    else:
        cleanup_errors = []
        for path in staged_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(path)
        if cleanup_errors:
            names = ", ".join(path.name for path in cleanup_errors)
            raise StoryRenderError(
                f"Finalization completed, but temporary files remain: {names}"
            )
    return {
        "approval_report": str(final_approval_path),
        "final_exports": {
            key: str(value) for key, value in final_paths.items()
        },
    }


def _removed_segments(
    payload: dict[str, Any], decisions: dict[str, str], actors: dict[str, str]
) -> list[dict[str, Any]]:
    """What each approved cut removes, why, and who approved it (auto or creator)."""
    removed = []
    for candidate in payload["candidates"]:
        candidate_id = candidate.get("candidate_id")
        if decisions.get(candidate_id) != "APPROVED":
            continue
        start, end = map(float, candidate["proposed_cut_seconds"])
        removed.append({
            "candidate_id": candidate_id,
            "kind": candidate.get("kind", "silence"),
            "confidence": candidate.get("confidence"),
            "source_start_seconds": start,
            "source_end_seconds": end,
            "reason": candidate.get("reason"),
            "decided_by": actors.get(candidate_id, "creator"),
        })
    return sorted(removed, key=lambda item: item["source_start_seconds"])


def _read_story_decision_actors(plan: Path) -> dict[str, str]:
    try:
        lines = (plan.parent / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StoryPlanError("Could not read story-plan approval history.") from exc
    actors: dict[str, str] = {}
    for index, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoryPlanError(f"Approval history line {index} is invalid JSON.") from exc
        actor = event.get("actor", "creator") if isinstance(event, dict) else None
        item_id = event.get("item_id") if isinstance(event, dict) else None
        if actor not in {"creator", "auto"} or not isinstance(item_id, str):
            raise StoryPlanError(f"Approval history line {index} has an invalid actor.")
        actors[item_id] = actor
    return actors


def _music_report(
    supplied: dict[str, Any] | None,
    music: MusicMetadata | None,
) -> dict[str, Any] | None:
    if supplied is None:
        return None
    complete = bool(
        supplied.get("user_confirmed_permitted_for_intended_use")
        and supplied.get("license_name_or_identifier")
        and supplied.get("license_evidence_reference")
    )
    return {
        **supplied,
        "track_filename": Path(supplied["path"]).name,
        "duration_seconds": music.duration_seconds if music else supplied.get("duration_seconds"),
        "status": "USER_CONFIRMED" if complete else "UNVERIFIED",
        "warnings": [
            "License confirmation is user supplied, not independent legal verification."
            if complete
            else "License evidence is incomplete; do not publish this music."
        ],
    }


def _available_ffmpeg_filters(executable: Path, required: set[str]) -> None:
    try:
        result = subprocess.run(
            [str(executable), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoryRenderError("Could not inspect local FFmpeg filters.") from exc
    if result.returncode != 0:
        raise StoryRenderError("FFmpeg could not list its available filters.")
    available = {
        fields[1]
        for line in result.stdout.splitlines()
        if (fields := line.split())
        and len(fields) >= 2
        and len(fields[0]) == 2
        and all(flag in ".TSC." for flag in fields[0])
    }
    missing = sorted(required - available)
    if missing:
        raise StoryRenderError(
            "Configured FFmpeg lacks required filters: " + ", ".join(missing)
        )


def request_ollama_suggestions(
    plan_path: str | Path,
    *,
    model: str,
    timeout_seconds: float = 60.0,
) -> Path:
    if not model or not model.strip():
        raise StoryPlanError("Specify an already-installed Ollama model.")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise StoryPlanError("Ollama timeout must be positive and finite.")
    payload, plan = _load_story_plan(plan_path)
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as response:
            tags = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise StoryPlanError(
            "Local Ollama is unavailable. Start it yourself; this command will not "
            "start Ollama or download a model."
        ) from exc
    installed = {
        item.get("name")
        for item in tags.get("models", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    } if isinstance(tags, dict) else set()
    if model not in installed:
        raise StoryPlanError(
            f"Ollama model {model!r} is not installed locally; no download was attempted."
        )

    allowed_ids = sorted(_decision_ids(payload))
    verified_text: list[dict[str, Any]] = []
    caption_asset = payload.get("captions")
    if caption_asset is not None and caption_asset.get("reviewed") is True:
        for index, cue in enumerate(load_caption_cues(caption_asset["path"])):
            if not cue.uncertain:
                verified_text.append(
                    {"cue_id": index + 1, "exact_text": cue.text}
                )
    response_schema = {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["item_id", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["suggestions"],
        "additionalProperties": False,
    }
    request_payload = {
        "model": model,
        "stream": False,
        "format": response_schema,
        "prompt": (
            "You are a local video-edit suggestion assistant. Return only structured "
            "suggestions for existing item IDs. Never invent dialogue, timings, or "
            "assets. Suggest whether existing silence candidates are worth human "
            "review; do not approve any item. Each reason must be under 300 chars. "
            "No transcript means you cannot infer story meaning. Input JSON: "
            + json.dumps(
                {
                    "items": [
                        {
                            "candidate_id": item["candidate_id"],
                            "source_interval_seconds": [
                                item["source_start_seconds"],
                                item["source_end_seconds"],
                            ],
                            "evidence": item["measured_evidence"],
                        }
                        for item in payload["candidates"]
                    ],
                    "verified_caption_text": verified_text,
                    "allowed_item_ids": allowed_ids,
                },
                ensure_ascii=False,
            )
        ),
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise StoryPlanError("Ollama returned no valid local planner response.") from exc
    try:
        suggestions_payload = json.loads(result["response"])
        suggestions = suggestions_payload["suggestions"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise StoryPlanError("Ollama response did not match the suggestion schema.") from exc
    if not isinstance(suggestions, list):
        raise StoryPlanError("Ollama suggestions must be a list.")
    for item in suggestions:
        if (
            not isinstance(item, dict)
            or set(item) != {"item_id", "reason"}
            or not isinstance(item["item_id"], str)
            or item["item_id"] not in allowed_ids
            or not isinstance(item["reason"], str)
            or not item["reason"].strip()
            or len(item["reason"]) > 300
        ):
            raise StoryPlanError(
                "Ollama suggested an invalid item or malformed reason; nothing was saved."
            )
    output = plan.parent / "ollama_suggestions.json"
    _write_json_exclusive(
        output,
        {
            "model": model,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "approval_status": "SUGGESTIONS_ONLY",
            "suggestions": suggestions,
        },
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create reviewable local story edits and dual-format previews."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="Analyze a source and create an approval-gated story plan."
    )
    plan_parser.add_argument("video", type=Path)
    plan_parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    plan_parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    plan_parser.add_argument("--captions", type=Path)
    plan_parser.add_argument("--title")
    plan_parser.add_argument("--music", type=Path)
    plan_parser.add_argument("--music-source-url", default="")
    plan_parser.add_argument("--music-provider", default="")
    plan_parser.add_argument("--music-license", default="")
    plan_parser.add_argument("--music-attribution", default="")
    plan_parser.add_argument("--music-evidence", default="")
    plan_parser.add_argument(
        "--confirm-music-license",
        action="store_true",
        help="Confirm supplied music is permitted for your intended use.",
    )

    review_parser = subparsers.add_parser(
        "review", help="Record an append-only human approval decision."
    )
    review_parser.add_argument("plan", type=Path)
    review_parser.add_argument("item_id")
    review_parser.add_argument("status", choices=("APPROVED", "REJECTED", "PENDING"))
    review_parser.add_argument("--note", default="")

    render_parser = subparsers.add_parser(
        "render", help="Render approved edits as landscape and vertical previews."
    )
    render_parser.add_argument("plan", type=Path)
    render_parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)

    finalize_parser = subparsers.add_parser(
        "finalize",
        help="Approve reviewed previews and create non-overwriting final export names.",
    )
    finalize_parser.add_argument("plan", type=Path)
    finalize_parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    finalize_parser.add_argument("--note", required=True)

    suggest_parser = subparsers.add_parser(
        "suggest", help="Ask an already-running local Ollama model for suggestions."
    )
    suggest_parser.add_argument("plan", type=Path)
    suggest_parser.add_argument("--model", required=True)
    suggest_parser.add_argument("--timeout", type=float, default=60)

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = create_story_plan(
                args.video,
                settings_path=args.settings,
                profile_path=args.profile,
                captions_path=args.captions,
                title=args.title,
                music_path=args.music,
                music_source_url=args.music_source_url,
                music_provider=args.music_provider,
                music_license=args.music_license,
                music_attribution=args.music_attribution,
                music_evidence=args.music_evidence,
                confirm_music_license=args.confirm_music_license,
            )
            print(f"Story plan: {result}")
            print(f"Review report: {result.with_suffix('.md')}")
            print("No changes were made to the source video.")
            return 0
        if args.command == "review":
            result = record_story_decision(
                args.plan,
                args.item_id,
                args.status,
                note=args.note,
            )
            print(f"Decision recorded: {result}")
            return 0
        if args.command == "suggest":
            result = request_ollama_suggestions(
                args.plan,
                model=args.model,
                timeout_seconds=args.timeout,
            )
            print(f"Human-review-only suggestions: {result}")
            return 0
        if args.command == "finalize":
            result = approve_story_exports(
                args.plan,
                note=args.note,
                settings_path=args.settings,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        result = render_story_edit(args.plan, settings_path=args.settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        AudioAnalysisError,
        CaptionError,
        EditPlanError,
        MediaReviewError,
        PostProductionError,
        ProbeError,
        StoryEditorError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
