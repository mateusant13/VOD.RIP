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

    def fake_twitch(login, limit):
        calls.append("Twitch")
        return [{
            "id": f"t{i}",
            "title": f"Twitch {i}",
            "duration": 200,
            "duration_string": "0:03:20",
            "created_at": "2026-08-02T00:00:00Z",
            "views": 10,
            "thumbnail_url": "https://twitch-thumb/t",
        } for i in range(1, 2)]

    def fake_youtube(ref, limit, playlist="videos", enrich=True):
        calls.append("YouTube")
        return [{
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


@pytest.mark.asyncio
async def test_first_fetch_persists_and_second_is_served_from_disk(client, _fake_platform_services, _scratch_archive_db):
    calls = _fake_platform_services

    first = await client.get(_videos_url(100))
    assert first.status_code == 200
    body = first.json()
    assert len(body["videos"]) == 6
    assert body["refreshing"] is False
    assert calls == ["Kick", "Twitch", "YouTube"]

    # Different limit = different L1 cache key; the index must serve it
    # without touching any platform service again.
    second = await client.get(_videos_url(50))
    assert second.status_code == 200
    body2 = second.json()
    assert len(body2["videos"]) == 6
    assert body2["refreshing"] is False
    assert calls == ["Kick", "Twitch", "YouTube"]

    conn = sqlite3.connect(_scratch_archive_db)
    rows = conn.execute("SELECT platform, video_id, status, archive_path FROM videos").fetchall()
    conn.close()
    assert len(rows) == 6
    assert all(status == "known" and archive_path is None for _, _, status, archive_path in rows)


@pytest.mark.asyncio
async def test_force_refetches_even_when_index_exists(client, _fake_platform_services):
    calls = _fake_platform_services

    await client.get(_videos_url(100))
    forced = await client.get(_videos_url(100, force="1"))
    assert forced.status_code == 200
    body = forced.json()
    assert len(body["videos"]) == 6
    assert body["refreshing"] is False
    assert calls == ["Kick", "Twitch", "YouTube", "Kick", "Twitch", "YouTube"]


@pytest.mark.asyncio
async def test_stale_snapshot_serves_index_and_refreshes_in_background(client, _fake_platform_services, _scratch_archive_db):
    calls = _fake_platform_services

    await client.get(_videos_url(100))
    assert calls == ["Kick", "Twitch", "YouTube"]

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
    # Blocking fetch did NOT run — the response was served from the index.
    assert calls == ["Kick", "Twitch", "YouTube"]

    await asyncio.sleep(0.8)  # let the background delta task finish
    assert calls == ["Kick", "Twitch", "YouTube", "Kick", "Twitch", "YouTube"]

    # After the background merge the snapshot is fresh again.
    fresh = await client.get(_videos_url(88))
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
