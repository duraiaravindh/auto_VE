import unittest

from namakadhai.forced_align import _spoken_tokens, build_aligned_captions, select_spoken_cues


class ForcedAlignTests(unittest.TestCase):
    def test_tokens_are_lowercase_letters_and_spelled_digits(self):
        self.assertEqual(_spoken_tokens("fun-aana"), ["fun", "aana"])
        self.assertEqual(_spoken_tokens("1"), ["one"])
        self.assertEqual(_spoken_tokens("TV-la,"), ["tv", "la"])

    def test_cues_start_on_first_spoken_word_and_never_overlap(self):
        cues = [{"text": "Naama chinna"}, {"text": "vayasilu indha"}]
        timings = [(5.6, 5.9, 0.9), (5.95, 6.4, 0.8), (6.5, 6.9, 0.7), (7.0, 7.3, 0.2)]

        aligned, report = build_aligned_captions(cues, timings, video_duration_seconds=10.0)

        self.assertEqual([cue["start"] for cue in aligned], [5.45, 6.35])
        self.assertEqual(aligned[0]["end"], 6.35)
        self.assertEqual(aligned[1]["end"], 7.7)
        self.assertEqual([cue["text"] for cue in aligned], ["Naama chinna", "vayasilu indha"])
        self.assertEqual(aligned[1]["words"][0]["start"], 6.5)
        self.assertEqual(report["low_confidence_words"], ["indha @ 7.00s"])

    def test_lines_missing_from_the_audio_are_dropped_one_at_a_time(self):
        # Line 2 was cut from the video; while it is present it drags line 1 down.
        def score_for(kept):
            return [0.05 if i == 2 else (0.2 if i == 1 and 2 in kept else 0.7) for i in kept]

        self.assertEqual(select_spoken_cues(4, score_for), [0, 1, 3])

    def test_cut_lines_are_reported_not_captioned(self):
        cues = [{"text": "Naama chinna"}, {"text": "cut line"}]
        timings = [(5.6, 5.9, 0.9), (5.95, 6.4, 0.8), None, None]

        aligned, report = build_aligned_captions(cues, timings, video_duration_seconds=10.0)

        self.assertEqual([cue["text"] for cue in aligned], ["Naama chinna"])
        self.assertEqual(report["not_in_audio"], ["cut line"])

    def test_long_two_line_cue_is_split_at_the_pause_keeping_every_word(self):
        text = "vera vera padatha ore TV-la ore time-la eppadi paakaradhudhanga"
        starts = [16.9, 17.2, 17.4, 17.9, 18.4, 18.7, 19.0, 19.3, 19.6]
        starts[4:] = [s + 0.5 for s in starts[4:]]  # a pause before "TV-la"
        timings = [(s, s + 0.25, 0.8) for s in starts]

        aligned, report = build_aligned_captions([{"text": text}], timings, video_duration_seconds=30.0)

        self.assertEqual([c["text"] for c in aligned],
                         ["vera vera padatha ore", "TV-la ore time-la eppadi", "paakaradhudhanga"])
        self.assertEqual(" ".join(c["text"] for c in aligned), text)
        self.assertTrue(all(len(c["text"]) <= 32 for c in aligned))
        self.assertEqual(report["long_captions_split"], 1)
        for previous, current in zip(aligned, aligned[1:]):
            self.assertLessEqual(previous["end"], current["start"])


if __name__ == "__main__":
    unittest.main()
