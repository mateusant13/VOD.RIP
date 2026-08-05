"""Whisper model settings — /api/settings roundtrip, model/cache resolution,
and inactive-model cache pruning (guard included). Scratch env only: the
real %APPDATA%/VOD.RIP/whisper-models dir and the shared backend are never
touched, and no model is ever downloaded (WhisperModel is patched).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_transcribe, disk_hygiene


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path):
    """Redirect the shared settings manager to a scratch file (no real
    %APPDATA% writes; mirrors test_api_integration's reset convention)."""
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


# --- /api/settings roundtrip ------------------------------------------------

@pytest.mark.asyncio
async def test_whisper_settings_roundtrip(client):
    resp = await client.post("/api/settings", json={
        "whisper_model": "Systran/faster-whisper-medium",
        "whisper_model_cache": "I:/produtos202608/BrandOps/dashboard/cache/huggingface/hub",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["whisper_model"] == "Systran/faster-whisper-medium"
    assert data["whisper_model_cache"] == "I:/produtos202608/BrandOps/dashboard/cache/huggingface/hub"

    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["whisper_model"] == "Systran/faster-whisper-medium"
    assert data["whisper_model_cache"] == "I:/produtos202608/BrandOps/dashboard/cache/huggingface/hub"


@pytest.mark.asyncio
async def test_whisper_settings_blank_model_falls_back_to_default(client):
    resp = await client.post("/api/settings", json={"whisper_model": "   "})
    assert resp.status_code == 200
    assert resp.json()["whisper_model"] == "large-v3-turbo"


@pytest.mark.asyncio
async def test_whisper_cache_empty_cleared_to_none(client):
    resp = await client.post("/api/settings", json={"whisper_model_cache": "C:/some/cache"})
    assert resp.status_code == 200
    assert resp.json()["whisper_model_cache"] == "C:/some/cache"

    resp = await client.post("/api/settings", json={"whisper_model_cache": "   "})
    assert resp.status_code == 200
    assert resp.json()["whisper_model_cache"] is None


# --- model/cache resolution (settings -> env -> default) --------------------

def test_transcribe_resolution_from_settings(monkeypatch):
    monkeypatch.delenv("VODRIP_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(
            whisper_model="Systran/faster-whisper-medium",
            whisper_model_cache="I:/cache/hub",
        )
        assert archive_transcribe.model_name() == "Systran/faster-whisper-medium"
        assert archive_transcribe._cache_dir() == Path("I:/cache/hub")


def test_transcribe_resolution_env_fallback(monkeypatch):
    monkeypatch.setenv("VODRIP_WHISPER_MODEL", "small")
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model="", whisper_model_cache="")
        assert archive_transcribe.model_name() == "small"
        assert str(archive_transcribe._cache_dir()).endswith("whisper-models")


def test_transcribe_resolution_defaults(monkeypatch):
    monkeypatch.delenv("VODRIP_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model="", whisper_model_cache="")
        assert archive_transcribe.model_name() == "large-v3-turbo"


def test_transcribe_env_override_beats_settings(monkeypatch):
    """The env knob stays the per-process override (pinned by
    test_disk_router.py::test_cleanup_whisper_models_env_active_wins)."""
    monkeypatch.setenv("VODRIP_WHISPER_MODEL", "small")
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model="Systran/faster-whisper-medium")
        assert archive_transcribe.model_name() == "small"


def test_transcribe_load_passes_resolved_id_and_cache(monkeypatch, tmp_path):
    """WhisperModel receives the settings-driven id + cache dir — the
    BrandOps HF hub reuse path (Systran CT2 checkpoints load directly)."""
    cache = tmp_path / "cache"
    (cache / "models--Systran--faster-whisper-medium").mkdir(parents=True)
    (cache / "models--Systran--faster-whisper-medium" / "model.bin").write_bytes(b"x")
    monkeypatch.setenv("VODRIP_WHISPER_DEVICE", "cpu")
    monkeypatch.delenv("VODRIP_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    archive_transcribe._model = None
    archive_transcribe._model_name = None
    try:
        with patch("deps.settings_mgr") as mgr, patch("faster_whisper.WhisperModel") as WM:
            mgr.get.return_value = SimpleNamespace(
                whisper_model="Systran/faster-whisper-medium",
                whisper_model_cache=str(cache),
            )
            archive_transcribe._get_model()
    finally:
        archive_transcribe._model = None
        archive_transcribe._model_name = None
    assert WM.call_count == 1
    args, kwargs = WM.call_args
    assert args[0] == "Systran/faster-whisper-medium"
    assert kwargs.get("download_root") == str(cache)


def test_get_model_reloads_when_device_override_flips(monkeypatch):
    """After a CUDA OOM the override flips to CPU — the cached CUDA model is
    broken, so _get_model must reload instead of returning it (it previously
    compared only the model name, so the retry re-ran on the corrupted
    context: cudaErrorInvalidDevice)."""
    archive_transcribe._model = object()
    archive_transcribe._model_name = "large-v3-turbo"
    archive_transcribe._model_device = "cuda"
    archive_transcribe._device_override = None
    monkeypatch.setattr(archive_transcribe, "model_name", lambda: "large-v3-turbo")
    monkeypatch.setattr(archive_transcribe, "_effective_device", lambda: ("cpu", "int8"))
    try:
        with patch("faster_whisper.WhisperModel") as WM:
            archive_transcribe._get_model()
        assert WM.call_count == 1, "override flip must trigger a reload"
        assert WM.call_args.kwargs["device"] == "cpu"
        assert archive_transcribe._model_device == "cpu"
    finally:
        archive_transcribe._model = None
        archive_transcribe._model_name = None
        archive_transcribe._model_device = None


# --- pruning (disk_hygiene) -------------------------------------------------

def _mk_model(cache: Path, name: str, size: int) -> None:
    d = cache / f"models--{name.replace('/', '--')}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.bin").write_bytes(b"x" * size)


def test_prune_keeps_active_deletes_other_hf_dirs(tmp_path):
    cache = tmp_path / "whisper-cache"
    _mk_model(cache, "Systran/faster-whisper-medium", 2048)
    _mk_model(cache, "Systran/faster-whisper-small", 1024)
    _mk_model(cache, "Systran/faster-whisper-large-v3-turbo", 8192)
    (cache / "not-a-model").mkdir()
    (cache / "not-a-model" / "user-file.txt").write_text("x")

    freed = disk_hygiene.prune_inactive_whisper_models(cache, "Systran/faster-whisper-medium")

    assert freed == 1024 + 8192
    assert (cache / "models--Systran--faster-whisper-medium").is_dir()
    assert (cache / "models--Systran--faster-whisper-small").exists() is False
    assert (cache / "models--Systran--faster-whisper-large-v3-turbo").exists() is False
    assert (cache / "not-a-model").is_dir(), "unknown dirs must be left alone"


def test_prune_active_dir_missing_deletes_nothing(tmp_path):
    """Guard: active model not in the cache yet -> pruning would brick the
    next transcription, so NOTHING is deleted."""
    cache = tmp_path / "whisper-cache"
    _mk_model(cache, "Systran/faster-whisper-medium", 2048)
    _mk_model(cache, "Systran/faster-whisper-large-v3-turbo", 8192)

    freed = disk_hygiene.prune_inactive_whisper_models(cache, "Systran/faster-whisper-small")

    assert freed == 0
    assert (cache / "models--Systran--faster-whisper-medium").is_dir()
    assert (cache / "models--Systran--faster-whisper-large-v3-turbo").is_dir()


def test_prune_missing_cache_is_noop(tmp_path):
    assert disk_hygiene.prune_inactive_whisper_models(tmp_path / "nope", "small") == 0


def test_startup_hygiene_prunes_with_settings_active(monkeypatch, tmp_path):
    """Live-ish: startup hygiene + fake cache dirs -> only the settings-active
    model survives (scratch APPDATA, never the real profile)."""
    cache = tmp_path / "cache"
    _mk_model(cache, "Systran/faster-whisper-medium", 2048)
    _mk_model(cache, "Systran/faster-whisper-small", 1024)
    scratch_tmp = tmp_path / "tmp"
    scratch_tmp.mkdir()

    monkeypatch.setenv("VODRIP_WHISPER_CACHE", str(cache))
    monkeypatch.delenv("VODRIP_WHISPER_MODEL", raising=False)
    monkeypatch.setattr("services.disk_hygiene._get_appdata_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(
            whisper_model="Systran/faster-whisper-medium", whisper_model_cache=None
        )
        stats = disk_hygiene.run_startup_hygiene()

    assert stats.get("whisper_models") == 1024
    assert (cache / "models--Systran--faster-whisper-medium").is_dir()
    assert (cache / "models--Systran--faster-whisper-small").exists() is False
