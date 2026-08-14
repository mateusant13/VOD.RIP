"""Kick v2 channel-list depth contracts (offline, _get_json faked).

Kick's v2 list endpoints are single-shot (no cursor): the service truncates
one API response at the REQUESTED depth, so show-more pages re-ask deeper
and the router's len>=fetch_limit rule decides exhaustion. These tests pin
that depth-truncation:

  * list_channel_videos_api — returns up to the requested depth (the old
    default-truncated-at-limit behavior kept the channel list unpaginatable
    past the first fetch), clamped at KICK_VIDEOS_CEILING.
  * list_channel_clips_api — returns up to the requested depth (was hard
    capped at 10, which made has_more unreachable for kick clips), clamped
    at KICK_CLIPS_CEILING.

Run from backend/: python -m pytest tests/test_kick_pagination.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from services import kick_api_service as k  # noqa: E402


def _video_items(n: int) -> list[dict]:
    return [{
        "is_live": False,
        "video": {"uuid": f"v{i:03d}"},
        "session_title": f"VOD {i}",
        "duration": 3600000,
        "thumbnail": f"https://thumb/v{i}",
        "views": 10,
        "created_at": f"2026-08-01T00:{i % 60:02d}:00Z",
    } for i in range(n)]


def _clip_items(n: int) -> list[dict]:
    return [{
        "id": f"c{i:03d}",
        "title": f"Clip {i}",
        "duration": 20,
        "views": 5,
        "thumbnail_url": f"https://thumb/c{i}",
        "created_at": f"2026-08-01T00:{i % 60:02d}:{i // 60:02d}Z",
    } for i in range(n)]


def _fake_get_json(monkeypatch, payload) -> None:
    monkeypatch.setattr(k, "_get_json", lambda path, referer: payload)


def test_videos_api_returns_requested_depth(monkeypatch) -> None:
    # 50-row channel list; limit 35 → 35 rows (depth, not the old 20/limit).
    _fake_get_json(monkeypatch, _video_items(50))
    vids = k.list_channel_videos_api("gaveta", 35)
    assert len(vids) == 35
    assert [v.id for v in vids] == [f"v{i:03d}" for i in range(35)]


def test_videos_api_exhausts_when_pool_shorter_than_depth(monkeypatch) -> None:
    _fake_get_json(monkeypatch, _video_items(7))
    vids = k.list_channel_videos_api("gaveta", 35)
    assert len(vids) == 7


def test_videos_api_clamps_at_ceiling(monkeypatch) -> None:
    # 700-row channel: a deeper ask must stop at KICK_VIDEOS_CEILING, not
    # loop or grow unbounded (the router turns has_more off at this bound).
    _fake_get_json(monkeypatch, _video_items(700))
    vids = k.list_channel_videos_api("gaveta", 700)
    assert len(vids) == k.KICK_VIDEOS_CEILING == 500


def test_clips_api_returns_requested_depth_not_10(monkeypatch) -> None:
    # The old `min(limit, 10)` cap made kick clips unpaginatable — a 30-row
    # list with limit 25 must come back 25 rows (newest first).
    _fake_get_json(monkeypatch, {"clips": _clip_items(30)})
    clips = k.list_channel_clips_api("gaveta", 25)
    assert len(clips) == 25
    assert [c.id for c in clips] == [f"c{i:03d}" for i in range(29, 4, -1)]


def test_clips_api_exhausts_when_pool_shorter_than_depth(monkeypatch) -> None:
    _fake_get_json(monkeypatch, {"clips": _clip_items(9)})
    clips = k.list_channel_clips_api("gaveta", 25)
    assert len(clips) == 9


def test_clips_api_clamps_at_ceiling(monkeypatch) -> None:
    _fake_get_json(monkeypatch, {"clips": _clip_items(600)})
    clips = k.list_channel_clips_api("gaveta", 600)
    assert len(clips) == k.KICK_CLIPS_CEILING == 500


def test_videos_sync_depth_truncates_via_url(monkeypatch) -> None:
    _fake_get_json(monkeypatch, _video_items(60))
    rows = k.list_channel_videos_sync("https://kick.com/gaveta/videos", 45)
    assert len(rows) == 45
    assert rows[0]["id"] == "v000"
    assert all(r["content_kind"] == "vod" for r in rows)


def test_clips_sync_depth_truncates_via_url(monkeypatch) -> None:
    _fake_get_json(monkeypatch, {"clips": _clip_items(60)})
    rows = k.list_channel_clips_sync("https://kick.com/gaveta/clips", 45, sort="date")
    assert len(rows) == 45
    assert rows[0]["id"] == "c059"  # newest first
    assert all(r["content_kind"] == "clip" for r in rows)
