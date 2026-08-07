"""Settings router roundtrip for the official-API hybrid (issue #4).

In-process ASGI client (no server): token save stamps updated_at, the
official-apis-status endpoint reports credential/quota state, and saving
settings auto-lifts the helix token from the cookie bridge without
clobbering a fresh manual paste.

Run from backend/: python -m pytest tests/test_official_apis_settings.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
import pytest

from app import app
from deps import settings_mgr
from models.schemas import AppSettings


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path):
    """Redirect the shared settings manager to scratch (mirrors
    test_archive_yt_captions._reset_settings)."""
    original_file = settings_mgr._settings_file
    original_dir = settings_mgr._settings_dir
    scratch_dir = tmp_path / "VOD.RIP"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    settings_mgr._settings_dir = scratch_dir
    settings_mgr._settings_file = scratch_dir / "settings.json"
    settings_mgr._settings = AppSettings()
    yield
    settings_mgr._settings_file = original_file
    settings_mgr._settings_dir = original_dir


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_token_save_stamps_updated_at(client):
    resp = await client.post("/api/settings", json={"twitch_helix_token": "tok-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["twitch_helix_token"] == "tok-1"
    assert body["twitch_helix_token_updated_at"] > 0

    # unchanged value -> no re-stamp
    before = body["twitch_helix_token_updated_at"]
    resp = await client.post("/api/settings", json={"twitch_helix_token": "tok-1"})
    assert resp.json()["twitch_helix_token_updated_at"] == before

    # change -> fresh stamp
    resp = await client.post("/api/settings", json={"twitch_helix_token": "tok-2"})
    assert resp.json()["twitch_helix_token_updated_at"] > before


async def test_status_endpoint_shape(client):
    resp = await client.get("/api/settings/official-apis-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["twitch_helix_token_set"] is False
    assert body["youtube_api_key_set"] is False
    assert body["youtube_quota_limit"] == 10000
    assert body["youtube_quota_used"] == 0
    assert body["youtube_degraded"] is False

    await client.post("/api/settings", json={"twitch_helix_token": "tok"})
    await client.post("/api/settings", json={"youtube_data_api_key": "AIza-x"})
    body = (await client.get("/api/settings/official-apis-status")).json()
    assert body["twitch_helix_token_set"] is True
    assert body["youtube_api_key_set"] is True


async def test_auto_lift_on_settings_save(client, tmp_path, monkeypatch):
    """Saving settings with the cookie bridge live fills an empty token
    (no manual paste needed for extension users)."""
    txt = tmp_path / "twitch.txt"
    txt.write_text("auth-token\tlifted-tok\n", encoding="utf-8")
    now = time.time()
    os.utime(txt, (now, now))
    monkeypatch.setattr("services.cookie_bridge.resolve_cookiefile", lambda platform: str(txt))
    monkeypatch.setattr("services.cookie_bridge.cookie_dict", lambda platform: {"auth-token": "lifted-tok"})

    resp = await client.post("/api/settings", json={"ui_language": "en"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["twitch_helix_token"] == "lifted-tok"
    assert body["twitch_helix_token_updated_at"] > 0


async def test_auto_lift_does_not_clobber_fresh_paste(client, tmp_path, monkeypatch):
    """A token saved in the SAME request must win over an older cookie export."""
    await client.post("/api/settings", json={"twitch_helix_token": "pasted-now"})
    now = time.time()
    txt = tmp_path / "twitch.txt"
    txt.write_text("auth-token\tcookie-old\n", encoding="utf-8")
    os.utime(txt, (now - 3600, now - 3600))  # cookie export is an hour old
    monkeypatch.setattr("services.cookie_bridge.resolve_cookiefile", lambda platform: str(txt))
    monkeypatch.setattr("services.cookie_bridge.cookie_dict", lambda platform: {"auth-token": "cookie-old"})

    resp = await client.post("/api/settings", json={"ui_language": "pt-BR"})
    assert resp.json()["twitch_helix_token"] == "pasted-now", "fresh paste must survive auto-lift"
