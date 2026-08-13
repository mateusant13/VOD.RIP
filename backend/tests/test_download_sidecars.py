import sqlite3
from pathlib import Path

import pytest

from services import archive_db
from services.download_sidecars import (
    format_chat_txt,
    format_transcript_txt,
    resolve_remote_thumbnail,
    write_chat_sidecar,
    write_download_sidecars,
    write_thumbnail_sidecar,
    write_transcript_sidecar,
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


# ── Trim-scoped transcript + trim-fallback chat (download-options contract) ──

_VOD = "2536167775"
_VOD_URL = "https://www.twitch.tv/videos/2536167775"


@pytest.fixture()
def _scratch_db(tmp_path, monkeypatch):
    """Isolated archive DB per test (env-swapped so archive_db reconnects)."""
    db = tmp_path / "archive.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db))
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield db
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def _seed_transcript() -> None:
    archive_db.execute(
        "DELETE FROM transcripts WHERE platform = 'twitch' AND video_id = ?", (_VOD,)
    )
    archive_db.insert_transcript("twitch", _VOD, [
        {"seg_idx": 0, "start_sec": 100.0, "end_sec": 104.0, "text": "before trim"},
        {"seg_idx": 1, "start_sec": 410.0, "end_sec": 414.0, "text": "in trim"},
        {"seg_idx": 2, "start_sec": 420.0, "end_sec": 423.0, "text": "still in trim"},
        {"seg_idx": 3, "start_sec": 600.0, "end_sec": 605.0, "text": "after trim"},
    ])


def _seed_chat() -> None:
    archive_db.execute(
        "DELETE FROM messages WHERE platform = 'twitch' AND video_id = ?", (_VOD,)
    )
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 50.0, "username": "a", "text": "before trim"},
        {"offset_sec": 415.0, "username": "bob", "text": "in trim"},
        {"offset_sec": 422.0, "username": "alice", "text": "still in"},
        {"offset_sec": 700.0, "username": "a", "text": "after trim"},
    ])


def test_write_transcript_sidecar_trim_scoped(_scratch_db, tmp_path: Path):
    """Transcript sidecar covers exactly the trim window: rows outside the
    crop range are excluded, rows inside (or straddling it) are kept."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    got = write_transcript_sidecar(
        str(out), "twitch", _VOD, crop_start=410.0, crop_end=423.0
    )
    assert got == str(tmp_path / "vod.txt")
    body = Path(got).read_text("utf-8")
    assert "in trim" in body
    assert "still in trim" in body
    assert "before trim" not in body
    assert "after trim" not in body


def test_write_transcript_sidecar_no_trim_writes_whole(_scratch_db, tmp_path: Path):
    """Without a trim window the transcript sidecar keeps every row."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    got = write_transcript_sidecar(str(out), "twitch", _VOD)
    body = Path(got).read_text("utf-8")
    assert "before trim" in body
    assert "after trim" in body


def test_chat_sidecar_falls_back_to_trim(_scratch_db, tmp_path: Path):
    """include_chat with NO markers exports the trim window, not the whole
    VOD: messages outside crop_start/crop_end are dropped."""
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=False, include_chat=True,
        crop_start=410.0, crop_end=423.0,
        chat_start_sec=None, chat_end_sec=None,
        platform="Twitch",
    )
    body = Path(res["chat"]).read_text("utf-8")
    assert body.strip() == "bob: in trim\nalice: still in"


def test_chat_sidecar_markers_win_over_trim(_scratch_db, tmp_path: Path):
    """Explicit chat markers take precedence over the trim window."""
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=False, include_chat=True,
        crop_start=0.0, crop_end=100.0,
        chat_start_sec=415.0, chat_end_sec=422.0,
        platform="Twitch",
    )
    assert Path(res["chat"]).read_text("utf-8").strip() == "bob: in trim\nalice: still in"


def test_chat_sidecar_disabled_skipped(_scratch_db, tmp_path: Path):
    """include_chat=False writes no chat sidecar even when markers exist."""
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=False, include_chat=False,
        crop_start=410.0, crop_end=423.0,
        chat_start_sec=415.0, chat_end_sec=422.0,
        platform="Twitch",
    )
    assert "chat" not in res
    assert not (tmp_path / "vod.chat.txt").exists()


def test_download_sidecars_transcript_and_chat_share_trim(_scratch_db, tmp_path: Path):
    """One download, no chat markers: the transcript sidecar is bounded to
    the trim window AND the chat sidecar falls back to the same trim."""
    _seed_transcript()
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=True, include_chat=True,
        crop_start=410.0, crop_end=423.0,
        chat_start_sec=None, chat_end_sec=None,
        platform="Twitch",
    )
    tbody = Path(res["transcript"]).read_text("utf-8")
    assert "in trim" in tbody and "still in trim" in tbody
    assert "before trim" not in tbody and "after trim" not in tbody
    cbody = Path(res["chat"]).read_text("utf-8")
    assert cbody.strip() == "bob: in trim\nalice: still in"
