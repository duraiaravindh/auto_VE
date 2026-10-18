from __future__ import annotations

import json
import math
import array
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from namakadhai.caption_renderer import CaptionCue, CaptionInputError, CaptionWord
from namakadhai.postproduction import MusicMetadata, _sha256_file
from namakadhai.probe import AudioStreamMetadata, VideoMetadata, VideoStreamMetadata
from namakadhai.story_editor import (
    StoryPlanError,
    StoryRenderError,
    _caption_asset,
    _approved_cuts,
    _build_render_graph,
    _measure_audio_quality,
    _map_captions,
    _validate_rendered,
    _validate_timeline,
    approve_story_exports,
    create_story_plan,
    load_creative_profile,
    request_ollama_suggestions,
    record_story_decision,
    record_story_preview_review,
    render_story_edit,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = PROJECT_ROOT / "config" / "nama_kadhai_nostalgic.json"
FFMPEG = PROJECT_ROOT / "tools" / "ffmpeg-9.0.2-full_build" / "bin" / "ffmpeg.exe"
FFPROBE = PROJECT_ROOT / "tools" / "ffmpeg-9.0.2-full_build" / "bin" / "ffprobe.exe"


class StoryEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _settings(self) -> Path:
        config = self.root / "config" / "settings.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "paths": {
                        "projects": "projects",
                        "ffmpeg_executable": str(FFMPEG),
                        "ffprobe_executable": str(FFPROBE),
                    },
                    "analysis": {
                        "audio": {
                            "silence_threshold_db": -35,
                            "minimum_silence_duration_seconds": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_loads_nostalgic_profile_and_dual_safe_formats(self):
        profile, digest = load_creative_profile(PROFILE_PATH)

        self.assertEqual(profile.profile_id, "nama_kadhai_nostalgic")
        self.assertEqual(profile.caption_style.font_name, "Poppins")
        self.assertTrue(profile.caption_style.bold)
        self.assertEqual(profile.transition_kind, "fade")
        self.assertEqual(profile.formats["vertical"]["width"], 1080)
        self.assertEqual(profile.formats["landscape"]["fit_mode"], "blurred_fill")
        self.assertFalse(profile.music_ducking_enabled)
        self.assertEqual(len(digest), 64)

    def test_profile_rejects_unbounded_crop_and_excessive_transition(self):
        profile_path = self.root / "profile.json"
        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        payload["formats"]["vertical"]["crop_x"] = 4
        profile_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(StoryPlanError, "crop"):
            load_creative_profile(profile_path)

        payload["formats"]["vertical"]["crop_x"] = 0.5
        payload["formats"]["landscape"]["fit_mode"] = "stretch"
        profile_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(StoryPlanError, "fit mode"):
            load_creative_profile(profile_path)

        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        payload["transition"]["duration_seconds"] = 3
        profile_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(StoryPlanError, "transition"):
            load_creative_profile(profile_path)

        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        payload["audio"]["ducking"]["ratio"] = 25
        profile_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(StoryPlanError, "ducking"):
            load_creative_profile(profile_path)

    def test_transition_graph_has_crossfades_and_format_specific_crop(self):
        profile, _ = load_creative_profile(PROFILE_PATH)

        graph, filters = _build_render_graph(
            ((0, 2), (3, 6)),
            profile=profile,
            layout=profile.formats["vertical"],
            source_audio=True,
            music=None,
            ass_path=None,
            output_duration=4.76,
        )

        self.assertIn("xfade=transition=fade", graph)
        self.assertIn("acrossfade=d=0.2400", graph)
        self.assertIn("scale=1080:1920", graph)
        self.assertIn("crop=1080:1920", graph)
        self.assertIn("colorbalance=", graph)
        self.assertIn("xfade", filters)
        self.assertIn("acrossfade", filters)

    def test_landscape_blurred_fill_preserves_portrait_source_framing(self):
        profile, _ = load_creative_profile(PROFILE_PATH)

        graph, filters = _build_render_graph(
            ((0, 2),),
            profile=profile,
            layout=profile.formats["landscape"],
            source_audio=True,
            music=None,
            ass_path=None,
            output_duration=2,
        )

        self.assertIn("split=2", graph)
        self.assertIn("boxblur=24:2", graph)
        self.assertIn("force_original_aspect_ratio=decrease", graph)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", graph)
        self.assertIn("tpad=stop_mode=clone", graph)
        self.assertIn("tpad", filters)
        self.assertIn("boxblur", filters)
        self.assertIn("overlay", filters)

    def test_ducking_is_optional_and_configurable_in_the_filter_graph(self):
        profile, _ = load_creative_profile(PROFILE_PATH)
        options = {
            "retained": ((0, 2),),
            "profile": profile,
            "layout": profile.formats["landscape"],
            "source_audio": True,
            "music": None,
            "ass_path": None,
            "output_duration": 2,
        }
        no_music_graph, no_music_filters = _build_render_graph(**options)
        self.assertNotIn("sidechaincompress", no_music_graph)
        self.assertNotIn("sidechaincompress", no_music_filters)

        music = MusicMetadata(
            self.root / "music.wav",
            "synthetic",
            2.0,
            "pcm_s16le",
            48000,
            1,
        )
        graph, filters = _build_render_graph(
            **{
                **options,
                "music": music,
                "music_ducking_enabled": True,
                "music_ducking_threshold_db": -30,
                "music_ducking_ratio": 8,
                "music_ducking_attack_ms": 20,
                "music_ducking_release_ms": 350,
            }
        )
        self.assertIn("[voice]asplit=2[voice_mix][voice_sc]", graph)
        self.assertIn("[music][voice_sc]sidechaincompress=threshold=0.03162278", graph)
        self.assertIn("ratio=8.000:attack=20.000:release=350.000", graph)
        self.assertIn("[voice_mix][music_ducked]amix", graph)
        self.assertIn("aformat=sample_rates=48000:channel_layouts=stereo", graph)
        self.assertIn("aresample=48000", graph)
        self.assertIn("sidechaincompress", filters)
        self.assertIn("afade=t=in", graph)
        self.assertIn("afade=t=out", graph)

    def test_synthetic_sidechain_reduces_music_during_dialogue(self):
        ffmpeg = str(FFMPEG)
        output = self.root / "ducked_music.f32"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                # Sources run 1 s past the measured window: sidechaincompress stops
                # at the first input EOF, and FFmpeg's threaded inputs make the
                # last ~0.1 s nondeterministic.
                "aevalsrc=exprs='if(between(t,0.5,1.5),0.8*sin(2*PI*330*t),0)':s=48000:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=3",
                "-filter_complex",
                "[1:a]volume=-6dB[m];"
                "[m][0:a]sidechaincompress=threshold=0.03:ratio=8:"
                "attack=10:release=250:makeup=1[out]",
                "-map",
                "[out]",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "f32le",
                str(output),
            ],
            check=True,
        )
        samples = array.array("f")
        with output.open("rb") as audio_file:
            samples.fromfile(audio_file, output.stat().st_size // samples.itemsize)
        self.assertGreaterEqual(len(samples), int(1.9 * 48000))

        def rms(start: float, end: float) -> float:
            segment = samples[int(start * 48000) : int(end * 48000)]
            return math.sqrt(sum(value * value for value in segment) / len(segment))

        before_speech = rms(0.15, 0.4)
        during_speech = rms(0.75, 1.2)
        after_speech = rms(1.7, 1.95)
        self.assertLess(during_speech, before_speech * 0.5)
        self.assertGreater(after_speech, during_speech * 1.5)

    def test_audio_measurement_reports_hot_and_silent_synthetic_audio(self):
        profile, _ = load_creative_profile(PROFILE_PATH)
        hot = self.root / "hot_audio.wav"
        silence = self.root / "silent_audio.wav"
        for target, source in (
            (hot, "sine=frequency=440:sample_rate=48000:duration=2"),
            (silence, "anullsrc=channel_layout=mono:sample_rate=48000:duration=2"),
        ):
            command = [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                source,
            ]
            if target == hot:
                command.extend(("-af", "volume=18dB"))
            command.extend(("-c:a", "pcm_s16le", str(target)))
            subprocess.run(command, check=True)

        measure_options = {
            "ffmpeg": FFMPEG,
            "target_lufs": profile.audio_target_lufs,
            "loudness_warning_tolerance_lu": profile.loudness_warning_tolerance_lu,
            "true_peak_warning_dbtp": profile.true_peak_warning_dbtp,
        }
        hot_result = _measure_audio_quality(hot, **measure_options)
        self.assertIsNotNone(hot_result["integrated_loudness_lufs"])
        self.assertTrue(
            any("True peak" in warning for warning in hot_result["warnings"])
        )
        silence_result = _measure_audio_quality(silence, **measure_options)
        self.assertTrue(silence_result["warnings"])

    def test_timeline_validation_is_frame_aware_and_detects_av_mismatch(self):
        metadata = VideoMetadata(
            file_name="synthetic.mp4",
            file_size_bytes=100,
            duration_seconds=3.4,
            format_name="mp4",
            format_long_name="MP4",
            video=VideoStreamMetadata("h264", 640, 360, 24.0, 3.4, 0.0),
            audio=AudioStreamMetadata("aac", 48000, 1, 3.399, 0.001),
        )
        checked = _validate_timeline(metadata, 3.36, label="synthetic.mp4")
        self.assertAlmostEqual(checked["duration_tolerance_seconds"], 1.5 / 24 + 2 / 48000)
        self.assertAlmostEqual(checked["audio_video_duration_difference_seconds"], 0.001)
        self.assertAlmostEqual(checked["audio_video_start_offset_seconds"], 0.001)
        self.assertLessEqual(
            checked["duration_difference_seconds"],
            checked["duration_tolerance_seconds"],
        )

        off_sync = VideoMetadata(
            **{
                **metadata.__dict__,
                "audio": AudioStreamMetadata("aac", 48000, 1, 3.2),
            }
        )
        with self.assertRaisesRegex(StoryRenderError, "audio/video stream durations differ"):
            _validate_timeline(off_sync, 3.36, label="off-sync.mp4")

        start_misaligned = VideoMetadata(
            **{
                **metadata.__dict__,
                "audio": AudioStreamMetadata("aac", 48000, 1, 3.399, 0.2),
            }
        )
        with self.assertRaisesRegex(StoryRenderError, "audio/video stream start times differ"):
            _validate_timeline(start_misaligned, 3.36, label="start-misaligned.mp4")

        two_frames_late = VideoMetadata(
            **{**metadata.__dict__, "duration_seconds": 3.36 + (2 / 24)}
        )
        with self.assertRaisesRegex(StoryRenderError, "duration differs"):
            _validate_timeline(two_frames_late, 3.36, label="late.mp4")

    def test_render_validation_rejects_missing_audio_stream(self):
        output = self.root / "no_audio.mp4"
        output.write_bytes(b"synthetic")
        metadata = VideoMetadata(
            file_name=output.name,
            file_size_bytes=output.stat().st_size,
            duration_seconds=2.0,
            format_name="mp4",
            format_long_name="MP4",
            video=VideoStreamMetadata("h264", 640, 360, 24.0, 2.0),
            audio=None,
        )
        with patch("namakadhai.story_editor.probe_video", return_value=metadata):
            with self.assertRaisesRegex(StoryRenderError, "missing the voice/music audio stream"):
                _validate_rendered(
                    output,
                    settings_path=None,
                    expected_duration=2.0,
                    width=640,
                    height=360,
                    expect_audio=True,
                )

    def test_caption_times_follow_retained_source_ranges_and_uncertain_lines_drop(self):
        cues = (
            CaptionCue(0.5, 1.5, "Exact Tanglish words", ("Tanglish",)),
            CaptionCue(1.5, 2.4, "Do not render this", uncertain=True),
            CaptionCue(3.2, 4.0, "Keep this line"),
        )

        mapped = _map_captions(cues, ((0, 2), (3, 5)), 0.2)

        self.assertEqual(len(mapped), 2)
        self.assertEqual(mapped[0].text, "Exact Tanglish words")
        self.assertEqual(mapped[0].highlights, ("Tanglish",))
        self.assertAlmostEqual(mapped[0].start_seconds, 0.5)
        self.assertAlmostEqual(mapped[1].start_seconds, 1.8 + 0.2)
        self.assertEqual(mapped[1].text, "Keep this line")

    def test_caption_spanning_removed_source_range_is_omitted_not_duplicated(self):
        cue = CaptionCue(1.5, 3.5, "One continuous line")

        mapped = _map_captions((cue,), ((0, 2), (3, 5)), 0.2)

        self.assertEqual(mapped, ())

    def test_word_timed_caption_is_split_around_cut_without_invented_timing(self):
        cue = CaptionCue(
            0.5,
            3.6,
            "one two three",
            words=(
                CaptionWord("one", 0.5, 0.9, 0.9, "ஒன்று"),
                CaptionWord("two", 1.1, 1.5, 0.9, "இரண்டு"),
                CaptionWord("three", 3.2, 3.6, 0.9, "மூன்று"),
            ),
        )

        mapped = _map_captions((cue,), ((0, 2), (3, 5)), 0.2)

        self.assertEqual([item.text for item in mapped], ["one two", "three"])
        self.assertAlmostEqual(mapped[0].start_seconds, 0.5)
        self.assertAlmostEqual(mapped[1].start_seconds, 1.85)
        self.assertAlmostEqual(mapped[1].words[0].start_seconds, 2.0)
        self.assertAlmostEqual(mapped[0].words[1].start_seconds, 1.1)
        self.assertEqual(mapped[1].words[0].original, "மூன்று")

    def test_approved_cuts_validate_and_pending_items_are_not_applied(self):
        candidate = {
            "candidate_id": "silence-example",
            "source_start_seconds": 2.0,
            "source_end_seconds": 4.0,
            "proposed_cut_seconds": [2.25, 3.75],
        }
        payload = {"candidates": [candidate]}

        self.assertEqual(_approved_cuts(payload, {}, 10), ())
        self.assertEqual(
            _approved_cuts(payload, {"silence-example": "APPROVED"}, 10),
            ((2.25, 3.75),),
        )
        with self.assertRaisesRegex(StoryPlanError, "out of bounds"):
            bad = {**candidate, "proposed_cut_seconds": [1.0, 3.0]}
            _approved_cuts({"candidates": [bad]}, {"silence-example": "APPROVED"}, 10)

    def _write_planner_plan(self) -> Path:
        source = self.root / "local_source.mp4"
        source.write_bytes(b"local source")
        plan = self.root / "story_edit.json"
        plan.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": {
                        "path": str(source),
                        "size_bytes": source.stat().st_size,
                        "sha256": _sha256_file(source),
                        "duration_seconds": 10,
                    },
                    "candidates": [
                        {
                            "candidate_id": "silence-candidate",
                            "source_start_seconds": 1.0,
                            "source_end_seconds": 2.0,
                            "proposed_cut_seconds": [1.25, 1.75],
                            "measured_evidence": "Synthetic test interval.",
                        }
                    ],
                    "captions": None,
                    "title": None,
                    "music": None,
                }
            ),
            encoding="utf-8",
        )
        (self.root / "approvals.jsonl").write_text("", encoding="utf-8")
        return plan

    def test_ollama_suggestions_are_schema_checked_and_never_approve(self):
        plan = self._write_planner_plan()

        class Response:
            def __init__(self, payload: dict):
                self.payload = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args
                return False

            def read(self):
                return self.payload

        responses = [
            Response({"models": [{"name": "local-model:7b"}]}),
            Response(
                {
                    "response": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "item_id": "silence-candidate",
                                    "reason": "Review the quiet span manually.",
                                }
                            ]
                        }
                    )
                }
            ),
        ]
        with patch(
            "namakadhai.story_editor.urllib.request.urlopen",
            side_effect=responses,
        ) as open_url:
            result = request_ollama_suggestions(plan, model="local-model:7b")

        saved = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(saved["approval_status"], "SUGGESTIONS_ONLY")
        self.assertEqual(len(saved["suggestions"]), 1)
        self.assertEqual((self.root / "approvals.jsonl").read_text(encoding="utf-8"), "")
        self.assertEqual(open_url.call_count, 2)

    def test_finalization_requires_all_creative_decisions_resolved(self):
        settings, source, _, captions = self._create_synthetic_inputs()
        plan = create_story_plan(
            source,
            settings_path=settings,
            profile_path=PROFILE_PATH,
            captions_path=captions,
            title="A pending title",
        )
        with self.assertRaisesRegex(StoryPlanError, "Review or explicitly reject"):
            approve_story_exports(plan, note="Synthetic review")
            approve_story_exports(plan, note="Synthetic review")

    def test_ollama_cannot_suggest_unlisted_cut_ids_or_download_models(self):
        plan = self._write_planner_plan()

        class Response:
            def __init__(self, payload: dict):
                self.payload = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args
                return False

            def read(self):
                return self.payload

        with patch(
            "namakadhai.story_editor.urllib.request.urlopen",
            return_value=Response({"models": []}),
        ) as open_url:
            with self.assertRaisesRegex(StoryPlanError, "not installed locally"):
                request_ollama_suggestions(plan, model="missing:latest")
        self.assertEqual(open_url.call_count, 1)

        responses = [
            Response({"models": [{"name": "local-model:7b"}]}),
            Response(
                {
                    "response": json.dumps(
                        {
                            "suggestions": [
                                {"item_id": "invented-cut", "reason": "Try this cut."}
                            ]
                        }
                    )
                }
            ),
        ]
        with patch(
            "namakadhai.story_editor.urllib.request.urlopen",
            side_effect=responses,
        ):
            with self.assertRaisesRegex(StoryPlanError, "invalid item"):
                request_ollama_suggestions(plan, model="local-model:7b")
        self.assertFalse((self.root / "ollama_suggestions.json").exists())

    def test_plan_requires_manually_reviewed_caption_json(self):
        captions = self.root / "captions.json"
        captions.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reviewed": False,
                    "captions": [{"start": 0, "end": 1, "text": "not reviewed"}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CaptionInputError, "reviewed: true"):
            _caption_asset(captions, 2)

    def _create_synthetic_inputs(self) -> tuple[Path, Path, Path, Path]:
        settings = self._settings()
        source = self.root / "synthetic_story.mp4"
        music = self.root / "synthetic_music.wav"
        captions = self.root / "reviewed_captions.json"
        subprocess.run(
            [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=24:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=330:sample_rate=48000:duration=4",
                "-af",
                "volume='if(between(t,0.1,1.2)+between(t,2.0,3.5),0.35,0)':eval=frame",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            check=True,
        )
        subprocess.run(
            [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=8",
                "-c:a",
                "pcm_s16le",
                str(music),
            ],
            check=True,
        )
        captions.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reviewed": True,
                    "captions": [
                        {
                            "start": 0.2,
                            "end": 0.9,
                            "text": "Stories from our childhood.",
                            "highlight": ["childhood"],
                        },
                        {
                            "start": 1.6,
                            "end": 2.6,
                            "text": "This uncertain line is omitted.",
                            "uncertain": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return settings, source, music, captions

    def test_synthetic_end_to_end_edit_renders_two_approved_formats_safely(self):
        settings, source, music, captions = self._create_synthetic_inputs()
        profile_payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        profile_payload["audio"]["ducking"]["enabled"] = True
        ducking_profile = self.root / "ducking_profile.json"
        ducking_profile.write_text(json.dumps(profile_payload), encoding="utf-8")
        source_hash = _sha256_file(source)
        plan = create_story_plan(
            source,
            settings_path=settings,
            profile_path=ducking_profile,
            captions_path=captions,
            title="Stories from home",
            music_path=music,
            music_provider="Synthetic integration fixture",
            music_license="Synthetic generated tone",
            music_evidence="Generated locally by the test",
            confirm_music_license=True,
        )
        payload = json.loads(plan.read_text(encoding="utf-8"))
        payload["candidates"].append(
            {
                "candidate_id": "synthetic-approved-cut",
                "source_start_seconds": 1.1,
                "source_end_seconds": 1.4,
                "proposed_cut_seconds": [1.1, 1.4],
            }
        )
        plan.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for item_id in ("synthetic-approved-cut", "captions", "title", "music"):
            record_story_decision(plan, item_id, "APPROVED", note="Synthetic test approval")

        attempt_exports = plan.parent / "render_attempts" / "synthetic-attempt-001"
        result = render_story_edit(
            plan,
            settings_path=settings,
            output_directory=attempt_exports,
        )
        self.assertEqual(Path(result["exports_directory"]), attempt_exports)

        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual(result["caption_status"], "APPROVED")
        self.assertEqual(result["music_status"], "APPROVED")
        for item in result["outputs"]:
            output = Path(item["path"])
            report = Path(item["render_report"])
            self.assertTrue(output.is_file())
            self.assertTrue(report.is_file())
            self.assertGreater(output.stat().st_size, 0)
            self.assertLessEqual(
                abs(item["duration_seconds"] - 3.46),
                1.5 / 24 + 2 / 48000,
            )
        landscape = next(item for item in result["outputs"] if item["format"] == "landscape")
        vertical = next(item for item in result["outputs"] if item["format"] == "vertical")
        self.assertEqual(landscape["dimensions"], [1920, 1080])
        self.assertEqual(vertical["dimensions"], [1080, 1920])
        self.assertEqual(
            json.loads(Path(landscape["render_report"]).read_text(encoding="utf-8"))[
                "output_metadata"
            ]["audio"]["sample_rate_hz"],
            48000,
        )
        self.assertEqual(_sha256_file(source), source_hash)
        landscape_report = json.loads(Path(landscape["render_report"]).read_text(encoding="utf-8"))
        self.assertEqual(landscape_report["captions"]["rendered_cue_count"], 1)
        self.assertEqual(landscape_report["captions"]["uncertain_cues_omitted"], 1)
        self.assertEqual(
            landscape_report["output_metadata"]["file_name"],
            "review_landscape.mp4",
        )
        self.assertTrue(landscape_report["music"]["used_in_render"])
        self.assertEqual(
            landscape_report["music"]["provenance"]["status"],
            "USER_CONFIRMED",
        )
        quality = landscape_report["audio_quality"]
        self.assertIsInstance(quality["integrated_loudness_lufs"], float)
        self.assertIsInstance(quality["true_peak_dbtp"], float)
        self.assertEqual(quality["measurement_filter"], "ebur128=peak=true")
        self.assertIn("duration_tolerance_seconds", landscape_report["timeline_validation"])
        self.assertEqual(
            landscape_report["audio_processing"]["music_ducking"]["enabled"],
            True,
        )
        self.assertEqual(
            landscape_report["timeline_validation"]["warnings"],
            [],
        )
        self.assertIsNotNone(
            landscape_report["timeline_validation"][
                "audio_video_duration_difference_seconds"
            ]
        )

        preview_bytes = Path(landscape["path"]).read_bytes()
        with self.assertRaisesRegex(StoryPlanError, "reviewed in the application"):
            approve_story_exports(
                plan,
                note="Synthetic outputs reviewed and approved.",
                settings_path=settings,
                output_directory=attempt_exports,
            )
        for output in result["outputs"]:
            record_story_preview_review(plan, output["format"], output["path"])
        Path(landscape["path"]).write_bytes(preview_bytes + b"tampered")
        with self.assertRaisesRegex(StoryPlanError, "Review the current landscape preview"):
            approve_story_exports(
                plan,
                note="Synthetic outputs reviewed and approved.",
                settings_path=settings,
                output_directory=attempt_exports,
            )
        Path(landscape["path"]).write_bytes(preview_bytes)

        report_bytes = Path(landscape["render_report"]).read_bytes()
        report_payload = json.loads(report_bytes)
        report_payload["output_sha256"] = "0" * 64
        Path(landscape["render_report"]).write_text(
            json.dumps(report_payload), encoding="utf-8"
        )
        with self.assertRaisesRegex(StoryPlanError, "preview report does not match"):
            approve_story_exports(
                plan,
                note="Synthetic report-integrity rejection.",
                settings_path=settings,
                output_directory=attempt_exports,
            )
        Path(landscape["render_report"]).write_bytes(report_bytes)

        finalized = approve_story_exports(
            plan,
            note="Synthetic outputs reviewed and approved.",
            settings_path=settings,
            output_directory=attempt_exports,
        )
        approval_path = Path(finalized["approval_report"])
        self.assertTrue(approval_path.is_file())
        approval_report = json.loads(approval_path.read_text(encoding="utf-8"))
        self.assertEqual(approval_report["source_sha256"], source_hash)
        for output_format, final_path in finalized["final_exports"].items():
            final = Path(final_path)
            preview = Path(
                next(
                    item["path"]
                    for item in result["outputs"]
                    if item["format"] == output_format
                )
            )
            self.assertTrue(final.is_file())
            self.assertEqual(_sha256_file(final), _sha256_file(preview))

        with self.assertRaisesRegex(StoryRenderError, "Refusing to overwrite finalized"):
            approve_story_exports(
                plan,
                note="Second finalization should not overwrite.",
                settings_path=settings,
                output_directory=attempt_exports,
            )

        with self.assertRaisesRegex(StoryRenderError, "Refusing to overwrite"):
            render_story_edit(
                plan,
                settings_path=settings,
                output_directory=attempt_exports,
            )


if __name__ == "__main__":
    unittest.main()
