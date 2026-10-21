"""Regression: preview session create POSTs must never hang forever.

The 'Starting preview…' spinner had no terminal event when
`create_session` wedged (stuck yt-dlp/InnerTube/CDN pass occupying a
PREVIEW_EXECUTOR worker): the request hung until the client's own 60s fetch
timeout. The router now wraps every preview create in a hard wall-clock
timeout (VODRIP_PREVIEW_CREATE_TIMEOUT_SEC, default 45s) and returns 504.

Run from anywhere: `python backend/tests/test_youtube_preview_create_timeout.py`
"""
from __future__ import annotations

# ponytail: anchor to backend/ so `from app import app` resolves.
import os
import sys
import time

# Set BEFORE importing the app — the router reads the constant at import time.
os.environ["VODRIP_PREVIEW_CREATE_TIMEOUT_SEC"] = "0.5"

_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from fastapi.testclient import TestClient
from app import app
from routers import preview as preview_router

YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TWITCH_URL = "https://www.twitch.tv/videos/123456789"


def test_preview_create_returns_504_when_create_session_hangs():
    """A wedged create_session must produce a prompt 504, not a hanging POST."""

    def _stuck(*_args, **_kwargs):
        time.sleep(60)  # simulates a wedged extract — must never return here
        raise AssertionError("stubbed create_session returned — timeout failed")

    # The env var is read at app-import time; in a full-suite run another
    # test imports the app first and the 0.5 never applies. Pin the module
    # constant directly so this test is order-independent (5.0 = the clamp
    # floor the env path would produce for 0.5).
    original_timeout = preview_router._PREVIEW_CREATE_HARD_TIMEOUT_SEC
    preview_router._PREVIEW_CREATE_HARD_TIMEOUT_SEC = 5.0
    original = preview_router.create_session
    preview_router.create_session = _stuck
    try:
        with TestClient(app) as c:
            for label, url in (("YouTube", YT_URL), ("Twitch", TWITCH_URL)):
                t0 = time.monotonic()
                resp = c.post("/api/preview/session", json={"url": url})
                elapsed = time.monotonic() - t0
                assert resp.status_code == 504, (
                    f"expected 504 from timed-out {label} create, got "
                    f"{resp.status_code}: {resp.text[:256]}"
                )
                assert elapsed < 12.0, f"{label} 504 arrived after {elapsed:.1f}s"
                assert "timed out" in resp.json().get("detail", "").lower()
    finally:
        preview_router.create_session = original
        preview_router._PREVIEW_CREATE_HARD_TIMEOUT_SEC = original_timeout


def test_youtube_create_passthrough_when_fast():
    """A fast YouTube create still returns 200 (timeout must not clip success)."""

    class _FakeSession:
        session_id = "sess-test-timeout-0001"
        kind = "hls"
        platform = "YouTube"
        vod_url = YT_URL
        master_url = "/api/preview/hls/sess-test-timeout-0001/master.m3u8"
        entry_url = "/api/preview/hls/sess-test-timeout-0001/master.m3u8"
        crop_start = 0.0
        crop_end = 0.0
        vod_duration = 120.0
        variant_entries = [(360, "/api/preview/hls/sess-test-timeout-0001/master.m3u8")]
        http_headers = {}
        cache_dir = None
        active_height = 360
        anonymous = True
        is_live = False
        channel_language = ""
        trim_timeline = False
        window_hls_mux_start = 0
        window_hls_mux_end = 0
        cached_progressive = False
        segment_buffer_ready = True
        playlist_ready = True
        mux_status = "ready"
        extract_source = "test"

    def _fast(*_args, **_kwargs):
        return _FakeSession()

    original = preview_router.create_session
    preview_router.create_session = _fast
    try:
        with TestClient(app) as c:
            resp = c.post("/api/preview/session", json={"url": YT_URL})
    finally:
        preview_router.create_session = original

    assert resp.status_code == 200, (
        f"expected 200 from fast stub create, got {resp.status_code}: "
        f"{resp.text[:256]}"
    )


if __name__ == "__main__":
    test_preview_create_returns_504_when_create_session_hangs()
    test_youtube_create_passthrough_when_fast()
    print("OK: youtube preview create timeout tests passed")


def test_a_follower_gets_the_longer_budget(monkeypatch):
    """Retry after a 504 must be allowed to wait for the leader it joined.

    Measured 2026-10-21: a cold YouTube click answered 504 at 8.0s and the SAME
    video answered in 0.04s two minutes later, from the snapshot the abandoned
    resolve filled. The client's retry arrives 1.2s after the 504 - while the
    leader is still working - so capping it with the leader's short budget
    guarantees a second failure for work that is already being done.
    """
    import threading

    class _FakeSession:
        session_id = "sess-follower-0001"
        kind = "progressive"
        platform = "YouTube"
        vod_url = YT_URL
        master_url = "/api/preview/hls/sess-follower-0001/master.m3u8"
        entry_url = "/api/preview/hls/sess-follower-0001/master.m3u8"
        crop_start = 0.0
        crop_end = 0.0
        vod_duration = 120.0
        variant_entries = [(360, "/api/preview/hls/sess-follower-0001/master.m3u8")]
        http_headers = {}
        cache_dir = None
        active_height = 360
        anonymous = False
        is_live = False
        channel_language = ""
        trim_timeline = False
        window_hls_mux_start = 0
        window_hls_mux_end = 0
        cached_progressive = False
        segment_buffer_ready = True
        playlist_ready = True
        mux_status = "ready"
        extract_source = "test"
        custom_master = False
        dash_window_hls = False
        prefer_height = 480
        stale = False

        def __getattr__(self, name):  # any other field reads as unset
            if name.startswith("__"):
                raise AttributeError(name)
            return None

    started = threading.Event()

    def _slow(*_args, **_kwargs):
        started.set()
        time.sleep(1.2)          # longer than the short cap, shorter than the follow budget
        return _FakeSession()

    monkeypatch.setattr(preview_router, "create_session", _slow)
    monkeypatch.setattr(preview_router, "youtube_create_inflight", lambda url: True)
    monkeypatch.setattr(preview_router, "_PREVIEW_CREATE_HARD_TIMEOUT_SEC", 0.4)
    monkeypatch.setattr(preview_router, "_PREVIEW_CREATE_FOLLOW_TIMEOUT_SEC", 10.0)
    with TestClient(app) as c:
        t0 = time.monotonic()
        resp = c.post("/api/preview/session", json={"url": YT_URL})
        elapsed = time.monotonic() - t0
    # What this pins is the BUDGET CHOICE: the follower outlived the 0.4s short
    # cap instead of being cut by it. The response shape is not the subject here
    # (the fake session is deliberately incomplete, and
    # test_youtube_create_passthrough_when_fast covers the 200 path with a
    # complete one), so any status but 504 passes.
    assert resp.status_code != 504, (
        f"follower was cut by the leader's short cap after {elapsed:.2f}s: "
        f"{resp.status_code} {resp.text[:160]}"
    )
    assert elapsed >= 1.0, f"follower returned before the leader finished ({elapsed:.2f}s)"


def test_a_leader_still_fails_fast(monkeypatch):
    """No leader in flight -> the short cap applies, so the UI never hangs."""
    def _stuck(*_args, **_kwargs):
        time.sleep(30)
        raise AssertionError("stub returned - the timeout failed to cut it")

    monkeypatch.setattr(preview_router, "create_session", _stuck)
    monkeypatch.setattr(preview_router, "youtube_create_inflight", lambda url: False)
    monkeypatch.setattr(preview_router, "_PREVIEW_CREATE_HARD_TIMEOUT_SEC", 0.4)
    monkeypatch.setattr(preview_router, "_PREVIEW_CREATE_FOLLOW_TIMEOUT_SEC", 10.0)
    with TestClient(app) as c:
        t0 = time.monotonic()
        resp = c.post("/api/preview/session", json={"url": YT_URL})
        elapsed = time.monotonic() - t0
    assert resp.status_code == 504, (resp.status_code, resp.text[:200])
    assert elapsed < 5.0, f"leader 504 arrived after {elapsed:.2f}s (short cap was 0.4s)"
