"""TASK10 immortal retry queue — archive_jobs retry-queue contract.

A failed job is requeued (status='queued', attempts+1, next_retry_at set)
instead of staying terminal, UNLESS the error is terminal (FileNotFound /
archive-file-missing / DownloadError) or attempts >= max_attempts. Only
then it lands on 'failed' (the UI's failure notification); a requeued job
(queued + attempts > 0) is the informational "retrying" state. Backoff:
60 * 2^(attempts-1) seconds, capped at 1 h, plus up to 30 s of jitter
(rate-limit failures wait out the yt/kick gates instead). _claim_next_job
only claims queued rows whose next_retry_at deadline has passed.

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
