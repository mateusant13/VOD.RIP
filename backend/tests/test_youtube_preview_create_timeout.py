"""Instant create: the preview POST never waits for the upstream resolve.

Rewritten 2026-10-21. The previous contract was "the POST waits, and a wedged
resolve is cut by a wall-clock timeout (504)". That contract could not deliver
the owner's requirement — a cold click answering in <=3 s — because the wait it
bounded IS the honest duration of an unauthenticated YouTube resolve (measured
10.4-13.6 s through the Vite proxy). Every cap tried made it worse: 4 s failed
100% of cold clicks, 8 s cut the 10-13 s ones, 15 s only made the failure rarer.

The contract now: the POST registers the session and answers immediately
(`resolving: true`), the resolve runs on its own pool, and its result is
adopted under the SAME session id. Failure is not a timeout — it is a terminal
`resolve_error` on the status poll. There is no 504 path left to test.

Run from anywhere: `python backend/tests/test_youtube_preview_create_timeout.py`
"""
from __future__ import annotations

import os
import sys
import time

_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from fastapi.testclient import TestClient

from app import app
from routers import preview as preview_router  # noqa: F401  (import keeps wiring honest)
from services.preview import session as preview_session

YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TWITCH_URL = "https://www.twitch.tv/videos/123456789"


class _Resolved:
    """A resolved session, shaped enough for adoption + response building.

    Everything is set on the INSTANCE: adoption copies `vars(resolved)`, so
    class attributes (or a __getattr__ fallback) would silently vanish.
    """

    def __init__(self, delay: float = 0.0):
        self.session_id = "internal-resolve-id"
        self.vod_url = TWITCH_URL
        self.master_url = "https://cdn.example/master.m3u8"
        self.entry_url = "https://cdn.example/master.m3u8"
        self.kind = "hls"
        self.platform = "Twitch"
        self.variant_entries = [(720, "https://cdn.example/720.m3u8")]
        self.variant_muxed = {}
        self.custom_master = None
        self.mux_status = "unnecessary"
        self.mux_error = None
        self.crop_start = 0.0
        self.crop_end = 30.0
        self.vod_duration = 90.0
        self.prefer_height = 360
        self.closed = False
        self.closed_at = 0.0
        self.is_live = False
        self.growing_vod = False
        self.anonymous = False
        self.http_headers = {}
        self.resource_map = {}
        self.cache_dir = preview_session.Path("G:/Temp/instant-create-fixture")
        self.last_access = time.time()
        self.delay = delay


def _wait_resolved(session_id: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        shell = preview_session.peek_session(session_id)
        if shell is not None and not shell.resolving:
            return
        time.sleep(0.02)
    raise AssertionError(f"session {session_id} never finished resolving")


def test_pending_create_answers_immediately_while_the_resolve_is_slow(monkeypatch):
    """A 4 s resolve must not hold the POST: the contract is <=3 s, always."""
    def _slow(*_args, **_kwargs):
        time.sleep(4.0)  # a cold YouTube resolve, in miniature
        return _Resolved()

    monkeypatch.setattr(preview_session._manager, "create_session", _slow)
    with TestClient(app) as c:
        t0 = time.monotonic()
        resp = c.post("/api/preview/session", json={"url": TWITCH_URL})
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200, (resp.status_code, resp.text[:256])
        body = resp.json()
        assert body["resolving"] is True, body
        assert body["variant_heights"] == [], body
        # The owner's hard rule: a click answers in <=3 s, or fails. The inline
        # window (2.5 s) is what this bounds — NOT the resolve.
        assert elapsed < 3.0, f"POST held the resolve for {elapsed:.2f}s"
        # ... and the session it returned is already addressable.
        st = c.get(f"/api/preview/session/{body['session_id']}/status").json()
        assert st["resolving"] is True
        assert st["playlist_ready"] is False
        assert st["mux_ready"] is False
        _wait_resolved(body["session_id"])
        # The refetch the client does after the poll: full body, same id.
        full = c.get(f"/api/preview/session/{body['session_id']}").json()
        assert full["session_id"] == body["session_id"]
        assert full["resolving"] is False
        assert full["variant_heights"] == [720]
        c.delete(f"/api/preview/session/{body['session_id']}")


def test_a_pending_shell_never_reads_as_ready():
    """The Twitch/Kick landmine: with no variants, those platforms are "ready".

    `preview_playlist_ready` returns True for every non-YouTube session, so an
    unresolved shell would have let the client attach an empty playlist and
    stall. All three predicates must refuse while `resolving` is true.
    """
    shell = preview_session._pending_session_shell(TWITCH_URL, 0.0, 0.0, 360)
    try:
        assert shell.resolving is True
        assert preview_session.preview_playlist_ready(shell) is False
        assert preview_session.preview_segment_buffer_ready(shell) is False
        assert preview_session.preview_mux_ready(shell) is False
        assert preview_session.session_variant_heights(shell) == []
        # A real directory, never the dataclass default `Path()` ("."): the
        # delete path rmtrees cache_dir, so "." would mean the process CWD.
        assert str(shell.cache_dir) not in ("", "."), shell.cache_dir
    finally:
        preview_session._manager._sessions.pop(shell.session_id, None)


def test_resolve_failure_is_a_terminal_status_not_a_timeout(monkeypatch):
    """A dead VOD says so at the poll, immediately — no 15 s spinner, no 504."""

    def _boom(*_args, **_kwargs):
        raise ValueError("upstream says gone")

    monkeypatch.setattr(preview_session._manager, "create_session", _boom)
    shell = preview_session.create_session_pending(
        "https://www.twitch.tv/videos/1", inline_wait_sec=0.0
    )
    _wait_resolved(shell.session_id)
    st = preview_session.preview_session_mux_status(shell.session_id)
    assert st["resolving"] is False
    assert "upstream says gone" in str(st["resolve_error"])
    assert st["mux_ready"] is False
    preview_session._manager._sessions.pop(shell.session_id, None)


def test_a_fast_resolve_is_adopted_under_the_same_session_id(monkeypatch):
    """The client already holds URLs built from the id it was given."""
    monkeypatch.setattr(
        preview_session._manager, "create_session", lambda *a, **kw: _Resolved()
    )
    shell = preview_session.create_session_pending(TWITCH_URL, inline_wait_sec=0.0)
    given_id = shell.session_id
    _wait_resolved(given_id)
    try:
        assert shell.session_id == given_id, "adoption must not change the id"
        assert shell.variant_entries == [(720, "https://cdn.example/720.m3u8")]
        assert preview_session.session_variant_heights(shell) == [720]
        assert preview_session.preview_playlist_ready(shell) is True
        assert "internal-resolve-id" not in preview_session._manager._sessions
    finally:
        preview_session._manager._sessions.pop(given_id, None)


if __name__ == "__main__":
    import inspect

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                continue  # monkeypatch-based, run under pytest
            fn()
    print("OK: instant preview create tests passed")
