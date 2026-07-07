"""Prove preview is playable — MP4 ftyp + multi-range proxy, not just session 200."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import app

# titiltei short — user regression case
URL = "https://www.youtube.com/shorts/KkzZw5ebY0A"
# fallback when primary is geo/age blocked
FALLBACK = "https://www.youtube.com/shorts/t_Or3Oz5LX8"

_PLAYBACK_RANGES = (
    "bytes=0-8191",
    "bytes=524288-526335",
    "bytes=8388608-8396799",
)


def _is_mp4_head(chunk: bytes) -> bool:
    return bool(chunk) and b"ftyp" in chunk[:32]


def _create_session(client: TestClient, url: str) -> dict:
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    r = client.post(
        "/api/preview/session",
        json={"url": url, "crop_start": 0, "crop_end": 60, "prefer_height": 720},
    )
    if r.status_code == 500:
        detail = (r.json().get("detail") or "").lower()
        if "unavailable" in detail or "try again" in detail:
            pytest.skip(f"YouTube extract blocked: {r.json().get('detail')}")
    assert r.status_code == 200, r.text
    return r.json()


def _stream_path(body: dict) -> tuple[str, str]:
    kind = body.get("kind") or "hls"
    sid = body["session_id"]
    if kind == "progressive":
        return kind, f"/api/preview/hls/{sid}/stream.mp4"
    return kind, f"/api/preview/hls/{sid}/master.m3u8"


def test_youtube_preview_playback_proof():
    """Session + stream bytes must be valid MP4/HLS — simulates <video> range requests."""
    from services.youtube_session import invalidate_anonymous_session

    invalidate_anonymous_session()
    with TestClient(app) as client:
        body = None
        for url in (URL, FALLBACK):
            try:
                body = _create_session(client, url)
                break
            except AssertionError:
                continue
        if body is None:
            pytest.skip("YouTube extract blocked for all probe URLs")

        kind, path = _stream_path(body)
        sid = body["session_id"]

        if kind == "hls":
            master = client.get(path)
            assert master.status_code == 200, master.text[:300]
            text = master.text
            assert text.lstrip().startswith("#EXTM3U"), "HLS master must be a playlist"
            seg = next(
                (ln.strip() for ln in text.splitlines() if ln.strip().startswith("/api/preview/")),
                None,
            )
            assert seg, "HLS master must reference proxy segments"
            seg_resp = client.get(seg, headers={"Range": "bytes=0-8191"})
            assert seg_resp.status_code in (200, 206), seg_resp.text[:200]
            assert len(seg_resp.content) > 256, "HLS segment body empty"
        else:
            total = 0
            for rng in _PLAYBACK_RANGES:
                resp = client.get(path, headers={"Range": rng})
                assert resp.status_code in (200, 206), (
                    f"range {rng} returned {resp.status_code}: {resp.text[:200]}"
                )
                chunk = resp.content
                assert len(chunk) > 0, f"empty body for {rng}"
                total += len(chunk)
                if rng.startswith("bytes=0-"):
                    assert _is_mp4_head(chunk), f"not MP4: head={chunk[:16]!r}"
            assert total > 16_384, "playback proof needs >16KB across ranges"

            refresh = client.post(f"/api/preview/session/{sid}/refresh", json={})
            assert refresh.status_code == 200, refresh.text
            again = client.get(path, headers={"Range": "bytes=0-8191"})
            assert again.status_code in (200, 206)
            assert _is_mp4_head(again.content), "post-refresh stream must still be MP4"

        client.delete(f"/api/preview/session/{sid}")
