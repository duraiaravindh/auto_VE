"""One-step captioning: align an SRT to a video's speech and render a preview.

Run with the alignment environment (it needs PyTorch):

    .venv-align\\Scripts\\python.exe -m namakadhai.caption_pipeline VIDEO SRT

Only the SRT's text and line grouping are used; its timestamps are replaced by
word-level timings measured from the audio. Nothing existing is overwritten.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from namakadhai.caption_renderer import (
    CaptionError,
    build_ass,
    build_render_command,
    load_caption_cues,
)
from namakadhai.forced_align import PROJECT_ROOT, import_forced_aligned_srt
from namakadhai.media_review import MediaReviewError, _resolve_ffmpeg, preflight_media
from namakadhai.story_editor import load_creative_profile
from namakadhai.subtitle_import import SubtitleImportError

DEFAULT_PROFILE = PROJECT_ROOT / "config" / "nama_kadhai_nostalgic.json"
OUTPUT_ROOT = PROJECT_ROOT / "projects" / "captioned"


def caption_video(
    video: Path,
    srt: Path,
    *,
    output_root: Path = OUTPUT_ROOT,
    profile_path: Path = DEFAULT_PROFILE,
    layout: str | None = None,
) -> dict[str, Path]:
    metadata, _ = preflight_media(video)
    width, height = metadata.video.width, metadata.video.height
    if width is None or height is None or metadata.duration_seconds is None:
        raise SubtitleImportError("FFprobe did not report the video size and duration.")
    ffmpeg = _resolve_ffmpeg(None)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = output_root / f"{video.stem}-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)

    print("1/3 Aligning caption words to the speech...", flush=True)
    captions = output_dir / "captions.json"
    payload = import_forced_aligned_srt(
        srt, video, captions,
        ffmpeg_path=ffmpeg, video_duration_seconds=metadata.duration_seconds,
    )

    print("2/3 Writing styled subtitles...", flush=True)
    style = load_creative_profile(profile_path)[0].caption_style
    chosen_layout = layout or ("vertical" if height > width else "landscape")
    ass = output_dir / "captions.ass"
    with ass.open("x", encoding="utf-8", newline="") as ass_file:
        ass_file.write(
            build_ass(
                load_caption_cues(captions), width=width, height=height,
                layout=chosen_layout, style=style,
            )
        )

    print("3/3 Rendering the captioned video...", flush=True)
    preview = output_dir / f"{video.stem}_captioned.mp4"
    result = subprocess.run(
        build_render_command(ffmpeg, video, ass, preview),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=3600, shell=False,
    )
    if result.returncode != 0 or not preview.is_file():
        raise CaptionError("FFmpeg could not render the captioned video. " + result.stderr[-400:])

    report = payload["source"]["alignment"]
    (output_dir / "alignment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"folder": output_dir, "captions": captions, "ass": ass, "video": preview,
            "report": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Align SRT captions to a video's speech and render the captioned video."
    )
    parser.add_argument("video", type=Path, help="Source video; it is only read.")
    parser.add_argument("srt", type=Path, help="SRT whose text matches what is spoken.")
    parser.add_argument("--layout", choices=("vertical", "landscape"),
                        help="Default: picked from the video's orientation.")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                        help="Creative profile with the caption style.")
    arguments = parser.parse_args(argv)
    try:
        outputs = caption_video(
            arguments.video.resolve(), arguments.srt.resolve(),
            profile_path=arguments.profile, layout=arguments.layout,
        )
    except (SubtitleImportError, CaptionError, MediaReviewError, FileNotFoundError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    report = outputs["report"]
    print(f"\nDone: {outputs['video']}")
    print(f"Aligned {report['aligned_word_count']} words, "
          f"mean confidence {report['mean_word_confidence']:.2f}.")
    if report["low_confidence_words"]:
        print("Check these spots (text may not match the speech): "
              + ", ".join(report["low_confidence_words"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
