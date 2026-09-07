"""innertube_original_meta TTL memo — repeat /api/info/video clicks must not re-probe.

The memo exists because the DB persistence path only stores a result when the
language resolves to pt/en, so a video with no caption tracks (language=None)
re-paid the full 3-client InnerTube probe on every click. These tests pin the
seam that makes the repeat click free: the uncached probe is counted, and a
cache hit must mean zero additional probes.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from services import youtube_innertube as yt


@pytest.fixture(autouse=True)
def _cold_memo():
    """Every test starts with an empty cache and ends with one."""
    with yt._ORIGINAL_META_LOCK:
        yt._ORIGINAL_META_CACHE.clear()
    yield
    with yt._ORIGINAL_META_LOCK:
        yt._ORIGINAL_META_CACHE.clear()


@pytest.fixture
def probe(monkeypatch):
    """Stub the network probe and count how many times it ran."""
    calls: list[str] = []

    def _fake(video_id: str, *, read_timeout: float = 4.0):
        calls.append(video_id)
        return {"title": f"title-{video_id}", "language": "pt"}

    monkeypatch.setattr(yt, "_innertube_original_meta_uncached", _fake)
    return calls


@pytest.fixture
def probe_returning(monkeypatch):
    """Stub the probe with a caller-supplied payload, counting calls."""
    calls: list[str] = []

    def _install(payload):
        def _fake(video_id: str, *, read_timeout: float = 4.0):
            calls.append(video_id)
            return payload

        monkeypatch.setattr(yt, "_innertube_original_meta_uncached", _fake)
        return calls

    return _install


def test_second_call_is_served_from_memo_without_network(probe):
    first = yt.innertube_original_meta("dQw4w9WgXcQ")
    second = yt.innertube_original_meta("dQw4w9WgXcQ")

    assert probe == ["dQw4w9WgXcQ"], "repeat click must not re-probe InnerTube"
    assert second == first == {"title": "title-dQw4w9WgXcQ", "language": "pt"}


def test_memo_is_per_video(probe):
    yt.innertube_original_meta("aaaaaaaaaaa")
    yt.innertube_original_meta("bbbbbbbbbbb")
    yt.innertube_original_meta("aaaaaaaaaaa")

    assert probe == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_language_none_result_is_memoized_once(probe_returning):
    """The motivating case: a video with no caption tracks probes to
    language=None and the DB path never persists it, so before the memo every
    click re-paid the full probe. A falsy-ish payload must still be cached."""
    calls = probe_returning({"title": "T", "language": None})

    first = yt.innertube_original_meta("nocapt000001")
    second = yt.innertube_original_meta("nocapt000001")

    assert first == second == {"title": "T", "language": None}
    assert calls == ["nocapt000001"]


def test_none_probe_result_is_memoized_once(probe_returning):
    """A total probe failure (None) is cached too — the hit test looks at the
    (ts, value) entry, not the value, so a dead video cannot storm YouTube."""
    calls = probe_returning(None)

    assert yt.innertube_original_meta("deadvid00001") is None
    assert yt.innertube_original_meta("deadvid00001") is None
    assert calls == ["deadvid00001"]


def test_expired_entry_re_probes(probe):
    vid = "dQw4w9WgXcQ"
    yt.innertube_original_meta(vid)

    with yt._ORIGINAL_META_LOCK:
        ts, payload = yt._ORIGINAL_META_CACHE[vid]
        yt._ORIGINAL_META_CACHE[vid] = (ts - yt._ORIGINAL_META_TTL_SEC - 1, payload)

    yt.innertube_original_meta(vid)
    assert probe == [vid, vid], "stale entry must fall through to a fresh probe"


def test_entry_one_second_inside_ttl_still_hits(probe):
    """TTL boundary: the comparison is `<`, so an entry just inside the window
    is a hit — no off-by-one re-probe storm at the 6h mark."""
    vid = "dQw4w9WgXcQ"
    yt.innertube_original_meta(vid)
    with yt._ORIGINAL_META_LOCK:
        ts, payload = yt._ORIGINAL_META_CACHE[vid]
        yt._ORIGINAL_META_CACHE[vid] = (ts - (yt._ORIGINAL_META_TTL_SEC - 1), payload)

    yt.innertube_original_meta(vid)
    assert probe == [vid]


def test_cap_bounds_the_cache_and_drops_oldest(probe):
    vid = "newest000000"
    old_ts = time.time() - 1000
    keys = [f"fill{i:08d}" for i in range(256)]
    with yt._ORIGINAL_META_LOCK:
        for i, k in enumerate(keys):
            yt._ORIGINAL_META_CACHE[k] = (old_ts + i, {"title": "t", "language": "pt"})

    yt.innertube_original_meta(vid)

    with yt._ORIGINAL_META_LOCK:
        assert len(yt._ORIGINAL_META_CACHE) == 256, "cap must bound the cache"
        assert vid in yt._ORIGINAL_META_CACHE
        assert keys[0] not in yt._ORIGINAL_META_CACHE, "oldest entry must be evicted"
        assert keys[1] in yt._ORIGINAL_META_CACHE


def test_eviction_survives_identical_timestamps(probe):
    """Windows time.time() granularity is ~15 ms, so two stores can share a
    tick. Eviction must compare timestamps only: comparing the stored
    (ts, payload) tuples falls through to the dict when the timestamps tie and
    raises TypeError, failing the whole /api/info/video click."""
    same_ts = time.time() - 500
    with yt._ORIGINAL_META_LOCK:
        for i in range(256):
            yt._ORIGINAL_META_CACHE[f"tie{i:08d}"] = (same_ts, {"title": f"t{i}", "language": "pt"})

    result = yt.innertube_original_meta("tiebreak0000")

    assert result == {"title": "title-tiebreak0000", "language": "pt"}
    with yt._ORIGINAL_META_LOCK:
        assert len(yt._ORIGINAL_META_CACHE) == 256


def test_concurrent_repeat_clicks_agree(probe):
    """The probe runs outside the lock by design (a ~1.4 s network call must
    not block the pool), so a cold stampede may probe more than once — but the
    cache must converge to one entry and every caller gets the same payload."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(yt.innertube_original_meta, ["dQw4w9WgXcQ"] * 4))

    assert all(r == {"title": "title-dQw4w9WgXcQ", "language": "pt"} for r in results)
    assert len(probe) <= 4
    with yt._ORIGINAL_META_LOCK:
        assert len(yt._ORIGINAL_META_CACHE) == 1
