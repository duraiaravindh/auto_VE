from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from namakadhai.audio_analysis import (
    AudioAnalysisOutputExistsError,
    AudioAnalysisToolError,
    analyze_audio,
)
from namakadhai.probe import (
    AudioStreamMetadata,
    VideoMetadata,
    VideoStreamMetadata,
)


class AudioAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.video = self.root / "Story video.mp4"
        self.video.write_bytes(b"original video")
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
                    },
                    "analysis": {
                        "audio": {
                            "silence_threshold_db": -32,
                            "minimum_silence_duration_seconds": 0.8,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.metadata = VideoMetadata(
            file_name=self.video.name,
            file_size_bytes=self.video.stat().st_size,
            duration_seconds=12.0,
            format_name="mov,mp4",
            format_long_name="QuickTime / MOV",
            video=VideoStreamMetadata("h264", 1280, 720, 30.0),
            audio=AudioStreamMetadata("aac", 48000, 2),
        )
        from namakadhai.media_review import _review_paths

        self.review_paths = _review_paths(self.video, self.settings_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _report_path(self) -> Path:
        return self.review_paths.project_directory / "audio_analysis.md"

    @patch("namakadhai.audio_analysis.subprocess.run")
    @patch("namakadhai.audio_analysis.preflight_media")
    def test_detects_quiet_intervals_and_reports_audio_levels(self, preflight, run):
        preflight.return_value = (self.metadata, self.review_paths)
        run.return_value = CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr=(
                "[silencedetect] silence_start: 2.125\n"
                "[silencedetect] silence_end: 3.500 | silence_duration: 1.375\n"
                "[Parsed_volumedetect] mean_volume: -22.4 dB\n"
                "[Parsed_volumedetect] max_volume: -2.1 dB\n"
            ),
        )

        result = analyze_audio(self.video, settings_path=self.settings_path)

        self.assertEqual(result.threshold_db, -32.0)
        self.assertEqual(result.minimum_duration_seconds, 0.8)
        self.assertEqual(len(result.quiet_intervals), 1)
        interval = result.quiet_intervals[0]
        self.assertAlmostEqual(interval.start_seconds, 2.125)
        self.assertAlmostEqual(interval.end_seconds, 3.5)
        self.assertAlmostEqual(interval.duration_seconds, 1.375)
        self.assertEqual(result.levels.mean_volume_db, -22.4)
        self.assertEqual(result.levels.max_volume_db, -2.1)
        self.assertTrue(result.report_path.is_relative_to(self.root / "projects"))
        report = result.report_path.read_text(encoding="utf-8")
        self.assertIn("2.125", report)
        self.assertIn("-22.40 dBFS", report)
        self.assertIn(
            "Quiet speech and expressive pauses can be misclassified", report
        )
        self.assertEqual(run.call_args.args[0][0], str(self.ffmpeg.resolve()))
        self.assertIn(
            "silencedetect=noise=-32.00dB:d=0.800,volumedetect",
            run.call_args.args[0],
        )
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(self.video.read_bytes(), b"original video")

    @patch("namakadhai.audio_analysis.subprocess.run")
    @patch("namakadhai.audio_analysis.preflight_media")
    def test_no_audio_writes_report_without_running_ffmpeg(self, preflight, run):
        no_audio = VideoMetadata(
            file_name=self.metadata.file_name,
            file_size_bytes=self.metadata.file_size_bytes,
            duration_seconds=12,
            format_name=self.metadata.format_name,
            format_long_name=self.metadata.format_long_name,
            video=self.metadata.video,
            audio=None,
        )
        preflight.return_value = (no_audio, self.review_paths)

        result = analyze_audio(self.video, settings_path=self.settings_path)

        self.assertFalse(result.has_audio)
        self.assertIsNone(result.levels)
        self.assertEqual(result.quiet_intervals, ())
        self.assertIn("No audio stream", result.warnings[0])
        self.assertIn(
            "No audio stream was detected",
            result.report_path.read_text(encoding="utf-8"),
        )
        run.assert_not_called()

    @patch("namakadhai.audio_analysis.subprocess.run")
    @patch("namakadhai.audio_analysis.preflight_media")
    def test_ffmpeg_failure_is_reported_without_report_success(self, preflight, run):
        preflight.return_value = (self.metadata, self.review_paths)
        run.return_value = CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="decoder failed",
        )

        with self.assertRaisesRegex(AudioAnalysisToolError, "exit code 1"):
            analyze_audio(self.video, settings_path=self.settings_path)
        self.assertFalse(self._report_path().exists())

    @patch("namakadhai.audio_analysis.subprocess.run")
    @patch("namakadhai.audio_analysis.preflight_media")
    def test_empty_detection_result_is_reported_as_no_candidates(self, preflight, run):
        preflight.return_value = (self.metadata, self.review_paths)
        run.return_value = CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr=(
                "[Parsed_volumedetect] mean_volume: -inf dB\n"
                "[Parsed_volumedetect] max_volume: -inf dB\n"
            ),
        )

        result = analyze_audio(self.video, settings_path=self.settings_path)

        self.assertEqual(result.quiet_intervals, ())
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("No intervals met", result.warnings[0])
        report = result.report_path.read_text(encoding="utf-8")
        self.assertIn("No quiet intervals met", report)
        self.assertIn("Mean volume: Unavailable", report)

    @patch("namakadhai.audio_analysis.subprocess.run")
    @patch("namakadhai.audio_analysis.preflight_media")
    def test_existing_report_is_not_overwritten(self, preflight, run):
        preflight.return_value = (self.metadata, self.review_paths)
        report_path = self._report_path()
        report_path.parent.mkdir(parents=True)
        report_path.write_text("preserve report", encoding="utf-8")

        with self.assertRaises(AudioAnalysisOutputExistsError):
            analyze_audio(self.video, settings_path=self.settings_path)

        self.assertEqual(report_path.read_text(encoding="utf-8"), "preserve report")
        run.assert_not_called()

    @patch("namakadhai.audio_analysis.subprocess.run")
    @patch("namakadhai.audio_analysis.preflight_media")
    def test_timeout_is_reported_clearly(self, preflight, run):
        preflight.return_value = (self.metadata, self.review_paths)
        run.side_effect = TimeoutExpired(cmd="ffmpeg", timeout=600)

        with self.assertRaisesRegex(AudioAnalysisToolError, "timed out"):
            analyze_audio(self.video, settings_path=self.settings_path)


if __name__ == "__main__":
    unittest.main()
