"""Caption Studio: a local web page to caption a video.

Upload a video and an SRT, pick a font and style, drag the caption to where
it should sit on the frame, then render. The render runs the normal caption
pipeline (word-timed against the audio) in the alignment environment.

    py -3.11 -m namakadhai.caption_studio          # opens http://127.0.0.1:8770
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import threading
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from namakadhai.srt_cleanup import clean_srt
from namakadhai.subtitle_import import parse_srt

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAGE = Path(__file__).resolve().parent / "studio_assets" / "studio.html"
FONTS_DIR = PROJECT_ROOT / "fonts"
UPLOADS = PROJECT_ROOT / "projects" / "uploads"
OUTPUTS = PROJECT_ROOT / "projects" / "captioned"
ALIGN_PYTHON = PROJECT_ROOT / ".venv-align" / "Scripts" / "python.exe"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
STYLE_KEYS = {
    "font_name": str, "text_color": str, "highlight_color": str, "outline_color": str,
    "outline_width": float, "shadow_depth": float, "fade_in_ms": int, "fade_out_ms": int,
    "animation": str, "entrance_ms": int, "bold": bool, "italic": bool,
    "word_highlight": bool, "font_scale": float, "margin_y_scale": float,
    "pos_x_scale": float, "box": bool, "box_color": str, "box_opacity": float,
    "uppercase": bool, "letter_spacing": float,
}


def bundled_fonts() -> list[dict]:
    """Fonts shipped in fonts/: family name from the file's name table."""
    found = []
    for path in sorted(FONTS_DIR.glob("*.[ot]tf")):
        family = _font_family(path) or path.stem.split("-")[0]
        found.append({"family": family, "file": path.name, "source": "bundled"})
    return found


def _font_family(path: Path) -> str | None:
    """Read the family name (name ID 1) from a TrueType/OpenType file."""
    try:
        data = path.read_bytes()
        tables = int.from_bytes(data[4:6], "big")
        for index in range(tables):
            record = data[12 + 16 * index: 28 + 16 * index]
            if record[:4] == b"name":
                offset = int.from_bytes(record[8:12], "big")
                count = int.from_bytes(data[offset + 2: offset + 4], "big")
                strings = offset + int.from_bytes(data[offset + 4: offset + 6], "big")
                for n in range(count):
                    entry = data[offset + 6 + 12 * n: offset + 18 + 12 * n]
                    platform, name_id = int.from_bytes(entry[0:2], "big"), int.from_bytes(entry[6:8], "big")
                    length, start = int.from_bytes(entry[8:10], "big"), int.from_bytes(entry[10:12], "big")
                    if name_id == 1 and platform == 3:
                        return data[strings + start: strings + start + length].decode("utf-16-be")
    except (OSError, ValueError, IndexError):
        return None
    return None


def system_fonts() -> list[str]:
    """Font families installed on Windows (libass finds these via DirectWrite)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Add-Type -AssemblyName System.Drawing; "
             "(New-Object System.Drawing.Text.InstalledFontCollection).Families | "
             "ForEach-Object { $_.Name }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    except (OSError, subprocess.TimeoutExpired):
        return []


def clean_style(raw: dict) -> dict:
    style = {}
    for key, kind in STYLE_KEYS.items():
        if key in raw and raw[key] is not None:
            value = raw[key]
            style[key] = bool(value) if kind is bool else kind(value)
    if not style.get("font_name"):
        raise ValueError("Choose a font.")
    return style


def _checked_captions(payload: dict) -> dict:
    """Validate timings edited in the browser: ordered, words inside their caption."""
    cues = payload.get("captions")
    if not isinstance(cues, list) or not cues:
        raise ValueError("No captions to render.")
    previous = 0.0
    for cue in cues:
        start, end = round(float(cue["start"]), 3), round(float(cue["end"]), 3)
        start = max(start, previous)
        if end - start < 0.05:
            end = start + 0.05
        cue["start"], cue["end"] = start, end
        for word in cue.get("words", []):
            word["start"] = min(max(round(float(word["start"]), 3), start), end - 0.01)
            word["end"] = min(max(round(float(word["end"]), 3), word["start"] + 0.01), end)
        previous = end
    return payload


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def _new(self, kind: str) -> dict:
        job = {"job_id": uuid.uuid4().hex, "kind": kind, "status": "running", "stage": "Starting",
               "progress": 2, "log": [], "output": None, "error": None}
        with self.lock:
            self.jobs[job["job_id"]] = job
        return job

    def start(self, video: Path, srt: Path, style: dict, timed: Path | None = None) -> dict:
        job = self._new("render")
        style_file = UPLOADS / f"style-{job['job_id']}.json"
        style_file.write_text(json.dumps({"caption": style}, indent=2), encoding="utf-8")
        extra = ["--timed-captions", str(timed)] if timed else []
        threading.Thread(target=self._run, args=(job, video, srt, style_file, extra), daemon=True).start()
        return dict(job)

    def start_align(self, video: Path, srt: Path) -> dict:
        """Time every SRT word against the audio, for checking before rendering."""
        job = self._new("align")
        output = UPLOADS / f"timing-{job['job_id']}.json"
        threading.Thread(target=self._align, args=(job, video, srt, output), daemon=True).start()
        return dict(job)

    def _align(self, job: dict, video: Path, srt: Path, output: Path) -> None:
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src"),
                   PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        cleaned = output.with_suffix(".srt")
        try:
            clean_srt(srt, cleaned)
            with self.lock:
                job.update(stage="Timing every word to your voice", progress=15)
            result = subprocess.run(
                [str(ALIGN_PYTHON), "-m", "namakadhai.forced_align", str(cleaned), str(video), str(output)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, cwd=PROJECT_ROOT, timeout=3600, check=False,
            )
            with self.lock:
                if result.returncode == 0 and output.is_file():
                    job.update(status="succeeded", stage="Done", progress=100, output=str(output))
                else:
                    detail = [l for l in (result.stderr or result.stdout).splitlines() if l.startswith("Error")]
                    job.update(status="failed", stage="Failed", progress=100, error=detail[-1] if detail else (
                        f"Stopped while loading the speech model (exit code {result.returncode}). "
                        "The PC is probably low on memory: close Chrome or restart, then try again."))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            with self.lock:
                job.update(status="failed", stage="Failed", progress=100, error=str(exc))

    def _run(self, job: dict, video: Path, srt: Path, style_file: Path, extra: list) -> None:
        stages = {"1/3": ("Timing every word to your voice", 10),
                  "2/3": ("Writing styled captions", 55),
                  "3/3": ("Burning captions into the video", 60)}
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src"),
                   PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            process = subprocess.Popen(
                [str(ALIGN_PYTHON), "-m", "namakadhai.caption_pipeline", str(video), str(srt),
                 "--style", str(style_file), *extra],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", env=env, cwd=PROJECT_ROOT,
            )
            for line in process.stdout:
                line = line.rstrip()
                if not line or "warnings.warn" in line or "UserWarning" in line:
                    continue
                with self.lock:
                    job["log"].append(line)
                    for key, (stage, progress) in stages.items():
                        if line.startswith(key):
                            job.update(stage=stage, progress=progress)
                    if line.startswith("Done: "):
                        job["output"] = line[6:].strip()
            code = process.wait()
            with self.lock:
                if code == 0 and job["output"]:
                    job.update(status="succeeded", stage="Done", progress=100)
                else:
                    reason = next((l for l in reversed(job["log"]) if l.startswith("Error")), "")
                    if not reason:
                        reason = ("Stopped while loading the speech model (exit code "
                                  f"{code}). The PC is probably low on memory: close Chrome "
                                  "or restart, then render again.")
                    job.update(status="failed", stage="Failed", error=reason, progress=100)
        except OSError as exc:
            with self.lock:
                job.update(status="failed", stage="Failed", error=str(exc), progress=100)

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return json.loads(json.dumps(self.jobs[job_id]))


class Handler(BaseHTTPRequestHandler):
    server: "Studio"

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": message}, status)

    def _allowed(self, path: Path) -> bool:
        resolved = path.resolve()
        return resolved.is_file() and any(
            resolved.is_relative_to(root) for root in (UPLOADS.resolve(), OUTPUTS.resolve())
        )

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path == "/":
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif route.path == "/api/fonts":
            self._json({"bundled": bundled_fonts(), "system": self.server.system_fonts})
        elif route.path.startswith("/fonts/"):
            font = FONTS_DIR / Path(unquote(route.path[7:])).name
            if font.is_file() and font.suffix.lower() in {".ttf", ".otf"}:
                self._send(HTTPStatus.OK, font.read_bytes(), "font/ttf")
            else:
                self._error("Font not found.", HTTPStatus.NOT_FOUND)
        elif route.path.startswith("/api/jobs/"):
            try:
                self._json({"job": self.server.jobs.get(route.path.rsplit("/", 1)[1])})
            except KeyError:
                self._error("Job not found.", HTTPStatus.NOT_FOUND)
        elif route.path == "/api/captions":
            path = Path(unquote(parse_qs(route.query).get("path", [""])[0]))
            if not self._allowed(path):
                self._error("Timing file not found.", HTTPStatus.NOT_FOUND)
            else:
                self._send(HTTPStatus.OK, path.read_bytes(), "application/json; charset=utf-8")
        elif route.path == "/media":
            self._media(parse_qs(route.query).get("path", [""])[0])
        else:
            self._error("Not found.", HTTPStatus.NOT_FOUND)

    def _media(self, raw: str) -> None:
        path = Path(unquote(raw))
        if not self._allowed(path):
            self._error("File is outside the studio folders.", HTTPStatus.FORBIDDEN)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:
                start = max(0, size - int(match.group(2)))
            end = min(end, size - 1)
        partial = bool(match and (match.group(1) or match.group(2)))
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if parse_qs(urlparse(self.path).query).get("download"):
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                block = handle.read(min(1 << 20, remaining))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (ConnectionError, OSError):
                    return
                remaining -= len(block)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            if route.path == "/api/upload":
                self._json(self._upload())
            elif route.path in ("/api/render", "/api/align"):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                video, srt = Path(body["video"]), Path(body["srt"])
                if not (self._allowed(video) and self._allowed(srt)):
                    raise ValueError("Upload the video and SRT first.")
                if route.path == "/api/align":
                    self._json({"job": self.server.jobs.start_align(video, srt)}, HTTPStatus.ACCEPTED)
                    return
                timed = None
                if body.get("captions"):
                    timed = UPLOADS / f"checked-{uuid.uuid4().hex}.json"
                    timed.write_text(json.dumps(_checked_captions(body["captions"]), ensure_ascii=False),
                                     encoding="utf-8")
                self._json({"job": self.server.jobs.start(video, srt, clean_style(body["style"]), timed)},
                           HTTPStatus.ACCEPTED)
            else:
                self._error("Not found.", HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._error(str(exc) or "Invalid request.")

    def _upload(self) -> dict:
        name = SAFE_NAME.sub("_", unquote(self.headers.get("X-Filename", "upload")))[-80:]
        kind = self.headers.get("X-Kind", "")
        if kind not in {"video", "srt"}:
            raise ValueError("Unknown upload type.")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("The file is empty.")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        target = UPLOADS / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}-{name}"
        with target.open("xb") as handle:
            remaining = length
            while remaining:
                block = self.rfile.read(min(1 << 20, remaining))
                if not block:
                    break
                handle.write(block)
                remaining -= len(block)
        result = {"path": str(target), "name": name}
        if kind == "srt":
            cleaned = target.with_suffix(".cleaned.srt")
            info = clean_srt(target, cleaned)
            result.update(info)
            result["lines_preview"] = [cue["text"] for cue in parse_srt(cleaned)][:40]
        return result


class Studio(ThreadingHTTPServer):
    def __init__(self, address) -> None:
        super().__init__(address, Handler)
        self.jobs = Jobs()
        self.system_fonts: list[str] = []
        threading.Thread(target=self._load_fonts, daemon=True).start()

    def _load_fonts(self) -> None:
        self.system_fonts = system_fonts()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Caption Studio (local web page).")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args(argv)
    server = Studio(("127.0.0.1", arguments.port))
    url = f"http://127.0.0.1:{arguments.port}/"
    print(f"Caption Studio running at {url}  (Ctrl+C to stop)", flush=True)
    if not arguments.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
