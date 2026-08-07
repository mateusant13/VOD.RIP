"""Official-API hybrid — Twitch helix layer (issue #4).

Unit tests (no network): helix PRIMARY when a token exists, GQL when
absent, silent GQL fallback on any helix error / missing VOD, and the
cookie-bridge auto-lift rules (fill when empty, never clobber a newer
manual paste, replace when the cookie export is newer).

Run from backend/: python -m pytest tests/test_twitch_helix_hybrid.py
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from models.schemas import AppSettings

# Isolate appdata before any settings/DB singleton binds (mirrors conftest).
_TMP = Path(tempfile.mkdtemp(prefix="helix-hybrid-"))
os.environ.setdefault("VODRIP_APP_DATA", str(_TMP / "VOD.RIP"))
os.environ.setdefault("VODRIP_ARCHIVE_DB", str(_TMP / "archive.db"))

from services import twitch_gql_service as tgs  # noqa: E402
from services import twitch_helix_service as ths  # noqa: E402


class _FakeMgr:
    """deps.settings_mgr stand-in: returns an AppSettings, records saves."""

    def __init__(self, settings=None):
        self._s = settings if settings is not None else AppSettings()
        self.saved: list[AppSettings] = []

    def get(self):
        return self._s

    def save(self, s):
        self.saved.append(s)
        self._s = s


HELIX_USERS = {"data": [{"id": "12345", "login": "cellbit", "display_name": "Cellbit"}]}
HELIX_VIDEOS = {"data": [{
    "id": "111",
    "user_id": "12345",
    "user_login": "cellbit",
    "user_name": "Cellbit",
    "title": "VOD 1",
    "created_at": "2026-08-01T00:00:00Z",
    "url": "https://www.twitch.tv/videos/111",
    "thumbnail_url": "https://cdn.example/thumb0-%{width}x%{height}.jpg",
    "viewable": "public",
    "view_count": 42,
    "language": "pt",
    "type": "archive",
    "duration": "4h21m33s",
    "game_id": "509658",
    "game_name": "Just Chatting",
}]}

GQL_LIST_DATA = {"user": {"videos": {"edges": [{"node": {
    "id": "222",
    "title": "GQL VOD",
    "createdAt": "2026-08-02T00:00:00Z",
    "lengthSeconds": 120,
    "viewCount": 7,
    "previewThumbnailURL": "https://gql.example/thumb.jpg",
    "language": "en",
}}]}}}

GQL_INFO_DATA = {"video": {
    "id": "222",
    "title": "GQL VOD",
    "createdAt": "2026-08-02T00:00:00Z",
    "lengthSeconds": 120,
    "viewCount": 7,
    "previewThumbnailURL": "https://gql.example/thumb.jpg",
    "game": {"displayName": "GQL Game"},
    "owner": {"displayName": "cellbit", "login": "cellbit"},
}}

_NO_PLAYBACK = (None, {}, [])


def _fake_settings(token: str) -> AppSettings:
    s = AppSettings()
    s.twitch_helix_token = token
    if token:
        s.twitch_helix_token_updated_at = time.time()
    return s


# --- list_channel_videos_sync --------------------------------------------

def test_helix_used_when_token_present(monkeypatch):
    """Token set -> helix serves the listing, GQL must NOT be reached."""
    calls = []
    monkeypatch.setattr(ths, "_helix_get", lambda path, params: (
        HELIX_USERS if path == "/users" else HELIX_VIDEOS
    ))
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: calls.append(a) or GQL_LIST_DATA)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))

    rows = tgs.list_channel_videos_sync("cellbit", limit=5)
    assert calls == [], "GQL must not run when helix serves"
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "111"
    assert row["duration"] == 4 * 3600 + 21 * 60 + 33
    assert row["duration_string"] == "4:21:33"
    assert row["language"] == "pt"
    assert row["created_at"] == "2026-08-01T00:00:00Z"
    assert row["thumbnail_url"] == "https://cdn.example/thumb0-320x180.jpg"
    assert row["url"] == "https://www.twitch.tv/videos/111"


def test_gql_used_when_token_absent(monkeypatch):
    """No token -> GQL as today, helix never called."""
    def _fail_helix(*a, **k):
        raise AssertionError("helix must not be called without a token")

    monkeypatch.setattr(ths, "_helix_get", _fail_helix)
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: GQL_LIST_DATA)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("")))

    rows = tgs.list_channel_videos_sync("cellbit", limit=5)
    assert len(rows) == 1 and rows[0]["id"] == "222"


def test_helix_error_falls_back_to_gql(monkeypatch):
    """Helix raises (401/429/5xx) -> silent GQL fallback."""
    def _boom(*a, **k):
        raise RuntimeError("Twitch Helix HTTP 401: bad token")

    monkeypatch.setattr(ths, "_helix_get", _boom)
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: GQL_LIST_DATA)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("stale-token")))

    rows = tgs.list_channel_videos_sync("cellbit", limit=5)
    assert len(rows) == 1 and rows[0]["id"] == "222"


def test_helix_empty_user_falls_back_to_gql(monkeypatch):
    """Helix user lookup empty (channel not found) -> GQL fallback."""
    monkeypatch.setattr(ths, "_helix_get", lambda path, params: {"data": []})
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: GQL_LIST_DATA)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))

    rows = tgs.list_channel_videos_sync("nobody", limit=5)
    assert len(rows) == 1 and rows[0]["id"] == "222"


# --- get_video_info_sync --------------------------------------------------

def _patch_playback(monkeypatch):
    monkeypatch.setattr(tgs, "get_vod_playback_sync", lambda vid: (
        "https://usher.example/master.m3u8",
        {"Referer": "https://www.twitch.tv/"},
        [{"height": 1080, "format_id": "1080p60", "tbr": 6000, "fps": 60}],
    ))
    monkeypatch.setattr(tgs, "_twitch_vod_playback_for_estimate", lambda vid: _NO_PLAYBACK)


def test_info_helix_used_when_token_present(monkeypatch):
    calls = []
    monkeypatch.setattr(ths, "_helix_get", lambda path, params: HELIX_VIDEOS)
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: calls.append(a) or GQL_INFO_DATA)
    _patch_playback(monkeypatch)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))

    payload = tgs.get_video_info_sync("111")
    assert calls == [], "GQL must not run when helix serves"
    assert payload["id"] == "111"
    assert payload["title"] == "VOD 1"
    assert payload["channel"] == "cellbit"
    assert payload["category"] == "Just Chatting"
    assert payload["duration"] == 4 * 3600 + 21 * 60 + 33
    assert payload["duration_string"] == "4:21:33"
    assert payload["created_at"] == "2026-08-01T00:00:00Z"
    assert payload["size_by_quality"], "enrich must still run after helix metadata"


def test_info_helix_missing_video_falls_back_to_gql(monkeypatch):
    """Helix returns empty data (missing VOD) -> GQL fallback."""
    monkeypatch.setattr(ths, "_helix_get", lambda path, params: {"data": []})
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: GQL_INFO_DATA)
    _patch_playback(monkeypatch)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))

    payload = tgs.get_video_info_sync("222")
    assert payload["id"] == "222" and payload["title"] == "GQL VOD"


def test_info_helix_error_falls_back_to_gql(monkeypatch):
    """Helix rate-limited -> silent GQL fallback."""
    def _boom(*a, **k):
        raise RuntimeError("Twitch Helix HTTP 429")

    monkeypatch.setattr(ths, "_helix_get", _boom)
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: GQL_INFO_DATA)
    _patch_playback(monkeypatch)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))

    payload = tgs.get_video_info_sync("222")
    assert payload["id"] == "222" and payload["category"] == "GQL Game"


def test_info_no_token_uses_gql(monkeypatch):
    def _fail_helix(*a, **k):
        raise AssertionError("helix must not be called without a token")

    monkeypatch.setattr(ths, "_helix_get", _fail_helix)
    monkeypatch.setattr(tgs, "_gql_request", lambda *a, **k: GQL_INFO_DATA)
    _patch_playback(monkeypatch)
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("")))

    payload = tgs.get_video_info_sync("222")
    assert payload["id"] == "222" and payload["channel"] == "cellbit"


# --- auto-lift (cookie bridge) --------------------------------------------

def _lift_env(monkeypatch, tmp_path, *, token, mtime):
    """Seed the cookie bridge + a real export file, return the fake mgr."""
    txt = tmp_path / "twitch.txt"
    txt.write_text(f"auth-token\t{token}\n", encoding="utf-8")
    os.utime(txt, (mtime, mtime))
    monkeypatch.setattr(
        "services.cookie_bridge.resolve_cookiefile", lambda platform: str(txt)
    )
    monkeypatch.setattr(
        "services.cookie_bridge.cookie_dict",
        lambda platform: {"auth-token": token},
    )
    return _FakeMgr()


def test_auto_lift_fills_empty_field(monkeypatch, tmp_path):
    mgr = _lift_env(monkeypatch, tmp_path, token="cookie-tok", mtime=time.time())
    mgr._s = _fake_settings("")  # empty field
    monkeypatch.setattr("deps.settings_mgr", mgr)

    assert ths.auto_lift_token() is True
    saved = mgr.saved[-1]
    assert saved.twitch_helix_token == "cookie-tok"
    assert saved.twitch_helix_token_updated_at > 0


def test_auto_lift_never_clobbers_newer_manual_paste(monkeypatch, tmp_path):
    now = time.time()
    mgr = _lift_env(monkeypatch, tmp_path, token="cookie-tok", mtime=now - 3600)
    s = _fake_settings("manual-tok")  # pasted 10 minutes ago
    s.twitch_helix_token_updated_at = now - 600
    mgr._s = s
    monkeypatch.setattr("deps.settings_mgr", mgr)

    assert ths.auto_lift_token() is False
    assert mgr._s.twitch_helix_token == "manual-tok", "manual paste must survive"


def test_auto_lift_replaces_when_cookie_is_newer(monkeypatch, tmp_path):
    now = time.time()
    mgr = _lift_env(monkeypatch, tmp_path, token="fresh-cookie-tok", mtime=now)
    s = _fake_settings("old-manual-tok")
    s.twitch_helix_token_updated_at = now - 7200
    mgr._s = s
    monkeypatch.setattr("deps.settings_mgr", mgr)

    assert ths.auto_lift_token() is True
    assert mgr.saved[-1].twitch_helix_token == "fresh-cookie-tok"


def test_auto_lift_no_cookies_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr("services.cookie_bridge.resolve_cookiefile", lambda p: None)
    monkeypatch.setattr("services.cookie_bridge.cookie_dict", lambda p: None)
    mgr = _FakeMgr(_fake_settings(""))
    monkeypatch.setattr("deps.settings_mgr", mgr)

    assert ths.auto_lift_token() is False
    assert mgr.saved == []


def test_auto_lift_same_token_is_noop(monkeypatch, tmp_path):
    mgr = _lift_env(monkeypatch, tmp_path, token="same-tok", mtime=time.time())
    s = _fake_settings("same-tok")
    s.twitch_helix_token_updated_at = time.time() - 10
    mgr._s = s
    monkeypatch.setattr("deps.settings_mgr", mgr)

    assert ths.auto_lift_token() is False
    assert mgr.saved == []


def test_auto_lift_disabled_bridge_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr("services.cookie_bridge.resolve_cookiefile", lambda p: None)
    monkeypatch.setattr("services.cookie_bridge.cookie_dict", lambda p: None)
    mgr = _FakeMgr(_fake_settings(""))
    monkeypatch.setattr("deps.settings_mgr", mgr)
    assert ths.auto_lift_token() is False
    assert mgr.saved == []


# --- clip support: _helix_post / token_info / HelixError -------------------

class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _patch_urlopen(monkeypatch, payload: bytes = b'{"data": []}', http_error=None):
    """Replace urllib.request.urlopen, capturing the built Request."""
    captured = {}

    def _urlopen(req, timeout=None):
        captured["req"] = req
        if http_error is not None:
            raise http_error
        return _FakeResp(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return captured


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.twitch.tv/helix/x", code, "err", {}, io.BytesIO(body)
    )


def test_helix_post_sends_json_body_with_token_client(monkeypatch):
    """POST carries Client-Id, Bearer token, JSON body; response parsed."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))
    captured = _patch_urlopen(monkeypatch, payload=b'{"data": [{"id": "c1"}]}')

    out = ths._helix_post("/videos/clips", {"video_id": "1", "vod_offset": 30}, "app-9")

    req = captured["req"]
    hdr = {k.lower(): v for k, v in req.headers.items()}
    assert req.full_url == "https://api.twitch.tv/helix/videos/clips"
    assert req.get_method() == "POST"
    assert hdr["client-id"] == "app-9"
    assert hdr["authorization"] == "Bearer tok-123"
    assert hdr["content-type"] == "application/json"
    assert json.loads(req.data.decode("utf-8")) == {"video_id": "1", "vod_offset": 30}
    assert out == {"data": [{"id": "c1"}]}


def test_helix_post_query_params_no_body(monkeypatch):
    """Clip endpoints pass their fields as URL query params (no JSON body)."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))
    captured = _patch_urlopen(monkeypatch, payload=b'{"data": [{"id": "c1"}]}')

    out = ths._helix_post(
        "/videos/clips",
        params={"broadcaster_id": "98765", "editor_id": "42", "title": "EPIC play"},
        client_id="app-9",
    )

    req = captured["req"]
    hdr = {k.lower(): v for k, v in req.headers.items()}
    assert req.full_url == (
        "https://api.twitch.tv/helix/videos/clips"
        "?broadcaster_id=98765&editor_id=42&title=EPIC+play"
    )
    assert req.get_method() == "POST"
    assert hdr["client-id"] == "app-9"
    assert hdr["authorization"] == "Bearer tok-123"
    assert "content-type" not in hdr, "no JSON body -> no Content-Type header"
    assert req.data is None
    assert out == {"data": [{"id": "c1"}]}


def test_helix_post_http_error_raises_helix_error_with_status(monkeypatch):
    """Helix 403 -> HelixError carrying status + raw detail (RuntimeError subclass)."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))
    _patch_urlopen(
        monkeypatch,
        http_error=_http_error(403, b'{"error":"Forbidden","status":403,"message":"Missing scope"}'),
    )

    with pytest.raises(ths.HelixError) as ei:
        ths._helix_post("/videos/clips", {"video_id": "1"}, "app-9")
    assert ei.value.status == 403
    assert "Missing scope" in ei.value.message
    assert isinstance(ei.value, RuntimeError), "must stay a RuntimeError for GQL-fallback callers"


def test_helix_get_accepts_client_id_override(monkeypatch):
    """_helix_get honors an explicit client_id (used by the clip router)."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))
    captured = _patch_urlopen(monkeypatch, payload=b'{"data": [{"id": "98765"}]}')

    ths._helix_get("/users", {"login": "surtepi"}, client_id="my-app")

    req = captured["req"]
    hdr = {k.lower(): v for k, v in req.headers.items()}
    assert hdr["client-id"] == "my-app"
    assert hdr["authorization"] == "Bearer tok-123"


def test_token_info_returns_validate_payload(monkeypatch):
    """oauth2/validate payload surfaces client_id + user_id + scopes for the clip router."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("tok-123")))
    payload = json.dumps({
        "client_id": "app-9",
        "login": "surtepi",
        "user_id": "591091436",
        "scopes": ["clips:edit", "user_read"],
        "expires_in": 5000,
    }).encode("utf-8")
    captured = _patch_urlopen(monkeypatch, payload=payload)

    out = ths.token_info()

    assert captured["req"].full_url == "https://id.twitch.tv/oauth2/validate"
    hdr = {k.lower(): v for k, v in captured["req"].headers.items()}
    assert hdr["authorization"] == "Bearer tok-123"
    assert out["client_id"] == "app-9"
    assert out["user_id"] == "591091436"
    assert out["scopes"] == ["clips:edit", "user_read"]


def test_token_info_invalid_token_raises_helix_error(monkeypatch):
    """Expired/invalid token -> HelixError(401) -> router maps to unauthorized."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("stale")))
    _patch_urlopen(monkeypatch, http_error=_http_error(401, b'{"status":401,"message":"invalid token"}'))

    with pytest.raises(ths.HelixError) as ei:
        ths.token_info()
    assert ei.value.status == 401


def test_token_info_without_token_raises_runtime_error(monkeypatch):
    """No stored token -> RuntimeError (router pre-checks token_available first)."""
    monkeypatch.setattr("deps.settings_mgr", _FakeMgr(_fake_settings("")))
    with pytest.raises(RuntimeError):
        ths.token_info()
