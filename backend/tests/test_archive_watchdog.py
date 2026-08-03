"""Watchdog integration test — fake live signals drive real sinks into a temp DB.

The env var MUST be set before the first services.archive_db import anywhere
in the pytest session; this module is the only importer, so it is set at
module top (before `from services import archive_db`), binding the global
connection to the temp DB.

Run from backend/: python -m pytest tests/test_archive_watchdog.py
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="watchdog-test-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db  # noqa: E402  (env must be set first)
from services import archive_watchdog as wd  # noqa: E402
from services.chat_sinks.base import ChatSink  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _watchdog_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB at module
    start (collection-order independent: later modules clobber the env var at
    import time), and drop it after so the next module rebinds fresh."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="watchdog-test-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False

CHANNEL = {"id": "ch_test", "twitchSlug": "testuser"}
STARTED_AT = "2026-08-02T10:00:00Z"


class StubSink(ChatSink):
    """Emits rows through the real flush path; optionally dies after N seconds."""

    platform = "twitch"

    def __init__(self, *, emit_interval: float = 0.02, die_after: float | None = None, **kwargs):
        super().__init__(**kwargs)
        self._emit_interval = emit_interval
        self._die_after = die_after
        self._t0: float | None = None
        self._n = 0

    def run(self) -> None:
        self._t0 = time.monotonic()
        while not self.stop_requested():
            if self._die_after is not None and time.monotonic() - self._t0 > self._die_after:
                self.disconnect_reason = "test death"
                return
            self._n += 1
            self.handle_row({
                "offset_sec": self._n / 10.0,
                "user_id": 1,
                "username": "stubuser",
                "text": f"stub row {self._n}",
                "badges": [],
                "emotes": [],
                "ts": None,
            })
            time.sleep(self._emit_interval)


def _wait_until(cond, timeout: float = 5.0, step: float = 0.05) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def _make_sinks():
    created = []

    def factory(platform, channel, entry, video_id, start_ms):
        sink = StubSink(video_id=video_id, channel=channel.get("twitchSlug"),
                        title=entry.get("title", ""), stream_start_ts=start_ms,
                        flush_interval=0.1)
        created.append(sink)
        return sink

    return created, factory


def test_capture_starts_rows_land_and_stream_end_closes_video():
    wd.stop_archive_watchdog()  # clean slate from any earlier test
    created, factory = _make_sinks()
    live = [True]

    def poll(channel):
        return ([{"platform": "twitch", "title": "Test Stream",
                  "url": "https://www.twitch.tv/testuser", "started_at": STARTED_AT}]
                if live[0] else [])

    try:
        wd.start_archive_watchdog(
            poll=poll, sink_factory=factory, channels_provider=lambda: [CHANNEL],
            poll_interval=0.05, restart_cooldown=0.05)
        assert _wait_until(lambda: len(wd.active_captures()) == 1), "capture never started"
        assert _wait_until(lambda: bool(archive_db.query(
            "SELECT * FROM messages WHERE platform='twitch'"))), "no rows landed"
        rows = archive_db.query("SELECT * FROM messages WHERE platform='twitch' ORDER BY offset_sec")
        assert len(rows) >= 2
        assert rows[0]["username"] == "stubuser"
        offsets = [r["offset_sec"] for r in rows]
        assert all(a <= b for a, b in zip(offsets, offsets[1:]))

        # videos row: status 'known' + canonical_key
        vids = archive_db.query("SELECT * FROM videos WHERE platform='twitch'")
        assert len(vids) == 1
        assert vids[0]["status"] == "known"
        assert vids[0]["canonical_key"] == "test-stream|2026-08-02"
        assert vids[0]["ended_at"] is None

        # stream ends -> capture stops, ended_at/duration_sec written
        live[0] = False
        assert _wait_until(lambda: bool(archive_db.query(
            "SELECT * FROM videos WHERE platform='twitch' AND ended_at IS NOT NULL")),
            timeout=8.0), "stream end never closed the video row"
        vids = archive_db.query("SELECT * FROM videos WHERE platform='twitch'")
        assert vids[0]["ended_at"] is not None
        assert vids[0]["duration_sec"] is not None and vids[0]["duration_sec"] > 0
    finally:
        wd.stop_archive_watchdog()


def test_youtube_real_video_id_used_when_provided():
    """A YouTube live entry carrying a real videoId stores it as video_id
    (archive rows must link to the actual video, not a synthetic id)."""
    wd.stop_archive_watchdog()
    created, factory = _make_sinks()

    def poll(channel):
        return [{"platform": "youtube", "title": "YT Live",
                 "url": "https://www.youtube.com/watch?v=AbCdEfGhIjK",
                 "videoId": "AbCdEfGhIjK", "started_at": STARTED_AT}]

    try:
        wd.start_archive_watchdog(
            poll=poll, sink_factory=factory, channels_provider=lambda: [CHANNEL],
            poll_interval=0.05, restart_cooldown=0.05)
        assert _wait_until(lambda: len(wd.active_captures()) == 1), "capture never started"
        cap = wd.active_captures()[0]
        assert cap.video_id == "AbCdEfGhIjK"
        vids = archive_db.query("SELECT * FROM videos WHERE platform='youtube'")
        assert len(vids) == 1
        assert vids[0]["video_id"] == "AbCdEfGhIjK"
    finally:
        wd.stop_archive_watchdog()


def test_youtube_synthetic_id_fallback_when_no_video_id():
    """Without an extracted videoId the watchdog keeps the synthetic
    <platform>-live-<slug>-<ms> id — that shape is what the frontend guard
    (isSyntheticArchiveId) recognises to disable preview affordances."""
    wd.stop_archive_watchdog()
    created, factory = _make_sinks()

    def poll(channel):
        return [{"platform": "youtube", "title": "YT Live",
                 "url": "https://www.youtube.com/watch?v=AbCdEfGhIjK",
                 "started_at": STARTED_AT}]

    try:
        wd.start_archive_watchdog(
            poll=poll, sink_factory=factory, channels_provider=lambda: [CHANNEL],
            poll_interval=0.05, restart_cooldown=0.05)
        assert _wait_until(lambda: len(wd.active_captures()) == 1), "capture never started"
        cap = wd.active_captures()[0]
        assert __import__("re").match(r"^youtube-live-[a-z0-9_]+-\d+$", cap.video_id), \
            f"expected synthetic id, got {cap.video_id!r}"
    finally:
        wd.stop_archive_watchdog()


def test_poll_live_passthrough_video_id(monkeypatch):
    """_poll_live must forward the videoId key from the live payload — the
    watchdog stores it as video_id, so dropping it here would silently
    regress every capture back to the synthetic id."""
    from routers import live as live_router

    monkeypatch.setattr(
        live_router, "_fetch_channel_live_payload",
        lambda ch: {"live": [{"platform": "YouTube", "title": "T", "url": "u",
                              "videoId": "AbCdEfGhIjK", "started_at": STARTED_AT}]})
    entries = wd._poll_live({"id": "ch_test", "youtubeSlug": "@x"})
    assert entries == [{"platform": "youtube", "title": "T", "url": "u",
                        "started_at": STARTED_AT, "videoId": "AbCdEfGhIjK"}]


def test_dead_sink_is_restarted_while_still_live():
    wd.stop_archive_watchdog()
    created, factory = _make_sinks()
    live = [True]

    def poll(channel):
        return ([{"platform": "twitch", "title": "Test Stream",
                  "url": "https://www.twitch.tv/testuser", "started_at": STARTED_AT}]
                if live[0] else [])

    try:
        wd.start_archive_watchdog(
            poll=poll, sink_factory=factory, channels_provider=lambda: [CHANNEL],
            poll_interval=0.05, restart_cooldown=0.2)
        assert _wait_until(lambda: len(created) == 1), "first sink never started"
        # let the first sink die; watchdog must spawn a replacement
        created[0]._die_after = 0.1
        assert _wait_until(lambda: len(created) >= 2, timeout=8.0), \
            "dead sink was not restarted"
        assert _wait_until(lambda: any(s.is_alive() for s in created))
        assert all("test death" == getattr(s, "disconnect_reason", None) or s.is_alive()
                   for s in created)
        live[0] = False
        assert _wait_until(lambda: len(wd.active_captures()) == 0, timeout=8.0), \
            "capture not stopped after stream end"
    finally:
        wd.stop_archive_watchdog()
