"""GET /api/chat/history — Chatterino-style backlog for the live chat panel.

Contract (best-effort by design, never 500):
- 200 {"messages":[{username, text, ts, color}]} ordered oldest→newest.
- Spans every archived video of the channel; the watchdog's synthetic live
  captures use <platform>-live-<slug>-<epoch-ms> ids with the login in
  videos.channel — matched case-insensitively.
- Recency key is ts when present, else the video_id/offset_sec fallback.
- Empty DB / unknown channel → {"messages": []}.
- limit respected; invalid platform/limit → 400/422.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_DB = Path(__file__).parent / "_chat_history_scratch.db"
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import app  # noqa: E402
from services import archive_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _chat_history_scratch_db():
    """Dedicated scratch DB: archive_db re-keys its connection on the env
    path, so rebinding here (like test_archive_chat_group) never touches
    conftest's shared scratch file or the real %APPDATA% DB."""
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


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _reset_db() -> None:
    archive_db.execute("DELETE FROM messages")
    archive_db.execute("DELETE FROM video_aliases")
    archive_db.execute("DELETE FROM videos")


def _seed_channel() -> None:
    """One synthetic watchdog live capture + one archived VOD for 'Chan'
    (case-mismatch on purpose: the query slug is lowercase), plus a foreign
    channel whose rows must never leak in."""
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "twitch-live-Chan-1750000000000",
        "channel": "Chan", "title": "live", "started_at": "2026-07-01T00:00:00Z",
        "kind": "live",
    })
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "v1", "channel": "Chan",
        "title": "VOD", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
    })
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "v-other", "channel": "other",
        "title": "VOD", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
    })
    archive_db.insert_messages("twitch", "twitch-live-Chan-1750000000000", [
        {"offset_sec": 0.0, "username": "livebot", "text": "first live",
         "ts": "2026-07-01T00:00:00Z", "color": "#ff0000"},
        {"offset_sec": 10.0, "username": "livebot", "text": "second live",
         "ts": "2026-07-01T00:01:00Z"},
    ])
    # "new vod msg" deliberately has NO ts — the video_id/offset fallback key
    # must still place it as the newest row of its video.
    archive_db.insert_messages("twitch", "v1", [
        {"offset_sec": 10.0, "username": "alice", "text": "old vod msg",
         "ts": "2026-08-01T00:00:00Z"},
        {"offset_sec": 30.0, "username": "bob", "text": "mid vod msg",
         "ts": "2026-08-01T00:05:00Z", "color": "#00ff00"},
        {"offset_sec": 50.0, "username": "carol", "text": "new vod msg"},
    ])
    archive_db.insert_messages("twitch", "v-other", [
        {"offset_sec": 1.0, "username": "eve", "text": "other channel",
         "ts": "2026-09-01T00:00:00Z"},
    ])


async def test_history_shape_order_and_channel_scope(client):
    _reset_db()
    _seed_channel()
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "twitch", "slug": "chan", "limit": 50},
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    # Oldest→newest across the synthetic live capture AND the archived VOD;
    # the foreign channel is excluded; lowercase slug matched 'Chan'.
    assert [m["text"] for m in msgs] == [
        "first live", "second live", "old vod msg", "mid vod msg", "new vod msg",
    ]
    for m in msgs:
        assert set(m) == {"username", "text", "ts", "color"}
        assert isinstance(m["username"], str) and m["username"]
        assert isinstance(m["text"], str) and m["text"]
        assert m["ts"] is None or isinstance(m["ts"], str)
        assert m["color"] is None or isinstance(m["color"], str)
    assert "other channel" not in {m["text"] for m in msgs}


async def test_history_respects_limit(client):
    _reset_db()
    _seed_channel()
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "twitch", "slug": "chan", "limit": 3},
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["text"] for m in msgs] == ["old vod msg", "mid vod msg", "new vod msg"]


async def test_history_fallback_order_for_ts_missing_rows(client):
    _reset_db()
    archive_db.upsert_video({
        "platform": "kick", "video_id": "k1", "channel": "chan",
        "title": "VOD", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
    })
    archive_db.insert_messages("kick", "k1", [
        {"offset_sec": 30.0, "username": "a", "text": "third"},
        {"offset_sec": 10.0, "username": "a", "text": "first"},
        {"offset_sec": 20.0, "username": "a", "text": "second"},
    ])
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "kick", "slug": "chan", "limit": 10},
    )
    assert resp.status_code == 200
    assert [m["text"] for m in resp.json()["messages"]] == ["first", "second", "third"]
    assert all(m["ts"] is None for m in resp.json()["messages"])


async def test_history_youtube_matches_bare_and_at_handle_slugs(client):
    """YouTube chat rows persist with the SAME schema and the endpoint serves
    them under BOTH slug forms ('titiltei' and '@titiltei') — the watchdog
    stores the bare settings login in videos.channel while the frontend's
    liveChatSlugFromUrl returns the @-prefixed path segment for youtube.com
    URLs; the @ normalization must never hide captured YouTube chat."""
    _reset_db()
    # Watchdog live capture row: channel stored as the bare settings login.
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "Ed9ph4z7RyU", "channel": "titiltei",
        "title": "SOLOQ", "started_at": "2026-08-13T19:46:06+00:00", "kind": "live",
    })
    # A second capture whose stored channel DOES carry the @ (settings saved
    # the handle verbatim) — the bare slug must match it too.
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "youtube-live-@titiltei-1785650000000",
        "channel": "@titiltei", "title": "soloq", "started_at": "2026-08-12T20:00:00+00:00",
        "kind": "live",
    })
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "v-other-yt", "channel": "someone",
        "title": "VOD", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
    })
    # Same message-row schema as Twitch/Kick: offset_sec, username, text, ts,
    # color (YouTube rows carry the @handle in username + a chat color).
    archive_db.insert_messages("youtube", "Ed9ph4z7RyU", [
        {"offset_sec": 5.0, "username": "@carlos_x_Y_z", "text": "e o hit nas costas?",
         "ts": "2026-08-13T19:13:52+00:00", "color": "#ff0000"},
        {"offset_sec": 60.0, "username": "@tio_wolf7", "text": "titizinho",
         "ts": "2026-08-13T19:44:17+00:00"},
    ])
    archive_db.insert_messages("youtube", "youtube-live-@titiltei-1785650000000", [
        {"offset_sec": 0.0, "username": "@fan", "text": "stored with @ channel",
         "ts": "2026-08-12T20:01:00+00:00"},
    ])
    archive_db.insert_messages("youtube", "v-other-yt", [
        {"offset_sec": 1.0, "username": "@eve", "text": "other channel",
         "ts": "2026-09-01T00:00:00Z"},
    ])

    # Bare settings login.
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "youtube", "slug": "titiltei", "limit": 50},
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["text"] for m in msgs] == [
        "stored with @ channel", "e o hit nas costas?", "titizinho",
    ]
    for m in msgs:
        assert set(m) == {"username", "text", "ts", "color"}
        assert isinstance(m["username"], str) and m["username"]
        assert isinstance(m["text"], str) and m["text"]
        assert m["ts"] is None or isinstance(m["ts"], str)
        assert m["color"] is None or isinstance(m["color"], str)
    assert "other channel" not in {m["text"] for m in msgs}

    # @-prefixed handle (liveChatSlugFromUrl's youtube.com path segment) —
    # the exact mismatch that used to hide ALL YouTube chat history.
    resp2 = await client.get(
        "/api/chat/history",
        params={"platform": "youtube", "slug": "@titiltei", "limit": 50},
    )
    assert resp2.status_code == 200
    assert [m["text"] for m in resp2.json()["messages"]] == [m["text"] for m in msgs]


async def test_history_empty_db_returns_empty(client):
    _reset_db()
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "twitch", "slug": "chan"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


async def test_history_unknown_channel_returns_empty(client):
    _reset_db()
    _seed_channel()
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "twitch", "slug": "nobody"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


async def test_history_rejects_unknown_platform(client):
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "myspace", "slug": "chan"},
    )
    assert resp.status_code == 400


async def test_history_rejects_out_of_range_limit(client):
    _reset_db()
    resp = await client.get(
        "/api/chat/history",
        params={"platform": "twitch", "slug": "chan", "limit": 0},
    )
    assert resp.status_code == 422
