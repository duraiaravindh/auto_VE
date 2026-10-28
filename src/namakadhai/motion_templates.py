"""Allowlisted FFmpeg visual templates for the dynamic vertical renderer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from namakadhai.caption_renderer import _escape_filter_path
from namakadhai.creative_timeline import CreativeTimelineError


def _duration(event: dict[str, Any]) -> float:
    return float(event["output_end"]) - float(event["output_start"])


def normalized_chain(width: int, height: int, fps: int, motion: str) -> str:
    chain = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    )
    if motion == "push_in":
        chain += (
            f",zoompan=z='min(zoom+0.00055,1.055)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
        )
    elif motion == "push_out":
        chain += (
            f",zoompan=z='if(eq(on,1),1.055,max(1.0,zoom-0.00055))':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
        )
    elif motion != "hold":
        raise CreativeTimelineError(f"Unsupported motion template: {motion}")
    return chain


def fit_chain(width: int, height: int, fps: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    )


def reaction_filter(event: dict[str, Any], width: int, height: int) -> str | None:
    """Render one sparse, locally generated reaction mark with a soft fade."""
    reaction = event.get("reaction", "none")
    if reaction == "none":
        return None
    designs = {
        "spark": ("WOW", "#F4D06F"),
        "question": ("?", "#7FDBDA"),
        "heart": ("<3", "#FF8FA3"),
        "comment": ("...", "#F4D06F"),
    }
    if reaction not in designs:
        raise CreativeTimelineError(f"Unsupported reaction template: {reaction}")
    text, color = designs[reaction]
    visible = min(1.25, max(0.55, _duration(event) - 0.08))
    fade_out = max(0.2, visible - 0.24)
    font = Path(__file__).resolve().parents[2] / "fonts" / "Poppins-Bold.ttf"
    return (
        f"drawtext=fontfile='{_escape_filter_path(font)}':text='{text}':"
        f"fontcolor={color}:fontsize={round(height * 0.092)}:borderw={max(3, round(height * 0.004))}:"
        "bordercolor=black@0.72:x=w-text_w-w*0.075:y=h*0.14:"
        f"alpha='if(lt(t,0.14),t/0.14,if(lt(t,{fade_out:.3f}),1,"
        f"max(0,({visible:.3f}-t)/0.24)))':enable='between(t,0,{visible:.3f})'"
    )


def finishing_chain(event: dict[str, Any], width: int, height: int) -> str:
    """Return bounded emphasis and readability effects for one event."""
    transition = event["transition_in"]
    transition_duration = float(event["transition_duration"])
    filters = []
    if transition == "soft_fade":
        filters.append(f"fade=t=in:st=0:d={transition_duration:.3f}")
    elif transition == "light_sweep":
        filters.append(
            "drawbox="
            f"x='-iw/3+t/{transition_duration:.3f}*iw*1.6':y=0:w=iw/3:h=ih:"
            f"color=white@0.34:t=fill:enable='between(t,0,{transition_duration:.3f})'"
        )
    elif transition == "smear_flash":
        blur_duration = min(0.12, transition_duration / 2)
        filters.extend(
            (
                f"gblur=sigma=16:steps=2:enable='lt(t,{blur_duration:.3f})'",
                f"fade=t=in:st=0:d={min(0.16, transition_duration):.3f}:color=white",
            )
        )
    elif transition != "cut":
        raise CreativeTimelineError(f"Unsupported transition template: {transition}")
    reaction = reaction_filter(event, width, height)
    if reaction:
        filters.append(reaction)
    filters.extend((f"trim=duration={_duration(event):.6f}", "setpts=PTS-STARTPTS"))
    return ",".join(filters)


def build_layout_filters(
    event: dict[str, Any],
    *,
    index: int,
    width: int,
    height: int,
    fps: int,
    presenter_input: str,
    asset_input: str | None,
) -> tuple[list[str], str]:
    """Build one event's layout and return filter statements plus output label."""
    duration = _duration(event)
    layout = event["layout"]
    filters: list[str] = []
    presenter = f"presenter{index}"
    if layout != "broll_full":
        if not presenter_input:
            raise CreativeTimelineError(f"{layout} requires the presenter video input.")
        filters.append(
            f"{presenter_input}{normalized_chain(width, height, fps, event['motion'])}[{presenter}]"
        )
    composed = f"composed{index}"
    if layout == "presenter_full":
        filters.append(f"[{presenter}]null[{composed}]")
    elif asset_input is None:
        raise CreativeTimelineError(f"{layout} requires a visual asset input.")
    elif layout == "broll_full":
        filters.append(f"{asset_input}{fit_chain(width, height, fps)}[{composed}]")
    elif layout == "split_top":
        top_height = round(height * 0.46)
        bottom_height = height - top_height
        asset = f"asset{index}"
        lower = f"lower{index}"
        background = f"background{index}"
        stage = f"stage{index}"
        filters.extend(
            (
                f"{asset_input}{fit_chain(width, top_height, fps)}[{asset}]",
                f"[{presenter}]scale={width}:{bottom_height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{bottom_height},setsar=1[{lower}]",
                f"color=c=#101010:s={width}x{height}:d={duration:.6f}:r={fps}[{background}]",
                f"[{background}][{asset}]overlay=0:0:shortest=1[{stage}]",
                f"[{stage}][{lower}]overlay=0:{top_height}:shortest=1[{composed}]",
            )
        )
    elif layout == "circle_pip":
        asset = f"asset{index}"
        bubble = max(220, round(width * 0.34))
        ring = bubble + max(12, round(width * 0.018))
        ring_label = f"ring{index}"
        ring_stage = f"ringstage{index}"
        pip = f"pip{index}"
        filters.extend(
            (
                f"{asset_input}{fit_chain(width, height, fps)}[{asset}]",
                f"color=c=#D6A638:s={ring}x{ring}:d={duration:.6f}:r={fps},format=rgba,"
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                f"a='if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow({ring}/2,2)),255,0)'[{ring_label}]",
                f"[{asset}][{ring_label}]overlay=(W-w)/2:H-h-{round(height * 0.12)}:shortest=1[{ring_stage}]",
                f"[{presenter}]scale={bubble}:{bubble}:force_original_aspect_ratio=increase,"
                f"crop={bubble}:{bubble},format=rgba,"
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                f"a='if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow({bubble}/2,2)),255,0)'[{pip}]",
                f"[{ring_stage}][{pip}]overlay=(W-w)/2:H-h-{round(height * 0.12 + (ring - bubble) / 2)}:"
                f"shortest=1[{composed}]",
            )
        )
    else:
        raise CreativeTimelineError(f"Unsupported layout template: {layout}")
    output = f"eventv{index}"
    filters.append(f"[{composed}]{finishing_chain(event, width, height)}[{output}]")
    return filters, f"[{output}]"

