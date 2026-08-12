from pathlib import Path

import pytest

from services.download_sidecars import (
    format_chat_txt,
    format_transcript_txt,
    resolve_remote_thumbnail,
    write_thumbnail_sidecar,
)


def test_format_transcript_srt_like():
    body = format_transcript_txt([
        {"start_sec": 1.0, "end_sec": 3.5, "text": "hello"},
    ])
    assert "00:00:01,000 --> 00:00:03,500" in body
    assert "hello" in body


def test_format_chat_lines():
    """One `user: message` per line — no timestamps (plain text for editors)."""
    body = format_chat_txt([
        {"offset_sec": 75, "username": "bob", "text": "hi"},
        {"offset_sec": 90, "username": "alice", "text": "hello there"},
    ])
    assert body.strip() == "bob: hi\nalice: hello there"


def test_write_thumbnail_sidecar_prefers_ytdlp_jpg(tmp_path: Path):
    """A yt-dlp writethumbnail sidecar next to the output wins — no network."""
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    (tmp_path / "vod.jpg").write_bytes(b"thumb-bytes")
    assert write_thumbnail_sidecar("https://cdn.example/x.jpg", str(out)) == str(
        tmp_path / "vod.jpg"
    )


def test_write_thumbnail_sidecar_downloads_remote(tmp_path: Path):
    """Remote thumbnail URL (with Twitch placeholders) is fetched to <stem>.thumb.jpg."""
    src = tmp_path / "thumb48x36.jpg"
    src.write_bytes(b"jpeg-data")
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"video")
    url = src.as_uri().replace("thumb48x36", "thumb%{width}x%{height}")
    got = write_thumbnail_sidecar(url, str(out))
    assert got == str(tmp_path / "clip.mp4.thumb.jpg")
    assert Path(got).read_bytes() == b"jpeg-data"


def test_write_thumbnail_sidecar_no_source(tmp_path: Path):
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    assert write_thumbnail_sidecar(None, str(out)) is None
    assert write_thumbnail_sidecar("not-a-url", str(out)) is None


def test_resolve_remote_thumbnail_youtube_derived_locally():
    got = resolve_remote_thumbnail("https://www.youtube.com/watch?v=AbC123xYz9-", "YouTube")
    assert got == "https://i.ytimg.com/vi/AbC123xYz9-/mqdefault.jpg"


def test_resolve_remote_thumbnail_unknown_platform():
    assert resolve_remote_thumbnail("https://example.com/v", "Unknown") is None
