"""Pytest fixtures — isolate download JSON + archive/cookie DBs from real %APPDATA%."""

from __future__ import annotations

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

__all__ = ["purge_download_manager"]


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
