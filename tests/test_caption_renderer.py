from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from namakadhai.caption_renderer import (
    CaptionCue,
    CaptionInputError,
    CaptionStyle,
    CaptionWord,
    CaptionOutputExistsError,
    CaptionRenderError,
    build_ass,
    build_render_command,
    load_caption_cues,
    render_threads,
    render_caption_preview,
)
from namakadhai.probe import AudioStreamMetadata, VideoMetadata, VideoStreamMetadata


class CaptionRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.video = self.root / "recording.mp4"
        self.video.write_bytes(b"source-video")
        self.caption_json = self.root / "captions.json"
        self.caption_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "captions": [
                        {
                            "start": 0.5,
                            "end": 3.2,
                            "text": "Chinna vayasula festival vandhaale,",
                            "highlight": ["festival"],
                        },
                        {
                            "start": 3.2,
                            "end": 6.0,
                            "text": "namakku oru thani excitement!",
                            "highlight": ["excitement"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.ffmpeg = self.root / "tools" / "ffmpeg.exe"
        self.ffmpeg.parent.mkdir()
        self.ffmpeg.write_bytes(b"ffmpeg")
        self.settings = self.root / "config" / "settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(
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
            file_size_bytes=self.video.stat().st_size,
            duration_seconds=10,
            format_name="mp4",
            format_long_name="MPEG-4",
            video=VideoStreamMetadata("h264", 1920, 1080, 30),
            audio=AudioStreamMetadata("aac", 48000, 2),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loads_manually_timed_sentence_cues(self) -> None:
        cues = load_caption_cues(self.caption_json)

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Chinna vayasula festival vandhaale,")
        self.assertEqual(cues[0].highlights, ("festival",))

    def test_ass_has_two_line_max_gold_word_and_timed_sentence_chunks(self) -> None:
        cues = load_caption_cues(self.caption_json)
        ass = build_ass(
            cues,
            width=1920,
            height=1080,
            layout="landscape",
        )

        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("Dialogue: 0,0:00:00.50,0:00:03.20", ass)
        self.assertIn(r"{\c&H0038A6D6&}festival{\c&H00DEF4FF&}", ass)
        self.assertIn(r"{\c&H0038A6D6&}excitement{\c&H00DEF4FF&}", ass)
        self.assertNotIn(r"\k", ass)
        self.assertEqual(ass.count("Dialogue: "), 2)
        self.assertIn(
            r"{\an2\move(960,1008,960,999,0,160)\fad(120,160)\fscx92\fscy92"
            r"\t(0,96,\fscx103\fscy103)\t(96,160,\fscx100\fscy100)}",
            ass,
        )
        for line in (line for line in ass.splitlines() if line.startswith("Dialogue:")):
            text = line.split(",,0,0,0,,", maxsplit=1)[1]
            self.assertLessEqual(text.count(r"\N") + 1, 2)

    def test_fade_animation_and_short_cue_clamping(self) -> None:
        ass = build_ass(
            (CaptionCue(0, 0.3, "Quick"),),
            width=1920,
            height=1080,
            layout="landscape",
            style=CaptionStyle(animation="fade"),
        )
        self.assertIn(r"{\fad(100,100)\fscx98\fscy98\t(0,120,\fscx100\fscy100)}Quick", ass)

        pop = build_ass(
            (CaptionCue(0, 0.3, "Quick"),), width=1920, height=1080, layout="landscape"
        )
        self.assertIn(r"\move(960,1008,960,999,0,150)", pop)

    def test_word_highlight_lights_each_word_while_spoken(self) -> None:
        cue = CaptionCue(
            1.0, 2.5, "oru periya",
            words=(CaptionWord("oru", 1.15, 1.4), CaptionWord("periya", 1.6, 2.1)),
        )
        ass = build_ass(
            (cue,), width=1920, height=1080, layout="landscape",
            style=CaptionStyle(word_highlight=True),
        )
        self.assertIn(
            r"{\1c&H00DEF4FF&\t(150,151,\1c&H0038A6D6&)\t(600,601,\1c&H00DEF4FF&)}oru "
            r"{\1c&H00DEF4FF&\t(600,601,\1c&H0038A6D6&)\t(1100,1101,\1c&H00DEF4FF&)}periya",
            ass,
        )
        mismatched = CaptionCue(1.0, 2.5, "oru periya", words=(CaptionWord("oru", 1.15, 1.4),))
        plain = build_ass(
            (mismatched,), width=1920, height=1080, layout="landscape",
            style=CaptionStyle(word_highlight=True),
        )
        self.assertNotIn(r"\t(150,151", plain)
        with self.assertRaises(CaptionInputError):
            build_ass(
                (CaptionCue(0, 1, "x"),), width=1920, height=1080, layout="landscape",
                style=CaptionStyle(animation="spin"),
            )

    def test_title_overlay_uses_separate_top_safe_area_and_fades(self) -> None:
        ass = build_ass(
            (CaptionCue(0, 2, "Reviewed caption"),),
            width=1080,
            height=1920,
            layout="vertical",
            title_text="A story from home",
        )

        self.assertIn("Style: NamaKadhaiTitle", ass)
        self.assertIn("Dialogue: 1,0:00:00.00,0:00:03.00,NamaKadhaiTitle", ass)
        self.assertIn(r"{\fad(180,260)}A story from home", ass)

    def test_title_can_render_without_dialogue_captions(self) -> None:
        ass = build_ass(
            (),
            width=1920,
            height=1080,
            layout="landscape",
            title_text="A user-supplied title",
        )

        self.assertEqual(ass.count("Dialogue: "), 1)
        self.assertIn("A user-supplied title", ass)

    def test_vertical_layout_uses_different_safe_area(self) -> None:
        cue = (CaptionCue(0, 2, "Namma childhood memories marakka mudiyuma?"),)
        landscape = build_ass(cue, width=1920, height=1080, layout="landscape")
        vertical = build_ass(cue, width=1080, height=1920, layout="vertical")

        self.assertIn("PlayResX: 1080", vertical)
        landscape_style = next(line for line in landscape.splitlines() if line.startswith("Style:"))
        vertical_style = next(line for line in vertical.splitlines() if line.startswith("Style:"))
        self.assertNotEqual(landscape_style, vertical_style)
        vertical_fields = vertical_style.split(",")
        self.assertEqual(int(vertical_fields[21]), 192)

    def test_rejects_caption_that_needs_more_than_two_lines(self) -> None:
        cue = (
            CaptionCue(
                0,
                2,
                "This deliberately long sentence contains far too many words "
                "for the configured width and must be split into suitable cues",
            ),
        )
        with self.assertRaisesRegex(CaptionInputError, "exceeds two lines"):
            build_ass(cue, width=1920, height=1080, layout="landscape")

    def test_rejects_invalid_timestamps_and_unsupported_schema(self) -> None:
        self.caption_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "captions": [{"start": 2, "end": 1, "text": "Bad time"}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(CaptionInputError):
            load_caption_cues(self.caption_json)

    def test_render_command_uses_new_file_and_does_not_overwrite(self) -> None:
        ass = self.root / "project" / "captions.ass"
        output = self.root / "project" / "caption_preview.mp4"
        command = build_render_command(self.ffmpeg, self.video, ass, output)

        self.assertIn("-n", command)
        threads = str(render_threads())
        self.assertEqual(command[command.index("-threads") + 1], threads)
        self.assertEqual(command[command.index("-threads:v") + 1], threads)
        self.assertEqual(command[command.index("-filter_threads") + 1], threads)
        self.assertIn(str(self.video.resolve()), command)
        self.assertEqual(command[-1], str(output))
        self.assertIn("ass=filename=", command[command.index("-vf") + 1])
        self.assertIn("fontsdir=", command[command.index("-vf") + 1])
        self.assertIn("0:a?", command)
        self.assertEqual(self.video.read_bytes(), b"source-video")

    def test_render_threads_default_and_override(self) -> None:
        with patch.dict("os.environ", {"NAMAKADHAI_RENDER_THREADS": ""}), \
                patch("os.cpu_count", return_value=8):
            self.assertEqual(render_threads(), 4)
        with patch.dict("os.environ", {"NAMAKADHAI_RENDER_THREADS": ""}), \
                patch("os.cpu_count", return_value=2):
            self.assertEqual(render_threads(), 1)
        with patch.dict("os.environ", {"NAMAKADHAI_RENDER_THREADS": "1"}):
            self.assertEqual(render_threads(), 1)

    @patch("namakadhai.caption_renderer.subprocess.run")
    @patch("namakadhai.caption_renderer.preflight_media")
    def test_renders_to_recording_project_folder_without_touching_input(
        self, preflight, run
    ) -> None:
        from namakadhai.media_review import ReviewPaths

        project = self.root / "projects" / "recording"
        paths = ReviewPaths(
            project_directory=project,
            report=project / "media_review.md",
            contact_sheet=project / "visual_review" / "contact_sheet.jpg",
        )
        preflight.return_value = (self.metadata, paths)

        def finish_render(command, **kwargs):
            self.assertFalse(kwargs["shell"])
            Path(command[-1]).write_bytes(b"captioned preview")
            return CompletedProcess(command, 0, "", "")

        run.side_effect = finish_render
        output = render_caption_preview(
            self.video,
            self.caption_json,
            settings_path=self.settings,
        )

        self.assertTrue(output.ass_file.is_file())
        self.assertTrue(output.video_file.is_file())
        self.assertEqual(output.video_file.read_bytes(), b"captioned preview")
        self.assertEqual(self.video.read_bytes(), b"source-video")
        self.assertFalse(run.call_args.kwargs["shell"])

    @patch("namakadhai.caption_renderer.subprocess.run")
    @patch("namakadhai.caption_renderer.preflight_media")
    def test_refuses_to_overwrite_existing_caption_outputs(self, preflight, run):
        from namakadhai.media_review import ReviewPaths

        project = self.root / "projects" / "recording"
        paths = ReviewPaths(project, project / "media_review.md", project / "sheet.jpg")
        preflight.return_value = (self.metadata, paths)
        caption_dir = project / "captions"
        caption_dir.mkdir(parents=True)
        existing = caption_dir / "captions.ass"
        existing.write_text("preserve", encoding="utf-8")

        with self.assertRaises(CaptionOutputExistsError):
            render_caption_preview(
                self.video,
                self.caption_json,
                settings_path=self.settings,
            )
        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")
        run.assert_not_called()

    @patch("namakadhai.caption_renderer.subprocess.run")
    @patch("namakadhai.caption_renderer.preflight_media")
    def test_ffmpeg_render_failure_is_not_reported_as_success(self, preflight, run):
        from namakadhai.media_review import ReviewPaths

        project = self.root / "projects" / "recording"
        preflight.return_value = (
            self.metadata,
            ReviewPaths(project, project / "report", project / "sheet"),
        )
        def partial_output(command, **kwargs):
            self.assertFalse(kwargs["shell"])
            Path(command[-1]).write_bytes(b"partial")
            return CompletedProcess(command, 1, "", "render failed")

        run.side_effect = partial_output

        with self.assertRaisesRegex(CaptionRenderError, "did not produce"):
            render_caption_preview(
                self.video,
                self.caption_json,
                settings_path=self.settings,
            )
        self.assertFalse((project / "captions" / "caption_preview.mp4").exists())


if __name__ == "__main__":
    unittest.main()
