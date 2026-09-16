"""Deep channel-transcript search — sweep-job tests.

Covers the job lifecycle (start -> running -> done), cancel, the shared
transcript cache (a second sweep performs ZERO fresh caption fetches — the
write-through to archive_db.transcripts is what makes re-queries cheap), the
negative marker on failed/no-caption fetches (re-sweeps pre-skip dead videos
instead of re-requesting them forever), the batched coverage probe (constant
query count vs per-video existence checks), and the deaccented matcher.

The fetch/enumeration seams (routers.archive._deep_fetch_transcript /
_deep_enumerate) are monkeypatched — no network.

Run from backend/: python -m pytest tests/test_archive_deep_search.py
"""
from __future__ import annotations
import re
import threading
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
    """No 1.5s pacing in tests — the gate itself is covered by reading code."""
    monkeypatch.setattr(archive, "_DEEP_MIN_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def isolate_deep_jobs():
    """Deep jobs are module-global (dedupe + running cap read them); clear
    the table around each test so one test's sweep can't join/409 another's.
    Also frees the 24h-marker test DB between sweeps."""
    with archive._deep_jobs_lock:
        archive._deep_jobs.clear()
    yield
    with archive._deep_jobs_lock:
        for j in archive._deep_jobs.values():
            j["cancel"].set()
        archive._deep_jobs.clear()


def _run(monkeypatch, videos, fetcher) -> tuple[str, dict]:
    """Patch seams, start the job through the endpoint + thread, return job."""
    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: (videos, False, len(videos)))
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    import asyncio

    job_id = asyncio.run(
        archive.archive_search_deep_start(archive.DeepSearchRequest(channel="deepchan", query="cesar"))
    )["job_id"]
    for _ in range(200):
        with archive._deep_jobs_lock:
            job = dict(archive._deep_jobs[job_id])
        if job["status"] != "running":
            return job_id, job
        time.sleep(0.025)
    raise AssertionError("deep job did not settle in 5s")


def test_sweep_finds_deaccented_hit_and_caches_transcript(monkeypatch, fast_pace):
    videos = [
        _video("d1", "2024-01-02T00:00:00+00:00", "SOTAQUE"),
        _video("d2", "2024-01-01T00:00:00+00:00", "outro"),
    ]
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        if vid == "d1":
            return _payload(vid, [(0.0, "intro"), (7.0, "dar a césar o que é de essência")])
        return _payload(vid, [(0.0, "nada relacionado")])

    job_id, job = _run(monkeypatch, videos, fetcher)
    assert job["status"] == "done"
    assert job["total"] == 2
    assert job["scanned"] == 2
    assert job["no_transcript"] == 0
    assert sorted(calls) == ["d1", "d2"]

    hits = [r for r in job["results"] if r["id"] == "d1"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["ts"] == 7
    assert "césar" in hit["snippet"]
    assert hit["title"] == "SOTAQUE"
    assert hit["date"] == "2024-01-02"
    assert hit["url"].endswith("v=d1")

    # Write-through: the sweep warmed the SAME cache the app reads.
    assert archive_db.has_transcript("youtube", "d1")
    stored = archive_db.transcript_for("youtube", "d1")
    assert any("césar" in r["text"] for r in stored)

    # Oracle (stargazer review): second query on the same channel performs
    # ZERO fresh caption downloads — coverage is read batched from the DB.
    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: (videos, False, len(videos)))
    fetch_calls_after = len(calls)
    _, job2 = _run(monkeypatch, videos, fetcher)
    assert job2["status"] == "done"
    assert len(calls) == fetch_calls_after, "cached sweep must not re-fetch"
    assert [r for r in job2["results"] if r["id"] == "d1"], "cache must still match"


def test_failed_and_captionless_fetches_get_negative_marker_preskip(monkeypatch, fast_pace):
    videos = [_video("d3", "2024-01-03T00:00:00+00:00", "falha"),
              _video("d4", "2024-01-04T00:00:00+00:00", "sem legenda")]
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        if vid == "d3":
            raise RuntimeError("socket hangup")  # transport failure, not the gate
        return {"url": "", "lang": None, "source": None, "has_subtitles": False, "rows": []}

    _, job = _run(monkeypatch, videos, fetcher)
    assert job["status"] == "done"
    assert job["no_transcript"] == 2
    assert len(calls) == 2

    # YtcConcept review item (1): failures AND no-caption verdicts stamp the
    # persistent marker, so re-sweeps pre-skip them (marker rides a seeded row).
    for vid in ("d3", "d4"):
        assert archive_db.captions_unavailable_at("youtube", vid), vid

    _, job2 = _run(monkeypatch, videos, fetcher)
    assert job2["status"] == "done"
    assert len(calls) == 2, "second sweep must not re-request dead videos"
    assert job2["no_transcript"] == 2


def test_coverage_probe_is_batched(monkeypatch, fast_pace):
    """The pre-skip probe scales O(chunks), not O(videos): one sweep of 12
    videos over the 500-id chunk size must not issue per-video SELECTs."""
    videos = [_video(f"b{i}", f"2024-02-{i % 27 + 1:02d}T00:00:00+00:00") for i in range(12)]
    for v in videos:
        archive_db.insert_transcript(
            "youtube", v["id"], [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 5.0, "text": "x"}], lang="pt",
        )
    real_query = archive_db.query
    seen: list[str] = []

    def counting_query(sql, params=()):
        seen.append(" ".join(sql.split()))
        return real_query(sql, params)

    monkeypatch.setattr(archive.archive_db, "query", counting_query)
    _, job = _run(monkeypatch, videos, lambda vid: pytest.fail("all covered — no fetch"))
    assert job["status"] == "done"
    assert job["scanned"] == 12
    probes = [s for s in seen if "video_id IN (" in s and "FROM transcripts" in s]
    assert len(probes) == 1, f"expected one batched coverage SELECT, got {len(probes)}"
    # No per-video existence probes anywhere in the sweep.
    assert not [s for s in seen if re.search(r"FROM transcripts WHERE platform = \? AND video_id = \? LIMIT", s)]


def test_cancel_stops_sweep(monkeypatch, fast_pace):
    videos = [_video(f"c{i}", f"2024-03-{i + 1:02d}T00:00:00+00:00") for i in range(6)]
    calls: list[str] = []
    import asyncio

    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: (videos, False, len(videos)))

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        with archive._deep_jobs_lock:
            job = archive._deep_jobs[job_id]
        deadline = time.monotonic() + 10.0
        while not job["cancel"].is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not job["cancel"].is_set():
            raise AssertionError("cancel never arrived")
        return {"url": "", "lang": None, "source": None, "has_subtitles": False, "rows": []}

    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    job_id = asyncio.run(
        archive.archive_search_deep_start(archive.DeepSearchRequest(channel="deepchan", query="cesar"))
    )["job_id"]
    for _ in range(200):
        if calls:
            break
        time.sleep(0.025)
    assert calls, "sweep should have started fetching"
    asyncio.run(archive.archive_search_deep_cancel(job_id))
    for _ in range(400):
        with archive._deep_jobs_lock:
            status = archive._deep_jobs[job_id]["status"]
        if status != "running":
            break
        time.sleep(0.025)
    assert status == "cancelled"
    # Queued-but-unstarted videos returned early: at most the 2 pool workers
    # could have entered the fetcher before the cancel landed.
    assert len(calls) <= 2


def test_status_and_cancel_404_unknown_job():
    import asyncio

    with pytest.raises(Exception) as exc:
        asyncio.run(archive.archive_search_deep_status("nope"))
    assert exc.value.status_code == 404
    with pytest.raises(Exception) as exc:
        asyncio.run(archive.archive_search_deep_cancel("nope"))
    assert exc.value.status_code == 404


def test_matcher_unit_deaccent_snippet_all_occurrences():
    segs = [
        (0.0, "x" * 200 + " DAR A CÉSAR O QUE É DE CÉSAR " + "y" * 200),
        (30.0, "outra fala com cesar mesmo"),
        (60.0, "terceira vez cesar aqui"),
        (90.0, "quarta cesar"),
        (120.0, "quinta cesar"),
        (150.0, "sexta cesar"),
    ]
    out = archive._deep_match_segments("César", segs)
    # Every occurrence is returned — no per-video cap (F4c). The first
    # segment has TWO ("césar" twice), the rest one each -> 7 total.
    assert len(out) == 7
    assert [o["ts"] for o in out] == [0, 0, 30, 60, 90, 120, 150]
    first = out[0]["snippet"]
    assert first.startswith("…") and first.endswith("…")
    body = first.strip("…")
    # ±120-char window around the (deaccented) match — the match text is
    # kept, the far filler is cut.
    assert len(body) <= 2 * archive._DEEP_SNIPPET_PAD + 40
    assert "DAR A CÉSAR" in body
    assert archive._deep_match_segments("   ", segs) == []
    # A raw '(' is a LITERAL character, not a pattern — it must still find
    # the substring (regex mode is gone).
    assert archive._deep_match_segments("(", [(0.0, "um ( aqui")])
    # '.' is a literal dot, not the regex wildcard: 'ce.ar' must NOT match
    # 'cesar' (literal semantics — see D1).
    assert archive._deep_match_segments("ce.ar", segs) == []
    # Metacharacters in pt-BR currency queries match literally, never as
    # regex anchors/wildcards.
    assert [o["ts"] for o in archive._deep_match_segments("R$ 10", [(0.0, "custou R$ 10 ontem")])] == [0]
    # ß→ss deaccent expansion still maps the snippet back to the original
    # text (D3 offset map).
    hit = archive._deep_match_segments("strasse", [(0.0, "a STRASSE grande")])
    assert hit and "STRASSE" in hit[0]["snippet"]


def test_enumerate_dedupes_sorts_and_reports_truncation(monkeypatch):
    """_deep_enumerate contract (L3/L4/L6): out-of-order + cross-tab dupes
    collapse to a deduped newest-first list; ANY tab that saturates the
    crawl, hits the row ceiling, or errors sets truncated."""
    from services import youtube_service

    calls: list[dict] = []

    def fake_list(handle, limit, *, playlist, enrich, return_has_more, return_crawl_saturation=False, start=0):
        calls.append({"playlist": playlist, "limit": limit, "start": start})
        assert return_has_more and return_crawl_saturation, "deep must ask the raw signal"
        if playlist == "videos":
            # unsorted on purpose; v2 also appears in shorts (cross-tab dupe)
            return [_video("v3", "2024-01-03T00:00:00+00:00"),
                    _video("v1", "2024-01-01T00:00:00+00:00"),
                    _video("v2", "2024-01-02T00:00:00+00:00")], False, False
        if playlist == "shorts":
            # first window saturated (raw crawl bound, independent of
            # has_more — L4); the walk pages forward and the second window
            # resolves it to covered (no truncation from THIS tab).
            if start == 0:
                return [_video("v5", "2024-01-05T00:00:00+00:00"),
                        _video("v2", "2024-01-02T00:00:00+00:00", title="dupe")], False, True
            return [], False, False
        # streams tab explodes — honest partial (L3)
        raise RuntimeError("bot gate on tab crawl")

    monkeypatch.setattr(youtube_service, "list_channel_videos_sync", fake_list)
    items, truncated, _total = archive._deep_enumerate("deepchan")
    assert [v["id"] for v in items] == ["v5", "v3", "v2", "v1"], "deduped, newest-first"
    assert truncated is True, "failed tab must report truncation"
    assert {c["playlist"] for c in calls} == {"videos", "shorts", "streams"}
    assert {c["start"] for c in calls if c["playlist"] == "shorts"} == {0, archive._DEEP_TAB_LIMIT}

    # A clean crawl (nothing saturated, no errors, under ceiling) stays
    # truncated=False — the flag must not latch on.
    def clean_list(handle, limit, *, playlist, enrich, return_has_more, return_crawl_saturation=False, start=0):
        return [_video(f"{playlist}1", "2024-01-01T00:00:00+00:00")], False, False

    monkeypatch.setattr(youtube_service, "list_channel_videos_sync", clean_list)
    items, truncated, total = archive._deep_enumerate("deepchan")
    assert truncated is False and len(items) == 3 and total == 3

    # A tab of exactly _DEEP_TAB_LIMIT rows saturates window 1, and the
    # EMPTY window 2 resolves it — fully covered, NOT truncated (a second
    # page covers it; acceptance iii).
    def full_list(handle, limit, *, playlist, enrich, return_has_more, return_crawl_saturation=False, start=0):
        if start == 0:
            rows = [_video(f"{playlist}{i}", f"2023-01-01T00:00:{i % 60:02d}+00:00")
                    for i in range(archive._DEEP_TAB_LIMIT)]
            return rows, False, True  # bound hit -> saturated
        return [], False, False  # empty second page -> exhausted

    monkeypatch.setattr(youtube_service, "list_channel_videos_sync", full_list)
    items, truncated, total = archive._deep_enumerate("deepchan")
    assert truncated is False
    # 3 tabs x 1000 distinct rows, all resolved to covered by empty page 2.
    assert total == 3 * archive._DEEP_TAB_LIMIT == len(items)


def test_same_channel_start_joins_running_job_and_cap_409(monkeypatch):
    """D2a: a second POST for a RUNNING channel returns the existing job
    (handle-normalised: case/@ insensitive); different channels start until
    the running cap, then 409."""
    import asyncio

    started: list[str] = []
    _real_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        name = str(kwargs.get("name", ""))
        if name.startswith("deep-search-"):
            # Sweep threads must NOT actually run (their enumeration would
            # hit the real, unmocked network) — count them as spawned but
            # make them no-ops, exactly as before.
            started.append(name)
            class _Noop:
                def start(self_inner):
                    pass

            return _Noop()
        # Every OTHER thread (notably the ThreadPoolExecutor workers that
        # asyncio.to_thread uses inside the start handler for the DB resume
        # lookup/finalize) must be REAL — a no-op here would starve the
        # to_thread future and hang the awaited handler.
        return _real_thread(*args, **kwargs)

    monkeypatch.setattr(archive.threading, "Thread", fake_thread)
    post = lambda ch: asyncio.run(
        archive.archive_search_deep_start(archive.DeepSearchRequest(channel=ch, query="abc"))
    )
    r1 = post("deepchan")
    r2 = post("@DeepChan")  # same handle normalised -> join
    assert r2["job_id"] == r1["job_id"] and r2.get("joined") is True
    r3 = post("otherchan")  # second channel -> starts (running=2)
    assert r3["job_id"] != r1["job_id"]
    with pytest.raises(Exception) as exc:
        post("thirdchan")  # at cap -> 409, no new thread
    assert exc.value.status_code == 409
    assert len(started) == 2, "only the two accepted jobs spawn threads"


def test_chat_matcher_stops_at_source_cap(monkeypatch):
    """FIX-1 regression: _deep_match_chat must NOT emit one hit per matching
    message over an unbounded video set — it stops at _DEEP_SOURCE_RESULT_CAP
    and reports capped=True so the job surfaces truncated_results."""
    # Seed >cap matching chat messages across video_ids (assert itself the
    # DB write landed so the query genuinely sees _DEEP_SOURCE_RESULT_CAP
    # matches). _deep_match_titles runs first in the pipeline but is a no-op
    # here (titles don't contain the query), isolating the chat cap.
    cap = archive._DEEP_SOURCE_RESULT_CAP
    n = cap + 50
    ids = [f"cv{i:05d}" for i in range(n)]
    for i, vid in enumerate(ids):
        archive_db.execute(
            "INSERT INTO messages (platform, video_id, offset_sec, username, text) "
            "VALUES ('youtube', ?, ?, 'viewer', ?)",
            (vid, float(i), f"mensagem com CESAR numero {i}"),
        )
    hits, capped = archive._deep_match_chat("cesar", ids)
    assert capped is True, "chat matcher must set capped when it hits the bound"
    assert len(hits) == cap, "chat matcher must stop exactly at the source cap"
    assert all(h["source"] == "chat" for h in hits)
    assert {h["video_id"] for h in hits} == set(ids[:cap]), (
        "first cap videos matched, nothing beyond the stop point"
    )


def test_job_surfaces_truncated_results_in_status_snapshot(monkeypatch, fast_pace):
    """FIX-1 regression: a sweep that crosses the per-source result cap must
    surface `truncated_results: True` in the status snapshot, keeping the FE
    informed that the hit list is bounded (not silent breakage)."""
    cap = archive._DEEP_SOURCE_RESULT_CAP
    videos = [_video(f"tv{i:05d}", f"2024-01-{i % 28 + 1:02d}T00:00:00+00:00",
                     "vlog neutral") for i in range(cap + 5)]
    for i, v in enumerate(videos):
        archive_db.execute(
            "INSERT INTO messages (platform, video_id, offset_sec, username, text) "
            "VALUES ('youtube', ?, ?, 'viewer', ?)",
            (v["id"], float(i), f"caixa do CESAR numero {i}"),
        )

    def fetcher(vid: str) -> dict:
        return _payload(vid, [(0.0, "nada relacionado")])

    job_id, job = _run(monkeypatch, videos, fetcher)
    assert job["status"] == "done"
    assert job["truncated_results"] is True, (
        "job crossing the chat source cap must flag truncated_results"
    )
    assert job["truncated"] is False, "enumerate itself was NOT truncated"
    assert len(job["results"]) == cap, "results list bounded at the source cap"
    # The status endpoint snapshot carries the same flag (FE sees it on poll).
    import asyncio
    snap = asyncio.run(archive.archive_search_deep_status(job_id))
    assert snap["truncated_results"] is True
    assert snap["truncated"] is False
