import unittest
from pathlib import Path
from unittest.mock import patch

from namakadhai.caption_pipeline import build_join_command
from namakadhai.remotion_cards import RemotionCardError, render_card, validate_card_props


class RemotionCardTests(unittest.TestCase):
    def test_props_are_plain_validated_data(self):
        props = validate_card_props(title=" Remote Sandai ", subtitle="Nama Kadhai",
                                    width=1080, height=1920, fps=29.97)
        self.assertEqual(props, {"title": "Remote Sandai", "subtitle": "Nama Kadhai",
                                 "durationSeconds": 3.0, "width": 1080, "height": 1920, "fps": 30})
        for bad in ({"title": ""}, {"title": "x" * 81}, {"width": 1081},
                    {"duration_seconds": 30.0}, {"fps": 120}):
            arguments = {"title": "ok", "width": 1080, "height": 1920, "fps": 30, **bad}
            with self.assertRaises(RemotionCardError):
                validate_card_props(**arguments)

    def test_render_refuses_unknown_card_and_overwrite(self):
        with self.assertRaises(RemotionCardError):
            render_card("Anything", {}, Path("x.mp4"))
        with self.assertRaisesRegex(RemotionCardError, "overwrite"):
            render_card("TitleCard", {}, Path(__file__))

    def test_join_adds_silence_for_cards_without_sound(self):
        with patch("namakadhai.caption_pipeline._has_audio", side_effect=[False, True]), \
                patch("namakadhai.caption_pipeline._duration", return_value=3.0):
            command = build_join_command(Path("ffmpeg.exe"), [Path("card.mp4"), Path("video.mp4")],
                                         Path("out.mp4"), 1080, 1920, 30)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("anullsrc=r=48000:cl=stereo,atrim=duration=3.000[a0]", graph)
        self.assertIn("[1:a:0]aresample=48000", graph)
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]", graph)
        self.assertIn("-n", command)


if __name__ == "__main__":
    unittest.main()
