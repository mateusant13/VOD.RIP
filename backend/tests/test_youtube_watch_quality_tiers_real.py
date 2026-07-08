"""YouTube watch URLs must expose multi-tier quality (incl. 1080p), not single 360p progressive."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import app
from services.preview_service import _RESOLVED_STREAM_CACHE
from services.ytdlp_hls import _EXTRACT_INFO_CACHE
from services.youtube_session import invalidate_anonymous_session

WATCH = "https://www.youtube.com/watch?v=4kyvGbRpV7M"


@pytest.mark.timeout(120)
def test_youtube_watch_variant_heights_include_1080():
    invalidate_anonymous_session()
    _EXTRACT_INFO_CACHE.clear()
    _RESOLVED_STREAM_CACHE.clear()
    with TestClient(app) as c:
        r = c.post(
            "/api/preview/session",
            json={"url": WATCH, "crop_start": 0, "crop_end": 120, "prefer_height": 720},
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        heights = body.get("variant_heights") or []
        assert max(heights) >= 1080, f"expected 1080p tier, got {heights} kind={body.get('kind')}"
        q = c.post(
            f"/api/preview/session/{body['session_id']}/quality",
            json={"prefer_height": 1080},
        )
        assert q.status_code == 200, q.text[:200]
        assert (q.json().get("active_height") or 0) >= 1080
        c.delete(f"/api/preview/session/{body['session_id']}")
