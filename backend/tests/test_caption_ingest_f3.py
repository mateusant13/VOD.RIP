"""F3 caption ingest — channel-add/scheduler pump + scope tests.

The channel caption-ingest pump (_run_channel_caption_ingest) walks the
FULL enumerated uploads+shorts+streams scope from a persistent deep_jobs
cursor, and shorts are NOT skipped a priori. The settings channel-add path
fires it non-blocking on its own daemon thread. These tests pin:

  * the pump's scope = the enumerated uploads+shorts+streams list, and a
    short is actually captioned (not skipped a priori);
  * per-pass budget + persistent cursor: resume continues exactly at the
    cursor, re-fetching nothing already processed;
  * the channel-add path fires the pump ONLY for NEW channels with a
    youtubeSlug (and the sweep runs on its own thread — the save is not
    blocked by it).

Enumerate/fetch seams are monkeypatched — no network.
Run from backend/: python -m pytest tests/test_caption_ingest_f3.py
"""
from __future__ import annotations

import pytest

from routers import archive
from routers import settings as settings_router


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
    monkeypatch.setattr(archive, "_DEEP_MIN_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def _reset_caches():
    from services import archive_db

    archive._deep_enumerate_cache.clear()
    # The persistent per-channel cursor is shared state across tests — reset
    # it so one test's sweep can't resume another's cursor.
    try:
        archive_db.execute("DELETE FROM deep_jobs")
    except Exception:
        pass
    # The archive DB is the shared session store (see conftest) — transcripts
    # and no-captions markers written by an earlier module in the merged run
    # would make _deep_covered_ids pre-skip these tests' ids and break their
    # exact remote-fetch-count assertions. Wipe YouTube coverage state so each
    # caption test sees a cold store regardless of module/test order.
    try:
        archive_db.execute("DELETE FROM transcripts WHERE platform='youtube'")
        archive_db.execute(
            "UPDATE videos SET captions_unavailable_at=NULL WHERE platform='youtube'"
        )
    except Exception:
        pass
    yield
    archive._deep_enumerate_cache.clear()


def test_caption_pump_fetches_shorts_and_full_scope(monkeypatch, fast_pace):
    """The pump's scope is the enumerated uploads+shorts+streams list, and a
    short is NOT skipped a priori — it is captioned like any other."""
    videos = [
        _video("vid-upload", "2024-01-05T00:00:00+00:00", "u", content_kind="video"),
        _video("vid-short", "2024-01-04T00:00:00+00:00", "s", content_kind="short"),
        _video("vid-stream", "2024-01-03T00:00:00+00:00", "st", content_kind="stream"),
    ]
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        return _payload(vid, [(0.0, "césar aqui")])

    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: (videos, False, len(videos)))
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)

    stats = archive._run_channel_caption_ingest("deepchan", budget=10)
    assert "vid-short" in calls, "a short in the scope must be captioned, not skipped"
    # OLDEST-FIRST walk (FIX-2): the pump's persistent cursor is a monotonic
    # resume index, so it requires APPEND-ONLY ordering — new uploads (greater
    # created_at) must land at the TAIL or a stale cursor would skip them
    # forever. 2024-01-03 stream is oldest -> processed first.
    assert calls == ["vid-stream", "vid-short", "vid-upload"]
    assert stats["processed"] == 3

    # Cursor persisted -> a second pump resumes at the cursor (no re-fetch).
    calls.clear()
    stats2 = archive._run_channel_caption_ingest("deepchan", budget=10)
    assert calls == [], "resume at cursor must not re-fetch processed videos"
    assert stats2["processed"] == 0


def test_caption_pump_respects_budget_and_advances_cursor(monkeypatch, fast_pace):
    """A per-pass budget caps remote fetches; the cursor advances and the
    next pass resumes exactly there — nothing covered is re-fetched."""
    videos = [_video(f"b{i}", f"2024-01-{i+1:02d}T00:00:00+00:00", content_kind="video")
              for i in range(6)]
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        return _payload(vid, [(0.0, "nada relacionado")])

    monkeypatch.setattr(archive, "_deep_enumerate", lambda handle: (videos, False, len(videos)))
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)

    stats1 = archive._run_channel_caption_ingest("deepchan", budget=2)
    assert len(calls) == 2, "budget=2 -> only 2 remote fetches in this pass"
    assert stats1["cursor"] >= 2

    calls.clear()
    stats2 = archive._run_channel_caption_ingest("deepchan", budget=10)
    assert len(calls) == 4, "resume fetches only the uncovered tail (6 total, 2 done)"
    assert stats2["cursor"] == len(videos)


def test_channel_add_spawns_caption_ingest_only_for_new_slugged(monkeypatch):
    """Channel-save path: adding a NEW youtube channel fires the caption
    pump; an existing channel, or one without a youtubeSlug, does not."""
    spawned: list[str] = []
    monkeypatch.setattr(archive, "_start_caption_ingest_channel",
                        lambda handle: spawned.append(handle))

    # Patch the OTHER saved-channel side-effects off so only our caption
    # kick runs (each is a lazy `from X import Y` inside the block, so
    # monkeypatching the module attribute before the call works).
    import services.archive_scheduler as sched
    import services.instant_preview as ip
    from services import preview
    from routers import live as live_router

    monkeypatch.setattr(sched, "kick_scheduler_pass", lambda: None)
    monkeypatch.setattr(ip, "remove_channel_previews", lambda cid: None)
    monkeypatch.setattr(live_router, "trigger_live_detection", lambda cid: None)
    monkeypatch.setattr(preview.warm, "warm_youtube_recent_channels",
                        lambda channels, per_channel=5: None)
    monkeypatch.setattr(settings_router, "_prioritize_new_channels",
                        lambda old, new: None)

    old = [{"id": "c1", "youtubeSlug": "oldchan"}]
    # settings_mgr must hold the OLD list so the block computes old_ids new.
    from deps import settings_mgr
    settings_mgr._settings.saved_channels = old
    update = settings_router.SettingsUpdate()

    class _Fake:
        saved_channels = [
            {"id": "c1", "youtubeSlug": "oldchan"},   # existing -> no pump
            {"id": "c2", "youtubeSlug": "newchan"},   # new + slug -> pump
            {"id": "c3", "youtubeSlug": ""},          # new, no slug -> skip
            {"id": "c4"},                             # new, no youtube -> skip
        ]

    update.saved_channels = _Fake.saved_channels
    settings_router._apply_settings_update(update)
    assert spawned == ["newchan"], f"expected only the new slugged channel, got {spawned}"


def test_caption_pump_new_video_at_tail_is_reached(fast_pace, monkeypatch):
    """FIX-2 regression: the pump walks OLDEST-FIRST, so a NEW upload (greater
    created_at) always lands at the TAIL of the sorted list. A resume at a
    mid-list cursor therefore still reaches it (cursor < len) instead of
    parking permanently the way a newest-first prepend would."""
    # Initial pass: c=2024-01-01 (oldest, index 0), b=2024-01-02, a=2024-01-03.
    initial = [
        _video("c", "2024-01-01T00:00:00+00:00", content_kind="video"),
        _video("b", "2024-01-02T00:00:00+00:00", content_kind="video"),
        _video("a", "2024-01-03T00:00:00+00:00", content_kind="video"),
    ]
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        return _payload(vid, [(0.0, "césar aqui")])

    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    # Pass 1: budget=1 -> only the OLDEST (c) is fetched; cursor advances to 1.
    monkeypatch.setattr(archive, "_deep_enumerate",
                        lambda handle: (initial, False, len(initial)))
    stats1 = archive._run_channel_caption_ingest("deepchan", budget=1)
    assert calls == ["c"], "pass 1 fetches only the oldest video (index 0)"
    assert stats1["processed"] == 1

    # A NEW upload (x, newer than everything) appears in the enumerate list.
    # OLDEST-FIRST sort appends it at the TAIL: [c, b, a, x].
    grown = initial + [_video("x", "2024-01-04T00:00:00+00:00", content_kind="video")]
    # Force the enumerate cache to refresh so the pump sees the GROWN list.
    archive._deep_enumerate_cache.clear()
    monkeypatch.setattr(archive, "_deep_enumerate",
                        lambda handle: (grown, False, len(grown)))
    calls.clear()
    stats2 = archive._run_channel_caption_ingest("deepchan", budget=10)
    # cursor (1) < new len (4) -> the uncovered videos AND the new tail one
    # are all reached. The new upload x is NOT parked forever.
    assert "x" in calls, "a newer upload must be reached on resume (tail, not prepended)"
    # b, a, and x are all still uncovered -> fetches exactly those three.
    assert calls == ["b", "a", "x"]
    assert stats2["processed"] == 3