"""Deep search pause/resume — park-loop + poll-flag tests.

Pause is a `paused: threading.Event()` on the job (status stays 'running',
so dedupe/running-cap/prune need zero changes); the poll snapshot gains a
single `paused: bool` key. Every existing field stays byte-identical — old
FE builds must not break.

The fetch/enumeration seams (routers.archive._deep_fetch_transcript /
_deep_enumerate) are monkeypatched — no network, no real 1.5s pace.

Run from backend/:
python -m pytest tests/test_archive_deep_pause.py \
    tests/test_archive_deep_search.py tests/test_archive_search_kind_filter.py
"""
from __future__ import annotations

import asyncio
import time

import pytest

from routers import archive


def _video(vid: str, created: str, title: str = "t", content_kind: str = "short") -> dict:
    return {
        "id": vid,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "created_at": created,
        "channel": "deepchan",
        "content_kind": content_kind,
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
    """No 1.5s pacing in tests — the gate itself is covered by reading code."""
    monkeypatch.setattr(archive, "_DEEP_MIN_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def isolate_deep_jobs():
    """Deep jobs are module-global (dedupe + running cap read them); clear
    the table around each test so one test's sweep can't join/409 another's."""
    with archive._deep_jobs_lock:
        archive._deep_jobs.clear()
    yield
    with archive._deep_jobs_lock:
        for j in archive._deep_jobs.values():
            j["cancel"].set()
        archive._deep_jobs.clear()


def _start(monkeypatch, videos, fetcher) -> str:
    """Patch seams, start the job, return the id WITHOUT waiting for settle."""
    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: (videos, False, len(videos)))
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    return asyncio.run(
        archive.archive_search_deep_start(
            archive.DeepSearchRequest(channel="deepchan", query="cesar")
        )
    )["job_id"]


def _poll(job_id: str) -> dict:
    return asyncio.run(archive.archive_search_deep_status(job_id))


def _wait_running_poll(job_id: str, want_scanned: int, timeout_s: float = 10.0) -> dict:
    """Wait until scanned reaches `want_scanned` while still running."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = _poll(job_id)
        if snap["status"] == "running" and snap["scanned"] >= want_scanned:
            return snap
        time.sleep(0.01)
    snap = _poll(job_id)
    raise AssertionError(
        f"job never reached scanned={want_scanned} while running: {snap['status']}/{snap['scanned']}"
    )


def _wait_terminal(job_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = _poll(job_id)
        if snap["status"] != "running":
            return snap
        time.sleep(0.01)
    raise AssertionError("deep job did not settle in time")


def _videos(prefix: str, n: int, **kwargs) -> list[dict]:
    return [
        _video(f"{prefix}{i}", f"2024-04-{(i % 28) + 1:02d}T00:00:00+00:00", **kwargs)
        for i in range(n)
    ]


def _cesar_fetch(calls: list[str], delay: float = 0.01):
    def fetcher(vid: str) -> dict:
        calls.append(vid)
        time.sleep(delay)
        return _payload(vid, [(0.0, f"dar a cesar o que é de {vid}")])

    return fetcher


def test_pause_freezes_scanned_and_resume_continues(monkeypatch, fast_pace):
    """(a)+(b)+(d): pause parks the workers (scanned stops advancing, poll
    shows paused=True, status stays 'running'); resume lets the sweep finish."""
    videos = _videos("pa", 30, content_kind="short")
    calls: list[str] = []
    job_id = _start(monkeypatch, videos, _cesar_fetch(calls))

    _wait_running_poll(job_id, want_scanned=4)
    fresh = _poll(job_id)
    assert fresh["paused"] is False, "a running job starts unpaused"

    assert asyncio.run(archive.archive_search_deep_pause(job_id)) == {"ok": True}
    # In-flight fetches (<=2 pool workers) may still land — let them settle.
    time.sleep(0.5)
    frozen = _poll(job_id)
    assert frozen["status"] == "running", "pause must NOT flip the status"
    assert frozen["paused"] is True
    assert frozen["scanned"] > 0
    assert frozen["total"] == 30, "progress counters stay poll-visible while paused"
    # Poll while paused keeps serving the accumulated partials.
    assert len(frozen["results"]) > 0, "partial hits must be poll-visible while paused"

    time.sleep(0.8)
    again = _poll(job_id)
    assert again["scanned"] == frozen["scanned"], "pause must freeze scanned progress"

    assert asyncio.run(archive.archive_search_deep_resume(job_id)) == {"ok": True}
    final = _wait_terminal(job_id)
    assert final["status"] == "done"
    assert final["paused"] is False
    assert final["scanned"] == 30
    assert len(calls) == 30, "every video still fetched exactly once after resume"


def test_cancel_while_paused_terminates_as_cancelled_unpaused(monkeypatch, fast_pace):
    """(c) + L1: the park loop re-checks cancel — a paused sweep is
    cancellable — and the terminal poll serves status cancelled with
    paused FALSE (no stale paused=true on the terminal job)."""
    videos = _videos("cp", 30, content_kind="short")
    calls: list[str] = []
    job_id = _start(monkeypatch, videos, _cesar_fetch(calls))

    _wait_running_poll(job_id, want_scanned=2)
    assert asyncio.run(archive.archive_search_deep_pause(job_id)) == {"ok": True}
    time.sleep(0.3)
    assert _poll(job_id)["paused"] is True

    assert asyncio.run(archive.archive_search_deep_cancel(job_id)) == {"ok": True}
    final = _wait_terminal(job_id)
    assert final["status"] == "cancelled"
    assert final["paused"] is False, "terminal jobs must not serve stale paused=true"
    # Cancel must have won over the park: queued-but-unstarted videos never
    # entered the fetcher after the cancel landed.
    assert len(calls) < 30


def test_pause_resume_404_unknown_job():
    """404 for unknown job ids on both endpoints."""
    for endpoint in (archive.archive_search_deep_pause, archive.archive_search_deep_resume):
        with pytest.raises(Exception) as exc:
            asyncio.run(endpoint("nope"))
        assert exc.value.status_code == 404


def test_pause_resume_409_on_finished_job(monkeypatch, fast_pace):
    """A done job answers pause/resume with 409 + an honest detail."""
    videos = _videos("t9", 2, content_kind="short")
    calls: list[str] = []
    job_id = _start(monkeypatch, videos, _cesar_fetch(calls))
    final = _wait_terminal(job_id)
    assert final["status"] == "done"
    for endpoint in (archive.archive_search_deep_pause, archive.archive_search_deep_resume):
        with pytest.raises(Exception) as exc:
            asyncio.run(endpoint(job_id))
        assert exc.value.status_code == 409
        assert "done" in str(exc.value.detail)


def test_result_dict_carries_video_kind_when_known_and_omits_when_unknown(
    monkeypatch, fast_pace
):
    """The contract addition: deep hits ship video_kind in the SAME
    vocabulary the unified search ships (content_kind 'video' -> 'vod',
    same map as _deep_seed_video_rows) so the FE kind chips compose;
    when unknown the key is omitted (never guessed). Cached (Pass 1)
    and fetched (Pass 2) hits both spell it."""
    from services import archive_db

    cached = _video("vkc", "2024-05-02T00:00:00+00:00", "cached hit", content_kind="video")
    fetched = _video("vkf", "2024-05-01T00:00:00+00:00", "fetched hit", content_kind="short")
    unknown = _video("vku", "2024-05-03T00:00:00+00:00", "mystery hit")
    del unknown["content_kind"]
    videos = [cached, fetched, unknown]
    # Pre-warm the transcript cache for the Pass 1 video.
    archive_db.insert_transcript("youtube", "vkc", [{
        "seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
        "text": "dar a cesar o que é de cesar (cache)",
    }])
    calls: list[str] = []
    job_id = _start(monkeypatch, videos, _cesar_fetch(calls))
    final = _wait_terminal(job_id)
    assert final["status"] == "done"
    by_id = {r["id"]: r for r in final["results"]}
    assert by_id["vkc"]["video_kind"] == "vod", "Pass 1 upload hit maps video->vod"
    assert calls == ["vkf", "vku"], "cached video must not be fetched"
    assert by_id["vkf"]["video_kind"] == "short", "Pass 2 hit carries the tab kind"
    assert "video_kind" not in by_id["vku"], "unknown kind must omit the key"
