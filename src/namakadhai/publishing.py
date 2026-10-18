"""Build a local, non-publishing delivery package from approved story exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from namakadhai.caption_renderer import load_caption_cues
from namakadhai.media_review import _resolve_ffmpeg


class PublishingError(RuntimeError):
    """A publishing package could not be created safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(value: str, fallback: str = "nama-kadhai") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return cleaned[:60] or fallback


def _write_exclusive(path: Path, content: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="") as output:
            output.write(content)
    except FileExistsError as exc:
        raise PublishingError(f"Refusing to overwrite publishing file: {path.name}") from exc
    except OSError as exc:
        raise PublishingError(f"Could not write publishing file: {path.name}") from exc


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        raise PublishingError(f"Refusing to overwrite publishing file: {destination.name}")
    try:
        os.link(source, destination)
    except OSError:
        try:
            with destination.open("xb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise PublishingError(f"Could not copy {source.name} into the package.") from exc


def _subtitle_time(seconds: float, separator: str) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"


def _subtitle_files(caption_path: Path, destination: Path) -> list[Path]:
    cues = [cue for cue in load_caption_cues(caption_path) if not cue.uncertain]
    if not cues:
        return []
    srt = destination / "reviewed_captions.srt"
    vtt = destination / "reviewed_captions.vtt"
    srt_rows: list[str] = []
    vtt_rows = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        srt_rows.extend(
            (
                str(index),
                f"{_subtitle_time(cue.start_seconds, ',')} --> {_subtitle_time(cue.end_seconds, ',')}",
                cue.text,
                "",
            )
        )
        vtt_rows.extend(
            (
                f"{_subtitle_time(cue.start_seconds, '.')} --> {_subtitle_time(cue.end_seconds, '.')}",
                cue.text,
                "",
            )
        )
    _write_exclusive(srt, "\n".join(srt_rows))
    _write_exclusive(vtt, "\n".join(vtt_rows))
    return [srt, vtt]


def _thumbnail(
    video: Path,
    destination: Path,
    *,
    at_seconds: float,
    ffmpeg: Path,
) -> None:
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, at_seconds):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#281d18",
        "-q:v",
        "2",
        str(destination),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=90,
        shell=False,
    )
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        detail = result.stderr.strip()[-500:]
        raise PublishingError(f"FFmpeg could not create {destination.name}. {detail}".strip())


def create_publishing_package(
    plan_path: str | Path,
    *,
    project_name: str,
    title: str = "",
    description: str = "",
    hashtags: str = "",
    social_caption: str = "",
    pinned_comment: str = "",
    settings_path: str | Path | None = None,
    exports_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Package only finalized, approval-bound files; never upload anything."""
    plan = Path(plan_path).expanduser().resolve()
    try:
        plan_payload = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishingError("Could not read the approved story plan.") from exc
    exports = (
        Path(exports_directory).expanduser().resolve()
        if exports_directory is not None
        else plan.parent / "exports"
    )
    try:
        exports.relative_to(plan.parent.resolve())
    except ValueError as exc:
        raise PublishingError("Approved exports directory must stay inside the story plan.") from exc
    approval_path = exports / "final_approval.json"
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishingError("Finalize both previews before building a package.") from exc
    if approval.get("plan_sha256") != _sha256(plan):
        raise PublishingError("Final approval does not match the current story plan.")

    approved_outputs = approval.get("outputs")
    if not isinstance(approved_outputs, dict):
        raise PublishingError("Final approval is missing validated output hashes.")
    approved_reports: list[Path] = []
    final_landscape = exports / "final_landscape.mp4"
    final_vertical = exports / "final_vertical.mp4"
    for format_name, final in (
        ("landscape", final_landscape),
        ("vertical", final_vertical),
    ):
        if not final.is_file() or final.stat().st_size == 0:
            raise PublishingError(f"Missing finalized output: {final.name}")
        approval_record = approved_outputs.get(format_name)
        if not isinstance(approval_record, dict):
            raise PublishingError(f"Final approval is missing the {format_name} output hash.")
        try:
            final_hash = _sha256(final)
        except OSError as exc:
            raise PublishingError(f"Could not verify finalized output: {final.name}") from exc
        if approval_record.get("preview_sha256") != final_hash:
            raise PublishingError(
                f"Finalized {format_name} output changed after approval."
            )
        report_path = exports / f"review_{format_name}.render-report.json"
        if not report_path.is_file() or not report_path.stat().st_size:
            raise PublishingError(f"Missing approved render report: {report_path.name}")
        try:
            report_hash = _sha256(report_path)
        except OSError as exc:
            raise PublishingError(
                f"Could not verify approved render report: {report_path.name}"
            ) from exc
        if approval_record.get("report_sha256") != report_hash:
            raise PublishingError(
                f"The {format_name} render report changed after approval."
            )
        approved_reports.append(report_path)

    run_id = datetime.now(timezone.utc).strftime("package-%Y%m%dT%H%M%S-%fZ")
    destination = plan.parent / "publishing" / run_id
    destination.mkdir(parents=True, exist_ok=False)
    created: list[Path] = []
    try:
        public_title = title.strip()
        if not public_title and isinstance(plan_payload.get("title"), dict):
            public_title = str(plan_payload["title"].get("text") or "").strip()
        public_title = public_title or project_name.strip() or "Nama Kadhai story"
        tag_text = hashtags.strip() or "#NamaKadhai #TamilStories #Storytelling"
        metadata = {
            "status": "EDITABLE_DRAFTS_NOT_PUBLISHED",
            "title_variants": [
                public_title,
                f"{public_title} | Nama Kadhai",
                f"A story to remember — {public_title}",
            ],
            "description": description.strip(),
            "hashtags": tag_text,
            "social_caption": social_caption.strip(),
            "pinned_comment": pinned_comment.strip(),
            "disclaimer": "Draft metadata only. Review before publishing; reach is not guaranteed.",
        }
        metadata_json = destination / "publishing_metadata.json"
        _write_exclusive(metadata_json, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        created.append(metadata_json)
        metadata_md = destination / "publishing_metadata.md"
        _write_exclusive(
            metadata_md,
            "\n".join(
                (
                    "# Publishing drafts",
                    "",
                    "> Editable drafts only. Nothing has been uploaded or published.",
                    "",
                    "## Title options",
                    *(f"- {item}" for item in metadata["title_variants"]),
                    "",
                    "## Description",
                    metadata["description"] or "[Add a reviewed description]",
                    "",
                    "## Hashtags",
                    metadata["hashtags"],
                    "",
                    "## Social caption",
                    metadata["social_caption"] or "[Add a reviewed social caption]",
                    "",
                    "## Pinned comment",
                    metadata["pinned_comment"] or "[Add a reviewed pinned comment]",
                    "",
                )
            ),
        )
        created.append(metadata_md)

        for source, name in (
            (final_landscape, f"{_safe_name(project_name)}_youtube.mp4"),
            (final_vertical, f"{_safe_name(project_name)}_reels-shorts.mp4"),
            (approval_path, "final_approval.json"),
        ):
            target = destination / name
            _link_or_copy(source, target)
            created.append(target)
        for report in approved_reports:
            target = destination / report.name
            _link_or_copy(report, target)
            created.append(target)

        captions = plan_payload.get("captions")
        if isinstance(captions, dict) and captions.get("path"):
            caption_path = Path(str(captions["path"])).resolve()
            if _sha256(caption_path) != captions.get("sha256"):
                raise PublishingError("Reviewed captions changed after the plan was created.")
            created.extend(_subtitle_files(caption_path, destination))

        duration = float(plan_payload["source"]["duration_seconds"])
        ffmpeg = _resolve_ffmpeg(settings_path)
        for index, fraction in enumerate((0.2, 0.5, 0.8), start=1):
            thumbnail = destination / f"thumbnail_option_{index}.jpg"
            _thumbnail(final_landscape, thumbnail, at_seconds=duration * fraction, ffmpeg=ffmpeg)
            created.append(thumbnail)

        manifest_path = destination / "package_manifest.json"
        manifest = {
            "version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "package_id": uuid.uuid4().hex,
            "project_name": project_name,
            "plan_path": str(plan),
            "plan_sha256": _sha256(plan),
            "approval_sha256": _sha256(approval_path),
            "publishing_action": "LOCAL_PACKAGE_ONLY",
            "files": [
                {
                    "name": item.name,
                    "size_bytes": item.stat().st_size,
                    "sha256": _sha256(item),
                }
                for item in created
            ],
        }
        _write_exclusive(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        created.append(manifest_path)
    except Exception:
        for item in created:
            item.unlink(missing_ok=True)
        try:
            destination.rmdir()
            destination.parent.rmdir()
        except OSError:
            pass
        raise
    return {
        "directory": str(destination),
        "manifest": str(destination / "package_manifest.json"),
        "files": [str(item) for item in created],
    }
