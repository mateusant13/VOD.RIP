"""Twitch GQL channel-list pagination contracts.

Covers:
  * list_channel_videos_sync(..., return_has_more=True) — cursor pages of
    100; has_more reflects the connection's real hasNextPage at the stop
    point, and the request depth is clamped at TWITCH_VIDEOS_CEILING.
  * list_channel_clips_sync — the return cap is the requested depth (not the
    old 10), non-era crawls stop at `need` or max_pages, and deep era
    windows scale their in-window target with the requested depth.

Run from backend/: python -m pytest tests/test_twitch_gql_pagination.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from services import twitch_gql_service as gql  # noqa: E402


def _video_node(vid: str) -> dict:
    return {
        "id": vid,
        "title": f"VOD {vid}",
        "createdAt": "2026-08-01T00:00:00Z",
        "lengthSeconds": 3600,
        "viewCount": 10,
        "previewThumbnailURL": f"https://thumb/{vid}",
        "language": "pt",
    }


def _videos_page(ids, has_next: bool, cursor: str | None) -> dict:
    return {
        "user": {
            "videos": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "edges": [{"node": _video_node(i)} for i in ids],
            }
        }
    }


def _clip_node(slug: str) -> dict:
    return {
        "slug": slug,
        "id": f"id-{slug}",
        "title": f"Clip {slug}",
        "durationSeconds": 20,
        "createdAt": "2026-08-01T00:00:00Z",
        "viewCount": 5,
        "thumbnailURL": f"https://thumb/{slug}",
        "url": f"https://clips.twitch.tv/{slug}",
    }


def _clips_page(ids, has_next: bool) -> dict:
    return {
        "user": {
            "clips": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": "c"},
                "edges": [{"node": _clip_node(i)} for i in ids],
            }
        }
    }


def test_videos_first_page_has_more_true_when_connection_continues(monkeypatch) -> None:
    calls: list[dict] = []
    pages = [
        _videos_page([str(i) for i in range(100)], has_next=True, cursor="c1"),
        _videos_page([str(i) for i in range(100, 150)], has_next=False, cursor=None),
    ]

    def fake_request(query, variables):
        calls.append(variables)
        return pages.pop(0)

    monkeypatch.setattr(gql, "_gql_request", fake_request)

    items, has_more = gql.list_channel_videos_sync("gaveta", 100, return_has_more=True)
    assert len(items) == 100
    assert has_more is True
    assert calls[0]["after"] is None and calls[0]["first"] == 100

    # Deeper depth walks the cursor inside the SAME call (page 2 continues
    # with the endCursor of page 1) until the depth is met, then reports
    # the connection exhausted.
    calls.clear()
    pages[:] = [
        _videos_page([str(i) for i in range(100)], has_next=True, cursor="c1"),
        _videos_page([str(i) for i in range(100, 150)], has_next=False, cursor=None),
    ]
    items2, has_more2 = gql.list_channel_videos_sync("gaveta", 150, return_has_more=True)
    assert len(items2) == 150
    assert has_more2 is False
    assert calls == [
        {"login": "gaveta", "after": None, "first": 100},
        {"login": "gaveta", "after": "c1", "first": 50},
    ]


def test_videos_last_page_has_more_false() -> None:
    # Exactly one full page with hasNextPage=false → exhausted, not saturated.
    items, has_more = None, None
    orig = gql._gql_request

    def fake_request(query, variables):
        return _videos_page([str(i) for i in range(100)], has_next=False, cursor=None)

    gql._gql_request = fake_request
    try:
        items, has_more = gql.list_channel_videos_sync("gaveta", 100, return_has_more=True)
    finally:
        gql._gql_request = orig
    assert len(items) == 100
    assert has_more is False


def test_videos_saturates_exactly_at_page_boundary() -> None:
    # 200 videos: page 2 hasNextPage=true → more exists even though the depth
    # request was fully served (the +50 tail is one more page away).
    pages = [
        _videos_page([str(i) for i in range(100)], has_next=True, cursor="c1"),
        _videos_page([str(i) for i in range(100, 200)], has_next=True, cursor="c2"),
    ]

    def fake_request(query, variables):
        return pages.pop(0)

    orig = gql._gql_request
    gql._gql_request = fake_request
    try:
        items, has_more = gql.list_channel_videos_sync("gaveta", 200, return_has_more=True)
    finally:
        gql._gql_request = orig
    assert len(items) == 200
    assert has_more is True


def test_videos_ceiling_clamps_request_depth(monkeypatch) -> None:
    seen: list[int] = []

    def fake_request(query, variables):
        seen.append(variables["first"])
        return _videos_page(
            [str(i) for i in range(seen[-1])], has_next=True, cursor="c"
        )

    monkeypatch.setattr(gql, "_gql_request", fake_request)
    items, has_more = gql.list_channel_videos_sync("gaveta", 2500, return_has_more=True)
    assert len(items) == gql.TWITCH_VIDEOS_CEILING
    # Request depth clamped at the ceiling: hasNextPage is still true on the
    # wire, but has_more goes False — a deeper ask is clamped to the same
    # crawl and can never serve new rows, so show-more must terminate here.
    assert has_more is False
    assert all(f == 100 for f in seen)
    assert len(seen) == 10  # exactly the ceiling's pages, never more


def test_clips_non_era_returns_requested_depth_not_10(monkeypatch) -> None:
    pages = [
        _clips_page([f"c{i}" for i in range(100)], has_next=True),
        _clips_page([f"c{i}" for i in range(100, 200)], has_next=False),
    ]

    def fake_persisted(op, hash_, variables):
        assert variables["limit"] == 100
        return pages.pop(0)

    monkeypatch.setattr(gql, "_gql_persisted", fake_persisted)
    clips = gql.list_channel_clips_sync("gaveta", 200, range_label="LAST_WEEK")
    assert len(clips) == 200
    assert clips[0]["id"].startswith("id-c0")


def test_clips_era_window_scales_depth_and_returns_deep_fetch(monkeypatch) -> None:
    """Era crawls (older_than_days>0) must go deep enough for show-more pages:
    with a 300-clip request the in-window target scales past 100 and the whole
    deep fetch is returned (the API layer window-filters + slices)."""
    import time as _time

    now = _time.time()
    pages_served = {"n": 0}

    def fake_persisted(op, hash_, variables):
        pages_served["n"] += 1
        # One huge page with more to come — era crawl should keep going.
        return _clips_page(
            [f"e{i}" for i in range(100)], has_next=True
        )

    monkeypatch.setattr(gql, "_gql_persisted", fake_persisted)
    # in_window_target = max(100, 301) = 301. Each page yields 100
    # in-window clips, so the target is met at 4 pages (400 clips).
    # Everything fetched is returned (deep) for era windows.
    clips = gql.list_channel_clips_sync(
        "gaveta", 300, range_label="LAST_MONTH",
        older_than_days=30, newer_than_days=14,
    )
    assert len(clips) == 400  # 4 pages of 100, in_window_target met
    assert pages_served["n"] == 4
    assert all(c["id"].startswith("id-e") for c in clips)
    _ = now
