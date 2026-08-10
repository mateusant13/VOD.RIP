"""Tests for the Twitch clip creation + history endpoints.

POST /api/twitch/clip now creates clips through the official Helix API
(POST /helix/videos/clips for VODs, POST /helix/clips for live) using the
stored twitch_helix_token. Tests monkeypatch the helix service (token_info /
_helix_get / _helix_post) so nothing touches the network, and isolate
VODRIP_DATA_DIR so the real user history is never read or written.
"""
import json
from pathlib import Path
from typing import List

import pytest
from httpx import AsyncClient, ASGITransport

from app import app
from models.schemas import AppSettings
from services import twitch_helix_service as ths

CLIP_DATA = {"data": [{"id": "ClipId123", "edit_url": "https://clips.twitch.tv/ClipId123-edit"}]}


class _FakeMgr:
    """deps.settings_mgr stand-in: returns a settings object with the token."""

    def __init__(self, token: str):
        s = AppSettings()
        s.twitch_helix_token = token
        self._s = s

    def get(self):
        return self._s


@pytest.fixture()
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    return tmp_path


def _patch_token(monkeypatch, *, available=True, scopes=("editor:manage:clips",), client_id="app-123"):
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr("tok-123" if available else ""))

    def _info():
        if not available:
            raise RuntimeError("no twitch helix token configured")
        return {
            "client_id": client_id,
            "user_id": "591091436",
            "login": "surtepi",
            "scopes": list(scopes),
        }

    monkeypatch.setattr(ths, "token_info", _info)
    return _info


def _patch_users(monkeypatch, *, user_id="98765"):
    """Resolve broadcaster_login -> user id (GET /users), recording the call."""
    calls = []
    monkeypatch.setattr(
        ths, "_helix_get",
        lambda path, params, client_id=None: calls.append(("get", path, params, client_id))
        or {"data": [{"id": user_id, "login": "surtepi"}]},
    )
    return calls


async def _post(client, body):
    return await client.post("/api/twitch/clip", json=body)


async def _history(client):
    res = await client.get("/api/twitch/clips/history")
    assert res.status_code == 200
    return res.json()


# --- history: legacy /create rows are dropped -----------------------------

@pytest.mark.anyio
async def test_history_filters_legacy_create_urls(_isolated_data_dir):
    """Pre-Helix rows (clips.twitch.tv/create → Twitch's /clips/500 error page)
    are dropped from history; Helix-era rows survive."""
    legacy = {"id": "old1", "status": "editor_opened", "url": "https://clips.twitch.tv/create?vodID=2536167775&broadcasterLogin=surtepi&offsetSeconds=434"}
    live = {"id": "new1", "status": "created", "url": "https://clips.twitch.tv/ClipId123-edit"}
    path = Path(_isolated_data_dir) / "twitch_clips.json"
    path.write_text(json.dumps([legacy, live], ensure_ascii=False), "utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rows = await _history(client)

    assert rows == [live]


# --- VOD clips (POST /helix/videos/clips) ---------------------------------

@pytest.mark.anyio
async def test_vod_clip_calls_helix_and_records_history(monkeypatch, _isolated_data_dir):
    """VOD request -> /users lookup + /videos/clips query params -> {ok,id,edit_url} + history."""
    _patch_token(monkeypatch)
    user_calls = _patch_users(monkeypatch)
    post_calls = []
    monkeypatch.setattr(
        ths, "_helix_post",
        lambda path, body=None, client_id="", params=None:
            post_calls.append((path, body, client_id, params)) or CLIP_DATA,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "2536167775",
            "offset_sec": 434,
            "duration_sec": 30,
            "title": "EPIC play!",
        })
        assert res.status_code == 200
        assert res.json() == {"ok": True, "id": "ClipId123", "edit_url": "https://clips.twitch.tv/ClipId123-edit"}

        assert user_calls == [("get", "/users", {"login": "surtepi"}, "app-123")]
        assert post_calls == [(
            "/videos/clips",
            None,  # official shape: fields are URL query params, not a JSON body
            "app-123",
            {
                "broadcaster_id": "98765",
                "editor_id": "591091436",
                "vod_id": "2536167775",
                "vod_offset": 434,
                "duration": 30,
                "title": "EPIC play!",
            },
        )]

        rows = await _history(client)
        assert len(rows) == 1
        assert rows[0]["id"] == "ClipId123"
        assert rows[0]["channel"] == "surtepi"
        assert rows[0]["vod_id"] == "2536167775"
        assert rows[0]["offset_sec"] == 434
        assert rows[0]["duration_sec"] == 30
        assert rows[0]["title"] == "EPIC play!"
        assert rows[0]["url"] == "https://clips.twitch.tv/ClipId123-edit"
        assert rows[0]["status"] == "created"
        # persisted on disk, newest first
        stored = json.loads(
            (_isolated_data_dir / "twitch_clips.json").read_text("utf-8")
        )
        assert stored[0]["id"] == rows[0]["id"]


@pytest.mark.anyio
async def test_blank_title_defaults_to_broadcaster_login(monkeypatch, _isolated_data_dir):
    """Empty/whitespace title -> the endpoint requires one, so the broadcaster
    login is sent (and recorded) instead of omitting the field."""
    _patch_token(monkeypatch)
    _patch_users(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ths, "_helix_post",
        lambda path, body=None, client_id="", params=None: calls.append(params) or CLIP_DATA,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 60,
            "duration_sec": 30,
            "title": "   ",
        })
        assert res.status_code == 200
        assert calls == [{
            "broadcaster_id": "98765",
            "editor_id": "591091436",
            "vod_id": "1",
            "vod_offset": 60,
            "duration": 30,
            "title": "surtepi",
        }]
        rows = await _history(client)
        assert rows[0]["title"] == "surtepi"


@pytest.mark.anyio
async def test_vod_clip_unknown_broadcaster_is_not_found(monkeypatch, _isolated_data_dir):
    """Login not resolvable via /users -> not_found, no clip POST."""
    _patch_token(monkeypatch)
    monkeypatch.setattr(ths, "_helix_get", lambda *a, **k: {"data": []})
    monkeypatch.setattr(ths, "_helix_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "nobody_xyz",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        assert res.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_title_too_long_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
            "title": "x" * 141,
        })
        assert res.status_code == 422
        assert "title must be 140 chars" in res.json()["detail"]


@pytest.mark.anyio
async def test_vod_clip_missing_scope_is_rejected_before_post(monkeypatch, _isolated_data_dir):
    """Token without editor/channel clip scopes -> missing_scope, no Helix POST."""
    _patch_token(monkeypatch, scopes=("user_read",))
    posted = []
    monkeypatch.setattr(ths, "_helix_post", lambda *a, **k: posted.append(a) or CLIP_DATA)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "missing_scope"
        assert "editor:manage:clips" in body["error"]["message"]
        assert "Cookie Bridge" in body["error"]["message"], \
            "must explain the auto-lifted browser token cannot carry the clip scope"
        assert "Settings → Official APIs" in body["error"]["message"], \
            "must point to the token paste location"
        assert posted == [], "scope pre-check must run before any Helix POST"
        assert await _history(client) == []


@pytest.mark.anyio
async def test_no_token_is_no_token_error(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch, available=False)
    monkeypatch.setattr(ths, "_helix_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        assert res.json()["error"]["code"] == "no_token"


@pytest.mark.anyio
async def test_invalid_token_is_unauthorized(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch)

    def _info():
        raise ths.HelixError(401, '{"error":"Unauthorized","status":401,"message":"invalid token"}')

    monkeypatch.setattr(ths, "token_info", _info)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False and body["error"]["code"] == "unauthorized"
        assert "invalid token" in body["error"]["message"]


@pytest.mark.anyio
async def test_helix_429_maps_to_rate_limited(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch)
    _patch_users(monkeypatch)

    def _boom(*a, **k):
        raise ths.HelixError(429, '{"error":"Too Many Requests","status":429,"message":"rate limited"}')

    monkeypatch.setattr(ths, "_helix_post", _boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        assert res.json()["error"]["code"] == "rate_limited"
        assert await _history(client) == []


@pytest.mark.anyio
async def test_helix_503_maps_to_clip_failed(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch)
    _patch_users(monkeypatch)

    def _boom(*a, **k):
        raise ths.HelixError(503, '{"error":"Service Unavailable","status":503,"message":"busy"}')

    monkeypatch.setattr(ths, "_helix_post", _boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "1",
            "offset_sec": 30,
            "duration_sec": 30,
        })
        assert res.status_code == 200
        assert res.json()["error"]["code"] == "clip_failed"
        assert await _history(client) == []


# --- live clips (POST /helix/clips) ---------------------------------------

@pytest.mark.anyio
async def test_live_clip_resolves_broadcaster_and_posts_clips(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch, scopes=("clips:edit",))
    calls = []
    monkeypatch.setattr(
        ths, "_helix_get",
        lambda path, params, client_id: calls.append(("get", path, params, client_id)) or {
            "data": [{"id": "98765", "login": "surtepi"}]
        },
    )
    monkeypatch.setattr(
        ths, "_helix_post",
        lambda path, body=None, client_id="", params=None:
            calls.append(("post", path, body, client_id, params)) or CLIP_DATA,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {"broadcaster_login": "surtepi"})
        assert res.status_code == 200
        assert res.json() == {"ok": True, "id": "ClipId123", "edit_url": "https://clips.twitch.tv/ClipId123-edit"}

        assert calls == [
            ("get", "/users", {"login": "surtepi"}, "app-123"),
            ("post", "/clips", None, "app-123", {"broadcaster_id": "98765"}),
        ]
        rows = await _history(client)
        assert rows[0]["channel"] == "surtepi"
        assert rows[0]["vod_id"] is None
        assert rows[0]["status"] == "created"


@pytest.mark.anyio
async def test_live_clip_missing_scope_is_rejected(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch, scopes=("user_read",))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {"broadcaster_login": "surtepi"})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False and body["error"]["code"] == "missing_scope"
        assert "clips:edit" in body["error"]["message"]
        assert "Cookie Bridge" in body["error"]["message"]
        assert "Settings → Official APIs" in body["error"]["message"]


@pytest.mark.anyio
async def test_live_clip_unknown_broadcaster_is_not_found(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch, scopes=("clips:edit",))
    monkeypatch.setattr(ths, "_helix_get", lambda *a, **k: {"data": []})
    monkeypatch.setattr(ths, "_helix_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {"broadcaster_login": "nobody_xyz"})
        assert res.status_code == 200
        assert res.json()["error"]["code"] == "not_found"


# --- batch delete ----------------------------------------------------------

def _distinct_clip_data():
    """Helix mock returning a fresh clip id per call."""
    counter = 0

    def _post(*a, **k):
        nonlocal counter
        counter += 1
        return {
            "data": [{
                "id": f"ClipId{counter}",
                "edit_url": f"https://clips.twitch.tv/ClipId{counter}-edit",
            }]
        }

    return _post


async def _seed_clips(client, n: int = 3) -> List[dict]:
    for i in range(n):
        await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": str(1000 + i),
            "offset_sec": 30 + i,
            "duration_sec": 30,
        })
    return await _history(client)


@pytest.mark.anyio
async def test_batch_delete_removes_selected_ids(monkeypatch, _isolated_data_dir):
    """DELETE with ids removes exactly those entries; others survive on disk."""
    _patch_token(monkeypatch)
    _patch_users(monkeypatch)
    monkeypatch.setattr(ths, "_helix_post", _distinct_clip_data())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rows = await _seed_clips(client)
        assert len(rows) == 3

        res = await client.request(
            "DELETE", "/api/twitch/clips/history",
            json={"ids": [rows[0]["id"], rows[2]["id"]]},
        )
        assert res.status_code == 200
        assert res.json() == {"ok": True, "removed": 2}

        rows = await _history(client)
        assert len(rows) == 1
        stored = json.loads(
            (_isolated_data_dir / "twitch_clips.json").read_text("utf-8")
        )
        assert len(stored) == 1 and stored[0]["id"] == rows[0]["id"]


@pytest.mark.anyio
async def test_batch_delete_unknown_ids_is_noop(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch)
    _patch_users(monkeypatch)
    monkeypatch.setattr(ths, "_helix_post", _distinct_clip_data())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _seed_clips(client, n=2)
        res = await client.request(
            "DELETE", "/api/twitch/clips/history", json={"ids": ["nope-1", "nope-2"]}
        )
        assert res.status_code == 200
        assert res.json() == {"ok": True, "removed": 0}
        assert len(await _history(client)) == 2


@pytest.mark.anyio
async def test_batch_delete_rejects_empty_ids(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.request("DELETE", "/api/twitch/clips/history", json={"ids": []})
        assert res.status_code == 422
        res = await client.request("DELETE", "/api/twitch/clips/history", json={"ids": ["", " "]})
        assert res.status_code == 422


# --- request validation (unchanged behaviour) -----------------------------

@pytest.mark.anyio
async def test_duration_out_of_range_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for bad in (4, 0, 61):
            res = await _post(client, {
                "broadcaster_login": "surtepi",
                "vod_id": "2536167775",
                "offset_sec": 434,
                "duration_sec": bad,
            })
            assert res.status_code == 422
            assert "duration_sec must be 5..60" in res.json()["detail"]
        rows = await _history(client)
        assert rows == []


@pytest.mark.anyio
async def test_duration_at_min_boundary_accepted(monkeypatch, _isolated_data_dir):
    _patch_token(monkeypatch)
    _patch_users(monkeypatch)
    monkeypatch.setattr(ths, "_helix_post", lambda *a, **k: CLIP_DATA)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {
            "broadcaster_login": "surtepi",
            "vod_id": "2536167775",
            "offset_sec": 434,
            "duration_sec": 5,
        })
        assert res.status_code == 200


@pytest.mark.anyio
async def test_bad_login_rejected(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await _post(client, {"broadcaster_login": "not a login!"})
        assert res.status_code == 422
        rows = await _history(client)
        assert rows == []


# --- debugging event sink (clip flow replay) ------------------------------@pytest.mark.anyio
async def test_clip_events_sink_roundtrip(_isolated_data_dir):
    """POST /api/debug/clip-events appends a timestamped JSON line to
    <data_dir>/clip-events.log; GET reads it back in order."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/debug/clip-events",
            json={"src": "ext", "event": "ext_start",
                  "data": {"hostname": "clips.twitch.tv", "startSec": 300}},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        r = await client.post(
            "/api/debug/clip-events",
            json={"src": "app", "event": "browser_open",
                  "data": {"url": "https://clips.twitch.tv/create?x=1"}},
        )
        assert r.status_code == 200
        rows = (await client.get("/api/debug/clip-events")).json()
        assert len(rows) == 2
        assert rows[0]["src"] == "ext" and rows[0]["event"] == "ext_start"
        assert rows[0]["startSec"] == 300  # data flattened into the line
        assert rows[1]["event"] == "browser_open"
        assert rows[0]["ts"] and rows[1]["ts"]
        assert (_isolated_data_dir / "clip-events.log").exists()


@pytest.mark.anyio
async def test_clip_events_sink_validates(_isolated_data_dir):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad_src = await client.post("/api/debug/clip-events", json={"src": "nope", "event": "x"})
        assert bad_src.status_code == 422
        bad_event = await client.post("/api/debug/clip-events", json={"src": "ext", "event": ""})
        assert bad_event.status_code == 422
        big = await client.post(
            "/api/debug/clip-events",
            json={"src": "ext", "event": "x", "data": {"blob": "y" * 9000}},
        )
        assert big.status_code == 422
        assert (await client.get("/api/debug/clip-events")).json() == []

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
