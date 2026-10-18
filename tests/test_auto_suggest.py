import unittest

from namakadhai.auto_suggest import Word, suggest_cuts


def line(text: str, start: float, step: float = 0.3) -> list[Word]:
    return [Word(token, start + i * step, start + i * step + 0.25) for i, token in enumerate(text.split())]


class AutoSuggestTests(unittest.TestCase):
    def kinds(self, cuts):
        return [(cut["kind"], cut["confidence"], cut["default_decision"]) for cut in cuts]

    def test_identical_retake_cuts_the_earlier_take(self):
        lines = [line("naan Durai idhu Namma Kadhai", 1.0), line("naan Durai idhu Namma Kadhai", 3.0)]

        cuts = suggest_cuts(lines)

        self.assertEqual(self.kinds(cuts), [("retake", "high", "APPROVED")])
        self.assertEqual(cuts[0]["proposed_cut_seconds"], [1.0, 3.0])

    def test_false_start_is_a_retake_even_with_spelling_differences(self):
        lines = [line("naan Durai idhu", 1.0), line("naan Dhurai idhu Namma Kadhai", 2.5)]

        self.assertEqual(self.kinds(suggest_cuts(lines)), [("retake", "high", "APPROVED")])

    def test_parallel_phrasing_is_never_pre_ticked(self):
        lines = [line("oru padatha paathutu iruppom", 1.0), line("innoru padatha paathutu iruppom", 2.5)]

        self.assertEqual(self.kinds(suggest_cuts(lines)), [("retake", "medium", "REJECTED")])

    def test_unrelated_lines_are_left_alone(self):
        lines = [line("Naama chinna vayasilu indha", 1.0), line("maadhiri edhavadhu oru festival", 2.5)]

        self.assertEqual(suggest_cuts(lines), [])

    def test_tamil_word_doubling_is_low_and_a_phrase_stutter_is_high(self):
        doubling = suggest_cuts([line("vera vera padatha", 1.0)])
        stutter = suggest_cuts([line("andha channel la andha channel la ad vandhadhum", 1.0)])

        self.assertEqual(self.kinds(doubling), [("repeat", "low", "REJECTED")])
        self.assertEqual(self.kinds(stutter), [("repeat", "high", "APPROVED")])
        self.assertEqual(stutter[0]["proposed_cut_seconds"], [1.0, 1.9])

    def test_fillers_pure_sounds_high_and_real_words_low(self):
        cuts = suggest_cuts([line("umm naan vandhu sonnen", 1.0)])

        self.assertEqual(
            [(cut["evidence"]["word"], cut["confidence"]) for cut in cuts],
            [("umm", "high"), ("vandhu", "low")],
        )

    def test_long_gap_between_words_is_shortened_not_removed(self):
        words = [Word("andha", 1.0, 1.3), Word("feel", 3.3, 3.6), Word("ku", 4.2, 4.4)]

        cuts = suggest_cuts([words])

        self.assertEqual(self.kinds(cuts), [("pause", "high", "APPROVED")])
        start, end = cuts[0]["proposed_cut_seconds"]
        self.assertAlmostEqual(start, 1.55)
        self.assertAlmostEqual(end, 3.1)
        self.assertAlmostEqual((3.3 - 1.3) - (end - start), 0.45)


if __name__ == "__main__":
    unittest.main()
