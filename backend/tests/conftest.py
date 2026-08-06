"""Pytest fixtures — isolate download JSON + archive/cookie DBs from real %APPDATA%."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from download_test_utils import purge_download_manager

# Isolate the archive + cookie stores BEFORE any test module imports the app:
# services.archive_db runs a module-level self-check that opens the DB on
# import (and services.cookie_store similarly), so without this the first
# test module in a merged run (alphabetically test_api_integration.py via
# `from app import app`) would bind the shared connection to the REAL
# %APPDATA%/VOD.RIP/archive.db and the kind-column migration + self-check
# would run on user data. Per-test modules may still override the env with
# their own scratch DB; this guarantees they never fall through to the real
# one.
_TMP = Path(tempfile.mkdtemp(prefix="vodrip-tests-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ["VODRIP_COOKIE_DB"] = str(_TMP / "cookies.db")
# Isolate the settings/history/queue JSON + cookie/whisper dirs the same way:
# app.py constructs DownloadManager/SettingsManager at import, before any
# per-test fixture can run, so an env override (not a patch) is the only
# thing that keeps those import-time singletons off the REAL %APPDATA%.
os.environ["VODRIP_APP_DATA"] = str(_TMP / "VOD.RIP")

# Pin the routed cache root (WS-8: cache_dir setting / biggest-fixed-drive
# auto pick) to scratch. Without this, the auto pick on a dev machine
# resolves to a REAL data drive (e.g. I:\) and any test that touches the
# whisper/yt-dlp/preview/embed cache paths would create dirs there. Per-cache
# env knobs (VODRIP_WHISPER_CACHE, VODRIP_EMBED_CACHE) still win; tests that
# need the real auto-pick behavior delenv VODRIP_CACHE_DIR.
os.environ.setdefault("VODRIP_CACHE_DIR", str(_TMP / "cache"))

# Same for the data root (Settings > Storage data-disk pick): the auto
# default resolves to the FASTEST real drive (fastest_disk -> PowerShell
# probe), so without this pin any test touching data_dir()/preview_root()
# would stall on a real probe and create dirs on a real data drive. Tests
# that exercise the auto behavior delenv VODRIP_DATA_DIR and patch the
# disk inventory (see test_disk_tiering.py).
os.environ.setdefault("VODRIP_DATA_DIR", str(_TMP / "data"))

# Snapshot of the REAL %APPDATA% archive.db taken here, before any test
# module import can run the archive/cookie-store self-checks. The cookie
# store's real file is the same archive.db (VODRIP_COOKIE_DB unset →
# appdata/archive.db), so one hash covers both stores. test_cookie_bridge.py
# asserts the file is still byte-identical at the end of a merged run.
def _sha256_or_none(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


REAL_APPDATA_DB_SHA256 = _sha256_or_none(
    Path(os.environ.get("APPDATA", "")) / "VOD.RIP" / "archive.db"
)

__all__ = ["purge_download_manager", "REAL_APPDATA_DB_SHA256"]


@pytest.fixture(autouse=True)
def _isolated_download_appdata(monkeypatch, tmp_path):
    app_dir = tmp_path / "VOD.RIP"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("services.settings._get_appdata_dir", lambda: app_dir)
    yield app_dir


@pytest.fixture
def download_test_counter():
    count = {"n": 0}

    def tick(mgr) -> None:
        count["n"] += 1
        if count["n"] % 10 == 0:
            purge_download_manager(mgr)

    return tick
