"""Full-chat-history coverage — the "chat stops at ~6 minutes" regression suite.

A VOD's stored chat is only "complete" when the newest row reaches the
video's end (archive_db.chat_covered). Partial captures — a backfill that
died mid-sweep, a run that happened while the broadcast was still live, a
watchdog capture of only the watched window — must be RESUMED (incremental,
from MAX(offset_sec)) instead of served as the full history forever. This
file pins that contract:

  * chat_covered: short head + known duration = False; head near the end =
    True; duration unknown = True once rows exist; no rows = False;
  * the scheduler guard: a partial capture with a done job stays covered
    for the scheduler (bounded) but is a RESUME for the user-facing lanes
    (retry_fresh_failed=True) — and the stable-id 'done' row is requeued
    in place, never orphaned;
  * preview_backfill_status: partial -> 'running' (kick owed, panel
    self-heals), full -> 'done', terminal no-chat marker -> 'idle';
  * the offset-cursor sweep: exhausts N pages to end-of-chat, dedupes the
    same-offset boundary re-fetch, and stops at the max_messages ceiling.

No network: archive_twitch._post_comments_page is monkeypatched.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="chat-full-history-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from services import archive_scheduler  # noqa: E402
from services import archive_twitch  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _seed_video(vid: str, duration: float | None = None) -> None:
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": vid,
        "channel": "cellbit",
        "title": f"vod {vid}",
        "started_at": "2026-08-01T00:00:00Z",
        "duration_sec": duration,
        "kind": "vod",
    })


def _row(offset: float, text: str = "hi") -> dict:
    return {
        "offset_sec": offset,
        "username": "lubu",
        "text": text,
        "badges": [],
        "emotes": [],
        "ts": "2026-08-01T20:00:00Z",
    }


def _node(offset: float, text: str | None = None) -> dict:
    """A minimal VideoComment GQL node (the shape _message_row consumes).
    Distinct username+text per offset so insert_messages' 60 s spam
    collapse (identical user+text within a minute merges rows) can't merge
    the sweep's pages into one row."""
    text = text or f"m{int(offset)}"
    return {
        "id": f"c{int(offset)}",
        "contentOffsetSeconds": offset,
        "createdAt": "2026-08-01T20:00:00Z",
        "commenter": {"id": f"u{int(offset)}", "login": f"u{int(offset)}", "displayName": f"u{int(offset)}"},
        "message": {"fragments": [{"text": text}], "userBadges": []},
    }


def _chat_jobs(vid: str) -> list:
    return list(archive_db.query(
        "SELECT * FROM archive_jobs WHERE kind='chat' AND platform='twitch' AND video_id=? "
        "ORDER BY created_at",
        (vid,),
    ))


# --- 1. coverage semantics -------------------------------------------------


def test_chat_covered_semantics(scratch_db):
    """A short head (rows 0..16 min of a 4.87 h VOD) is NOT covered; the
    head reaching the video's end is. Duration-less captures (watchdog
    synthetic ids) are covered once rows exist — nothing to measure."""
    _seed_video("6101", duration=17526.0)
    assert archive_db.chat_covered("twitch", "6101") is False, "no rows = not covered"
    archive_db.insert_messages("twitch", "6101", [_row(14.0), _row(947.0)])
    assert archive_db.chat_covered("twitch", "6101") is False, (
        "rows ending at 947s of a 17526s VOD must be INCOMPLETE"
    )
    archive_db.insert_messages("twitch", "6101", [_row(17500.0)])
    assert archive_db.chat_covered("twitch", "6101") is True, (
        "rows reaching within the margin of the VOD end are covered"
    )
    # Unknown duration: rows exist -> covered (nothing to measure against).
    _seed_video("6102", duration=None)
    assert archive_db.chat_covered("twitch", "6102") is False
    archive_db.insert_messages("twitch", "6102", [_row(5.0)])
    assert archive_db.chat_covered("twitch", "6102") is True
    # Unknown video row.
    assert archive_db.chat_covered("twitch", "9999") is False


# --- 2. scheduler guard: partial capture resumes on user action only -------


def test_guard_partial_capture_resume_vs_scheduler(scratch_db):
    """A partial capture with a done job is covered for the scheduler (no
    re-kick loop) but is a RESUME for the user-facing lanes
    (retry_fresh_failed=True — preview/search open the chat now). A fully
    covered video and the no-chat marker stay covered everywhere."""
    _seed_video("6201", duration=3600.0)
    archive_db.insert_messages("twitch", "6201", [_row(947.0)])  # partial head
    archive_db.enqueue_job("tw-backfill-p1", "chat", "twitch", "6201")
    archive_db.update_job("tw-backfill-p1", status="done")
    assert archive_scheduler._chat_job_guard("twitch", "6201") is True, (
        "scheduler lane must not re-kick a done partial capture (bounded)"
    )
    assert archive_scheduler._chat_job_guard("twitch", "6201", retry_fresh_failed=True) is False, (
        "the user asking now must resume the partial capture"
    )
    # A queued/running job covers both lanes (the resume is in flight).
    _seed_video("6204", duration=3600.0)
    archive_db.insert_messages("twitch", "6204", [_row(947.0)])
    archive_db.enqueue_job("tw-backfill-p4", "chat", "twitch", "6204")
    archive_db.update_job("tw-backfill-p4", status="running")
    assert archive_scheduler._chat_job_guard("twitch", "6204") is True
    assert archive_scheduler._chat_job_guard("twitch", "6204", retry_fresh_failed=True) is True

    # Full coverage: done job + head at the end -> covered for everyone.
    _seed_video("6202", duration=1000.0)
    archive_db.insert_messages("twitch", "6202", [_row(999.0)])
    archive_db.enqueue_job("tw-backfill-p2", "chat", "twitch", "6202")
    archive_db.update_job("tw-backfill-p2", status="done")
    assert archive_scheduler._chat_job_guard("twitch", "6202") is True
    assert archive_scheduler._chat_job_guard("twitch", "6202", retry_fresh_failed=True) is True

    # No-chat marker: done job on a chat-less video is permanent.
    _seed_video("6203", duration=1000.0)
    archive_db.enqueue_job("tw-backfill-p3", "chat", "twitch", "6203")
    archive_db.update_job("tw-backfill-p3", status="done")
    assert archive_scheduler._chat_job_guard("twitch", "6203") is True
    assert archive_scheduler._chat_job_guard("twitch", "6203", retry_fresh_failed=True) is True


def test_partial_done_row_requeued_in_place_for_user_resume(scratch_db, monkeypatch):
    """The stable kick-lane job id already exists as 'done' on a partial
    capture: the user resume requeues THAT row in place (no orphaned id,
    no duplicate row) and the sweep continues from the stored head."""
    _seed_video("6301", duration=3600.0)
    archive_db.insert_messages("twitch", "6301", [_row(947.0)])
    archive_db.enqueue_job("tw-backfill-6301", "chat", "twitch", "6301")
    archive_db.update_job("tw-backfill-6301", status="done")
    calls = {"n": 0}

    def fake_page(vid, offset, size):
        calls["n"] += 1
        if calls["n"] > 3:
            return []  # tail ends after 3 advancing pages
        base = float(offset)
        return [_node(base), _node(base + 30.0), _node(base + 60.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    out = archive_twitch.backfill_chat("cellbit", "6301", max_messages=100)
    assert out["inserted"] == 6, "only the nodes beyond the stored head are new"
    jobs = _chat_jobs("6301")
    assert len(jobs) == 1 and jobs[0]["id"] == "tw-backfill-6301", (
        "the resume must reuse the stable id, not create a second row"
    )
    assert jobs[0]["status"] == "done" and jobs[0]["error"] is None
    hi = archive_db.query(
        "SELECT MAX(offset_sec) m FROM messages WHERE platform='twitch' AND video_id=?",
        ("6301",),
    )[0]["m"]
    assert hi == 1127.0, "the sweep must advance past the stored head"


# --- 3. preview panel status ------------------------------------------------


def test_preview_status_partial_is_running_and_full_is_done(scratch_db):
    """The panel's status envelope drives the poll loop: a partial capture
    is 'running' (kick owed — the panel keeps polling and the resume
    fires), full coverage is 'done', and the no-chat marker is 'idle'."""
    import routers.archive as ar

    with ar._backfill_lock:
        ar._backfill_inflight.clear()
        ar._backfill_attempted_at.clear()

    # Partial head, no job: 'running' (kick owed) — self-heal.
    _seed_video("6401", duration=3600.0)
    archive_db.insert_messages("twitch", "6401", [_row(947.0)])
    assert ar.preview_backfill_status("twitch", "6401")[0] == "running", (
        "a partial capture must keep the panel polling so the resume fires"
    )
    # Full coverage: 'done'.
    archive_db.insert_messages("twitch", "6401", [_row(3599.0)])
    assert ar.preview_backfill_status("twitch", "6401")[0] == "done"
    # Terminal no-chat marker: 'idle' (panel stops polling, empty timeline).
    _seed_video("6402", duration=3600.0)
    archive_db.enqueue_job("tw-backfill-s2", "chat", "twitch", "6402")
    archive_db.update_job("tw-backfill-s2", status="done")
    assert ar.preview_backfill_status("twitch", "6402")[0] == "idle"
    # Queued worker job: 'running' (bounded polls while the worker owns it).
    _seed_video("6403", duration=3600.0)
    archive_db.enqueue_job("tw-backfill-s3", "chat", "twitch", "6403")
    assert ar.preview_backfill_status("twitch", "6403")[0] == "running"


async def test_preview_kick_resumes_partial_capture(scratch_db, monkeypatch):
    """kick_preview_backfill on a partial capture starts a resume task
    (the guard passes with retry_fresh_failed=True), and a full capture
    consumes no budget."""
    import routers.archive as ar

    with ar._backfill_lock:
        ar._last_auto_kick = 0.0
        ar._backfill_inflight.clear()
        ar._backfill_attempted_at.clear()

    _seed_video("6501", duration=3600.0)
    archive_db.insert_messages("twitch", "6501", [_row(947.0)])
    archive_db.enqueue_job("tw-backfill-k1", "chat", "twitch", "6501")
    archive_db.update_job("tw-backfill-k1", status="done")
    _seed_video("6502", duration=3600.0)
    archive_db.insert_messages("twitch", "6502", [_row(3599.0)])
    spawned: list = []

    async def fake_run(*a, **k):
        spawned.append(a)

    monkeypatch.setattr(ar, "_run_backfill", fake_run)
    try:
        assert ar.kick_preview_backfill("twitch", "6501") == "queued", (
            "opening the chat of a partial capture must kick the resume"
        )
        await asyncio.sleep(0.02)  # the spawned task needs a loop tick
        assert ar.kick_preview_backfill("twitch", "6502") == "", (
            "a fully covered video must not consume the kick budget"
        )
        assert len(spawned) == 1
        assert spawned[0][:2] == ("6501", "cellbit")
    finally:
        with ar._backfill_lock:
            ar._backfill_inflight.clear()
            ar._backfill_attempted_at.clear()


# --- 4. offset-cursor sweep: pages, dedupe, terminal, ceiling ---------------


def test_forward_sweep_exhausts_pages_and_dedupes(scratch_db, monkeypatch):
    """The forward sweep walks the offset cursor page by page to
    end-of-chat (empty page), deduping the same-offset boundary re-fetch —
    no duplicate rows, no premature stop."""
    _seed_video("6601", duration=3600.0)
    requested: list[float] = []

    def fake_page(vid, offset, size):
        requested.append(float(offset))
        page = len(requested)
        if page > 5:  # after 5 data pages the API answers empty -> end
            return []
        base = float(offset)
        # First node sits AT the requested offset (the boundary re-fetch
        # duplicate) — the sweep must skip it and keep the advancing ones.
        return [_node(base), _node(base + 30.0), _node(base + 60.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    out = archive_twitch.backfill_chat("cellbit", "6601", max_messages=100)
    assert out["stopped"] == "end_of_chat"
    assert out["pages"] == 6, "5 data pages + the empty terminal page"
    assert out["inserted"] == 10, "2 new rows per page, the boundary dup skipped"
    assert requested == [0.0, 60.0, 120.0, 180.0, 240.0, 300.0], (
        "each page must resume exactly where the last one advanced to"
    )
    n = archive_db.query(
        "SELECT COUNT(*) n FROM messages WHERE platform='twitch' AND video_id=?",
        ("6601",),
    )[0]["n"]
    assert n == 10, "no duplicate rows may reach the archive"


def test_forward_sweep_stops_at_max_messages_ceiling(scratch_db, monkeypatch):
    """max_messages is the hard ceiling (the worker lane's 100k bound): the
    sweep stops mid-tail and reports it, so the caller can resume later."""
    _seed_video("6602", duration=3600.0)

    def fake_page(vid, offset, size):
        base = float(offset)
        return [_node(base + 30.0), _node(base + 60.0), _node(base + 90.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    # 6 = exactly two full pages; the loop re-checks the ceiling between
    # page fetches, so a 7th row can never land (no overshoot).
    out = archive_twitch.backfill_chat("cellbit", "6602", max_messages=6)
    assert out["inserted"] == 6 and out["stopped"] == "max_messages"
    assert out["pages"] == 2, "2 full pages (6 rows), then the ceiling stops the sweep"
