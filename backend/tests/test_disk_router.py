"""Disk router tests — usage, status, and one-click cleanups against scratch
dirs only (APPDATA/LOCALAPPDATA/TEMP/VODRIP_* all point at tmp_path). The
real user profile and the real AI-models dir are never touched.
"""

import os
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from app import app
from routers import disk


@pytest.fixture
def scratch_env(tmp_path, monkeypatch):
    """Point every disk category at scratch dirs under tmp_path."""
    appdata = tmp_path / "appdata" / "VOD.RIP"
    localappdata = tmp_path / "localappdata" / "VOD.RIP"
    scratch_tmp = tmp_path / "tmp"
    appdata.mkdir(parents=True, exist_ok=True)
    localappdata.mkdir(parents=True, exist_ok=True)
    scratch_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata.parent))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata.parent))
    monkeypatch.setenv("TEMP", str(scratch_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))  # gettempdir() cache
    # _get_appdata_dir() honors VODRIP_APP_DATA before APPDATA (conftest sets it
    # for suite-wide isolation) — the logs category falls through to it, so the
    # scratch override must include it or logs scan the conftest temp dir.
    monkeypatch.setenv("VODRIP_APP_DATA", str(appdata))
    monkeypatch.setenv("VODRIP_ARCHIVE_DIR", str(appdata / "archive"))
    monkeypatch.setenv("VODRIP_WHISPER_CACHE", str(appdata / "whisper-models"))
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(appdata / "archive.db"))
    # Route the WS-8 cache root at the scratch tmp so the yt-dlp cache and
    # AI-models cache land where the fixture writes them; the preview_cache
    # category follows the DATA root (fastest-disk pick), pinned here too.
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(scratch_tmp))
    monkeypatch.setenv("VODRIP_DATA_DIR", str(scratch_tmp))
    return SimpleNamespace(appdata=appdata, tmp=scratch_tmp)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# --- usage -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_usage_reports_every_category(scratch_env, client):
    a = scratch_env.appdata
    _write(a / "archive" / "kick" / "a.mp4", 1000)
    _write(a / "archive" / "kick" / "b.mp4", 2000)
    _write(a / "whisper-models" / "parakeet" / "model.onnx", 4096)
    _write(a / "archive.db", 512)
    _write(a / "archive.db-wal", 256)
    _write(a / "archive.db-shm", 32)
    _write(a / "logs" / "app.log", 64)
    _write(a / "logs" / "nested" / "x.log", 32)
    _write(scratch_env.tmp / "kd_preview" / "sess" / "seg.ts", 128)
    _write(scratch_env.tmp / "VOD.RIP-Updates" / "patch.bin", 300)

    resp = await client.get("/api/disk/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["archive_vods"] == 3000
    assert data["ai_models"] == 4096
    assert data["db"] == 512 + 256 + 32
    assert data["logs"] == 64 + 32
    assert data["preview_cache"] == 128
    assert data["update_temps"] == 300
    assert data["total"] == 3000 + 4096 + 800 + 96 + 128 + 300


@pytest.mark.asyncio
async def test_usage_missing_dirs_are_zero(scratch_env, client):
    resp = await client.get("/api/disk/usage")
    assert resp.status_code == 200
    data = resp.json()
    for cat in ("archive_vods", "ai_models", "db", "logs", "preview_cache", "update_temps"):
        assert data[cat] == 0
    assert data["total"] == 0


# --- status ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_defaults_keep_count_five(scratch_env, client, monkeypatch):
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace()
        monkeypatch.setattr(disk, "_free_bytes", lambda: 100 * 1024**3)
        resp = await client.get("/api/disk/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["free_bytes"] == 100 * 1024**3
    assert data["threshold_bytes"] == 5 * 1024**3
    assert data["low"] is False
    assert data["keep_count"] == 5


@pytest.mark.asyncio
async def test_status_low_and_settings_keep_count(scratch_env, client, monkeypatch):
    monkeypatch.setattr(disk, "_free_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(disk, "_keep_count", lambda: 3)
    resp = await client.get("/api/disk/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["low"] is True
    assert data["keep_count"] == 3


# --- preview_cache / update_temps ------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_preview_cache_frees_bytes_keeps_root(scratch_env, client):
    root = scratch_env.tmp / "kd_preview"
    _write(root / "sess1" / "seg.ts", 100)
    _write(root / "sess2" / "a" / "b.ts", 50)
    resp = await client.post("/api/disk/cleanup", json={"category": "preview_cache"})
    assert resp.status_code == 200
    assert resp.json() == {"freed_bytes": 150}
    assert root.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_cleanup_update_temps(scratch_env, client):
    root = scratch_env.tmp / "VOD.RIP-Updates"
    _write(root / "setup.exe", 777)
    resp = await client.post("/api/disk/cleanup", json={"category": "update_temps"})
    assert resp.status_code == 200
    assert resp.json() == {"freed_bytes": 777}
    assert root.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_cleanup_unknown_category_400(scratch_env, client):
    resp = await client.post("/api/disk/cleanup", json={"category": "nope"})
    assert resp.status_code == 400


# --- ai_models -------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_ai_models_wipes_folder_contents(scratch_env, client):
    """The AI-models folder is a deliberate wipe now (parakeet is the one
    model family; the old prune-inactive-whisper guard is gone)."""
    cache = scratch_env.appdata / "whisper-models"
    _write(cache / "parakeet" / "model.onnx", 2048)
    _write(cache / "parakeet" / "tokens.txt", 512)
    _write(cache / "embed" / "onnx" / "model.onnx", 8192)

    resp = await client.post("/api/disk/cleanup", json={"category": "ai_models"})
    assert resp.status_code == 200
    assert resp.json() == {"freed_bytes": 2048 + 512 + 8192}
    assert cache.is_dir(), "the AI-models root itself is kept"
    assert list(cache.iterdir()) == [], "every weight under it is deleted"


@pytest.mark.asyncio
async def test_cleanup_ai_models_wipes_even_nested_unknown_files(scratch_env, client):
    """No active-model protection: anything under the folder is wiped, and a
    missing folder frees 0 bytes."""
    cache = scratch_env.appdata / "whisper-models"
    _write(cache / "nested" / "deep" / "file.bin", 10)
    resp = await client.post("/api/disk/cleanup", json={"category": "ai_models"})
    assert resp.status_code == 200
    assert resp.json() == {"freed_bytes": 10}
    assert list(cache.iterdir()) == []

    empty = scratch_env.appdata / "whisper-models"
    empty.rmdir()
    resp = await client.post("/api/disk/cleanup", json={"category": "ai_models"})
    assert resp.status_code == 200
    assert resp.json() == {"freed_bytes": 0}


# --- archive_vods -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_archive_vods_db_driven_keeps_newest(scratch_env, client, monkeypatch):
    """Real post-merge path: eviction is DB-driven (archive rows beyond the
    keep count, newest by started_at), not filesystem-mtime based."""
    from services import archive_db

    monkeypatch.setattr(disk, "_keep_count", lambda: 2)
    root = scratch_env.appdata / "archive" / "kick"
    root.mkdir(parents=True, exist_ok=True)
    sizes = {0: 100, 1: 200, 2: 400, 3: 800}
    for i, size in sizes.items():
        _write(root / f"vod{i}.mp4", size)
        archive_db.upsert_video({
            "platform": "kick",
            "video_id": f"vod{i}",
            "channel": "ch",
            "title": f"vod {i}",
            "started_at": f"2026-07-{28 + i:02d}T12:00:00Z",
            "duration_sec": 100.0,
            "archive_path": str(root / f"vod{i}.mp4"),
            "canonical_key": f"vod-{i}",
            "status": "ready",
        })

    resp = await client.post("/api/disk/cleanup", json={"category": "archive_vods"})
    assert resp.status_code == 200
    assert resp.json() == {"freed_bytes": 100 + 200}  # two oldest gone
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["vod2.mp4", "vod3.mp4"]


@pytest.mark.asyncio
async def test_cleanup_archive_vods_prefers_retention_module(scratch_env, client, monkeypatch):
    """Post-merge path: enforce_archive_vod_retention is called with the
    settings keep count and the freed bytes are measured around it."""
    monkeypatch.setattr(disk, "_keep_count", lambda: 5)
    called: dict = {}

    fake = types.ModuleType("services.archive_retention")

    def enforce(keep_count=None):
        called["keep_count"] = keep_count
        # evict everything to prove before/after measurement
        for p in (scratch_env.appdata / "archive").glob("**/*.mp4"):
            p.unlink()

    fake.enforce_archive_vod_retention = enforce
    monkeypatch.setitem(sys.modules, "services.archive_retention", fake)

    root = scratch_env.appdata / "archive" / "kick"
    _write(root / "old.mp4", 1000)
    _write(root / "new.mp4", 2000)

    resp = await client.post("/api/disk/cleanup", json={"category": "archive_vods"})
    assert resp.status_code == 200
    assert called == {"keep_count": 5}
    assert resp.json() == {"freed_bytes": 3000}
