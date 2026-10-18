"""Render Nama Kadhai title/outro cards with Remotion (optional, local, free for individuals).

The card source lives in the repository's ``remotion/`` folder. Its npm install
lives outside OneDrive (default ``%USERPROFILE%\\dev\\namakadhai-remotion``)
because node_modules holds tens of thousands of files. ``sync_runtime`` copies
the source there; ``setup`` installs it once (an explicit, user-run step).
"""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "remotion"
FONT_FILE = PROJECT_ROOT / "fonts" / "Poppins-Bold.ttf"
DEFAULT_RUNTIME_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "dev" / "namakadhai-remotion"
COMPOSITIONS = ("TitleCard", "Outro")


class RemotionCardError(RuntimeError):
    """A card could not be prepared or rendered."""


def validate_card_props(
    *, title: str, subtitle: str = "", duration_seconds: float = 3.0,
    width: int, height: int, fps: float,
) -> dict:
    """The exact props passed to Remotion; plain data only, never code."""
    if not isinstance(title, str) or not title.strip() or len(title) > 80:
        raise RemotionCardError("Card title must be 1-80 characters.")
    if not isinstance(subtitle, str) or len(subtitle) > 120:
        raise RemotionCardError("Card subtitle must be at most 120 characters.")
    if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 10:
        raise RemotionCardError("Card duration must be between 1 and 10 seconds.")
    if not (320 <= width <= 3840 and 320 <= height <= 3840):
        raise RemotionCardError("Card size must be between 320 and 3840 pixels.")
    if width % 2 or height % 2:
        raise RemotionCardError("Card width and height must be even for H.264.")
    if not math.isfinite(fps) or not 24 <= fps <= 60:
        raise RemotionCardError("Card frame rate must be between 24 and 60.")
    return {
        "title": title.strip(), "subtitle": subtitle.strip(),
        "durationSeconds": float(duration_seconds),
        "width": int(width), "height": int(height), "fps": round(fps),
    }


def sync_runtime(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> Path:
    """Copy the card source and font into the install folder when they changed."""
    pairs = [(SOURCE_DIR / name, runtime_dir / name) for name in ("package.json", "tsconfig.json")]
    pairs += [(path, runtime_dir / path.relative_to(SOURCE_DIR)) for path in (SOURCE_DIR / "src").rglob("*") if path.is_file()]
    pairs.append((FONT_FILE, runtime_dir / "public" / FONT_FILE.name))
    for source, destination in pairs:
        if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return runtime_dir


def _npx() -> str:
    return "npx.cmd" if os.name == "nt" else "npx"


def is_ready(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> bool:
    return (runtime_dir / "node_modules" / "remotion" / "package.json").is_file()


def setup(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> None:
    """One-time install of Remotion and its headless browser (downloads ~300 MB)."""
    sync_runtime(runtime_dir)
    npm = "npm.cmd" if os.name == "nt" else "npm"
    for command in ([npm, "install", "--no-fund", "--no-audit"], [_npx(), "remotion", "browser", "ensure"]):
        result = subprocess.run(command, cwd=runtime_dir, check=False, shell=False)
        if result.returncode != 0:
            raise RemotionCardError(f"Remotion setup step failed: {' '.join(command)}")


def render_card(
    composition: str,
    props: dict,
    output: Path,
    *,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    timeout_seconds: float = 600,
) -> Path:
    """Render one card to a new H.264 MP4 (no audio). Never overwrites."""
    if composition not in COMPOSITIONS:
        raise RemotionCardError(f"Unknown card: {composition}")
    output = Path(output).resolve()
    if output.exists():
        raise RemotionCardError(f"Refusing to overwrite card: {output.name}")
    if not is_ready(runtime_dir):
        raise RemotionCardError(
            "Remotion is not set up. Run: python -m namakadhai.remotion_cards setup"
        )
    sync_runtime(runtime_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    props_file = runtime_dir / f".props-{token}.json"
    staged = output.with_name(f".{token}.partial.mp4")
    props_file.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    command = [
        _npx(), "remotion", "render", "src/index.ts", composition, str(staged),
        f"--props={props_file}", "--codec=h264", "--log=error", "--overwrite=false",
    ]
    try:
        result = subprocess.run(
            command, cwd=runtime_dir, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=timeout_seconds, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        staged.unlink(missing_ok=True)
        raise RemotionCardError(f"Remotion timed out rendering {composition}.") from exc
    finally:
        props_file.unlink(missing_ok=True)
    if result.returncode != 0 or not staged.is_file():
        staged.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RemotionCardError(
            f"Remotion could not render {composition}" + (f": {detail[-1]}" if detail else ".")
        )
    staged.replace(output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nama Kadhai Remotion cards.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Install Remotion (one-time download, ~300 MB).")
    render = commands.add_parser("render", help="Render one card to a new MP4.")
    render.add_argument("composition", choices=COMPOSITIONS)
    render.add_argument("output", type=Path)
    render.add_argument("--title", required=True)
    render.add_argument("--subtitle", default="")
    render.add_argument("--duration", type=float, default=3.0)
    render.add_argument("--width", type=int, default=1080)
    render.add_argument("--height", type=int, default=1920)
    render.add_argument("--fps", type=float, default=30)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "setup":
            setup()
            print(f"Remotion ready in {DEFAULT_RUNTIME_DIR}")
            return 0
        props = validate_card_props(
            title=arguments.title, subtitle=arguments.subtitle,
            duration_seconds=arguments.duration, width=arguments.width,
            height=arguments.height, fps=arguments.fps,
        )
        print(f"Card: {render_card(arguments.composition, props, arguments.output)}")
        return 0
    except (RemotionCardError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
