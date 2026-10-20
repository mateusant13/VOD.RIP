"""Boot-time cookie fill from the user's own browser.

The rule these pin is the one that makes it safe to run unattended: it FILLS
GAPS and never overwrites. The extension is the refresh path (it pushes on
cookies.onChanged); letting a periodically imported browser value replace a
live session would be a downgrade dressed as a refresh.

Kick matters here more than it looks. It is the platform that most often has
NO auth_token in the browser (logged out), and silently skipping it is what
made "cookies work" look finished when it was not - youtube_authenticated was
true while kick had zero rows.
"""
from __future__ import annotations

import pytest

from services import browser_cookie_import as imp


def _cookie(name: str, domain: str, value: str = "v") -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "is_session": False,
        "expires": 4102444800,
        "secure": True,
        "httponly": True,
    }


@pytest.fixture
def bridge_db(monkeypatch, tmp_path):
    """Throwaway cookie DB + state file (state lives beside the DB)."""
    monkeypatch.setenv("VODRIP_COOKIE_DB", str(tmp_path / "cookies.db"))
    from services import cookie_store

    cookie_store._conn = None
    cookie_store._schema_ready = False
    yield cookie_store
    cookie_store._conn = None
    cookie_store._schema_ready = False


@pytest.fixture
def enabled(monkeypatch):
    from types import SimpleNamespace

    from services import browser_cookie_import as mod

    monkeypatch.setattr(
        mod,
        "_enabled",
        lambda: True,
    )
    import deps

    monkeypatch.setattr(
        deps,
        "settings_mgr",
        SimpleNamespace(get=lambda: SimpleNamespace(cookie_bridge_enabled=True)),
    )
    return True


@pytest.fixture
def browser(monkeypatch):
    """Stub both readers; records what was asked for."""
    calls: dict = {"cdp": 0, "file": 0}
    payload: dict = {"cookies": {}}

    def fake_cdp(ports=None):
        calls["cdp"] += 1
        return payload["cookies"], ("" if payload["cookies"] else "no port")

    def fake_file(profile_dir=None):
        calls["file"] += 1
        return {}, "no browser Cookies database found"

    monkeypatch.setattr(
        "services.browser_cookie_cdp.fetch_platform_cookies", fake_cdp
    )
    monkeypatch.setattr(
        "services.browser_cookie_extract.extract_platform_cookies", fake_file
    )
    return calls, payload


def test_a_missing_platform_is_filled(bridge_db, enabled, browser):
    calls, payload = browser
    payload["cookies"] = {"youtube": [_cookie("SID", ".youtube.com")]}

    out = imp.import_missing()

    assert out["ok"] is True
    assert out["imported"] == {"youtube": 1}
    assert bridge_db.counts().get("youtube") == 1


def test_kick_is_not_silently_skipped(bridge_db, enabled, browser):
    """The platform most often missing is the one that must be asked for."""
    calls, payload = browser
    payload["cookies"] = {
        "youtube": [_cookie("SID", ".youtube.com")],
        "kick": [_cookie("auth_token", ".kick.com")],
    }

    out = imp.import_missing(force=True)

    assert "kick" in out["imported"]
    assert bridge_db.counts().get("kick") == 1


def test_a_platform_that_already_has_cookies_is_never_overwritten(
    bridge_db, enabled, browser
):
    calls, payload = browser
    payload["cookies"] = {
        "youtube": [_cookie("SID", ".youtube.com", "browser-value")],
        "kick": [_cookie("auth_token", ".kick.com", "browser-kick")],
    }
    # youtube already stored (the extension's, fresher) - kick is missing.
    bridge_db.upsert_cookies([_cookie("SID", ".youtube.com", "extension-value")])

    imp.import_missing(force=True)

    stored = {c["name"]: c["value"] for c in bridge_db.list_cookies("youtube")}
    assert stored["SID"] == "extension-value", "the live session was clobbered"
    assert bridge_db.counts().get("kick") == 1


def test_nothing_to_do_makes_no_request_at_all(bridge_db, enabled, browser):
    calls, _payload = browser
    for platform, name, domain in (
        ("youtube", "SID", ".youtube.com"),
        ("twitch", "auth-token", ".twitch.tv"),
        ("kick", "auth_token", ".kick.com"),
    ):
        bridge_db.upsert_cookies([_cookie(name, domain)])

    out = imp.import_missing()

    assert out["ok"] is True
    assert out["imported"] == {}
    assert calls["cdp"] == 0 and calls["file"] == 0


def test_disabled_bridge_does_no_work(bridge_db, monkeypatch, browser):
    calls, payload = browser
    payload["cookies"] = {"youtube": [_cookie("SID", ".youtube.com")]}
    monkeypatch.setattr(imp, "_enabled", lambda: False)

    out = imp.import_missing(force=True)

    assert out["ok"] is False
    assert "disabled" in out["reason"]
    assert calls["cdp"] == 0


def test_the_interval_guard_stops_a_restart_loop(bridge_db, enabled, browser):
    calls, payload = browser
    payload["cookies"] = {}  # nothing to find: the failure case

    imp.import_missing()
    first = calls["cdp"]
    imp.import_missing()  # immediately again
    second = calls["cdp"]

    assert first >= 1
    assert second == first, "a boot loop must not hammer the DevTools ports"

    imp.import_missing(force=True)
    assert calls["cdp"] > first


def test_no_cookies_anywhere_is_a_reason_not_a_crash(bridge_db, enabled, browser):
    calls, payload = browser
    payload["cookies"] = {}

    out = imp.import_missing(force=True)

    assert out["ok"] is False
    assert out["reason"], "a failure must say why, not return an empty answer"
    assert calls["cdp"] >= 1 and calls["file"] >= 1, (
        "both readers must be consulted before giving up"
    )


def test_file_reader_is_the_fallback_when_no_devtools_port_answers(
    bridge_db, enabled, browser, monkeypatch
):
    calls, payload = browser
    payload["cookies"] = {}
    read: list = []

    def fake_file(profile_dir=None):
        calls["file"] += 1
        read.append(profile_dir)
        return ({"youtube": [_cookie("SID", ".youtube.com")]}, "")

    monkeypatch.setattr(
        "services.browser_cookie_extract.extract_platform_cookies", fake_file
    )

    out = imp.import_missing(force=True)

    assert out["ok"] is True
    assert out["source"] == "profile"
    assert read == [None], "the fallback scans the default user-data roots"
    assert bridge_db.counts().get("youtube") == 1


def test_start_importer_is_idempotent(monkeypatch):
    started: list = []
    monkeypatch.setattr(imp, "_STARTED", False)
    monkeypatch.setattr(
        imp.threading,
        "Thread",
        lambda **kw: type(
            "T", (), {"start": lambda self: started.append(kw.get("name"))}
        )(),
    )

    assert imp.start_importer(delay_sec=0) is True
    assert imp.start_importer(delay_sec=0) is False
    assert started == ["cookie-auto-import"]


def test_a_logged_out_platform_says_so(bridge_db, enabled, browser):
    """The browser holds kick cookies but none of the session ones: the answer
    must name that, not just report zero rows."""
    _calls, payload = browser
    payload["cookies"] = {
        "kick": [
            _cookie("cf_clearance", ".kick.com"),
            _cookie("KP_UIDz", ".kick.com"),
        ]
    }

    out = imp.import_missing(force=True)

    assert out["ok"] is False
    assert "kick" in out["reason"]
    assert "logged out" in out["reason"]
    assert "auth_token" in out["reason"]
    assert bridge_db.counts().get("kick", 0) == 0
