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

    monkeypatch.setattr(preview_router, "create_session", _gated)
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

    monkeypatch.setattr(preview_router, "create_session", _slow)
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
