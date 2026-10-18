import json

import pytest

from transcribe import validate_output_path, write_transcript


def test_transcript_write_refuses_source_video(tmp_path):
    video = tmp_path / "recording.mp4"
    video.write_bytes(b"original video")

    with pytest.raises(ValueError, match="source video"):
        write_transcript(video, video, {"segments": []})

    assert video.read_bytes() == b"original video"


def test_transcript_write_refuses_existing_output(tmp_path):
    video = tmp_path / "recording.mp4"
    video.write_bytes(b"original video")
    output = tmp_path / "recording.transcript.json"
    output.write_text("keep this", encoding="utf-8")

    with pytest.raises(FileExistsError, match="existing output"):
        write_transcript(output, video, {"segments": []})

    assert output.read_text(encoding="utf-8") == "keep this"


def test_transcript_write_creates_new_json(tmp_path):
    video = tmp_path / "recording.mp4"
    video.write_bytes(b"original video")
    output = tmp_path / "recording.transcript.json"
    payload = {"language": "en", "highlights": [], "segments": []}

    write_transcript(output, video, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_caption_output_paths_refuse_existing_mp4_and_ass(tmp_path):
    import captions

    video = tmp_path / "recording.mp4"
    video.write_bytes(b"original video")
    output = tmp_path / "captioned.mp4"
    ass = output.with_suffix(".ass")
    ass.write_text("keep this", encoding="utf-8")

    with pytest.raises(FileExistsError, match="existing caption output"):
        captions.validate_output_paths(video, output)

    assert ass.read_text(encoding="utf-8") == "keep this"


def test_caption_output_paths_reject_source_video(tmp_path):
    import captions

    video = tmp_path / "recording.mp4"
    video.write_bytes(b"original video")

    with pytest.raises(ValueError, match="source video"):
        captions.validate_output_paths(video, video)

    assert video.read_bytes() == b"original video"
