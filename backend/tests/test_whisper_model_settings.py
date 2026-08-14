"""ASR settings after the parakeet-only migration.

  * the whisper_model setting is GONE (the engine is fixed) — only
    whisper_model_cache (the AI-models root) remains in /api/settings;
  * the models root resolves the same way it always did (env ->
    settings -> auto best-ROI drive -> appdata) and the sherpa parakeet
    cache is a subdir of it;
  * no whisper download default: the fixed parakeet model id is
    PARAKEET_MODEL and _job_engine() is a clean-failure router
    (_AsrUnsupportedLanguage for uncovered languages — no whisper
    fallback).

Scratch env only: the real %APPDATA%/VOD.RIP/whisper-models dir and the
shared backend are never touched, and nothing is ever downloaded.
"""

from __future__ import annotations

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
from services.archive_transcribe import (
    PARAKEET_LANG_CANDIDATES,
    PARAKEET_MODEL,
    _AsrLaneUnavailable,
    _AsrUnsupportedLanguage,
)
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


# --- /api/settings roundtrip (parakeet-only reality) -----------------------

@pytest.mark.asyncio
async def test_settings_roundtrip_models_root(client):
    """whisper_model_cache (the AI-models root) still roundtrips."""
    resp = await client.post("/api/settings", json={
        "whisper_model_cache": "I:/produtos202608/BrandOps/dashboard/cache/huggingface/hub",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["whisper_model_cache"] == "I:/produtos202608/BrandOps/dashboard/cache/huggingface/hub"

    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["whisper_model_cache"] == "I:/produtos202608/BrandOps/dashboard/cache/huggingface/hub"


@pytest.mark.asyncio
async def test_settings_whisper_model_field_is_gone(client):
    """The engine-selection setting no longer exists: the field is absent
    from GET and a stray POST key is ignored (no download default to set)."""
    resp = await client.post("/api/settings", json={
        "whisper_model": "Systran/faster-whisper-medium",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "whisper_model" not in data

    resp = await client.get("/api/settings")
    data = resp.json()
    assert "whisper_model" not in data
    # The model itself is no longer configurable in the schema either.
    assert not hasattr(AppSettings(), "whisper_model")


@pytest.mark.asyncio
async def test_settings_cache_blank_cleared_to_none(client):
    resp = await client.post("/api/settings", json={"whisper_model_cache": "C:/some/cache"})
    assert resp.status_code == 200
    assert resp.json()["whisper_model_cache"] == "C:/some/cache"

    resp = await client.post("/api/settings", json={"whisper_model_cache": "   "})
    assert resp.status_code == 200
    assert resp.json()["whisper_model_cache"] is None


# --- models-root resolution (settings -> env -> default) -------------------

def test_cache_dir_resolution_from_settings(monkeypatch):
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache="I:/cache/hub")
        assert archive_transcribe._cache_dir() == Path("I:/cache/hub")
        assert disk_hygiene.whisper_cache_dir() == Path("I:/cache/hub")


def test_cache_dir_env_beats_settings(monkeypatch):
    monkeypatch.setenv("VODRIP_WHISPER_CACHE", "Z:/env-cache")
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache="I:/settings-cache")
        assert archive_transcribe._cache_dir() == Path("Z:/env-cache")


def test_cache_dir_appdata_fallback(monkeypatch):
    """No env, no setting, no usable drive -> appdata whisper-models root
    (keeps the test hermetic: the auto pick probes real disks)."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.setattr("services.disk_hygiene.best_model_cache_drive", lambda: None)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache=None)
        assert str(archive_transcribe._cache_dir()).endswith("whisper-models")


def test_parakeet_cache_lives_under_models_root(monkeypatch, tmp_path):
    """Every weight lives under the AI-models root: the sherpa parakeet
    cache is <models root>/parakeet-models (nothing downloads at settings
    level)."""
    monkeypatch.delenv("VODRIP_WHISPER_CACHE", raising=False)
    monkeypatch.delenv("VODRIP_SHERRPA_CACHE", raising=False)
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(whisper_model_cache=str(tmp_path / "models"))
        assert archive_transcribe._parakeet_cache_dir() == tmp_path / "models" / "parakeet-models"
        assert archive_transcribe._parakeet_resolve_dir() is None  # nothing seeded -> None


# --- no whisper download default (engine reality) --------------------------

def test_fixed_engine_model_is_parakeet():
    """The engine model id is the fixed sherpa int8 parakeet repo — no
    faster-whisper id anywhere near the default."""
    assert PARAKEET_MODEL == "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    assert "faster-whisper" not in PARAKEET_MODEL
    assert archive_transcribe._asr_model_name() == PARAKEET_MODEL


def test_job_engine_parakeet_for_covered_and_unknown(monkeypatch):
    monkeypatch.setattr(archive_transcribe, "_parakeet_available", lambda: True)
    monkeypatch.setattr(archive_transcribe, "_parakeet_langs",
                        lambda: PARAKEET_LANG_CANDIDATES)
    assert archive_transcribe._job_engine("pt") == "parakeet"
    assert archive_transcribe._job_engine("es") == "parakeet"
    assert archive_transcribe._job_engine(None) == "parakeet"  # auto-detect
    assert archive_transcribe._job_engine("") == "parakeet"  # auto-detect


def test_job_engine_uncovered_language_is_clean_failure(monkeypatch):
    """ja/ko/zh/ar are KNOWN but outside parakeet's 26 European languages —
    a clean _AsrUnsupportedLanguage ('ASR unsupported'), never a fallback."""
    monkeypatch.setattr(archive_transcribe, "_parakeet_available", lambda: True)
    monkeypatch.setattr(archive_transcribe, "_parakeet_langs",
                        lambda: PARAKEET_LANG_CANDIDATES)
    for lang in ("ja", "ko", "zh", "ar"):
        with pytest.raises(_AsrUnsupportedLanguage) as ei:
            archive_transcribe._job_engine(lang)
        assert "ASR unsupported" in str(ei.value)
    assert "ja" not in PARAKEET_LANG_CANDIDATES
    assert {"pt", "en", "es"} <= PARAKEET_LANG_CANDIDATES


def test_job_engine_lane_unavailable_is_clean_failure(monkeypatch):
    """No parakeet at all -> _AsrLaneUnavailable ('ASR unavailable')."""
    monkeypatch.setattr(archive_transcribe, "_thread_pin", lambda: None)
    monkeypatch.setattr(archive_transcribe, "_effective_device", lambda: ("cpu", "int8"))
    monkeypatch.setattr(archive_transcribe, "_parakeet_available", lambda: False)
    with pytest.raises(_AsrLaneUnavailable) as ei:
        archive_transcribe._job_engine("pt")
    assert "ASR unavailable" in str(ei.value)


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


def test_best_model_cache_drive_fastest_tier_wins(monkeypatch):
    # NVMe below the 8 GB floor drops out; the SSD (rank 2) beats the big
    # HDD (rank 3) — models are small + hot, so speed beats headroom.
    monkeypatch.setattr(
        "services.disk_detect.disk_inventory",
        lambda: [
            _inv("C", 5 * 1024**3, 1),    # NVMe but below the 8 GB floor
            _inv("F", 100 * 1024**3, 2),  # SSD
            _inv("I", 400 * 1024**3, 3),  # HDD with lots of space -> loses
        ],
    )
    assert disk_hygiene.best_model_cache_drive() == "F:\\"


def test_best_model_cache_drive_ssd_wins_near_tie(monkeypatch):
    monkeypatch.setattr(
        "services.disk_detect.disk_inventory",
        lambda: [
            _inv("D", 120 * 1024**3, 3),  # HDD, 120 GB
            _inv("H", 100 * 1024**3, 1),  # NVMe, 100 GB — faster tier wins
        ],
    )
    assert disk_hygiene.best_model_cache_drive() == "H:\\"


def test_best_model_cache_drive_hdd_fallback(monkeypatch):
    """No fast tier has room -> the HDD is the last resort, not a competitor."""
    monkeypatch.setattr(
        "services.disk_detect.disk_inventory",
        lambda: [
            _inv("C", 3 * 1024**3, 1),  # NVMe below the floor
            _inv("F", 2 * 1024**3, 2),  # SSD below the floor
            _inv("I", 40 * 1024**3, 3),  # only the HDD qualifies
        ],
    )
    assert disk_hygiene.best_model_cache_drive() == "I:\\"


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
