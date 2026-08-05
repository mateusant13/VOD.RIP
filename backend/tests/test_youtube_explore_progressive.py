"""Long-VOD explore: window-HLS with the full adaptive ladder; cached full-mux
visits serve progressive MP4s with byte-range seek."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import app

# titiltei 6h VOD — DASH-only on first extract, muxed after force-fresh
URL = "https://www.youtube.com/watch?v=1tap3CLaqr8"
FALLBACK = "https://www.youtube.com/watch?v=m7lRXNO1b4c"
# explore sends full VOD duration as crop_end (not 0)
EXPLORE_CROP_END = 25_000.0


def _create_explore_session(client: TestClient, url: str) -> dict:
    from services.preview_service import _RESOLVED_STREAM_CACHE
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    _RESOLVED_STREAM_CACHE.clear()
    r = client.post(
        "/api/preview/session",
        json={
            "url": url,
            "crop_start": 0,
            "crop_end": EXPLORE_CROP_END,
            "prefer_height": 720,
        },
    )
    if r.status_code == 500:
        detail = (r.json().get("detail") or "").lower()
        if "unavailable" in detail or "try again" in detail:
            pytest.skip(f"YouTube extract blocked: {r.json().get('detail')}")
    assert r.status_code == 200, r.text
    return r.json()


def test_long_explore_serves_window_hls_with_full_ladder():
    """Full-VOD explore: window-HLS session exposing the full adaptive ladder
    (1080p included) with a playable master; mid-VOD seek remuxes a bounded
    chunk around the playhead instead of flattening to the lone 360p muxed
    tier. Repeat visits still get the cached progressive MP4 (byte-range
    seek) via the full-mux cache in _finalize_youtube_session."""
    from services.youtube_session import invalidate_anonymous_session

    invalidate_anonymous_session()
    with TestClient(app) as client:
        body = None
        for url in (URL, FALLBACK):
            try:
                body = _create_explore_session(client, url)
                break
            except AssertionError:
                continue
        if body is None:
            pytest.skip("YouTube extract blocked for explore probe URLs")

        sid = body["session_id"]
        assert body.get("kind") == "hls", body
        assert max(body.get("variant_heights") or [0]) >= 1080, body

        master = client.get(f"/api/preview/hls/{sid}/master.m3u8")
        assert master.status_code == 200, master.text[:200]
        assert master.text.lstrip().startswith("#EXTM3U")
        assert "/resource?id=" in master.text

        # Mid-VOD seek (~15 min in) must not 500 — window-HLS remuxes a
        # bounded chunk around the playhead.
        mid = client.post(
            f"/api/preview/session/{sid}/seek",
            json={"position_sec": 900.0},
        )
        assert mid.status_code == 200, mid.text[:200]
        assert mid.json().get("ok") is True, mid.text[:200]

        client.delete(f"/api/preview/session/{sid}")


def test_long_explore_muxed_pick_accepts_single_low_tier():
    """resolve_stream_info discards lone 360p — explore downgrade must keep it."""
    from services.preview_service import _youtube_muxed_progressive_for_long_explore

    info = {
        "formats": [
            {
                "height": 720,
                "protocol": "https",
                "url": "https://example.com/v720.mp4",
                "acodec": "none",
                "ext": "mp4",
            },
            {
                "height": 360,
                "protocol": "https",
                "url": "https://example.com/p360.mp4",
                "acodec": "mp4a.40.2",
                "vcodec": "avc1.4d401e",
                "ext": "mp4",
            },
        ],
        "http_headers": {"Referer": "https://www.youtube.com/"},
    }
    picked = _youtube_muxed_progressive_for_long_explore(
        "https://www.youtube.com/watch?v=unit",
        None,
        720,
        yt_info=info,
    )
    assert picked is not None
    assert picked[0] == "https://example.com/p360.mp4"
    assert [f.get("height") for f in picked[1]] == [360]
