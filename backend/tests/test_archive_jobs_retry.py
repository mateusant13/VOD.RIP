"""TASK10 immortal retry queue — archive_jobs retry-queue contract.

A failed job is requeued (status='queued', attempts+1, next_retry_at set)
instead of staying terminal, UNLESS the error is terminal (FileNotFound /
archive-file-missing / DownloadError) or attempts >= max_attempts. Only
then it lands on 'failed' (the UI's failure notification); a requeued job
(queued + attempts > 0) is the informational "retrying" state. Backoff:
60 * 2^(attempts-1) seconds, capped at 1 h, plus up to 30 s of jitter
(rate/gate failures — HTTP 429, 'rate limit', YouTube bot-gate, Kick
Cloudflare block — wait out the active yt/kick gates instead, tracking the
freeze deadline, and never exhaust max_attempts: an IP block is transient,
not a per-video verdict). _claim_next_job only claims queued rows whose
next_retry_at deadline has passed.

No network; fresh VODRIP_ARCHIVE_DB per test (module-scoped rebind,
mirroring test_ws1_queue_priority.py).
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="archive-jobs-retry-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db  # noqa: E402  (env must be set first)
from services.archive_transcribe import _claim_next_job  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _retry_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB at module
    start (collection-order independent) and restore the env + unbind after,
    so the next module rebinds fresh."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="archive-jobs-retry-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


def _job(job_id: str) -> dict:
    return archive_db.query(
        "SELECT * FROM archive_jobs WHERE id = ?", (job_id,)
    )[0]


# --- 1. transient failure -> requeued, not terminal -------------------------


def test_transient_failure_requeues_with_attempt_and_deadline():
    archive_db.enqueue_job("rt-trans", "transcribe", "twitch", "rt-vid-1")
    archive_db.update_job("rt-trans", status="failed", error="boom")

    row = _job("rt-trans")
    assert row["status"] == "queued", (
        "a transient failure must requeue, not go terminal"
    )
    assert row["attempts"] == 1
    assert row["next_retry_at"] is not None, (
        "a requeued job must carry a backoff deadline"
    )
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert row["next_retry_at"] > now_iso, (
        "next_retry_at must be in the future (ISO strings compare lexically)"
    )


# --- 2. terminal failure -> stays failed ------------------------------------


def test_terminal_failure_stays_failed_with_attempt():
    archive_db.enqueue_job("rt-term", "transcribe", "twitch", "rt-vid-2")
    archive_db.update_job("rt-term", status="failed", error="FileNotFound: x")

    row = _job("rt-term")
    assert row["status"] == "failed", (
        "a terminal error must stay terminal (no requeue)"
    )
    assert row["attempts"] == 1, "the attempt still counts as a final failure"
    assert row["next_retry_at"] is None, "a terminal failure has no retry deadline"


# --- 3. attempts exhausted -> failed ----------------------------------------


def test_transient_failures_exhausting_max_attempts_land_failed():
    archive_db.enqueue_job("rt-exh", "transcribe", "twitch", "rt-vid-3")
    # max_attempts defaults to 3: the 3rd transient failure is the last one.
    for _ in range(3):
        archive_db.update_job("rt-exh", status="failed", error="boom")

    row = _job("rt-exh")
    assert row["status"] == "failed", (
        "attempts >= max_attempts must go terminal (no more retries)"
    )
    assert row["attempts"] == 3
    # next_retry_at keeps the stale deadline from the last requeue — it is
    # never cleared on terminal failure, but 'failed' rows are not claimable,
    # so the leftover deadline is inert (the UI sees the final failure).
    assert row["next_retry_at"] is not None


# --- 4. backoff curve: exponential, capped, jittered ------------------------


def test_retry_delay_curve_and_cap():
    d1 = archive_db._retry_delay_sec(1, rate=False)
    assert 60.0 <= d1 < 90.0, f"attempt 1 must be 60s + jitter, got {d1}"
    d6 = archive_db._retry_delay_sec(6, rate=False)
    assert 1920.0 <= d6 < 1950.0, (
        f"attempt 6 is 60*2^5=1920s (below the cap), got {d6}"
    )
    d7 = archive_db._retry_delay_sec(7, rate=False)
    assert 3600.0 <= d7 < 3630.0, (
        f"attempt 7 must hit the 1h cap + jitter, got {d7}"
    )


# --- 5. claim-time honors next_retry_at -------------------------------------


def test_claim_skips_future_deadline_and_claims_past_one():
    archive_db.enqueue_job("rt-claim", "transcribe", "twitch", "rt-vid-4")
    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at=? WHERE id=?",
        ("2999-01-01T00:00:00+00:00", "rt-claim"),
    )
    assert _claim_next_job() is None, (
        "a queued job whose retry deadline is in the future must not be claimed"
    )
    assert _job("rt-claim")["status"] == "queued", (
        "the skipped claim must not touch the row"
    )

    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", "rt-claim"),
    )
    claimed = _claim_next_job()
    assert claimed is not None and claimed["id"] == "rt-claim", (
        "a queued job whose deadline has passed must be claimed"
    )
    assert _job("rt-claim")["status"] == "running", (
        "the claim flips the row to running"
    )


# --- 6. bot-gate failure -> gate-tracking deadline + truthful error ---------


def test_bot_gate_failure_requeues_with_gate_remaining_deadline():
    """A YouTube bot-gate failure names the gate and parks the retry until
    the freeze lifts (next_retry_at >= gate remaining) — no 60s hot retry."""
    from services import yt_gate

    archive_db.enqueue_job("rt-gate", "transcribe", "youtube", "rt-gate-vid")
    yt_gate.note_youtube_gate("test arm", freeze_sec=120)
    try:
        archive_db.update_job(
            "rt-gate", status="failed",
            error="extract error: YouTube bot-gate active — retrying after it clears",
        )
        row = _job("rt-gate")
        assert row["status"] == "queued", (
            "a gate failure must requeue (retrying), never go terminal"
        )
        assert row["attempts"] == 1, "one prior failure recorded"
        assert "bot-gate" in (row["error"] or "").lower(), (
            "the job error must name the real cause, not the generic 'extract error'"
        )
        remaining = yt_gate.gate_remaining_sec()
        delay = (
            datetime.fromisoformat(row["next_retry_at"]) - datetime.now(timezone.utc)
        ).total_seconds()
        assert delay >= remaining - 1.0, (
            f"next_retry_at ({delay:.0f}s out) must track the gate ({remaining:.0f}s left)"
        )
    finally:
        yt_gate.clear_youtube_gate()


def test_gate_failures_never_exhaust_max_attempts():
    """Gate/rate failures are transient IP conditions, not per-video
    verdicts — they must stay 'retrying' past max_attempts (3) instead of
    going terminal while the gate is still up."""
    from services import yt_gate

    archive_db.enqueue_job("rt-gate-inf", "transcribe", "youtube", "rt-gi-vid")
    yt_gate.note_youtube_gate("test arm", freeze_sec=120)
    try:
        for _ in range(4):
            archive_db.update_job(
                "rt-gate-inf", status="failed",
                error="extract error: YouTube bot-gate active — retrying after it clears",
            )
    finally:
        yt_gate.clear_youtube_gate()
    row = _job("rt-gate-inf")
    assert row["status"] == "queued", (
        "4 gate failures (max_attempts=3) must NOT land 'failed' — the gate "
        "is transient and the job drains once it clears"
    )
    assert row["attempts"] == 4, "each failure still counts for observability"


# --- 7. non-gate extract failure -> exponential backoff unchanged -----------


def test_extract_error_without_gate_keeps_exponential_backoff():
    """A non-gate extract failure (e.g. DRM) keeps the plain exponential
    curve — the gate-aware rate path must not fire on ordinary errors."""
    from services import kick_gate, yt_gate

    yt_gate.clear_youtube_gate()
    kick_gate.clear_kick_gate()
    archive_db.enqueue_job("rt-plain", "transcribe", "youtube", "rt-plain-vid")
    archive_db.update_job(
        "rt-plain", status="failed",
        error="extract error: ERROR: [youtube] abc: This video is DRM protected",
    )
    row = _job("rt-plain")
    assert row["status"] == "queued", "a transient extract failure requeues"
    assert row["attempts"] == 1
    delay = (
        datetime.fromisoformat(row["next_retry_at"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert 55.0 <= delay <= 95.0, (
        f"attempt 1 non-gate failure must be 60s + jitter, got {delay:.1f}s"
    )


# --- 8. gate-clear path unchanged -------------------------------------------


def test_gate_clear_retry_proceeds_on_normal_curve():
    """Once the gate lifts, a gate-requeued job is claimable again (its
    deadline is still honored) — no permanent parking."""
    from services import yt_gate

    archive_db.enqueue_job("rt-gate-clear", "transcribe", "youtube", "rt-gc-vid")
    yt_gate.note_youtube_gate("test arm", freeze_sec=120)
    try:
        archive_db.update_job(
            "rt-gate-clear", status="failed",
            error="extract error: YouTube bot-gate active — retrying after it clears",
        )
        assert _job("rt-gate-clear")["status"] == "queued"
    finally:
        yt_gate.clear_youtube_gate()
    # The deadline set during the freeze is still honored after it clears —
    # a past deadline means the retry is due.
    archive_db.execute(
        "UPDATE archive_jobs SET next_retry_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", "rt-gate-clear"),
    )
    claimed = _claim_next_job()
    assert claimed is not None and claimed["id"] == "rt-gate-clear", (
        "a YouTube job must be claimable again once the gate clears"
    )


# --- 9. kick gate honored by the same rate machinery ------------------------


def test_retry_delay_rate_waits_out_kick_gate():
    from services import kick_gate

    kick_gate.note_kick_gate_event("test 403")
    try:
        d = archive_db._retry_delay_sec(1, rate=True)
        assert d >= kick_gate.gate_remaining_sec() - 1.0, (
            "a rate failure must wait out the active Kick gate"
        )
        assert d >= 120.0, "the 2m floor still applies"
    finally:
        kick_gate.clear_kick_gate()


# --- 10. ingest_video failure path (the fix site) ---------------------------


def test_ingest_video_gate_failure_stamps_gate_error_and_deadline(monkeypatch):
    """End-to-end at the fix site: ingest_video's failure path classifies
    the bot-gate, names it in the job error, and parks the retry on the
    gate deadline instead of the 60s exponential."""
    from contextlib import contextmanager

    from services import archive_ytdlp, yt_gate

    @contextmanager
    def _raise_gate(_outdir, *, video_id=None):
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(archive_ytdlp, "_guarded_youtube_dl", _raise_gate)
    yt_gate.note_youtube_gate("pre-arm", freeze_sec=120)
    try:
        with pytest.raises(RuntimeError):
            archive_ytdlp.ingest_video("https://www.youtube.com/watch?v=GATEGATE111")
        row = _job("yt-ingest-GATEGATE111")
        assert row["status"] == "queued", "a gate failure requeues, never terminal"
        assert "bot-gate" in (row["error"] or "").lower(), (
            f"job error must name the gate, got: {row['error']!r}"
        )
        remaining = yt_gate.gate_remaining_sec()
        delay = (
            datetime.fromisoformat(row["next_retry_at"]) - datetime.now(timezone.utc)
        ).total_seconds()
        assert delay >= remaining - 1.0, (
            f"deadline ({delay:.0f}s) must track the gate ({remaining:.0f}s left)"
        )
    finally:
        yt_gate.clear_youtube_gate()


def test_ingest_video_plain_failure_keeps_exponential(monkeypatch):
    """A non-gate extract failure (DRM) keeps the 60s exponential curve and
    surfaces the real reason instead of the generic 'extract error'."""
    from contextlib import contextmanager

    from services import archive_ytdlp, kick_gate, yt_gate

    @contextmanager
    def _raise_plain(_outdir, *, video_id=None):
        raise RuntimeError("This video is DRM protected")

    monkeypatch.setattr(archive_ytdlp, "_guarded_youtube_dl", _raise_plain)
    yt_gate.clear_youtube_gate()
    kick_gate.clear_kick_gate()
    with pytest.raises(RuntimeError):
        archive_ytdlp.ingest_video("https://www.youtube.com/watch?v=PLAINPLAIN1")
    row = _job("yt-ingest-PLAINPLAIN1")
    assert row["status"] == "queued" and row["attempts"] == 1
    assert "DRM protected" in (row["error"] or ""), (
        "the job error must surface the real cause"
    )
    delay = (
        datetime.fromisoformat(row["next_retry_at"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert 55.0 <= delay <= 95.0, (
        f"non-gate failure must stay 60s + jitter, got {delay:.1f}s"
    )


# --- 11. scheduler skips YouTube ingest while the gate is frozen ------------


def test_scheduler_skips_youtube_ingest_while_gate_frozen():
    """The scheduler pass must not spawn any YouTube extract while the
    bot-gate freeze is active (mirrors the instant-preview gate skip)."""
    from services import archive_scheduler, yt_gate

    channel = {
        "youtubeSlug": "gaveta",
        "channel_youtube_enabled": True,
        "vodVideos": ["https://www.youtube.com/watch?v=AAAAAAAAAAA"],
        "clipVideos": [],
    }
    archive_scheduler._yt_inflight.clear()
    yt_gate.note_youtube_gate("test", freeze_sec=60)
    try:
        archive_scheduler._ingest_youtube(channel)
        assert archive_scheduler._yt_inflight == set(), (
            "no YouTube extract may spawn while the bot-gate freeze is active"
        )
    finally:
        yt_gate.clear_youtube_gate()
        archive_scheduler._yt_inflight.clear()
