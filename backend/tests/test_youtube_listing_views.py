"""YouTube listing fixes: localized view-count parsing + shorts freshness union.

Covers:
  * parse_abbreviated_view_count — '8.7K', '8,7 mil', '19 mil', '1,2 mi',
    '8.700' (pt thousands), plain ints; None for unparseable text.
  * _install_localized_count_parser — yt-dlp's parse_count learns pt/es
    suffixes + comma decimals (8.7k short no longer ships as 87 views).
  * looks_like_clip_entry — YouTube shorts (content_kind='short') survive the
    clips/Shorts listing filter (regression from the "clip" -> "short"
    classification split).
  * _union_rss_shorts + list_channel_videos_sync(playlist='shorts') — the
    channel atom RSS feed is unioned with the (possibly lagging) /shorts tab;
    newest shorts appear, streams/VODs and member-only entries stay out, and
    ids already in the tab are deduped.

Run from backend/: python -m pytest tests/test_youtube_listing_views.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, ".")

from services import youtube_service as ys  # noqa: E402
from utils import looks_like_clip_entry  # noqa: E402


def test_parse_abbreviated_view_count_forms() -> None:
    cases = {
        "8.7K views": 8700,
        "8,7 mil visualizações": 8700,
        "19 mil": 19000,
        "42 mil": 42000,
        "2,5 mil": 2500,
        "1 mil": 1000,
        "44 mil": 44000,
        "1,7 mil": 1700,
        "7,5 mil": 7500,
        "1,2 mi": 1_200_000,
        "1,2 bilhão": 1_200_000_000,
        "8.700": 8700,
        "8,700": 8700,
        "8700": 8700,
        "1000": 1000,
    }
    for text, expected in cases.items():
        assert ys.parse_abbreviated_view_count(text) == expected, text
    for text in ("", None, "no views", "views", "x"):
        assert ys.parse_abbreviated_view_count(text) is None, text


def test_localized_parse_count_installed(monkeypatch) -> None:
    import yt_dlp.utils
    from yt_dlp.extractor.youtube import _base as yt_base

    orig_utils = yt_dlp.utils.parse_count
    orig_base = yt_base.parse_count
    try:
        ys._LOCALIZED_PARSE_INSTALLED = False
        ys._install_localized_count_parser()
        assert yt_dlp.utils.parse_count("8,7 mil visualizações") == 8700
        assert yt_base.parse_count("8,7 mil visualizações") == 8700
        assert yt_dlp.utils.parse_count("19 mil") == 19000
        # plain ints and yt-dlp's own suffixes keep working
        assert yt_dlp.utils.parse_count("8700") == 8700
        assert yt_dlp.utils.parse_count("8.7K views") == 8700
        # idempotent
        ys._install_localized_count_parser()
        assert yt_dlp.utils.parse_count("1,2 mil") == 1200
    finally:
        yt_dlp.utils.parse_count = orig_utils
        yt_base.parse_count = orig_base
        ys._LOCALIZED_PARSE_INSTALLED = False


def test_looks_like_clip_entry_youtube_shorts_pass() -> None:
    assert looks_like_clip_entry({
        "id": "s1", "platform": "YouTube", "content_kind": "short",
        "url": "https://www.youtube.com/shorts/ysC0MYx1SY0", "duration": 32,
    })
    # long-form YouTube video never passes the clips filter
    assert not looks_like_clip_entry({
        "id": "v1", "platform": "YouTube", "content_kind": "video",
        "url": "https://www.youtube.com/watch?v=1tap3CLaqr8", "duration": 1026,
    })
    # Twitch/Kick clips still pass; their VODs still fail
    assert looks_like_clip_entry({
        "id": "c1", "platform": "Twitch", "content_kind": "clip",
        "url": "https://clips.twitch.tv/RelentlessDarkWren", "duration": 18,
    })
    assert not looks_like_clip_entry({
        "id": "c2", "platform": "Kick", "content_kind": "vod",
        "url": "https://kick.com/x/videos/53ebeb1c-c691-47f9-9fd1-7139384a009a",
        "duration": 3600,
    })


class _FakeYDL:
    def __init__(self, info):
        self._info = info

    def extract_info(self, url, download=False):
        return self._info

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@contextmanager
def _fake_guard(info):
    yield _FakeYDL(info)


def test_union_rss_shorts_merges_newest_dedups_and_filters() -> None:
    tab_rows = [
        {"id": "tabShort", "title": "Old short", "content_kind": "short"},
        {"id": "dupRss", "title": "Already in tab", "content_kind": "short"},
    ]
    rss_rows = [
        {"id": "newShort1", "title": "Brand new short", "created_at": "2026-08-13T14:28:05+00:00", "views": 277},
        {"id": "newShort2", "title": "Second new short", "created_at": "2026-08-12T10:00:00+00:00", "views": 1021},
        {"id": "dupRss", "title": "Already in tab", "created_at": "2026-08-11T00:00:00+00:00", "views": 5},
        {"id": "newStream", "title": "Soloqz stream", "created_at": "2026-08-10T00:00:00+00:00", "views": 1024},
        {"id": "memberOnly", "title": "Members short", "created_at": "2026-08-09T00:00:00+00:00", "views": 1},
    ]

    def probe(vid):
        return {
            "newShort1": {"content_kind": "short", "duration": 45, "availability": None},
            "newShort2": {"content_kind": "short", "duration": 58, "availability": None},
            "newStream": {"content_kind": "stream", "duration": 18922, "availability": None},
            "memberOnly": {"content_kind": "short", "duration": 30, "availability": "subscriber_only"},
        }.get(vid)

    orig_fetch = ys._fetch_youtube_rss_rows
    ys._fetch_youtube_rss_rows = lambda cid: rss_rows  # type: ignore[assignment]
    try:
        merged = ys._union_rss_shorts(tab_rows, "UCfbFx_dj1RXW0lBATXsI43A", probe)
    finally:
        ys._fetch_youtube_rss_rows = orig_fetch

    ids = [r["id"] for r in merged]
    assert ids == ["tabShort", "dupRss", "newShort1", "newShort2"]  # union appends; caller sorts
    assert ids.count("dupRss") == 1, "tab ids must not be duplicated by the union"
    assert "newStream" not in ids, "streams must never pollute the shorts listing"
    assert "memberOnly" not in ids, "member-only entries must stay filtered"
    added = [r for r in merged if r["id"] == "newShort1"][0]
    assert added["views"] == 277 and added["created_at"].startswith("2026-08-13")
    assert added["content_kind"] == "short" and added["url"].endswith("/shorts/newShort1")


def test_union_rss_shorts_respects_probe_budget() -> None:
    orig_fetch = ys._fetch_youtube_rss_rows
    ys._fetch_youtube_rss_rows = lambda cid: [  # type: ignore[assignment]
        {"id": f"n{i}", "title": f"new {i}", "created_at": f"2026-08-{10 - i}T00:00:00+00:00", "views": 1}
        for i in range(5)
    ]
    probed = []

    def probe(vid):
        probed.append(vid)
        return {"content_kind": "short", "duration": 30, "availability": None}

    try:
        merged = ys._union_rss_shorts(
            [{"id": "tabShort", "title": "old", "content_kind": "short"}],
            "UCx",
            probe,
            budget=2,
        )
    finally:
        ys._fetch_youtube_rss_rows = orig_fetch

    assert len(probed) == 2 and merged[-1]["id"] == "n1"


def test_list_channel_videos_sync_shorts_union_integration(monkeypatch) -> None:
    """End-to-end (stubbed yt-dlp): newest RSS short surfaces first with exact views."""
    from services import ytdlp_guard
    from services import youtube_session

    tab_info = {
        "channel_id": "UCfbFx_dj1RXW0lBATXsI43A",
        "entries": [
            {"id": "tabShort", "title": "Old short", "url": "https://www.youtube.com/shorts/tabShort",
             "view_count": 8700, "duration": 32},
            {"id": "tabVideo", "title": "Tab video", "url": "https://www.youtube.com/watch?v=tabVideo",
             "view_count": 100, "duration": 400},
        ],
    }
    rss_rows = [
        {"id": "newShort1", "title": "Brand new short", "created_at": "2026-08-13T14:28:05+00:00", "views": 277},
        {"id": "newStream", "title": "Soloqz stream", "created_at": "2026-08-12T10:00:00+00:00", "views": 1024},
    ]

    def fake_probe(vid):
        return {
            "newShort1": {"content_kind": "short", "duration": 45, "availability": None},
            "newStream": {"content_kind": "stream", "duration": 18922, "availability": None},
        }.get(vid)

    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts: _fake_guard(tab_info))
    monkeypatch.setattr(youtube_session, "youtube_session_from_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(youtube_session, "ytdlp_extractor_args", lambda s, auto_auth=True: {"youtube": {}})
    monkeypatch.setattr(youtube_session, "apply_ytdlp_cookie_opts", lambda opts, s, auto_auth=True: None)
    monkeypatch.setattr(ys, "_fetch_youtube_rss_rows", lambda cid: rss_rows)
    monkeypatch.setattr(ys, "_make_rss_probe", lambda: fake_probe)

    rows = ys.list_channel_videos_sync("@titiltei", 50, playlist="shorts", enrich=True)
    ids = [r["id"] for r in rows]
    assert ids[0] == "newShort1", ids  # RSS-union short sorts first (newest date)
    assert rows[0]["views"] == 277
    assert rows[0]["created_at"].startswith("2026-08-13")
    assert rows[0]["content_kind"] == "short"
    assert "newStream" not in ids
    assert ids.count("tabShort") == 1 and ids.count("newShort1") == 1
    tab_short = next(r for r in rows if r["id"] == "tabShort")
    assert tab_short["views"] == 8700  # flat-tab int count passes through

    # limit applies after the union (union rows are cut last)
    limited = ys.list_channel_videos_sync("@titiltei", 1, playlist="shorts", enrich=True)
    assert [r["id"] for r in limited] == ["newShort1"]
