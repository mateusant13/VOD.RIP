"""The two data-dir caches must move together, and only when they should.

Gap 2 of the prodb review. `archive_db._db_path()` memoizes the resolved DB
path so a warm panel open doesn't re-read settings (a deep copy of the whole
AppSettings), and `disk_hygiene.data_dir()` pins its auto (fastest-drive) pick
in `_auto_data_dir` so the drive probe runs once per process. The memo key used
to be env-only, which silently froze the whole precedence at its first
resolution: once the auto branch had answered, nothing could move the DB path —
not a settings save switching the pick back to Auto, not a direct change to the
pin — because the inputs the memo watched never moved.

One invariant per cache interaction:

1. a geometry-only save (data_dir untouched) drops the memo but must NOT
   re-arm the drive probe — saves are frequent, resets are not free;
2. a save that EDITS data_dir resets the pin, so the next resolution re-probes
   and `_db_path()` follows the fresh answer;
3. the pin is part of the memo key, so moving it forces a miss.

Cases 2 and 3 are the pre-fix repro: both go red against e136310 (pin never
reset, pin absent from the key). Case 1 goes the other way — it pins the
GATING, and goes red under the naive fix that resets unconditionally.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_db, disk_detect, disk_hygiene

# Drive roots the stubbed probe hands back. Never a real one, and the test
# only ever compares paths — nothing here creates or opens a directory.
BOOT_PIN = Path("H:\\VOD.RIP-data")
EXPLICIT_PICK = Path("D:\\seed")
REPROBED = Path("G:\\VOD.RIP-data")


@pytest.fixture(autouse=True)
def _isolate_caches(monkeypatch):
    """Scratch settings file, restorable pins, and a counting probe.

    Yields the probe counter so each case can assert on it.

    `VODRIP_ARCHIVE_DB` is set process-wide by tests/conftest.py, and
    `_db_path()` returns that override BEFORE consulting the memo — so it has
    to go for the precedence chain (and therefore the memo) to be exercised at
    all. `VODRIP_DATA_DIR` is cleared to '' (the env tier treats '' as absent),
    which leaves resolution in the settings -> auto chain the two caches share.
    """
    original_file = settings_mgr._settings_file
    temp_file = original_file.parent / f"settings_pincache_{os.getpid()}.json"
    temp_file.unlink(missing_ok=True)
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    settings_mgr._settings._vodrip_base = settings_mgr._settings.model_copy(deep=True)
    # Neutralize get()'s ffmpeg autofill: it re-enters save() on success, which
    # would add an unattributed invalidation to every case here.
    monkeypatch.setattr(settings_mgr, "_ffmpeg_probe_failed", True)

    monkeypatch.delenv("VODRIP_ARCHIVE_DB", raising=False)
    monkeypatch.setenv("VODRIP_DATA_DIR", "")
    monkeypatch.setattr(disk_hygiene, "_auto_data_dir", None)
    monkeypatch.setattr(archive_db, "_DB_PATH_MEMO", None)

    probe = {"n": 0}

    def fake_fastest_disk() -> str:
        probe["n"] += 1
        return "G:\\"

    monkeypatch.setattr(disk_detect, "fastest_disk", fake_fastest_disk)
    # Commit a baseline generation so every save below diffs against known
    # state (same seeding as test_settings_save_interleave.py).
    settings_mgr.save(settings_mgr.get())

    yield probe

    temp_file.unlink(missing_ok=True)
    settings_mgr._settings_file = original_file


def _save(**changes) -> AppSettings:
    """The production shape: get() -> set one field -> save()."""
    payload = settings_mgr.get()
    for key, value in changes.items():
        setattr(payload, key, value)
    return settings_mgr.save(payload)


def test_geometry_save_keeps_pin_but_drops_memo(_isolate_caches):
    """A save that doesn't touch data_dir must not re-arm the probe.

    The memo still drops — another settings key can move the path — but it
    re-resolves from the PRESERVED pin: `_auto_data_dir` intact, `fastest_disk`
    never called, same answer. An unconditional reset in `SettingsManager.save()`
    would put a PowerShell drive probe on the next DB touch instead; window
    geometry saves are frequent, so that is a real cost, not a nit.
    """
    probe = _isolate_caches
    disk_hygiene._auto_data_dir = BOOT_PIN  # as if boot resolved Auto
    warm = archive_db._db_path()
    assert warm == BOOT_PIN / "archive.db"
    assert archive_db._DB_PATH_MEMO is not None, "the warm path must be memoized"
    assert probe["n"] == 0, "a pinned answer must not probe"

    _save(window_geometry={"x": 10, "y": 20, "w": 1280, "h": 720})

    assert archive_db._DB_PATH_MEMO is None, (
        "a save must drop the memo even when data_dir is untouched"
    )
    assert disk_hygiene._auto_data_dir == BOOT_PIN, (
        "a geometry-only save must not reset the auto pick"
    )
    assert archive_db._db_path() == warm, "re-resolution must reuse the pinned drive"
    assert probe["n"] == 0, "the preserved pin must answer without a probe"


def test_data_dir_edit_resets_pin_and_reprobes(_isolate_caches):
    """Clearing an explicit pick back to Auto must re-probe and move.

    Boot pinned Auto to H:, the user then chose an explicit data disk, and now
    switches back to Auto. Pre-fix `save()` never touched the pin, so "Auto"
    silently resurrected the boot-time drive instead of re-evaluating the
    fastest one — and `_db_path()` kept serving the memoized answer on top of
    it.
    """
    probe = _isolate_caches
    _save(data_dir=str(EXPLICIT_PICK))
    disk_hygiene._auto_data_dir = BOOT_PIN  # as if boot resolved Auto
    assert archive_db._db_path() == EXPLICIT_PICK / "archive.db"

    _save(data_dir="")

    assert disk_hygiene._auto_data_dir is None, (
        "editing data_dir must reset the pinned auto pick"
    )
    assert archive_db._db_path() == REPROBED / "archive.db", (
        "_db_path() must follow the fresh probe, not the stale pin"
    )
    assert probe["n"] == 1, "the reset must re-arm exactly one probe"
    assert disk_hygiene._auto_data_dir == REPROBED, "the new pick re-pins"


def test_pin_change_forces_memo_miss(_isolate_caches):
    """The pin is part of the memo key, so moving it moves the answer.

    No save and no env change — the only inputs the old key watched. Pre-fix
    this was a memo hit that kept serving the old drive forever: the freeze
    `test_db_path_precedence` steps 3 -> 4 document (the same env state, a
    different pin, and a path that had to move).
    """
    probe = _isolate_caches
    disk_hygiene._auto_data_dir = BOOT_PIN
    assert archive_db._db_path() == BOOT_PIN / "archive.db"

    disk_hygiene._auto_data_dir = REPROBED

    assert archive_db._db_path() == REPROBED / "archive.db", (
        "the memo key must include disk_hygiene._auto_data_dir"
    )
    assert probe["n"] == 0, "a pin change is answered from the new pin, not a probe"
    assert archive_db._DB_PATH_MEMO is not None, "the miss must re-memoize"
