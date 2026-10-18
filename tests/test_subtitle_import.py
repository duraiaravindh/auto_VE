import tempfile
import unittest
from pathlib import Path

from namakadhai.subtitle_import import (
    SubtitleImportError,
    align_cues_to_speech_activity,
    detect_speech_activity,
    import_reviewed_srt,
    parse_srt_time,
)


class SubtitleImportTests(unittest.TestCase):
    def test_parses_standard_and_legacy_timestamps(self):
        self.assertEqual(parse_srt_time("00:00:01,800"), 1.8)
        self.assertEqual(parse_srt_time("01:01:500"), 61.5)

    def test_preserves_text_and_explicitly_scales_long_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "captions.srt"
            output = root / "captions.json"
            source.write_text(
                "1\n00:00:000 --> 00:05:000\nExact Tanglish\n\n"
                "2\n00:05:000 --> 00:10:000\nSecond line\n",
                encoding="utf-8",
            )

            payload = import_reviewed_srt(
                source, output, video_duration_seconds=8.0, fit_to_video=True, tail_seconds=0.0
            )

            self.assertEqual(payload["source"]["timing_scale"], 0.8)
            self.assertEqual(payload["captions"][0]["text"], "Exact Tanglish")
            self.assertEqual(payload["captions"][-1]["end"], 8.0)

    def test_speech_alignment_skips_silent_intro_and_cuts_in_pauses(self):
        # 3 s silence, a click, 2 s silence, then three phrases split by pauses.
        levels = [-45.0] * 300 + [-20.0] * 15 + [-45.0] * 200
        for length in (120, 240, 120):
            levels += [-10.0] * length + [-45.0] * 40
        active = detect_speech_activity(levels)
        cues = [
            {"start": 0.0, "end": 2.0, "text": "oru rendu"},
            {"start": 2.0, "end": 4.0, "text": "moonu naalu anju aaru"},
            {"start": 4.0, "end": 6.0, "text": "ezhu ettu"},
        ]

        aligned, report = align_cues_to_speech_activity(
            cues, active, video_duration_seconds=len(levels) / 100, levels=levels
        )

        self.assertAlmostEqual(aligned[0]["start"], 5.07, places=2)
        self.assertEqual([cue["alignment"] for cue in aligned], ["speech_pause"] * 3)
        self.assertAlmostEqual(aligned[1]["start"], 6.67, places=2)
        self.assertAlmostEqual(aligned[2]["start"], 9.47, places=2)
        self.assertEqual([cue["text"] for cue in aligned], [cue["text"] for cue in cues])
        self.assertEqual(report["pause_boundary_count"], 2)
        for previous, current in zip(aligned, aligned[1:]):
            self.assertLessEqual(previous["end"], current["start"])

    def test_refuses_implicit_retiming(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "captions.srt"
            source.write_text("1\n00:00:000 --> 00:10:000\nWords\n", encoding="utf-8")
            with self.assertRaisesRegex(SubtitleImportError, "after the"):
                import_reviewed_srt(
                    source,
                    Path(directory) / "captions.json",
                    video_duration_seconds=5.0,
                )


if __name__ == "__main__":
    unittest.main()

