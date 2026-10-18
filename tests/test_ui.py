from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from namakadhai.application import ApplicationError, ProjectNotFoundError
from namakadhai.ui import EditorHTTPServer, EditorHandler, JobManager


PROJECT_ID = "ui-project-123456"


class FakeApplication:
    def __init__(self, root: Path) -> None:
        self.projects_root = root / "projects" / "app"
        self.projects_root.mkdir(parents=True)
        self.project = {"project_id": PROJECT_ID, "state": "PREVIEW_READY"}
        self.render_error: Exception | None = None
        self.package_metadata: dict[str, object] | None = None
        self.plan_path = root / "plan.json"
        self.plan_path.write_text('{"revision":1}', encoding="utf-8")

    def list_projects(self) -> list[dict[str, str]]:
        return [{"project_id": PROJECT_ID}]

    def get_project(self, project_id: str) -> dict[str, object]:
        if project_id != PROJECT_ID:
            raise ProjectNotFoundError("Project was not found.")
        return dict(self.project)

    def render_context(
        self,
        project_id: str,
        attempt_id: str,
        *,
        expected_plan_sha256: str | None = None,
    ) -> dict[str, object]:
        plan_hash = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
        if expected_plan_sha256 is not None and expected_plan_sha256 != plan_hash:
            raise ApplicationError("Cannot retry: the active story revision has changed.")
        output_directory = self.plan_path.parent / "render_attempts" / attempt_id
        return {
            "attempt_id": attempt_id,
            "plan_path": str(self.plan_path),
            "plan_sha256": plan_hash,
            "revision_id": "1",
            "requested_formats": ["landscape", "vertical"],
            "output_directory": str(output_directory),
            "output_paths": [
                str(output_directory / "review_landscape.mp4"),
                str(output_directory / "review_vertical.mp4"),
            ],
        }

    def register_render_attempt(self, project_id: str, job: dict[str, Any]) -> None:
        self.project["active_job"] = dict(job, status="queued", progress=0)

    def fail_render_attempt(self, project_id: str, job_id: str, error: str) -> None:
        self.project["active_job"] = {
            **self.project.get("active_job", {}),
            "job_id": job_id,
            "status": "failed",
            "error": error,
        }

    def finalize(self, project_id: str, note: str) -> dict[str, object]:
        if not note.strip():
            raise ApplicationError("Enter a final approval note before approving the edit.")
        return self.get_project(project_id)

    def package(
        self, project_id: str, metadata: dict[str, Any]
    ) -> dict[str, object]:
        self.package_metadata = metadata
        return self.get_project(project_id)

    def render(self, project_id: str, progress, *, attempt=None) -> dict[str, object]:
        progress("Rendering previews", 50)
        if self.render_error is not None:
            raise self.render_error
        progress("Preview ready", 100)
        output_directory = Path(attempt["output_directory"])
        output_directory.mkdir(parents=True)
        outputs = []
        for format_name in attempt["requested_formats"]:
            output_path = output_directory / f"review_{format_name}.mp4"
            output_path.write_bytes(f"{format_name} validated output".encode())
            output_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
            report_path = output_directory / f"review_{format_name}.render-report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "validated": True,
                        "format": format_name,
                        "plan_sha256": attempt["plan_sha256"],
                        "output_sha256": output_hash,
                    }
                ),
                encoding="utf-8",
            )
            outputs.append(
                {
                    "format": format_name,
                    "path": str(output_path),
                    "render_report": str(report_path),
                }
            )
        self.project["outputs"] = outputs
        return {"outputs": outputs}


class EditorHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.application = FakeApplication(Path(self.temp.name))
        self.server = EditorHTTPServer(
            ("127.0.0.1", 0),
            EditorHandler,
            self.application,
            JobManager(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _request(
        self, path: str, *, method: str = "GET", body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_and_project_reopen_routes_return_local_project_state(self) -> None:
        status, health = self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["offline_core"])
        self.assertFalse(health["transcription_enabled"])

        status, result = self._request(f"/api/projects/{PROJECT_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(result["project"]["project_id"], PROJECT_ID)

    def test_finalization_gate_is_reported_as_conflict(self) -> None:
        status, result = self._request(
            f"/api/projects/{PROJECT_ID}/finalize",
            method="POST",
            body={"note": ""},
        )

        self.assertEqual(status, 409)
        self.assertIn("approval note", result["error"])

    def test_package_route_forwards_metadata_and_returns_project(self) -> None:
        metadata = {
            "title": "Reviewed title",
            "description": "Description",
            "hashtags": "#NamaKadhai",
        }

        status, result = self._request(
            f"/api/projects/{PROJECT_ID}/package",
            method="POST",
            body=metadata,
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.application.package_metadata, metadata)
        self.assertEqual(result["project"]["project_id"], PROJECT_ID)

    def test_render_job_reports_completion_and_progress(self) -> None:
        status, result = self._request(
            f"/api/projects/{PROJECT_ID}/render",
            method="POST",
            body={},
        )
        self.assertEqual(status, 202)
        job_id = result["job"]["job_id"]

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, result = self._request(f"/api/jobs/{job_id}")
            if result["job"]["status"] == "completed":
                break
            time.sleep(0.01)

        self.assertEqual(status, 200)
        self.assertEqual(result["job"]["status"], "succeeded")
        self.assertEqual(result["job"]["progress"], 100)

    def test_render_failure_is_visible_in_job_status(self) -> None:
        self.application.render_error = RuntimeError("synthetic render failure")
        status, result = self._request(
            f"/api/projects/{PROJECT_ID}/render",
            method="POST",
            body={},
        )
        self.assertEqual(status, 202)
        job_id = result["job"]["job_id"]

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, result = self._request(f"/api/jobs/{job_id}")
            if result["job"]["status"] == "failed":
                break
            time.sleep(0.01)

        self.assertEqual(status, 200)
        self.assertEqual(result["job"]["status"], "failed")
        self.assertEqual(result["job"]["error"], "synthetic render failure")

    def test_interrupted_render_can_retry_through_http_route(self) -> None:
        self.application.render_error = RuntimeError("synthetic interrupted attempt")
        status, result = self._request(
            f"/api/projects/{PROJECT_ID}/render",
            method="POST",
            body={},
        )
        self.assertEqual(status, 202)
        first = result["job"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, first = self._request(f"/api/jobs/{first['job_id']}")
            first = first["job"]
            if first["status"] == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(first["status"], "failed")

        self.application.render_error = None
        status, result = self._request(
            f"/api/jobs/{first['job_id']}/retry",
            method="POST",
            body={},
        )
        self.assertEqual(status, 202)
        retry = result["job"]
        self.assertNotEqual(retry["attempt_id"], first["attempt_id"])
        self.assertEqual(retry["plan_sha256"], first["plan_sha256"])
        self.assertNotEqual(retry["output_directory"], first["output_directory"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, result = self._request(f"/api/jobs/{retry['job_id']}")
            retry = result["job"]
            if retry["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(retry["status"], "succeeded")

    def test_persisted_running_job_becomes_interrupted_and_can_retry(self) -> None:
        storage = Path(self.temp.name) / "durable-jobs"
        storage.mkdir()
        job_id = "a" * 32
        (storage / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "project_id": PROJECT_ID,
                    "action": "render",
                    "status": "running",
                    "attempt_id": "b" * 32,
                    "plan_path": str(self.application.plan_path),
                    "plan_sha256": hashlib.sha256(
                        self.application.plan_path.read_bytes()
                    ).hexdigest(),
                    "revision_id": "1",
                    "requested_formats": ["landscape", "vertical"],
                    "output_directory": str(Path(self.temp.name) / "old-attempt"),
                    "output_paths": [],
                    "created_at_utc": "2026-10-16T00:00:00+00:00",
                    "updated_at_utc": "2026-10-16T00:00:00+00:00",
                    "progress": 50,
                }
            ),
            encoding="utf-8",
        )
        manager = JobManager(storage)
        interrupted = manager.get(job_id)
        self.assertEqual(interrupted["status"], "interrupted")

        metadata = self.application.render_context(PROJECT_ID, "c" * 32)
        retried = manager.retry(
            job_id,
            lambda update, job: self.application.render(
                PROJECT_ID, update, attempt=job
            ),
            metadata=metadata,
            prepare=lambda job: self.application.register_render_attempt(
                PROJECT_ID, job
            ),
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            retried = manager.get(retried["job_id"])
            if retried["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)

        self.assertEqual(retried["status"], "succeeded")
        self.assertNotEqual(retried["attempt_id"], interrupted["attempt_id"])
        self.assertEqual(retried["revision_id"], interrupted["revision_id"])
        self.assertEqual(len(retried["outputs"]), 2)
        self.assertTrue(all(output["validated"] for output in retried["outputs"]))
        recovered = JobManager(storage).get(retried["job_id"])
        self.assertEqual(recovered["status"], "succeeded")

        Path(recovered["outputs"][0]["path"]).write_bytes(b"tampered output")
        stale = JobManager(storage).get(retried["job_id"])
        self.assertEqual(stale["status"], "failed")
        self.assertIn("Integrity checks", stale["error"])

    def test_render_start_rejects_existing_attempt_output_directory(self) -> None:
        manager = JobManager()
        metadata = self.application.render_context(PROJECT_ID, "d" * 32)
        Path(str(metadata["output_directory"])).mkdir(parents=True)

        with self.assertRaisesRegex(ApplicationError, "refusing to overwrite"):
            manager.start(
                PROJECT_ID,
                "render",
                lambda _update, _job: {},
                metadata=metadata,
            )


if __name__ == "__main__":
    unittest.main()
