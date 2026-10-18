import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from namakadhai.application import ApplicationError, ProjectApplication
from namakadhai.probe import AudioStreamMetadata, VideoMetadata, VideoStreamMetadata


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.settings.write_text(
            json.dumps(
                {
                    "paths": {
                        "projects": str(self.root / "projects"),
                        "ffmpeg_executable": "ffmpeg.exe",
                        "ffprobe_executable": "ffprobe.exe",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"immutable source")
        self.metadata = VideoMetadata(
            file_name="source.mp4",
            file_size_bytes=self.source.stat().st_size,
            duration_seconds=10.0,
            format_name="mp4",
            format_long_name="MP4",
            video=VideoStreamMetadata("h264", 1080, 1920, 30.0),
            audio=AudioStreamMetadata("aac", 48000, 2),
        )

    def tearDown(self):
        self.temp.cleanup()

    def _fake_plan(self, *args, **kwargs):
        plan_dir = self.root / "story-plan"
        plan_dir.mkdir(exist_ok=True)
        plan = plan_dir / "story_edit.json"
        plan.write_text(
            json.dumps(
                {
                    "source": {"path": str(self.source), "duration_seconds": 10},
                    "candidates": [
                        {
                            "candidate_id": "silence-1",
                            "source_start_seconds": 1,
                            "source_end_seconds": 2,
                            "proposed_cut_seconds": [1.2, 1.8],
                        }
                    ],
                    "captions": None,
                    "title": None,
                    "music": None,
                    "limitations": [],
                }
            ),
            encoding="utf-8",
        )
        (plan_dir / "approvals.jsonl").write_text("", encoding="utf-8")
        return plan

    def _project_with_outputs(self, project_id="review-story-123456"):
        app = ProjectApplication(self.settings)
        directory = app.projects_root / project_id
        directory.mkdir(parents=True)
        plan = self._fake_plan()
        outputs_dir = plan.parent / "exports"
        outputs_dir.mkdir(exist_ok=True)
        outputs = []
        for output_format in ("landscape", "vertical"):
            output = outputs_dir / f"review_{output_format}.mp4"
            output.write_bytes(f"{output_format} preview".encode("utf-8"))
            outputs.append({"format": output_format, "path": str(output)})
        record = {
            "version": 1,
            "project_id": project_id,
            "name": "My Story",
            "created_at_utc": "now",
            "updated_at_utc": "now",
            "state": "PREVIEW_READY",
            "source": {
                "path": str(self.source),
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "metadata": self.metadata.to_dict(),
            },
            "preferences": {},
            "plan_revisions": [],
            "active_plan": str(plan),
            "render_result": {"outputs": outputs},
            "preview_reviews": {},
            "finalization": None,
            "package": None,
            "last_error": None,
        }
        (directory / "project.json").write_text(json.dumps(record), encoding="utf-8")
        (directory / "events.jsonl").write_text("", encoding="utf-8")
        return app, record, outputs

    @patch("namakadhai.application.analyze_video_signals")
    @patch("namakadhai.application.create_story_plan")
    @patch("namakadhai.application.probe_video")
    def test_project_creation_is_versioned_and_source_hash_bound(self, probe, create_plan, analyze):
        probe.return_value = self.metadata
        create_plan.side_effect = self._fake_plan
        analyze.return_value = {"report_path": "visual.json", "warnings": []}
        app = ProjectApplication(self.settings)

        project = app.create_project(self.source, project_name="My Story", crop_x=0.4)

        self.assertEqual(project["state"], "REVIEW")
        self.assertEqual(project["source"]["sha256"], app._read(project["project_id"])["source"]["sha256"])
        self.assertEqual(project["unresolved"], ["silence-1"])
        self.assertEqual(self.source.read_bytes(), b"immutable source")
        self.assertTrue(Path(project["paths"]["workspace"]).is_dir())
        self.assertEqual(len(app.list_projects()), 1)

    @patch("namakadhai.application.subprocess.run")
    @patch("namakadhai.application.analyze_video_signals")
    @patch("namakadhai.application.create_story_plan")
    @patch("namakadhai.application.probe_video")
    def test_srt_captions_are_word_aligned_before_planning(
        self, probe, create_plan, analyze, run
    ):
        probe.return_value = self.metadata
        create_plan.side_effect = self._fake_plan
        analyze.return_value = {"report_path": "visual.json", "warnings": []}
        srt = self.root / "captions.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nNaama chinna\n", encoding="utf-8")

        def fake_align(command, **kwargs):
            self.assertEqual(command[1:3], ["-m", "namakadhai.forced_align"])
            Path(command[-1]).write_text(
                json.dumps({"version": 1, "source": {"alignment": {"mean_word_confidence": 0.7}},
                            "captions": []}),
                encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        run.side_effect = fake_align
        with patch.object(ProjectApplication, "caption_alignment_ready", return_value=True):
            project = ProjectApplication(self.settings).create_project(
                self.source, captions_path=str(srt)
            )

        self.assertNotIn("--edited", run.call_args.args[0])
        passed = Path(create_plan.call_args.kwargs["captions_path"])
        self.assertEqual(passed.name, "captions_aligned.json")
        self.assertEqual(passed.parent.name, project["project_id"])
        record = json.loads((passed.parent / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(record["caption_alignment"]["mean_word_confidence"], 0.7)
        self.assertEqual(record["caption_alignment"]["srt_path"], str(srt.resolve()))

    @patch("namakadhai.application.subprocess.run")
    @patch("namakadhai.application.analyze_video_signals")
    @patch("namakadhai.application.create_story_plan")
    @patch("namakadhai.application.probe_video")
    def test_edited_video_captions_are_timed_on_the_original(
        self, probe, create_plan, analyze, run
    ):
        probe.return_value = self.metadata
        create_plan.side_effect = self._fake_plan
        analyze.return_value = {"report_path": "visual.json", "warnings": []}
        srt = self.root / "captions.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nNaama chinna\n", encoding="utf-8")
        original = self.root / "raw.mp4"
        original.write_bytes(b"raw recording")

        def fake_align(command, **kwargs):
            Path(command[-3] if command[-2] == "--edited" else command[-1]).write_text(
                json.dumps({"version": 1, "source": {"alignment": {}}, "captions": []}),
                encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        run.side_effect = fake_align
        with patch.object(ProjectApplication, "caption_alignment_ready", return_value=True):
            ProjectApplication(self.settings).create_project(
                self.source, captions_path=str(srt), original_path=str(original)
            )

        command = run.call_args.args[0]
        self.assertEqual(command[3:5], [str(srt.resolve()), str(original.resolve())])
        self.assertEqual(command[-2:], ["--edited", str(self.source.resolve())])
        self.assertEqual(original.read_bytes(), b"raw recording")

    @patch("namakadhai.application.record_story_decision")
    @patch("namakadhai.application.create_story_plan")
    def test_auto_mode_pre_ticks_confident_cuts_and_stops_at_review(self, create_plan, decide):
        app, record, _ = self._project_with_outputs("auto-story-123456")
        directory = app.projects_root / "auto-story-123456"
        (directory / "creative_profile.json").write_text(
            json.dumps({"transition": {"kind": "fade"}}), encoding="utf-8"
        )
        captions = directory / "captions_aligned.json"
        words = lambda text, start: [
            {"text": t, "start": start + i * 0.3, "end": start + i * 0.3 + 0.25}
            for i, t in enumerate(text.split())
        ]
        captions.write_text(json.dumps({"version": 1, "captions": [
            {"text": "naan Durai idhu", "words": words("naan Durai idhu", 1.0)},
            {"text": "naan Durai idhu Namma Kadhai", "words": words("naan Durai idhu Namma Kadhai", 2.5)},
            {"text": "vera vera padatha", "words": words("vera vera padatha", 5.0)},
        ]}), encoding="utf-8")
        old_plan = Path(record["active_plan"])
        payload = json.loads(old_plan.read_text(encoding="utf-8"))
        payload["captions"] = {"path": str(captions)}
        old_plan.write_text(json.dumps(payload), encoding="utf-8")
        record["caption_alignment"] = {"aligned_word_count": 11}
        (directory / "project.json").write_text(json.dumps(record), encoding="utf-8")

        new_plan = self.root / "auto-plan" / "story_edit.json"
        new_plan.parent.mkdir()
        new_plan.write_text(json.dumps({
            "source": {"path": str(self.source), "duration_seconds": 10},
            "captions": {"path": str(captions)}, "title": None, "music": None,
            "candidates": [
                {"candidate_id": "silence-long", "source_start_seconds": 6.5, "source_end_seconds": 8.5,
                 "proposed_cut_seconds": [6.75, 8.25]},
                {"candidate_id": "silence-short", "source_start_seconds": 8.6, "source_end_seconds": 9.6,
                 "proposed_cut_seconds": [8.85, 9.35]},
            ],
        }), encoding="utf-8")
        (new_plan.parent / "approvals.jsonl").write_text("", encoding="utf-8")
        create_plan.return_value = new_plan

        project = app.run_auto("auto-story-123456")

        decisions = {call.args[1]: (call.args[2], call.kwargs["actor"]) for call in decide.call_args_list}
        retake = next(key for key in decisions if key.startswith("auto-retake-"))
        doubling = next(key for key in decisions if key.startswith("auto-repeat-"))
        self.assertEqual(decisions[retake], ("APPROVED", "auto"))
        self.assertEqual(decisions[doubling], ("REJECTED", "auto"))
        self.assertEqual(decisions["silence-long"], ("APPROVED", "auto"))
        self.assertEqual(decisions["silence-short"], ("REJECTED", "auto"))
        self.assertEqual(decisions["captions"], ("APPROVED", "auto"))
        self.assertEqual(project["state"], "REVIEW")
        self.assertIsNone(project["render_result"])
        saved = json.loads(new_plan.read_text(encoding="utf-8"))
        self.assertEqual(saved["automation"]["transcription"]["source"], "srt")
        self.assertEqual(self.source.read_bytes(), b"immutable source")

    @patch("namakadhai.application.record_story_decision")
    def test_decision_updates_record_without_changing_source(self, record_decision):
        app = ProjectApplication(self.settings)
        directory = app.projects_root / "my-story-123456"
        directory.mkdir(parents=True)
        plan = self._fake_plan()
        record = {
            "version": 1,
            "project_id": "my-story-123456",
            "name": "My Story",
            "created_at_utc": "now",
            "updated_at_utc": "now",
            "state": "REVIEW",
            "source": {"path": str(self.source), "sha256": "hash", "metadata": self.metadata.to_dict()},
            "preferences": {},
            "plan_revisions": [],
            "active_plan": str(plan),
            "render_result": None,
            "finalization": None,
            "package": None,
            "last_error": None,
        }
        (directory / "project.json").write_text(json.dumps(record), encoding="utf-8")
        (directory / "events.jsonl").write_text("", encoding="utf-8")

        result = app.decide("my-story-123456", "silence-1", "REJECTED", "Keep pause")

        record_decision.assert_called_once()
        self.assertEqual(result["state"], "REVIEW")
        self.assertEqual(self.source.read_bytes(), b"immutable source")

    def test_preview_review_is_bound_to_the_current_output_hash(self):
        app, record, outputs = self._project_with_outputs()

        project = app.record_preview_review(record["project_id"], "landscape")

        expected_hash = hashlib.sha256(Path(outputs[0]["path"]).read_bytes()).hexdigest()
        self.assertEqual(
            project["preview_reviews"]["landscape"]["output_sha256"], expected_hash
        )
        receipt_path = Path(outputs[0]["path"]).parent / "preview_reviews.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt["reviews"]["landscape"]["output_sha256"], expected_hash
        )
        self.assertEqual(self.source.read_bytes(), b"immutable source")
        with self.assertRaisesRegex(ApplicationError, "landscape or vertical"):
            app.record_preview_review(record["project_id"], "portrait")

    @patch.object(ProjectApplication, "_plan_state", return_value=({}, {}, []))
    @patch("namakadhai.application.approve_story_exports")
    def test_interrupted_render_attempt_cannot_be_finalized(
        self, approve_exports, _plan_state
    ):
        app, record, _outputs = self._project_with_outputs()
        record["active_job"] = {
            "job_id": "a" * 32,
            "attempt_id": "b" * 32,
            "status": "running",
            "requested_formats": ["landscape", "vertical"],
            "output_paths": [],
        }
        (app.projects_root / record["project_id"] / "project.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

        app.recover_interrupted_jobs(
            [
                {
                    "job_id": "a" * 32,
                    "project_id": record["project_id"],
                    "status": "interrupted",
                    "error": "Restarted during rendering.",
                }
            ]
        )

        with self.assertRaisesRegex(ApplicationError, "successfully validated"):
            app.finalize(record["project_id"], "Do not approve an interrupted render.")
        self.assertEqual(
            app.get_project(record["project_id"])["active_job"]["status"],
            "interrupted",
        )
        approve_exports.assert_not_called()

    @patch("namakadhai.application.approve_story_exports")
    @patch.object(ProjectApplication, "_plan_state", return_value=({}, {}, []))
    def test_finalization_requires_current_preview_reviews_and_a_note(
        self, _plan_state, approve_exports
    ):
        app, record, outputs = self._project_with_outputs()
        project_id = record["project_id"]

        with self.assertRaisesRegex(ApplicationError, "Watch both current previews"):
            app.finalize(project_id, "Both previews watched.")
        approve_exports.assert_not_called()

        app.record_preview_review(project_id, "landscape")
        app.record_preview_review(project_id, "vertical")
        Path(outputs[1]["path"]).write_bytes(b"changed after review")
        with self.assertRaisesRegex(ApplicationError, "changed after review"):
            app.finalize(project_id, "Both previews watched.")
        approve_exports.assert_not_called()

        Path(outputs[1]["path"]).write_bytes(b"vertical preview")
        with self.assertRaisesRegex(ApplicationError, "approval note"):
            app.finalize(project_id, "  ")
        approve_exports.assert_not_called()


if __name__ == "__main__":
    unittest.main()
