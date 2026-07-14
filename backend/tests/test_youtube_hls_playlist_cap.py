"""Real-network: YouTube HLS variant playlists must not 502 on the 512KB cap."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app
from services import preview_service as ps


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.network
def test_youtube_hls_variant_playlist_not_capped_at_512kb(client: TestClient):
    url = "https://www.youtube.com/watch?v=m7lRXNO1b4c"
    created = client.post(
        "/api/preview/session",
        json={"url": url, "crop_start": 0, "crop_end": 3600, "prefer_height": 720},
    )
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    master = client.get(f"/api/preview/hls/{sid}/master.m3u8")
    assert master.status_code == 200
    assert master.text.lstrip().startswith("#EXTM3U")
    resource_lines = [
        ln.split("id=", 1)[1].strip()
        for ln in master.text.splitlines()
        if "/resource?id=" in ln
    ]
    assert resource_lines, "master must reference at least one proxied resource"
    for rid in resource_lines:
        if rid == ps.WINDOW_HLS_PLAYLIST_RESOURCE:
            continue
        resp = client.get(f"/api/preview/hls/{sid}/resource", params={"id": rid})
        assert resp.status_code == 200, f"{rid}: {resp.text[:200]}"
        assert resp.text.lstrip().startswith("#EXTM3U"), f"{rid}: not m3u8"
        assert "/api/preview/hls/" in resp.text
    client.delete(f"/api/preview/session/{sid}")
