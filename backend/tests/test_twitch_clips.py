"""Tests for the Twitch clip browser-path endpoints.

Clips are created in Twitch's own editor (clips.twitch.tv/create), opened by
the frontend with vodrip_* params and published by the VOD.RIP cookie
extension. The backend only:

- POST /api/twitch/clips/record — extension posts the published clip URL so
  the clip lands in history with a download button (idempotent by slug).
- GET/DELETE /api/twitch/clips/history — read / batch-remove history rows.
- POST/GET /api/debug/clip-events — append-only event-sequence sink.

Tests isolate VODRIP_DATA_DIR so the real user history is never touched.
"""
import json
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import app


@pytest.fixture()
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    return tmp_path


async def _history(client):
    res = await client.get("/api/twitch/clips/history")
    assert res.status_code == 200
    return res.json()


async def _record(client, body):
    return await client.post("/api/twitch/clips/record", json=body)


# --- history: legacy /create rows are dropped -----------------------------

@pytest.mark.anyio
async def test_history_filters_legacy_create_urls(_isolated_data_dir):
    """Legacy rows (clips.twitch.tv/create → Twitch's /clips/500 error page)
    are dropped from history; browser-flow rows survive."""
    legacy = {"id": "old1", "status": "editor_opened", "url": "https://clips.twitch.tv/create?vodID=2536167775&broadcasterLogin=surtepi&offsetSeconds=434"}
    live = {"id": "ClipId123", "status": "created", "url": "https://clips.twitch.tv/ClipId123-edit"}
    path = Path(_isolated_data_dir) / "twitch_clips.json"
    path.write_text(json.dumps([legacy, live], ensure_ascii=False), "utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rows = await _history(client)

    assert rows == [live]


# --- record: the extension posts the published clip URL -------------------

@pytest.mark.anyio
async def test_record_creates_history_row(_isolated_data_dir):
    """Published clip URL -> {ok,id,url} + a history row persisted to disk."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
            "title": "EPIC play!",
            "channel": "surtepi",
            "vod_id": "2536167775",
            "offset_sec": 434,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        assert res.json() == {
            "ok": True,
            "id": "CuteSlothOMG1",
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
        }

        rows = await _history(client)
        assert len(rows) == 1
        assert rows[0]["id"] == "CuteSlothOMG1"
        assert rows[0]["channel"] == "surtepi"
        assert rows[0]["vod_id"] == "2536167775"
        assert rows[0]["offset_sec"] == 434
        assert rows[0]["duration_sec"] == 30
        assert rows[0]["title"] == "EPIC play!"
        assert rows[0]["status"] == "created"
        # persisted on disk, newest first
        stored = json.loads(
            (_isolated_data_dir / "twitch_clips.json").read_text("utf-8")
        )
        assert stored[0]["id"] == rows[0]["id"]


@pytest.mark.anyio
async def test_record_is_idempotent_by_slug(_isolated_data_dir):
    """Re-posting the same clip (retry or duplicate publish) must not duplicate."""
    body = {
        "url": "https://clips.twitch.tv/CuteSlothOMG1",
        "channel": "surtepi",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(3):
            res = await _record(client, body)
            assert res.status_code == 200
        rows = await _history(client)
        assert len(rows) == 1
        assert rows[0]["id"] == "CuteSlothOMG1"


@pytest.mark.anyio
async def test_record_accepts_query_and_fragment(_isolated_data_dir):
    """The extension may post the tab's full URL — query/fragment are stripped."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1?t=65s#clip-viewer",
        })
        assert res.status_code == 200
        assert res.json()["id"] == "CuteSlothOMG1"


@pytest.mark.anyio
async def test_record_twitch_channel_clip_url(_isolated_data_dir):
    """twitch.tv/<channel>/clip/<slug> is an accepted public URL shape."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {"url": "https://www.twitch.tv/surtepi/clip/CuteSlothOMG1"})
        assert res.status_code == 200
        assert res.json()["id"] == "CuteSlothOMG1"


@pytest.mark.anyio
async def test_record_invalid_url_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for bad in ("not-a-url", "https://example.com/video", "https://clips.twitch.tv/"):
            res = await _record(client, {"url": bad})
            assert res.status_code == 422
        assert await _history(client) == []


@pytest.mark.anyio
async def test_record_invalid_channel_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
            "channel": "not a valid login!",
        })
        assert res.status_code == 422


@pytest.mark.anyio
async def test_record_offset_duration_validation(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
            "offset_sec": -1,
        })
        assert res.status_code == 422
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
            "duration_sec": 120,
        })
        assert res.status_code == 422
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
            "offset_sec": 1.5,
        })
        assert res.status_code == 422


# --- history delete -------------------------------------------------------

@pytest.mark.anyio
async def test_delete_history_removes_rows(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _record(client, {"url": "https://clips.twitch.tv/CuteSlothOMG1"})
        await _record(client, {"url": "https://clips.twitch.tv/AnotherClip2"})

        res = await client.request("DELETE", "/api/twitch/clips/history", json={"ids": ["CuteSlothOMG1"]})
        assert res.status_code == 200
        assert res.json() == {"ok": True, "removed": 1}

        rows = await _history(client)
        assert [r["id"] for r in rows] == ["AnotherClip2"]

        # deleting again removes nothing
        res = await client.request("DELETE", "/api/twitch/clips/history", json={"ids": ["CuteSlothOMG1"]})
        assert res.json()["removed"] == 0


@pytest.mark.anyio
async def test_delete_history_requires_ids(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.request("DELETE", "/api/twitch/clips/history", json={"ids": []})
        assert res.status_code == 422


# --- clip-events sink -----------------------------------------------------

@pytest.mark.anyio
async def test_clip_events_roundtrip(_isolated_data_dir):
    """App/ext steps POSTed to the sink come back in order via GET."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for src, event in (("app", "editor_open"), ("ext", "title_filled"), ("ext", "save_clicked")):
            res = await client.post(
                "/api/debug/clip-events",
                json={"src": src, "event": event, "data": {"clip_url": "https://clips.twitch.tv/CuteSlothOMG1"}},
            )
            assert res.status_code == 200
            assert res.json() == {"ok": True}

        res = await client.get("/api/debug/clip-events")
        assert res.status_code == 200
        events = res.json()
        assert [e["event"] for e in events] == ["editor_open", "title_filled", "save_clicked"]
        assert all(e["src"] in ("app", "ext") for e in events)
        assert all(e["ts"] for e in events)


@pytest.mark.anyio
async def test_clip_events_validation(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/debug/clip-events", json={"src": "nope", "event": "x"})
        assert res.status_code == 422
        res = await client.post("/api/debug/clip-events", json={"src": "app", "event": ""})
        assert res.status_code == 422
        res = await client.post(
            "/api/debug/clip-events",
            json={"src": "app", "event": "x", "data": {"blob": "z" * 9000}},
        )
        assert res.status_code == 422


@pytest.mark.anyio
async def test_record_appends_clip_event(_isolated_data_dir):
    """Recording a clip writes an 'api' event line so the flow is replayable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _record(client, {"url": "https://clips.twitch.tv/CuteSlothOMG1"})
        events = (await client.get("/api/debug/clip-events")).json()
    assert [e["event"] for e in events] == ["api_clip_recorded"]
    assert events[0]["src"] == "api"
    assert events[0]["id"] == "CuteSlothOMG1"
