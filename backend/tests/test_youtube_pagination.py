"""YouTube channel-tab pagination contracts.

Covers list_channel_videos_sync(..., return_has_more=True):
  * has_more False when the flat tab crawl is exhausted before playlistend
  * has_more True when the crawl saturates playlistend (deeper entries exist)
  * playlistend is clamped at YOUTUBE_PLAYLIST_CEILING (1000), keeping
    has_more honest at the bot-wall bound instead of looping forever
  * the returned rows are always capped at `limit`

Run from backend/: python -m pytest tests/test_youtube_pagination.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, ".")

from services import youtube_service as ys  # noqa: E402
from services import ytdlp_guard  # noqa: E402
from services import youtube_session  # noqa: E402


class _FakeYDL:
    def __init__(self, info):
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False, process=True):
        return self._info


@contextmanager
def _fake_guard(info):
    yield _FakeYDL(info)


def _tab_info(n_entries: int) -> dict:
    return {
        "channel_id": "UCx",
        "entries": [
            {
                "id": f"v{i}",
                "title": f"VOD {i}",
                "url": f"https://www.youtube.com/watch?v=v{i}",
                "view_count": 100,
                "duration": 400,
                "upload_date": "20260801",
            }
            for i in range(n_entries)
        ],
    }


def _stub_session(monkeypatch) -> None:
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts: _fake_guard(_tab_info(0)))
    monkeypatch.setattr(youtube_session, "youtube_session_from_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(youtube_session, "ytdlp_extractor_args", lambda s, auto_auth=True: {"youtube": {}})
    monkeypatch.setattr(youtube_session, "apply_ytdlp_cookie_opts", lambda opts, s, auto_auth=True: None)


def _call(channel_ref, limit, entries, playlist="videos", monkeypatch=None, enrich=False):
    assert monkeypatch is not None
    _stub_session(monkeypatch)
    monkeypatch.setattr(
        ytdlp_guard, "guarded_youtube_dl_channel", lambda opts: _fake_guard(_tab_info(entries))
    )
    return ys.list_channel_videos_sync(
        channel_ref, limit, playlist=playlist, enrich=enrich, return_has_more=True
    )


def test_videos_exhausted_before_playlistend_has_more_false(monkeypatch) -> None:
    # 50 entries, playlistend = min(150, 1000) = 150 → crawl finished early.
    rows, has_more = _call("@gaveta", 50, entries=50, monkeypatch=monkeypatch)
    assert len(rows) == 50
    assert has_more is False


def test_videos_saturated_at_playlistend_has_more_true(monkeypatch) -> None:
    # 150 entries == playlistend → deeper entries exist beyond the crawl.
    rows, has_more = _call("@gaveta", 50, entries=150, monkeypatch=monkeypatch)
    assert len(rows) == 50  # rows capped at limit, not playlistend
    assert has_more is True


def test_videos_returned_rows_capped_at_limit(monkeypatch) -> None:
    rows, has_more = _call("@gaveta", 25, entries=400, monkeypatch=monkeypatch)
    assert len(rows) == 25
    assert has_more is True


def test_videos_ceiling_clamps_playlistend(monkeypatch) -> None:
    # limit=400 → playlistend would be 1200, clamped to 1000.
    # 999 entries < 1000 → exhausted; 1000 entries == bound and the request
    # depth is below the ceiling → more exists (the next page crawls the same
    # bound and CAN serve new rows).
    rows, has_more = _call("@gaveta", 400, entries=999, monkeypatch=monkeypatch)
    assert len(rows) == 400
    assert has_more is False

    rows2, has_more2 = _call("@gaveta", 400, entries=1000, monkeypatch=monkeypatch)
    assert len(rows2) == 400
    assert has_more2 is True


def test_videos_saturation_false_at_ceiling_depth(monkeypatch) -> None:
    # Request depth at the ceiling (limit=1000, playlistend clamped at 1000):
    # a deeper ask is clamped to the same crawl and can never serve new rows,
    # so has_more goes False — the walk terminates at the bound instead of
    # looping forever on empty pages.
    rows, has_more = _call("@gaveta", 1000, entries=1000, monkeypatch=monkeypatch)
    assert len(rows) == 1000
    assert has_more is False

    # One below the bound: has_more stays True (deeper serves new rows).
    rows2, has_more2 = _call("@gaveta", 999, entries=1000, monkeypatch=monkeypatch)
    assert len(rows2) == 999
    assert has_more2 is True


def test_videos_windowed_pagination_selects_playlist_items(monkeypatch) -> None:
    """F4 deep-ALL pagination seam: a caller that asks `start>0` selects a
    playlist_items window [start+1, start+playlistend] in the yt-dlp opts;
    start==0 omits the override entirely (previous behavior preserved)."""
    _stub_session(monkeypatch)
    seen: list[dict] = []

    def wintab_info(opts: dict) -> dict:
        seen.append(dict(opts))
        # yt-dlp, given a playlist_items window, returns only that slice's
        # entries; a window with no start override returns the full bound.
        win = opts.get("playlist_items")
        total = list(range(1200))
        if win:
            a, _, b = win.partition("-")
            picked = total[int(a) - 1 : int(b)]
        else:
            picked = total[: ys.YOUTUBE_PLAYLIST_CEILING]
        return {
            "channel_id": "UCx",
            "entries": [
                {
                    "id": f"v{i}",
                    "title": f"VOD {i}",
                    "url": f"https://www.youtube.com/watch?v=v{i}",
                    "view_count": 100,
                    "duration": 400,
                    "upload_date": "20260801",
                }
                for i in picked
            ],
        }

    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts: _fake_guard(wintab_info(opts)))

    # Window 1: start=0 — NO playlist_items override, first ceiling rows.
    w1, has_more1, sat1 = ys.list_channel_videos_sync(
        "@gaveta", 1000, playlist="videos", enrich=False,
        return_has_more=True, return_crawl_saturation=True,
    )
    assert "playlist_items" not in seen[-1], "start=0 must not override playlist_items"
    assert len(w1) == 1000
    assert w1[0]["id"] == "v0"
    assert sat1 is True  # 1200 > 1000 -> more yet

    # Window 2: start=1000 -> playlist_items "1001-2000" (start+1..start+1000).
    w2, has_more2, sat2 = ys.list_channel_videos_sync(
        "@gaveta", 1000, start=1000, playlist="videos", enrich=False,
        return_has_more=True, return_crawl_saturation=True,
    )
    assert seen[-1]["playlist_items"] == f"1001-{1000 + ys.YOUTUBE_PLAYLIST_CEILING}"
    assert [r["id"] for r in w2] == [f"v{i}" for i in range(1000, 1200)]
    assert sat2 is False  # window covered the tail

    # Together the two windows cover all 1200 ids exactly once.
    ids = [r["id"] for r in w1 + w2]
    assert len(ids) == 1200 and len(set(ids)) == 1200
