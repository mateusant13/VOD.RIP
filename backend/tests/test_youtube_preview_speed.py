"""YouTube preview session speed — post-warm path (real paste UX)."""
from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from app import app

TITILTEI_URLS = (
    "https://www.youtube.com/watch?v=1tap3CLaqr8",
    "https://www.youtube.com/shorts/KkzZw5ebY0A",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
)
POST_WARM_BUDGET_MS = 3000


def test_youtube_preview_session_post_warm_under_3s():
    from services.preview_service import warm_youtube_preview_resolve, _RESOLVED_STREAM_CACHE
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE
    from services.youtube_session import invalidate_anonymous_session

    invalidate_anonymous_session()
    _EXTRACT_INFO_CACHE.clear()
    _RESOLVED_STREAM_CACHE.clear()
    with TestClient(app) as client:
        last_err = ""
        for url in TITILTEI_URLS:
            warm_youtube_preview_resolve(url)
            t0 = time.monotonic()
            r = client.post(
                "/api/preview/session",
                json={"url": url, "crop_start": 0, "crop_end": 60, "prefer_height": 480},
            )
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                assert ms <= POST_WARM_BUDGET_MS, f"{url} took {ms}ms (limit {POST_WARM_BUDGET_MS})"
                client.delete(f"/api/preview/session/{r.json()['session_id']}")
                return
            last_err = r.text
        pytest.skip(f"YouTube extract blocked for titiltei probes: {last_err[:200]}")
