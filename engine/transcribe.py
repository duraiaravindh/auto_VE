"""Transcribes a clip with the locally cached Whisper model. Nothing is downloaded or uploaded.

Run from the AI Video Editor folder:
    python engine/transcribe.py <clip> <output folder>
Writes <output folder>/whisper_ta.json with segments and word timings. The clip is only read.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, "engine")
from analyze_references import FF

import torch
import whisper

clip, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
wav = f"{out}/voice_16k.wav"
subprocess.run([FF, "-v", "error", "-y", "-i", clip, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)

torch.set_num_threads(4)
model = whisper.load_model("small", device="cpu", download_root=os.path.expanduser("~/.cache/whisper"))
result = model.transcribe(wav, language="ta", task="transcribe", word_timestamps=True, fp16=False,
                          temperature=0.0, condition_on_previous_text=False)
with open(f"{out}/whisper_ta.json", "w", encoding="utf-8") as f:
    json.dump(result["segments"], f, ensure_ascii=False, indent=0)
print("segments", len(result["segments"]))
