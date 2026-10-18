from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from namakadhai.media_review import (
    MediaReviewError,
    ReviewOutputExistsError,
    UnsupportedMediaError,
    build_contact_sheet_command,
    preflight_media,
    prepare_media_review,
)
from namakadhai.probe import (
    AudioStreamMetadata,
    ProbeOutputError,
    VideoMetadata,
    VideoStreamMetadata,
)


class MediaReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.video = self.root / "Tamil Story.mp4"
        self.video.write_bytes(b"source bytes")
        self.settings_path = self.root / "config" / "settings.json"
        self.settings_path.parent.mkdir()
        self.ffmpeg = self.root / "tools" / "ffmpeg.exe"
        self.ffmpeg.parent.mkdir()
        self.ffmpeg.write_bytes(b"ffmpeg stub")
        self.settings_path.write_text(
            json.dumps(
                {
                    "paths": {
                        "projects": "projects",
                        "ffmpeg_executable": "tools/ffmpeg.exe",
                        "ffprobe_executable": "tools/ffprobe.exe",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.metadata = VideoMetadata(
            file_name=self.video.name,
            file_size_bytes=len(b"source bytes"),
            duration_seconds=25.0,
            format_name="mov,mp4",
            format_long_name="QuickTime / MOV",
            video=VideoStreamMetadata(
                codec="h264",
                width=1280,
                height=720,
                frame_rate=30.0,
            ),
            audio=AudioStreamMetadata(
                codec="aac",
                sample_rate_hz=48000,
                channels=2,
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("namakadhai.media_review.probe_video")
    def test_preflight_rejects_missing_file(self, probe) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "missing.mp4"):
            preflight_media(self.root / "missing.mp4", settings_path=self.settings_path)
        probe.assert_not_called()

    @patch("namakadhai.media_review.probe_video")
    def test_preflight_rejects_unsupported_extension(self, probe) -> None:
        text_file = self.root / "not-video.txt"
        text_file.write_text("not video", encoding="utf-8")
        with self.assertRaises(UnsupportedMediaError):
            preflight_media(text_file, settings_path=self.settings_path)
        probe.assert_not_called()

    @patch("namakadhai.media_review.probe_video")
    def test_preflight_surfaces_invalid_probe_output(self, probe) -> None:
        probe.side_effect = ProbeOutputError("No video stream was found.")
        with self.assertRaisesRegex(MediaReviewError, "No video stream"):
            preflight_media(self.video, settings_path=self.settings_path)

    def test_contact_sheet_command_is_bounded_and_reads_source(self) -> None:
        output = self.root / "projects" / "story" / "visual_review" / "contact_sheet.jpg"
        command = build_contact_sheet_command(
            self.ffmpeg,
            self.video,
            output,
            duration_seconds=600,
            interval_seconds=5,
            max_frames=10,
            thumbnail_width=240,
            columns=4,
        )

        self.assertEqual(command[0], str(self.ffmpeg))
        self.assertIn(str(self.video.resolve()), command)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        filter_graph = command[command.index("-vf") + 1]
        self.assertIn("fps=1/5.000000", filter_graph)
        self.assertIn("scale=240:-2", filter_graph)
        self.assertIn("tile=4x3:nb_frames=10", filter_graph)
        self.assertEqual(command[-1], str(output))
        self.assertFalse(output.parent.exists())
        self.assertEqual(self.video.read_bytes(), b"source bytes")

    def test_contact_sheet_rejects_unbounded_frame_counts(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 24"):
            build_contact_sheet_command(
                self.ffmpeg,
                self.video,
                self.root / "sheet.jpg",
                duration_seconds=100,
                max_frames=25,
            )

    @patch("namakadhai.media_review.subprocess.run")
    @patch("namakadhai.media_review.probe_video")
    def test_review_writes_report_and_contact_sheet_in_project_output(
        self, probe, run
    ) -> None:
        probe.return_value = self.metadata

        def create_contact_sheet(command, **kwargs):
            Path(command[-1]).write_bytes(b"sheet")
            return CompletedProcess(command, 0, "", "")

        run.side_effect = create_contact_sheet
        result = prepare_media_review(
            self.video,
            settings_path=self.settings_path,
            interval_seconds=8,
            max_frames=6,
        )

        self.assertTrue(result.paths.project_directory.is_relative_to(self.root / "projects"))
        self.assertTrue(result.paths.report.is_file())
        self.assertTrue(result.paths.contact_sheet.is_file())
        report = result.paths.report.read_text(encoding="utf-8")
        self.assertIn("30.000 fps", report)
        self.assertIn("aac", report)
        self.assertIn("generic suggestions, not AI findings", report)
        self.assertIn("No speech or story analysis was performed", report)
        self.assertEqual(self.video.read_bytes(), b"source bytes")
        self.assertFalse(run.call_args.kwargs["shell"])

    @patch("namakadhai.media_review.subprocess.run")
    @patch("namakadhai.media_review.probe_video")
    def test_report_warns_when_ffmpeg_fails(self, probe, run) -> None:
        probe.return_value = self.metadata
        run.return_value = CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="decoder failure",
        )

        result = prepare_media_review(self.video, settings_path=self.settings_path)

        self.assertEqual(len(result.warnings), 1)
        self.assertIn("FFmpeg failed", result.warnings[0])
        report = result.paths.report.read_text(encoding="utf-8")
        self.assertIn("decoder failure", report)
        self.assertIn("Not generated", report)

    @patch("namakadhai.media_review.probe_video")
    def test_existing_report_is_not_overwritten(self, probe) -> None:
        probe.return_value = self.metadata
        project = self.root / "projects"
        project_name = self._expected_project_name()
        report_path = project / project_name / "media_review.md"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("keep this report", encoding="utf-8")

        with patch("namakadhai.media_review.subprocess.run") as run:
            with self.assertRaises(ReviewOutputExistsError):
                prepare_media_review(self.video, settings_path=self.settings_path)
            run.assert_not_called()
        self.assertEqual(report_path.read_text(encoding="utf-8"), "keep this report")

    def _expected_project_name(self) -> str:
        from namakadhai.media_review import _safe_project_name

        return _safe_project_name(self.video)


if __name__ == "__main__":
    unittest.main()
