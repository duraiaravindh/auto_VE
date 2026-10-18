import json
import tempfile
import unittest
from pathlib import Path

from namakadhai.auto_edit import make_caption_cues, suggest_transcript_cuts, transliterate_tamil


class AutoEditTests(unittest.TestCase):
    def test_tamil_is_transliterated_deterministically_without_changing_latin_text(self):
        self.assertEqual(transliterate_tamil("தமிழ் கதை"), "thamizh kathai")
        self.assertEqual(transliterate_tamil("Nama Kadhai"), "Nama Kadhai")

    def test_word_timed_cues_flag_low_confidence(self):
        words = [
            {"original": "ஒரு", "text": "oru", "start": 0.1, "end": 0.5, "confidence": 0.9, "verify": False},
            {"original": "கதை", "text": "kathai", "start": 0.55, "end": 1.0, "confidence": 0.2, "verify": True},
        ]
        cues = make_caption_cues(words)

        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "oru kathai")
        self.assertTrue(cues[0]["uncertain"])
        self.assertEqual(cues[0]["verification_required_words"], ["kathai"])
        self.assertEqual(cues[0]["words"][1]["start"], 0.55)

    def test_filler_and_repeat_rules_create_bounded_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.json"
            transcript.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "words": [
                                    {"text": "um", "original": "um", "start": 1.0, "end": 1.2, "confidence": 0.8},
                                    {"text": "naan", "original": "நான்", "start": 2.0, "end": 2.3, "confidence": 0.9},
                                    {"text": "naan", "original": "நான்", "start": 2.4, "end": 2.7, "confidence": 0.9},
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            candidates = suggest_transcript_cuts(transcript, duration_seconds=5.0)

        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(item["candidate_id"].startswith("auto-") for item in candidates))
        self.assertTrue(all(item["transition_kind"] == "cut" for item in candidates))
        self.assertIn("intentional emphasis", candidates[1]["reason"])


if __name__ == "__main__":
    unittest.main()

