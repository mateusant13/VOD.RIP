"""Bot-gate cooldown + YouTube chat pacing for the archive worker.

Covers services.yt_gate (classification, arm/extend/lift), the worker's
gate-requeue contract (chat + transcribe jobs requeue, never fail) and the
chat-fetch pacing helper. DB isolation: fresh VODRIP_ARCHIVE_DB per module.
"""
import os
import pathlib
import tempfile
import threading
import time

_DB = pathlib.Path(tempfile.mkdtemp(prefix="yt-gate-")) / "archive.db"
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402
from services import archive_transcribe as at  # noqa: E402
from services import archive_twitch as atw  # noqa: E402
from services import yt_gate  # noqa: E402


def _reset_gate():
    yt_gate.clear_youtube_gate()


def _seed_job(kind: str, platform: str, video_id: str) -> str:
    job_id = f"test-{kind}-{platform}-{video_id}"
    try:
        archive_db.enqueue_job(job_id, kind, platform, video_id)
    except Exception:
        archive_db.execute("DELETE FROM archive_jobs WHERE id = ?", (job_id,))
        archive_db.enqueue_job(job_id, kind, platform, video_id)
    return job_id


def test_gate_classify_markers():
    assert yt_gate.classify_youtube_gate_error(
        RuntimeError("Sign in to confirm you're not a bot")
    )
    assert yt_gate.classify_youtube_gate_error(
        RuntimeError("The current session has been rate-limited by YouTube for up to an hour")
    )
    assert yt_gate.classify_youtube_gate_error(RuntimeError("too many requests (429)"))
    assert not yt_gate.classify_youtube_gate_error(
        RuntimeError("This video is unavailable")
    )
    assert not yt_gate.classify_youtube_gate_error(RuntimeError("members-only content"))


def test_gate_lifecycle_and_extend():
    _reset_gate()
    assert not yt_gate.youtube_gate_active()
    yt_gate.note_youtube_gate("probe", freeze_sec=0.2)
    assert yt_gate.youtube_gate_active()
    assert yt_gate.gate_remaining_sec() > 0
    # longer freeze wins; shorter does not shorten
    yt_gate.note_youtube_gate("probe2", freeze_sec=0.5)
    assert yt_gate.gate_remaining_sec() > 0.2
    yt_gate.note_youtube_gate("short", freeze_sec=0.05)
    assert yt_gate.gate_remaining_sec() > 0.2, "shorter freeze must not shrink"
    _reset_gate()
    assert not yt_gate.youtube_gate_active()


def test_chat_job_requeues_during_gate():
    _reset_gate()
    job_id = _seed_job("chat", "youtube", "ABCDEFGHIJK")
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    try:
        out = at._process_chat_job(job_id, "youtube", "ABCDEFGHIJK")
        assert out.get("requeued") == "youtube-gate"
        row = archive_db.query(
            "SELECT status, error FROM archive_jobs WHERE id = ?", (job_id,)
        )[0]
        assert row["status"] == "queued", "gated chat job must be requeued, not failed"
        assert "cooldown" in (row["error"] or "")
    finally:
        _reset_gate()


def test_transcribe_job_requeues_during_gate():
    _reset_gate()
    job_id = _seed_job("transcribe", "youtube", "ZZZZZZZZZZZ")
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    try:
        out = at._process_job({"id": job_id, "kind": "transcribe",
                               "platform": "youtube", "video_id": "ZZZZZZZZZZZ"})
        assert out.get("requeued") == "youtube-gate"
        row = archive_db.query(
            "SELECT status, error FROM archive_jobs WHERE id = ?", (job_id,)
        )[0]
        assert row["status"] == "queued"
    finally:
        _reset_gate()


def test_twitch_chat_job_not_gated(monkeypatch):
    """The gate freezes YOUTUBE work only — a twitch chat job still runs."""
    _reset_gate()
    job_id = _seed_job("chat", "twitch", "123456789")
    calls = []

    def fake_backfill(channel, video_id, **kw):
        calls.append((channel, video_id))
        return {"inserted": 1}

    monkeypatch.setattr("services.archive_scheduler.BACKFILL_MAX_MESSAGES", 100)
    monkeypatch.setattr("services.archive_twitch.backfill_chat", fake_backfill)
    monkeypatch.setattr(archive_db, "video_channel", lambda p, v: "somechannel")
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    try:
        out = at._process_chat_job(job_id, "twitch", "123456789")
        assert out["chat_messages"] == 1
        assert calls == [("somechannel", "123456789")]
        row = archive_db.query(
            "SELECT status FROM archive_jobs WHERE id = ?", (job_id,)
        )[0]
        assert row["status"] == "done"
    finally:
        _reset_gate()


def test_claim_skips_youtube_jobs_during_gate():
    """A gated YouTube job must NOT be claimed — claiming would requeue it
    and the refill loop would re-claim the same row in a hot loop (~2ms/iter,
    spamming 'requeued: bot-gate cooldown' for the whole freeze window)."""
    _reset_gate()
    yt_job = _seed_job("chat", "youtube", "YTF3ddYsEnc")
    tw_job = _seed_job("chat", "twitch", "123456789")
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    try:
        # The twitch job (same priority, later created_at) is the one claimed.
        claimed = at._claim_next_job()
        assert claimed is not None and claimed["id"] == tw_job
        row = archive_db.query(
            "SELECT status FROM archive_jobs WHERE id = ?", (yt_job,)
        )[0]
        assert row["status"] == "queued", "gated youtube job must stay queued untouched"
    finally:
        _reset_gate()


def test_claim_clears_when_gate_lifts():
    """Once the gate lifts, the youtube job is claimable again."""
    _reset_gate()
    archive_db.execute("DELETE FROM archive_jobs")
    yt_job = _seed_job("chat", "youtube", "YTF3ddYsEnc")
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    try:
        assert at._claim_next_job() is None
    finally:
        _reset_gate()
    claimed = at._claim_next_job()
    assert claimed is not None and claimed["id"] == yt_job


def test_chat_pacing_min_interval():
    at._YOUTUBE_CHAT_MIN_INTERVAL_S = 0.15
    at._youtube_chat_last_start = 0.0
    try:
        t0 = time.monotonic()
        at._pace_youtube_chat()
        at._pace_youtube_chat()
        elapsed = time.monotonic() - t0
        # sleep(15.6ms granularity on Windows) — assert the bulk of the
        # interval, not the exact wall time
        assert elapsed >= 0.10, f"second paced start must wait ~interval, got {elapsed:.3f}s"
    finally:
        at._YOUTUBE_CHAT_MIN_INTERVAL_S = 12.0


# --- two-lane contract (user requirement) ----------------------------------
# Interactive lane (preview/download/click-chat/search/watch): NEVER delayed
# by background pacing, NEVER frozen by the gate, fail-fast on gate signals.
# Background lane (worker): fully paced, activity-aware, gate-cooldown +
# requeue. The tests below prove the interactive lane completes instantly
# while the background lane is mid-pace / gated.


def test_pacing_activity_aware(monkeypatch):
    """The worker's chat pacing slows while the app's interactive lane is
    active (app-activity heartbeat fresh) and ramps up when idle."""
    monkeypatch.setattr(
        archive_db, "worker_live", lambda age_s=30, tag="transcribe": tag == "app-activity"
    )
    assert at._youtube_chat_interval() == at._YOUTUBE_CHAT_ACTIVE_INTERVAL_S
    monkeypatch.setattr(
        archive_db, "worker_live", lambda age_s=30, tag="transcribe": False
    )
    assert at._youtube_chat_interval() == at._YOUTUBE_CHAT_MIN_INTERVAL_S


def test_interactive_kick_never_paced_or_gated(monkeypatch):
    """A simulated interactive request (twitch chat backfill, the router
    path) completes instantly while the background lane is mid-pace AND the
    YouTube gate is armed — no pacing sleep, no gate consult, no hang."""
    _reset_gate()
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    at._youtube_chat_last_start = time.monotonic()  # pacing budget exhausted
    at._YOUTUBE_CHAT_MIN_INTERVAL_S = 0.15
    try:
        sleeps = []

        def fake_sleep(sec):
            sleeps.append(sec)

        monkeypatch.setattr(atw.time, "sleep", fake_sleep)
        monkeypatch.setattr(
            atw, "_post_comments_page",
            lambda vid, offset, size: [{"contentOffsetSeconds": str(offset + 5)}],
        )
        t0 = time.monotonic()
        # Fresh video id: test_twitch_chat_job_not_gated already left a DONE
        # chat job on 123456789, which the kick-lane dedupe (stable id +
        # no-chat marker) now treats as covered — this test needs a video
        # with no job so the kick actually runs.
        out = atw.backfill_chat("somechannel", "333333333", max_messages=1)
        elapsed = time.monotonic() - t0
        assert out["inserted"] == 1, out
        assert elapsed < 1.0, f"interactive kick must not wait on pacing, got {elapsed:.3f}s"
        assert not sleeps, "interactive lane must add no artificial delay"
        assert yt_gate.youtube_gate_active(), "interactive traffic must not clear the gate"
    finally:
        _reset_gate()
        at._YOUTUBE_CHAT_MIN_INTERVAL_S = 12.0


def test_interactive_backfill_busy_fails_fast(monkeypatch):
    """When the background lane holds both per-IP backfill slots, an
    interactive kick never queues behind it — non-blocking acquire, instant
    'busy' status, job row requeued for the worker."""
    _reset_gate()
    # Order-proof: a background thread from an earlier test may still hold
    # slots on the module-global semaphore; a fresh one makes this test
    # deterministic regardless of what ran before it.
    monkeypatch.setattr(atw, "_BACKFILL_SEM", threading.BoundedSemaphore(2))
    held = []
    try:
        for _ in range(2):
            assert atw._BACKFILL_SEM.acquire(blocking=False)
            held.append(True)
        t0 = time.monotonic()
        out = atw.backfill_chat("somechannel", "111111111", max_messages=1)
        elapsed = time.monotonic() - t0
        assert out["stopped"] == "busy", out
        assert elapsed < 0.5, f"busy kick must fail fast, got {elapsed:.3f}s"
        rows = archive_db.query(
            "SELECT status FROM archive_jobs "
            "WHERE kind='chat' AND platform='twitch' AND video_id='111111111' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        assert rows and rows[0]["status"] == "queued", "busy kick must requeue for the worker"
    finally:
        for _ in held:
            atw._BACKFILL_SEM.release()


def test_interactive_backfill_fails_fast_on_429(monkeypatch):
    """Foreground kick on a gate signal (Twitch GQL 429): one attempt, no
    retry-loop, no backoff sleep — the error surfaces immediately."""
    _reset_gate()
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(atw.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        atw, "_post_comments_page",
        lambda vid, offset, size: (_ for _ in ()).throw(atw._RateLimited("Twitch GQL 429: nope")),
    )
    t0 = time.monotonic()
    try:
        atw.backfill_chat("somechannel", "222222222", max_messages=100)
        raise AssertionError("interactive 429 must raise")
    except atw._RateLimited:
        pass
    elapsed = time.monotonic() - t0
    assert not sleeps, f"interactive 429 must not backoff-sleep, slept {sleeps}"
    assert elapsed < 0.5, f"interactive 429 must fail fast, got {elapsed:.3f}s"


def test_worker_backfill_retries_429(monkeypatch):
    """The background lane (job_id passed) keeps the jittered 429 backoff —
    it is paced by design and requeues on exhaustion."""
    _reset_gate()
    sleeps = []
    monkeypatch.setattr(atw.time, "sleep", lambda sec: sleeps.append(sec))
    calls = {"n": 0}

    def rate_limited(vid, offset, size):
        calls["n"] += 1
        raise atw._RateLimited("Twitch GQL 429: nope")

    monkeypatch.setattr(atw, "_post_comments_page", rate_limited)
    job_id = f"tw-backfill-retry-{int(time.time())}"
    archive_db.enqueue_job(job_id, "chat", "twitch", "123456789")
    try:
        atw.backfill_chat("somechannel", "123456789", max_messages=100, job_id=job_id)
        raise AssertionError("worker 429 must exhaust and re-raise")
    except atw._RateLimited:
        pass
    assert calls["n"] == atw.BACKOFF_MAX_ATTEMPTS, calls
    assert len(sleeps) == atw.BACKOFF_MAX_ATTEMPTS - 1, "each failed attempt sleeps"
    row = archive_db.query(
        "SELECT status FROM archive_jobs WHERE id = ?", (job_id,)
    )[0]
    assert row["status"] == "failed", "exhausted worker retries must mark the job failed"
