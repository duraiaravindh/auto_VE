from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from namakadhai.audio_analysis import AudioAnalysisResult, AudioLevels, QuietInterval
from namakadhai.postproduction import (
    CutCandidate,
    EditPlanError,
    MusicMetadata,
    PostProductionError,
    RenderValidationError,
    SourceSnapshot,
    _atomic_publish,
    _build_filter_graph,
    _candidate_id,
    _make_candidates,
    _music_provenance,
    _probe_music,
    _required_render_filters,
    _retained_ranges,
    _validate_output_timeline,
    _validate_rendered_output,
    analyze_for_edit_plan,
    record_candidate_decision,
    render_approved_preview,
    validate_approved_cuts,
)
from namakadhai.probe import (
    AudioStreamMetadata,
    VideoMetadata,
    VideoStreamMetadata,
)


def _metadata(path: Path, duration: float = 20.0, *, audio: bool = True) -> VideoMetadata:
    return VideoMetadata(
        file_name=path.name,
        file_size_bytes=path.stat().st_size if path.exists() else 10,
        duration_seconds=duration,
        format_name="mp4",
        format_long_name="MPEG-4",
        video=VideoStreamMetadata("h264", 1280, 720, 30.0, duration, 0.0),
        audio=AudioStreamMetadata("aac", 48000, 2, duration, 0.001) if audio else None,
    )


class PostProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "story with spaces.mp4"
        self.source.write_bytes(b"source media bytes")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.snapshot = SourceSnapshot(
            self.source,
            self.source.stat().st_size,
            self.digest,
            _metadata(self.source),
        )
        self.candidate = CutCandidate(
            candidate_id=_candidate_id(2.0, 5.0),
            source_start=2.0,
            source_end=5.0,
            cut_start=2.25,
            cut_end=4.75,
            retained_ranges=((2.0, 2.25), (4.75, 5.0)),
            detection_method="FFmpeg silencedetect",
            evidence="Below -35 dBFS for 3 seconds.",
            confidence="LOW",
            confidence_explanation="Signal-level only.",
            limitations="Listen manually.",
        )
        self.plan_path = self.root / "edit_plan.json"
        self.plan_payload = {
            "version": 1,
            "source": self.snapshot.to_dict(),
            "analysis": {},
            "candidates": [self.candidate.to_dict()],
        }
        self.plan_path.write_text(
            json.dumps(self.plan_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        self.approvals_path = self.root / "approvals.jsonl"
        self.approvals_path.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_silence_candidates_include_padding_and_retained_ranges(self):
        analysis = AudioAnalysisResult(
            report_path=self.root / "audio.md",
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(QuietInterval(2, 5, 3),),
            levels=AudioLevels(-20, -3),
            warnings=(),
        )

        candidates = _make_candidates(
            analysis,
            duration_seconds=20,
            padding_before_seconds=0.3,
            padding_after_seconds=0.4,
            minimum_cut_seconds=0.25,
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.status, "PENDING")
        self.assertEqual((candidate.cut_start, candidate.cut_end), (2.3, 4.6))
        self.assertEqual(candidate.retained_ranges, ((2, 2.3), (4.6, 5)))
        self.assertEqual(candidate.confidence, "LOW")
        self.assertIn("silencedetect", candidate.detection_method)

    def test_cut_boundaries_are_snapped_inward_to_source_frame_grid(self):
        analysis = AudioAnalysisResult(
            report_path=self.root / "audio.md",
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(QuietInterval(2.01, 5.01, 3),),
            levels=AudioLevels(-20, -3),
            warnings=(),
        )
        candidate = _make_candidates(
            analysis,
            duration_seconds=20,
            padding_before_seconds=0.25,
            padding_after_seconds=0.25,
            minimum_cut_seconds=0.25,
            frame_rate_hz=30,
        )[0]
        self.assertAlmostEqual(candidate.cut_start * 30, round(candidate.cut_start * 30))
        self.assertAlmostEqual(candidate.cut_end * 30, round(candidate.cut_end * 30))
        self.assertGreaterEqual(candidate.cut_start, 2.01)
        self.assertLessEqual(candidate.cut_end, 5.01)

    def test_no_valid_silence_candidates_are_invented(self):
        analysis = AudioAnalysisResult(
            report_path=self.root / "audio.md",
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(QuietInterval(2, 2.2, 0.2),),
            levels=AudioLevels(-20, -3),
            warnings=(),
        )

        self.assertEqual(
            _make_candidates(
                analysis,
                duration_seconds=20,
                padding_before_seconds=0.25,
                padding_after_seconds=0.25,
                minimum_cut_seconds=0.25,
            ),
            (),
        )

    def test_rejects_negative_padding_and_invalid_source_interval(self):
        analysis = AudioAnalysisResult(
            report_path=self.root / "audio.md",
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(QuietInterval(19, 21, 2),),
            levels=AudioLevels(-20, -3),
            warnings=(),
        )
        self.assertEqual(
            _make_candidates(
                analysis,
                duration_seconds=20,
                padding_before_seconds=0,
                padding_after_seconds=0,
                minimum_cut_seconds=0.25,
            ),
            (),
        )
        with self.assertRaises(ValueError):
            _make_candidates(
                analysis,
                duration_seconds=20,
                padding_before_seconds=-0.1,
                padding_after_seconds=0,
                minimum_cut_seconds=0.25,
            )

    def test_validates_approved_cut_bounds_and_retained_ranges(self):
        cuts = validate_approved_cuts(
            self.plan_payload,
            {self.candidate.candidate_id: {"status": "APPROVED"}},
        )
        self.assertEqual(cuts, ((2.25, 4.75),))
        self.assertEqual(_retained_ranges(cuts, 20), ((0, 2.25), (4.75, 20)))
        self.assertEqual(
            validate_approved_cuts(self.plan_payload, {}),
            (),
        )

    def test_rejects_out_of_bounds_or_invalid_approved_cuts(self):
        for invalid_cut in ((-1, 2), (5, 5), (19, 21)):
            payload = json.loads(json.dumps(self.plan_payload))
            payload["candidates"][0]["proposed_cut_seconds"] = list(invalid_cut)
            with self.subTest(cut=invalid_cut):
                with self.assertRaises(EditPlanError):
                    validate_approved_cuts(
                        payload,
                        {self.candidate.candidate_id: {"status": "APPROVED"}},
                    )

    def test_rejects_overlapping_approved_cuts(self):
        second = self.candidate.to_dict()
        second.update(
            candidate_id="silence-other",
            source_start_seconds=4,
            source_end_seconds=7,
            proposed_cut_seconds=[4.5, 6.0],
        )
        payload = json.loads(json.dumps(self.plan_payload))
        payload["candidates"].append(second)
        approvals = {
            self.candidate.candidate_id: {"status": "APPROVED"},
            "silence-other": {"status": "APPROVED"},
        }
        with self.assertRaisesRegex(EditPlanError, "overlap"):
            validate_approved_cuts(payload, approvals)

    def test_rejects_stale_source_hash(self):
        self.source.write_bytes(b"changed source")
        with self.assertRaisesRegex(EditPlanError, "stale"):
            validate_approved_cuts(
                self.plan_payload,
                {self.candidate.candidate_id: {"status": "APPROVED"}},
            )

    def test_review_decisions_are_append_only_and_plan_bound(self):
        first = record_candidate_decision(
            self.plan_path,
            self.candidate.candidate_id,
            "APPROVED",
            note="Reviewed by user",
        )
        record_candidate_decision(
            self.plan_path,
            self.candidate.candidate_id,
            "REJECTED",
            note="Changed decision",
        )
        events = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["status"] for event in events], ["APPROVED", "REJECTED"])
        self.assertTrue(all(event["plan_sha256"] for event in events))

    def test_rejects_unknown_candidate_and_invalid_review_status(self):
        with self.assertRaises(EditPlanError):
            record_candidate_decision(self.plan_path, "unknown", "APPROVED")
        with self.assertRaises(EditPlanError):
            record_candidate_decision(
                self.plan_path, self.candidate.candidate_id, "maybe"
            )

    def test_filter_graph_normalizes_voice_and_keeps_music_subtle(self):
        music = MusicMetadata(
            self.root / "music.wav",
            "music-hash",
            30,
            "pcm_s16le",
            48000,
            2,
        )
        graph, filters = _build_filter_graph(
            ((0, 20),),
            has_source_audio=True,
            audio_target_lufs=-20,
            true_peak_dbtp=-2,
            music=music,
            music_gain_db=-24,
            fade_in_seconds=1,
            fade_out_seconds=2,
            video_correction=(0.02, 1.02, 1.0),
            white_balance=(-0.02, 0.0, 0.02),
        )
        self.assertIn("loudnorm=I=-20.00:TP=-2.00", graph)
        self.assertIn("volume=-24.00dB", graph)
        self.assertIn("afade=t=in", graph)
        self.assertIn("afade=t=out", graph)
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("eq=brightness=0.0200", graph)
        self.assertIn("colorbalance=rs=-0.0200:gs=0.0000:bs=0.0200", graph)
        self.assertNotIn("scale=", graph)
        self.assertIn("alimiter", filters)
        self.assertIn("colorbalance", filters)

        ducked_graph, ducked_filters = _build_filter_graph(
            ((0, 20),),
            has_source_audio=True,
            audio_target_lufs=-20,
            true_peak_dbtp=-2,
            music=music,
            music_gain_db=-24,
            fade_in_seconds=1,
            fade_out_seconds=2,
            video_correction=None,
            white_balance=None,
            music_ducking_enabled=True,
        )
        self.assertIn("[voice]asplit=2[voice_mix][voice_sc]", ducked_graph)
        self.assertIn("[music][voice_sc]sidechaincompress", ducked_graph)
        self.assertIn("[voice_mix][music_ducked]amix", ducked_graph)
        self.assertIn("sidechaincompress", ducked_filters)
        self.assertIn("linear=false:print_format=json", ducked_graph)
        self.assertIn("aformat=sample_rates=48000:channel_layouts=stereo", ducked_graph)

    def test_hard_cut_joins_apply_short_audio_fades_and_cfr(self):
        graph, filters = _build_filter_graph(
            ((0, 2), (4, 6)),
            has_source_audio=True,
            audio_target_lufs=-20,
            true_peak_dbtp=-2,
            music=None,
            music_gain_db=-24,
            fade_in_seconds=0,
            fade_out_seconds=0,
            video_correction=None,
            frame_rate_hz=29.97,
        )
        self.assertIn("fps=29.970000,settb=AVTB", graph)
        self.assertIn("afade=t=out", graph)
        self.assertIn("afade=t=in", graph)
        self.assertIn("afade", filters)

    def test_filter_graph_rejects_unsafe_levels_and_color_settings(self):
        options = dict(
            retained=((0, 20),),
            has_source_audio=True,
            audio_target_lufs=-20,
            true_peak_dbtp=-2,
            music=None,
            music_gain_db=-24,
            fade_in_seconds=1,
            fade_out_seconds=2,
            video_correction=None,
            white_balance=None,
        )
        with self.assertRaises(ValueError):
            _build_filter_graph(**{**options, "true_peak_dbtp": 0})
        with self.assertRaises(ValueError):
            _build_filter_graph(**{**options, "audio_target_lufs": 0})
        with self.assertRaises(ValueError):
            _build_filter_graph(**{**options, "video_correction": (1, 1, 1)})
        with self.assertRaises(ValueError):
            _build_filter_graph(**{**options, "white_balance": (0, 0, 0.2)})

    def test_required_filters_match_optional_streams_and_corrections(self):
        required = _required_render_filters(
            retained_range_count=1,
            has_source_audio=False,
            has_music=False,
            has_fades=False,
            video_correction=False,
            white_balance=False,
        )
        self.assertEqual(required, {"trim", "setpts", "null", "fps", "settb"})

        required = _required_render_filters(
            retained_range_count=2,
            has_source_audio=True,
            has_music=True,
            has_fades=True,
            video_correction=True,
            white_balance=True,
        )
        self.assertTrue(
            {
                "concat",
                "atrim",
                "asetpts",
                "aformat",
                "aresample",
                "loudnorm",
                "alimiter",
                "volume",
                "afade",
                "amix",
                "eq",
                "colorbalance",
            }.issubset(required)
        )
        ducked_required = _required_render_filters(
            retained_range_count=1,
            has_source_audio=True,
            has_music=True,
            has_fades=True,
            video_correction=False,
            white_balance=False,
            music_ducking=True,
        )
        self.assertIn("sidechaincompress", ducked_required)

    def test_music_provenance_does_not_assert_independent_legal_clearance(self):
        music = MusicMetadata(self.root / "track.mp3", "sha", 60, "mp3", 44100, 2)
        record = _music_provenance(
            music,
            source_url="",
            provider="",
            license_name="",
            attribution="",
            evidence="",
            user_confirmed=False,
        )
        self.assertEqual(record["status"], "UNVERIFIED")
        self.assertIn("do not publish", record["warnings"][0])

        confirmed = _music_provenance(
            music,
            source_url="https://example.invalid",
            provider="Provider",
            license_name="User supplied licence",
            attribution="Credit required",
            evidence="local licence copy",
            user_confirmed=True,
        )
        self.assertEqual(confirmed["status"], "USER_CONFIRMED")
        self.assertIn("not independent legal verification", confirmed["warnings"][0])

    def test_rejects_missing_music_file(self):
        with self.assertRaisesRegex(PostProductionError, "does not exist"):
            _probe_music(self.root / "missing.wav", settings_path=None)

    @patch("namakadhai.postproduction._run_ffmpeg")
    @patch("namakadhai.postproduction._resolve_ffprobe")
    def test_music_probe_records_audio_metadata_and_hash(self, resolve, run_ffprobe):
        music_path = self.root / "track.wav"
        music_path.write_bytes(b"music bytes")
        resolve.return_value = self.root / "ffprobe.exe"
        run_ffprobe.return_value = CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {"duration": "61.5"},
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "pcm_s16le",
                            "sample_rate": "48000",
                            "channels": 2,
                        }
                    ],
                }
            ),
            stderr="",
        )

        result = _probe_music(music_path, settings_path=self.root / "settings.json")

        self.assertEqual(result.sha256, hashlib.sha256(b"music bytes").hexdigest())
        self.assertEqual(result.duration_seconds, 61.5)
        self.assertEqual(result.sample_rate_hz, 48000)
        self.assertEqual(result.channels, 2)

    @patch("namakadhai.postproduction._run_ffmpeg")
    @patch("namakadhai.postproduction._resolve_ffprobe")
    def test_music_probe_rejects_media_without_audio(self, resolve, run_ffprobe):
        music_path = self.root / "video-only.mp4"
        music_path.write_bytes(b"video bytes")
        resolve.return_value = self.root / "ffprobe.exe"
        run_ffprobe.return_value = CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"format": {"duration": "10"}, "streams": []}),
            stderr="",
        )

        with self.assertRaisesRegex(PostProductionError, "no readable audio"):
            _probe_music(music_path, settings_path=self.root / "settings.json")

    def test_atomic_publish_refuses_existing_destination(self):
        temp = self.root / "temp.mp4"
        output = self.root / "output.mp4"
        temp.write_bytes(b"render")
        output.write_bytes(b"keep")
        with self.assertRaises(PostProductionError):
            _atomic_publish(temp, output)
        self.assertEqual(output.read_bytes(), b"keep")
        self.assertTrue(temp.exists())

    def test_atomic_publish_falls_back_to_windows_rename_when_links_are_unavailable(self):
        temp = self.root / "staged.mp4"
        output = self.root / "published.mp4"
        temp.write_bytes(b"rendered")
        with patch(
            "namakadhai.postproduction.os.link",
            side_effect=OSError("hard links unsupported"),
        ):
            _atomic_publish(temp, output)
        self.assertEqual(output.read_bytes(), b"rendered")
        self.assertFalse(temp.exists())

    @patch("namakadhai.postproduction.preflight_media")
    @patch("namakadhai.postproduction.analyze_audio")
    @patch("namakadhai.postproduction._load_settings")
    def test_analysis_creates_hash_bound_pending_plan_without_media_edits(
        self, settings, analyze, preflight
    ):
        settings.return_value = (
            {"paths": {"projects": "projects"}},
            self.root,
        )
        preflight.return_value = (
            _metadata(self.source),
            type(
                "Paths",
                (),
                {"project_directory": self.root / "projects" / "story"},
            )(),
        )

        def analysis_result(*_args, **kwargs):
            self.assertEqual(_args[0], self.source)
            report_path = kwargs["report_path"]
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("analysis", encoding="utf-8")
            return AudioAnalysisResult(
                report_path=report_path,
                has_audio=True,
                threshold_db=-35,
                minimum_duration_seconds=1,
                quiet_intervals=(),
                levels=AudioLevels(-15, -2),
                warnings=(),
            )

        analyze.side_effect = analysis_result
        result = analyze_for_edit_plan(
            self.source,
            settings_path=self.root / "config" / "settings.json",
        )
        stored = json.loads(result.plan_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["source"]["sha256"], self.digest)
        self.assertEqual(stored["candidates"], [])
        self.assertTrue(result.report_path.is_file())
        self.assertTrue(result.approvals_path.is_file())
        self.assertEqual(self.source.read_bytes(), b"source media bytes")

    def _approved_plan(self) -> None:
        record_candidate_decision(
            self.plan_path,
            self.candidate.candidate_id,
            "APPROVED",
            note="approved test cut",
        )

    @patch("namakadhai.postproduction._available_filters")
    @patch("namakadhai.postproduction._resolve_ffmpeg")
    @patch("namakadhai.postproduction._snapshot_source")
    @patch("namakadhai.postproduction._run_ffmpeg")
    def test_dry_run_shows_command_without_render_or_output(
        self, run_ffmpeg, snapshot, resolve_ffmpeg, filters
    ):
        self._approved_plan()
        snapshot.return_value = self.snapshot
        resolve_ffmpeg.return_value = self.root / "ffmpeg.exe"
        output = self.root / "preview.mp4"

        result = render_approved_preview(
            self.plan_path,
            output,
            dry_run=True,
        )

        self.assertTrue(result["dry_run"])
        self.assertIn("-n", result["command"])
        self.assertIn("-filter_complex", result["command"])
        self.assertEqual(result["approved_cut_ranges_seconds"], [[2.25, 4.75]])
        self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".*.partial.mp4")))
        filters.assert_called_once()
        run_ffmpeg.assert_not_called()

    @patch("namakadhai.postproduction._available_filters")
    @patch("namakadhai.postproduction._resolve_ffmpeg")
    @patch("namakadhai.postproduction._snapshot_source")
    def test_pending_cut_is_not_applied_during_dry_run(
        self, snapshot, resolve_ffmpeg, _filters
    ):
        snapshot.return_value = self.snapshot
        resolve_ffmpeg.return_value = self.root / "ffmpeg.exe"

        result = render_approved_preview(
            self.plan_path,
            self.root / "pending.mp4",
            dry_run=True,
        )

        self.assertEqual(result["approved_cut_ranges_seconds"], [])
        self.assertEqual(result["retained_source_ranges_seconds"], [[0.0, 20.0]])
        _filters.assert_called_once()

    @patch("namakadhai.postproduction._available_filters")
    @patch("namakadhai.postproduction._resolve_ffmpeg")
    @patch("namakadhai.postproduction._snapshot_source")
    @patch("namakadhai.postproduction._run_ffmpeg")
    def test_render_failure_cleans_partial_outputs(
        self, run_ffmpeg, snapshot, resolve_ffmpeg, filters
    ):
        snapshot.return_value = self.snapshot
        resolve_ffmpeg.return_value = self.root / "ffmpeg.exe"
        run_ffmpeg.side_effect = PostProductionError("synthetic FFmpeg failure")
        output = self.root / "failed_preview.mp4"

        with self.assertRaisesRegex(PostProductionError, "synthetic FFmpeg failure"):
            render_approved_preview(self.plan_path, output)

        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".render-report.json").exists())
        self.assertFalse(any(self.root.glob(".*.partial*")))
        filters.assert_called_once()

    @patch("namakadhai.postproduction._snapshot_source")
    def test_render_refuses_existing_output(self, snapshot):
        snapshot.return_value = self.snapshot
        output = self.root / "existing_preview.mp4"
        output.write_bytes(b"keep")

        with self.assertRaisesRegex(PostProductionError, "Refusing to overwrite"):
            render_approved_preview(self.plan_path, output, dry_run=True)

        self.assertEqual(output.read_bytes(), b"keep")

    @patch("namakadhai.postproduction.probe_video")
    def test_output_validation_checks_duration_and_audio(self, probe):
        probe.return_value = _metadata(self.source, 20, audio=True)
        result = _validate_rendered_output(
            self.source,
            settings_path=None,
            expected_duration=20,
            expect_audio=True,
        )
        self.assertEqual(result.duration_seconds, 20)

        probe.return_value = _metadata(self.source, 5, audio=True)
        with self.assertRaises(RenderValidationError):
            _validate_rendered_output(
                self.source,
                settings_path=None,
                expected_duration=20,
                expect_audio=True,
            )

    def test_legacy_timeline_validation_uses_frame_tolerance_and_checks_sync(self):
        metadata = _metadata(self.source, 20.04, audio=True)
        checked = _validate_output_timeline(metadata, 20.0)
        self.assertLessEqual(
            checked["duration_tolerance_seconds"],
            1.5 / 30 + 2 / 48000,
        )
        self.assertAlmostEqual(checked["audio_video_start_offset_seconds"], 0.001)

        too_long = _metadata(self.source, 20.1, audio=True)
        with self.assertRaisesRegex(RenderValidationError, "duration differs"):
            _validate_output_timeline(too_long, 20.0)

        desynchronized = VideoMetadata(
            file_name=self.source.name,
            file_size_bytes=self.source.stat().st_size,
            duration_seconds=20.0,
            format_name="mp4",
            format_long_name="MPEG-4",
            video=VideoStreamMetadata("h264", 1280, 720, 30.0, 20.0, 0.0),
            audio=AudioStreamMetadata("aac", 48000, 2, 20.0, 0.1),
        )
        with self.assertRaisesRegex(RenderValidationError, "start times differ"):
            _validate_output_timeline(desynchronized, 20.0)


if __name__ == "__main__":
    unittest.main()
