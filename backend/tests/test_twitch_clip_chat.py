"""GET /api/twitch/clips/{slug}/chat tests against a scratch archive DB.

History rows store offset_sec as the VOD END of the published clip media
(downloads crop [end-duration, end]), so the endpoint windows the source
VOD's archived chat to [offset_sec - duration_sec, offset_sec] and returns
it ordered by offset, capped at the archive window cap. Clips without
vod/offset metadata, or whose source VOD has no archived chat, return an
empty list gracefully; unknown slugs 404.

Run from backend/: python -m pytest tests/test_twitch_clip_chat.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

_TMP = Path(tempfile.mkdtemp(prefix="twitch-clip-chat-"))
_DB = _TMP / "archive.db"

# Empty scratch DB: the app's own DDL is applied on first connect.
sqlite3.connect(str(_DB)).close()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from app import app  # noqa: E402
from services import archive_db  # noqa: E402


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


@pytest.fixture(autouse=True)
def _skip_clip_gql_enrich(monkeypatch):
    # Record tests use fake slugs; never hit Twitch GQL unless a test patches this.
    monkeypatch.setattr(
        "services.twitch_gql_service.get_clip_info_sync",
        lambda url: (_ for _ in ()).throw(RuntimeError("gql skipped in tests")),
    )


_VOD = "2536167775"


def _wipe_messages() -> None:
    archive_db.execute("DELETE FROM messages")


async def _record(client, url: str, **extra):
    res = await client.post("/api/twitch/clips/record", json={"url": url, **extra})
    assert res.status_code == 200, res.text
    return res.json()["id"]


async def _chat(client, slug: str):
    res = await client.get(f"/api/twitch/clips/{slug}/chat")
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.anyio
async def test_clip_chat_windows_source_vod_messages(_isolated_data_dir):
    """Chat rows inside [offset_sec - duration_sec, offset_sec] come back
    ordered by offset; rows outside the window (before, after, other VOD)
    are excluded."""
    _wipe_messages()
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 400.0, "username": "a", "text": "before window"},
        {"offset_sec": 410.0, "username": "a", "text": "in 1"},
        {"offset_sec": 420.0, "username": "b", "text": "in 2"},
        {"offset_sec": 430.0, "username": "c", "text": "in 3"},
        {"offset_sec": 440.0, "username": "a", "text": "after window"},
    ])
    archive_db.insert_messages("twitch", "999999", [
        {"offset_sec": 420.0, "username": "x", "text": "other vod"},
    ])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        slug = await _record(client, "https://clips.twitch.tv/CuteSlothOMG1", vod_id=_VOD, offset_sec=434, duration_sec=30)
        resp = await _chat(client, slug)

    assert [m["offset_sec"] for m in resp["messages"]] == [410.0, 420.0, 430.0]
    assert all(m["text"] in ("in 1", "in 2", "in 3") for m in resp["messages"])
    assert all(m["platform"] == "twitch" and m["video_id"] == _VOD for m in resp["messages"])
    for key in ("offset_sec", "username", "text", "spam_count", "color"):
        assert key in resp["messages"][0]
    assert resp["total"] == 3
    assert resp["truncated"] is False


@pytest.mark.anyio
async def test_clip_chat_empty_window_is_graceful(_isolated_data_dir):
    """A clip whose source VOD has no archived chat in the window returns an
    empty list (200), not an error."""
    _wipe_messages()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        slug = await _record(client, "https://clips.twitch.tv/QuietClip1", vod_id=_VOD, offset_sec=434, duration_sec=30)
        resp = await _chat(client, slug)

    assert resp == {"messages": [], "truncated": False, "total": 0}


@pytest.mark.anyio
async def test_clip_chat_without_vod_metadata_returns_empty(_isolated_data_dir):
    """Clips with no vod/offset metadata (GQL enrich unavailable and the
    extension posted nothing) cannot be windowed — empty list, no crash."""
    _wipe_messages()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        slug = await _record(client, "https://clips.twitch.tv/NoVodClip1")
        resp = await _chat(client, slug)

    assert resp == {"messages": [], "truncated": False, "total": 0}


@pytest.mark.anyio
async def test_clip_chat_unknown_slug_404(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/twitch/clips/NeverRecorded1/chat")
    assert res.status_code == 404


@pytest.mark.anyio
async def test_clip_chat_truncates_at_archive_cap(_isolated_data_dir):
    """A dense window (> CHAT_WINDOW_HALF_LIMIT rows) is capped and the
    response reports the cut so the UI can show the archive-cap notice."""
    from services.archive_db import CHAT_WINDOW_HALF_LIMIT

    _wipe_messages()
    dense = [
        {"offset_sec": round(404.0 + i * 0.12, 3), "username": "u", "text": f"msg {i}"}
        for i in range(CHAT_WINDOW_HALF_LIMIT + 50)
    ]
    archive_db.insert_messages("twitch", _VOD, dense)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        slug = await _record(client, "https://clips.twitch.tv/DenseClip1", vod_id=_VOD, offset_sec=434, duration_sec=30)
        resp = await _chat(client, slug)

    assert len(resp["messages"]) == CHAT_WINDOW_HALF_LIMIT
    assert resp["total"] == len(dense)
    assert resp["truncated"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider", "--tb=short"])
