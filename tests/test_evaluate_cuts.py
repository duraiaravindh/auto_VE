import unittest

from namakadhai.evaluate_cuts import evaluate, parse_time


class EvaluateCutsTests(unittest.TestCase):
    def test_counts_caught_missed_and_false_cuts(self):
        marks = [(10.0, 14.0, "retake"), (30.0, 30.5, "filler"), (60.0, 61.2, "pause")]
        cuts = [
            {"kind": "retake", "proposed_cut_seconds": [10.2, 14.0]},
            {"kind": "repeat", "proposed_cut_seconds": [50.0, 51.0]},
            {"kind": "pause", "proposed_cut_seconds": [60.0, 61.0]},
        ]

        result = evaluate(cuts, marks)

        self.assertEqual([m["label"] for m in result["caught"]], ["retake", "pause"])
        self.assertEqual(result["missed"][0]["label"], "filler")
        self.assertEqual([c["kind"] for c in result["correct_cuts"]], ["retake"])
        self.assertEqual([c["kind"] for c in result["false_cuts"]], ["repeat"])
        self.assertEqual(result["pauses_shortened"], 1)

    def test_time_formats(self):
        self.assertEqual(parse_time("1:02.5"), 62.5)
        self.assertEqual(parse_time("7.25"), 7.25)


if __name__ == "__main__":
    unittest.main()
