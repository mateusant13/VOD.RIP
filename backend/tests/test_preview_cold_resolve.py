#!/usr/bin/env python3
"""Gap 1 regression: cold YouTube resolve must fail fast, never grind in-request.

The 06:35 probe saw POST /api/preview/session block 45s (504) then 503 on
retry. Root cause: create_session slept 5-15s and re-ran the whole extract
chain *inside the request* on soft-negative (bot-gate) errors. The loop is
gone — the first failure raises, the router maps it to 503 + Retry-After, and
the 30s negative cache + frontend bounded retries cover transience.

Also pins the new concurrent-cold-create dedup (leader resolves, followers
reuse the snapshot — one resolve per video) and the resolve_ms field.

Run: python -m pytest backend/tests/test_preview_cold_resolve.py -x -q
"""
from __future__ import annotations

import os
import sys
import threading
import time

_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

import pytest
from fastapi.testclient import TestClient

from app import app
from routers import preview as preview_router
import services.preview.session as session_mod

YT_URL = "https://www.youtube.com/watch?v=aexkXGl9Gr4"
VID = "aexkXGl9Gr4"

def _stub_youtube_entry(monkeypatch):
    """Neutralize the warm waits / snapshot reuse / finalize side effects."""
    import services.preview.warm as warm_mod

    monkeypatch.setattr(
        session_mod, "_youtube_preview_is_anonymous", lambda: False
    )
    monkeypatch.setattr(
        session_mod, "await_youtube_warm_if_pending", lambda *a, **k: None
    )
    monkeypatch.setattr(
        session_mod, "_await_youtube_warm_catchup", lambda *a, **k: None
    )
    monkeypatch.setattr(
        session_mod, "_finalize_youtube_session", lambda s, _c: s
    )
    monkeypatch.setattr(warm_mod, "_await_pot_readiness_once", lambda *a, **k: None)


def test_soft_neg_raises_without_in_request_sleep(monkeypatch):
    """A bot-gated resolve must propagate immediately — no 5-15s in-request sleep."""

    def _boom(*_a, **_k):
        raise RuntimeError("Sign in to confirm you're not a bot")

    def _no_sleep(*_a, **_k):
        raise AssertionError("create_session slept in-request (Gap 1 regression)")

    _stub_youtube_entry(monkeypatch)
    monkeypatch.setattr(session_mod, "_get_session_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "resolve_stream_info", _boom)
    monkeypatch.setattr(session_mod.time, "sleep", _no_sleep)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="not a bot"):
        session_mod._manager.create_session(YT_URL, 0.0, 0.0, prefer_height=360)
    assert time.monotonic() - t0 < 2.0, "cold soft-neg must fail fast, not grind"


def test_concurrent_cold_creates_resolve_once(monkeypatch):
    """Two simultaneous cold clicks on one video: leader resolves, follower reuses."""
    _stub_youtube_entry(monkeypatch)

    leader_done = threading.Event()
    resolve_calls = []
    lock = threading.Lock()

    def _fake_resolve(url, prefer_height=360, **_k):
        with lock:
            resolve_calls.append(url)
        # hold the leader so the follower is guaranteed to hit the inflight wait
        leader_done.wait(5.0)
        return (VID, prefer_height, {"session_id": "snap-dedup", "fake": True})

    def _fake_get_snapshot(vid, height):
        # cold check returns None; after the leader stores, the snapshot exists
        if leader_done.is_set():
            return {"session_id": "snap-dedup", "fake": True}
        return None

    monkeypatch.setattr(
        session_mod, "_resolve_and_cache_youtube_snapshot", _fake_resolve
    )
    monkeypatch.setattr(session_mod, "_get_session_snapshot", _fake_get_snapshot)
    monkeypatch.setattr(
        session_mod.PreviewManager,
        "_reuse_youtube_snapshot",
        lambda self, url, cs, ce, ph, snap, anonymous=False: types_Sentinel(),
    )

    results = {}

    def _click(name):
        results[name] = session_mod._manager.create_session(
            YT_URL, 0.0, 0.0, prefer_height=360
        )

    t1 = threading.Thread(target=_click, args=("a",))
    t2 = threading.Thread(target=_click, args=("b",))
    t1.start()
    time.sleep(0.2)  # ensure t1 registers as leader first
    t2.start()
    time.sleep(0.4)  # follower is now waiting on the leader's event
    leader_done.set()
    t1.join(10)
    t2.join(10)

    assert not t1.is_alive() and not t2.is_alive(), "create_session threads wedged"
    assert len(results) == 2
    assert len(resolve_calls) == 1, (
        f"expected exactly one resolve (dedup), got {len(resolve_calls)}"
    )


def types_Sentinel():
    """Minimal stand-in for a PreviewSession (reuse path is stubbed)."""
    return type("_S", (), {"session_id": "dedup-sentinel"})()


def test_503_carry_retry_after_header(monkeypatch):
    """Transient-gate 503 must tell the client when to come back (Retry-After: 30)."""

    def _gated(*_a, **_k):
        raise RuntimeError("YouTube preview unavailable for this video")

    monkeypatch.setattr(preview_router, "create_session_pending", _gated)
    with TestClient(app) as c:
        resp = c.post("/api/preview/session", json={"url": YT_URL})
    assert resp.status_code == 503, resp.text[:200]
    assert resp.headers.get("retry-after") == "30"
    assert "temporarily restricted" in resp.json()["detail"].lower()


def test_resolve_ms_surfaces_on_create(monkeypatch):
    """POST create response carries the server-side resolve wall time."""

    class _FakeSession:
        session_id = "sess-resolve-ms-0001"
        kind = "progressive"
        platform = "YouTube"
        vod_url = YT_URL
        master_url = "/api/preview/hls/sess-resolve-ms-0001/master.m3u8"
        entry_url = "https://example.com/v.mp4"
        crop_start = 0.0
        crop_end = 0.0
        vod_duration = 120.0
        variant_entries = [(360, "https://example.com/v.mp4")]
        http_headers = {}
        cache_dir = None
        active_height = 360
        anonymous = True
        is_live = False
        growing_vod = False
        custom_master = None
        variant_muxed = {360: True}
        preview_audio_url = None
        prefer_height = 360
        dash_window_hls = False
        mux_status = "ready"
        extract_source = "test"
        cached_progressive_path = None
        window_hls_mux_start = 0.0
        window_hls_mux_end = 0.0
        archive_entry_url = None
        archive_duration = 0.0
        allowed_hosts = {"example.com"}
        timing_created_mono = 0.0

    def _slow(*_a, **_k):
        time.sleep(0.25)
        return _FakeSession()

    monkeypatch.setattr(preview_router, "create_session_pending", _slow)
    monkeypatch.setattr(
        preview_router, "_priority_transcribe_for_preview", lambda *_a, **_k: None
    )
    with TestClient(app) as c:
        resp = c.post("/api/preview/session", json={"url": YT_URL})
    assert resp.status_code == 200, resp.text[:300]
    resolve_ms = resp.json()["resolve_ms"]
    assert 200.0 <= resolve_ms < 5000.0, f"resolve_ms={resolve_ms} out of range"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-x", "-q"]))


def test_snapshot_is_reused_across_heights():
    """A warm at 360 must serve a click at 720. The snapshot carries the whole
    variant ladder, so the height key is a preference, not an identity - the
    mismatch was the NORMAL case (client warms at PREVIEW_FAST_START_HEIGHT=360,
    click sends its own height) and cost a full cold resolve: measured
    2026-10-11, 11 ms reuse at 360 vs 6,342 ms at 720 for the same video.
    """
    import services.preview.warm as warm_mod

    vid = "XACROSSHEIGHT"
    snap = {"kind": "hls"}
    warm_mod._put_session_snapshot(vid, 360, snap)
    assert warm_mod._get_session_snapshot(vid, 720) is None, "exact key must miss"
    got = warm_mod._get_session_snapshot_closest(vid, 720)
    assert got is not None, "the warm at 360 was not offered to a 720 click"
    stored_height, stored_snap = got
    assert stored_height == 360 and stored_snap is snap
    # Ties prefer the lower height so the first frame never upscales.
    warm_mod._put_session_snapshot(vid, 720, snap)
    assert warm_mod._get_session_snapshot_closest(vid, 540)[0] == 360
    # A different video is never served from it.
    assert warm_mod._get_session_snapshot_closest("OTHERVIDEOID", 720) is None


def test_resolved_stream_cache_is_reused_across_heights():
    """The Twitch resolve is the master playlist + full variant list, so a cached
    entry at one height serves every other height. Keyed strictly per height, a
    quality change re-resolved: measured 2026-10-11, 192 ms cached vs 4,667 ms at
    another height on the same VOD 15 s later.
    """
    import services.preview.warm as warm_mod

    value = ("https://master.example/vod.m3u8", {"User-Agent": "x"}, [{"height": 720}])
    warm_mod._put_resolved_stream_cache("twvod:999:360", value)
    got = warm_mod._get_resolved_stream_cached_any_height("twvod:999:", 1080)
    assert got is not None, "a cached 360 resolve was not offered to a 1080 click"
    assert got[0] == value[0]
    # Another video must not be served from this cache entry.
    assert warm_mod._get_resolved_stream_cached_any_height("twvod:123:", 1080) is None
