import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from namakadhai.publishing import PublishingError, create_publishing_package


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.exports = self.run / "exports"
        self.exports.mkdir(parents=True)
        self.captions = self.root / "captions.json"
        self.captions.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reviewed": True,
                    "captions": [
                        {"start": 0.1, "end": 1.0, "text": "Exact Tanglish"},
                        {"start": 1.1, "end": 1.8, "text": "[VERIFY] word", "uncertain": True},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.plan = self.run / "story_edit.json"
        self.plan.write_text(
            json.dumps(
                {
                    "source": {"duration_seconds": 4.0},
                    "title": {"text": "Home story"},
                    "captions": {"path": str(self.captions), "sha256": digest(self.captions)},
                }
            ),
            encoding="utf-8",
        )
        for name in ("final_landscape.mp4", "final_vertical.mp4"):
            (self.exports / name).write_bytes((name + " bytes").encode())
        output_records = {}
        for format_name in ("landscape", "vertical"):
            final = self.exports / f"final_{format_name}.mp4"
            report = self.exports / f"review_{format_name}.render-report.json"
            report.write_text(
                json.dumps(
                    {
                        "validated": True,
                        "format": format_name,
                        "output_sha256": digest(final),
                    }
                ),
                encoding="utf-8",
            )
            output_records[format_name] = {
                "preview_sha256": digest(final),
                "report_sha256": digest(report),
            }
        (self.exports / "final_approval.json").write_text(
            json.dumps(
                {
                    "plan_sha256": digest(self.plan),
                    "outputs": output_records,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    @patch("namakadhai.publishing._resolve_ffmpeg", return_value=Path("ffmpeg"))
    @patch("namakadhai.publishing._thumbnail")
    def test_package_contains_two_formats_captions_thumbnails_and_manifest(self, thumbnail, _resolve):
        thumbnail.side_effect = lambda _video, destination, **_kwargs: destination.write_bytes(b"jpeg")

        result = create_publishing_package(self.plan, project_name="My Story")

        package = Path(result["directory"])
        self.assertTrue((package / "my-story_youtube.mp4").is_file())
        self.assertTrue((package / "my-story_reels-shorts.mp4").is_file())
        self.assertTrue((package / "review_landscape.render-report.json").is_file())
        self.assertTrue((package / "review_vertical.render-report.json").is_file())
        self.assertIn("Exact Tanglish", (package / "reviewed_captions.srt").read_text(encoding="utf-8"))
        self.assertNotIn("VERIFY", (package / "reviewed_captions.srt").read_text(encoding="utf-8"))
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["publishing_action"], "LOCAL_PACKAGE_ONLY")
        self.assertEqual(thumbnail.call_count, 3)

    def test_package_requires_matching_final_approval(self):
        (self.exports / "final_approval.json").write_text(
            json.dumps({"plan_sha256": "stale", "outputs": {}}), encoding="utf-8"
        )
        with self.assertRaisesRegex(PublishingError, "does not match"):
            create_publishing_package(self.plan, project_name="My Story")

    def test_package_rejects_final_output_changed_after_approval(self):
        (self.exports / "final_landscape.mp4").write_bytes(b"changed after approval")

        with self.assertRaisesRegex(PublishingError, "changed after approval"):
            create_publishing_package(self.plan, project_name="My Story")

    def test_package_rejects_render_report_changed_after_approval(self):
        (self.exports / "review_vertical.render-report.json").write_text(
            '{"validated": false}', encoding="utf-8"
        )

        with self.assertRaisesRegex(PublishingError, "report changed after approval"):
            create_publishing_package(self.plan, project_name="My Story")


if __name__ == "__main__":
    unittest.main()
