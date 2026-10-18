#!/usr/bin/env python3
"""Local Whisper transcription for Nama Kadhai (faster-whisper, CPU, int8).

NOTHING is downloaded unless you pass --approve-download. Without it the script
only uses a model that is already on disk and otherwise stops with a message.

  pip install faster-whisper
  python transcribe.py recording.mp4                       # uses cached model only
  python transcribe.py recording.mp4 --approve-download    # allow one-time model download

Output: <name>.transcript.json  (word-level timings, ready for captions.py)

Review step: open the JSON and fix the "text" field of any segment.
  - Keep the "words" list as is: timings are reused when the word count matches,
    and re-spread across the segment when it doesn't.
  - Wrap words in *asterisks* to highlight them in gold:  "Chinna vayasula *festival* vandhaale,"
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Leave decoding unseeded by default to avoid biasing recognition toward sample text.
DEFAULT_PROMPT = ""


def validate_output_path(out: Path, video: Path) -> Path:
    """Reject transcript destinations that could replace the source or prior work."""
    out = Path(out)
    video = Path(video)
    if out.resolve() == video.resolve():
        raise ValueError("refusing to overwrite the source video")
    if out.exists():
        if video.exists() and out.samefile(video):
            raise ValueError("refusing to overwrite the source video")
        raise FileExistsError(f"refusing to overwrite existing output: {out}")
    return out


def write_transcript(out: Path, video: Path, payload: dict) -> None:
    out = validate_output_path(out, video)
    with out.open("x", encoding="utf-8") as output_file:
        json.dump(payload, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--model", default="small", help="tiny/base/small/medium (small is a good start on 8 GB RAM)")
    ap.add_argument("--language", default="en",
                    help="transcription language; use 'ta' for Tamil audio")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT,
                    help="optional initial prompt (empty by default)")
    ap.add_argument("--approve-download", action="store_true",
                    help="explicitly allow downloading model weights")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    out = args.out or args.video.with_suffix(".transcript.json")
    try:
        validate_output_path(out, args.video)
    except (OSError, ValueError) as e:
        ap.error(str(e))

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed:  pip install faster-whisper")

    try:
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             local_files_only=not args.approve_download)
    except Exception as e:  # noqa: BLE001
        if not args.approve_download:
            sys.exit(f"Model '{args.model}' is not on disk, and downloads are not approved.\n"
                     f"Re-run with --approve-download to fetch it once.\n({e})")
        raise

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(args.video), "-vn",
                        "-ac", "1", "-ar", "16000", str(wav)], check=True)
        segments, info = model.transcribe(
            str(wav), language=args.language, initial_prompt=args.prompt,
            word_timestamps=True, vad_filter=True, beam_size=5,
            condition_on_previous_text=False)
        out_segments = []
        for i, s in enumerate(segments, 1):
            words = [{"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)}
                     for w in (s.words or []) if w.word.strip()]
            out_segments.append({"id": i, "start": round(s.start, 3), "end": round(s.end, 3),
                                 "text": s.text.strip(), "words": words})

    try:
        write_transcript(
            out,
            args.video,
            {"language": args.language, "highlights": [], "segments": out_segments},
        )
    except (OSError, ValueError) as e:
        ap.error(str(e))
    print(f"wrote {out}  ({len(out_segments)} segments). Review the text, then run captions.py.")


if __name__ == "__main__":
    main()
