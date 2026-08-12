"""GET /api/archive/jobs — row shape + video-title enrichment (progress UI).

QueueTab polls this endpoint every 3s; the router enriches each job row with
the video's display title (WS-4 rule: original_title preferred over the
auto-translated walk-time copy). Row values come straight from the DB
(jobs carry id/kind/platform/video_id/status/progress/error/created_at/
updated_at/heartbeat) — this test pins the fields the frontend consumes.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from services import archive_db


@pytest.fixture(autouse=True)
def _clean_jobs_and_videos():
    archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE 'prog-ui-%'")
    archive_db.execute(
        "DELETE FROM videos WHERE platform='twitch' AND video_id IN "
        "('prog-1001', 'prog-1002')"
    )
    yield
    archive_db.execute("DELETE FROM archive_jobs WHERE id LIKE 'prog-ui-%'")
    archive_db.execute(
        "DELETE FROM videos WHERE platform='twitch' AND video_id IN "
        "('prog-1001', 'prog-1002')"
    )


def _seed_video(vid: str, title: str, original_title: str = "") -> None:
    row = {
        "platform": "twitch",
        "video_id": vid,
        "channel": "cellbit",
        "title": title,
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "vod",
    }
    if original_title:
        row["original_title"] = original_title
    archive_db.upsert_video(row)


@pytest.mark.asyncio
async def test_archive_jobs_rows_carry_shape_and_display_title():
    _seed_video("prog-1001", "Auto-Translated Title", original_title="Título Original")
    _seed_video("prog-1002", "Sem transcrição ainda")
    archive_db.enqueue_job("prog-ui-1", "transcribe", "twitch", "prog-1001")
    archive_db.update_job("prog-ui-1", status="running", progress=0.42)
    archive_db.enqueue_job("prog-ui-2", "chat", "twitch", "prog-1002")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/archive/jobs")

    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    by_id = {j["id"]: j for j in jobs}

    tr = by_id["prog-ui-1"]
    assert tr["kind"] == "transcribe"
    assert tr["platform"] == "twitch"
    assert tr["video_id"] == "prog-1001"
    assert tr["status"] == "running"
    assert tr["progress"] == pytest.approx(0.42)
    # Display title prefers the original (non-auto-translated) copy.
    assert tr["title"] == "Título Original"

    chat = by_id["prog-ui-2"]
    assert chat["kind"] == "chat"
    assert chat["status"] == "queued"
    assert chat["title"] == "Sem transcrição ainda"


@pytest.mark.asyncio
async def test_archive_jobs_title_empty_when_video_row_absent():
    archive_db.enqueue_job("prog-ui-3", "transcribe", "twitch", "prog-9999")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/archive/jobs")

    assert resp.status_code == 200
    row = next(j for j in resp.json()["jobs"] if j["id"] == "prog-ui-3")
    assert row["title"] == ""
    # The endpoint shape is otherwise untouched: the full job row survives.
    assert row["kind"] == "transcribe" and row["status"] == "queued"


@pytest.mark.asyncio
async def test_archive_jobs_clear_removes_only_terminal_rows():
    archive_db.enqueue_job("prog-ui-clr-done", "transcribe", "twitch", "prog-1001")
    archive_db.update_job("prog-ui-clr-done", status="done", progress=1)
    archive_db.enqueue_job("prog-ui-clr-fail", "chat", "twitch", "prog-1001")
    # TASK10 requeues transient failures; only a terminal error (FileNotFound)
    # stays 'failed', so the seed must be terminal for clear to drop the row.
    archive_db.update_job(
        "prog-ui-clr-fail", status="failed", error="FileNotFound: missing archive")
    archive_db.enqueue_job("prog-ui-clr-run", "transcribe", "twitch", "prog-1001")
    archive_db.update_job("prog-ui-clr-run", status="running", progress=0.5)
    archive_db.enqueue_job("prog-ui-clr-que", "events", "twitch", "prog-1001")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/archive/jobs/clear")
        list_resp = await client.get("/api/archive/jobs")

    assert resp.status_code == 200
    assert resp.json()["cleared"] == 2
    ids = {j["id"] for j in list_resp.json()["jobs"]}
    # Terminal rows are gone; queued/running survive (worker + boot depend on them).
    assert "prog-ui-clr-done" not in ids
    assert "prog-ui-clr-fail" not in ids
    assert "prog-ui-clr-run" in ids
    assert "prog-ui-clr-que" in ids


@pytest.mark.asyncio
async def test_archive_jobs_clear_idempotent():
    archive_db.enqueue_job("prog-ui-clr2", "transcribe", "twitch", "prog-1001")
    archive_db.update_job("prog-ui-clr2", status="done", progress=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/archive/jobs/clear")
        second = await client.post("/api/archive/jobs/clear")

    assert first.json()["cleared"] == 1
    assert second.json()["cleared"] == 0
