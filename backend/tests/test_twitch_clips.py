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
import logging
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import app
from routers.twitch_clips import _CLIP_ECHO_QUIET, _format_clip_event, published_clip_range_from_gql


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


def test_clip_event_terminal_line_is_compact():
    line = _format_clip_event(
        "ext",
        "ext_range",
        {"ok": True, "valuetext": "1:18 to 1:30", "relStart": 78, "census": ["skip"]},
    )
    assert line.startswith("CLIP ext ext_range")
    assert "relStart=78" in line
    assert "valuetext=1:18 to 1:30" in line
    assert "census" not in line
    assert "trace_dom" in _CLIP_ECHO_QUIET


@pytest.mark.anyio
async def test_clip_event_echoes_to_logger(caplog, _isolated_data_dir):
    caplog.set_level(logging.INFO, logger="routers.twitch_clips")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/debug/clip-events",
            json={"src": "ext", "event": "ext_start", "data": {"startSec": 12, "endSec": 24}},
        )
        assert res.status_code == 200
        quiet = await client.post(
            "/api/debug/clip-events",
            json={"src": "ext", "event": "trace_dom", "data": {"reason": "mutation"}},
        )
        assert quiet.status_code == 200
    messages = [rec.message for rec in caplog.records]
    assert any("CLIP ext ext_start" in msg and "startSec=12" in msg for msg in messages)
    assert not any("trace_dom" in msg for msg in messages)


@pytest.mark.anyio
async def test_record_appends_clip_event(_isolated_data_dir):
    """Recording a clip writes an 'api' event line so the flow is replayable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {
            "url": "https://clips.twitch.tv/CuteSlothOMG1",
            "channel": "surtepi",
            "vod_id": "2536167775",
            "offset_sec": 434,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        events = (await client.get("/api/debug/clip-events")).json()
        assert any(e.get("event") == "api_clip_recorded" and e.get("id") == "CuteSlothOMG1" for e in events)


@pytest.mark.anyio
async def test_clip_endpoints_answer_cors_preflight(_isolated_data_dir):
    """The clip-assist content script POSTs JSON from https://clips.twitch.tv to
    the localhost app — cross-origin, so the browser blocks the POST unless
    OPTIONS answers with the allow headers (this was the root cause of zero
    ext_* events reaching the sink). Both direct-POST endpoints must also
    carry ACAO on the response so the caller's fetch resolves."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        paths = ("/api/debug/clip-events", "/api/twitch/clips/record")
        for path in paths:
            r = await client.options(
                path,
                headers={
                    "Origin": "https://clips.twitch.tv",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert r.status_code == 200, path
            assert r.headers.get("access-control-allow-origin") == "https://clips.twitch.tv"
            assert "content-type" in (r.headers.get("access-control-allow-headers") or "").lower()
            assert "post" in (r.headers.get("access-control-allow-methods") or "").lower()
        # POST responses must carry ACAO too, or the request lands but the
        # content-script fetch rejects on the CORS response check.
        r = await client.post(
            "/api/debug/clip-events",
            json={"src": "ext", "event": "ext_start", "data": {"cors": True}},
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "https://clips.twitch.tv"
        r = await client.post(
            "/api/twitch/clips/record",
            json={"url": "https://clips.twitch.tv/SomeCorsSlug123"},
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "https://clips.twitch.tv"

# --- browser-path clip record (extension posts the published URL) ---------

@pytest.mark.anyio
async def test_clip_record_roundtrip_and_idempotent(_isolated_data_dir):
    """POST /api/twitch/clips/record adds a browser-path clip to history;
    re-posting the same clip slug (either URL shape) does not duplicate."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "url": "https://clips.twitch.tv/ColorfulBlindingEmuPicoMause-2ZWixeGOPIGpLrTL",
            "title": "yetz",
            "channel": "scriptingkata",
            "vod_id": "949802656",
            "offset_sec": 956,
            "duration_sec": 60,
        }
        r = await client.post("/api/twitch/clips/record", json=body)
        assert r.status_code == 200 and r.json()["ok"] is True
        r2 = await client.post(
            "/api/twitch/clips/record",
            json={**body, "url": "https://www.twitch.tv/scriptingkata/clip/ColorfulBlindingEmuPicoMause-2ZWixeGOPIGpLrTL"},
        )
        assert r2.status_code == 200
        rows = await _history(client)
        assert len(rows) == 1, "re-post must not duplicate the row"
        assert rows[0]["id"] == "ColorfulBlindingEmuPicoMause-2ZWixeGOPIGpLrTL"
        assert rows[0]["channel"] == "scriptingkata"
        assert rows[0]["vod_id"] == "949802656"
        assert rows[0]["duration_sec"] == 60
        assert rows[0]["url"] == "https://clips.twitch.tv/ColorfulBlindingEmuPicoMause-2ZWixeGOPIGpLrTL"


@pytest.mark.anyio
async def test_clip_record_validates(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for bad in ("https://twitch.tv/videos/123", "not-a-url", "https://clips.twitch.tv/"):
            r = await client.post("/api/twitch/clips/record", json={"url": bad})
            assert r.status_code == 422
        bad_ch = await client.post(
            "/api/twitch/clips/record",
            json={"url": "https://clips.twitch.tv/SomeSlug123", "channel": "not a login!"},
        )
        assert bad_ch.status_code == 422
        bad_dur = await client.post(
            "/api/twitch/clips/record",
            json={"url": "https://clips.twitch.tv/SomeSlug123", "duration_sec": 120},
        )
        assert bad_dur.status_code == 422
        assert (await _history(client)) == []


@pytest.mark.anyio
async def test_clip_record_accepts_query_strings_and_mobile_host(_isolated_data_dir):
    """Share links carry ?t=... and m.twitch.tv — both must record cleanly."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/twitch/clips/record",
            json={"url": "https://clips.twitch.tv/SlugOne?t=10s&tab=clips"},
        )
        assert r.status_code == 200 and r.json()["id"] == "SlugOne"
        r2 = await client.post(
            "/api/twitch/clips/record",
            json={"url": "https://m.twitch.tv/srdogg/clip/SlugTwo#clip"},
        )
        assert r2.status_code == 200 and r2.json()["id"] == "SlugTwo"
        rows = await _history(client)
        assert {row["id"] for row in rows} == {"SlugOne", "SlugTwo"}
        assert rows[0]["url"] == "https://clips.twitch.tv/SlugTwo"

def test_published_clip_range_from_gql_uses_offset_as_end():
    # Live 15s clip: GQL offset 886 duration 15 -> history END 886, start 871.
    assert published_clip_range_from_gql(886, 15) == (886, 15)
    # Live 19s clip: GQL offset 896 duration 18 -> END 896, start 878.
    assert published_clip_range_from_gql(896, 18) == (896, 18)


def test_published_clip_range_from_gql_early_vod_is_start():
    assert published_clip_range_from_gql(10, 19) == (29, 19)
    assert published_clip_range_from_gql(None, 19) is None
    assert published_clip_range_from_gql(100, 3) is None


@pytest.mark.anyio
async def test_record_prefers_twitch_gql_range(monkeypatch, _isolated_data_dir):
    monkeypatch.setattr(
        "services.twitch_gql_service.get_clip_info_sync",
        lambda url: {"vod_id": "2844207886", "offset_sec": 896, "duration": 18},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _record(client, {
            "url": "https://clips.twitch.tv/RelentlessDarkWrenYouDontSay-55K3Z0K_1ROMFuLm",
            "channel": "titiltei",
            "vod_id": "2844207886",
            "offset_sec": 90,
            "duration_sec": 19,
        })
        assert res.status_code == 200
        rows = await _history(client)
        assert rows[0]["offset_sec"] == 896
        assert rows[0]["duration_sec"] == 18
        assert rows[0]["vod_id"] == "2844207886"
