"""Stateful application layer for the local Nama Kadhai editing workflow."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from namakadhai.auto_edit import FasterWhisperProvider, suggest_transcript_cuts
from namakadhai.caption_renderer import load_caption_cues
from namakadhai.probe import probe_video
from namakadhai.publishing import create_publishing_package
from namakadhai.story_editor import (
    DEFAULT_PROFILE_PATH,
    DEFAULT_SETTINGS_PATH,
    StoryPlanError,
    StoryRenderError,
    approve_story_exports,
    create_story_plan,
    record_story_decision,
    record_story_preview_review,
    render_story_edit,
)
from namakadhai.video_analysis import analyze_video_signals

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALIGNMENT_PYTHON = PROJECT_ROOT / ".venv-align" / "Scripts" / "python.exe"


class ApplicationError(RuntimeError):
    """The local application could not complete a workflow action."""


class ProjectNotFoundError(ApplicationError):
    """The requested project does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return cleaned[:48] or "story"


class ProjectApplication:
    """Coordinate durable project records with the existing story renderer."""

    def __init__(self, settings_path: str | Path | None = None) -> None:
        self.settings_path = Path(settings_path or DEFAULT_SETTINGS_PATH).resolve()
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            configured = Path(settings["paths"]["projects"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ApplicationError("Could not load application project settings.") from exc
        root = self.settings_path.parent.parent
        self.projects_root = (configured if configured.is_absolute() else root / configured) / "app"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def transcription_provider(self) -> FasterWhisperProvider:
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            configured = settings["transcription"]
            model = str(configured["model"])
            threshold = float(configured.get("confidence_threshold", 0.35))
            models = Path(settings["paths"]["models"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ApplicationError("Transcription settings are incomplete.") from exc
        root = self.settings_path.parent.parent
        model_directory = (models if models.is_absolute() else root / models) / "whisper" / model
        return FasterWhisperProvider(
            model_name=model,
            model_directory=model_directory,
            language=str(configured.get("language", "ta")),
            confidence_threshold=threshold,
        )

    def transcription_status(self) -> dict[str, Any]:
        return self.transcription_provider().status()

    @staticmethod
    def caption_alignment_ready() -> bool:
        return ALIGNMENT_PYTHON.is_file()

    def _align_srt_captions(
        self, srt: Path, source: Path, directory: Path, original: Path | None = None
    ) -> Path:
        """Word-align SRT text to the source speech in the separate alignment environment.

        With ``original``, ``source`` is an edit of it (e.g. an OpenReel export):
        words are timed on the original and moved onto the edit.
        """
        if not srt.is_file():
            raise ApplicationError(f"Caption file does not exist: {srt.name}")
        if not self.caption_alignment_ready():
            raise ApplicationError(
                "SRT captions need the alignment environment (.venv-align); see the README, "
                "'Captioning a video'."
            )
        output = directory / "captions_aligned.json"
        environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src"))
        try:
            result = subprocess.run(
                [str(ALIGNMENT_PYTHON), "-m", "namakadhai.forced_align",
                 str(srt), str(original or source), str(output)]
                + (["--edited", str(source)] if original else []),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False, timeout=3600, shell=False, env=environment, cwd=PROJECT_ROOT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApplicationError("Caption alignment could not run.") from exc
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ApplicationError(
                "Caption alignment failed" + (f": {detail[-1]}" if detail else ".")
            )
        return output

    def _directory(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,100}", project_id):
            raise ProjectNotFoundError("Project ID is invalid.")
        directory = self.projects_root / project_id
        if not directory.is_dir():
            raise ProjectNotFoundError("Project was not found.")
        return directory

    def _record_path(self, project_id: str) -> Path:
        return self._directory(project_id) / "project.json"

    def _read(self, project_id: str) -> dict[str, Any]:
        path = self._record_path(project_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplicationError("Project record is unreadable.") from exc
        if not isinstance(payload, dict) or payload.get("project_id") != project_id:
            raise ApplicationError("Project record has an unsupported schema.")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        path = self._record_path(str(payload["project_id"]))
        payload["updated_at_utc"] = _now()
        staged = path.with_name(f".{uuid.uuid4().hex}.partial.json")
        try:
            with staged.open("x", encoding="utf-8", newline="") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(staged, path)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            raise ApplicationError("Could not update the project record safely.") from exc

    def _event(self, project_id: str, kind: str, detail: dict[str, Any]) -> None:
        event = {"at_utc": _now(), "kind": kind, "detail": detail}
        try:
            with (self._directory(project_id) / "events.jsonl").open(
                "a", encoding="utf-8", newline=""
            ) as output:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise ApplicationError("Could not append the project event log.") from exc

    def create_project(
        self,
        source_path: str | Path,
        *,
        project_name: str = "",
        title: str = "",
        captions_path: str | Path | None = None,
        music_path: str | Path | None = None,
        original_path: str | Path | None = None,
        crop_x: float = 0.5,
        editing_intensity: str = "balanced",
        captions_enabled: bool = True,
        auto_mode: bool = False,
        transcription_enabled: bool = False,
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise ApplicationError(f"Video file does not exist: {source.name}")
        try:
            crop = float(crop_x)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("Vertical crop must be between 0 and 1.") from exc
        if not math.isfinite(crop) or not 0 <= crop <= 1:
            raise ApplicationError("Vertical crop must be between 0 and 1.")
        if editing_intensity not in {"subtle", "balanced", "dynamic"}:
            raise ApplicationError("Editing intensity must be subtle, balanced, or dynamic.")
        name = project_name.strip() or source.stem
        project_id = f"{_slug(name)}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        directory = self.projects_root / project_id
        directory.mkdir(parents=True, exist_ok=False)
        record_path = directory / "project.json"
        events_path = directory / "events.jsonl"
        try:
            metadata = probe_video(source, settings_path=self.settings_path)
            source_hash = _sha256(source)
            record: dict[str, Any] = {
                "version": 1,
                "project_id": project_id,
                "name": name,
                "created_at_utc": _now(),
                "updated_at_utc": _now(),
                "state": "ANALYZING",
                "source": {
                    "path": str(source),
                    "sha256": source_hash,
                    "preserved": True,
                    "metadata": metadata.to_dict(),
                },
                "preferences": {
                    "crop_x": crop,
                    "editing_intensity": editing_intensity,
                    "captions_enabled": bool(captions_enabled),
                    "title": title.strip() or None,
                    "auto_mode": bool(auto_mode),
                    "transcription_enabled": bool(transcription_enabled),
                    "caption_mode": "tanglish_draft",
                },
                "plan_revisions": [],
                "active_plan": None,
                "visual_analysis": None,
                "transcription": None,
                "automation": None,
                "render_result": None,
                "preview_reviews": {},
                "finalization": None,
                "package": None,
                "last_error": None,
            }
            with record_path.open("x", encoding="utf-8", newline="") as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.write("\n")
            events_path.touch(exist_ok=False)
            record["visual_analysis"] = analyze_video_signals(
                source,
                metadata,
                directory / "visual_analysis.json",
                settings_path=self.settings_path,
            )
            self._write(record)
            if captions_enabled and captions_path and Path(captions_path).suffix.lower() == ".srt":
                srt = Path(captions_path).expanduser().resolve()
                original = Path(original_path).expanduser().resolve() if original_path else None
                if original is not None and not original.is_file():
                    raise ApplicationError(f"Original recording does not exist: {original.name}")
                captions_path = self._align_srt_captions(srt, source, directory, original)
                alignment = json.loads(captions_path.read_text(encoding="utf-8"))["source"]["alignment"]
                record["caption_alignment"] = {"srt_path": str(srt), **alignment}
                self._write(record)
            profile_path = self._profile_for_project(directory, crop, editing_intensity)
            plan = create_story_plan(
                source,
                settings_path=self.settings_path,
                profile_path=profile_path,
                captions_path=(captions_path if captions_enabled and captions_path else None),
                title=(title.strip() or None),
                music_path=(music_path if music_path else None),
            )
            record["active_plan"] = str(plan)
            record["plan_revisions"].append(
                {"revision": 1, "created_at_utc": _now(), "plan_path": str(plan), "reason": "initial"}
            )
            record["state"] = "REVIEW"
            self._write(record)
            self._event(project_id, "PROJECT_CREATED", {"plan_path": str(plan)})
            if _sha256(source) != source_hash:
                raise ApplicationError("Source changed while the project was being created.")
        except Exception:
            if record_path.exists():
                try:
                    current = json.loads(record_path.read_text(encoding="utf-8"))
                    current["state"] = "ERROR"
                    current["last_error"] = "Project creation did not complete. See server diagnostics."
                    self._write(current)
                except Exception:
                    pass
            raise
        return self.get_project(project_id)

    def _profile_for_project(self, directory: Path, crop_x: float, intensity: str) -> Path:
        try:
            profile = json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplicationError("Could not load the Nama Kadhai creative profile.") from exc
        profile["formats"]["vertical"]["crop_x"] = crop_x
        if intensity == "subtle":
            profile["transition"]["duration_seconds"] = 0.16
        elif intensity == "dynamic":
            profile["transition"]["duration_seconds"] = 0.32
        destination = directory / "creative_profile.json"
        with destination.open("x", encoding="utf-8", newline="") as output:
            json.dump(profile, output, ensure_ascii=False, indent=2)
            output.write("\n")
        return destination

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for record in self.projects_root.glob("*/project.json"):
            try:
                payload = json.loads(record.read_text(encoding="utf-8"))
                projects.append(
                    {
                        "project_id": payload["project_id"],
                        "name": payload["name"],
                        "state": payload["state"],
                        "updated_at_utc": payload["updated_at_utc"],
                        "source_file": payload["source"]["metadata"]["file_name"],
                    }
                )
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return sorted(projects, key=lambda item: item["updated_at_utc"], reverse=True)

    def _plan_state(self, plan_path: Path) -> tuple[dict[str, Any], dict[str, str], list[str]]:
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplicationError("Active story plan is unreadable.") from exc
        decision_ids = [str(item["candidate_id"]) for item in plan.get("candidates", [])]
        decision_ids.extend(key for key in ("captions", "title", "music") if plan.get(key) is not None)
        approvals = plan_path.parent / "approvals.jsonl"
        decisions: dict[str, str] = {}
        try:
            lines = approvals.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ApplicationError("Could not read approval history.") from exc
        plan_hash = _sha256(plan_path)
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ApplicationError("Approval history contains invalid JSON.") from exc
            if event.get("plan_sha256") != plan_hash:
                raise ApplicationError("Approval history is stale for the active plan.")
            decisions[str(event.get("item_id"))] = str(event.get("status"))
        unresolved = [item for item in decision_ids if decisions.get(item) not in {"APPROVED", "REJECTED"}]
        return plan, decisions, unresolved

    @staticmethod
    def _write_plan_revision(plan_path: Path, payload: dict[str, Any]) -> None:
        staged = plan_path.with_name(f".{uuid.uuid4().hex}.partial.json")
        try:
            with staged.open("x", encoding="utf-8", newline="") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(staged, plan_path)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            raise ApplicationError("Could not update the automatic story plan safely.") from exc

    def _decision_receipts(self, plan_path: Path) -> dict[str, dict[str, Any]]:
        receipts: dict[str, dict[str, Any]] = {}
        try:
            lines = (plan_path.parent / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ApplicationError("Could not read approval history.") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ApplicationError("Approval history contains invalid JSON.") from exc
            receipts[str(event.get("item_id"))] = {
                "status": event.get("status"),
                "actor": event.get("actor", "creator"),
                "note": event.get("reviewer_notes", ""),
                "recorded_at_utc": event.get("recorded_at_utc"),
            }
        return receipts

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read(project_id)
            plan_path = Path(record["active_plan"]).resolve() if record.get("active_plan") else None
            plan: dict[str, Any] = {}
            decisions: dict[str, str] = {}
            unresolved: list[str] = []
            if plan_path is not None:
                plan, decisions, unresolved = self._plan_state(plan_path)
            outputs = []
            if isinstance(record.get("render_result"), dict):
                outputs = record["render_result"].get("outputs", [])
            response = json.loads(json.dumps(record))
            response["plan"] = {
                "path": str(plan_path) if plan_path else None,
                "candidates": plan.get("candidates", []),
                "captions": plan.get("captions"),
                "title": plan.get("title"),
                "music": plan.get("music"),
                "limitations": plan.get("limitations", []),
            }
            response["decisions"] = decisions
            response["decision_receipts"] = self._decision_receipts(plan_path) if plan_path else {}
            response["unresolved"] = unresolved
            response["outputs"] = [self._output_view(item) for item in outputs]
            response["preview_reviews"] = record.get("preview_reviews", {})
            response["paths"] = {
                "workspace": str(self._directory(project_id)),
                "plan": str(plan_path) if plan_path else None,
            }
            return response

    def run_auto(
        self,
        project_id: str,
        progress: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        """Transcribe, create a fresh plan, label auto decisions, and render previews."""
        callback = progress or (lambda _stage, _percent: None)
        with self._lock:
            record = self._read(project_id)
            if not record.get("preferences", {}).get("transcription_enabled"):
                raise ApplicationError("Enable transcription for this project before running auto mode.")
            if not record.get("preferences", {}).get("auto_mode"):
                raise ApplicationError("Auto mode is not enabled for this project.")
            old_plan_path = Path(record["active_plan"])
            old_plan, _, _ = self._plan_state(old_plan_path)
            record["state"] = "TRANSCRIBING"
            record["last_error"] = None
            self._write(record)
        try:
            provider = self.transcription_provider()
            status = provider.status()
            if not status["ready"]:
                missing = []
                if not status["dependency_installed"]:
                    missing.append("faster-whisper package")
                if not status["model_installed"]:
                    missing.append(f"local {status['model']} model")
                raise ApplicationError(
                    "Auto transcription is enabled but not ready: missing " + " and ".join(missing) + ". No download was attempted."
                )
            callback("Transcribing locally with word timings", 8)
            revision = len(record["plan_revisions"]) + 1
            transcription_dir = self._directory(project_id) / f"transcription_r{revision}"
            transcription = provider.transcribe(Path(record["source"]["path"]), transcription_dir)
            callback("Building Tanglish caption cues", 42)

            profile_path = self._directory(project_id) / f"auto_profile_r{revision}.json"
            base_profile = json.loads((self._directory(project_id) / "creative_profile.json").read_text(encoding="utf-8"))
            # Automatic transcript-rule cuts are hard cuts. Topic-change fades remain
            # unavailable until a tested topic-boundary classifier is present.
            base_profile["transition"]["kind"] = "cut"
            with profile_path.open("x", encoding="utf-8", newline="") as output:
                json.dump(base_profile, output, ensure_ascii=False, indent=2)
                output.write("\n")
            title_data = old_plan.get("title")
            music_data = old_plan.get("music")
            new_plan = create_story_plan(
                record["source"]["path"],
                settings_path=self.settings_path,
                profile_path=profile_path,
                captions_path=transcription["captions_path"],
                title=(title_data.get("text") if isinstance(title_data, dict) else None),
                music_path=(music_data.get("path") if isinstance(music_data, dict) else None),
            )
            callback("Suggesting conservative filler and repeat cuts", 50)
            plan_payload = json.loads(new_plan.read_text(encoding="utf-8"))
            transcript_candidates = suggest_transcript_cuts(
                transcription["transcript_path"],
                duration_seconds=float(record["source"]["metadata"]["duration_seconds"]),
            )
            existing_ranges = [tuple(item["proposed_cut_seconds"]) for item in plan_payload["candidates"]]
            for candidate in transcript_candidates:
                start, end = candidate["proposed_cut_seconds"]
                if any(start < old_end and end > old_start for old_start, old_end in existing_ranges):
                    continue
                plan_payload["candidates"].append(candidate)
                existing_ranges.append((start, end))
            plan_payload["candidates"].sort(key=lambda item: item["proposed_cut_seconds"])
            plan_payload["automation"] = {
                "mode": "auto",
                "actor": "auto",
                "transcription": transcription,
                "final_human_approval_required": True,
                "transition_policy": "hard_cuts_only_until_topic_boundary_detection_is_tested",
            }
            self._write_plan_revision(new_plan, plan_payload)

            intensity = record["preferences"].get("editing_intensity", "balanced")
            for candidate in plan_payload["candidates"]:
                candidate_id = candidate["candidate_id"]
                length = float(candidate["proposed_cut_seconds"][1]) - float(candidate["proposed_cut_seconds"][0])
                transcript_rule = str(candidate_id).startswith("auto-")
                approved = (
                    (transcript_rule and intensity in {"balanced", "dynamic"})
                    or (not transcript_rule and intensity == "balanced" and length >= 1.25)
                    or (not transcript_rule and intensity == "dynamic" and length >= 0.8)
                )
                record_story_decision(
                    new_plan,
                    candidate_id,
                    "APPROVED" if approved else "REJECTED",
                    note="Automatic rule decision; mandatory final human review remains.",
                    actor="auto",
                )
            record_story_decision(
                new_plan,
                "captions",
                "APPROVED",
                note="Local faster-whisper Tanglish draft; uncertain cues omitted and reported.",
                actor="auto",
            )
            if plan_payload.get("title") is not None:
                record_story_decision(new_plan, "title", "APPROVED", note="User-supplied title.", actor="auto")
            if plan_payload.get("music") is not None:
                record_story_decision(
                    new_plan,
                    "music",
                    "REJECTED",
                    note="Auto mode does not approve music licensing evidence.",
                    actor="auto",
                )
            callback("Rendering auto-approved preview revision", 58)
            with self._lock:
                record = self._read(project_id)
                record["active_plan"] = str(new_plan)
                record["plan_revisions"].append(
                    {"revision": revision, "created_at_utc": _now(), "plan_path": str(new_plan), "reason": "auto_transcription"}
                )
                record["transcription"] = transcription
                record["automation"] = plan_payload["automation"]
                record["render_result"] = None
                record["finalization"] = None
                record["package"] = None
                record["state"] = "AUTO_RENDERING"
                self._write(record)
            result = render_story_edit(new_plan, settings_path=self.settings_path)
            callback("Validating auto previews", 94)
            with self._lock:
                record = self._read(project_id)
                record["render_result"] = result
                record["state"] = "PREVIEW_READY"
                self._write(record)
                self._event(
                    project_id,
                    "AUTO_PREVIEWS_RENDERED",
                    {"actor": "auto", "transcription": transcription, "outputs": len(result.get("outputs", []))},
                )
            callback("Auto preview ready for mandatory human review", 100)
            return self.get_project(project_id)
        except Exception as exc:
            with self._lock:
                current = self._read(project_id)
                current["state"] = "ERROR"
                current["last_error"] = str(exc)
                self._write(current)
                self._event(project_id, "AUTO_PIPELINE_FAILED", {"error": str(exc)})
            raise

    @staticmethod
    def _output_view(item: dict[str, Any]) -> dict[str, Any]:
        output = dict(item)
        if item.get("path"):
            output["media_url"] = f"/media?path={Path(str(item['path'])).resolve().as_posix()}"
        if item.get("render_report"):
            output["report_url"] = f"/media?path={Path(str(item['render_report'])).resolve().as_posix()}"
        return output

    def render_context(
        self,
        project_id: str,
        attempt_id: str,
        *,
        expected_plan_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
            raise ApplicationError("Render attempt ID is invalid.")
        with self._lock:
            record = self._read(project_id)
            if not record.get("active_plan"):
                raise ApplicationError("Project has no active plan.")
            plan = Path(record["active_plan"]).resolve()
            if not plan.is_file():
                raise ApplicationError("Active story plan is missing.")
            plan_sha256 = _sha256(plan)
            if expected_plan_sha256 is not None and plan_sha256 != expected_plan_sha256:
                raise ApplicationError("Cannot retry: the active story revision has changed.")
            revisions = record.get("plan_revisions", [])
            revision_id = (
                str(revisions[-1].get("revision", len(revisions)))
                if revisions and isinstance(revisions[-1], dict)
                else "initial"
            )
            output_directory = plan.parent / "render_attempts" / attempt_id
            if output_directory.exists():
                raise ApplicationError("Render attempt output directory already exists.")
            return {
                "attempt_id": attempt_id,
                "plan_path": str(plan),
                "plan_sha256": plan_sha256,
                "revision_id": revision_id,
                "requested_formats": ["landscape", "vertical"],
                "output_directory": str(output_directory),
                "output_paths": [
                    str(output_directory / f"review_{format_name}.mp4")
                    for format_name in ("landscape", "vertical")
                ],
            }

    def register_render_attempt(self, project_id: str, job: dict[str, Any]) -> None:
        with self._lock:
            record = self._read(project_id)
            if record.get("active_plan") != job.get("plan_path"):
                raise ApplicationError("Cannot start a render for a stale story revision.")
            if _sha256(Path(str(job["plan_path"]))) != job.get("plan_sha256"):
                raise ApplicationError("Cannot start a render for a changed story revision.")
            active_job = record.get("active_job")
            if (
                isinstance(active_job, dict)
                and active_job.get("job_id") != job.get("retry_of_job_id")
                and active_job.get("status") in {
                "queued",
                "running",
                "cancel_pending",
                }
            ):
                raise ApplicationError("A render attempt is already active for this project.")
            record["active_job"] = {
                key: job.get(key)
                for key in (
                    "job_id",
                    "attempt_id",
                    "plan_path",
                    "plan_sha256",
                    "revision_id",
                    "requested_formats",
                    "output_paths",
                )
            }
            record["active_job"].update(
                status="queued",
                progress=0,
                stage="Queued",
                started_at_utc=_now(),
                error=None,
            )
            record["state"] = "RENDERING"
            record["last_error"] = None
            self._write(record)
            self._event(
                project_id,
                "RENDER_ATTEMPT_QUEUED",
                {
                    "job_id": job["job_id"],
                    "attempt_id": job["attempt_id"],
                    "revision_id": job["revision_id"],
                },
            )

    def recover_interrupted_jobs(self, jobs: list[dict[str, Any]]) -> None:
        recovered_jobs = {
            str(job.get("job_id")): job
            for job in jobs
            if job.get("status") in {"interrupted", "failed", "cancelled"}
        }
        with self._lock:
            for summary in self.list_projects():
                project_id = summary["project_id"]
                record = self._read(project_id)
                active = record.get("active_job")
                if not isinstance(active, dict):
                    continue
                job_id = str(active.get("job_id", ""))
                recovered = recovered_jobs.get(job_id)
                if recovered is None:
                    if active.get("status") in {"queued", "running", "cancel_pending"}:
                        recovered = {
                            "status": "interrupted",
                            "error": "Server restarted while this render attempt was active.",
                        }
                    else:
                        continue
                elif (
                    active.get("status") not in {"queued", "running", "cancel_pending"}
                    and active.get("status") == recovered.get("status")
                ):
                    continue
                status = str(recovered["status"])
                active.update(
                    status=status,
                    stage=(
                        "Interrupted by server restart"
                        if status == "interrupted"
                        else f"Render attempt {status}"
                    ),
                    error=recovered.get(
                        "error",
                        "Server restarted while this render attempt was active."
                        if status == "interrupted"
                        else f"Render attempt {status}.",
                    ),
                    progress=int(active.get("progress", 0)),
                    completed_at_utc=_now(),
                )
                record["state"] = "ERROR"
                record["last_error"] = active["error"]
                self._write(record)
                self._event(
                    project_id,
                    "RENDER_ATTEMPT_" + status.upper(),
                    {"job_id": job_id, "attempt_id": active.get("attempt_id")},
                )

    def fail_render_attempt(
        self, project_id: str, job_id: str, error: str
    ) -> None:
        with self._lock:
            record = self._read(project_id)
            active = record.get("active_job")
            if not isinstance(active, dict) or active.get("job_id") != job_id:
                return
            if active.get("status") == "failed":
                return
            active.update(
                status="failed",
                stage="Render failed validation",
                error=error,
                completed_at_utc=_now(),
            )
            record["state"] = "ERROR"
            record["last_error"] = error
            self._write(record)
            self._event(
                project_id,
                "RENDER_ATTEMPT_FAILED",
                {"job_id": job_id, "attempt_id": active.get("attempt_id"), "error": error},
            )

    def decide(self, project_id: str, item_id: str, status: str, note: str = "") -> dict[str, Any]:
        with self._lock:
            record = self._read(project_id)
            if not record.get("active_plan"):
                raise ApplicationError("Project has no active plan.")
            record_story_decision(record["active_plan"], item_id, status, note=note)
            record["state"] = "REVIEW"
            record["last_error"] = None
            self._write(record)
            self._event(project_id, "DECISION_RECORDED", {"item_id": item_id, "status": status.upper()})
            return self.get_project(project_id)

    def revise_captions(
        self, project_id: str, captions: list[dict[str, Any]], *, reviewed: bool
    ) -> dict[str, Any]:
        if reviewed is not True:
            raise ApplicationError("Caption revisions must be explicitly marked reviewed.")
        with self._lock:
            record = self._read(project_id)
            source = Path(record["source"]["path"])
            old_plan_path = Path(record["active_plan"])
            old_plan, _, _ = self._plan_state(old_plan_path)
            revision = len(record["plan_revisions"]) + 1
            caption_path = self._directory(project_id) / f"reviewed_captions_r{revision}.json"
            payload = {"version": 1, "reviewed": True, "captions": captions}
            with caption_path.open("x", encoding="utf-8", newline="") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
            try:
                cues = load_caption_cues(caption_path)
                duration = float(record["source"]["metadata"]["duration_seconds"])
                if any(cue.end_seconds > duration for cue in cues):
                    raise ApplicationError("A caption ends after the source video.")
                title_data = old_plan.get("title")
                music_data = old_plan.get("music")
                profile_path = self._directory(project_id) / "creative_profile.json"
                new_plan = create_story_plan(
                    source,
                    settings_path=self.settings_path,
                    profile_path=profile_path,
                    captions_path=caption_path,
                    title=(title_data.get("text") if isinstance(title_data, dict) else None),
                    music_path=(music_data.get("path") if isinstance(music_data, dict) else None),
                )
            except Exception:
                caption_path.unlink(missing_ok=True)
                raise
            record["active_plan"] = str(new_plan)
            record["plan_revisions"].append(
                {"revision": revision, "created_at_utc": _now(), "plan_path": str(new_plan), "reason": "caption_revision"}
            )
            record["render_result"] = None
            record["preview_reviews"] = {}
            record["finalization"] = None
            record["package"] = None
            record["state"] = "REVIEW"
            self._write(record)
            self._event(project_id, "CAPTIONS_REVISED", {"revision": revision, "cue_count": len(cues)})
            return self.get_project(project_id)

    def render(
        self,
        project_id: str,
        progress: Callable[[str, int], None] | None = None,
        *,
        attempt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        callback = progress or (lambda _stage, _percent: None)
        with self._lock:
            record = self._read(project_id)
            plan = record.get("active_plan")
            if not plan:
                raise ApplicationError("Project has no active plan.")
            if attempt is not None and (
                str(Path(plan).resolve()) != attempt.get("plan_path")
                or _sha256(Path(plan)) != attempt.get("plan_sha256")
            ):
                raise ApplicationError("Render attempt refers to a stale story revision.")
            record["state"] = "RENDERING"
            record["last_error"] = None
            if attempt is not None:
                record["active_job"].update(status="running", stage="Starting", progress=0)
            self._write(record)
        callback("Preparing approved edit", 10)
        try:
            callback("Rendering landscape and vertical previews", 25)
            result = render_story_edit(
                plan,
                settings_path=self.settings_path,
                output_directory=(
                    attempt["output_directory"] if attempt is not None else None
                ),
            )
            callback("Validating outputs", 90)
            with self._lock:
                record = self._read(project_id)
                if _sha256(Path(record["source"]["path"])) != record["source"]["sha256"]:
                    raise ApplicationError("Source changed during rendering.")
                if record.get("active_plan") != plan:
                    raise ApplicationError("Story revision changed during rendering.")
                if attempt is not None and _sha256(Path(plan)) != attempt["plan_sha256"]:
                    raise ApplicationError("Story plan changed during rendering.")
                record["render_result"] = result
                record["preview_reviews"] = {}
                record["state"] = "PREVIEW_READY"
                if attempt is not None:
                    record["active_job"].update(
                        status="succeeded",
                        stage="Validated previews ready for review",
                        progress=100,
                        completed_at_utc=_now(),
                        outputs=result.get("outputs", []),
                    )
                self._write(record)
                self._event(project_id, "PREVIEWS_RENDERED", {"count": len(result.get("outputs", []))})
            callback("Preview ready for review", 100)
            return self.get_project(project_id)
        except Exception as exc:
            with self._lock:
                record = self._read(project_id)
                record["state"] = "ERROR"
                record["last_error"] = str(exc)
                if attempt is not None and isinstance(record.get("active_job"), dict):
                    record["active_job"].update(
                        status="failed",
                        stage="Render failed",
                        error=str(exc),
                        completed_at_utc=_now(),
                    )
                self._write(record)
                self._event(project_id, "RENDER_FAILED", {"error": str(exc)})
            raise

    def record_preview_review(self, project_id: str, output_format: str) -> dict[str, Any]:
        if output_format not in {"landscape", "vertical"}:
            raise ApplicationError("Preview format must be landscape or vertical.")
        with self._lock:
            record = self._read(project_id)
            render_result = record.get("render_result")
            outputs = render_result.get("outputs", []) if isinstance(render_result, dict) else []
            output = next(
                (
                    item
                    for item in outputs
                    if isinstance(item, dict) and item.get("format") == output_format
                ),
                None,
            )
            if output is None or not output.get("path"):
                raise ApplicationError("Render this preview format before recording its review.")
            path = Path(str(output["path"])).resolve()
            if not path.is_file():
                raise ApplicationError("The current preview file is missing.")
            try:
                review = record_story_preview_review(
                    record["active_plan"],
                    output_format,
                    path,
                )
                output_hash = _sha256(path)
            except OSError as exc:
                raise ApplicationError("Could not verify the current preview file.") from exc
            except (StoryPlanError, StoryRenderError) as exc:
                raise ApplicationError(str(exc)) from exc
            reviews = record.setdefault("preview_reviews", {})
            reviews[output_format] = {
                "output_sha256": output_hash,
                "reviewed_at_utc": review["reviewed_at_utc"],
            }
            self._write(record)
            self._event(
                project_id,
                "PREVIEW_REVIEWED",
                {"format": output_format, "output_sha256": output_hash},
            )
            return self.get_project(project_id)

    def finalize(self, project_id: str, note: str) -> dict[str, Any]:
        with self._lock:
            record = self._read(project_id)
            _, _, unresolved = self._plan_state(Path(record["active_plan"]))
            if unresolved:
                raise ApplicationError("Resolve every creative decision before final approval.")
            if not record.get("render_result"):
                raise ApplicationError("Render and review both previews before final approval.")
            active_job = record.get("active_job")
            if isinstance(active_job, dict) and active_job.get("status") != "succeeded":
                raise ApplicationError(
                    "Only a successfully validated render attempt can be finalized."
                )
            if not isinstance(note, str) or not note.strip():
                raise ApplicationError("Enter a final approval note before approving the edit.")
            outputs = record["render_result"].get("outputs", [])
            for output_format in ("landscape", "vertical"):
                output = next(
                    (
                        item
                        for item in outputs
                        if isinstance(item, dict) and item.get("format") == output_format
                    ),
                    None,
                )
                review = record.get("preview_reviews", {}).get(output_format)
                if output is None or not output.get("path") or not isinstance(review, dict):
                    raise ApplicationError(
                        "Watch both current previews from beginning to end before final approval."
                    )
                path = Path(str(output["path"])).resolve()
                if not path.is_file():
                    raise ApplicationError("A reviewed preview file is missing.")
                try:
                    output_hash = _sha256(path)
                except OSError as exc:
                    raise ApplicationError("Could not verify a reviewed preview file.") from exc
                if review.get("output_sha256") != output_hash:
                    raise ApplicationError(
                        "A preview changed after review. Watch both current previews before approval."
                    )
            result = approve_story_exports(
                record["active_plan"],
                note=note,
                settings_path=self.settings_path,
                output_directory=(
                    record["render_result"].get("exports_directory")
                    if isinstance(record.get("render_result"), dict)
                    else None
                ),
            )
            record["finalization"] = result
            record["state"] = "FINALIZED"
            record["last_error"] = None
            self._write(record)
            self._event(project_id, "EXPORTS_FINALIZED", {"note": note.strip()})
            return self.get_project(project_id)

    def package(self, project_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = self._read(project_id)
            if not record.get("finalization"):
                raise ApplicationError("Finalize approved previews before building the package.")
            result = create_publishing_package(
                record["active_plan"],
                project_name=record["name"],
                title=str(metadata.get("title", "")),
                description=str(metadata.get("description", "")),
                hashtags=str(metadata.get("hashtags", "")),
                social_caption=str(metadata.get("social_caption", "")),
                pinned_comment=str(metadata.get("pinned_comment", "")),
                settings_path=self.settings_path,
                exports_directory=str(
                    Path(record["finalization"]["approval_report"]).parent
                ),
            )
            result["file_urls"] = [
                f"/media?path={Path(item).resolve().as_posix()}" for item in result["files"]
            ]
            record["package"] = result
            record["state"] = "PACKAGED"
            self._write(record)
            self._event(project_id, "PACKAGE_CREATED", {"directory": result["directory"]})
            return self.get_project(project_id)
