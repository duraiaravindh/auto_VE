import unittest

try:
    import numpy as np
except ImportError:  # the core app runs without numpy; this feature needs it
    np = None

if np is not None:
    from namakadhai.edit_matcher import Segment, _drop_outliers, map_words, match_envelopes


@unittest.skipIf(np is None, "numpy is only installed in the alignment environment")
class EditMatcherTests(unittest.TestCase):
    def setUp(self):
        # A speech-like 10 ms envelope: 60 s of random syllable energy.
        rng = np.random.default_rng(7)
        self.original = np.log10(rng.gamma(0.6, 1.0, 6000) + 1e-3)

    def test_finds_a_removed_section_and_the_cut_point(self):
        edited = np.concatenate([self.original[:2000], self.original[3000:]])

        segments = match_envelopes(self.original, edited)

        self.assertEqual(
            segments,
            [Segment(0.0, 20.0, 0.0), Segment(20.0, 50.0, 10.0)],
        )

    def test_reordered_parts_map_back(self):
        edited = np.concatenate([self.original[3000:4500], self.original[0:1500]])

        segments = match_envelopes(self.original, edited)

        self.assertEqual([round(s.offset, 2) for s in segments], [30.0, -15.0])
        self.assertAlmostEqual(segments[0].edit_end, 15.0, places=2)

    def test_words_move_with_their_part_and_cut_words_drop(self):
        segments = [Segment(0.0, 20.0, 0.0), Segment(20.0, 50.0, 10.0)]

        mapped = map_words([(5.0, 5.4), (25.0, 25.3), (31.0, 31.2), (29.9, 30.4)], segments)

        self.assertEqual(mapped[0], (5.0, 5.4))
        self.assertIsNone(mapped[1])
        self.assertAlmostEqual(mapped[2][0], 21.0)
        self.assertIsNone(mapped[3])

    def test_short_backward_jump_between_ordered_parts_is_dropped(self):
        # (edited start frame, offset): the middle window claims audio from long before.
        groups = [[(0, 0), (50, 0)], [(150, -1200)], [(200, 30), (250, 30)]]

        self.assertEqual(_drop_outliers(groups), [groups[0], groups[2]])

    def test_long_reused_part_is_kept(self):
        groups = [[(0, 0), (50, 0)], [(150, -100), (200, -100), (250, -100)], [(400, 30)]]

        self.assertEqual(_drop_outliers(groups), groups)


if __name__ == "__main__":
    unittest.main()
