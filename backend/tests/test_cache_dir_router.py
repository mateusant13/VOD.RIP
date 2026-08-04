"""WS-8 cache-dir routing — /api/settings cache_dir round-trip, per-cache
root routing (whisper / yt-dlp / preview / embed), env-override precedence,
and the probe-file acceptance (a configured cache_dir receives new cache
writes). Scratch env only: the shared settings manager is redirected to a
tmp file and VODRIP_CACHE_DIR (pinned by conftest) is cleared where the
setting itself must win. Real %APPDATA%/VOD.RIP is never touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_embed, archive_transcribe, disk_hygiene, ytdlp_cache
from services.preview._state import preview_root


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path):
    """Redirect the shared settings manager to a scratch file (mirrors
    test_whisper_model_settings' reset convention; env restored, not popped)."""
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


# --- /api/settings round-trip ------------------------------------------------

@pytest.mark.asyncio
async def test_cache_dir_roundtrip(client):
    resp = await client.post("/api/settings", json={"cache_dir": "D:/caches"})
    assert resp.status_code == 200
    assert resp.json()["cache_dir"] == "D:/caches"

    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["cache_dir"] == "D:/caches"


@pytest.mark.asyncio
async def test_cache_dir_blank_returns_to_auto(client):
    resp = await client.post("/api/settings", json={"cache_dir": "D:/caches"})
    assert resp.status_code == 200
    resp = await client.post("/api/settings", json={"cache_dir": "   "})
    assert resp.status_code == 200
    assert resp.json()["cache_dir"] == "", "whitespace cache_dir means auto"


# --- per-cache routing (setting wins once env is cleared) --------------------

@pytest.mark.asyncio
async def test_whisper_cache_routed_through_cache_dir(client, monkeypatch):
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    resp = await client.post("/api/settings", json={"cache_dir": "D:/caches"})
    assert resp.status_code == 200
    assert disk_hygiene.whisper_cache_dir() == Path("D:/caches") / "whisper-models"
    assert archive_transcribe._cache_dir() == Path("D:/caches") / "whisper-models"


@pytest.mark.asyncio
async def test_ytdlp_cache_routed_through_cache_dir(client, tmp_path, monkeypatch):
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    caches = tmp_path / "caches"
    resp = await client.post("/api/settings", json={"cache_dir": str(caches)})
    assert resp.status_code == 200
    assert ytdlp_cache._get_cache_dir() == caches / "yt-dlp-cache"
    assert (caches / "yt-dlp-cache").is_dir()


@pytest.mark.asyncio
async def test_preview_root_routed_through_cache_dir(client, monkeypatch):
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    resp = await client.post("/api/settings", json={"cache_dir": "D:/caches"})
    assert resp.status_code == 200
    assert preview_root() == Path("D:/caches") / "kd_preview"


def test_embed_cache_routed_through_cache_dir(monkeypatch):
    monkeypatch.delenv("VODRIP_EMBED_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(cache_dir="D:/caches")
        assert archive_embed._cache_dir() == Path("D:/caches") / "embed-models"


# --- env overrides still win -------------------------------------------------

def test_whisper_env_beats_cache_dir(monkeypatch):
    monkeypatch.setenv("VODRIP_WHISPER_CACHE", "X:/hub")
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(cache_dir="D:/caches")
        assert disk_hygiene.whisper_cache_dir() == Path("X:/hub")


def test_embed_env_beats_cache_dir(monkeypatch):
    monkeypatch.setenv("VODRIP_EMBED_CACHE", "X:/embed")
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(cache_dir="D:/caches")
        assert archive_embed._cache_dir() == Path("X:/embed")


# --- probe-file acceptance ---------------------------------------------------

@pytest.mark.asyncio
async def test_probe_file_lands_under_cache_dir(client, tmp_path, monkeypatch):
    """Acceptance: with cache_dir persisted in a scratch settings.json, a new
    cache write lands under it (per-cache env cleared so the setting wins)."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    resp = await client.post("/api/settings", json={"cache_dir": str(tmp_path / "caches")})
    assert resp.status_code == 200

    cache = disk_hygiene.whisper_cache_dir()
    assert cache == tmp_path / "caches" / "whisper-models"
    probe = cache / "probe.bin"
    probe.parent.mkdir(parents=True, exist_ok=True)  # the cache consumer creates dirs
    probe.write_bytes(b"probe")
    assert probe.is_file(), "a cache write must land under the configured cache_dir"
    assert (tmp_path / "caches" / "whisper-models" / "probe.bin").is_file()
