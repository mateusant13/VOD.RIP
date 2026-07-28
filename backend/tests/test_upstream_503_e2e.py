"""Self-check: proxy_master returns 503 Retry-After when upstream returns 404/410.

Run from anywhere: `python backend/tests/test_upstream_503_e2e.py`
(Requires pytest + pytest-asyncio for the async test).
"""
import os
import sys

# ponytail: anchor to backend/ so `from app import app` and `from services.X`
# resolve identically to `run.py`'s import surface.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest
from httpx import ASGITransport, AsyncClient
from pathlib import Path

from app import app
from services import preview_service
from services.preview_service import (
    UpstreamPreviewUnavailable,
    PreviewSession,
)


def _mock_upstream_error(*args, **kwargs):
    """Raise UpstreamPreviewUnavailable for any call to _open_upstream_stream."""
    raise UpstreamPreviewUnavailable("upstream HTTP 404")


@pytest.mark.asyncio
async def test_upstream_503_retry_after():
    """Session exists but upstream returns 404 — endpoint responds 503 with Retry-After: 30."""
    sid = "test-503-retry"
    session = PreviewSession(
        session_id=sid,
        vod_url="https://example.com/vod",
        master_url="https://example.com/master.m3u8",
        entry_url="https://example.com/vod",
        platform="Twitch",
        http_headers={},
        allowed_hosts=set(),
        cache_dir=Path("."),
        kind="hls",
        crop_start=0.0,
        crop_end=0.0,
        prefer_height=720,
    )
    preview_service._manager._sessions[sid] = session

    original = preview_service._open_upstream_stream
    preview_service._open_upstream_stream = _mock_upstream_error
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/preview/hls/{sid}/master.m3u8")
            assert resp.status_code == 503, f"Expected 503 got {resp.status_code}: {resp.text[:200]}"
            assert resp.headers.get("Retry-After") == "30", f"Missing Retry-After header: {dict(resp.headers)}"
    finally:
        preview_service._open_upstream_stream = original
        # Clean up the test session
        preview_service._manager._sessions.pop(sid, None)
