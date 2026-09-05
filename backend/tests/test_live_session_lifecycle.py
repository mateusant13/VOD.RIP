"""Live session lifecycle — regression tests for the persona night shift round 2.

Friction found exercising the preview routes as a real user (backend dev 7897):
sessions silently vanish after SESSION_TTL_SEC of no access (or LRU eviction)
with no warning surface — the client had no way to see the end coming, and the
only lifecycle signal was a bare 404 after the fact.

Now GET /api/preview/session/{id}/status and POST /api/preview/live/rotate
carry ``expires_in`` (seconds until the wipe; 0 = already eligible), computed
from a PRE-TOUCH snapshot of last_access: the poll itself touches the session
(extends the TTL), so reading the value after get_session would always report
the full constant and hide the real countdown.
"""
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
import services.preview.session as session_mod
from services.preview.session import SESSION_TTL_SEC, PreviewSession, _manager


@pytest.fixture()
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _real_session_lookup():
    """test_live_preview_ux_guard._live_session patches
    ``session_mod.get_session`` module-globally without restoring it; re-bind
    the real manager method so these tests exercise the real registry."""
    session_mod.get_session = _manager.get_session
    yield


@pytest.fixture()
def no_auto_cleanup(monkeypatch):
    """Freeze _maybe_cleanup so an over-TTL session survives until the test
    triggers the sweep explicitly (deterministic 0-expiry / 404 assertions)."""
    monkeypatch.setattr(_manager, "_cleanup_interval", 10 ** 9)
    monkeypatch.setattr(_manager, "_last_cleanup_time", time.monotonic())


def _register_session(**kw) -> PreviewSession:
    """Insert a real session into the real PreviewManager registry."""
    sid = uuid.uuid4().hex[:16]
    cache_dir = Path(tempfile.mkdtemp(prefix="vodrip-lifecycle-test-"))
    session = PreviewSession(
        session_id=sid,
        vod_url=kw.get(
            "vod_url",
            "https://usher.ttvnw.net/api/channel/hls/gaules.m3u8?token=x",
        ),
        master_url=kw.get(
            "master_url",
            "https://usher.ttvnw.net/api/channel/hls/gaules.m3u8?token=x",
        ),
        entry_url="",
        platform=kw.get("platform", "twitch"),
        is_live=kw.get("is_live", True),
        cache_dir=cache_dir,
    )
    with _manager._lock:
        _manager._sessions[sid] = session
    return session


def _unregister(session: PreviewSession) -> None:
    """Drop the session so no test ever wipes a sibling's cache dir via the
    cleanup sweep, and remove its scratch dir."""
    with _manager._lock:
        _manager._sessions.pop(session.session_id, None)
    shutil.rmtree(session.cache_dir, ignore_errors=True)


async def test_status_reports_expires_in_for_fresh_session(client, no_auto_cleanup):
    """A just-created session reports a full TTL budget on /status."""
    session = _register_session()
    try:
        resp = await client.get(
            f"/api/preview/session/{session.session_id}/status"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert SESSION_TTL_SEC - 2 <= body["expires_in"] <= SESSION_TTL_SEC
    finally:
        _unregister(session)


async def test_status_expires_in_counts_real_idle_age(client, no_auto_cleanup):
    """THE regression: expires_in reflects the pre-poll idle age, not the
    constant. The poll touches the session (renews the TTL) — reading the
    countdown after that touch would always say SESSION_TTL_SEC and hide the
    approach of the silent wipe."""
    session = _register_session()
    try:
        session.last_access = time.time() - 500
        resp = await client.get(
            f"/api/preview/session/{session.session_id}/status"
        )
        assert resp.status_code == 200
        assert 1295 <= resp.json()["expires_in"] <= 1300
    finally:
        _unregister(session)


async def test_status_expires_in_zero_when_ttl_elapsed(client, no_auto_cleanup):
    """Past the TTL but not yet swept → 0 = removal is due at the next sweep."""
    session = _register_session()
    try:
        session.last_access = time.time() - (SESSION_TTL_SEC + 100)
        resp = await client.get(
            f"/api/preview/session/{session.session_id}/status"
        )
        assert resp.status_code == 200
        assert resp.json()["expires_in"] == 0
    finally:
        _unregister(session)


async def test_status_404_after_expiry_sweep(client, no_auto_cleanup):
    """The lifecycle end users actually see: once the sweep removes the
    session, /status is a 404 with the 'not found or expired' reason."""
    session = _register_session()
    try:
        session.last_access = time.time() - (SESSION_TTL_SEC + 100)
        _manager._cleanup_stale_sessions()
        resp = await client.get(
            f"/api/preview/session/{session.session_id}/status"
        )
        assert resp.status_code == 404
        assert "expired" in resp.json()["detail"].lower()
    finally:
        _unregister(session)


def test_rotate_response_includes_expires_in(monkeypatch):
    """Rotation response carries the remaining TTL (and the unchanged proxied
    master) so the client sees lifecycle state without a /status round-trip."""
    import routers.preview as preview_router

    session = _register_session()
    try:
        monkeypatch.setattr(
            "services.live_capture.probe_twitch_live_master",
            lambda login, **kw: {
                "url": (
                    "https://usher.ttvnw.net/api/channel/hls/"
                    f"{login}.m3u8?token=fresh"
                ),
                "headers": {"Referer": "https://www.twitch.tv/"},
                "player_type": "embed",
                "ad_free": False,
            },
        )
        out = preview_router._rotate_live_twitch_session(session.session_id, None)
        assert out["ok"] is True
        assert SESSION_TTL_SEC - 2 <= out["expires_in"] <= SESSION_TTL_SEC
        assert out["master_url"] == (
            f"/api/preview/hls/{session.session_id}/master.m3u8"
        )
    finally:
        _unregister(session)
