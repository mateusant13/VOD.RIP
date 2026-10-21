"""The live-status cache survives a restart, so the first render has badges.

Measured 2026-10-20 on the live app: the cache was in-memory only, so every
start re-derived all 39 channels from Kick/Twitch/YouTube before a single badge
could paint (11-16 s per cold channel), the boot warm burst fired ~117 remote
fetches at once, the API stopped answering and the supervisor restarted it -
five bursts in two minutes, with the page blank the whole time.

Persisting the last-known payload means the first `/api/channels/{id}/live`
after a start answers from disk in microseconds, and the (now 4x8) warm only
refreshes in the background. The staleness bound still applies: a badge is
never resurrected from an ancient file.
"""
from __future__ import annotations

import json
import time

import pytest

from routers import live as live_router


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(live_router, "_live_cache_path", lambda: tmp_path / "live.json")
    monkeypatch.setattr(live_router, "_live_cache_last_save", 0.0)
    with live_router._LIVE_STATUS_LOCK:
        live_router._LIVE_STATUS_CACHE.clear()
    yield
    with live_router._LIVE_STATUS_LOCK:
        live_router._LIVE_STATUS_CACHE.clear()


def _entry(payload):
    return {"live": [{"platform": "Twitch", "url": "https://twitch.tv/x", "is_live": True}], "channel_id": payload}


def test_a_saved_entry_is_hydrated_with_its_age(monkeypatch):
    payload = _entry("ch_persist")
    live_router._LIVE_STATUS_CACHE["ch_persist"] = (time.monotonic() - 12.0, payload)

    live_router._save_live_status_cache()
    with live_router._LIVE_STATUS_LOCK:
        live_router._LIVE_STATUS_CACHE.clear()

    live_router._load_live_status_cache()

    with live_router._LIVE_STATUS_LOCK:
        ts, loaded = live_router._LIVE_STATUS_CACHE["ch_persist"]
    assert loaded == payload
    age = time.monotonic() - ts
    assert 11.0 <= age <= 15.0, f"age must survive the restart, got {age:.1f}s"


def test_an_ancient_entry_is_not_resurrected():
    payload = _entry("ch_ancient")
    file_age = live_router._LIVE_STATUS_MAX_STALE_SEC + 5.0
    path = live_router._live_cache_path()
    path.write_text(
        json.dumps({"ch_ancient": {"fetched_at": time.time() - file_age, "payload": payload}}),
        encoding="utf-8",
    )

    live_router._load_live_status_cache()

    with live_router._LIVE_STATUS_LOCK:
        assert "ch_ancient" not in live_router._LIVE_STATUS_CACHE, (
            "a badge must never come back from a file older than the staleness bound"
        )


def test_a_broken_file_is_ignored_and_never_raises():
    live_router._live_cache_path().write_text("{not json", encoding="utf-8")

    live_router._load_live_status_cache()  # must not raise

    with live_router._LIVE_STATUS_LOCK:
        assert live_router._LIVE_STATUS_CACHE == {}


def test_saving_is_throttled_but_the_first_write_always_lands():
    payload = _entry("ch_throttle")
    live_router._LIVE_STATUS_CACHE["ch_throttle"] = (time.monotonic(), payload)

    live_router._save_live_status_cache()
    assert live_router._live_cache_path().exists()

    stored = json.loads(live_router._live_cache_path().read_text(encoding="utf-8"))
    assert "ch_throttle" in stored
    assert stored["ch_throttle"]["payload"] == payload


def test_the_warm_burst_does_not_flood_the_platform_apis():
    """39 channels must not become ~117 concurrent fetches again.

    The 8x12 pools did exactly that and the API stopped answering under them;
    4x8 keeps the request path alive while the warm fills in waves.
    """
    import inspect

    src = inspect.getsource(live_router.warm_all_saved_channel_live_status)
    assert "min(len(channels), 4)" in src, "warm channel concurrency must stay bounded"
    assert "min(len(channels) * 2, 8)" in src, "platform concurrency must stay bounded"
