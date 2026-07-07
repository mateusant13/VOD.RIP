"""Real-network: DASH segment mux must work when seeking (high segment indices)."""
from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from app import app

CANDIDATES = [
    "https://www.youtube.com/watch?v=r6rh6Wh2WjI",
    "https://www.youtube.com/watch?v=4kyvGbRpV7M",
]


def _is_mpegts(body: bytes) -> bool:
    if len(body) < 376:
        return False
    hits = sum(1 for i in range(0, min(len(body), 188 * 5), 188) if body[i] == 0x47)
    return hits >= 2


def _fetch_segment(c, path: str, retries: int = 120) -> tuple[int, bytes]:
    for _ in range(retries):
        resp = c.get(path)
        if resp.status_code == 503:
            time.sleep(1)
            continue
        return resp.status_code, resp.content
    return 503, b""


def test_youtube_dash_seek_segments_high_index():
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    failures: list[str] = []
    tested = False

    with TestClient(app) as c:
        for url in CANDIDATES:
            created = c.post(
                "/api/preview/session",
                json={"url": url, "crop_start": 0, "crop_end": 120, "prefer_height": 720},
            )
            if created.status_code != 200:
                failures.append(f"CREATE {url}: {created.status_code}")
                continue
            body = created.json()
            sid = body["session_id"]
            if not body.get("trim_timeline"):
                c.delete(f"/api/preview/session/{sid}")
                continue

            tested = True
            master = c.get(f"/api/preview/hls/{sid}/master.m3u8")
            paths = [
                ln.strip()
                for ln in master.text.splitlines()
                if ln.strip().startswith("/api/") and "ytseg-" in ln
            ]
            if len(paths) < 6:
                failures.append(f"PLAYLIST {url}: only {len(paths)} segments")
                c.delete(f"/api/preview/session/{sid}")
                continue

            for idx in (0, 5, 8, 9):
                status, content = _fetch_segment(c, paths[idx], retries=180)
                if status not in (200, 206):
                    failures.append(f"seg{idx} {url}: HTTP {status} body={content[:120]!r}")
                    continue
                if len(content) < 50_000:
                    failures.append(f"seg{idx} {url}: tiny body {len(content)}")
                    continue
                if not _is_mpegts(content):
                    failures.append(f"seg{idx} {url}: not MPEG-TS head={content[:20]!r}")

            c.delete(f"/api/preview/session/{sid}")
            break

    if not tested:
        pytest.skip("no DASH-segment YouTube URL in candidates")
    assert not failures, "\n".join(failures)
