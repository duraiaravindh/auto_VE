import unittest

from namakadhai.video_analysis import _parse


class VideoAnalysisTests(unittest.TestCase):
    def test_parses_signal_candidates_without_claiming_story_understanding(self):
        stderr = "\n".join(
            (
                "black_start:1.0 black_end:2.0 black_duration:1.0",
                "freeze_start: 3.0",
                "freeze_end: 5.5 | freeze_duration: 2.5",
                "[Parsed_showinfo_2] pts_time:4.25",
            )
        )
        stdout = "lavfi.signalstats.YAVG=30\nlavfi.signalstats.YAVG=50\n"

        result = _parse(stderr, stdout, 10.0)

        self.assertEqual(result["black_intervals"][0]["duration_seconds"], 1.0)
        self.assertEqual(result["frozen_intervals"][0]["start_seconds"], 3.0)
        self.assertEqual(result["brightness"]["average_y"], 40.0)
        self.assertIn(4.25, result["scene_change_times_seconds"])
        self.assertIn("human review", result["interpretation"])


if __name__ == "__main__":
    unittest.main()

