"""Pytest fixtures — isolate download JSON from real %APPDATA%."""

from __future__ import annotations

import pytest

from download_test_utils import purge_download_manager

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
