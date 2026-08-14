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
from services.settings import recommended_resource_defaults


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
    # No usable drive -> appdata fallback (keeps the test hermetic: the real
    # auto pick probes actual disks via best_model_cache_drive()).
    monkeypatch.setattr("services.disk_hygiene.best_model_cache_drive", lambda: None)
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
    # _get_model resolves the cache dir; pin the ROI auto-pick to keep the
    # test hermetic (no real-disk PowerShell probe, no real drive writes).
    monkeypatch.setattr(disk_hygiene, "best_model_cache_drive", lambda: None)
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


# --- model-cache auto pick (Settings > Disk "AI Models Folder" Auto) --------

def _inv(letter, free, rank):
    return {
        "drive": f"{letter}:\\",
        "label": "",
        "total_bytes": 1000 * 1024**3,
        "free_bytes": free,
        "media_type": "Unknown",
        "bus_type": "Unknown",
        "speed_rank": rank,
    }


def test_model_cache_score_prefers_ssd_near_ties():
    # SSD with adequate free beats an HDD with a bit more (100+32 > 120).
    assert disk_hygiene._model_cache_score(100 * 1024**3, 2) > disk_hygiene._model_cache_score(120 * 1024**3, 3)
    # A large slow HDD beats a nearly-full SSD (400 > 300+64).
    assert disk_hygiene._model_cache_score(400 * 1024**3, 3) > disk_hygiene._model_cache_score(300 * 1024**3, 1)
    # NVMe credit > SSD credit at equal free space.
    assert disk_hygiene._model_cache_score(50 * 1024**3, 1) > disk_hygiene._model_cache_score(50 * 1024**3, 2)


def test_best_model_cache_drive_roi(monkeypatch):
    monkeypatch.setattr(
        "services.disk_detect.disk_inventory",
        lambda: [
            _inv("C", 5 * 1024**3, 1),    # NVMe but below the 8 GB floor
            _inv("F", 100 * 1024**3, 2),  # SSD
            _inv("I", 400 * 1024**3, 3),  # HDD with lots of space -> wins
        ],
    )
    assert disk_hygiene.best_model_cache_drive() == "I:\\"


def test_best_model_cache_drive_ssd_wins_near_tie(monkeypatch):
    monkeypatch.setattr(
        "services.disk_detect.disk_inventory",
        lambda: [
            _inv("D", 120 * 1024**3, 3),  # HDD, 120 GB
            _inv("H", 100 * 1024**3, 1),  # NVMe, 100 GB -> 164 GB score
        ],
    )
    assert disk_hygiene.best_model_cache_drive() == "H:\\"


def test_best_model_cache_drive_empty(monkeypatch):
    monkeypatch.setattr("services.disk_detect.disk_inventory", lambda: [])
    assert disk_hygiene.best_model_cache_drive() is None


def test_whisper_cache_dir_auto_uses_roi_drive(monkeypatch):
    """Auto (unset setting) resolves to <best-ROI drive>\\VOD.RIP-models."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.setattr(
        "services.disk_detect.disk_inventory",
        lambda: [_inv("H", 61 * 1024**3, 1)],
    )
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache=None)
        assert disk_hygiene.whisper_cache_dir() == Path("H:\\VOD.RIP-models")


def test_whisper_cache_legacy_cache_dir_reused_until_migrated(monkeypatch, tmp_path):
    """Migration: an EMPTY models folder falls back to the legacy
    <cache root>/whisper-models (no re-download); once the models folder has
    a real model dir it wins."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    legacy = tmp_path / "cache" / "whisper-models"
    (legacy / "models--Systran--faster-whisper-large-v3-turbo").mkdir(parents=True)
    models_root = tmp_path / "drive" / "VOD.RIP-models"
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache=None)
        monkeypatch.setattr(
            "services.disk_hygiene.best_model_cache_drive",
            lambda: str(tmp_path / "drive"),
        )
        # the models folder is empty -> legacy cache-dir models reused
        assert disk_hygiene.whisper_cache_dir() == legacy
        # a real model dir in the models folder flips resolution to it
        (models_root / "models--Systran--faster-whisper-small").mkdir(parents=True)
        assert disk_hygiene.whisper_cache_dir() == models_root


def test_whisper_cache_dir_explicit_beats_auto(monkeypatch):
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache="Z:/shared/hub")
        assert disk_hygiene.whisper_cache_dir() == Path("Z:/shared/hub")


# --- recommended resource defaults (Settings > Recommended) -----------------

def test_recommended_threads_half_cores_capped_by_ram():
    assert recommended_resource_defaults(
        cpu_count=20, ram_bytes=32 * 1024**3, drive_total=0, drive_free=0
    )["download_threads"] == 10  # round(20 * 0.5)
    # RAM guard: 8 GB -> at most 4 concurrent downloaders (~2 GB each).
    assert recommended_resource_defaults(
        cpu_count=20, ram_bytes=8 * 1024**3, drive_total=0, drive_free=0
    )["download_threads"] == 4
    # Clamps: 2 min / 16 max.
    assert recommended_resource_defaults(
        cpu_count=2, ram_bytes=64 * 1024**3, drive_total=0, drive_free=0
    )["download_threads"] == 2
    assert recommended_resource_defaults(
        cpu_count=64, ram_bytes=128 * 1024**3, drive_total=0, drive_free=0
    )["download_threads"] == 16


def test_recommended_cache_mb_scales_with_free_share():
    assert recommended_resource_defaults(
        cpu_count=20, ram_bytes=32 * 1024**3,
        drive_total=1000 * 1024**3, drive_free=500 * 1024**3,
    )["max_cache_mb"] == 1000  # 50% free -> 1000 MB
    assert recommended_resource_defaults(
        cpu_count=20, ram_bytes=32 * 1024**3,
        drive_total=1000 * 1024**3, drive_free=10 * 1024**3,
    )["max_cache_mb"] == 50  # 1% free -> floor
    assert recommended_resource_defaults(
        cpu_count=20, ram_bytes=32 * 1024**3,
        drive_total=1000 * 1024**3, drive_free=1000 * 1024**3,
    )["max_cache_mb"] == 2000  # 100% free -> cap
