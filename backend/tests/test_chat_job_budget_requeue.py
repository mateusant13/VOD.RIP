"""A dry YouTube rate budget requeues the chat job DURABLY, not by return.

The 2026-10-12 owner log showed YtGovernorExhausted escaping as ERROR + a
full traceback once per chat job (8 times in 8 minutes). It is transient by
construction: the budget is dry, the video is not broken. This pins the law a
code review of that fix demanded and the repo already states for the bot-gate
branch (test_yt_gate.py:75): the job must be a QUEUED row in the archive DB,
not only a "requeued" key in the return value - a caller that ignores the
dict would otherwise leave the job stuck in_progress forever.

Both arms fail: drop the update_job call and the row assertion goes red;
let the exception escape and the traceback path comes back.
"""

from __future__ import annotations

import pytest

from services import archive_db
from services import archive_transcribe as at
from services.archive_ytdlp import YtGovernorExhausted


def _seed_job(kind: str, platform: str, video_id: str) -> str:
    job_id = f"test-{kind}-{platform}-{video_id}"
    try:
        archive_db.enqueue_job(job_id, kind, platform, video_id)
    except Exception:
        archive_db.execute("DELETE FROM archive_jobs WHERE id = ?", (job_id,))
        archive_db.enqueue_job(job_id, kind, platform, video_id)
    return job_id


def test_budget_exhausted_requeues_the_chat_job(monkeypatch):
    def dry_backfill(video_id):
        raise YtGovernorExhausted(
            "yt-dlp yt_dlp_extract refused: YouTube rate limit budget still "
            "exhausted after waiting 30s (platform=youtube source=auto "
            "ceiling=20.0 rpm)"
        )

    monkeypatch.setattr(at, "_pace_youtube_chat", lambda: None, raising=False)
    import services.archive_ytdlp as ay
    monkeypatch.setattr(ay, "backfill_live_chat", dry_backfill)

    job_id = _seed_job("chat", "youtube", "ABCDEFGHIJK")
    # the worker marks it running before it calls in, exactly as line 5437 does
    archive_db.update_job(job_id, status="running", progress=0.0)
    out = at._process_chat_job(job_id, "youtube", "ABCDEFGHIJK")

    assert out.get("requeued") == "youtube-budget"
    row = archive_db.query(
        "SELECT status, error FROM archive_jobs WHERE id = ?", (job_id,)
    )[0]
    # THE law: durable requeue, not a terminal failure and not stuck_in_progress
    assert row["status"] == "queued", ("a dry budget is a delay, so the row "
                                        "must be queued again, never terminal")
    assert "rate budget" in (row["error"] or "")


def test_budget_exhausted_logs_one_warning_not_a_traceback(monkeypatch, caplog):
    """Noise arm: the owner's log had ERROR + Traceback per job, 8 in 8 min."""
    def dry_backfill(video_id):
        raise YtGovernorExhausted("budget dry")

    monkeypatch.setattr(at, "_pace_youtube_chat", lambda: None, raising=False)
    import services.archive_ytdlp as ay
    monkeypatch.setattr(ay, "backfill_live_chat", dry_backfill)
    job_id = _seed_job("chat", "youtube", "KLMNOPQRSTU")
    with caplog.at_level("WARNING"):
        at._process_chat_job(job_id, "youtube", "KLMNOPQRSTU")
    assert "chat backfill deferred" in caplog.text
    assert "Traceback" not in caplog.text, ("the transient case must not "
                                              "cost the owner a traceback")