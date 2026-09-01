"""Channel VOD index permanence tests — disk-backed accumulation, delta refresh.

The channel view must: accumulate VOD rows forever (never prune), serve
instantly from the archive index after the first fetch, refresh stale
snapshots in the background, and never clobber archive fields (archive_path,
status, canonical_key) that the download pipeline owns.
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from routers import channels
from services import channel_cache
from services.archive_db import (
    channel_snapshot_age_sec,
    get_conn,
    touch_channel_snapshot,
    upsert_channel_video,
    upsert_video,
)


@pytest.fixture(autouse=True)
def _scratch_archive_db(monkeypatch, tmp_path):
    """Route tests use their own archive DB + a cold L1 cache."""
    db = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db))
    with channel_cache._cache._lock:
        channel_cache._cache._cache.clear()
    yield db


@pytest.fixture(autouse=True)
def _fake_platform_services(monkeypatch):
    """Offline fakes for the three platform fetchers + preview warmer."""
    calls: list[str] = []

    def fake_kick(url, limit):
        calls.append("Kick")
        return [{
            "id": f"k{i}",
            "title": f"Kick {i}",
            "duration": 100 + i,
            "duration_string": "0:01:40",
            "created_at": f"2026-08-0{i}T00:00:00Z",
            "views": 5,
            "thumbnail": f"https://kick-thumb/{i}",
        } for i in range(1, 3)]

    def fake_twitch(login, limit, return_has_more=False):
        calls.append("Twitch")
        return [{
            "id": f"t{i}",
            "title": f"Twitch {i}",
            "duration": 200,
            "duration_string": "0:03:20",
            "created_at": "2026-08-02T00:00:00Z",
            "views": 10,
            "thumbnail_url": "https://twitch-thumb/t",
        } for i in range(1, 2)], False

    def fake_youtube(ref, limit, playlist="videos", enrich=True, return_has_more=False):
        calls.append("YouTube")
        items = [{
            "id": f"y{i}",
            "platform": "YouTube",
            "title": f"YT {i}",
            "duration": 300,
            "duration_string": "0:05:00",
            "created_at": "2026-08-01T00:00:00Z",
            "views": 20,
            "thumbnail_url": "https://yt-thumb/y",
            "url": "https://www.youtube.com/watch?v=yy",
            "channel": ref,
            "content_kind": "vod",
        } for i in range(1, 4)]
        if return_has_more:
            return items, False
        return items

    async def no_warm(videos):
        return None

    monkeypatch.setattr(channels, "kick_list_channel_videos_sync", fake_kick)
    monkeypatch.setattr(channels, "twitch_list_channel_videos_sync", fake_twitch)
    monkeypatch.setattr(channels, "youtube_list_channel_videos_sync", fake_youtube)
    monkeypatch.setattr(channels, "_warm_youtube_previews", no_warm)
    return calls


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _videos_url(limit, **extra):
    params = {
        "url": "titiltei",
        "limit": str(limit),
        "days": "0",
        "platforms": "Kick,Twitch,YouTube",
        "content": "vods",
        "kick_slug": "titiltei",
        "twitch_login": "titiltei",
        "youtube_slug": "@titiltei",
    }
    params.update(extra)
    return "/api/channel/videos?" + "&".join(f"{k}={v}" for k, v in params.items())
async def _drain_youtube_warm(expected: int) -> None:
    """Let the fire-and-forget background warm persist its rows.

    The cold-YouTube path returns the (empty) index immediately and fills it
    from a background crawl, so tests must wait for that crawl to land before
    asserting the merged index. YouTube rows are upserted by the bg warm."""
    deadline = asyncio.get_event_loop().time() + 30.0
    while True:
        n = get_conn().execute(
            "SELECT COUNT(*) AS n FROM videos WHERE platform='youtube'"
        ).fetchone()["n"]
        if n >= expected:
            return
        assert asyncio.get_event_loop().time() < deadline, "background warm never completed"
        await asyncio.sleep(0.25)


@pytest.mark.asyncio
async def test_first_fetch_persists_and_second_is_served_from_disk(client, _fake_platform_services, _scratch_archive_db):
    calls = _fake_platform_services

    first = await client.get(_videos_url(100))
    assert first.status_code == 200
    body = first.json()
    # Cold YouTube never blocks the browse: the first response carries the
    # Kick/Twitch items plus refreshing=True while the crawl runs in the bg.
    assert len(body["videos"]) == 3
    assert body["refreshing"] is True
    await _drain_youtube_warm(3)
    assert calls == ["Kick", "Twitch", "YouTube", "YouTube"]

    # Different limit = different L1 cache key; the index must serve it
    # without touching any platform service again.
    second = await client.get(_videos_url(50))
    assert second.status_code == 200
    body2 = second.json()
    assert len(body2["videos"]) == 6
    assert body2["refreshing"] is False
    assert calls == ["Kick", "Twitch", "YouTube", "YouTube"]

    conn = sqlite3.connect(_scratch_archive_db)
    rows = conn.execute("SELECT platform, video_id, status, archive_path FROM videos").fetchall()
    conn.close()
    assert len(rows) == 6
    assert all(status == "known" and archive_path is None for _, _, status, archive_path in rows)


@pytest.mark.asyncio
async def test_force_refetches_even_when_index_exists(client, _fake_platform_services):
    calls = _fake_platform_services

    await client.get(_videos_url(100))
    # Cold YouTube is backgrounded on the first fetch; let it land so the
    # forced fetch sees a warm index and re-fetches all three platforms.
    await _drain_youtube_warm(3)
    forced = await client.get(_videos_url(100, force="1"))
    assert forced.status_code == 200
    body = forced.json()
    assert len(body["videos"]) == 6
    assert body["refreshing"] is False
    assert calls == ["Kick", "Twitch", "YouTube", "YouTube", "Kick", "Twitch", "YouTube", "YouTube"]


@pytest.mark.asyncio
async def test_stale_snapshot_serves_index_and_refreshes_in_background(client, _fake_platform_services, _scratch_archive_db):
    calls = _fake_platform_services

    await client.get(_videos_url(100))
    # Cold YouTube is backgrounded; drain it so the youtube snapshot is fresh
    # before we age everything below (else the bg revisit re-freshens it).
    await _drain_youtube_warm(3)
    assert calls == ["Kick", "Twitch", "YouTube", "YouTube"]

    # Age the snapshots so the next request sees them stale.
    conn = sqlite3.connect(_scratch_archive_db)
    conn.execute("UPDATE channel_snapshots SET fetched_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    assert all(
        channel_snapshot_age_sec(p, c) is not None and channel_snapshot_age_sec(p, c) > 1e8
        for p, c in [("kick", "titiltei"), ("twitch", "titiltei"), ("youtube", "@titiltei")]
    )

    # New limit to dodge the L1 cache: index serves instantly, refreshing=True,
    # and the delta fetch runs in the background.
    stale = await client.get(_videos_url(77))
    assert stale.status_code == 200
    body = stale.json()
    assert len(body["videos"]) == 6
    assert body["refreshing"] is True
    # refreshing=True already proves the blocking fetch did NOT run (a
    # blocking fetch would leave bg_set empty and flip refreshing False).
    # The background delta task may legitimately have started its fetches
    # by now — asserting the calls list here would race it.

    # Let the background delta task finish (it also fetches platform
    # language clues, so it is slower than the fake fetch alone — poll
    # instead of assuming a wall-clock budget).
    deadline = asyncio.get_event_loop().time() + 30.0
    while True:
        await asyncio.sleep(0.25)
        fresh = await client.get(_videos_url(88))
        if fresh.json()["refreshing"] is False:
            break
        assert asyncio.get_event_loop().time() < deadline, "background refresh never completed"
    assert calls == ["Kick", "Twitch", "YouTube", "YouTube", "Kick", "Twitch", "YouTube", "YouTube"]

    # After the background merge the snapshot is fresh again.
    assert fresh.json()["refreshing"] is False


@pytest.mark.asyncio
async def test_route_upsert_never_clobbers_archive_fields(client, _fake_platform_services, _scratch_archive_db):
    # A row owned by the download pipeline: has a file on disk, marked ready.
    upsert_video({
        "platform": "kick",
        "video_id": "k1",
        "channel": "titiltei",
        "title": "Old title",
        "kind": "vod",
        "started_at": "2026-08-01T00:00:00Z",
        "archive_path": "C:/vods/k1.mp4",
        "canonical_key": "k1-canonical",
        "status": "ready",
    })

    forced = await client.get(_videos_url(100, force="1"))
    assert forced.status_code == 200

    row = get_conn().execute(
        "SELECT title, archive_path, canonical_key, status FROM videos WHERE platform='kick' AND video_id='k1'"
    ).fetchone()
    assert row["title"] == "Kick 1"          # list metadata refreshed
    assert row["archive_path"] == "C:/vods/k1.mp4"
    assert row["canonical_key"] == "k1-canonical"
    assert row["status"] == "ready"          # archive state untouched


def test_snapshot_helper_roundtrip():
    touch_channel_snapshot("kick", "titiltei")
    age = channel_snapshot_age_sec("kick", "titiltei")
    assert age is not None and age < 5.0
    assert channel_snapshot_age_sec("kick", "nobody") is None


def test_merge_youtube_playlists_streams_win_on_id():
    vods = [{"id": "a", "content_kind": "vod"}, {"id": "b", "content_kind": "vod"}]
    streams = [
        {"id": "s", "content_kind": "stream"},
        # same video id as a /videos row — the streams-tab classification
        # (was_live) must win over the flat /videos row's 'vod'
        {"id": "a", "content_kind": "stream"},
    ]
    merged = channels.merge_youtube_playlists(vods, streams)
    by_id = {v["id"]: v for v in merged}
    assert set(by_id) == {"a", "b", "s"}
    assert by_id["a"]["content_kind"] == "stream"
    assert by_id["b"]["content_kind"] == "vod"
    assert by_id["s"]["content_kind"] == "stream"


@pytest.mark.asyncio
async def test_vods_fetch_merges_streams_tab_and_persists_stream_kind(
    client, _scratch_archive_db, monkeypatch
):
    """The /videos fetch must also pull the /streams tab so recorded
    broadcasts show in the channel panel, and persist them with kind
    'stream' (not flattened to 'vod')."""
    def playlist_aware(ref, limit, playlist="videos", enrich=True, return_has_more=False):
        if playlist == "streams":
            items = [{
                "id": "s1", "platform": "YouTube", "title": "Stream 1",
                "duration": 14814, "duration_string": "4:06:54",
                "created_at": "2026-07-01T00:00:00Z", "views": 744,
                "thumbnail_url": "https://yt-thumb/s1",
                "url": "https://www.youtube.com/watch?v=s1", "channel": ref,
                "content_kind": "stream",
            }]
        else:
            items = [{
                "id": "u1", "platform": "YouTube", "title": "Upload 1",
                "duration": 300, "duration_string": "0:05:00",
                "created_at": "2026-08-01T00:00:00Z", "views": 20,
                "thumbnail_url": "https://yt-thumb/u1",
                "url": "https://www.youtube.com/watch?v=u1", "channel": ref,
                "content_kind": "vod",
            }]
        if return_has_more:
            return items, False
        return items

    monkeypatch.setattr(channels, "youtube_list_channel_videos_sync", playlist_aware)

    resp = await client.get(_videos_url(100, platforms="YouTube"))
    assert resp.status_code == 200
    body = resp.json()
    # YouTube is cold → the /videos + /streams crawl runs in the background;
    # the first response is the empty index + refreshing=True.
    assert body["videos"] == []
    assert body["refreshing"] is True
    await _drain_youtube_warm(2)
    # A warm (indexed) fetch now serves the merged /videos + /streams list.
    warm = await client.get(_videos_url(50, platforms="YouTube"))
    assert warm.status_code == 200
    kinds = {v["id"]: v["content_kind"] for v in warm.json()["videos"]}
    assert kinds == {"u1": "vod", "s1": "stream"}

    row = get_conn().execute(
        "SELECT kind, duration_sec FROM videos WHERE platform='youtube' AND video_id='s1'"
    ).fetchone()
    assert row is not None
    assert row["kind"] == "stream"
    assert row["duration_sec"] == 14814


@pytest.mark.asyncio
async def test_vods_excludes_watchdog_synthetic_rows(
    client, _fake_platform_services, _scratch_archive_db
):
    """Watchdog chat-capture rows use synthetic ids (<platform>-live-<slug>-<ms>)
    with no real video behind them — the channel API must never return them
    (they used to leak dead watch URLs into the UI: WS-5)."""
    upsert_channel_video({
        "platform": "twitch",
        "video_id": "1234567890",
        "channel": "titiltei",
        "title": "Real VOD",
        "kind": "vod",
        "started_at": "2026-08-01T00:00:00Z",
    })
    upsert_video({
        "platform": "twitch",
        "video_id": "twitch-live-titiltei-1785788650977",
        "channel": "titiltei",
        "title": "Twitch live capture",
        "kind": "live",
        "started_at": "2026-08-01T00:00:00Z",
    })
    upsert_video({
        "platform": "kick",
        "video_id": "kick-live-titiltei-1785788650972",
        "channel": "titiltei",
        "title": "Kick live capture",
        "kind": "live",
        "started_at": "2026-08-01T00:00:00Z",
    })

    # Twitch-only: the index already holds a real row, so the response is
    # served from the accumulated index (no blocking fetch).
    resp = await client.get(_videos_url(50, platforms="Twitch"))
    assert resp.status_code == 200
    ids = [v["id"] for v in resp.json()["videos"]]
    assert "1234567890" in ids
    assert "twitch-live-titiltei-1785788650977" not in ids

    # All platforms: the Kick synthetic row must not leak either (Kick has no
    # real index rows, so the fake fetcher supplies the real ones).
    resp_all = await client.get(_videos_url(50))
    assert resp_all.status_code == 200
    ids_all = [v["id"] for v in resp_all.json()["videos"]]
    assert not any(vid.startswith(("twitch-live-", "kick-live-", "youtube-live-")) for vid in ids_all)
    assert "kick-live-titiltei-1785788650972" not in ids_all

    # Regression guard: synthetic rows stay STORED (chat history) — only the
    # API responses exclude them.
    stored = get_conn().execute(
        "SELECT COUNT(*) AS n FROM videos WHERE video_id LIKE '%-live-%'"
    ).fetchone()
    assert stored["n"] == 2
