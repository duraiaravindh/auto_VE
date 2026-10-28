"""Burn word-timed captions from an SRT into a video, in one step.

Run with the alignment environment (it needs PyTorch):

    .venv-align\\Scripts\\python.exe -m namakadhai.caption_pipeline VIDEO SRT

Only the SRT's text is used; every word is timed against the audio. The
video keeps its size and length; nothing existing is overwritten.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from namakadhai.caption_renderer import (
    CaptionError,
    CaptionStyle,
    build_ass,
    build_render_command,
    load_caption_cues,
)
from namakadhai.forced_align import PROJECT_ROOT, import_forced_aligned_srt
from namakadhai.media_review import MediaReviewError, _resolve_ffmpeg, preflight_media
from namakadhai.srt_cleanup import clean_srt
from namakadhai.subtitle_import import (
    SubtitleImportError,
    detect_speech_activity,
    measure_speech_envelope,
)

DEFAULT_STYLE = PROJECT_ROOT / "config" / "caption_style.json"
OUTPUT_ROOT = PROJECT_ROOT / "projects" / "captioned"


def load_caption_style(path: Path = DEFAULT_STYLE) -> CaptionStyle:
    """Caption look from a JSON file; unknown keys are ignored."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptionError(f"Could not read caption style: {Path(path).name}") from exc
    payload = payload.get("caption", payload)
    known = {field.name for field in dataclasses.fields(CaptionStyle)}
    return CaptionStyle(**{key: value for key, value in payload.items() if key in known})


def caption_video(
    video: Path,
    srt: Path,
    *,
    output_root: Path = OUTPUT_ROOT,
    style_path: Path = DEFAULT_STYLE,
    layout: str | None = None,
    timed_captions: Path | None = None,
) -> dict:
    """Caption ``video``. With ``timed_captions`` (checked in the studio), those
    exact timings are burned in and the SRT is not re-timed."""
    metadata, _ = preflight_media(video)
    width, height = metadata.video.width, metadata.video.height
    if width is None or height is None or metadata.duration_seconds is None:
        raise SubtitleImportError("FFprobe did not report the video size and duration.")
    style = load_caption_style(style_path)
    ffmpeg = _resolve_ffmpeg(None)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = output_root / f"{video.stem}-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)

    captions = output_dir / "captions.json"
    if timed_captions is not None:
        print("1/3 Using your checked caption timings...", flush=True)
        payload = json.loads(Path(timed_captions).read_text(encoding="utf-8"))
        with captions.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        cleanup = {}
    else:
        cleaned = output_dir / "subtitles_cleaned.srt"
        cleanup = clean_srt(srt, cleaned)
        print("1/3 Aligning caption words to the speech...", flush=True)
        payload = import_forced_aligned_srt(
            cleaned, video, captions,
            ffmpeg_path=ffmpeg, video_duration_seconds=metadata.duration_seconds,
        )

    print("2/3 Writing styled subtitles...", flush=True)
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
    output = output_dir / f"{video.stem}_captioned.mp4"
    result = subprocess.run(
        build_render_command(ffmpeg, video, ass, output),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=3600, shell=False,
    )
    if result.returncode != 0 or not output.is_file():
        raise CaptionError("FFmpeg could not render the captioned video. " + result.stderr[-400:])

    report = dict(payload["source"]["alignment"])
    report["srt_cleanup"] = cleanup
    report["uncaptioned_speech"] = _uncaptioned_speech(
        video, ffmpeg, payload["captions"], metadata.duration_seconds
    )
    (output_dir / "alignment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"folder": output_dir, "captions": captions, "ass": ass, "video": output,
            "report": report}


def _uncaptioned_speech(media: Path, ffmpeg: Path, captions: list, duration: float) -> list:
    """Stretches of speech (>= 2 s) after the last caption or in long caption gaps."""
    active = detect_speech_activity(measure_speech_envelope(media, ffmpeg))
    gaps, previous_end = [], 0.0
    for start, end in [(c["start"], c["end"]) for c in captions] + [(duration, duration)]:
        if start - previous_end >= 2.0:
            speech = sum(active[int(previous_end * 100):int(start * 100)]) / 100
            if speech >= 2.0:
                gaps.append({"from_seconds": round(previous_end, 1), "to_seconds": round(start, 1),
                             "speech_seconds": round(speech, 1)})
        previous_end = max(previous_end, end)
    return gaps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Burn word-timed captions from an SRT into a video."
    )
    parser.add_argument("video", type=Path, help="Video; it is only read.")
    parser.add_argument("srt", type=Path, help="SRT whose text matches what is spoken.")
    parser.add_argument("--layout", choices=("vertical", "landscape"),
                        help="Default: picked from the video's orientation.")
    parser.add_argument("--style", type=Path, default=DEFAULT_STYLE,
                        help="Caption style JSON (default: config/caption_style.json).")
    parser.add_argument("--timed-captions", type=Path,
                        help="Use these checked timings (captions.json) instead of re-timing.")
    arguments = parser.parse_args(argv)
    try:
        outputs = caption_video(
            arguments.video.resolve(), arguments.srt.resolve(),
            style_path=arguments.style, layout=arguments.layout,
            timed_captions=arguments.timed_captions,
        )
    except (ValueError, SubtitleImportError, CaptionError, MediaReviewError,
            FileNotFoundError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    report = outputs["report"]
    print(f"\nDone: {outputs['video']}")
    print(f"Aligned {report['aligned_word_count']} words, "
          f"mean confidence {report['mean_word_confidence']:.2f}.")
    if report["low_confidence_words"]:
        print("Check these spots (text may not match the speech): "
              + ", ".join(report["low_confidence_words"]))
    cleanup = report.get("srt_cleanup", {})
    if cleanup.get("junk_lines_removed"):
        print(f"Removed {cleanup['junk_lines_removed']} junk lines from the SRT (numbers/timestamps shown as text).")
    if cleanup.get("tamil_words_transliterated"):
        print("Tamil script written in English letters: " + ", ".join(cleanup["tamil_words_transliterated"]))
    if report.get("not_in_audio"):
        print("SRT lines not heard in the video (no caption): " + " | ".join(report["not_in_audio"]))
    for gap in report.get("uncaptioned_speech", []):
        print(f"WARNING: speech with no caption from {gap['from_seconds']}s to {gap['to_seconds']}s "
              f"(~{gap['speech_seconds']}s of talking). Your SRT may be missing lines there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
