"""Dependency-free local web application for the Nama Kadhai editor."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from namakadhai.application import (
    ApplicationError,
    ProjectApplication,
    ProjectNotFoundError,
)
from namakadhai.caption_renderer import CaptionError
from namakadhai.postproduction import PostProductionError
from namakadhai.probe import ProbeError
from namakadhai.publishing import PublishingError
from namakadhai.story_editor import StoryEditorError

ASSET_DIRECTORY = Path(__file__).with_name("ui_assets")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
PROJECT_ROUTE = re.compile(r"^/api/projects/([a-z0-9][a-z0-9-]{5,100})(?:/(.*))?$")
JOB_ROUTE = re.compile(r"^/api/jobs/([a-f0-9]{32})(?:/(cancel|retry))?$")


class JobManager:
    """Run long renders in-process while exposing honest, pollable state."""

    def __init__(self, storage_directory: Path | None = None) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.storage_directory = (
            storage_directory.resolve() if storage_directory is not None else None
        )
        if self.storage_directory is not None:
            self.storage_directory.mkdir(parents=True, exist_ok=True)
            self._load_persisted()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _persist(self, job: dict[str, Any]) -> None:
        if self.storage_directory is None:
            return
        destination = self.storage_directory / f"{job['job_id']}.json"
        staged = self.storage_directory / f".{job['job_id']}.{uuid.uuid4().hex}.partial"
        try:
            with staged.open("x", encoding="utf-8", newline="") as output:
                json.dump(job, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(staged, destination)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            raise ApplicationError("Could not persist render job state safely.") from exc

    def _load_persisted(self) -> None:
        assert self.storage_directory is not None
        for path in sorted(self.storage_directory.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ApplicationError(f"Render job record is unreadable: {path.name}") from exc
            if (
                not isinstance(job, dict)
                or not re.fullmatch(r"[a-f0-9]{32}", str(job.get("job_id", "")))
                or path.stem != job.get("job_id")
            ):
                raise ApplicationError(f"Render job record has an invalid schema: {path.name}")
            if job.get("status") in {"queued", "running", "cancel_pending"}:
                job.update(
                    status="interrupted",
                    stage="Interrupted by server restart",
                    error="The local server restarted before this render attempt completed.",
                    message=None,
                    completed_at_utc=self._now(),
                )
                self._persist(job)
            elif job.get("status") == "succeeded" and not self._validate_completed_job(job):
                job.update(
                    status="failed",
                    stage="Previously successful render failed integrity checks",
                    error="Integrity checks failed: a completed render, report, or plan changed after job validation.",
                    completed_at_utc=self._now(),
                )
                self._persist(job)
            self._jobs[job["job_id"]] = job

    def _validate_completed_job(self, job: dict[str, Any]) -> bool:
        plan_path = Path(str(job.get("plan_path", "")))
        try:
            if not plan_path.is_file() or self._sha256(plan_path) != job.get("plan_sha256"):
                return False
            outputs = job.get("outputs")
            if not isinstance(outputs, list):
                return False
            records = {item.get("format"): item for item in outputs if isinstance(item, dict)}
            for output_format in job.get("requested_formats", []):
                item = records.get(output_format)
                if not isinstance(item, dict):
                    return False
                video = Path(str(item.get("path", "")))
                report_path = Path(str(item.get("render_report", "")))
                if (
                    not video.is_file()
                    or not report_path.is_file()
                    or self._sha256(video) != item.get("sha256")
                    or self._sha256(report_path) != item.get("render_report_sha256")
                ):
                    return False
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if (
                    report.get("validated") is not True
                    or report.get("format") != output_format
                    or report.get("plan_sha256") != job.get("plan_sha256")
                    or report.get("output_sha256") != item.get("sha256")
                ):
                    return False
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        return True

    def start(
        self,
        project_id: str,
        action: str,
        function: Callable[[Callable[[str, int], None], dict[str, Any]], Any],
        *,
        metadata: dict[str, Any],
        prepare: Callable[[dict[str, Any]], None] | None = None,
        on_failure: Callable[[dict[str, Any], Exception], None] | None = None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        if Path(str(metadata["output_directory"])).exists():
            raise ApplicationError(
                "Render attempt output directory already exists; refusing to overwrite it."
            )
        job = {
            "job_id": job_id,
            "attempt_id": metadata["attempt_id"],
            "project_id": project_id,
            "action": action,
            "status": "queued",
            "stage": "Queued",
            "progress": 0,
            "cancel_requested": False,
            "message": None,
            "error": None,
            "plan_path": metadata["plan_path"],
            "plan_sha256": metadata["plan_sha256"],
            "revision_id": metadata["revision_id"],
            "requested_formats": metadata["requested_formats"],
            "output_directory": metadata["output_directory"],
            "output_paths": metadata["output_paths"],
            "retry_of_job_id": metadata.get("retry_of_job_id"),
            "created_at_utc": self._now(),
            "updated_at_utc": self._now(),
            "completed_at_utc": None,
        }
        with self._lock:
            if any(
                existing["project_id"] == project_id
                and existing["status"] in {"queued", "running", "cancel_pending"}
                for existing in self._jobs.values()
            ):
                raise ApplicationError("A render attempt is already active for this project.")
            self._jobs[job_id] = job
            try:
                self._persist(job)
                if prepare is not None:
                    prepare(dict(job))
                    self._persist(job)
            except Exception:
                job.update(
                    status="failed",
                    stage="Could not prepare render attempt",
                    error="The render attempt could not be recorded with the project.",
                    completed_at_utc=self._now(),
                )
                self._persist(job)
                raise

        def update(stage: str, percent: int) -> None:
            with self._lock:
                current = self._jobs[job_id]
                current["stage"] = stage
                current["progress"] = max(0, min(100, int(percent)))
                current["updated_at_utc"] = self._now()
                self._persist(current)

        def run() -> None:
            with self._lock:
                current = self._jobs[job_id]
                if current["cancel_requested"]:
                    current.update(
                        status="cancelled",
                        stage="Cancelled before start",
                        completed_at_utc=self._now(),
                    )
                    self._persist(current)
                    return
                current.update(status="running", stage="Starting", updated_at_utc=self._now())
                self._persist(current)
            try:
                result = function(update, dict(self._jobs[job_id]))
                outputs = result.get("outputs", []) if isinstance(result, dict) else []
                output_records = []
                for output_format in job["requested_formats"]:
                    output = next(
                        (
                            item
                            for item in outputs
                            if isinstance(item, dict) and item.get("format") == output_format
                        ),
                        None,
                    )
                    if output is None:
                        raise ApplicationError(
                            f"Render did not return the requested {output_format} output."
                        )
                    output_path = Path(str(output["path"])).resolve()
                    report_path = Path(str(output["render_report"])).resolve()
                    if not output_path.is_file() or not report_path.is_file():
                        raise ApplicationError(
                            f"Validated {output_format} output or report is missing."
                        )
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    output_hash = self._sha256(output_path)
                    report_hash = self._sha256(report_path)
                    if (
                        report.get("validated") is not True
                        or report.get("format") != output_format
                        or report.get("plan_sha256") != job["plan_sha256"]
                        or report.get("output_sha256") != output_hash
                    ):
                        raise ApplicationError(
                            f"Validated {output_format} output does not match its render report."
                        )
                    output_records.append(
                        {
                            "format": output_format,
                            "path": str(output_path),
                            "sha256": output_hash,
                            "render_report": str(report_path),
                            "render_report_sha256": report_hash,
                            "validated": True,
                        }
                    )
            except Exception as exc:  # surfaced to the local user through the job endpoint
                error_message = str(exc)
                if on_failure is not None:
                    try:
                        on_failure(dict(self._jobs[job_id]), exc)
                    except Exception as state_error:
                        error_message = (
                            f"{error_message}; could not persist project failure state: "
                            f"{state_error}"
                        )
                with self._lock:
                    self._jobs[job_id].update(
                        status="failed",
                        stage="Failed",
                        error=error_message,
                        progress=100,
                        completed_at_utc=self._now(),
                        updated_at_utc=self._now(),
                    )
                    self._persist(self._jobs[job_id])
            else:
                with self._lock:
                    current = self._jobs[job_id]
                    message = None
                    if current["cancel_requested"]:
                        message = (
                            "Cancellation was requested after FFmpeg had started; the validated "
                            "render completed safely and was retained."
                        )
                    current.update(
                        status="succeeded",
                        stage="Succeeded and validated",
                        progress=100,
                        message=message,
                        outputs=output_records,
                        completed_at_utc=self._now(),
                        updated_at_utc=self._now(),
                    )
                    self._persist(current)

        threading.Thread(target=run, name=f"namakadhai-{action}-{job_id[:8]}", daemon=True).start()
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise ApplicationError("Job was not found.")
            return dict(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise ApplicationError("Job was not found.")
            job = self._jobs[job_id]
            if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return dict(job)
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job.update(status="cancelled", stage="Cancelled before start")
            else:
                job.update(
                    status="cancel_pending",
                    stage="Cancellation requested; finishing the current safe FFmpeg stage",
                )
            job["updated_at_utc"] = self._now()
            self._persist(job)
            return dict(job)

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (
                    dict(job)
                    for job in self._jobs.values()
                    if job.get("project_id") == project_id
                ),
                key=lambda job: job.get("created_at_utc", ""),
            )

    def latest_for_project(self, project_id: str) -> dict[str, Any] | None:
        jobs = self.list_for_project(project_id)
        return jobs[-1] if jobs else None

    def all_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(job) for job in self._jobs.values()]

    def retry(
        self,
        job_id: str,
        function: Callable[[Callable[[str, int], None], dict[str, Any]], Any],
        *,
        metadata: dict[str, Any],
        prepare: Callable[[dict[str, Any]], None] | None = None,
        on_failure: Callable[[dict[str, Any], Exception], None] | None = None,
    ) -> dict[str, Any]:
        previous = self.get(job_id)
        if previous.get("status") not in {"interrupted", "failed", "cancelled"}:
            raise ApplicationError(
                "Only an interrupted, failed, or cancelled render can be retried."
            )
        if previous.get("action") != "render":
            raise ApplicationError("Only render jobs can be retried.")
        return self.start(
            str(previous["project_id"]),
            "render",
            function,
            metadata=metadata,
            prepare=prepare,
            on_failure=on_failure,
        )



class EditorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        application: ProjectApplication,
        jobs: JobManager,
    ) -> None:
        super().__init__(address, handler)
        self.application = application
        self.jobs = jobs


class EditorHandler(BaseHTTPRequestHandler):
    server: EditorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {format % args}")

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(encoded))
        self.wfile.write(encoded)

    def _error(self, exc: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": str(exc), "type": type(exc).__name__}, status)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApplicationError("Invalid request size.") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ApplicationError("JSON request is empty or too large.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationError("Request body must contain valid UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise ApplicationError("JSON request must be an object.")
        return payload

    def _serve_index(self) -> None:
        path = ASSET_DIRECTORY / "index.html"
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._error(ApplicationError("The application UI is unavailable."), HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
        self.wfile.write(data)

    def _start_render_job(
        self,
        project_id: str,
        *,
        retry_of: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt_id = uuid.uuid4().hex
        metadata = self.server.application.render_context(
            project_id,
            attempt_id,
            expected_plan_sha256=(
                str(retry_of["plan_sha256"]) if retry_of is not None else None
            ),
        )
        if retry_of is not None:
            metadata["retry_of_job_id"] = retry_of["job_id"]

        def prepare(job: dict[str, Any]) -> None:
            self.server.application.register_render_attempt(project_id, job)

        return self.server.jobs.start(
            project_id,
            "render",
            lambda update, job: self.server.application.render(
                project_id, update, attempt=job
            ),
            metadata=metadata,
            prepare=prepare,
            on_failure=lambda job, error: self.server.application.fail_render_attempt(
                project_id, job["job_id"], str(error)
            ),
        )

    def _allowed_media(self, path: Path) -> bool:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.server.application.projects_root.parent)
            return resolved.is_file()
        except ValueError:
            pass
        for summary in self.server.application.list_projects():
            try:
                project = self.server.application.get_project(summary["project_id"])
                if resolved == Path(project["source"]["path"]).resolve():
                    return resolved.is_file()
            except ApplicationError:
                continue
        return False

    def _serve_media(self, query: str) -> None:
        values = parse_qs(query).get("path", [])
        if len(values) != 1:
            self._error(ApplicationError("A single media path is required."))
            return
        path = Path(unquote(values[0]))
        if not self._allowed_media(path):
            self._error(ApplicationError("Media path is outside this application."), HTTPStatus.FORBIDDEN)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            elif match.group(2):
                requested = int(match.group(2))
                start = max(0, size - requested)
            if start > end or start >= size:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            if route.path == "/":
                self._serve_index()
                return
            if route.path == "/api/health":
                status_provider = getattr(self.server.application, "transcription_status", None)
                transcription = status_provider() if callable(status_provider) else {
                    "enabled": False,
                    "ready": False,
                    "downloads_allowed": False,
                }
                self._json(
                    {
                        "ok": True,
                        "offline_core": True,
                        "transcription_enabled": bool(transcription.get("enabled")),
                        "transcription": transcription,
                        "caption_alignment_ready": bool(
                            getattr(self.server.application, "caption_alignment_ready", lambda: False)()
                        ),
                    }
                )
                return
            if route.path == "/api/projects":
                self._json({"projects": self.server.application.list_projects()})
                return
            if route.path == "/api/jobs":
                project_ids = parse_qs(route.query).get("project_id", [])
                if len(project_ids) != 1:
                    raise ApplicationError("A single project_id is required.")
                self._json(
                    {"jobs": self.server.jobs.list_for_project(project_ids[0])}
                )
                return
            project_match = PROJECT_ROUTE.fullmatch(route.path)
            if project_match and not project_match.group(2):
                project_id = project_match.group(1)
                project = self.server.application.get_project(project_id)
                latest_job = self.server.jobs.latest_for_project(project_id)
                if latest_job is not None:
                    project["active_job"] = latest_job
                self._json({"project": project})
                return
            job_match = JOB_ROUTE.fullmatch(route.path)
            if job_match and not job_match.group(2):
                self._json({"job": self.server.jobs.get(job_match.group(1))})
                return
            if route.path == "/media":
                self._serve_media(route.query)
                return
            self._error(ApplicationError("Route was not found."), HTTPStatus.NOT_FOUND)
        except ProjectNotFoundError as exc:
            self._error(exc, HTTPStatus.NOT_FOUND)
        except ApplicationError as exc:
            self._error(exc)
        except Exception as exc:
            self._error(exc, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            if route.path == "/api/projects":
                body = self._read_json()
                project = self.server.application.create_project(
                    body.get("source_path", ""),
                    project_name=str(body.get("project_name", "")),
                    title=str(body.get("title", "")),
                    captions_path=(body.get("captions_path") or None),
                    music_path=(body.get("music_path") or None),
                    original_path=(body.get("original_path") or None),
                    crop_x=body.get("crop_x", 0.5),
                    editing_intensity=str(body.get("editing_intensity", "balanced")),
                    captions_enabled=body.get("captions_enabled", True) is True,
                    auto_mode=body.get("auto_mode", True) is True,
                    transcription_enabled=body.get("transcription_enabled", True) is True,
                )
                self._json({"project": project}, HTTPStatus.CREATED)
                return
            project_match = PROJECT_ROUTE.fullmatch(route.path)
            if project_match:
                project_id, action = project_match.groups()
                body = self._read_json() if self.headers.get("Content-Length", "0") != "0" else {}
                if action == "decisions":
                    project = self.server.application.decide(
                        project_id,
                        str(body.get("item_id", "")),
                        str(body.get("status", "")),
                        str(body.get("note", "")),
                    )
                    self._json({"project": project})
                    return
                if action == "captions/revise":
                    captions = body.get("captions")
                    if not isinstance(captions, list):
                        raise ApplicationError("Captions must be a JSON list.")
                    project = self.server.application.revise_captions(
                        project_id, captions, reviewed=body.get("reviewed") is True
                    )
                    self._json({"project": project})
                    return
                if action == "previews/review":
                    project = self.server.application.record_preview_review(
                        project_id, str(body.get("format", ""))
                    )
                    self._json({"project": project})
                    return
                if action == "render":
                    job = self._start_render_job(project_id)
                    self._json({"job": job}, HTTPStatus.ACCEPTED)
                    return
                if action == "auto":
                    job = self.server.jobs.start(
                        project_id,
                        "auto",
                        lambda update: self.server.application.run_auto(project_id, update),
                    )
                    self._json({"job": job}, HTTPStatus.ACCEPTED)
                    return
                if action == "finalize":
                    project = self.server.application.finalize(project_id, body.get("note", ""))
                    self._json({"project": project})
                    return
                if action == "package":
                    project = self.server.application.package(project_id, body)
                    self._json({"project": project})
                    return
            job_match = JOB_ROUTE.fullmatch(route.path)
            if job_match and job_match.group(2) == "cancel":
                self._json({"job": self.server.jobs.cancel(job_match.group(1))})
                return
            if job_match and job_match.group(2) == "retry":
                previous = self.server.jobs.get(job_match.group(1))
                if previous.get("status") not in {"interrupted", "failed", "cancelled"}:
                    raise ApplicationError(
                        "Only an interrupted, failed, or cancelled render can be retried."
                    )
                if previous.get("action") != "render":
                    raise ApplicationError("Only render jobs can be retried.")
                job = self._start_render_job(
                    str(previous["project_id"]), retry_of=previous
                )
                self._json({"job": job}, HTTPStatus.ACCEPTED)
                return
            self._error(ApplicationError("Route was not found."), HTTPStatus.NOT_FOUND)
        except ProjectNotFoundError as exc:
            self._error(exc, HTTPStatus.NOT_FOUND)
        except (ApplicationError, CaptionError, PostProductionError, ProbeError, PublishingError, StoryEditorError, ValueError) as exc:
            message = str(exc)
            status = (
                HTTPStatus.CONFLICT
                if any(
                    phrase in message
                    for phrase in (
                        "Resolve every",
                        "Watch both current previews",
                        "A preview changed after review",
                        "successfully validated render attempt",
                        "active story revision has changed",
                        "stale story revision",
                        "approval note",
                        "Refusing to overwrite",
                    )
                )
                else HTTPStatus.BAD_REQUEST
            )
            self._error(exc, status)
        except Exception as exc:
            self._error(exc, HTTPStatus.INTERNAL_SERVER_ERROR)


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ApplicationError("The editor binds only to the local computer.")
    application = ProjectApplication()
    jobs = JobManager(application.projects_root / "render_jobs")
    application.recover_interrupted_jobs(jobs.all_jobs())
    server = EditorHTTPServer((host, port), EditorHandler, application, jobs)
    url = f"http://{host}:{server.server_address[1]}"
    print(f"Nama Kadhai local editor: {url}")
    print("Core editing is offline. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        serve(args.host, args.port, open_browser=not args.no_browser)
        return 0
    except (ApplicationError, OSError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
