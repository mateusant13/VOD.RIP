"""Index-stuck batch-3: the Twitch chat-backtrack job layer.

Covers the chat job machinery behind 'Indexando 2 vídeos (retrocesso de
chat ×2)' + the chat-less-VOD forever-rekick:

  * the interactive kick lane (backfill_chat without job_id) enqueues with
    a STABLE id (tw-backfill-<vid>) + the scheduler dedupe: a repeat kick
    for the same video is skipped (chat rows / queued / running / done),
    and a failed row is requeued IN PLACE (IntegrityError backstop) — no
    duplicated chat rows on restart, no same-second IntegrityError;
  * a 0-row backfill leaves a terminal no-chat marker (a done 'chat' job on
    a chat-less VOD — Twitch's comments-disabled signal); backfill_chat,
    _maybe_auto_backfill, _kick_backfill, kick_preview_backfill and
    preview_backfill_status all consult it, so chat-less VODs stop being
    kick candidates;
  * _claim_next_job reclaims a running twitch chat job whose heartbeat went
    stale (dead/wedged executor) and does NOT reclaim a fresh one; yt chat
    jobs with NULL heartbeat keep the flat 2h updated_at window.

No network: archive_twitch._post_comments_page is monkeypatched everywhere;
kicked background tasks are observed through patched backfill_chat /
_run_backfill. Fresh VODRIP_ARCHIVE_DB per test.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="chat-backfill-jobs-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from services import archive_scheduler  # noqa: E402
from services import archive_twitch  # noqa: E402
from services import archive_transcribe  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _seed_video(vid: str) -> None:
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": vid,
        "channel": "cellbit",
        "title": f"vod {vid}",
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "vod",
    })


def _node(offset: float, text: str = "hi") -> dict:
    """A minimal VideoComment GQL node (the shape _message_row consumes)."""
    return {
        "id": f"c{int(offset)}",
        "contentOffsetSeconds": offset,
        "createdAt": "2026-08-01T20:00:00Z",
        "commenter": {"id": f"u{int(offset)}", "login": "lubu", "displayName": "lubu"},
        "message": {"fragments": [{"text": text}], "userBadges": []},
    }


def _chat_jobs(vid: str) -> list:
    return list(archive_db.query(
        "SELECT * FROM archive_jobs WHERE kind='chat' AND platform='twitch' AND video_id=? "
        "ORDER BY created_at",
        (vid,),
    ))


def _reset_router_state() -> None:
    """Zero the router's throttle/cooldown/inflight clocks between tests."""
    import routers.archive as ar

    with ar._backfill_lock:
        ar._last_auto_kick = 0.0
        ar._backfill_inflight.clear()
        ar._backfill_attempted_at.clear()
        ar._backfill_failed_resumes.clear()


# --- 1. stable kick-lane job id + scheduler dedupe --------------------------


def test_kick_lane_stable_id_dedupes_repeat_kick(scratch_db, monkeypatch):
    """First kick enqueues tw-backfill-<vid> and fetches; a second kick for
    the same video is a no-op (chat rows exist) — no second fetch, no
    duplicate job row."""
    _seed_video("1001")
    calls = {"n": 0}

    def fake_page(vid, offset, size):
        calls["n"] += 1
        return [_node(5.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    out = archive_twitch.backfill_chat("cellbit", "1001", max_messages=1)
    assert out["inserted"] == 1 and out["stopped"] == "max_messages"
    jobs = _chat_jobs("1001")
    assert [j["id"] for j in jobs] == ["tw-backfill-1001"], "kick lane must use the stable id"
    assert jobs[0]["status"] == "done"

    out2 = archive_twitch.backfill_chat("cellbit", "1001", max_messages=1)
    assert out2["stopped"] == "already" and out2["inserted"] == 0
    assert calls["n"] == 1, "a repeat kick must not fetch again"
    assert len(_chat_jobs("1001")) == 1, "a repeat kick must not add a job row"


def test_kick_lane_skips_queued_and_running_jobs(scratch_db, monkeypatch):
    """A queued or running chat job (from the scheduler lane or an earlier
    kick) suppresses the interactive kick — no fetch, no new row."""
    _seed_video("1003")
    _seed_video("1004")
    archive_db.enqueue_job("tw-backfill-1003", "chat", "twitch", "1003")
    archive_db.enqueue_job("tw-backfill-1004", "chat", "twitch", "1004")
    archive_db.update_job("tw-backfill-1004", status="running")
    calls = {"n": 0}

    def fake_page(vid, offset, size):
        calls["n"] += 1
        return [_node(5.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    out = archive_twitch.backfill_chat("cellbit", "1003", max_messages=1)
    assert out["stopped"] == "queued" and out["inserted"] == 0
    out2 = archive_twitch.backfill_chat("cellbit", "1004", max_messages=1)
    assert out2["stopped"] == "queued" and out2["inserted"] == 0
    assert calls["n"] == 0, "no fetch when a job already covers the video"


def test_kick_lane_requeues_failed_row_in_place(scratch_db, monkeypatch):
    """A terminal-failed row under the stable id stays 'failed' (TASK10 only
    requeues transient errors) and a re-kick requeues it IN PLACE via the
    IntegrityError backstop — the retry reuses the row (no orphaned failed
    id, no crash), and only one job row ever exists for the video."""
    _seed_video("1002")
    archive_db.enqueue_job("tw-backfill-1002", "chat", "twitch", "1002")
    archive_db.update_job(
        "tw-backfill-1002", status="failed", error="FileNotFound: missing archive")

    def fake_page(vid, offset, size):
        return [_node(5.0)]

    monkeypatch.setattr(archive_twitch, "_post_comments_page", fake_page)
    out = archive_twitch.backfill_chat("cellbit", "1002", max_messages=1)
    assert out["inserted"] == 1
    jobs = _chat_jobs("1002")
    assert len(jobs) == 1 and jobs[0]["id"] == "tw-backfill-1002", (
        "the retry must reuse the stable id, not create a second row"
    )
    assert jobs[0]["status"] == "done" and jobs[0]["error"] is None


# --- 2. terminal no-chat marker (0-row backfill) ----------------------------


def test_zero_row_backfill_marks_no_chat_and_stops_repeat_kicks(scratch_db, monkeypatch):
    """An empty page (comments disabled / purged) ends the job 'done' with 0
    rows — that done row is the terminal no-chat marker: the guard blocks
    and a repeat kick is a permanent no-op."""
    _seed_video("2001")

    def empty_page(vid, offset, size):
        return []

    monkeypatch.setattr(archive_twitch, "_post_comments_page", empty_page)
    out = archive_twitch.backfill_chat("cellbit", "2001", max_messages=100)
    assert out["inserted"] == 0 and out["stopped"] == "end_of_chat"
    jobs = _chat_jobs("2001")
    assert jobs and jobs[0]["id"] == "tw-backfill-2001" and jobs[0]["status"] == "done"
    assert archive_scheduler._chat_job_guard("twitch", "2001") is True, (
        "a done chat job on a chat-less VOD must block further kicks"
    )
    out2 = archive_twitch.backfill_chat("cellbit", "2001", max_messages=100)
    assert out2["stopped"] == "queued" and out2["inserted"] == 0


async def test_auto_backfill_skips_marked_videos(scratch_db, monkeypatch):
    """_maybe_auto_backfill consults the marker + scheduler guard: a
    marked (done no-chat) video is never a kick candidate, while an
    unmarked chat-less video still is."""
    import routers.archive as ar

    _seed_video("2001")
    _seed_video("2002")
    monkeypatch.setattr(archive_twitch, "_post_comments_page", lambda v, o, s: [])
    archive_twitch.backfill_chat("cellbit", "2001", max_messages=100)  # stamps marker
    kicked_log: list[str] = []
    monkeypatch.setattr(
        "services.archive_twitch.backfill_chat",
        lambda channel, video_id, **kw: (kicked_log.append(video_id), {"inserted": 0})[1],
    )
    _reset_router_state()
    try:
        kicked = ar._maybe_auto_backfill(
            platform="twitch", channel=None, source="both", q="vod")
        await asyncio.sleep(0.1)  # background tasks need a loop tick
        assert kicked_log == ["2002"], "only the unmarked video is kicked"
        assert [e["video_id"] for e in kicked] == ["2002"]
        # video-scoped search on the marked video: still nothing
        _reset_router_state()
        kicked2 = ar._maybe_auto_backfill(
            platform="twitch", channel=None, source="both", q="vod", video_id="2001")
        await asyncio.sleep(0.1)
        assert kicked2 == [], "a marked video must never kick, even video-scoped"
    finally:
        _reset_router_state()


async def test_kick_backfill_and_preview_respect_marker(scratch_db, monkeypatch):
    """The manual-backfill endpoint path (_kick_backfill) and the preview
    lanes consult the marker: no task is spawned, the panel goes terminal
    'idle' instead of polling 'running' forever."""
    import routers.archive as ar

    _seed_video("3001")
    _seed_video("3002")
    _seed_video("3003")
    monkeypatch.setattr(archive_twitch, "_post_comments_page", lambda v, o, s: [])
    archive_twitch.backfill_chat("cellbit", "3001", max_messages=100)  # marker
    archive_twitch.backfill_chat("cellbit", "3002", max_messages=100)  # marker
    archive_db.enqueue_job("tw-backfill-3003", "chat", "twitch", "3003")  # queued
    spawned: list = []
    monkeypatch.setattr(ar, "_run_backfill", lambda *a, **k: spawned.append(a))
    try:
        assert ar._kick_backfill("3001", "cellbit") == "already"
        assert ar.kick_preview_backfill("twitch", "3002") == "", (
            "a marked video must not consume the preview kick budget"
        )
        assert spawned == [], "no background task for marked videos"
        assert ar.preview_backfill_status("twitch", "3001")[0] == "idle", (
            "a marked video is terminal 'idle' — the panel stops polling"
        )
        assert ar.preview_backfill_status("twitch", "3003")[0] == "running", (
            "a queued worker job keeps the panel in bounded 'running'"
        )
    finally:
        _reset_router_state()


# --- 4. P2-6: failed-resume loop breaker ------------------------------------


def test_failed_resume_limit_stops_preview_and_auto_kick(scratch_db):
    """P2-6: after _BACKFILL_FAILED_RESUME_LIMIT consecutive failed resume
    fetches the re-kick loop must STOP: the panel goes terminal 'idle' (no
    more polling), the preview kick is a no-op, and _maybe_auto_backfill
    stops kicking the video — no more done->failed->re-kick churn on a tail
    the API will not serve."""
    import routers.archive as ar

    _seed_video("4001")
    with ar._backfill_lock:
        ar._backfill_failed_resumes["4001"] = ar._BACKFILL_FAILED_RESUME_LIMIT

    assert ar.preview_backfill_status("twitch", "4001")[0] == "idle", (
        "the panel must stop polling after the failed-resume limit"
    )
    assert ar.kick_preview_backfill("twitch", "4001") == "", (
        "the preview kick must be a no-op past the failed-resume limit"
    )
    kicked = ar._maybe_auto_backfill(
        platform="twitch", channel=None, source="both", q="vod", video_id="4001"
    )
    assert kicked == [], "auto-kicks must stop past the failed-resume limit"
    # Below the limit the same video still kicks (recovery is possible).
    with ar._backfill_lock:
        ar._backfill_failed_resumes["4001"] = ar._BACKFILL_FAILED_RESUME_LIMIT - 1
    assert ar.preview_backfill_status("twitch", "4001")[0] == "running", (
        "under the limit the panel keeps polling (self-healing tail)"
    )


async def test_run_backfill_tallies_and_resets_failure_streak(scratch_db, monkeypatch):
    """_run_backfill counts each failed resume fetch and resets the streak
    on a real (or terminal) completion."""
    import routers.archive as ar

    _seed_video("4002")
    _reset_router_state()

    def failing_backfill(channel, video_id, **kw):
        raise RuntimeError("GQL service error")

    monkeypatch.setattr(archive_twitch, "backfill_chat", failing_backfill)
    await ar._run_backfill("4002", "cellbit")
    with ar._backfill_lock:
        assert ar._backfill_failed_resumes.get("4002") == 1, (
            "a failed resume fetch must be tallied"
        )

    monkeypatch.setattr(
        archive_twitch, "backfill_chat",
        lambda channel, video_id, **kw: {"stopped": "end_of_chat", "inserted": 0},
    )
    await ar._run_backfill("4002", "cellbit")
    with ar._backfill_lock:
        assert ar._backfill_failed_resumes.get("4002") == 0, (
            "a successful resume must clear the failure streak"
        )


# --- 3. heartbeat-stale reclaim ---------------------------------------------


def test_claim_next_job_reclaims_stale_heartbeat_only(scratch_db):
    """A running twitch chat job with a stale heartbeat is reclaimed; a
    fresh one is not. yt chat jobs with NULL heartbeat keep the flat 2h
    updated_at window; transcribe keeps its 30-min window."""
    # fresh running twitch job -> not reclaimable
    archive_db.enqueue_job("tw-bf-fresh", "chat", "twitch", "9001")
    archive_db.update_job("tw-bf-fresh", status="running")
    assert archive_transcribe._claim_next_job() is None, (
        "a fresh running chat job must not be reclaimed"
    )
    row = archive_db.query(
        "SELECT status FROM archive_jobs WHERE id='tw-bf-fresh'")[0]
    assert row["status"] == "running"

    # stale-heartbeat running twitch job -> reclaimed, heartbeat refreshed
    archive_db.enqueue_job("tw-bf-stale", "chat", "twitch", "9002")
    archive_db.update_job("tw-bf-stale", status="running")
    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat=? WHERE id=?",
        ("2020-01-01T00:00:00Z", "tw-bf-stale"),
    )
    claimed = archive_transcribe._claim_next_job()
    assert claimed and claimed["id"] == "tw-bf-stale", (
        "a running chat job whose heartbeat stalled must be reclaimed"
    )
    row = archive_db.query(
        "SELECT * FROM archive_jobs WHERE id='tw-bf-stale'")[0]
    assert row["status"] == "running"
    assert row["heartbeat"] != "2020-01-01T00:00:00Z", (
        "the claim must refresh the heartbeat so it is not re-reclaimed"
    )
    assert archive_transcribe._claim_next_job() is None, (
        "fresh rows (incl. the just-reclaimed one) must stay untouched"
    )

    # yt chat job, NULL heartbeat: fresh updated_at -> 2h window holds;
    # stale updated_at -> reclaimed
    archive_db.enqueue_job("chat-youtube-9003", "chat", "youtube", "9003")
    archive_db.update_job("chat-youtube-9003", status="running")
    archive_db.execute(
        "UPDATE archive_jobs SET heartbeat=NULL WHERE id='chat-youtube-9003'")
    assert archive_transcribe._claim_next_job() is None, (
        "a yt chat job with NULL heartbeat keeps the flat 2h window"
    )
    archive_db.execute(
        "UPDATE archive_jobs SET updated_at=? WHERE id=?",
        ("2020-01-01T00:00:00Z", "chat-youtube-9003"),
    )
    claimed = archive_transcribe._claim_next_job()
    assert claimed and claimed["id"] == "chat-youtube-9003", (
        "a yt chat job stale beyond the 2h window is reclaimed"
    )

    # transcribe window unchanged (30 min; transcribe jobs heartbeat on
    # progress, so both columns go stale together in production)
    archive_db.enqueue_job("tr-stale", "transcribe", "youtube", "9004")
    archive_db.update_job("tr-stale", status="running")
    archive_db.execute(
        "UPDATE archive_jobs SET updated_at=?, heartbeat=? WHERE id=?",
        ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", "tr-stale"),
    )
    claimed = archive_transcribe._claim_next_job()
    assert claimed and claimed["id"] == "tr-stale", (
        "a transcribe job stale past its 30-min window is reclaimed"
    )
