"""Generate original local motion cards from conservative transcript concepts."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from namakadhai.caption_renderer import _escape_filter_path, render_threads
from namakadhai.creative_timeline import CreativeTimelineError, sha256_file


@dataclass(frozen=True)
class GraphicConcept:
    concept_id: str
    title: str
    subtitle: str
    tags: tuple[str, ...]
    background: str
    accent: str


CONCEPTS = (
    GraphicConcept(
        "festival-memory", "FESTIVAL", "MEMORIES", ("festival", "season"), "#14213D", "#F4D06F"
    ),
    GraphicConcept(
        "movie-night", "MOVIE", "NIGHT", ("padam", "padatha", "tv-la", "movie"), "#2B193D", "#F4A261"
    ),
    GraphicConcept(
        "ad-break", "AD", "BREAK", (" ad ", "commercial"), "#3A0F1F", "#FFCC66"
    ),
    GraphicConcept(
        "channel-change", "CHANGE", "THE CHANNEL", ("channel", "maathi"), "#0F3D3E", "#7FDBDA"
    ),
    GraphicConcept(
        "the-feeling", "THAT", "FEELING", ("satisfaction", "feel"), "#352F44", "#FAE8A4"
    ),
    GraphicConcept(
        "ayudha-poojai", "AYUDHA", "POOJAI", ("aayudha", "poojai", "pooja"), "#3D2C1E", "#F4D06F"
    ),
    GraphicConcept(
        "deepavali-countdown", "DEEPAVALI", "COUNTDOWN", ("deepavali", "pattaasu", "sweets"), "#4B1535", "#FFB703"
    ),
    GraphicConcept(
        "family-memories", "FAMILY", "MEMORIES", ("family", "family-aa", "ninaivugal", "moments-ah"), "#183A37", "#8ED1C5"
    ),
    GraphicConcept(
        "clean-together", "CLEAN", "TOGETHER", ("clean", "wash", "help"), "#17324D", "#7CC7FF"
    ),
    GraphicConcept(
        "your-story", "YOUR STORY", "COMMENT BELOW", ("comment-la", "comment"), "#342A47", "#E9C46A"
    ),
)


def matching_concepts(text: str) -> list[GraphicConcept]:
    normalized = re.sub(r"\s+", " ", text.casefold())
    return [
        concept
        for concept in CONCEPTS
        if any(
            re.search(rf"(?<!\w){re.escape(tag.strip())}(?!\w)", normalized)
            for tag in concept.tags
        )
    ]


def generate_graphic_catalog(
    transcript_text: str,
    output_directory: str | Path,
    *,
    ffmpeg_path: str | Path,
    width: int,
    height: int,
    fps: int,
    duration_seconds: float = 4.0,
) -> Path | None:
    """Create original MP4 topic cards and restrained procedural sound effects."""
    concepts = matching_concepts(transcript_text)
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=False)
    ffmpeg = Path(ffmpeg_path).resolve()
    font = Path(__file__).resolve().parents[2] / "fonts" / "Poppins-Bold.ttf"
    font_filter = _escape_filter_path(font)
    assets = []
    for concept in concepts:
        destination = output / f"{concept.concept_id}.mp4"
        title = concept.title.replace("'", r"\'").replace(":", r"\:")
        subtitle = concept.subtitle.replace("'", r"\'").replace(":", r"\:")
        video_filter = ",".join(
            (
                f"drawbox=x='-iw*0.35+t*iw*0.20':y=ih*0.12:w=iw*0.32:h=ih*0.76:"
                f"color={concept.accent}@0.10:t=fill",
                f"drawbox=x='iw-t*iw*0.16':y=ih*0.18:w=iw*0.18:h=ih*0.64:"
                f"color=white@0.05:t=fill",
                f"drawtext=fontfile='{font_filter}':text='{title}':fontcolor={concept.accent}:"
                f"fontsize={round(height * 0.085)}:x=(w-text_w)/2:y=h*0.40-text_h",
                f"drawtext=fontfile='{font_filter}':text='{subtitle}':fontcolor=white:"
                f"fontsize={round(height * 0.052)}:x=(w-text_w)/2:y=h*0.52",
                f"drawtext=fontfile='{font_filter}':text='NAMA KADHAI':fontcolor=white@0.55:"
                f"fontsize={round(height * 0.018)}:x=(w-text_w)/2:y=h*0.82",
                "fade=t=in:st=0:d=0.18",
                f"fade=t=out:st={duration_seconds - 0.22:.3f}:d=0.22",
                "format=yuv420p",
            )
        )
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i",
            f"color=c={concept.background}:s={width}x{height}:d={duration_seconds:.3f}:r={fps}",
            "-vf", video_filter,
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-threads", str(render_threads()), "-pix_fmt", "yuv420p", str(destination),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
            shell=False,
        )
        if result.returncode != 0 or not destination.is_file():
            destination.unlink(missing_ok=True)
            raise CreativeTimelineError(
                f"Could not generate local motion card {concept.concept_id}. " + result.stderr[-300:]
            )
        assets.append(
            {
                "asset_id": f"generated-{concept.concept_id}",
                "kind": "video",
                "path": str(destination),
                "sha256": sha256_file(destination),
                "approved": True,
                "license": "Original locally generated motion graphic",
                "provider": "Nama Kadhai local template",
                "source_url": "",
                "attribution": "Generated from creator-approved transcript concepts",
                "tags": [tag.strip() for tag in concept.tags],
            }
        )
    sfx_specs = (
        (
            "pop", "sine=frequency=920:duration=0.14:sample_rate=48000",
            "afade=t=out:st=0.035:d=0.105,volume=0.28",
        ),
        (
            "whoosh", "anoisesrc=color=pink:duration=0.42:sample_rate=48000",
            "highpass=f=350,lowpass=f=5200,afade=t=in:st=0:d=0.08,"
            "afade=t=out:st=0.20:d=0.22,volume=0.10",
        ),
        (
            "impact", "sine=frequency=96:duration=0.28:sample_rate=48000",
            "afade=t=out:st=0.025:d=0.255,volume=0.32",
        ),
    )
    for effect_id, source_filter, audio_filter in sfx_specs:
        destination = output / f"{effect_id}.wav"
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", source_filter,
            "-af", audio_filter, "-ar", "48000", "-ac", "2",
            "-c:a", "pcm_s16le", str(destination),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            shell=False,
        )
        if result.returncode != 0 or not destination.is_file():
            destination.unlink(missing_ok=True)
            raise CreativeTimelineError(
                f"Could not generate local sound effect {effect_id}. " + result.stderr[-300:]
            )
        assets.append(
            {
                "asset_id": f"generated-{effect_id}",
                "kind": "sfx",
                "path": str(destination),
                "sha256": sha256_file(destination),
                "approved": True,
                "license": "Original locally generated procedural sound effect",
                "provider": "Nama Kadhai local template",
                "source_url": "",
                "attribution": "Generated locally by the Nama Kadhai renderer",
                "tags": [effect_id],
            }
        )
    catalog = output / "asset_catalog.json"
    catalog.write_text(
        json.dumps({"version": 1, "assets": assets}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return catalog

