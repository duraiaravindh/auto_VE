from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from namakadhai.probe import (
    ProbeExecutionError,
    ProbeOutputError,
    ProbeTimeoutError,
    probe_video,
)


VALID_PROBE_OUTPUT = {
    "format": {
        "duration": "12.5",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
    },
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30/1",
            "duration": "12.5",
            "start_time": "0",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
            "duration": "12.499",
            "start_time": "0.001",
        },
    ],
}


class ProbeVideoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.video = self.root / "Tamil video.mp4"
        self.video.write_bytes(b"original-media")
        self.settings = self.root / "config" / "settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(
            json.dumps(
                {
                    "paths": {
                        "ffprobe_executable": "tools/ffmpeg/bin/ffprobe.exe"
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_probe(self, payload: str | dict = VALID_PROBE_OUTPUT):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    @patch("namakadhai.probe.subprocess.run")
    def test_reports_video_and_audio_metadata(self, run) -> None:
        run.return_value = self.run_probe()

        report = probe_video(self.video, settings_path=self.settings)

        self.assertEqual(report.file_name, self.video.name)
        self.assertEqual(report.file_size_bytes, len(b"original-media"))
        self.assertEqual(report.duration_seconds, 12.5)
        self.assertEqual(report.format_name, "mov,mp4,m4a,3gp,3g2,mj2")
        self.assertEqual(report.video.codec, "h264")
        self.assertEqual(report.video.width, 1920)
        self.assertEqual(report.video.height, 1080)
        self.assertAlmostEqual(report.video.frame_rate, 30000 / 1001)
        self.assertEqual(report.video.duration_seconds, 12.5)
        self.assertEqual(report.video.start_time_seconds, 0.0)
        self.assertEqual(report.audio.codec, "aac")
        self.assertEqual(report.audio.sample_rate_hz, 48000)
        self.assertEqual(report.audio.channels, 2)
        self.assertEqual(report.audio.duration_seconds, 12.499)
        self.assertEqual(report.audio.start_time_seconds, 0.001)
        self.assertNotIn(str(self.root), json.dumps(report.to_dict()))

        command = run.call_args.args[0]
        self.assertEqual(
            command[1:],
            [
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(self.video.resolve()),
            ],
        )
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(
            Path(command[0]),
            (self.root / "tools" / "ffmpeg" / "bin" / "ffprobe.exe").resolve(),
        )
        self.assertEqual(self.video.read_bytes(), b"original-media")

    @patch("namakadhai.probe.subprocess.run")
    def test_reports_video_without_audio(self, run) -> None:
        payload = {
            "format": {"duration": "4"},
            "streams": [{"codec_type": "video", "codec_name": "hevc"}],
        }
        run.return_value = self.run_probe(payload)

        report = probe_video(self.video, settings_path=self.settings)

        self.assertIsNone(report.audio)
        self.assertIsNone(report.video.width)
        self.assertIsNone(report.video.frame_rate)

    @patch("namakadhai.probe.subprocess.run")
    def test_ignores_nonfinite_media_durations(self, run) -> None:
        payload = {
            "format": {"duration": "nan"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "duration": "inf",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 1,
                    "duration": "-inf",
                },
            ],
        }
        run.return_value = self.run_probe(payload)

        report = probe_video(self.video, settings_path=self.settings)

        self.assertIsNone(report.duration_seconds)
        self.assertIsNone(report.video.duration_seconds)
        self.assertIsNone(report.audio.duration_seconds)

    def test_rejects_missing_video_file(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "missing.mp4"):
            probe_video(self.root / "missing.mp4", settings_path=self.settings)

    @patch("namakadhai.probe.subprocess.run")
    def test_rejects_invalid_json(self, run) -> None:
        run.return_value = self.run_probe("{not json")
        with self.assertRaisesRegex(ProbeOutputError, "malformed JSON"):
            probe_video(self.video, settings_path=self.settings)

    @patch("namakadhai.probe.subprocess.run")
    def test_rejects_metadata_without_video_stream(self, run) -> None:
        run.return_value = self.run_probe(
            {"streams": [{"codec_type": "audio"}]}
        )
        with self.assertRaisesRegex(ProbeOutputError, "No video stream"):
            probe_video(self.video, settings_path=self.settings)

    @patch("namakadhai.probe.subprocess.run")
    def test_reports_nonzero_ffprobe_exit(self, run) -> None:
        run.return_value = CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Could not open {self.video.resolve()}",
        )
        with self.assertRaises(ProbeExecutionError) as context:
            probe_video(self.video, settings_path=self.settings)
        self.assertIn(self.video.name, str(context.exception))
        self.assertNotIn(str(self.root), str(context.exception))

    @patch("namakadhai.probe.subprocess.run")
    def test_reports_timeout(self, run) -> None:
        run.side_effect = TimeoutExpired(cmd="ffprobe", timeout=1)
        with self.assertRaisesRegex(ProbeTimeoutError, "timed out"):
            probe_video(self.video, settings_path=self.settings, timeout=1)


if __name__ == "__main__":
    unittest.main()
