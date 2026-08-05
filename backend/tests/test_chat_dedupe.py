"""wt-chatdupe: chat duplicate fixes.

(a) insert_messages skips a second writer's copy of the same message within
    +/-2 s (cross-flush dedupe, keeping the existing <=60 s spam collapse);
(b) dedupe_messages() deletes EXACT duplicates keeping the MIN rowid
    (idempotent, bounded);
(c) YTLiveSink._tail survives the .part -> final rename without re-sending
    the whole chat (read position persisted across the reopen);
(d) preview_panel kicks the throttled Twitch backfill for chat-less archived
    VODs with a numeric id, mirroring the archive-search auto-backfill.

No network: archive_twitch.backfill_chat is monkeypatched; the tail test
drives _tail against a scratch tmpdir with a fake worker thread.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from services import archive_db
from services.chat_sinks.yt_live import YTLiveSink

PLATFORM = "twitch"
VIDEO = "__chat_dedupe__"


@pytest.fixture(autouse=True)
def _scrub():
    archive_db.execute("DELETE FROM messages WHERE video_id=?", (VIDEO,))
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (VIDEO,))
    yield
    archive_db.execute("DELETE FROM messages WHERE video_id=?", (VIDEO,))
    archive_db.execute("DELETE FROM videos WHERE video_id=?", (VIDEO,))


def _stored() -> list:
    return archive_db.query(
        "SELECT offset_sec, username, text, spam_count FROM messages "
        "WHERE platform=? AND video_id=? ORDER BY offset_sec, id",
        (PLATFORM, VIDEO),
    )


# --- (a) insert_messages cross-flush dedupe --------------------------------


def test_insert_dedupes_cross_writer_shifted_resend() -> None:
    """A second writer re-sending the same batch at +1 s offsets must not
    append copies: every run outside the batch head is skipped by the
    +/-2 s window."""
    archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 100.0, "username": "alice", "text": "hi"},
        {"offset_sec": 100.5, "username": "bob", "text": "yo"},
    ])
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 101.0, "username": "alice", "text": "hi"},
        {"offset_sec": 101.5, "username": "bob", "text": "yo"},
    ])
    assert accepted == 2, "accepted count still counts every row that arrived"
    rows = _stored()
    assert len(rows) == 2, "second writer must not append duplicate rows"
    assert [r["text"] for r in rows] == ["hi", "yo"]


def test_insert_merge_head_then_dedupe_tail() -> None:
    """Batch head merging into the stored row (<=60 s spam continuation)
    must not stop the +/-2 s window from skipping the re-sent tail."""
    archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 100.0, "username": "alice", "text": "hi"},
        {"offset_sec": 100.5, "username": "bob", "text": "yo"},
    ])
    accepted = archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 101.0, "username": "bob", "text": "yo"},
        {"offset_sec": 101.5, "username": "alice", "text": "hi"},
    ])
    assert accepted == 2
    rows = _stored()
    assert len(rows) == 2, "head merges and tail dedupes — no new rows"
    bob = next(r for r in rows if r["username"] == "bob")
    assert bob["spam_count"] == 2, "merged head bumps the stored row"


def test_insert_keeps_different_text_and_far_offset() -> None:
    """The window must not swallow a genuinely different message at a nearby
    offset, nor the same text more than 2 s away."""
    archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 100.0, "username": "alice", "text": "hi"},
    ])
    archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 100.5, "username": "alice", "text": "different"},
        {"offset_sec": 103.5, "username": "alice", "text": "hi"},
    ])
    assert len(_stored()) == 3


# --- (b) dedupe_messages() exact-key cleanup -------------------------------


def test_dedupe_messages_keeps_min_rowid_exact_key() -> None:
    """Exact (platform, video_id, offset_sec, username, text) duplicates are
    deleted keeping the MIN rowid; distinct rows survive; idempotent."""
    conn = archive_db.get_conn()
    with archive_db._lock:
        with conn:
            for off, user, text in (
                (10.0, "alice", "hello"),
                (20.0, "bob", "world"),
            ):
                conn.execute(
                    "INSERT INTO messages (platform, video_id, offset_sec,"
                    " username, text) VALUES (?, ?, ?, ?, ?)",
                    (PLATFORM, VIDEO, off, user, text),
                )
            # Pre-fix duplicates written at the SAME offsets (bypasses the new
            # insert-time dedupe, exactly like rows already in the real DB).
            for off, user, text in (
                (10.0, "alice", "hello"),
                (20.0, "bob", "world"),
                (30.0, "carol", "unique"),
            ):
                conn.execute(
                    "INSERT INTO messages (platform, video_id, offset_sec,"
                    " username, text) VALUES (?, ?, ?, ?, ?)",
                    (PLATFORM, VIDEO, off, user, text),
                )
    assert len(_stored()) == 5
    deleted = archive_db.dedupe_messages()
    assert deleted == 2
    rows = _stored()
    assert len(rows) == 3
    assert [r["username"] for r in rows] == ["alice", "bob", "carol"], (
        "the MIN-rowid copies must survive"
    )
    assert archive_db.dedupe_messages() == 0, "idempotent: second run deletes nothing"
    assert len(_stored()) == 3


def test_dedupe_messages_untouched_without_duplicates() -> None:
    archive_db.insert_messages(PLATFORM, VIDEO, [
        {"offset_sec": 1.0, "username": "alice", "text": "one"},
        {"offset_sec": 2.0, "username": "bob", "text": "two"},
    ])
    assert archive_db.dedupe_messages() == 0
    assert len(_stored()) == 2


# --- (c) yt_live tail position across the .part -> final rename ------------


def _chat_line(username: str, text: str, ts_usec: int) -> str:
    return json.dumps({
        "replayChatItemAction": {
            "actions": [{
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "authorName": {"simpleText": username},
                            "message": {"runs": [{"text": text}]},
                            "timestampUsec": str(ts_usec),
                        }
                    }
                }
            }]
        }
    }) + "\n"


class _FakeWorker:
    """Duck-typed yt-dlp worker thread: alive until the test says so."""

    def __init__(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def set_dead(self) -> None:
        self._alive = False


def test_yt_live_tail_position_survives_part_rename(tmp_path) -> None:
    """The .part -> final rename must not re-send the whole chat: the tail
    reopens the newer file at the saved position, so every line is flushed
    exactly once.

    A real os.replace() can't run here — on Windows a read handle (the
    tail's) blocks the rename (WinError 32) — so the final file is written
    fresh with the SAME prefix bytes plus the new lines, byte-identical to
    what the rename would produce. The reopen+seek path under test is
    identical."""
    part = tmp_path / "stream.live_chat.json.part"
    final = tmp_path / "stream.live_chat.json"
    lines = [
        _chat_line("alice", f"msg{i}", 1_000_000 + i * 1_000_000)
        for i in range(5)
    ]
    part.write_bytes("".join(lines[:3]).encode("utf-8"))

    collected: list[dict] = []

    def _collect(rows):
        collected.extend(rows)
        return len(rows)

    sink = YTLiveSink(video_id="yt-tail-vid", channel="chan", handle="chan",
                      flush_cb=_collect)
    worker = _FakeWorker()
    tail_thread = threading.Thread(
        target=sink._tail, args=(str(tmp_path), worker), daemon=True
    )
    tail_thread.start()
    try:
        time.sleep(1.0)  # tail opens the .part and reads msg0-msg2
        # yt-dlp's end-of-stream rename: the final file carries the same
        # bytes as the .part plus everything written before the rename.
        final.write_bytes("".join(lines).encode("utf-8"))
        time.sleep(1.0)  # tail wakes, picks up the newer file
    finally:
        worker.set_dead()
        tail_thread.join(timeout=10)
    assert not tail_thread.is_alive(), "tail must exit once the worker dies"
    sink.flush()
    texts = [r["text"] for r in collected]
    assert texts == ["msg0", "msg1", "msg2", "msg3", "msg4"], (
        "post-rename reopen must resume at the saved position, not re-send"
    )


# --- (d) preview-open backfill kick -----------------------------------------

_BACKFILL_VIDEOS = (
    "1234567890", "twitch-live-somechannel-1700000000", "9999999999",
)


@pytest.fixture()
def _no_network_backfill(monkeypatch):
    """Stub backfill_chat so kicked tasks stay offline (same pattern as
    test_archive_search_filters._no_network_backfill)."""
    calls: list[str] = []
    monkeypatch.setattr(
        "services.archive_twitch.backfill_chat",
        lambda channel, video_id, **kwargs: (calls.append(video_id), {"inserted": 0})[1],
    )
    return calls


def _reset_backfill_clocks() -> None:
    import routers.archive as ar

    with ar._backfill_lock:
        ar._last_auto_kick = 0.0
        ar._backfill_inflight.clear()
        ar._backfill_attempted_at.clear()


def _cleanup_backfill_videos() -> None:
    archive_db.execute(
        "DELETE FROM videos WHERE video_id IN"
        " ('1234567890','twitch-live-somechannel-1700000000','9999999999')"
    )
    archive_db.execute(
        "DELETE FROM messages WHERE video_id IN"
        " ('1234567890','twitch-live-somechannel-1700000000','9999999999')"
    )


async def test_preview_panel_kicks_backfill_for_chatless_twitch_vod(
    _no_network_backfill,
) -> None:
    from routers.preview import preview_panel, _PANEL_LIMIT_DEFAULT

    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": "1234567890",
        "channel": "somechannel",
        "title": "test vod",
    })
    _reset_backfill_clocks()
    try:
        payload = await preview_panel("twitch", "1234567890",
                                      limit=_PANEL_LIMIT_DEFAULT)
        assert payload["has_chat"] is False
        await asyncio.sleep(0.05)  # background task needs a loop tick
        assert _no_network_backfill == ["1234567890"], (
            "preview open must kick chat backfill for a chat-less twitch VOD"
        )
    finally:
        _cleanup_backfill_videos()


async def test_preview_panel_skips_synthetic_and_chatty_videos(
    _no_network_backfill,
) -> None:
    from routers.preview import preview_panel, _PANEL_LIMIT_DEFAULT

    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": "twitch-live-somechannel-1700000000",
        "channel": "somechannel",
        "title": "watchdog row",
    })
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": "9999999999",
        "channel": "somechannel",
        "title": "already has chat",
    })
    archive_db.insert_messages("twitch", "9999999999",
                               [{"offset_sec": 1.0, "username": "u", "text": "t"}])
    _reset_backfill_clocks()
    try:
        await preview_panel("twitch", "twitch-live-somechannel-1700000000",
                            limit=_PANEL_LIMIT_DEFAULT)
        await preview_panel("twitch", "9999999999", limit=_PANEL_LIMIT_DEFAULT)
        await asyncio.sleep(0.05)
        assert _no_network_backfill == [], (
            "synthetic watchdog ids and chatty VODs must not be kicked"
        )
    finally:
        _cleanup_backfill_videos()
