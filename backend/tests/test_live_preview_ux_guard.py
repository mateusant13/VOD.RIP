"""Live preview UX hardening — regression tests for the persona night shift.

Covers the two user-facing frictions found exercising the preview routes as a
real user (backend dev 7897):

1. POST /api/preview/live used to accept ANY http(s) URL — a channel page
   (twitch.tv/xqc) or a watch URL created a session that later served the
   Twitch HTML page through master.m3u8 with HTTP 200 while hls.js spun.
   Now: non-.m3u8 URLs are rejected 400 with an actionable reason.

2. GET /api/preview/session/{id}/status used to report all-green
   (mux_ready/playlist_ready/segment_buffer_ready true) even when the live
   upstream master/media resolve had already failed (403/404 upstream) — the
   player just spun. Now live sessions expose ``live_upstream_error`` ('' =
   healthy) recorded by the background prewarm, cleared on rotate.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from httpx import ASGITransport, AsyncClient

from app import app


@pytest.fixture()
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_live_rejects_channel_page_url(client):
    """A Twitch channel page is not an HLS playlist — 400 with a reason that
    tells the user what to do (resolve the master via /api/live first)."""
    resp = await client.post(
        "/api/preview/live",
        json={"url": "https://www.twitch.tv/xqc", "platform": "twitch"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert ".m3u8" in detail
    assert "/api/live/" in detail


async def test_live_rejects_watch_page_url(client):
    """A YouTube watch URL is not an HLS playlist — same 400 contract."""
    resp = await client.post(
        "/api/preview/live",
        json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "platform": "youtube",
        },
    )
    assert resp.status_code == 400


async def test_live_empty_url_keeps_original_error(client):
    resp = await client.post("/api/preview/live", json={"url": "", "platform": "twitch"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Live preview requires a master.m3u8 url"


def _live_session(platform: str = "twitch"):
    from services.preview_service import PreviewSession, get_session
    import services.preview.session as session_mod

    session = PreviewSession(
        session_id="a" * 16,
        vod_url="https://usher.ttvnw.net/api/channel/hls/xqc.m3u8",
        master_url="https://usher.ttvnw.net/api/channel/hls/xqc.m5u8",
        entry_url="",
        platform=platform,
        is_live=True,
    )
    session_mod.get_session = lambda sid: session
    return session


async def test_status_reports_live_upstream_error(client, monkeypatch):
    """When the background prewarm fails, the status poll must say so."""
    session = _live_session()
    session.live_upstream_error = (
        "upstream HTTP 403 for https://usher.ttvnw.net/api/channel/hls/xqc.m3u8"
    )
    resp = await client.get(f"/api/preview/session/{session.session_id}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "403" in body["live_upstream_error"]
    # The legacy fields stay untouched (VOD pollers rely on them).
    assert body["mux_ready"] is True


async def test_status_healthy_live_session_has_empty_error(client):
    session = _live_session()
    resp = await client.get(f"/api/preview/session/{session.session_id}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live_upstream_error"] == ""


async def test_status_vod_session_reports_empty_live_error(client):
    """Non-live sessions carry the field as '' (pydantic default) — the
    meaningful contract: only a failed live upstream yields non-empty."""
    session = _live_session()
    session.is_live = False
    resp = await client.get(f"/api/preview/session/{session.session_id}/status")
    assert resp.status_code == 200
    assert resp.json()["live_upstream_error"] == ""


async def test_rotate_clears_stale_upstream_error(monkeypatch):
    """A successful rotation swaps the upstream — the old failure reason is
    stale and must be cleared so the next poll re-evaluates."""
    import routers.preview as preview_router
    from services.preview_service import PreviewSession
    import services.preview.session as session_mod

    session = PreviewSession(
        session_id="b" * 16,
        vod_url="https://usher.ttvnw.net/api/channel/hls/xqc.m3u8",
        master_url="https://usher.ttvnw.net/api/channel/hls/xqc.m3u8",
        entry_url="",
        platform="twitch",
        is_live=True,
    )
    session.live_upstream_error = "upstream HTTP 403 for old master"
    monkeypatch.setattr(preview_router, "get_session", lambda sid: session)
    monkeypatch.setattr(
        "services.live_capture.probe_twitch_live_master",
        lambda login, **kw: {
            "url": f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?token=fresh",
            "headers": {"Referer": "https://www.twitch.tv/"},
            "player_type": "embed",
            "ad_free": False,
        },
    )
    out = preview_router._rotate_live_twitch_session(session.session_id, None)
    assert out["ok"] is True
    assert session.live_upstream_error is None


async def test_prewarm_failure_is_recorded_on_session(client, monkeypatch):
    """The background prewarm records its failure on the session (the same
    reason /status then exposes) instead of only logging at DEBUG."""
    from services.preview_service import create_live_session
    import services.preview.session as session_mod

    def fake_proxy_playlist(session_id, upstream_url):
        raise RuntimeError("upstream HTTP 403 for fake-dead-cdn.example")

    def fake_resolve_entry(session, entry_url, prefer_height=720):
        raise RuntimeError("upstream HTTP 403 for fake-dead-cdn.example")

    monkeypatch.setattr(
        "services.preview.hls.proxy_playlist", fake_proxy_playlist
    )
    monkeypatch.setattr(session_mod, "_resolve_preview_entry", fake_resolve_entry)
    monkeypatch.setattr(session_mod, "_hosts_for_url", lambda url: set())

    # create_live_session spawns a daemon prewarm thread that hits the fakes
    # through its private pool; give it a moment to record the failure.
    import time

    session = create_live_session(
        "https://usher.ttvnw.net/api/channel/hls/xqc.m3u8", {}, "twitch"
    )
    for _ in range(60):
        if session.live_upstream_error:
            break
        time.sleep(0.05)
    try:
        assert "403" in (session.live_upstream_error or ""), session.live_upstream_error
    finally:
        # Never leak the throwaway session into the shared manager registry.
        session.closed = True
        import shutil

        shutil.rmtree(session.cache_dir, ignore_errors=True)
