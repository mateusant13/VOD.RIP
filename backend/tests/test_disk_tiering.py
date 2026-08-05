"""Disk tiering tests — /api/disks inventory, fastest_disk() ranking, and
data_dir() resolution (Settings > Storage disk pickers).

Everything that shells out or reads real disks is monkeypatched: no
PowerShell, no real drives, no real app data.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from models.schemas import AppSettings
from routers import disk
from services import disk_detect, disk_hygiene


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _inv(letter, free, rank, media="SSD", bus="SATA", total=1000 * 1024**3):
    return {
        "drive": f"{letter}:\\",
        "label": "",
        "total_bytes": total,
        "free_bytes": free,
        "media_type": media,
        "bus_type": bus,
        "speed_rank": rank,
    }


def _fake_layout(disks=None, letters=None):
    return {"disks": disks or {}, "letters": letters or {}}


# --- _speed_rank -----------------------------------------------------------

def test_speed_rank_classification():
    # NVMe first, then SSD, then HDD, Unknown last.
    assert disk_detect._speed_rank("SSD", "NVMe") == 1
    assert disk_detect._speed_rank("NVMe", "NVMe") == 1
    assert disk_detect._speed_rank("SSD", "SATA") == 2
    assert disk_detect._speed_rank("SSD", "USB") == 2  # media wins over bus
    assert disk_detect._speed_rank("HDD", "SATA") == 3
    assert disk_detect._speed_rank("Unspecified", "USB") == 4
    assert disk_detect._speed_rank("", "") == 4


# --- disk_inventory (no PowerShell, no real drives) ------------------------

def test_inventory_shape_and_unknown_fallback(monkeypatch):
    monkeypatch.setattr(disk_detect, "_list_drives", lambda: ["C:\\", "D:\\"])
    monkeypatch.setattr(
        disk_detect,
        "_storage_layout",
        lambda: _fake_layout(
            disks={0: {"media_type": "NVMe", "bus_type": "NVMe"}},
            letters={"D": 0},
        ),
    )
    monkeypatch.setattr(
        disk_detect,
        "_drive_usage",
        lambda d: (500 * 1024**3, 100 * 1024**3),
    )
    monkeypatch.setattr(disk_detect, "_volume_label", lambda d: "Data" if d == "D:\\" else "")

    items = {i["drive"]: i for i in disk_detect.disk_inventory()}
    assert set(items) == {"C:\\", "D:\\"}
    # D maps to a physical NVMe disk; C has no mapping -> Unknown.
    assert items["D:\\"]["media_type"] == "NVMe"
    assert items["D:\\"]["bus_type"] == "NVMe"
    assert items["D:\\"]["speed_rank"] == 1
    assert items["D:\\"]["label"] == "Data"
    assert items["C:\\"]["media_type"] == "Unknown"
    assert items["C:\\"]["speed_rank"] == 4
    for item in items.values():
        assert item["total_bytes"] == 500 * 1024**3
        assert item["free_bytes"] == 100 * 1024**3


def test_inventory_skips_unreadable_drives(monkeypatch):
    monkeypatch.setattr(disk_detect, "_list_drives", lambda: ["C:\\", "Z:\\"])
    monkeypatch.setattr(disk_detect, "_storage_layout", lambda: _fake_layout())
    # Z has no media (empty CD-ROM) -> disk_usage raises -> skipped.
    monkeypatch.setattr(
        disk_detect,
        "_drive_usage",
        lambda d: None if d == "Z:\\" else (100 * 1024**3, 50 * 1024**3),
    )
    monkeypatch.setattr(disk_detect, "_volume_label", lambda d: "")
    items = disk_detect.disk_inventory()
    assert [i["drive"] for i in items] == ["C:\\"]


def test_inventory_powershell_failure_is_unknown(monkeypatch):
    """A failed PS probe (missing powershell) degrades to Unknown ranks."""
    monkeypatch.setattr(disk_detect, "_list_drives", lambda: ["C:\\"])
    monkeypatch.setattr(disk_detect, "_run_powershell", lambda _s: None)
    monkeypatch.setattr(
        disk_detect, "_drive_usage", lambda d: (100 * 1024**3, 50 * 1024**3)
    )
    monkeypatch.setattr(disk_detect, "_volume_label", lambda d: "")
    item = disk_detect.disk_inventory()[0]
    assert item["media_type"] == "Unknown"
    assert item["bus_type"] == "Unknown"
    assert item["speed_rank"] == 4


def test_storage_layout_parses_powershell_payload(monkeypatch):
    raw = {
        "disks": [
            {"DeviceId": 0, "FriendlyName": "NVMe Disk", "MediaType": "SSD", "BusType": "NVMe"},
            {"DeviceId": 1, "FriendlyName": "Data HDD", "MediaType": "HDD", "BusType": "SATA"},
        ],
        "partitions": [
            {"DriveLetter": "C", "DiskNumber": 0},
            {"DriveLetter": "d", "DiskNumber": 1},
        ],
    }
    monkeypatch.setattr(disk_detect, "_run_powershell", lambda _s: raw)
    monkeypatch.setattr(disk_detect, "_layout_cache", {})  # fresh TTL cache
    layout = disk_detect._storage_layout()
    assert layout["disks"][0]["media_type"] == "SSD"
    assert layout["disks"][1]["bus_type"] == "SATA"
    assert layout["letters"] == {"C": 0, "D": 1}  # letters normalized upper


# --- fastest_disk ----------------------------------------------------------

def test_fastest_disk_rank_then_free(monkeypatch):
    monkeypatch.setattr(
        disk,
        "disk_inventory",
        lambda: [
            _inv("C", 10 * 1024**3, 2),   # SSD, 10 GB
            _inv("D", 500 * 1024**3, 4),  # unknown, huge free — rank loses
            _inv("I", 9 * 1024**3, 1),    # NVMe, 9 GB -> wins on rank
        ],
    )
    assert disk.fastest_disk() == "I:\\"


def test_fastest_disk_tie_breaks_by_free(monkeypatch):
    monkeypatch.setattr(
        disk,
        "disk_inventory",
        lambda: [
            _inv("C", 10 * 1024**3, 1),
            _inv("I", 300 * 1024**3, 1),  # same rank, more free -> wins
        ],
    )
    assert disk.fastest_disk() == "I:\\"


def test_fastest_disk_excludes_low_free(monkeypatch):
    monkeypatch.setattr(
        disk,
        "disk_inventory",
        lambda: [
            _inv("C", 1 * 1024**3, 1),   # NVMe but < 2 GB -> excluded
            _inv("D", 100 * 1024**3, 2),  # only usable candidate
        ],
    )
    assert disk.fastest_disk() == "D:\\"


def test_fastest_disk_empty(monkeypatch):
    monkeypatch.setattr(disk, "disk_inventory", lambda: [])
    assert disk.fastest_disk() == ""


# --- /api/disks ------------------------------------------------------------

@pytest.mark.asyncio
async def test_disks_route_response(client, monkeypatch):
    monkeypatch.setattr(
        disk,
        "disk_inventory",
        lambda: [
            _inv("C", 90 * 1024**3, 1, media="NVMe", bus="NVMe"),
            _inv("I", 344 * 1024**3, 1, media="NVMe", bus="NVMe"),
        ],
    )
    monkeypatch.setattr(disk, "biggest_fixed_drive", lambda: "I:\\")
    resp = await client.get("/api/disks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["drives"]) == 2
    assert data["drives"][0] == {
        "drive": "C:\\",
        "label": "",
        "total_bytes": 1000 * 1024**3,
        "free_bytes": 90 * 1024**3,
        "media_type": "NVMe",
        "bus_type": "NVMe",
        "speed_rank": 1,
    }
    assert data["fastest"] == "I:\\"  # tie rank -> most free
    assert data["biggest"] == "I:\\"


# --- data_dir() ------------------------------------------------------------

def _patch_settings(data_dir_value):
    mgr = SimpleNamespace(get=lambda: SimpleNamespace(data_dir=data_dir_value))
    return patch("deps.settings_mgr", mgr)


def test_data_dir_env_wins(monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", "X:\\env-data")
    with _patch_settings("Y:\\settings-data"):
        assert disk_hygiene.data_dir() == Path("X:\\env-data")


def test_data_dir_setting_overrides_default(monkeypatch):
    monkeypatch.delenv("VODRIP_DATA_DIR", raising=False)
    with _patch_settings("Y:\\settings-data"):
        assert disk_hygiene.data_dir() == Path("Y:\\settings-data")


def test_data_dir_blank_env_falls_through(monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", "   ")
    with _patch_settings("Y:\\settings-data"):
        assert disk_hygiene.data_dir() == Path("Y:\\settings-data")


def test_data_dir_defaults_to_appdata(monkeypatch):
    monkeypatch.delenv("VODRIP_DATA_DIR", raising=False)
    with _patch_settings(""):
        assert disk_hygiene.data_dir() == disk_hygiene._get_appdata_dir()


def test_data_dir_none_setting_falls_back(monkeypatch):
    monkeypatch.delenv("VODRIP_DATA_DIR", raising=False)
    with _patch_settings(None):
        assert disk_hygiene.data_dir() == disk_hygiene._get_appdata_dir()


# --- settings persistence --------------------------------------------------

@pytest.mark.asyncio
async def test_settings_route_persists_data_dir(client):
    class FakeMgr:
        def __init__(self):
            self.saved = None

        def get(self):
            return AppSettings()

        def save(self, settings):
            self.saved = settings

    mgr = FakeMgr()
    # The route uses module-level names bound at import (from deps import
    # settings_mgr), so patch them on routers.settings, not on deps.
    with patch("routers.settings.settings_mgr", mgr), patch("routers.settings.download_mgr") as dm:
        resp = await client.post(
            "/api/settings",
            json={"cache_dir": "", "data_dir": "D:\\VOD.RIP-data"},
        )
    assert resp.status_code == 200
    assert dm.apply_settings.called
    saved = resp.json()
    assert saved["data_dir"] == "D:\\VOD.RIP-data"
    assert saved["cache_dir"] == ""
    assert mgr.saved.data_dir == "D:\\VOD.RIP-data"

    # Blank data_dir clears back to auto ('').
    with patch("routers.settings.settings_mgr", mgr), patch("routers.settings.download_mgr"):
        resp = await client.post("/api/settings", json={"data_dir": "  "})
    assert resp.status_code == 200
    assert resp.json()["data_dir"] == ""
