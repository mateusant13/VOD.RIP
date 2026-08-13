"""Regression: the YouTube preview create POST must never hang forever.

The 'Starting YouTube preview…' spinner had no terminal event when
`create_session` wedged (stuck yt-dlp/innerTube pass occupying a
PREVIEW_EXECUTOR worker): the request hung until the client's own 60s fetch
timeout (x3 retries ≈ 3 min of spinner). The router now wraps the YouTube
create in a hard wall-clock timeout (VODRIP_PREVIEW_CREATE_TIMEOUT_SEC, default
45s) and returns 504. Non-YouTube creates are NOT capped.

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


def test_youtube_create_returns_504_when_create_session_hangs():
    """A wedged create_session must produce a prompt 504, not a hanging POST."""

    def _stuck(*_args, **_kwargs):
        time.sleep(60)  # simulates a wedged extract — must never return here
        raise AssertionError("stubbed create_session returned — timeout failed")

    # The env var is read at app-import time; in a full-suite run another
    # test imports the app first and the 0.5 never applies. Pin the module
    # constant directly so this test is order-independent (5.0 = the clamp
    # floor the env path would produce for 0.5).
    original_timeout = preview_router._YOUTUBE_CREATE_HARD_TIMEOUT_SEC
    preview_router._YOUTUBE_CREATE_HARD_TIMEOUT_SEC = 5.0
    original = preview_router.create_session
    preview_router.create_session = _stuck
    try:
        with TestClient(app) as c:
            t0 = time.monotonic()
            resp = c.post("/api/preview/session", json={"url": YT_URL})
            elapsed = time.monotonic() - t0
    finally:
        preview_router.create_session = original
        preview_router._YOUTUBE_CREATE_HARD_TIMEOUT_SEC = original_timeout

    assert resp.status_code == 504, (
        f"expected 504 from timed-out YouTube create, got {resp.status_code}: "
        f"{resp.text[:256]}"
    )
    # The constant clamps to a 5s floor, so the response lands ~5s — far
    # before the 60s stub could ever return on its own.
    assert elapsed < 12.0, f"504 arrived after {elapsed:.1f}s — timeout not applied"
    assert "timed out" in resp.json().get("detail", "").lower()


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
    test_youtube_create_returns_504_when_create_session_hangs()
    test_youtube_create_passthrough_when_fast()
    print("OK: youtube preview create timeout tests passed")
