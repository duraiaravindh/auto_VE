"""Render manually timed sentence captions with local FFmpeg and libass."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from namakadhai.media_review import (
    MediaReviewError,
    _resolve_ffmpeg,
    preflight_media,
)

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"
DEFAULT_MAX_LINES = 2
CAPTION_ANIMATIONS = ("pop", "fade")
LAYOUTS = {
    "landscape": {
        "font_scale": 0.05,
        "margin_x_scale": 0.07,
        "margin_y_scale": 0.075,
        "wrap_width": 34,
    },
    "vertical": {
        "font_scale": 0.055,
        "margin_x_scale": 0.08,
        "margin_y_scale": 0.10,
        "wrap_width": 24,
    },
}
WORD_PATTERN = re.compile(r"\w+", re.UNICODE)
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
ASS_COLOR_PATTERN = re.compile(r"^&H[0-9A-Fa-f]{8}$")


class CaptionError(RuntimeError):
    """Base error for caption generation and rendering."""


class CaptionInputError(CaptionError):
    """Caption data or styling is invalid."""


class CaptionOutputExistsError(CaptionError):
    """A caption output already exists and will not be overwritten."""


class CaptionRenderError(CaptionError):
    """FFmpeg failed to render the caption preview."""


@dataclass(frozen=True)
class CaptionWord:
    text: str
    start_seconds: float
    end_seconds: float
    confidence: float | None = None
    original: str | None = None


@dataclass(frozen=True)
class CaptionCue:
    start_seconds: float
    end_seconds: float
    text: str
    highlights: tuple[str, ...] = ()
    uncertain: bool = False
    words: tuple[CaptionWord, ...] = ()


@dataclass(frozen=True)
class CaptionStyle:
    font_name: str = "Varela Round"
    text_color: str = "#FFF4DE"
    highlight_color: str = "#D6A638"
    outline_color: str = "#241B14"
    outline_width: float = 3.0
    shadow_depth: float = 1.0
    fade_in_ms: int = 120
    fade_out_ms: int = 160
    animation: str = "pop"
    entrance_ms: int = 160
    bold: bool = False
    word_highlight: bool = False
    keyword_scale_percent: int = 100
    font_scale: float | None = None
    margin_y_scale: float | None = None
    italic: bool = False


@dataclass(frozen=True)
class CaptionOutput:
    project_directory: Path
    ass_file: Path
    video_file: Path


def load_caption_cues(path: str | Path) -> tuple[CaptionCue, ...]:
    """Load explicitly supplied, manually timed captions from a JSON file."""
    caption_file = Path(path)
    try:
        payload = json.loads(caption_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CaptionInputError(
            f"Could not read caption input file: {caption_file.name}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CaptionInputError("Caption input contains invalid JSON.") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise CaptionInputError("Caption input must use schema version 1.")
    raw_cues = payload.get("captions")
    if not isinstance(raw_cues, list) or not raw_cues:
        raise CaptionInputError("Caption input must contain a non-empty captions list.")

    cues: list[CaptionCue] = []
    previous_start = -1.0
    for index, raw in enumerate(raw_cues, start=1):
        if not isinstance(raw, dict):
            raise CaptionInputError(f"Caption {index} must be a JSON object.")
        try:
            start = float(raw["start"])
            end = float(raw["end"])
            text = raw["text"]
            highlights = raw.get("highlight", [])
            uncertain = raw.get("uncertain", False)
            raw_words = raw.get("words", [])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CaptionInputError(
                f"Caption {index} needs numeric start/end and text."
            ) from exc
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
        ):
            raise CaptionInputError(f"Caption {index} has an invalid time range.")
        if start < previous_start:
            raise CaptionInputError("Caption cues must be ordered by start time.")
        if not isinstance(text, str) or not text.strip():
            raise CaptionInputError(f"Caption {index} text must not be empty.")
        if (
            not isinstance(highlights, list)
            or any(not isinstance(term, str) or not term.strip() for term in highlights)
        ):
            raise CaptionInputError(
                f"Caption {index} highlight must be a list of non-empty words."
            )
        if not isinstance(uncertain, bool):
            raise CaptionInputError(f"Caption {index} uncertain must be a boolean.")
        if not isinstance(raw_words, list):
            raise CaptionInputError(f"Caption {index} words must be a list.")
        words: list[CaptionWord] = []
        for word_index, raw_word in enumerate(raw_words, start=1):
            if not isinstance(raw_word, dict):
                raise CaptionInputError(f"Caption {index} word {word_index} must be an object.")
            try:
                word_text = raw_word["text"]
                word_start = float(raw_word["start"])
                word_end = float(raw_word["end"])
                confidence = raw_word.get("confidence")
                confidence = float(confidence) if confidence is not None else None
                original = raw_word.get("original")
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CaptionInputError(f"Caption {index} word {word_index} is invalid.") from exc
            if (
                not isinstance(word_text, str)
                or not word_text.strip()
                or not math.isfinite(word_start)
                or not math.isfinite(word_end)
                or word_start < start
                or word_end > end
                or word_end <= word_start
                or (confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1))
                or (original is not None and not isinstance(original, str))
            ):
                raise CaptionInputError(f"Caption {index} word {word_index} is out of bounds.")
            words.append(CaptionWord(word_text.strip(), word_start, word_end, confidence, original))
        cues.append(
            CaptionCue(
                start_seconds=start,
                end_seconds=end,
                text=text.strip(),
                highlights=tuple(term.strip() for term in highlights),
                uncertain=uncertain,
                words=tuple(words),
            )
        )
        previous_start = start
    return tuple(cues)


def _rgb_to_ass(color: str) -> str:
    if not COLOR_PATTERN.fullmatch(color):
        raise CaptionInputError("Caption colors must use #RRGGBB format.")
    red, green, blue = color[1:3], color[3:5], color[5:7]
    return f"&H00{blue}{green}{red}".upper()


def _ass_timestamp(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole_seconds:02}.{centiseconds:02}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _wrap_caption(text: str, width: int) -> tuple[str, ...]:
    lines = textwrap.wrap(
        " ".join(text.split()),
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not lines:
        return ()
    if len(lines) > DEFAULT_MAX_LINES:
        raise CaptionInputError(
            "A caption exceeds two lines at the selected layout width; "
            "split it into naturally timed sentence-sized cues."
        )
    return tuple(lines)


def _highlight_line(
    line: str,
    highlights: tuple[str, ...],
    highlight_ass_color: str,
    normal_ass_color: str,
    keyword_scale_percent: int = 100,
) -> str:
    terms = {term.casefold() for term in highlights}
    cursor = 0
    output: list[str] = []
    for match in WORD_PATTERN.finditer(line):
        word = match.group()
        output.append(_escape_ass_text(line[cursor : match.start()]))
        if word.casefold() in terms:
            scale_on = (
                f"\\fscx{keyword_scale_percent}\\fscy{keyword_scale_percent}"
                if keyword_scale_percent != 100
                else ""
            )
            output.extend(
                (
                    "{\\c",
                    highlight_ass_color,
                    "&",
                    scale_on,
                    "}",
                    _escape_ass_text(word),
                    "{\\c",
                    normal_ass_color,
                    "&",
                    "\\fscx100\\fscy100" if keyword_scale_percent != 100 else "",
                    "}",
                )
            )
        else:
            output.append(_escape_ass_text(word))
        cursor = match.end()
    output.append(_escape_ass_text(line[cursor:]))
    return "".join(output)


def _cue_animation_tags(
    cue: CaptionCue,
    style: CaptionStyle,
    width: int,
    baseline_y: int,
    height: int,
) -> str:
    """ASS override tags for a cue's entrance and exit animation."""
    duration_ms = max(1, round((cue.end_seconds - cue.start_seconds) * 1000))
    fade_in = min(style.fade_in_ms, duration_ms // 3)
    fade_out = min(style.fade_out_ms, duration_ms // 3)
    if style.animation == "fade":
        return f"\\fad({fade_in},{fade_out})\\fscx98\\fscy98\\t(0,120,\\fscx100\\fscy100)"
    # "pop": rise a few pixels while scaling up with a slight overshoot, then
    # fade out at the end of the cue. Text stays sharp so it is readable at once.
    entrance = min(style.entrance_ms, duration_ms // 2)
    overshoot = round(entrance * 0.6)
    x = width // 2
    rise = max(4, round(height * 0.008))
    return (
        f"\\an2\\move({x},{baseline_y + rise},{x},{baseline_y},0,{entrance})"
        f"\\fad({fade_in},{fade_out})\\fscx92\\fscy92"
        f"\\t(0,{overshoot},\\fscx103\\fscy103)"
        f"\\t({overshoot},{entrance},\\fscx100\\fscy100)"
    )


def _karaoke_lines(
    cue: CaptionCue,
    lines: tuple[str, ...],
    highlight_ass_color: str,
    normal_ass_color: str,
) -> str | None:
    """Colour each word as it is spoken; None when words don't match the text."""
    tokens = [token for line in lines for token in line.split()]
    if len(tokens) != len(cue.words) or any(
        token != word.text for token, word in zip(tokens, cue.words)
    ):
        return None
    cue_ms = round((cue.end_seconds - cue.start_seconds) * 1000)
    rendered_lines: list[str] = []
    index = 0
    for line in lines:
        parts = []
        for token in line.split():
            word = cue.words[index]
            index += 1
            on = max(0, round((word.start_seconds - cue.start_seconds) * 1000))
            # Stay lit until the next word starts so the highlight never blinks off.
            following = cue.words[index].start_seconds if index < len(cue.words) else word.end_seconds
            off = min(cue_ms, max(on + 1, round((following - cue.start_seconds) * 1000)))
            parts.append(
                f"{{\\1c{normal_ass_color}&\\t({on},{on + 1},\\1c{highlight_ass_color}&)"
                f"\\t({off},{off + 1},\\1c{normal_ass_color}&)}}{_escape_ass_text(token)}"
            )
        rendered_lines.append(" ".join(parts))
    return r"\N".join(rendered_lines)


def build_ass(
    cues: tuple[CaptionCue, ...],
    *,
    width: int,
    height: int,
    layout: str,
    style: CaptionStyle = CaptionStyle(),
    title_text: str | None = None,
    title_duration_seconds: float = 3.0,
) -> str:
    """Format manually supplied sentence cues into a responsive ASS subtitle."""
    if layout not in LAYOUTS:
        raise CaptionInputError("layout must be 'landscape' or 'vertical'.")
    if width <= 0 or height <= 0:
        raise CaptionInputError("Video width and height must be positive.")
    if not cues and title_text is None:
        raise CaptionInputError("At least one caption cue or a title is required.")
    if (
        not math.isfinite(style.outline_width)
        or not math.isfinite(style.shadow_depth)
        or style.outline_width < 0
        or style.shadow_depth < 0
    ):
        raise CaptionInputError("Outline and shadow sizes must not be negative.")
    if (
        isinstance(style.fade_in_ms, bool)
        or isinstance(style.fade_out_ms, bool)
        or not isinstance(style.fade_in_ms, int)
        or not isinstance(style.fade_out_ms, int)
        or style.fade_in_ms < 0
        or style.fade_out_ms < 0
    ):
        raise CaptionInputError("Caption fade durations must be non-negative integers.")
    if style.animation not in CAPTION_ANIMATIONS:
        raise CaptionInputError(
            "Caption animation must be one of: " + ", ".join(CAPTION_ANIMATIONS) + "."
        )
    if (
        isinstance(style.entrance_ms, bool)
        or not isinstance(style.entrance_ms, int)
        or style.entrance_ms < 0
    ):
        raise CaptionInputError("Caption entrance duration must be a non-negative integer.")
    if (
        isinstance(style.keyword_scale_percent, bool)
        or not isinstance(style.keyword_scale_percent, int)
        or not 100 <= style.keyword_scale_percent <= 160
    ):
        raise CaptionInputError("Keyword scale must be an integer from 100 to 160 percent.")
    for label, value in (
        ("font scale", style.font_scale),
        ("vertical margin scale", style.margin_y_scale),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.02 <= float(value) <= 0.35
        ):
            raise CaptionInputError(f"Caption {label} must be between 0.02 and 0.35.")
    if title_text is not None and (
        not isinstance(title_text, str) or not title_text.strip()
    ):
        raise CaptionInputError("Title text must be non-empty when provided.")
    if not math.isfinite(title_duration_seconds) or title_duration_seconds <= 0:
        raise CaptionInputError("Title duration must be positive and finite.")
    layout_style = LAYOUTS[layout]
    font_scale = (
        float(style.font_scale)
        if style.font_scale is not None
        else float(layout_style["font_scale"])
    )
    margin_y_scale = (
        float(style.margin_y_scale)
        if style.margin_y_scale is not None
        else float(layout_style["margin_y_scale"])
    )
    font_size = max(16, round(height * font_scale))
    margin_x = round(width * layout_style["margin_x_scale"])
    margin_y = round(height * margin_y_scale)
    primary = _rgb_to_ass(style.text_color)
    highlight = _rgb_to_ass(style.highlight_color)
    outline = _rgb_to_ass(style.outline_color)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: NamaKadhai,{style.font_name},{font_size},{primary},"
        f"{primary},{outline},&H80000000,{-1 if style.bold else 0},"
        f"{-1 if style.italic else 0},0,0,100,100,0,0,1,"
        f"{style.outline_width:g},{style.shadow_depth:g},2,"
        f"{margin_x},{margin_x},{margin_y},1",
    ]
    if title_text is not None:
        title_size = max(24, round(height * 0.06))
        lines.append(
            f"Style: NamaKadhaiTitle,{style.font_name},{title_size},{primary},"
            f"{primary},{outline},&H80241B14,-1,0,0,0,100,100,0,0,3,"
            f"{style.outline_width:g},{style.shadow_depth:g},8,"
            f"{margin_x},{margin_x},{round(height * 0.14)},1"
        )
    lines.extend(
        (
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text",
        )
    )
    if title_text is not None:
        title_lines = _wrap_caption(title_text, int(layout_style["wrap_width"]))
        title_text_ass = r"\N".join(_escape_ass_text(line) for line in title_lines)
        lines.append(
            "Dialogue: 1,0:00:00.00,"
            f"{_ass_timestamp(title_duration_seconds)},NamaKadhaiTitle,,0,0,0,,"
            f"{{\\fad(180,260)}}{title_text_ass}"
        )
    for cue in cues:
        wrapped = _wrap_caption(cue.text, int(layout_style["wrap_width"]))
        if not wrapped:
            raise CaptionInputError("Caption text must not be empty.")
        caption_text = (
            _karaoke_lines(cue, wrapped, highlight, primary)
            if style.word_highlight and cue.words
            else None
        ) or r"\N".join(
            _highlight_line(
                line,
                cue.highlights,
                highlight,
                primary,
                style.keyword_scale_percent,
            )
            for line in wrapped
        )
        lines.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(cue.start_seconds)},"
            f"{_ass_timestamp(cue.end_seconds)},"
            "NamaKadhai,,0,0,0,,"
            f"{{{_cue_animation_tags(cue, style, width, height - margin_y, height)}}}"
            f"{caption_text}"
        )
    return "\n".join(lines) + "\n"


def render_threads() -> int:
    """FFmpeg threads: half the logical CPUs, at most 4, so an 8 GB laptop stays usable.

    Override with the NAMAKADHAI_RENDER_THREADS environment variable (1 = old behaviour).
    """
    configured = os.environ.get("NAMAKADHAI_RENDER_THREADS", "").strip()
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _escape_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    return value


def build_render_command(
    ffmpeg_executable: str | Path,
    source: str | Path,
    ass_file: str | Path,
    output: str | Path,
) -> list[str]:
    """Build an FFmpeg command that creates a new subtitled MP4 without overwriting."""
    font_directory = Path(__file__).resolve().parents[2] / "fonts"
    subtitle_filter = (
        f"ass=filename='{_escape_filter_path(Path(ass_file))}':"
        f"fontsdir='{_escape_filter_path(font_directory)}'"
    )
    return [
        str(ffmpeg_executable),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-filter_threads",
        str(render_threads()),
        "-threads",
        str(render_threads()),
        "-n",
        "-i",
        str(Path(source).resolve()),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        subtitle_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-threads:v",
        str(render_threads()),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]


def render_caption_preview(
    media_path: str | Path,
    caption_file: str | Path,
    *,
    layout: str = "landscape",
    settings_path: str | Path | None = None,
    style: CaptionStyle = CaptionStyle(),
) -> CaptionOutput:
    """Burn explicitly provided captions into a separate project-local MP4."""
    cues = load_caption_cues(caption_file)
    metadata, review_paths = preflight_media(
        media_path,
        settings_path=settings_path,
    )
    if metadata.video.width is None or metadata.video.height is None:
        raise CaptionInputError("FFprobe did not provide video dimensions.")
    if metadata.duration_seconds is not None:
        for index, cue in enumerate(cues, start=1):
            if cue.end_seconds > metadata.duration_seconds:
                raise CaptionInputError(
                    f"Caption {index} ends after the video duration."
                )
    ass_content = build_ass(
        cues,
        width=metadata.video.width,
        height=metadata.video.height,
        layout=layout,
        style=style,
    )
    try:
        ffmpeg = _resolve_ffmpeg(settings_path)
    except MediaReviewError as exc:
        raise CaptionRenderError(str(exc)) from exc

    output_dir = review_paths.project_directory / "captions"
    ass_file = output_dir / "captions.ass"
    video_file = output_dir / "caption_preview.mp4"
    if ass_file.exists() or video_file.exists():
        raise CaptionOutputExistsError(
            "Caption output already exists; refusing to overwrite captions.ass "
            "or caption_preview.mp4."
        )
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with ass_file.open("x", encoding="utf-8", newline="") as output_file:
            output_file.write(ass_content)
    except FileExistsError as exc:
        raise CaptionOutputExistsError(
            "Caption sidecar appeared during setup; refusing to overwrite."
        ) from exc
    except OSError as exc:
        raise CaptionRenderError("Could not write the ASS caption sidecar.") from exc

    command = build_render_command(ffmpeg, media_path, ass_file, video_file)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        video_file.unlink(missing_ok=True)
        raise CaptionRenderError("FFmpeg timed out while rendering captions.") from exc
    except OSError as exc:
        video_file.unlink(missing_ok=True)
        raise CaptionRenderError("Could not start the configured FFmpeg executable.") from exc

    if (
        result.returncode != 0
        or not video_file.is_file()
        or video_file.stat().st_size == 0
    ):
        video_file.unlink(missing_ok=True)
        detail = result.stderr.strip()
        detail = detail.replace(str(Path(media_path).resolve()), Path(media_path).name)
        detail = detail.replace(str(ffmpeg), "ffmpeg")
        raise CaptionRenderError(
            "FFmpeg did not produce the caption preview successfully."
            + (f" {detail}" if detail else "")
        )
    return CaptionOutput(
        project_directory=review_paths.project_directory,
        ass_file=ass_file,
        video_file=video_file,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render manually timed captions into a new local video file."
    )
    parser.add_argument("video", type=Path, help="Source video; it will not be modified.")
    parser.add_argument("captions", type=Path, help="Version 1 timed-caption JSON.")
    parser.add_argument(
        "--layout",
        choices=tuple(LAYOUTS),
        default="landscape",
        help="Caption safe-area profile; does not crop or resize the video.",
    )
    arguments = parser.parse_args(argv)
    try:
        output = render_caption_preview(
            arguments.video,
            arguments.captions,
            layout=arguments.layout,
        )
    except (
        CaptionError,
        MediaReviewError,
        FileNotFoundError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"ASS captions: {output.ass_file}")
    print(f"Caption preview: {output.video_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
