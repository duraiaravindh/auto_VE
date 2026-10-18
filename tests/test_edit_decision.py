from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from namakadhai.audio_analysis import (
    AudioAnalysisOutputExistsError,
    AudioAnalysisResult,
    AudioLevels,
    QuietInterval,
)
from namakadhai.edit_decision import (
    EditDecisionOutputExistsError,
    _audio_issues,
    _render_edit_decision_list,
    create_edit_decision_list,
)
from namakadhai.media_review import ReviewPaths
from namakadhai.probe import (
    AudioStreamMetadata,
    VideoMetadata,
    VideoStreamMetadata,
)


class EditDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.video = self.root / "story.mp4"
        self.video.write_bytes(b"original")
        self.project = self.root / "projects" / "story"
        self.paths = ReviewPaths(
            project_directory=self.project,
            report=self.project / "media_review.md",
            contact_sheet=self.project / "visual_review" / "contact_sheet.jpg",
        )
        self.metadata = VideoMetadata(
            file_name=self.video.name,
            file_size_bytes=self.video.stat().st_size,
            duration_seconds=20,
            format_name="mp4",
            format_long_name="MPEG-4",
            video=VideoStreamMetadata("h264", 1280, 720, 30),
            audio=AudioStreamMetadata("aac", 48000, 2),
        )
        self.analysis = AudioAnalysisResult(
            report_path=self.project / "edit_review" / "audio_analysis.md",
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(QuietInterval(2, 5, 3),),
            levels=AudioLevels(-15, -2),
            warnings=(),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_edl_proposes_padded_silence_cut_for_review_only(self) -> None:
        report = _render_edit_decision_list(
            self.analysis,
            file_name=self.video.name,
            duration_seconds=20,
        )

        self.assertIn("2.000–5.000", report)
        self.assertIn("2.250–4.750", report)
        self.assertIn("[ ] Approve [ ] Reject", report)
        self.assertIn("No cuts, audio processing, or video rendering were applied", report)

    def test_short_pause_has_no_proposed_removal(self) -> None:
        analysis = AudioAnalysisResult(
            report_path=self.analysis.report_path,
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(QuietInterval(2, 2.4, 0.4),),
            levels=AudioLevels(-15, -2),
            warnings=(),
        )

        report = _render_edit_decision_list(
            analysis,
            file_name=self.video.name,
            duration_seconds=20,
        )

        self.assertIn("None (too short after padding)", report)

    def test_audio_level_issues_are_signal_level_warnings(self) -> None:
        analysis = AudioAnalysisResult(
            report_path=self.analysis.report_path,
            has_audio=True,
            threshold_db=-35,
            minimum_duration_seconds=1,
            quiet_intervals=(),
            levels=AudioLevels(-32, -0.5),
            warnings=(),
        )

        issues = _audio_issues(analysis)

        self.assertEqual(len(issues), 2)
        self.assertIn("Low overall level", issues[0])
        self.assertIn("near full scale", issues[1])

    @patch("namakadhai.edit_decision.analyze_audio")
    @patch("namakadhai.edit_decision.preflight_media")
    def test_creates_separate_review_files_without_modifying_source(
        self, preflight, analyze
    ) -> None:
        preflight.return_value = self.metadata, self.paths

        def write_audio_report(*args, **kwargs):
            report_path = kwargs["report_path"]
            report_path.parent.mkdir(parents=True)
            report_path.write_text("audio report", encoding="utf-8")
            return self.analysis

        analyze.side_effect = write_audio_report

        result = create_edit_decision_list(self.video)

        self.assertTrue(result.report_path.is_file())
        self.assertTrue(self.analysis.report_path.is_file())
        self.assertEqual(self.video.read_bytes(), b"original")
        self.assertEqual(analyze.call_args.kwargs["report_path"], self.analysis.report_path)

    @patch("namakadhai.edit_decision.analyze_audio")
    @patch("namakadhai.edit_decision.preflight_media")
    def test_existing_edl_is_not_overwritten(self, preflight, analyze) -> None:
        preflight.return_value = self.metadata, self.paths
        output = self.project / "edit_review" / "edit_decision_list.md"
        output.parent.mkdir(parents=True)
        output.write_text("keep this", encoding="utf-8")

        with self.assertRaises(EditDecisionOutputExistsError):
            create_edit_decision_list(self.video)

        self.assertEqual(output.read_text(encoding="utf-8"), "keep this")
        analyze.assert_not_called()

    @patch("namakadhai.edit_decision.analyze_audio")
    @patch("namakadhai.edit_decision.preflight_media")
    def test_existing_analysis_is_not_overwritten(self, preflight, analyze) -> None:
        preflight.return_value = self.metadata, self.paths
        output = self.project / "edit_review" / "audio_analysis.md"
        output.parent.mkdir(parents=True)
        output.write_text("keep this", encoding="utf-8")

        with self.assertRaises(AudioAnalysisOutputExistsError):
            create_edit_decision_list(self.video)

        self.assertEqual(output.read_text(encoding="utf-8"), "keep this")
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
