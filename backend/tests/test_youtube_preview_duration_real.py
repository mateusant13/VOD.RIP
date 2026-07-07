"""Real-network: placeholder crop_end must clamp to extracted VOD duration."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import app

# First YouTube upload (~19s) — stable public ID
SHORT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_preview_session_clamps_placeholder_crop_end():
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    with TestClient(app) as c:
        created = c.post(
            "/api/preview/session",
            json={"url": SHORT, "crop_start": 0, "crop_end": 7200, "prefer_height": 720},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        dur = float(body.get("duration_sec") or 0)
        assert 5 < dur < 7200, f"unexpected duration_sec={dur}"
        sid = body["session_id"]
        master = c.get(f"/api/preview/hls/{sid}/master.m3u8")
        assert master.status_code == 200
        if body.get("trim_timeline"):
            seg_count = master.text.count("ytseg-")
            import math
            expected = int(math.ceil(dur / 10)) + 1
            assert seg_count <= expected, (
                f"playlist has {seg_count} segments for {dur:.0f}s vod (expected ~{expected})"
            )
        c.delete(f"/api/preview/session/{sid}")
