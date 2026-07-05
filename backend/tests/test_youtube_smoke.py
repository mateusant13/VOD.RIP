"""YouTube preview smoke — real InnerTube, no browser spawn."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import app

URLS = [
    "https://www.youtube.com/shorts/IbkQI11-NZk",
    "https://www.youtube.com/shorts/t_Or3Oz5LX8",
    "https://www.youtube.com/watch?v=4kyvGbRpV7M",
]


@pytest.mark.timeout(300)
def test_youtube_preview_no_500():
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    failures: list[str] = []
    with TestClient(app) as c:
        for url in URLS:
            r = c.post(
                "/api/preview/session",
                json={"url": url, "crop_start": 0, "crop_end": 30, "prefer_height": 720},
            )
            if r.status_code != 200:
                failures.append(f"CREATE {url}: {r.status_code} {r.text[:200]}")
                continue
            body = r.json()
            sid = body["session_id"]
            kind = body.get("kind")
            path = (
                f"/api/preview/hls/{sid}/stream.mp4"
                if kind == "progressive"
                else f"/api/preview/hls/{sid}/master.m3u8"
            )
            hdrs = {"Range": "bytes=0-8191"} if kind == "progressive" else None
            s = c.get(path, headers=hdrs)
            if s.status_code not in (200, 206) or not s.content:
                failures.append(f"STREAM {url} kind={kind}: {s.status_code} {s.text[:200]}")
            q = c.post(f"/api/preview/session/{sid}/quality", json={"prefer_height": 480})
            if q.status_code != 200:
                failures.append(f"QUALITY {url}: {q.status_code} {q.text[:200]}")
            c.delete(f"/api/preview/session/{sid}")
    assert not failures, "\n".join(failures)
