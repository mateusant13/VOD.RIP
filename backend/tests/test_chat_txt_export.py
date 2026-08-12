"""Chat .txt export tests against a scratch archive DB.

The download chat sidecar writes <media>.chat.txt next to the finished
download when START/END markers are set in the chat history: one
`user: message` per line, NO timestamps, ordered by offset, bounded to
[start_sec, end_sec]. A clip download carries the clip slug but the chat
archive lives under the source VOD id — the writer resolves the slug via
the recorded clip history (twitch_clips.json).

Run from backend/: python -m pytest tests/test_chat_txt_export.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="chat-txt-export-"))
_DB = _TMP / "archive.db"

# Empty scratch DB: the app's own DDL is applied on first connect.
sqlite3.connect(str(_DB)).close()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402
from services.download_sidecars import (  # noqa: E402
    _clip_source_vod_id,
    write_chat_sidecar,
)


@pytest.fixture(scope="module", autouse=True)
def _chat_scratch_db():
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


@pytest.fixture()
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    return tmp_path


_VOD = "2536167775"


def _wipe_messages() -> None:
    archive_db.execute("DELETE FROM messages")


def test_chat_txt_marker_range_bounds_lines(tmp_path: Path):
    """Markers bound the txt to [start, end]: `user: message` per line,
    no timestamps, ordered; rows outside the range are excluded."""
    _wipe_messages()
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 400.0, "username": "a", "text": "before"},
        {"offset_sec": 410.0, "username": "bob", "text": "start msg"},
        {"offset_sec": 420.0, "username": "alice", "text": "middle"},
        {"offset_sec": 430.0, "username": "carol", "text": "end msg"},
        {"offset_sec": 440.0, "username": "a", "text": "after"},
    ])
    dest = tmp_path / "vod.chat.txt"
    got = write_chat_sidecar(str(dest), "twitch", _VOD, start_sec=410.0, end_sec=430.0)
    assert got == str(dest)
    body = dest.read_text("utf-8").strip()
    assert body == "bob: start msg\nalice: middle\ncarol: end msg"
    for line in body.splitlines():
        # user: message shape — no timestamp brackets anywhere
        assert "[" not in line and "]" not in line
        assert line.startswith(("bob:", "alice:", "carol:"))


def test_chat_txt_open_ended_markers(tmp_path: Path):
    """A single marker leaves the other end open: only start set keeps every
    row from start onward."""
    _wipe_messages()
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 10.0, "username": "a", "text": "early"},
        {"offset_sec": 20.0, "username": "b", "text": "late"},
        {"offset_sec": 30.0, "username": "c", "text": "last"},
    ])
    dest = tmp_path / "vod.chat.txt"
    got = write_chat_sidecar(str(dest), "twitch", _VOD, start_sec=20.0)
    assert got == str(dest)
    assert dest.read_text("utf-8").strip() == "b: late\nc: last"


def test_chat_txt_no_markers_writes_full_ordered(tmp_path: Path):
    """Without markers the whole archived chat is written, offset-ordered."""
    _wipe_messages()
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 5.0, "username": "x", "text": "first"},
        {"offset_sec": 2.0, "username": "y", "text": "zero"},
    ])
    dest = tmp_path / "vod.chat.txt"
    got = write_chat_sidecar(str(dest), "twitch", _VOD)
    assert got == str(dest)
    assert dest.read_text("utf-8").strip() == "y: zero\nx: first"


def test_chat_txt_range_excluding_everything_writes_nothing(tmp_path: Path):
    """A range that matches no row yields no file (download behavior
    unchanged: nothing on disk when the window is empty)."""
    _wipe_messages()
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 100.0, "username": "a", "text": "hi"},
    ])
    dest = tmp_path / "vod.chat.txt"
    assert write_chat_sidecar(str(dest), "twitch", _VOD, start_sec=500.0, end_sec=600.0) is None
    assert not dest.exists()


def test_clip_slug_resolves_source_vod_chat(_isolated_data_dir):
    """A clip download carries the clip slug, but the chat archive lives
    under the source VOD id — the writer resolves the slug through the
    recorded clip history so clip downloads get their chat txt too."""
    _wipe_messages()
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 100.0, "username": "a", "text": "clip chat"},
        {"offset_sec": 300.0, "username": "b", "text": "outside"},
    ])
    history = _isolated_data_dir / "twitch_clips.json"
    history.write_text(json.dumps([
        {"id": "FunnySlug1", "url": "https://clips.twitch.tv/FunnySlug1", "vod_id": _VOD},
    ]), encoding="utf-8")
    assert _clip_source_vod_id("FunnySlug1") == _VOD
    dest = _isolated_data_dir / "clip.chat.txt"
    got = write_chat_sidecar(str(dest), "twitch", "FunnySlug1", start_sec=0.0, end_sec=200.0)
    assert got == str(dest)
    assert dest.read_text("utf-8").strip() == "a: clip chat"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider", "--tb=short"])
