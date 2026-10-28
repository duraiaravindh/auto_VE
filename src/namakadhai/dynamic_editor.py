"""Reference-inspired dynamic vertical video preparation, rendering and QA.

This command-line path intentionally does not depend on the browser UI.  It
uses reviewed SRT text, deterministic speech-activity timing, an allowlisted
creative timeline, approved local assets, and the project-local FFmpeg build.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from namakadhai.caption_renderer import (
    CaptionStyle,
    _escape_filter_path,
    build_ass,
    load_caption_cues,
    render_threads,
)
from namakadhai.creative_timeline import (
    CreativeTimelineError,
    create_creative_timeline,
    load_creative_timeline,
    sha256_file,
)
from namakadhai.media_review import MediaReviewError, _resolve_ffmpeg
from namakadhai.motion_templates import build_layout_filters
from namakadhai.graphic_assets import generate_graphic_catalog
from namakadhai.probe import ProbeError, probe_video
from namakadhai.subtitle_import import (
    SubtitleImportError,
    align_cues_to_speech_activity,
    detect_speech_activity,
    make_short_word_timed_captions,
    measure_speech_envelope,
    parse_srt,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "projects" / "dynamic"
LOUDNESS = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS")
TRUE_PEAK = re.compile(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s+dBFS")
BLACK = re.compile(r"black_start:(-?\d+(?:\.\d+)?)\s+black_end:(-?\d+(?:\.\d+)?)")
FREEZE_START = re.compile(r"freeze_start:\s*(-?\d+(?:\.\d+)?)")
FREEZE_END = re.compile(r"freeze_end:\s*(-?\d+(?:\.\d+)?)")


class DynamicEditorError(RuntimeError):
    """The dynamic-video workflow could not complete safely."""


def _write_json_exclusive(path: Path, payload: Any) -> None:
    if path.exists():
        raise DynamicEditorError(f"Refusing to overwrite {path.name}.")
    staged = path.with_name(f".{uuid.uuid4().hex}.partial.json")
    try:
        with staged.open("x", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        staged.replace(path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise DynamicEditorError(f"Could not write {path.name}.") from exc


def _dynamic_style() -> CaptionStyle:
    return CaptionStyle(
        font_name="Poppins",
        text_color="#F7E642",
        highlight_color="#F7E642",
        outline_color="#080808",
        outline_width=5.0,
        shadow_depth=2.5,
        fade_in_ms=70,
        fade_out_ms=100,
        animation="fade",
        entrance_ms=120,
        bold=True,
        italic=True,
        word_highlight=False,
        keyword_scale_percent=100,
        font_scale=0.050,
        margin_y_scale=0.075,
    )


def _shift_captions(cues: list[dict[str, Any]], offset: float, duration: float) -> list[dict[str, Any]]:
    shifted = []
    for cue in cues:
        start = max(0.0, float(cue["start"]) - offset)
        end = min(duration, float(cue["end"]) - offset)
        if end <= start:
            continue
        words = []
        for word in cue.get("words", []):
            word_start = max(start, float(word["start"]) - offset)
            word_end = min(end, float(word["end"]) - offset)
            if word_end > word_start:
                words.append({**word, "start": round(word_start, 3), "end": round(word_end, 3)})
        shifted.append(
            {**cue, "start": round(start, 3), "end": round(end, 3), "words": words}
        )
    if not shifted:
        raise DynamicEditorError("No captions remain after trimming non-speech handles.")
    return shifted


def prepare_dynamic_project(
    video_path: str | Path,
    srt_path: str | Path,
    *,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    asset_catalog_path: str | Path | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    auto_graphics: bool = True,
) -> dict[str, Path | None]:
    """Align captions, build a creative timeline, and write render inputs."""
    source = Path(video_path).resolve()
    srt = Path(srt_path).resolve()
    metadata = probe_video(source)
    if metadata.duration_seconds is None or metadata.audio is None:
        raise DynamicEditorError("The source needs a known duration and an audio stream.")
    parse_srt(srt)  # validate before creating output directories
    root = Path(output_root).resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    project = root / f"{source.stem}-{stamp}-{sha256_file(source)[:8]}"
    project.mkdir(parents=True, exist_ok=False)
    captions_path = project / "captions.json"
    timeline_path = project / "creative_timeline.json"
    ass_path = project / "captions.ass"
    try:
        source_cues = parse_srt(srt)
        subtitle_format = source_cues[0].get("source_format", "standard_srt")
        levels = measure_speech_envelope(source, _resolve_ffmpeg(None))
        active = detect_speech_activity(levels)
        aligned, paragraph_report = align_cues_to_speech_activity(
            source_cues,
            active,
            video_duration_seconds=metadata.duration_seconds,
            levels=levels,
        )
        short_cues, word_report = make_short_word_timed_captions(
            aligned,
            active,
            video_duration_seconds=metadata.duration_seconds,
            maximum_words=5,
            maximum_characters=30,
        )
        trim_start = max(0.0, float(paragraph_report["speech_start_seconds"]) - 0.12)
        trim_end = min(
            metadata.duration_seconds,
            float(paragraph_report["speech_end_seconds"]) + 0.18,
        )
        edit_duration = trim_end - trim_start
        short_cues = _shift_captions(short_cues, trim_start, edit_duration)
        caption_payload = {
            "version": 1,
            "reviewed": True,
            "review_actor": "source_srt_plus_signal_alignment",
            "review_required_before_publish": True,
            "source": {
                "srt": str(srt),
                "srt_sha256": sha256_file(srt),
                "media": str(source),
                "subtitle_format": subtitle_format,
                "paragraph_alignment": paragraph_report,
                "word_alignment": word_report,
                "source_trim_start_seconds": round(trim_start, 3),
                "source_trim_end_seconds": round(trim_end, 3),
            },
            "captions": short_cues,
        }
        _write_json_exclusive(captions_path, caption_payload)
        effective_catalog = Path(asset_catalog_path).resolve() if asset_catalog_path else None
        if effective_catalog is None and auto_graphics:
            effective_catalog = generate_graphic_catalog(
                " ".join(str(cue["text"]) for cue in source_cues),
                project / "generated_assets",
                ffmpeg_path=_resolve_ffmpeg(None),
                width=width,
                height=height,
                fps=fps,
            )
        timeline = create_creative_timeline(
            source,
            captions_path,
            timeline_path,
            asset_catalog_path=effective_catalog,
            width=width,
            height=height,
            fps=fps,
            source_start_seconds=trim_start,
            source_end_seconds=trim_end,
        )
        ass_content = build_ass(
            load_caption_cues(captions_path),
            width=width,
            height=height,
            layout="vertical" if height >= width else "landscape",
            style=_dynamic_style(),
        )
        with ass_path.open("x", encoding="utf-8", newline="") as output:
            output.write(ass_content)
        report_path = project / "preparation_report.json"
        generated_local_graphics = bool(effective_catalog and not asset_catalog_path)
        asset_warning = (
            "Only original locally generated motion graphics were used; no external B-roll was downloaded."
            if generated_local_graphics
            else "External B-roll is omitted unless an approved asset catalogue is supplied."
        )
        _write_json_exclusive(
            report_path,
            {
                "version": 1,
                "source": {"path": str(source), "sha256": timeline["source"]["sha256"]},
                "srt": {"path": str(srt), "sha256": sha256_file(srt)},
                "caption_count": len(short_cues),
                "event_count": len(timeline["events"]),
                "asset_count": len(timeline["assets"]),
                "asset_catalog": str(effective_catalog) if effective_catalog else None,
                "generated_local_graphics": generated_local_graphics,
                "subtitle_format": subtitle_format,
                "paragraph_alignment": paragraph_report,
                "word_alignment": word_report,
                "source_trim": {
                    "start": round(trim_start, 3),
                    "end": round(trim_end, 3),
                    "removed_leading_seconds": round(trim_start, 3),
                    "removed_trailing_seconds": round(metadata.duration_seconds - trim_end, 3),
                },
                "warnings": [
                    word_report["warning"],
                    *(
                        ["The supplied nested-SRT wrapper was recovered into its original cue structure."]
                        if subtitle_format == "nested_srt_recovered"
                        else []
                    ),
                    asset_warning,
                    "Final creator review is required before publishing.",
                ],
            },
        )
    except Exception:
        # Keep completed diagnostics, but never leave an empty project that looks
        # successful.  The source and SRT are outside this directory and untouched.
        if not any(project.iterdir()):
            project.rmdir()
        raise
    return {
        "project": project,
        "captions": captions_path,
        "ass": ass_path,
        "timeline": timeline_path,
        "report": report_path,
        "assets": effective_catalog,
    }


def _asset_inputs(payload: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    used = []
    seen = set()
    for event in payload["events"]:
        asset_id = event.get("asset_id")
        if asset_id and asset_id not in seen:
            used.append((asset_id, payload["assets"][asset_id]))
            seen.add(asset_id)
    music_id = payload["audio"].get("music_asset_id")
    if music_id and music_id not in seen:
        used.append((music_id, payload["assets"][music_id]))
        seen.add(music_id)
    for sfx in payload.get("sfx_events", []):
        asset_id = sfx["asset_id"]
        if asset_id not in seen:
            used.append((asset_id, payload["assets"][asset_id]))
            seen.add(asset_id)
    return used, {asset_id: index + 1 for index, (asset_id, _) in enumerate(used)}


def build_dynamic_render_command(
    timeline_path: str | Path,
    ass_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path | None = None,
) -> list[str]:
    """Build a bounded FFmpeg command from an already validated timeline."""
    payload = load_creative_timeline(timeline_path)
    ffmpeg = Path(ffmpeg_path).resolve() if ffmpeg_path else _resolve_ffmpeg(None)
    output = Path(output_path).resolve()
    ass = Path(ass_path).resolve()
    if not ass.is_file():
        raise DynamicEditorError("Caption ASS file is missing.")
    width = int(payload["output"]["width"])
    height = int(payload["output"]["height"])
    fps = int(payload["output"]["fps"])
    total_duration = float(payload["output"]["duration"])
    assets, input_index = _asset_inputs(payload)
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-filter_complex_threads", str(render_threads()),
        "-i", payload["source"]["path"],
    ]
    for _, asset in assets:
        if asset["kind"] == "image":
            command.extend(("-loop", "1", "-framerate", str(fps)))
        elif asset["kind"] in {"video", "music"}:
            command.extend(("-stream_loop", "-1"))
        command.extend(("-i", asset["path"]))

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, event in enumerate(payload["events"]):
        source_start = float(event["source_start"])
        source_end = float(event["source_end"])
        duration = float(event["output_end"]) - float(event["output_start"])
        presenter_base = f"pbase{index}"
        presenter_label = ""
        if event["layout"] != "broll_full":
            filters.append(
                f"[0:v:0]trim=start={source_start:.6f}:end={source_end:.6f},"
                f"setpts=PTS-STARTPTS[{presenter_base}]"
            )
            presenter_label = f"[{presenter_base}]"
        asset_label = None
        if event.get("asset_id"):
            asset_index = input_index[event["asset_id"]]
            asset_base = f"assetbase{index}"
            asset_start = float(event.get("asset_start", 0.0))
            filters.append(
                f"[{asset_index}:v:0]trim=start={asset_start:.6f}:duration={duration:.6f},"
                f"setpts=PTS-STARTPTS[{asset_base}]"
            )
            asset_label = f"[{asset_base}]"
        layout_filters, video_label = build_layout_filters(
            event,
            index=index,
            width=width,
            height=height,
            fps=fps,
            presenter_input=presenter_label,
            asset_input=asset_label,
        )
        filters.extend(layout_filters)
        audio_label = f"eventa{index}"
        filters.append(
            f"[0:a:0]atrim=start={source_start:.6f}:end={source_end:.6f},"
            f"asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:"
            f"sample_rates=48000:channel_layouts=stereo[{audio_label}]"
        )
        concat_inputs.extend((video_label, f"[{audio_label}]"))
    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(payload['events'])}:v=1:a=1[vcat][voice]"
    )
    font_directory = PROJECT_ROOT / "fonts"
    filters.append(
        f"[vcat]ass=filename='{_escape_filter_path(ass)}':"
        f"fontsdir='{_escape_filter_path(font_directory)}',format=yuv420p[vout]"
    )

    audio_label = "[voice]"
    music_id = payload["audio"].get("music_asset_id")
    if music_id:
        music_index = input_index[music_id]
        gain = float(payload["audio"].get("music_gain_db", -26.0))
        filters.append(
            f"[{music_index}:a:0]atrim=duration={total_duration:.6f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={gain:.3f}dB[musicbase]"
        )
        ducking = payload["audio"].get("ducking", {})
        music_mix = "[musicbase]"
        if ducking.get("enabled") is True:
            threshold = 10 ** (float(ducking.get("threshold_db", -30.0)) / 20)
            filters.append(
                f"[musicbase][voice]sidechaincompress=threshold={threshold:.8f}:"
                f"ratio={float(ducking.get('ratio', 8.0)):.3f}:"
                f"attack={float(ducking.get('attack_ms', 20.0)):.3f}:"
                f"release={float(ducking.get('release_ms', 350.0)):.3f}[musicducked]"
            )
            music_mix = "[musicducked]"
        filters.append(f"[voice]{music_mix}amix=inputs=2:duration=first:normalize=0[mixed]")
        audio_label = "[mixed]"

    sfx_labels = []
    for index, sfx in enumerate(payload.get("sfx_events", [])):
        asset_index = input_index[sfx["asset_id"]]
        delay = round(float(sfx["start"]) * 1000)
        gain = float(sfx.get("gain_db", -16.0))
        duration = float(sfx.get("duration", 2.0))
        label = f"sfx{index}"
        filters.append(
            f"[{asset_index}:a:0]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={gain:.3f}dB,adelay={delay}|{delay}[{label}]"
        )
        sfx_labels.append(f"[{label}]")
    if sfx_labels:
        filters.append(
            audio_label + "".join(sfx_labels)
            + f"amix=inputs={1 + len(sfx_labels)}:duration=first:normalize=0[withsfx]"
        )
        audio_label = "[withsfx]"
    audio = payload["audio"]
    filters.append(
        f"{audio_label}loudnorm=I={float(audio['target_lufs']):.2f}:"
        f"TP={float(audio['true_peak_dbtp']):.2f}:LRA={float(audio.get('loudness_range_lu', 7.0)):.2f},"
        "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
    )
    command.extend(
        (
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", "-t", f"{total_duration:.6f}", str(output),
        )
    )
    return command


def _run_analysis(command: list[str], timeout: int = 600) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise DynamicEditorError("FFmpeg output validation failed.")
    return result.stderr


def validate_dynamic_render(
    timeline_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path | None = None,
) -> dict[str, Any]:
    payload = load_creative_timeline(timeline_path)
    output = Path(output_path).resolve()
    ffmpeg = Path(ffmpeg_path).resolve() if ffmpeg_path else _resolve_ffmpeg(None)
    metadata = probe_video(output)
    errors, warnings = [], []
    expected = payload["output"]
    if metadata.video.width != expected["width"] or metadata.video.height != expected["height"]:
        errors.append("Rendered dimensions do not match the timeline.")
    if metadata.audio is None:
        errors.append("Rendered output has no audio stream.")
    elif metadata.audio.sample_rate_hz != 48000:
        errors.append("Rendered audio is not 48 kHz.")
    frame_tolerance = 2 / float(expected["fps"])
    actual_duration = metadata.duration_seconds or 0.0
    if abs(actual_duration - float(expected["duration"])) > frame_tolerance:
        errors.append("Rendered duration differs from the timeline by more than two frames.")

    loudness_log = _run_analysis(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(output),
         "-af", "ebur128=peak=true", "-f", "null", "NUL"]
    )
    loudness_values = LOUDNESS.findall(loudness_log)
    peak_values = TRUE_PEAK.findall(loudness_log)
    loudness = float(loudness_values[-1]) if loudness_values else None
    true_peak = float(peak_values[-1]) if peak_values else None
    if loudness is None or true_peak is None:
        warnings.append("Could not parse final loudness or true peak.")
    else:
        if abs(loudness - float(payload["audio"]["target_lufs"])) > 1.5:
            warnings.append("Final loudness is more than 1.5 LU from the selected target.")
        if true_peak > float(payload["audio"]["true_peak_dbtp"]) + 0.2:
            warnings.append("Final true peak is above the selected limit.")

    black_log = _run_analysis(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(output),
         "-vf", "blackdetect=d=0.25:pix_th=0.03", "-an", "-f", "null", "NUL"]
    )
    black_intervals = [
        {"start": float(start), "end": float(end)} for start, end in BLACK.findall(black_log)
    ]
    if black_intervals:
        warnings.append("Output contains a black interval lasting at least 250 ms.")
    freeze_log = _run_analysis(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(output),
         "-vf", "freezedetect=n=-50dB:d=0.9", "-an", "-f", "null", "NUL"]
    )
    freeze_starts = [float(value) for value in FREEZE_START.findall(freeze_log)]
    freeze_ends = [float(value) for value in FREEZE_END.findall(freeze_log)]
    freeze_intervals = [
        {"start": start, "end": end} for start, end in zip(freeze_starts, freeze_ends)
    ]
    generated_ranges = []
    for event in payload["events"]:
        asset_id = event.get("asset_id")
        asset = payload["assets"].get(asset_id, {}) if asset_id else {}
        if asset.get("provider") == "Nama Kadhai local template":
            generated_ranges.append((float(event["output_start"]), float(event["output_end"])))
    expected_freezes, unexpected_freezes = [], []
    for interval in freeze_intervals:
        target = expected_freezes if any(
            interval["start"] >= start - 0.15 and interval["end"] <= end + 0.15
            for start, end in generated_ranges
        ) else unexpected_freezes
        target.append(interval)
    if unexpected_freezes:
        warnings.append("Output contains a frozen interval lasting at least 900 ms.")
    return {
        "version": 1,
        "passed": not errors,
        "output": str(output),
        "output_sha256": sha256_file(output),
        "metadata": metadata.to_dict(),
        "expected_duration": expected["duration"],
        "integrated_lufs": loudness,
        "true_peak_dbtp": true_peak,
        "black_intervals": black_intervals,
        "freeze_intervals": freeze_intervals,
        "expected_template_freeze_intervals": expected_freezes,
        "unexpected_freeze_intervals": unexpected_freezes,
        "errors": errors,
        "warnings": warnings,
        "final_human_review": "REQUIRED",
    }


def render_dynamic_timeline(
    timeline_path: str | Path,
    *,
    output_path: str | Path | None = None,
    ass_path: str | Path | None = None,
) -> dict[str, Path]:
    timeline = Path(timeline_path).resolve()
    project = timeline.parent
    ass = Path(ass_path).resolve() if ass_path else project / "captions.ass"
    output = Path(output_path).resolve() if output_path else project / "dynamic_vertical_preview.mp4"
    report_path = output.with_suffix(".render_report.json")
    if output.exists() or report_path.exists():
        raise DynamicEditorError("Refusing to overwrite an existing render or report.")
    partial = output.with_name(f".{uuid.uuid4().hex}.partial.mp4")
    command = build_dynamic_render_command(timeline, ass, partial)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=7200,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        partial.unlink(missing_ok=True)
        raise DynamicEditorError("FFmpeg could not complete the dynamic render.") from exc
    if result.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        detail = result.stderr[-1200:].strip()
        raise DynamicEditorError("Dynamic render failed." + (f" {detail}" if detail else ""))
    partial.replace(output)
    report = validate_dynamic_render(timeline, output)
    _write_json_exclusive(report_path, report)
    if not report["passed"]:
        raise DynamicEditorError("Render exists but failed validation; inspect the render report.")
    return {"video": output, "report": report_path}


def render_dynamic_variants(
    timeline_path: str | Path,
    *,
    ass_path: str | Path | None = None,
) -> dict[str, Path]:
    """Render the reference edit as vertical and landscape review outputs."""
    timeline = Path(timeline_path).resolve()
    project = timeline.parent
    payload = load_creative_timeline(timeline)
    if int(payload["output"]["height"]) <= int(payload["output"]["width"]):
        raise DynamicEditorError("Dual-format rendering requires a vertical base timeline.")

    vertical = render_dynamic_timeline(timeline, ass_path=ass_path)
    landscape_timeline = project / "creative_timeline_landscape.json"
    landscape_ass = project / "captions_landscape.ass"
    landscape_payload = copy.deepcopy(payload)
    landscape_payload["profile"] = "reference_dynamic_v1_landscape"
    landscape_payload["output"]["width"] = 1920
    landscape_payload["output"]["height"] = 1080
    _write_json_exclusive(landscape_timeline, landscape_payload)
    landscape_content = build_ass(
        load_caption_cues(payload["captions"]["path"]),
        width=1920,
        height=1080,
        layout="landscape",
        style=CaptionStyle(
            **{
                **_dynamic_style().__dict__,
                "font_scale": 0.052,
                "margin_y_scale": 0.075,
            }
        ),
    )
    with landscape_ass.open("x", encoding="utf-8", newline="") as output:
        output.write(landscape_content)
    landscape = render_dynamic_timeline(
        landscape_timeline,
        output_path=project / "dynamic_landscape_preview.mp4",
        ass_path=landscape_ass,
    )
    return {
        "vertical_video": vertical["video"],
        "vertical_report": vertical["report"],
        "landscape_video": landscape["video"],
        "landscape_report": landscape["report"],
        "landscape_timeline": landscape_timeline,
        "landscape_ass": landscape_ass,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create reference-inspired dynamic vertical videos.")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Align captions and build a creative timeline.")
    prepare.add_argument("video", type=Path)
    prepare.add_argument("srt", type=Path)
    prepare.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    prepare.add_argument("--assets", type=Path, help="Approved version-1 asset catalogue JSON.")
    prepare.add_argument("--width", type=int, default=1080)
    prepare.add_argument("--height", type=int, default=1920)
    prepare.add_argument("--fps", type=int, default=30)
    prepare.add_argument("--no-auto-graphics", action="store_true")
    render = sub.add_parser("render", help="Render and validate a creative timeline.")
    render.add_argument("timeline", type=Path)
    render.add_argument("--out", type=Path)
    render.add_argument("--ass", type=Path)
    all_command = sub.add_parser("all", help="Prepare, render and validate in one command.")
    all_command.add_argument("video", type=Path)
    all_command.add_argument("srt", type=Path)
    all_command.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    all_command.add_argument("--assets", type=Path)
    all_command.add_argument("--width", type=int, default=1080)
    all_command.add_argument("--height", type=int, default=1920)
    all_command.add_argument("--fps", type=int, default=30)
    all_command.add_argument("--no-auto-graphics", action="store_true")
    all_command.add_argument(
        "--dual-format", action="store_true",
        help="Render both 1080x1920 vertical and 1920x1080 landscape previews.",
    )
    args = parser.parse_args(argv)
    try:
        if args.command in {"prepare", "all"}:
            outputs = prepare_dynamic_project(
                args.video,
                args.srt,
                output_root=args.output_root,
                asset_catalog_path=args.assets,
                width=args.width,
                height=args.height,
                fps=args.fps,
                auto_graphics=not args.no_auto_graphics,
            )
            print(f"Project: {outputs['project']}")
            print(f"Captions: {outputs['captions']}")
            print(f"Timeline: {outputs['timeline']}")
            if args.command == "all":
                if args.dual_format:
                    rendered = render_dynamic_variants(outputs["timeline"], ass_path=outputs["ass"])
                    print(f"Vertical video: {rendered['vertical_video']}")
                    print(f"Vertical report: {rendered['vertical_report']}")
                    print(f"Landscape video: {rendered['landscape_video']}")
                    print(f"Landscape report: {rendered['landscape_report']}")
                else:
                    rendered = render_dynamic_timeline(outputs["timeline"], ass_path=outputs["ass"])
                    print(f"Video: {rendered['video']}")
                    print(f"Render report: {rendered['report']}")
        else:
            rendered = render_dynamic_timeline(args.timeline, output_path=args.out, ass_path=args.ass)
            print(f"Video: {rendered['video']}")
            print(f"Render report: {rendered['report']}")
    except (
        CreativeTimelineError,
        DynamicEditorError,
        MediaReviewError,
        ProbeError,
        SubtitleImportError,
        FileNotFoundError,
        OSError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

