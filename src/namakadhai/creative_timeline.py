"""Validated, source-bound creative timeline for dynamic vertical edits.

The timeline contains declarative, allowlisted editing decisions.  It never
contains arbitrary FFmpeg expressions and never treats an unapproved external
asset as publishable media.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from namakadhai.caption_renderer import load_caption_cues
from namakadhai.probe import probe_video

TIMELINE_VERSION = 1
LAYOUTS = frozenset({"presenter_full", "split_top", "broll_full", "circle_pip"})
TRANSITIONS = frozenset({"cut", "soft_fade", "light_sweep", "smear_flash"})
MOTIONS = frozenset({"hold", "push_in", "push_out"})
REACTIONS = frozenset({"none", "spark", "question", "heart", "comment"})
ASSET_KINDS = frozenset({"video", "image", "music", "sfx", "logo"})
STRONG_TRANSITIONS = frozenset({"light_sweep", "smear_flash"})
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


class CreativeTimelineError(RuntimeError):
    """A creative plan or asset catalogue is unsafe or invalid."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise CreativeTimelineError(f"{label} must be numeric.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CreativeTimelineError(f"{label} must be numeric.") from exc
    if not math.isfinite(parsed) or parsed < minimum:
        raise CreativeTimelineError(f"{label} is outside the allowed range.")
    return parsed


def _atomic_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise CreativeTimelineError(f"Refusing to overwrite {path.name}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        with staged.open("x", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        staged.replace(path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise CreativeTimelineError(f"Could not write {path.name}.") from exc


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    kind: str
    path: str
    sha256: str
    approved: bool
    license: str
    provider: str = "local"
    source_url: str = ""
    attribution: str = ""
    tags: tuple[str, ...] = ()


def load_asset_catalog(path: str | Path | None) -> dict[str, AssetRecord]:
    """Load and verify explicitly approved local assets and provenance."""
    if path is None:
        return {}
    catalog_path = Path(path).resolve()
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CreativeTimelineError("Could not read the asset catalogue.") from exc
    except json.JSONDecodeError as exc:
        raise CreativeTimelineError("Asset catalogue contains invalid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise CreativeTimelineError("Asset catalogue must use schema version 1.")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise CreativeTimelineError("Asset catalogue needs an assets list.")
    assets: dict[str, AssetRecord] = {}
    for index, raw in enumerate(raw_assets, start=1):
        if not isinstance(raw, dict):
            raise CreativeTimelineError(f"Asset {index} must be an object.")
        asset_id = raw.get("asset_id")
        kind = raw.get("kind")
        local = Path(str(raw.get("path", ""))).expanduser()
        if not local.is_absolute():
            local = catalog_path.parent / local
        local = local.resolve()
        approved = raw.get("approved") is True
        license_name = raw.get("license")
        tags = raw.get("tags", [])
        if not isinstance(asset_id, str) or not SAFE_ID.fullmatch(asset_id):
            raise CreativeTimelineError(f"Asset {index} has an invalid asset_id.")
        if asset_id in assets:
            raise CreativeTimelineError(f"Duplicate asset_id: {asset_id}")
        if kind not in ASSET_KINDS:
            raise CreativeTimelineError(f"Asset {asset_id} has an unsupported kind.")
        if not local.is_file():
            raise CreativeTimelineError(f"Asset {asset_id} local file is missing.")
        if not isinstance(license_name, str) or not license_name.strip():
            raise CreativeTimelineError(f"Asset {asset_id} needs a recorded license.")
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise CreativeTimelineError(f"Asset {asset_id} tags must be strings.")
        expected_hash = raw.get("sha256")
        actual_hash = sha256_file(local)
        if expected_hash is not None and expected_hash != actual_hash:
            raise CreativeTimelineError(f"Asset {asset_id} changed after catalogue entry.")
        assets[asset_id] = AssetRecord(
            asset_id=asset_id,
            kind=kind,
            path=str(local),
            sha256=actual_hash,
            approved=approved,
            license=license_name.strip(),
            provider=str(raw.get("provider", "local")),
            source_url=str(raw.get("source_url", "")),
            attribution=str(raw.get("attribution", "")),
            tags=tuple(tag.strip().casefold() for tag in tags if tag.strip()),
        )
    return assets


def _event_boundaries(cues: tuple[Any, ...], duration: float, target: float = 2.4) -> list[float]:
    boundaries = [0.0]
    for cue in cues:
        end = min(duration, float(cue.end_seconds))
        if end - boundaries[-1] >= target:
            boundaries.append(end)
    if duration - boundaries[-1] < 0.6 and len(boundaries) > 1:
        boundaries[-1] = duration
    elif duration > boundaries[-1]:
        boundaries.append(duration)
    return boundaries


def _matching_asset(text: str, assets: dict[str, AssetRecord], used: set[str]) -> AssetRecord | None:
    normalized = text.casefold()
    tokens = set(re.findall(r"[\w-]+", normalized, re.UNICODE))

    def matches(tag: str) -> bool:
        tag = tag.strip().casefold()
        return (tag in normalized) if " " in tag else (tag in tokens)

    choices = [
        asset for asset in assets.values()
        if asset.approved and asset.kind in {"video", "image"}
        and asset.asset_id not in used
        and any(matches(tag) for tag in asset.tags)
    ]
    if not choices:
        return None
    return sorted(choices, key=lambda asset: asset.asset_id)[0]


def _reaction_for_text(text: str, *, has_visual_asset: bool) -> str:
    """Choose a sparse allowlisted reaction from dialogue, never arbitrary artwork."""
    if has_visual_asset:
        return "none"
    normalized = text.casefold()
    tokens = set(re.findall(r"[\w-]+", normalized, re.UNICODE))
    if "comment" in normalized:
        return "comment"
    if "?" in text or tokens.intersection({"enna", "theriyumaa", "sollunga"}):
        return "question"
    if tokens.intersection({"family", "family-aa", "ninaivugal", "moments", "moments-ah"}):
        return "heart"
    if tokens.intersection({"excitement", "azhaga", "happy", "satisfaction"}):
        return "spark"
    return "none"


def _approved_asset(assets: dict[str, AssetRecord], asset_id: str) -> AssetRecord | None:
    asset = assets.get(asset_id)
    return asset if asset and asset.approved else None


def create_creative_timeline(
    source_path: str | Path,
    captions_path: str | Path,
    output_path: str | Path,
    *,
    asset_catalog_path: str | Path | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    target_lufs: float = -15.0,
    true_peak_dbtp: float = -1.0,
    source_start_seconds: float = 0.0,
    source_end_seconds: float | None = None,
) -> dict[str, Any]:
    """Create a conservative auto draft from reviewed, timed captions."""
    source = Path(source_path).resolve()
    captions = Path(captions_path).resolve()
    destination = Path(output_path).resolve()
    metadata = probe_video(source)
    if metadata.duration_seconds is None or metadata.audio is None:
        raise CreativeTimelineError("Dynamic editing needs a known duration and dialogue audio.")
    source_start_seconds = _finite(source_start_seconds, "source trim start")
    source_end_seconds = (
        metadata.duration_seconds
        if source_end_seconds is None
        else _finite(source_end_seconds, "source trim end", minimum=0.05)
    )
    if source_end_seconds > metadata.duration_seconds + 0.002 or source_end_seconds <= source_start_seconds:
        raise CreativeTimelineError("Source trim range is outside the media duration.")
    edit_duration = source_end_seconds - source_start_seconds
    if width < 360 or height < 640 or width > 2160 or height > 3840 or fps not in range(12, 61):
        raise CreativeTimelineError("Output dimensions or frame rate are outside safe limits.")
    cues = load_caption_cues(captions)
    if cues[-1].end_seconds > edit_duration + 0.05:
        raise CreativeTimelineError("Captions extend beyond the edited output duration.")
    assets = load_asset_catalog(asset_catalog_path)
    boundaries = _event_boundaries(cues, edit_duration)
    used_assets: set[str] = set()
    events: list[dict[str, Any]] = []
    first_strong = min(range(1, len(boundaries)), key=lambda i: abs(boundaries[i] - 7.0))
    final_strong = min(range(1, len(boundaries)), key=lambda i: abs(boundaries[i] - max(10.0, edit_duration - 8.0)))
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        event_cues = [
            (cue_index, cue) for cue_index, cue in enumerate(cues)
            if cue.start_seconds < end and cue.end_seconds > start
        ]
        event_text = " ".join(cue.text for _, cue in event_cues)
        asset = _matching_asset(event_text, assets, used_assets)
        if asset:
            used_assets.add(asset.asset_id)
            if asset.asset_id.endswith("your-story"):
                layout = "broll_full"
            elif start < 6.5:
                layout = "circle_pip"
            else:
                layout = "split_top" if index % 2 else "broll_full"
        else:
            layout = "presenter_full"
        transition = "cut"
        transition_duration = 0.0
        boundary_index = index
        if index > 0 and boundary_index == first_strong:
            transition, transition_duration = "light_sweep", 0.42
        elif index > 0 and boundary_index == final_strong and final_strong != first_strong:
            transition, transition_duration = "smear_flash", 0.32
        if asset and asset.asset_id.endswith("your-story") and transition == "cut":
            transition, transition_duration = "soft_fade", 0.28
        events.append(
            {
                "event_id": f"event-{index + 1:03d}",
                "output_start": round(start, 3),
                "output_end": round(end, 3),
                "source_start": round(source_start_seconds + start, 3),
                "source_end": round(source_start_seconds + end, 3),
                "layout": layout,
                "asset_id": asset.asset_id if asset else None,
                "asset_start": 0.0,
                "caption_ids": [cue_index for cue_index, _ in event_cues],
                "motion": "push_in" if index % 2 == 0 else "push_out",
                "reaction": _reaction_for_text(event_text, has_visual_asset=asset is not None),
                "transition_in": transition,
                "transition_duration": transition_duration,
                "decision": {"status": "AUTO", "reason": "reference_dynamic_v1 rule set"},
            }
        )
    pop = _approved_asset(assets, "generated-pop")
    whoosh = _approved_asset(assets, "generated-whoosh")
    impact = _approved_asset(assets, "generated-impact")
    sfx_events: list[dict[str, Any]] = []
    for event in events:
        start = float(event["output_start"])
        if event["transition_in"] in STRONG_TRANSITIONS and whoosh:
            sfx_events.append(
                {"asset_id": whoosh.asset_id, "start": round(start, 3),
                 "duration": 0.42, "gain_db": -22.0, "reason": "emphasis transition"}
            )
        asset_id = str(event.get("asset_id") or "")
        if asset_id.endswith("your-story") and impact:
            sfx_events.append(
                {"asset_id": impact.asset_id, "start": round(start + 0.08, 3),
                 "duration": 0.28, "gain_db": -20.0, "reason": "closing card"}
            )
        elif asset_id and pop:
            sfx_events.append(
                {"asset_id": pop.asset_id, "start": round(start + 0.06, 3),
                 "duration": 0.14, "gain_db": -24.0, "reason": "topic graphic"}
            )
    sfx_events = [
        event for event in sfx_events
        if float(event["start"]) + float(event["duration"]) <= edit_duration + 0.001
    ]

    payload = {
        "version": TIMELINE_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": "reference_dynamic_v1",
        "source": {
            "path": str(source),
            "sha256": sha256_file(source),
            "duration": round(metadata.duration_seconds, 6),
            "width": metadata.video.width,
            "height": metadata.video.height,
            "fps": metadata.video.frame_rate,
            "selected_start": round(source_start_seconds, 6),
            "selected_end": round(source_end_seconds, 6),
        },
        "captions": {"path": str(captions), "sha256": sha256_file(captions), "count": len(cues)},
        "assets": {asset_id: asdict(asset) for asset_id, asset in assets.items()},
        "output": {"width": width, "height": height, "fps": fps, "duration": round(edit_duration, 6)},
        "audio": {
            "target_lufs": target_lufs,
            "true_peak_dbtp": true_peak_dbtp,
            "loudness_range_lu": 7.0,
            "music_asset_id": None,
            "music_gain_db": -26.0,
            "ducking": {"enabled": True, "threshold_db": -30.0, "ratio": 8.0, "attack_ms": 20.0, "release_ms": 350.0},
        },
        "events": events,
        "sfx_events": sfx_events,
        "approval": {"creative_decisions": "AUTO_DRAFT", "final_human_review": "REQUIRED"},
    }
    validate_creative_timeline(payload, verify_files=True)
    _atomic_json(destination, payload)
    return payload


def validate_creative_timeline(payload: dict[str, Any], *, verify_files: bool = True) -> None:
    """Reject malformed, stale, unbounded, or unapproved creative operations."""
    if not isinstance(payload, dict) or payload.get("version") != TIMELINE_VERSION:
        raise CreativeTimelineError("Creative timeline must use schema version 1.")
    source = payload.get("source")
    captions = payload.get("captions")
    output = payload.get("output")
    events = payload.get("events")
    assets = payload.get("assets", {})
    if not all(isinstance(item, dict) for item in (source, captions, output, assets)):
        raise CreativeTimelineError("Timeline source, captions, output and assets must be objects.")
    if not isinstance(events, list) or not events:
        raise CreativeTimelineError("Timeline needs at least one event.")
    duration = _finite(output.get("duration"), "output duration", minimum=0.05)
    width, height = output.get("width"), output.get("height")
    fps = output.get("fps")
    if isinstance(width, bool) or not isinstance(width, int) or not 360 <= width <= 2160:
        raise CreativeTimelineError("Output width is outside safe limits.")
    if isinstance(height, bool) or not isinstance(height, int) or not 640 <= height <= 3840:
        raise CreativeTimelineError("Output height is outside safe limits.")
    if isinstance(fps, bool) or not isinstance(fps, int) or not 12 <= fps <= 60:
        raise CreativeTimelineError("Output frame rate is outside safe limits.")
    source_duration = _finite(source.get("duration"), "source duration", minimum=0.05)
    if verify_files:
        for item, label in ((source, "source"), (captions, "captions")):
            file_path = Path(str(item.get("path", ""))).resolve()
            if not file_path.is_file() or sha256_file(file_path) != item.get("sha256"):
                raise CreativeTimelineError(f"Timeline {label} file is missing or changed.")
    previous_end = 0.0
    strong = 0
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise CreativeTimelineError(f"Event {index} must be an object.")
        start = _finite(event.get("output_start"), f"event {index} output start")
        end = _finite(event.get("output_end"), f"event {index} output end", minimum=0.01)
        source_start = _finite(event.get("source_start"), f"event {index} source start")
        source_end = _finite(event.get("source_end"), f"event {index} source end", minimum=0.01)
        if abs(start - previous_end) > 0.002 or end <= start or source_end <= source_start:
            raise CreativeTimelineError("Timeline events must be positive, ordered and gap-free.")
        if source_end > source_duration + 0.002:
            raise CreativeTimelineError(f"Event {index} exceeds the source duration.")
        if abs((end - start) - (source_end - source_start)) > 0.003:
            raise CreativeTimelineError("Speed changes are not supported by this timeline version.")
        layout = event.get("layout")
        transition = event.get("transition_in")
        motion = event.get("motion")
        reaction = event.get("reaction", "none")
        transition_duration = _finite(
            event.get("transition_duration", 0), f"event {index} transition duration"
        )
        if (
            layout not in LAYOUTS or transition not in TRANSITIONS
            or motion not in MOTIONS or reaction not in REACTIONS
        ):
            raise CreativeTimelineError(f"Event {index} uses an unsupported template.")
        if transition_duration > 0.6 or (transition == "cut" and transition_duration != 0):
            raise CreativeTimelineError(f"Event {index} transition duration is unsafe.")
        if transition in STRONG_TRANSITIONS:
            strong += 1
        asset_id = event.get("asset_id")
        if layout != "presenter_full":
            asset = assets.get(asset_id)
            if not isinstance(asset, dict) or asset.get("approved") is not True:
                raise CreativeTimelineError(f"Event {index} needs an approved visual asset.")
            if asset.get("kind") not in {"video", "image"}:
                raise CreativeTimelineError(f"Event {index} asset is not visual media.")
            if verify_files:
                asset_path = Path(str(asset.get("path", ""))).resolve()
                if not asset_path.is_file() or sha256_file(asset_path) != asset.get("sha256"):
                    raise CreativeTimelineError(f"Event {index} asset is missing or changed.")
        previous_end = end
    if abs(previous_end - duration) > 0.003:
        raise CreativeTimelineError("Timeline events do not cover the complete output duration.")
    strong_limit = max(2, math.ceil(duration / 60 * 3))
    if strong > strong_limit:
        raise CreativeTimelineError("Strong transition density exceeds the safety limit.")
    audio = payload.get("audio")
    if not isinstance(audio, dict):
        raise CreativeTimelineError("Timeline audio settings must be an object.")
    target = _finite(audio.get("target_lufs"), "target loudness", minimum=-40)
    peak = _finite(audio.get("true_peak_dbtp"), "true peak", minimum=-10)
    if not -24 <= target <= -12 or not -6 <= peak <= -0.5:
        raise CreativeTimelineError("Audio target is outside the safe social-video range.")
    music_id = audio.get("music_asset_id")
    if music_id is not None:
        music = assets.get(music_id)
        if not isinstance(music, dict) or music.get("approved") is not True or music.get("kind") != "music":
            raise CreativeTimelineError("Music must reference an approved music asset.")
        gain = _finite(audio.get("music_gain_db", -26), "music gain", minimum=-50)
        if gain > -8:
            raise CreativeTimelineError("Music gain is too high for the dialogue-first mix.")
    ducking = audio.get("ducking", {})
    if not isinstance(ducking, dict) or not isinstance(ducking.get("enabled", False), bool):
        raise CreativeTimelineError("Audio ducking settings are invalid.")
    if ducking.get("enabled"):
        threshold = _finite(ducking.get("threshold_db", -30), "ducking threshold", minimum=-60)
        ratio = _finite(ducking.get("ratio", 8), "ducking ratio", minimum=1)
        attack = _finite(ducking.get("attack_ms", 20), "ducking attack", minimum=1)
        release = _finite(ducking.get("release_ms", 350), "ducking release", minimum=1)
        if threshold > -6 or ratio > 20 or attack > 2000 or release > 9000:
            raise CreativeTimelineError("Audio ducking settings exceed safe limits.")
    sfx_events = payload.get("sfx_events", [])
    if not isinstance(sfx_events, list):
        raise CreativeTimelineError("SFX events must be a list.")
    for index, sfx in enumerate(sfx_events, start=1):
        if not isinstance(sfx, dict):
            raise CreativeTimelineError(f"SFX event {index} must be an object.")
        asset = assets.get(sfx.get("asset_id"))
        if not isinstance(asset, dict) or asset.get("approved") is not True or asset.get("kind") != "sfx":
            raise CreativeTimelineError(f"SFX event {index} needs an approved SFX asset.")
        start = _finite(sfx.get("start"), f"SFX event {index} start")
        sfx_duration = _finite(sfx.get("duration", 2), f"SFX event {index} duration", minimum=0.05)
        gain = _finite(sfx.get("gain_db", -16), f"SFX event {index} gain", minimum=-40)
        if start >= duration or start + sfx_duration > duration + 0.05 or sfx_duration > 5 or gain > -6:
            raise CreativeTimelineError(f"SFX event {index} is outside safe bounds.")
    if verify_files:
        referenced = {event.get("asset_id") for event in events if event.get("asset_id")}
        referenced.update(sfx.get("asset_id") for sfx in sfx_events)
        if music_id:
            referenced.add(music_id)
        for asset_id in referenced:
            asset = assets[asset_id]
            asset_path = Path(str(asset.get("path", ""))).resolve()
            if not asset_path.is_file() or sha256_file(asset_path) != asset.get("sha256"):
                raise CreativeTimelineError(f"Referenced asset {asset_id} is missing or changed.")


def load_creative_timeline(path: str | Path, *, verify_files: bool = True) -> dict[str, Any]:
    timeline_path = Path(path).resolve()
    try:
        payload = json.loads(timeline_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CreativeTimelineError("Could not read the creative timeline.") from exc
    except json.JSONDecodeError as exc:
        raise CreativeTimelineError("Creative timeline contains invalid JSON.") from exc
    validate_creative_timeline(payload, verify_files=verify_files)
    return payload

