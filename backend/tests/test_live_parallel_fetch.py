"""Live-status router tests — parallel platform fetches + non-blocking reads.

No network: the three platform fetchers are stubbed, the warm pool is stubbed
(so no background refresh can leak into other test modules), and the live
cache is reset per test. Covers:
  - _fetch_channel_live_payload runs Kick/Twitch/YouTube concurrently
  - /api/channels/{id}/live never blocks on a cold-miss refresh
  - stale payloads are served with a background refresh, fresh hits short-circuit
  - unknown channels 404
  - the archive watchdog routes through the shared cache (no duplicate fetches)
"""

import threading
import time
from types import SimpleNamespace

import pytest

from routers import live as live_router
from services import archive_watchdog as wd


class _FakePool:
    """Records submits; never executes (background refreshes are test-hostile)."""

    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return None


class _ImmediatePool:
    """Executes the submitted callable synchronously; returns a completed Future."""

    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        from concurrent.futures import Future

        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut


@pytest.fixture(autouse=True)
def _isolate_live_state(monkeypatch):
    live_router._LIVE_STATUS_CACHE.clear()
    live_router._LIVE_REFRESH_INFLIGHT.clear()
    pool = _FakePool()
    monkeypatch.setattr(live_router, "_LIVE_WARM_POOL", pool)
    yield pool
    live_router._LIVE_STATUS_CACHE.clear()
    live_router._LIVE_REFRESH_INFLIGHT.clear()


def _settings(channels):
    return SimpleNamespace(get=lambda: SimpleNamespace(saved_channels=channels))


def _live_entry(platform):
    return {
        "is_live": True,
        "platform": platform,
        "title": f"{platform} stream",
        "viewers": 1,
        "url": f"https://cdn.example.com/{platform}/live.m3u8",
        "headers": {},
        "type": "hls",
    }


# ---------------------------------------------------------------------------
# Parallel platform fetches
# ---------------------------------------------------------------------------

def test_platform_fetches_run_concurrently(monkeypatch):
    """3 platforms x 0.25s fakes: a serial chain takes >= 0.75s; the parallel
    implementation must finish in well under that (max, not sum)."""
    called = {"n": 0}

    def slow_fake(slug):
        time.sleep(0.25)
        called["n"] += 1
        return _live_entry("X")

    monkeypatch.setattr(live_router, "kick_live_info", slow_fake)
    monkeypatch.setattr(live_router, "twitch_live_info", slow_fake)
    monkeypatch.setattr(live_router, "youtube_live_info", slow_fake)

    channel = {"id": "ch_par1", "kickSlug": "k", "twitchSlug": "t", "youtubeSlug": "y"}
    t0 = time.monotonic()
    payload = live_router._fetch_channel_live_payload(channel)
    elapsed = time.monotonic() - t0

    assert called["n"] == 3
    assert elapsed < 0.6, f"platform fetches ran serially? wall={elapsed:.2f}s"
    # Platform labels are slot-derived and stable regardless of completion order.
    assert [e["platform"] for e in payload["live"]] == ["Kick", "Twitch", "YouTube"]


def test_missing_slugs_skip_fetches(monkeypatch):
    called = {"n": 0}

    def fake(slug):
        called["n"] += 1
        return None

    monkeypatch.setattr(live_router, "kick_live_info", fake)
    monkeypatch.setattr(live_router, "twitch_live_info", fake)
    monkeypatch.setattr(live_router, "youtube_live_info", fake)

    payload = live_router._fetch_channel_live_payload({"id": "ch_par2"})
    assert called["n"] == 0
    assert payload == {"live": [], "channel_id": "ch_par2"}


def test_first_platform_error_still_fails_the_refresh(monkeypatch):
    """A dead platform keeps the old failure semantics: the refresh fails
    (stale-serve kicks in) even though the other platforms succeeded."""

    def bad(slug):
        raise RuntimeError("kick API down")

    def good(slug):
        return _live_entry("Twitch")

    monkeypatch.setattr(live_router, "kick_live_info", bad)
    monkeypatch.setattr(live_router, "twitch_live_info", good)
    monkeypatch.setattr(live_router, "youtube_live_info", good)

    with pytest.raises(RuntimeError, match="kick API down"):
        live_router._fetch_channel_live_payload(
            {"id": "ch_par3", "kickSlug": "k", "twitchSlug": "t", "youtubeSlug": "y"}
        )


# ---------------------------------------------------------------------------
# /api/channels/{id}/live — never block, always background-refresh
# ---------------------------------------------------------------------------

def test_cold_miss_returns_instantly_and_schedules_background_refresh(monkeypatch, _isolate_live_state):
    pool = _isolate_live_state
    monkeypatch.setattr(
        live_router, "settings_mgr",
        _settings([{"id": "ch_cold1", "twitchSlug": "t"}]),
    )

    def slow_refresh(cid, channel):
        time.sleep(5.0)  # would blow the frontend budget if called inline
        return {"live": [], "channel_id": cid}

    monkeypatch.setattr(live_router, "_refresh_channel_live_cache", slow_refresh)

    t0 = time.monotonic()
    payload = live_router.channel_live_status("ch_cold1")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"endpoint blocked on the refresh: {elapsed:.2f}s"
    assert payload == {"live": [], "channel_id": "ch_cold1"}
    assert len(pool.calls) == 1
    assert pool.calls[0][1] == ("ch_cold1", {"id": "ch_cold1", "twitchSlug": "t"})


def test_stale_payload_served_empty_with_background_refresh(monkeypatch, _isolate_live_state):
    pool = _isolate_live_state
    monkeypatch.setattr(
        live_router, "settings_mgr",
        _settings([{"id": "ch_stale1", "kickSlug": "k"}]),
    )
    old = {"live": [_live_entry("Kick")], "channel_id": "ch_stale1"}
    # max-stale == TTL == 60s: a stale entry is never trusted — serve empty
    # (never a stale "LIVE" lie) and refresh in the background.
    live_router._LIVE_STATUS_CACHE["ch_stale1"] = (
        time.monotonic() - live_router._LIVE_STATUS_TTL_SEC - 1.0, old,
    )

    assert live_router.channel_live_status("ch_stale1") == {"live": [], "channel_id": "ch_stale1"}
    assert len(pool.calls) == 1, "stale read must kick a background refresh"


def test_fresh_cache_hit_short_circuits(monkeypatch, _isolate_live_state):
    pool = _isolate_live_state
    monkeypatch.setattr(live_router, "settings_mgr", _settings([{"id": "ch_fresh1"}]))
    payload = {"live": [], "channel_id": "ch_fresh1"}
    live_router._LIVE_STATUS_CACHE["ch_fresh1"] = (time.monotonic(), payload)

    assert live_router.channel_live_status("ch_fresh1") == payload
    assert pool.calls == [], "fresh reads must not schedule a refresh"


def test_beyond_max_stale_serves_empty(monkeypatch, _isolate_live_state):
    pool = _isolate_live_state
    monkeypatch.setattr(
        live_router, "settings_mgr",
        _settings([{"id": "ch_ancient1", "kickSlug": "k"}]),
    )
    ancient = {"live": [_live_entry("Kick")], "channel_id": "ch_ancient1"}
    live_router._LIVE_STATUS_CACHE["ch_ancient1"] = (
        time.monotonic() - live_router._LIVE_STATUS_MAX_STALE_SEC - 1.0, ancient,
    )

    assert live_router.channel_live_status("ch_ancient1") == {"live": [], "channel_id": "ch_ancient1"}
    assert len(pool.calls) == 1


def test_ttl_trip_waits_and_returns_fresh(monkeypatch, _isolate_live_state):
    """A TTL-trip read must return the FRESH payload (after the bounded wait),
    not the stale one — that is the poll that updates the badge, so a
    streamer going live shows up on this poll instead of the next one."""
    pool = _ImmediatePool()
    monkeypatch.setattr(live_router, "_LIVE_WARM_POOL", pool)
    monkeypatch.setattr(
        live_router, "settings_mgr",
        _settings([{"id": "ch_trip1", "twitchSlug": "t"}]),
    )
    stale = {"live": [], "channel_id": "ch_trip1"}
    fresh = {"live": [_live_entry("Twitch")], "channel_id": "ch_trip1"}
    live_router._LIVE_STATUS_CACHE["ch_trip1"] = (
        time.monotonic() - live_router._LIVE_STATUS_TTL_SEC - 1.0, stale,
    )
    monkeypatch.setattr(
        live_router, "_fetch_channel_live_payload",
        lambda ch: fresh,
    )

    payload = live_router.channel_live_status("ch_trip1")
    assert payload == fresh, "TTL-trip must return the refreshed payload, not the stale one"
    assert len(pool.calls) == 1


def test_ttl_trip_shares_inflight_refresh(monkeypatch, _isolate_live_state):
    """Two overlapping TTL-trip reads for the same channel must share ONE
    refresh — the inflight map dedupes so the rate-limited platform APIs are
    never double-fetched by the poll + watchdog."""
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        monkeypatch.setattr(live_router, "_LIVE_WARM_POOL", executor)
        monkeypatch.setattr(
            live_router, "settings_mgr",
            _settings([{"id": "ch_trip2", "twitchSlug": "t"}]),
        )
        stale = {"live": [], "channel_id": "ch_trip2"}
        fresh = {"live": [_live_entry("Twitch")], "channel_id": "ch_trip2"}
        live_router._LIVE_STATUS_CACHE["ch_trip2"] = (
            time.monotonic() - live_router._LIVE_STATUS_TTL_SEC - 1.0, stale,
        )
        calls = {"n": 0}

        def slow_fetch(channel):
            calls["n"] += 1
            time.sleep(0.2)
            return fresh

        monkeypatch.setattr(live_router, "_fetch_channel_live_payload", slow_fetch)

        # Simulate the watchdog already holding an in-flight refresh.
        inflight = live_router._submit_refresh("ch_trip2", {"id": "ch_trip2", "twitchSlug": "t"})
        assert inflight is not None

        payload = live_router.channel_live_status("ch_trip2")
        assert payload == fresh
        assert calls["n"] == 1, "second TTL-trip must reuse the in-flight refresh"
    finally:
        executor.shutdown(wait=True)


def test_unknown_channel_404(monkeypatch, _isolate_live_state):
    monkeypatch.setattr(live_router, "settings_mgr", _settings([]))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        live_router.channel_live_status("ch_ghost")
    assert exc.value.status_code == 404


def test_warm_all_submits_every_saved_channel(monkeypatch, _isolate_live_state):
    pool = _isolate_live_state
    channels = [{"id": f"ch_w{i}", "twitchSlug": "t"} for i in range(3)]
    monkeypatch.setattr(live_router, "settings_mgr", _settings(channels))

    live_router.warm_all_saved_channel_live_status()
    assert [c[1][0] for c in pool.calls] == ["ch_w0", "ch_w1", "ch_w2"]


# ---------------------------------------------------------------------------
# Archive watchdog routes through the shared live cache
# ---------------------------------------------------------------------------

def test_watchdog_poll_reuses_fresh_cache(monkeypatch):
    """_poll_live must reuse a cache entry younger than the watchdog's poll
    interval — the 30s loop used to re-fetch every channel from scratch each
    cycle, burning quota and warming nothing."""
    calls: list = []
    monkeypatch.setattr(
        live_router, "_fetch_channel_live_payload",
        lambda ch: calls.append(ch) or {
            "live": [{
                "is_live": True, "platform": "Twitch", "title": "T",
                "url": "u", "started_at": "2026-08-02T10:00:00Z",
            }],
            "channel_id": "ch_watch1",
        },
    )
    channel = {"id": "ch_watch1", "twitchSlug": "t"}
    cached = {"live": [{
        "is_live": True, "platform": "Twitch", "title": "T",
        "url": "u", "started_at": "2026-08-02T10:00:00Z",
    }], "channel_id": "ch_watch1"}
    try:
        live_router._LIVE_STATUS_CACHE["ch_watch1"] = (time.monotonic(), cached)
        entries = wd._poll_live(channel)
        assert calls == [], "fresh cache entry must not trigger a platform fetch"
        assert entries == [{
            "platform": "twitch", "title": "T", "url": "u",
            "started_at": "2026-08-02T10:00:00Z", "videoId": None,
        }]

        # Stale cache -> blocking refresh through the (patched) fetch; result
        # is cached for the next cycle.
        live_router._LIVE_STATUS_CACHE["ch_watch1"] = (
            time.monotonic() - wd.POLL_INTERVAL_SEC - 1.0, {"live": [], "channel_id": "ch_watch1"},
        )
        entries2 = wd._poll_live(channel)
        assert len(calls) == 1
        assert entries2 == [{
            "platform": "twitch", "title": "T", "url": "u",
            "started_at": "2026-08-02T10:00:00Z", "videoId": None,
        }]
    finally:
        live_router._LIVE_STATUS_CACHE.clear()
