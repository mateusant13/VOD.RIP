"""Stale or gate-refused cookies are refreshed from the browser, without a click.

Policy before 2026-10-20 was GAP-FILL ONLY: a platform that already had cookies
was never re-imported. Measured on the live app that day:

    services.browser_cookie_cdp: cdp cookies: Chrome/155 -> {... 'youtube': ...}
    services.browser_cookie_import: cookie auto-import (cdp): accepted=0 dropped=18
    services.youtube_auth: YouTube using cached cookies youtube.txt   <- 10 h old

The browser was holding a FRESH YouTube session and the importer dropped it
because the platform "already had cookies"; the app went on serving the 10-hour
file, and yt-dlp answered "Sign in to confirm you're not a bot" (the 503 the
owner saw). The extension is not running on every box, so "the extension is the
refresh path" left the store stale forever.

The narrow refresh pinned here: a platform is re-imported when its stored set is
old, or when that platform's gate is frozen (the platform itself is rejecting
what we hold). A fresh, healthy platform is still left alone, and a browser that
is logged out still cannot overwrite anything (KEEP_LISTS drops it).
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import browser_cookie_import as imp  # noqa: E402


def _status(platform: str, age_hours: float) -> dict:
    grabbed = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        platform: {
            "count": 5,
            "lastGrabAt": grabbed.isoformat(timespec="seconds"),
            "expiredCount": 0,
        }
    }


def test_an_old_stored_set_is_refreshed(monkeypatch):
    monkeypatch.setattr(
        "services.cookie_store.status", lambda: _status("youtube", 10.0)
    )
    monkeypatch.setattr("services.yt_gate.youtube_gate_active", lambda: False)

    refresh, gated = imp._stale_or_gated({"youtube"})

    assert refresh == {"youtube"}, "a 10 h old YouTube set must be re-read"
    assert gated == set()


def test_a_fresh_healthy_set_is_left_alone(monkeypatch):
    monkeypatch.setattr(
        "services.cookie_store.status", lambda: _status("youtube", 0.5)
    )
    monkeypatch.setattr("services.yt_gate.youtube_gate_active", lambda: False)

    refresh, gated = imp._stale_or_gated({"youtube"})

    assert refresh == set(), "a 30 min old, non-gated set must not be touched"


def test_a_gated_platform_is_refreshed_even_when_fresh(monkeypatch):
    monkeypatch.setattr(
        "services.cookie_store.status", lambda: _status("youtube", 0.5)
    )
    monkeypatch.setattr("services.yt_gate.youtube_gate_active", lambda: True)

    refresh, gated = imp._stale_or_gated({"youtube"})

    assert refresh == {"youtube"}
    assert gated == {"youtube"}, "the gate is what justifies bypassing the interval"


def test_the_interval_is_bypassed_for_a_gated_platform(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(imp, "_enabled", lambda: True)
    monkeypatch.setattr(imp, "_platforms_present", lambda: {"youtube"})
    monkeypatch.setattr(imp, "_stale_or_gated", lambda present: ({"youtube"}, {"youtube"}))
    monkeypatch.setattr(imp, "_read_state", lambda: {"last_attempt": 9e18})
    monkeypatch.setattr(
        imp, "_collect", lambda: ({"youtube": [{"name": "SID"}]}, "", "cdp")
    )
    monkeypatch.setattr(imp, "_write_state", lambda payload: None)

    def _upsert(flat):
        calls.append("upsert")
        return len(flat), 0

    monkeypatch.setattr("services.cookie_store.upsert_cookies", _upsert)

    result = imp.import_missing()

    assert calls == ["upsert"], "a gated platform must not wait out the interval"
    assert result["ok"] is True


def test_the_interval_still_guards_a_non_gated_pass(monkeypatch):
    monkeypatch.setattr(imp, "_enabled", lambda: True)
    monkeypatch.setattr(imp, "_platforms_present", lambda: {"youtube"})
    monkeypatch.setattr(imp, "_stale_or_gated", lambda present: (set(), set()))
    monkeypatch.setattr(imp, "_read_state", lambda: {"last_attempt": 9e18})
    monkeypatch.setattr(
        imp, "_collect", lambda: (_ for _ in ()).throw(AssertionError("must not read"))
    )

    result = imp.import_missing()

    assert result["reason"] == "checked recently"
