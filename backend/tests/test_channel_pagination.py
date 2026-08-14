"""Channel-list pagination end-to-end (router level, offline fakes).

Reproduces the reported bug: a channel with MORE than the first-page rows
(e.g. Gaveta ~103+ videos) must keep serving pages on show-more until the
platform is exhausted — and stop honestly at the platform ceiling instead of
looping forever.

Covers per platform (twitch/kick/youtube) and item type (vods/clips/shorts):
  * first page: has_more=true + page=N, page size rows
  * next pages: append-only slices, ids never repeated across pages
  * last page: has_more=false
  * ceiling stop: has_more=false at _PLATFORM_CEILINGS (no infinite loop)
  * Kick single-shot: page 2 never refetches the API

Run from backend/: python -m pytest tests/test_channel_pagination.py -q -p no:cacheprovider
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from routers import channels
from services import channel_cache


def _vods_pool(prefix: str, total: int, platform: str = "Twitch") -> list[dict]:
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [{
        "id": f"{prefix}{i:05d}",
        "platform": platform,
        "title": f"VOD {prefix}{i}",
        "duration": 3600,
        "duration_string": "1:00:00",
        "created_at": (base + timedelta(minutes=i)).isoformat(),
        "views": 10,
        "thumbnail_url": f"https://thumb/{prefix}{i}",
    } for i in range(total)]


def _clips_pool(prefix: str, total: int, platform: str = "Twitch") -> list[dict]:
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [{
        "id": f"{prefix}{i:05d}",
        "platform": platform,
        "title": f"Clip {prefix}{i}",
        "duration": 20,
        "duration_string": "0:00:20",
        "created_at": (base + timedelta(minutes=i)).isoformat(),
        "views": 5,
        "thumbnail_url": f"https://thumb/{prefix}{i}",
        "url": f"https://clips.twitch.tv/{prefix}{i}",
    } for i in range(total)]


def _shorts_pool(prefix: str, total: int) -> list[dict]:
    """YouTube shorts rows — the clips router path keeps kind 'short' rows."""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [{
        "id": f"{prefix}{i:05d}",
        "platform": "YouTube",
        "title": f"Short {prefix}{i}",
        "duration": 30,
        "duration_string": "0:00:30",
        "created_at": (base + timedelta(minutes=i)).isoformat(),
        "views": 5,
        "thumbnail_url": f"https://thumb/{prefix}{i}",
        "url": f"https://www.youtube.com/shorts/{prefix}{i}",
        "content_kind": "short",
    } for i in range(total)]


@pytest.fixture(autouse=True)
def _scratch_archive_db(monkeypatch, tmp_path):
    db = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db))
    with channel_cache._cache._lock:
        channel_cache._cache._cache.clear()
    yield db


@pytest.fixture(autouse=True)
def _paged_services(monkeypatch):
    """Stateless platform fakes over shared pools + offline warmers.

    Pools hold 1030/130 rows and the fakes mirror the real services:
      * Twitch videos — newest `limit` rows; has_more = connection continues
        (pageInfo.hasNextPage), false once the request depth is clamped at
        TWITCH_VIDEOS_CEILING (the router stops at the bound).
      * YouTube — the flat tab crawl takes min(limit*3, 1000) entries
        (playlistend) and returns <=limit rows; has_more = saturated AND the
        depth is below YOUTUBE_PLAYLIST_CEILING.
      * Kick — single-shot (no cursor): newest `limit` rows, no explicit
        has_more; the router's len>=fetch_limit rule handles exhaustion.
    """
    pools: dict[str, list[dict]] = {
        "twitch_vods": _vods_pool("tv", 1030, "Twitch"),
        "kick_vods": _vods_pool("kv", 120, "Kick"),
        "youtube_vods": _vods_pool("yv", 1030, "YouTube"),
        "youtube_shorts": _shorts_pool("ys", 1030),
        "twitch_clips": _clips_pool("tc", 130, "Twitch"),
        "kick_clips": _clips_pool("kc", 130, "Kick"),
    }
    calls: list[str] = []

    def fake_twitch_videos(login, limit, return_has_more=False):
        calls.append("twitch_vods")
        pool = pools["twitch_vods"]
        out = pool[-limit:]
        if return_has_more:
            return out, len(pool) > limit and limit < channels.TWITCH_VIDEOS_CEILING
        return out

    def fake_twitch_clips(login, limit, **kw):
        calls.append("twitch_clips")
        return pools["twitch_clips"][-limit:]

    def fake_kick_videos(url, limit):
        calls.append("kick_vods")
        return list(pools["kick_vods"])[-limit:]

    def fake_kick_clips(url, limit, **kw):
        calls.append("kick_clips")
        return list(pools["kick_clips"])[-limit:]

    def fake_youtube(ref, limit, playlist="videos", enrich=True, return_has_more=False):
        calls.append("youtube")
        pool = pools["youtube_shorts"] if playlist == "shorts" else pools["youtube_vods"]
        limit = int(limit)
        # Flat crawl: min(limit*3, ceiling) entries; rows capped at limit.
        crawl = min(len(pool), limit * 3, channels.YOUTUBE_PLAYLIST_CEILING)
        out = pool[-min(limit, crawl):]
        if return_has_more:
            has_more = (
                crawl >= min(limit * 3, channels.YOUTUBE_PLAYLIST_CEILING)
                and limit < channels.YOUTUBE_PLAYLIST_CEILING
            )
            return out, has_more
        return out

    async def no_warm(videos):
        return None

    async def no_backfill(channel):
        return None

    monkeypatch.setattr(channels, "twitch_list_channel_videos_sync", fake_twitch_videos)
    monkeypatch.setattr(channels, "twitch_list_channel_clips_sync", fake_twitch_clips)
    monkeypatch.setattr(channels, "kick_list_channel_videos_sync", fake_kick_videos)
    monkeypatch.setattr(channels, "kick_list_channel_clips_sync", fake_kick_clips)
    monkeypatch.setattr(channels, "youtube_list_channel_videos_sync", fake_youtube)
    monkeypatch.setattr(channels, "innertube_channel_language", lambda ids: None)
    monkeypatch.setattr(channels, "_warm_youtube_previews", no_warm)
    monkeypatch.setattr(channels, "_run_original_backfill", no_backfill)
    return calls, pools


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _videos_url(limit, page, platforms, **extra):
    params = {
        "url": "gaveta",
        "limit": str(limit),
        "page": str(page),
        "days": "0",
        "platforms": platforms,
        "content": "vods",
        "kick_slug": "gaveta",
        "twitch_login": "gaveta",
        "youtube_slug": "@gaveta",
    }
    params.update(extra)
    return "/api/channel/videos?" + "&".join(f"{k}={v}" for k, v in params.items())


def _clips_url(limit, page, platforms, **extra):
    params = {
        "url": "gaveta",
        "limit": str(limit),
        "page": str(page),
        "days": "0",
        "platforms": platforms,
        "kick_slug": "gaveta",
        "twitch_login": "gaveta",
        "youtube_slug": "@gaveta",
    }
    params.update(extra)
    return "/api/channel/clips?" + "&".join(f"{k}={v}" for k, v in params.items())


async def _walk(client, url, limit, key="videos"):
    """Fetch pages until has_more goes False; return (ids, page_meta)."""
    ids: list[str] = []
    page = 1
    pages_with_more = 0
    last = None
    while True:
        resp = await client.get(url(limit, page))
        assert resp.status_code == 200
        last = resp.json()
        batch = [v["id"] for v in last[key]]
        ids.extend(batch)
        assert last["page"] == page
        if last["has_more"]:
            pages_with_more += 1
        if not last["has_more"] or not batch:
            break
        page += 1
        # Shorts walks with a 25-row page size need 40 pages to the 1000
        # ceiling — 60 is the runaway guard, far past any honest walk.
        assert page <= 60, "pagination did not terminate"
    return ids, pages_with_more, last


@pytest.mark.asyncio
async def test_vods_twitch_first_page_has_more_and_next_pages_append(client, _paged_services):
    # First page — the exact shape that used to hard-cap at ~100/103.
    first = await client.get(_videos_url(100, 1, "Twitch"))
    assert first.status_code == 200
    body = first.json()
    assert len(body["videos"]) == 100
    assert body["has_more"] is True
    assert body["page"] == 1

    # Page 2 appends the next 100, no id repeats.
    second = await client.get(_videos_url(100, 2, "Twitch"))
    body2 = second.json()
    assert len(body2["videos"]) == 100
    assert body2["has_more"] is True
    first_ids = {v["id"] for v in body["videos"]}
    assert not (first_ids & {v["id"] for v in body2["videos"]})


@pytest.mark.asyncio
async def test_vods_twitch_walk_until_ceiling_stops(client, _paged_services):
    """1030 real videos, 100/page → pages 1..9 has_more=true, page 10 serves
    the final 100 with has_more=false at the 1000 ceiling — no infinite loop."""
    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _videos_url(l, p, "Twitch"), 100
    )
    assert pages_with_more == 9
    assert last["has_more"] is False
    assert len(ids) == 1000
    assert len(set(ids)) == 1000, "ids must never repeat across pages"


@pytest.mark.asyncio
async def test_vods_youtube_walk_until_ceiling_stops(client, _paged_services):
    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _videos_url(l, p, "YouTube"), 100
    )
    assert pages_with_more == 9
    assert last["has_more"] is False
    assert len(ids) == 1000 and len(set(ids)) == 1000


@pytest.mark.asyncio
async def test_vods_kick_index_grows_on_deeper_fetch(client, _paged_services):
    """Kick's v2 API is single-shot (no cursor): the service returns the
    newest fetch_limit rows, so a deeper page re-asks deeper and the index
    grows (the old 'never refetch' plan dead-ended at the first fetch).
    Page 2 serves the pool tail past 100, page 3 is empty + has_more False."""
    calls, _ = _paged_services
    first = await client.get(_videos_url(100, 1, "Kick"))
    assert first.json()["has_more"] is True
    assert len(first.json()["videos"]) == 100
    second = await client.get(_videos_url(100, 2, "Kick"))
    body2 = second.json()
    assert len(body2["videos"]) == 20
    assert body2["has_more"] is False
    third = await client.get(_videos_url(100, 3, "Kick"))
    body3 = third.json()
    assert body3["videos"] == [] and body3["has_more"] is False
    # Depth-truncation: pages 1..3 each re-ask deeper (100 → 200 → 300).
    assert calls.count("kick_vods") == 3


@pytest.mark.asyncio
async def test_vods_twitch_exhausts_before_ceiling(client, _paged_services):
    """250-video channel: page 3 has_more=false once the pool is drained."""
    _, pools = _paged_services
    pools["twitch_vods"] = _vods_pool("tv", 250, "Twitch")
    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _videos_url(l, p, "Twitch"), 100
    )
    assert pages_with_more == 2
    assert last["has_more"] is False
    assert len(ids) == 250


@pytest.mark.asyncio
async def test_clips_twitch_paginate_until_exhausted(client, _paged_services):
    first = await client.get(_clips_url(25, 1, "Twitch"))
    assert first.status_code == 200
    body = first.json()
    assert len(body["clips"]) == 25
    assert body["has_more"] is True
    assert body["page"] == 1

    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _clips_url(l, p, "Twitch"), 25, key="clips"
    )
    assert pages_with_more == 5
    assert last["has_more"] is False
    assert len(ids) == 130 and len(set(ids)) == 130


@pytest.mark.asyncio
async def test_clips_kick_paginate_until_exhausted(client, _paged_services):
    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _clips_url(l, p, "Kick"), 25, key="clips"
    )
    assert pages_with_more == 5
    assert last["has_more"] is False
    assert len(ids) == 130 and len(set(ids)) == 130


@pytest.mark.asyncio
async def test_shorts_paginate_until_exhausted(client, _paged_services):
    """YouTube shorts ride the clips endpoint (playlist=shorts inside
    list_channel_videos_sync): the walk must terminate on the same
    YOUTUBE_PLAYLIST_CEILING — 39 pages with more, the 40th serves the final
    rows with has_more=false."""
    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _clips_url(l, p, "YouTube"), 25, key="clips"
    )
    assert pages_with_more == 39
    assert last["has_more"] is False
    assert len(ids) == 1000 and len(set(ids)) == 1000


@pytest.mark.asyncio
async def test_multi_platform_pages_merge_without_duplicates(client, _paged_services):
    """All three platforms at once: pages walk the merged list; identical ids
    across platforms can't occur here, but the same id must never appear
    twice within the walk."""
    ids, pages_with_more, last = await _walk(
        client, lambda l, p: _videos_url(l, p, "Kick,Twitch,YouTube"), 100
    )
    # 1030 twitch + 1030 youtube rows (each ceiling-capped at 1000) + the
    # 120-row single-shot kick list = 2120 servable rows.
    assert pages_with_more == 21
    assert last["has_more"] is False
    assert len(ids) == 2120 and len(set(ids)) == 2120
