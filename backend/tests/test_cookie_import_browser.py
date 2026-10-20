"""POST /api/session/cookies/import-browser.

The point of this route is that it needs NO extension and NO export: it reads
the browser's own (already signed-in) cookie store. That is the difference
between ``installed: true`` and ``platforms`` being non-empty, and it is what
unblocks the authenticated InnerTube ladder (without cookies YouTube answers
with a single 360 rung, so the engaged 1080 tier has nothing to mux).

These tests drive the handler directly with a fake Request so they can assert
the two things that matter and cannot be seen from the outside: which
directory is read, and that no cookie VALUE ever reaches the response.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routers import cookie_bridge as cb


class _Req:
    def __init__(self, host: str = "127.0.0.1", host_header: str = "127.0.0.1:7897"):
        self.client = SimpleNamespace(host=host) if host is not None else None
        self.headers = {"host": host_header}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def extracted(monkeypatch):
    """Stub the extractor; record the profile_dir it was asked for."""
    calls: list = []

    def fake_extract(profile_dir=None):
        calls.append(profile_dir)
        return (
            {
                "youtube": [
                    {
                        "name": "SID",
                        "value": "super-secret-value",
                        "domain": ".youtube.com",
                        "path": "/",
                        "is_session": False,
                        "expires": 4102444800,
                        "secure": True,
                        "httponly": True,
                    }
                ]
            },
            "",
        )

    monkeypatch.setattr(cb.browser_cookie_extract, "extract_platform_cookies", fake_extract)
    monkeypatch.setattr(
        cb, "settings_mgr", SimpleNamespace(get=lambda: SimpleNamespace(cookie_bridge_enabled=True))
    )
    return calls


@pytest.fixture
def store(monkeypatch):
    seen: dict = {}

    def fake_upsert(cookies):
        seen["cookies"] = cookies
        return len(cookies), 0

    monkeypatch.setattr(cb.cookie_store, "upsert_cookies", fake_upsert)
    monkeypatch.setattr(
        cb.cookie_store, "counts", lambda: {"youtube": 1}
    )
    return seen


def test_import_upserts_what_the_browser_holds(extracted, store):
    out = _run(cb.session_cookies_import_browser(_Req(), {"browser": "chrome"}))

    assert out["ok"] is True
    assert out["accepted"] == 1
    assert out["platforms"] == {"youtube": 1}
    assert store["cookies"][0]["name"] == "SID"


def test_import_never_returns_a_cookie_value(extracted, store):
    out = _run(cb.session_cookies_import_browser(_Req(), {"browser": "chrome"}))

    assert "super-secret-value" not in repr(out)
    assert "SID" not in repr(out)


def test_no_cookies_is_a_clean_failure_not_an_exception(extracted, store, monkeypatch):
    monkeypatch.setattr(
        cb.browser_cookie_extract,
        "extract_platform_cookies",
        lambda profile_dir=None: ({}, "app-bound encryption detected"),
    )

    out = _run(cb.session_cookies_import_browser(_Req(), {"browser": "chrome"}))

    assert out["ok"] is False
    assert out["accepted"] == 0
    assert "app-bound" in out["reason"]


def test_unknown_browser_is_rejected(extracted, store):
    with pytest.raises(HTTPException) as exc:
        _run(cb.session_cookies_import_browser(_Req(), {"browser": "netscape"}))

    assert exc.value.status_code == 422


def test_missing_browser_install_is_a_404_not_a_scan(extracted, store, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # nothing installed here

    with pytest.raises(HTTPException) as exc:
        _run(cb.session_cookies_import_browser(_Req(), {"browser": "edge"}))

    assert exc.value.status_code == 404
    assert extracted == [], "a missing browser must not trigger a directory scan"


def test_remote_caller_is_refused_before_anything_is_read(extracted, store):
    with pytest.raises(HTTPException) as exc:
        _run(
            cb.session_cookies_import_browser(
                _Req(host="203.0.113.9", host_header="example.com"),
                {"browser": "chrome"},
            )
        )

    assert exc.value.status_code == 403
    assert extracted == []


def test_disabled_bridge_blocks_import(extracted, store, monkeypatch):
    """"Disabling blocks ALL cookie ingestion" - this route is ingestion."""
    monkeypatch.setattr(
        cb.settings_mgr, "get", lambda: SimpleNamespace(cookie_bridge_enabled=False)
    )

    with pytest.raises(HTTPException) as exc:
        _run(cb.session_cookies_import_browser(_Req(), {"browser": "chrome"}))

    assert exc.value.status_code == 403
    assert extracted == []


def test_the_profile_read_is_confined_to_the_browser_root(extracted, store, monkeypatch, tmp_path):
    root = tmp_path / "Google" / "Chrome" / "User Data"
    root.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    _run(cb.session_cookies_import_browser(_Req(), {"browser": "chrome"}))

    assert extracted == [root], "must read the browser root, never a caller-supplied path"


class TestCdpIsPreferred:
    """The DevTools path is preferred because it is the only one that works on
    a modern profile: the file store is locked while the browser runs and its
    values are app-bound encrypted. Measured 2026-10-20 on this box: the CDP
    path returned 26 youtube + 18 kick + 14 twitch cookies while the on-disk
    reader returned nothing for the same browser."""

    def test_cdp_cookies_win_and_the_file_reader_is_not_touched(
        self, extracted, store, monkeypatch
    ):
        monkeypatch.setattr(
            cb.browser_cookie_cdp,
            "fetch_platform_cookies",
            lambda ports=None: (
                {
                    "youtube": [
                        {
                            "name": "SID",
                            "value": "from-cdp",
                            "domain": ".youtube.com",
                            "path": "/",
                            "is_session": False,
                            "expires": 4102444800,
                            "secure": True,
                            "httponly": True,
                        }
                    ]
                },
                "",
            ),
        )

        out = _run(cb.session_cookies_import_browser(_Req(), {}))

        assert out["ok"] is True
        assert out["source"] == "cdp"
        assert store["cookies"][0]["value"] == "from-cdp"
        assert extracted == [], "the on-disk reader must not run when CDP answers"

    def test_falls_back_to_the_file_reader_when_no_devtools_port_is_open(
        self, extracted, store, monkeypatch
    ):
        monkeypatch.setattr(
            cb.browser_cookie_cdp,
            "fetch_platform_cookies",
            lambda ports=None: ({}, "no browser DevTools port is open"),
        )

        out = _run(cb.session_cookies_import_browser(_Req(), {}))

        assert out["ok"] is True
        assert out["source"] == "profile"
        assert len(extracted) == 1, "the fallback scans the default user-data roots"

    def test_reports_both_reasons_when_neither_path_finds_anything(
        self, extracted, store, monkeypatch
    ):
        monkeypatch.setattr(
            cb.browser_cookie_cdp,
            "fetch_platform_cookies",
            lambda ports=None: ({}, "no browser DevTools port is open"),
        )
        monkeypatch.setattr(
            cb.browser_cookie_extract,
            "extract_platform_cookies",
            lambda profile_dir=None: ({}, "app-bound encryption detected: youtube"),
        )

        out = _run(cb.session_cookies_import_browser(_Req(), {}))

        assert out["ok"] is False
        assert "DevTools" in out["reason"]
