import unittest

from namakadhai.forced_align import _spoken_tokens, build_aligned_captions


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


if __name__ == "__main__":
    unittest.main()
