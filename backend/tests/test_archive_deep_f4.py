"""F4 deep-ALL acceptance tests.

Covers (from the task's acceptance criteria):
  (iii) pagination: a fake 1200-video channel tab is paged past the first
        _DEEP_TAB_LIMIT window via the `start` offset; enumerated_total ==
        1200 and truncated is False because window 2 fully covers the tail.
  (iv)  deep matching spans chat AND titles: a query present ONLY in a chat
        message and ONLY in a title both surface as hits, tagged source
        "chat" / "title", without any transcript fetch.
  (v)   a transcript holding 9 occurrences of the query yields 9 hits (the
        per-video result cap is gone).

The enumeration seam (routers.archive._deep_enumerate) and the caption
fetch seam (routers.archive._deep_fetch_transcript) are monkeypatched; chat
and title rows are seeded through archive_db.execute directly — no network.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from routers import archive
from services import archive_db


def _video(vid: str, created: str, title: str = "t") -> dict:
    return {
        "id": vid,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "created_at": created,
        "channel": "deepchan",
        "content_kind": "video",
        "duration": 60,
        "duration_string": "1:00",
        "views": 10,
        "thumbnail_url": None,
    }


def _payload(vid: str, lines: list[tuple[float, str]]) -> dict:
    return {
        "url": f"https://www.youtube.com/watch?v={vid}",
        "lang": "pt",
        "source": "auto",
        "has_subtitles": True,
        "rows": [{"offset_sec": s, "text": t} for s, t in lines],
    }


@pytest.fixture()
def fast_pace(monkeypatch):
    monkeypatch.setattr(archive, "_DEEP_MIN_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def isolate_deep_jobs():
    with archive._deep_jobs_lock:
        archive._deep_jobs.clear()
    yield
    with archive._deep_jobs_lock:
        for j in archive._deep_jobs.values():
            j["cancel"].set()
        archive._deep_jobs.clear()


def _run(
    monkeypatch,
    videos,
    fetcher,
    query="cesar",
    channel="deepchan",
    *,
    enumerate_result=None,
) -> dict:
    if enumerate_result is None:
        enumerate_result = (videos, False, len(videos))
    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: enumerate_result)
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    job_id = asyncio.run(
        archive.archive_search_deep_start(
            archive.DeepSearchRequest(channel=channel, query=query)
        )
    )["job_id"]
    for _ in range(400):
        with archive._deep_jobs_lock:
            job = dict(archive._deep_jobs[job_id])
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("deep job did not settle in 8s")


# ── (iii) windowed pagination: 1200-video tab resolves to covered ──────────
def test_deep_paginates_past_single_window_1200_videos(monkeypatch, fast_pace):
    """A 1200-row channel must be enumerated exhaustively via two windows
    (1000 + 200), NOT capped at the first thousand. enumerated_total == 1200
    and truncated is False: window 2 covers the tail, so the sweep claims
    full coverage (acceptance iii)."""
    videos = [_video(f"v{i:04d}", f"2023-01-01T00:00:00+00:00") for i in range(1200)]

    def fetcher(vid: str) -> dict:
        return _payload(vid, [(0.0, "nada relacionado")])

    job = _run(monkeypatch, videos, fetcher)
    assert job["status"] == "done"
    assert job["total"] == 1200
    assert job["enumerated_total"] == 1200
    assert job["truncated"] is False
    assert job["scanned"] == 1200


# ── (iv) chat-only + title-only queries both surface ───────────────────────
def test_deep_matches_chat_only_query(monkeypatch, fast_pace):
    """A query that exists ONLY in a chat message (no transcript contains it)
    still hits, tagged source == 'chat' (acceptance iv)."""
    # Transcript has NO hit; the chat message carries the query.
    videos = [_video("c1", "2024-01-02T00:00:00+00:00", "Sobre gatos")]
    archive_db.execute(
        "INSERT INTO messages (platform, video_id, offset_sec, username, text) "
        "VALUES ('youtube', 'c1', 42.0, 'viewer', 'alguém falou CESAR aqui no chat')"
    )

    def fetcher(vid: str) -> dict:
        return _payload(vid, [(7.0, "falando de outra coisa")])

    job = _run(monkeypatch, videos, fetcher)
    assert job["status"] == "done"
    hits = [r for r in job["results"] if r["id"] == "c1"]
    assert len(hits) >= 1
    assert any(h["source"] == "chat" for h in hits)
    chat_hit = next(h for h in hits if h["source"] == "chat")
    assert chat_hit["ts"] == 42
    assert "CESAR" in chat_hit["snippet"]


def test_deep_matches_title_only_query(monkeypatch, fast_pace):
    """A query present ONLY in a video title surfaces as a hit tagged
    source == 'title', even with no transcript at all (acceptance iv)."""
    videos = [_video("t1", "2024-01-02T00:00:00+00:00", "César e a Grande Guerra")]

    def fetcher(vid: str) -> dict:
        return _payload(vid, [(0.0, "sem nada a ver")])

    job = _run(monkeypatch, videos, fetcher, query="guerra")
    assert job["status"] == "done"
    hits = [r for r in job["results"] if r["id"] == "t1"]
    assert len(hits) >= 1
    assert any(h["source"] == "title" for h in hits)
    title_hit = next(h for h in hits if h["source"] == "title")
    assert title_hit["ts"] == 0
    assert "Grande Guerra" in title_hit["snippet"]


# ── (v) no per-video cap: all 9 occurrences surface ────────────────────────
def test_deep_returns_all_occurrences_in_one_video(monkeypatch, fast_pace):
    """F4c: the 5-cap is gone — a transcript with 9 occurrences in one video
    yields 9 result rows (acceptance v)."""
    line = " ".join(["aqui tem o termo chave AGULHA"] * 3)  # 3 per segment
    segments = [(float(i * 10), line) for i in range(3)]  # 9 total
    videos = [_video("n1", "2024-01-02T00:00:00+00:00", "novelo")]

    def fetcher(vid: str) -> dict:
        return _payload(vid, segments)

    job = _run(monkeypatch, videos, fetcher, query="agulha")
    assert job["status"] == "done"
    hits = [r for r in job["results"] if r["id"] == "n1"]
    assert len(hits) == 9
    assert {h["source"] for h in hits} == {"transcript"}


# ── (iii) enumerated_total reported even when truncated by the hard cap ────
def test_deep_reports_enumerated_total_when_truncated(monkeypatch, fast_pace):
    """When the merge exceeds _DEEP_ENUM_MAX, truncated is True but the
    enumerated_total still reports how far the crawl really got."""
    n = archive._DEEP_ENUM_MAX + 10
    videos = [_video(f"h{i:04d}", f"2022-01-01T00:00:00+00:00") for i in range(n)]

    def fetcher(vid: str) -> dict:
        return _payload(vid, [(0.0, "nada")])

    job = _run(
        monkeypatch, videos, fetcher,
        enumerate_result=(videos[: archive._DEEP_ENUM_MAX], True, n),
    )
    assert job["status"] == "done"
    assert job["enumerated_total"] == n
    assert job["truncated"] is True
    assert job["total"] == len(videos[: archive._DEEP_ENUM_MAX])