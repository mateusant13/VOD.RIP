"""WS-8 cache-dir routing — /api/settings cache_dir round-trip, per-cache
root routing (whisper / yt-dlp / embed; preview follows the DATA disk),
env-override precedence, and the probe-file acceptance (a configured
cache_dir receives new cache writes). Scratch env only: the shared settings
manager is redirected to a tmp file and VODRIP_CACHE_DIR / VODRIP_DATA_DIR
(pinned by conftest) are cleared where the setting itself must win. Real
%APPDATA%/VOD.RIP is never touched.
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
async def test_whisper_cache_routed_by_own_setting(client, monkeypatch):
    """Whisper models follow whisper_model_cache (own disk choice), NOT the
    heavy cache_dir: the picker writes <drive>\\VOD.RIP-models and the auto
    rule uses best_model_cache_drive()."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    resp = await client.post(
        "/api/settings",
        json={"cache_dir": "D:/caches", "whisper_model_cache": "H:/VOD.RIP-models"},
    )
    assert resp.status_code == 200
    assert disk_hygiene.whisper_cache_dir() == Path("H:/VOD.RIP-models")
    assert archive_transcribe._cache_dir() == Path("H:/VOD.RIP-models")

    # cache_dir alone must NOT move the whisper cache (falls back to auto).
    monkeypatch.setattr("services.disk_hygiene.best_model_cache_drive", lambda: None)
    # No legacy cache-dir models exist (hermetic: cache_root pinned to None so
    # the auto branch cannot fall back to a real <cache root>/whisper-models).
    monkeypatch.setattr("services.settings.cache_root", lambda: None)
    resp = await client.post(
        "/api/settings", json={"cache_dir": "D:/caches", "whisper_model_cache": ""}
    )
    assert resp.status_code == 200
    assert disk_hygiene.whisper_cache_dir() == disk_hygiene._get_appdata_dir() / "whisper-models"


@pytest.mark.asyncio
async def test_ytdlp_cache_routed_through_cache_dir(client, tmp_path, monkeypatch):
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    caches = tmp_path / "caches"
    resp = await client.post("/api/settings", json={"cache_dir": str(caches)})
    assert resp.status_code == 200
    assert ytdlp_cache._get_cache_dir() == caches / "yt-dlp-cache"
    assert (caches / "yt-dlp-cache").is_dir()


@pytest.mark.asyncio
async def test_preview_root_routed_through_data_dir(client, monkeypatch):
    """Preview media is 'fetched quickly' data: kd_preview follows the data
    disk (fastest), NOT the heavy cache disk (biggest free)."""
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    monkeypatch.delenv("VODRIP_DATA_DIR", raising=False)
    resp = await client.post(
        "/api/settings", json={"cache_dir": "C:/heavy", "data_dir": "D:/data"}
    )
    assert resp.status_code == 200
    assert preview_root() == Path("D:/data") / "kd_preview"


def test_embed_models_follow_models_folder_not_cache_dir(monkeypatch, tmp_path):
    """Embed ONNX weights follow the AI-models folder (whisper_model_cache),
    NOT the heavy cache_dir — model weights never resolve under the cache
    disk (storage-ownership fix)."""
    monkeypatch.delenv("VODRIP_EMBED_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)  # env cleared so the setting wins
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(
            cache_dir="D:/caches", whisper_model_cache="H:/VOD.RIP-models"
        )
        assert archive_embed._cache_dir() == Path("H:/VOD.RIP-models") / "embed-models"


def test_embed_models_auto_under_models_drive(monkeypatch, tmp_path):
    """Auto (no models setting): embed models resolve under the auto-picked
    models drive, never under cache_dir."""
    monkeypatch.delenv("VODRIP_EMBED_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("services.disk_hygiene.best_model_cache_drive", lambda: "H:\\")
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(cache_dir="D:/caches", whisper_model_cache="")
        assert archive_embed._cache_dir() == Path("H:\\VOD.RIP-models") / "embed-models"


def test_embed_models_legacy_cache_dir_reused_until_migrated(monkeypatch, tmp_path):
    """Migration: a models folder that holds nothing yet falls back to the
    legacy <cache root>/embed-models (no re-download); a populated models
    folder wins over the legacy location."""
    monkeypatch.delenv("VODRIP_EMBED_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    legacy = tmp_path / "cache" / "embed-models"
    (legacy / "e5-small-int8").mkdir(parents=True)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(
            cache_dir=str(tmp_path / "cache"), whisper_model_cache=str(tmp_path / "models")
        )
        # primary (models folder) is empty -> legacy reused
        assert archive_embed._cache_dir() == legacy
        # a real model dir in the models folder flips resolution to primary
        (tmp_path / "models" / "embed-models" / "e5-small-int8").mkdir(parents=True)
        assert archive_embed._cache_dir() == tmp_path / "models" / "embed-models"


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
async def test_probe_file_lands_under_whisper_model_cache(client, tmp_path, monkeypatch):
    """Acceptance: with whisper_model_cache persisted in a scratch settings
    file, a new whisper cache write lands under it (env cleared so the
    setting wins)."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    resp = await client.post(
        "/api/settings", json={"whisper_model_cache": str(tmp_path / "caches" / "VOD.RIP-models")}
    )
    assert resp.status_code == 200

    cache = disk_hygiene.whisper_cache_dir()
    assert cache == tmp_path / "caches" / "VOD.RIP-models"
    probe = cache / "probe.bin"
    probe.parent.mkdir(parents=True, exist_ok=True)  # the cache consumer creates dirs
    probe.write_bytes(b"probe")
    assert probe.is_file(), "a cache write must land under the configured whisper_model_cache"
    assert (tmp_path / "caches" / "VOD.RIP-models" / "probe.bin").is_file()
