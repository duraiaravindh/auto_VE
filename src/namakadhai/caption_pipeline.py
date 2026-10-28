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
from namakadhai.remotion_cards import RemotionCardError, render_card, validate_card_props
from namakadhai.media_review import MediaReviewError, _resolve_ffmpeg, preflight_media
from namakadhai.story_editor import load_creative_profile
from namakadhai.srt_cleanup import clean_srt
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
    original: Path | None = None,
    intro: tuple[str, str] | None = None,
    outro: tuple[str, str] | None = None,
) -> dict[str, Path]:
    """Caption ``video``; with ``original``, ``video`` is an edit of that recording."""
    metadata, _ = preflight_media(video)
    width, height = metadata.video.width, metadata.video.height
    if width is None or height is None or metadata.duration_seconds is None:
        raise SubtitleImportError("FFprobe did not report the video size and duration.")
    ffmpeg = _resolve_ffmpeg(None)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = output_root / f"{video.stem}-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)

    cleaned = output_dir / "subtitles_cleaned.srt"
    cleanup = clean_srt(srt, cleaned)
    print("1/3 Aligning caption words to the speech...", flush=True)
    captions = output_dir / "captions.json"
    payload = import_forced_aligned_srt(
        cleaned, original or video, captions,
        ffmpeg_path=ffmpeg, video_duration_seconds=metadata.duration_seconds,
        edited_path=video if original else None,
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

    final = preview
    if intro or outro:
        print("+ Rendering Remotion cards and joining them...", flush=True)
        fps = round(metadata.video.frame_rate or 30)
        cards = {}
        for name, composition, text in (("intro", "TitleCard", intro), ("outro", "Outro", outro)):
            if text:
                props = validate_card_props(
                    title=text[0], subtitle=text[1], width=width, height=height, fps=fps,
                )
                cards[name] = render_card(composition, props, output_dir / f"{name}_card.mp4")
        final = output_dir / f"{video.stem}_final.mp4"
        parts = [cards.get("intro"), preview, cards.get("outro")]
        result = subprocess.run(
            build_join_command(ffmpeg, [part for part in parts if part], final, width, height, fps),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=3600, shell=False,
        )
        if result.returncode != 0 or not final.is_file():
            raise CaptionError("FFmpeg could not join the cards. " + result.stderr[-400:])

    report = payload["source"]["alignment"]
    report["srt_cleanup"] = cleanup
    report["uncaptioned_speech"] = _uncaptioned_speech(
        original or video, ffmpeg, payload["captions"], metadata.duration_seconds
    )
    (output_dir / "alignment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"folder": output_dir, "captions": captions, "ass": ass, "video": final,
            "report": report}


def _uncaptioned_speech(media: Path, ffmpeg: Path, captions: list, duration: float) -> list:
    """Stretches of speech (>= 2 s) after the last caption or in long caption gaps."""
    from namakadhai.subtitle_import import detect_speech_activity, measure_speech_envelope

    active = detect_speech_activity(measure_speech_envelope(media, ffmpeg))
    covered = [(c["start"], c["end"]) for c in captions]
    gaps, previous_end = [], 0.0
    for start, end in covered + [(duration, duration)]:
        if start - previous_end >= 2.0:
            speech = sum(active[int(previous_end * 100):int(start * 100)]) / 100
            if speech >= 2.0:
                gaps.append({"from_seconds": round(previous_end, 1), "to_seconds": round(start, 1),
                             "speech_seconds": round(speech, 1)})
        previous_end = max(previous_end, end)
    return gaps


def build_join_command(
    ffmpeg: Path, parts: list[Path], output: Path, width: int, height: int, fps: int,
) -> list[str]:
    """Join clips end to end; clips without sound (Remotion cards) get silence."""
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-n"]
    for part in parts:
        command += ["-i", str(part)]
    filters, labels = [], []
    for index, part in enumerate(parts):
        filters.append(
            f"[{index}:v:0]scale={width}:{height},setsar=1,fps={fps},format=yuv420p[v{index}]"
        )
        if _has_audio(ffmpeg, part):
            filters.append(
                f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={_duration(ffmpeg, part):.3f}[a{index}]"
            )
        labels.append(f"[v{index}][a{index}]")
    filters.append("".join(labels) + f"concat=n={len(parts)}:v=1:a=1[v][a]")
    return command + [
        "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output),
    ]


def _probe(ffmpeg: Path, media: Path, entries: str, stream: str | None = None) -> str:
    ffprobe = ffmpeg.with_name(ffmpeg.name.replace("ffmpeg", "ffprobe"))
    command = [str(ffprobe), "-v", "error", "-show_entries", entries, "-of", "csv=p=0"]
    if stream:
        command += ["-select_streams", stream]
    return subprocess.run(
        command + [str(media)], capture_output=True, text=True, check=False, shell=False
    ).stdout.strip()


def _has_audio(ffmpeg: Path, media: Path) -> bool:
    return bool(_probe(ffmpeg, media, "stream=index", "a"))


def _duration(ffmpeg: Path, media: Path) -> float:
    return float(_probe(ffmpeg, media, "format=duration") or 0)


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
    parser.add_argument("--original", type=Path,
                        help="The unedited recording, when VIDEO is an edit of it (e.g. from OpenReel).")
    parser.add_argument("--intro", help="Title for a Remotion opening card.")
    parser.add_argument("--intro-subtitle", default="", help="Second line of the opening card.")
    parser.add_argument("--outro", help="Title for a Remotion closing card.")
    parser.add_argument("--outro-subtitle", default="", help="Call-to-action line of the closing card.")
    arguments = parser.parse_args(argv)
    try:
        outputs = caption_video(
            arguments.video.resolve(), arguments.srt.resolve(),
            profile_path=arguments.profile, layout=arguments.layout,
            original=arguments.original.resolve() if arguments.original else None,
            intro=(arguments.intro, arguments.intro_subtitle) if arguments.intro else None,
            outro=(arguments.outro, arguments.outro_subtitle) if arguments.outro else None,
        )
    except (ValueError, SubtitleImportError, CaptionError, MediaReviewError, RemotionCardError,
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
    for gap in report.get("uncaptioned_speech", []):
        print(f"WARNING: speech with no caption from {gap['from_seconds']}s to {gap['to_seconds']}s "
              f"(~{gap['speech_seconds']}s of talking). Your SRT may be missing lines there.")
    if report.get("edit", {}).get("lines_cut_in_edit"):
        print("Lines cut in the edit (no caption): " + " | ".join(report["edit"]["lines_cut_in_edit"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
