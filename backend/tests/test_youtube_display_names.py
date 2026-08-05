"""YouTube chat display-name resolution (UC channel id -> displayed name).

YouTube live-chat payloads carry only the @handle (username); the name
viewers see comes from the author channel page. resolve_youtube_display_names
fetches it via yt-dlp (one channel extract per distinct UC id) and caches it
in messages.display_name. The USER search filter then matches the displayed
name. Bot-walled ids stay NULL and are retried on a later run.

Run from backend/: python -m pytest tests/test_youtube_display_names.py
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

_DB = Path(tempfile.mkdtemp(prefix="yt-display-names-")) / "archive.db"
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set before import)
from services import archive_ytdlp  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _names_scratch_db():
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


def _seed_youtube_chat(user_id: str, handle: str, text: str = "oi") -> None:
    archive_db.insert_messages(
        "youtube", "yt-vid-1",
        [{"offset_sec": 1.0, "user_id": user_id, "username": handle, "text": text}],
    )


class _FakeYdl:
    def __init__(self, titles: dict[str, str]) -> None:
        self.titles = titles

    def extract_info(self, url: str, download: bool = False) -> dict:
        uid = url.rsplit("/", 1)[-1]
        title = self.titles.get(uid)
        if title is None:
            raise RuntimeError("bot wall")
        return {"title": title}


@contextmanager
def _fake_guard(ydl):
    yield ydl


def test_resolver_populates_display_name_and_is_idempotent(monkeypatch):
    _seed_youtube_chat("UCdyk2210", "@dyk2210")
    _seed_youtube_chat("UCScriptingkata", "@Scriptingkata")
    fake = _FakeYdl({"UCdyk2210": "dyk2210", "UCScriptingkata": "Scripting Kata"})
    monkeypatch.setattr(archive_ytdlp, "guarded_youtube_dl_channel",
                        lambda opts: _fake_guard(fake))

    n = archive_ytdlp.resolve_youtube_display_names(10)
    assert n == 2

    rows = archive_db.query(
        "SELECT DISTINCT user_id, display_name FROM messages "
        "WHERE platform = 'youtube' ORDER BY user_id"
    )
    by_id = {r["user_id"]: r["display_name"] for r in rows}
    assert by_id == {"UCdyk2210": "dyk2210", "UCScriptingkata": "Scripting Kata"}

    # Idempotent: resolved ids never come back.
    assert archive_ytdlp.resolve_youtube_display_names(10) == 0


def test_resolver_bot_wall_leaves_null(monkeypatch):
    _seed_youtube_chat("UCblocked", "@blocked")
    fake = _FakeYdl({"UCblocked": None})  # extract raises
    monkeypatch.setattr(archive_ytdlp, "guarded_youtube_dl_channel",
                        lambda opts: _fake_guard(fake))

    assert archive_ytdlp.resolve_youtube_display_names(10) == 0
    rows = archive_db.query(
        "SELECT display_name FROM messages WHERE user_id = 'UCblocked'"
    )
    assert rows[0]["display_name"] is None


def test_resolver_no_candidates_is_noop(monkeypatch):
    monkeypatch.setattr(archive_ytdlp, "guarded_youtube_dl_channel",
                        lambda opts: (_ for _ in ()).throw(AssertionError("no ydl")))
    assert archive_ytdlp.resolve_youtube_display_names(10) == 0
