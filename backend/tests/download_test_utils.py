"""Helpers for backend tests (not named conftest — avoids stdlib 'tests' package clash)."""

from __future__ import annotations

import time


def purge_download_manager(mgr) -> None:
    mgr.cancel_all()
    time.sleep(0.05)
    state = mgr.get_active_and_history()
    for d in state["queue"]:
        mgr.discard_from_queue(d.download_id)
    for d in state["history"] + state["recent"]:
        mgr.remove_history(d.download_id)
