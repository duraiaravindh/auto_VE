from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from namakadhai.transcription import (
    ModelApprovalRequiredError,
    OutputExistsError,
    TranscriptionConfig,
    TranscriptionConfigurationError,
    TranscriptionDependencyError,
    TranscriptionInferenceError,
    TranscriptionMediaError,
    TranscriptionOutputError,
    _load_local_whisper_model,
    format_subtitles,
    load_transcription_config,
    transcribe_media,
)


class FakeWhisperModel:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or {
            "text": "naan innikku oru kathai solla poren",
            "segments": [
                {"start": 0.2, "end": 1.75, "text": "naan innikku oru kathai"},
                {"start": 1.75, "end": 3.0, "text": "solla poren"},
            ],
        }
        self.error = error
        self.audio_path: Path | None = None
        self.options = None

    def transcribe(self, audio: str, **options):
        self.audio_path = Path(audio)
        self.options = options
        if self.error:
            raise self.error
        return self.result


class TranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "சென்னை கதை.mp4"
        self.source.write_bytes(b"original video bytes")
        self.model_directory = self.root / "models" / "whisper"
        self.model_directory.mkdir(parents=True)
        (self.model_directory / "base.pt").write_bytes(b"test checkpoint")
        self.projects = self.root / "projects"
        self.temp = self.root / "temp"
        self.ffmpeg = self.root / "tools" / "ffmpeg.exe"
        self.ffmpeg.parent.mkdir(parents=True)
        self.ffmpeg.write_bytes(b"fake ffmpeg executable")
        self.config = TranscriptionConfig(
            model_directory=self.model_directory,
            projects_directory=self.projects,
            temp_directory=self.temp,
            ffmpeg_executable=self.ffmpeg,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def fake_model_loader(self, model: str, checkpoint: Path, device: str):
        self.assertEqual(model, "base")
        self.assertEqual(checkpoint, self.model_directory / "base.pt")
        self.assertEqual(device, "cpu")
        return self.model

    def setUp_fake_model(self, result=None, error: Exception | None = None):
        self.model = FakeWhisperModel(result=result, error=error)

    def run_fake_ffmpeg(self, command, **kwargs):
        self.assertFalse(kwargs["shell"])
        self.assertEqual(command[0], str(self.ffmpeg))
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        Path(command[-1]).write_bytes(b"temporary 16 kHz audio")
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    @patch("namakadhai.transcription.subprocess.run")
    def test_writes_raw_review_srt_and_vtt_with_cpu_options(self, run) -> None:
        self.setUp_fake_model()
        run.side_effect = self.run_fake_ffmpeg

        result = transcribe_media(
            self.source,
            config=self.config,
            model_loader=self.fake_model_loader,
        )

        outputs = result.outputs
        raw = outputs.raw_transcript.read_text(encoding="utf-8")
        self.assertEqual(raw, "naan innikku oru kathai solla poren")
        self.assertEqual(
            outputs.review_transcript.read_text(encoding="utf-8"), raw
        )
        self.assertEqual(
            outputs.subtitles_srt.read_text(encoding="utf-8"),
            "1\n00:00:00,200 --> 00:00:01,750\nnaan innikku oru kathai\n\n"
            "2\n00:00:01,750 --> 00:00:03,000\nsolla poren",
        )
        self.assertEqual(
            outputs.subtitles_vtt.read_text(encoding="utf-8"),
            "WEBVTT\n\n"
            "00:00:00.200 --> 00:00:01.750\nnaan innikku oru kathai\n\n"
            "00:00:01.750 --> 00:00:03.000\nsolla poren",
        )
        self.assertEqual(self.model.options["language"], "ta")
        self.assertEqual(self.model.options["task"], "transcribe")
        self.assertFalse(self.model.options["fp16"])
        self.assertFalse(self.model.options["word_timestamps"])
        self.assertTrue(
            self.model.audio_path and self.model.audio_path.name.endswith(".wav")
        )
        self.assertFalse(self.model.audio_path.exists())
        self.assertEqual(self.source.read_bytes(), b"original video bytes")
        self.assertEqual(list(self.temp.iterdir()), [])

    @patch("namakadhai.transcription.subprocess.run")
    def test_empty_transcription_writes_empty_transcript_and_valid_vtt(self, run) -> None:
        self.setUp_fake_model(result={"text": "", "segments": []})
        run.side_effect = self.run_fake_ffmpeg

        result = transcribe_media(
            self.source,
            config=self.config,
            model_loader=self.fake_model_loader,
        )

        self.assertEqual(result.outputs.raw_transcript.read_bytes(), b"")
        self.assertEqual(result.outputs.subtitles_srt.read_bytes(), b"")
        self.assertEqual(
            result.outputs.subtitles_vtt.read_text(encoding="utf-8"), "WEBVTT\n\n"
        )

    def test_missing_input_file_is_reported(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "missing.mp4"):
            transcribe_media(
                self.root / "missing.mp4",
                config=self.config,
                model_loader=self.fake_model_loader,
            )

    @patch("namakadhai.transcription.subprocess.run")
    def test_missing_weights_require_explicit_approval_and_do_not_run(self, run) -> None:
        (self.model_directory / "base.pt").unlink()
        with self.assertRaisesRegex(ModelApprovalRequiredError, "explicit approval"):
            transcribe_media(
                self.source,
                config=self.config,
                model_loader=self.fake_model_loader,
            )
        run.assert_not_called()
        self.assertFalse(self.projects.exists())

    def test_missing_whisper_dependency_has_a_clear_error(self) -> None:
        with patch.dict(sys.modules, {"whisper": None}):
            with self.assertRaisesRegex(TranscriptionDependencyError, ".venv"):
                _load_local_whisper_model(
                    "base", self.model_directory / "base.pt", "cpu"
                )

    @patch("namakadhai.transcription.subprocess.run")
    def test_inference_failure_cleans_temp_audio(self, run) -> None:
        self.setUp_fake_model(error=RuntimeError("test failure"))
        run.side_effect = self.run_fake_ffmpeg

        with self.assertRaisesRegex(TranscriptionInferenceError, "inference failed"):
            transcribe_media(
                self.source,
                config=self.config,
                model_loader=self.fake_model_loader,
            )

        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertFalse((self.projects / "subtitles.srt").exists())

    def test_srt_and_vtt_timestamp_formatting_and_wrapping(self) -> None:
        srt, vtt = format_subtitles(
            [
                {
                    "start": 3600.25,
                    "end": 3603.5,
                    "text": "one two three four five six seven eight nine ten eleven",
                }
            ]
        )

        self.assertEqual(
            srt,
            "1\n01:00:00,250 --> 01:00:03,500\n"
            "one two three four five six seven eight\n"
            "nine ten eleven",
        )
        self.assertEqual(
            vtt,
            "WEBVTT\n\n"
            "01:00:00.250 --> 01:00:03.500\n"
            "one two three four five six seven eight\n"
            "nine ten eleven",
        )

    @patch("namakadhai.transcription.subprocess.run")
    def test_existing_outputs_are_not_overwritten(self, run) -> None:
        self.setUp_fake_model()
        output_dir = self.root / "existing-project"
        output_dir.mkdir()
        existing = output_dir / "transcript_raw.txt"
        existing.write_text("keep this", encoding="utf-8")

        with self.assertRaises(OutputExistsError):
            transcribe_media(
                self.source,
                config=self.config,
                project_directory=output_dir,
                model_loader=self.fake_model_loader,
            )

        self.assertEqual(existing.read_text(encoding="utf-8"), "keep this")
        run.assert_not_called()

    def test_loads_project_settings_and_enforces_cpu_no_download(self) -> None:
        settings_path = self.root / "config" / "settings.json"
        settings_path.parent.mkdir()
        settings_path.write_text(
            json.dumps(
                {
                    "paths": {
                        "models": "models",
                        "projects": "projects",
                        "temp": "temp",
                        "ffmpeg_executable": "tools/ffmpeg.exe",
                    },
                    "transcription": {
                        "model": "base",
                        "device": "cpu",
                        "language": "ta",
                        "download_model_on_first_use": False,
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = load_transcription_config(settings_path)

        self.assertEqual(loaded.device, "cpu")
        self.assertEqual(loaded.language, "ta")
        self.assertFalse(loaded.download_model_on_first_use)
        self.assertEqual(loaded.ffmpeg_executable, self.root / "tools" / "ffmpeg.exe")

    def test_rejects_english_only_model_and_automatic_model_downloads(self) -> None:
        with self.assertRaises(TranscriptionConfigurationError):
            TranscriptionConfig(model="base.en").validate()
        with self.assertRaises(TranscriptionConfigurationError):
            TranscriptionConfig(download_model_on_first_use=True).validate()

    @patch("namakadhai.transcription.subprocess.run")
    def test_ffmpeg_failure_is_reported(self, run) -> None:
        self.setUp_fake_model()
        run.return_value = CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="No audio stream",
        )

        with self.assertRaisesRegex(TranscriptionMediaError, "could not extract audio"):
            transcribe_media(
                self.source,
                config=self.config,
                model_loader=self.fake_model_loader,
            )
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_invalid_subtitle_timestamps_are_rejected(self) -> None:
        with self.assertRaises(TranscriptionOutputError):
            format_subtitles([{"start": 3, "end": 2, "text": "bad timing"}])


if __name__ == "__main__":
    unittest.main()
