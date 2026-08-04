"""WS-8 disk detection + cache relocation + settings round-trip.

Drive enumeration is stdlib-only (ctypes GetLogicalDriveStringsA /
GetDriveTypeW + shutil.disk_usage). The machine-dependent assertions are
sanity-shaped (ordering by type rank then free space; biggest_fixed_drive is
the max-free FIXED drive); the pure ranking function and relocation logic are
fully unit-tested with fake drives / scratch dirs. The real-biggest-drive
proof lives in the module self-check (VODRIP_DISK_DETECT_SELFCHECK=1) and is
cross-checked against PowerShell Get-PSDrive on the dev machine.

Scratch-only: relocation runs on tmp dirs, settings round-trip on a
monkeypatched app-data dir. Real %APPDATA%/VOD.RIP is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import disk_detect
from services.disk_detect import (
    DRIVE_FIXED,
    DRIVE_REMOVABLE,
    _ranked,
    biggest_fixed_drive,
    free_space,
    relocate_cache,
)
from services.settings import SettingsManager, cache_root
from models.schemas import AppSettings


# --- drive ranking (pure) ---------------------------------------------------

def test_ranked_orders_by_type_then_free():
    drives = [
        ("E:\\", DRIVE_REMOVABLE, 1_000_000),
        ("C:\\", DRIVE_FIXED, 5_000),
        ("D:\\", DRIVE_FIXED, 9_000),
        ("F:\\", 5, 99_000_000),  # CD-ROM -> lowest rank despite free space
    ]
    ranked = _ranked(drives)
    assert ranked == [
        ("D:\\", 9_000),
        ("C:\\", 5_000),
        ("E:\\", 1_000_000),
        ("F:\\", 99_000_000),
    ], "fixed before removable before others; free desc within a type"


def test_ranked_empty():
    assert _ranked([]) == []


# --- live machine sanity (no writes, read-only stat calls) ------------------

def test_ranked_drives_live_are_wellformed():
    ranked = disk_detect.ranked_drives()
    if not ranked:  # non-Windows / no drives -> vacuous
        return
    for letter, free in ranked:
        assert len(letter) == 3 and letter[1] == ":" and letter.endswith("\\")
        assert isinstance(free, int) and free >= 0
    types = [disk_detect._drive_type(letter) for letter, _ in ranked]
    for i in range(len(types) - 1):
        assert disk_detect._drive_rank(types[i]) >= disk_detect._drive_rank(types[i + 1])
        if disk_detect._drive_rank(types[i]) == disk_detect._drive_rank(types[i + 1]):
            assert ranked[i][1] >= ranked[i + 1][1]


def test_biggest_fixed_drive_is_max_free_fixed(monkeypatch):
    monkeypatch.setattr(
        disk_detect, "_list_drives", lambda: ["C:\\", "D:\\", "E:\\"]
    )
    monkeypatch.setattr(
        disk_detect,
        "_drive_type",
        lambda d: DRIVE_FIXED if d in ("C:\\", "D:\\") else DRIVE_REMOVABLE,
    )
    monkeypatch.setattr(
        disk_detect,
        "free_space",
        lambda d: {"C:\\": 10, "D:\\": 50, "E:\\": 999}.get(d, 0),
    )
    assert biggest_fixed_drive() == "D:\\", "removable with more free space must lose"


def test_biggest_fixed_drive_none_when_no_fixed(monkeypatch):
    monkeypatch.setattr(disk_detect, "_list_drives", lambda: ["E:\\"])
    monkeypatch.setattr(disk_detect, "_drive_type", lambda d: DRIVE_REMOVABLE)
    monkeypatch.setattr(disk_detect, "free_space", lambda d: 100)
    assert biggest_fixed_drive() is None


def test_free_space_returns_nonnegative_int():
    assert isinstance(free_space(os.getcwd()), int)
    assert free_space(os.getcwd()) >= 0
    assert free_space("Z:\\definitely-not-a-drive") == 0


# --- relocation -------------------------------------------------------------

def _make_tree(root: Path) -> Path:
    src = root / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"x" * 1024)
    (src / "sub" / "b.bin").write_bytes(b"hello world")
    return src


def test_relocate_same_volume_skips(tmp_path):
    src = _make_tree(tmp_path)
    dst = tmp_path / "dst"
    assert relocate_cache(src, dst) == {"moved": False, "reason": "same-volume-skip"}
    assert src.is_dir() and (src / "a.bin").is_file()
    assert not dst.exists()


def test_relocate_cross_volume_copies_verifies_removes(tmp_path, monkeypatch):
    src = _make_tree(tmp_path)
    dst = tmp_path / "dst"
    monkeypatch.setattr(disk_detect, "_same_volume", lambda a, b: False)
    result = relocate_cache(src, dst)
    assert result == {"moved": True, "reason": "copied-and-verified"}
    assert not src.exists(), "source removed only after verification"
    assert (dst / "sub" / "b.bin").read_bytes() == b"hello world"
    assert (dst / "a.bin").read_bytes() == b"x" * 1024


def test_relocate_failure_leaves_source_intact(tmp_path, monkeypatch):
    src = _make_tree(tmp_path)
    monkeypatch.setattr(disk_detect, "_same_volume", lambda a, b: False)
    # Destination already exists -> copytree path refuses before copying.
    dst = tmp_path / "dst"
    dst.mkdir()
    result = relocate_cache(src, dst)
    assert result["moved"] is False and result["reason"].startswith("copy-failed:")
    assert src.is_dir() and (src / "sub" / "b.bin").is_file(), "source must survive a failed relocation"


def test_relocate_missing_source_noop(tmp_path):
    assert relocate_cache(tmp_path / "nope", tmp_path / "dst") == {
        "moved": False,
        "reason": "source-missing",
    }


# --- settings round-trip ----------------------------------------------------

def test_settings_cache_dir_roundtrip(monkeypatch, tmp_path):
    app_dir = tmp_path / "VOD.RIP"
    monkeypatch.setattr("services.settings._get_appdata_dir", lambda: app_dir)
    mgr = SettingsManager()
    mgr.save(AppSettings(cache_dir="D:/caches"))
    raw = json.loads((app_dir / "settings.json").read_text(encoding="utf-8"))
    assert raw["cache_dir"] == "D:/caches", "settings.json must round-trip cache_dir"
    reloaded = SettingsManager()
    assert reloaded.get().cache_dir == "D:/caches"


def test_settings_cache_dir_default_empty():
    assert AppSettings().cache_dir == ""
    assert AppSettings(cache_dir="").cache_dir == ""


# --- cache_root precedence --------------------------------------------------

def test_cache_root_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "env"))
    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(cache_dir="D:/explicit")
        assert cache_root() == tmp_path / "env", "env override beats the setting"


def test_cache_root_setting_wins_over_auto(monkeypatch):
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    with patch("deps.settings_mgr") as mgr, patch(
        "services.disk_detect.biggest_fixed_drive", return_value="I:\\"
    ):
        mgr.get.return_value = SimpleNamespace(cache_dir="D:/explicit")
        assert cache_root() == Path("D:/explicit")


def test_cache_root_auto_biggest_drive(monkeypatch):
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    with patch("deps.settings_mgr") as mgr, patch(
        "services.disk_detect.biggest_fixed_drive", return_value="I:\\"
    ):
        mgr.get.return_value = SimpleNamespace(cache_dir="")
        assert cache_root() == Path("I:\\") / "VOD.RIP-cache"


def test_cache_root_none_without_drive(monkeypatch):
    monkeypatch.delenv("VODRIP_CACHE_DIR", raising=False)
    with patch("deps.settings_mgr") as mgr, patch(
        "services.disk_detect.biggest_fixed_drive", return_value=None
    ):
        mgr.get.return_value = SimpleNamespace(cache_dir="")
        assert cache_root() is None
