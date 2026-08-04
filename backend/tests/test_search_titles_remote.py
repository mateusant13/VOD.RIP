"""Video-title search pass + remote YouTube channel-search endpoint tests.

The titles pass makes saved-channel uploads searchable before any
transcript/chat exists (the channel index accumulates every upload the panel
has fetched; transcripts are lazy). The remote endpoint searches a saved
channel's YouTube tab for titles the local index never holds (older than the
~100-upload fetch cap).

Standalone: VODRIP_ARCHIVE_DB points at a scratch file before the first
services.archive_db import, so the schema auto-creates there.

Run from backend/: python -m pytest tests/test_search_titles_remote.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="search-titles-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402
from routers.archive import archive_search_remote  # noqa: E402


def _seed() -> None:
    archive_db.upsert_channel_video({
        "platform": "youtube", "video_id": "est1", "channel": "gaveta",
        "title": "VALE DA ESTRANHEZA #5 — o deserto", "kind": "vod",
        "started_at": "2021-03-01T12:00:00+00:00", "duration_sec": 900,
    })
    archive_db.upsert_channel_video({
        "platform": "youtube", "video_id": "acao1", "channel": "gaveta",
        "title": "AÇÃO TOTAL — reagindo ao vídeo", "kind": "vod",
        "started_at": "2022-01-01T12:00:00+00:00", "duration_sec": 1200,
    })
    archive_db.upsert_channel_video({
        "platform": "twitch", "video_id": "other1", "channel": "outro",
        "title": "VALE DA ESTRANHEZA MARATONA", "kind": "vod",
        "started_at": "2023-01-01T12:00:00+00:00", "duration_sec": 300,
    })
    # est1 also has chat — proves the title pass merges with content hits.
    archive_db.insert_messages(
        "youtube", "est1",
        [{"offset_sec": 5.0, "username": "u", "text": "vale da estranheza aqui"}],
    )


def _titles(resp: list[dict]) -> list[dict]:
    return [h for h in resp if h["kind"] == "title"]


def test_titles_pass_finds_channel_upload_without_transcript() -> None:
    _seed()
    resp = archive_db.search("vale da estranheza")
    hits = _titles(resp)
    assert hits, "title hit expected for a matching upload"
    hit = next(h for h in hits if h["video_id"] == "est1")
    assert hit["score"] == 1.0
    assert hit["offset_sec"] == 0
    assert hit["text"] == "VALE DA ESTRANHEZA #5 — o deserto"
    assert hit["channel"] == "gaveta"
    assert hit["date"] == "2021-03-01T12:00:00+00:00"
    assert hit["video_kind"] == "vod"


def test_titles_pass_accent_fold() -> None:
    _seed()
    resp = archive_db.search("acao total")
    assert any(h["video_id"] == "acao1" for h in _titles(resp)), (
        "accent-folded title must match the unaccented query"
    )


def test_titles_pass_partial_coverage_scores_lower() -> None:
    _seed()
    resp = archive_db.search("estranheza maratona")
    est1 = next(h for h in _titles(resp) if h["video_id"] == "est1")
    other1 = next(h for h in _titles(resp) if h["video_id"] == "other1")
    assert est1["score"] < other1["score"]  # est1 matched 1/2 tokens, other1 both
    assert other1["score"] == 1.0


def test_titles_pass_excluded_for_source_chat_and_transcript() -> None:
    _seed()
    for source in ("chat", "transcript"):
        resp = archive_db.search("vale da estranheza", source=source)
        assert not _titles(resp), f"titles must not leak into source={source}"


def test_titles_pass_respects_channel_and_kind_filters() -> None:
    _seed()
    resp = archive_db.search("vale da estranheza", channel="gaveta")
    assert {h["video_id"] for h in _titles(resp)} == {"est1"}
    resp = archive_db.search("vale da estranheza", channel="outro")
    assert {h["video_id"] for h in _titles(resp)} == {"other1"}
    resp = archive_db.search("vale da estranheza", kind="clip")
    assert not _titles(resp), "kind=clip must exclude vod titles"


async def test_titles_pass_hint_scopes_before_matching() -> None:
    from routers.archive import archive_search

    _seed()
    # Leading slug token scopes to that channel (hint pass); the stripped
    # query still matches the title within the scope.
    with patch("deps.settings_mgr") as fake_mgr:
        fake_mgr.get.return_value = SimpleNamespace(archive_smart_enrich=False)
        resp = await archive_search(q="gaveta estranheza", limit=20)
    assert resp["channel_hint"] == "gaveta"
    assert {h["video_id"] for h in _titles(resp["hits"])} == {"est1"}


async def test_remote_search_route_happy_path() -> None:
    fake_items = [{
        "id": "r1", "platform": "YouTube", "title": "VALE DA ESTRANHEZA #1",
        "duration": 492, "duration_string": "8:12",
        "created_at": "2021-02-01T00:00:00+00:00", "views": 100,
        "thumbnail_url": "https://i.ytimg.com/vi/r1/mqdefault.jpg",
        "url": "https://www.youtube.com/watch?v=r1", "channel": "gaveta",
    }]
    with patch("services.youtube_service.search_channel_videos_sync",
               return_value=fake_items) as m:
        with patch("deps.settings_mgr") as fake_mgr:
            fake_mgr.get.return_value = SimpleNamespace(
                archive_smart_enrich=False,
                saved_channels=[{
                    "id": "c1", "displayName": "gaveta",
                    "kickSlug": "", "twitchSlug": "", "youtubeSlug": "gaveta",
                }],
            )
            resp = await archive_search_remote(q="vale da estranheza", channel="gaveta", limit=20)
    assert resp["error"] is None
    assert len(resp["hits"]) == 1
    h = resp["hits"][0]
    assert h["kind"] == "youtube"
    assert h["platform"] == "youtube"
    assert h["video_id"] == "r1"
    assert h["offset_sec"] == 0
    assert h["text"] == "VALE DA ESTRANHEZA #1"
    assert h["duration_string"] == "8:12"
    assert h["date"] == "2021-02-01T00:00:00+00:00"
    m.assert_called_once_with("gaveta", "vale da estranheza", 20)


async def test_remote_search_route_resolves_slug_from_any_platform() -> None:
    # channel param may be the kick/twitch slug of a multi-platform channel.
    with patch("services.youtube_service.search_channel_videos_sync",
               return_value=[]) as m:
        with patch("deps.settings_mgr") as fake_mgr:
            fake_mgr.get.return_value = SimpleNamespace(
                archive_smart_enrich=False,
                saved_channels=[{
                    "id": "c1", "displayName": "gaveta",
                    "kickSlug": "gaveta", "twitchSlug": "", "youtubeSlug": "gaveta",
                }],
            )
            resp = await archive_search_remote(q="x", channel="gaveta", limit=5)
    assert resp["error"] is None
    m.assert_called_once_with("gaveta", "x", 5)


async def test_remote_search_no_youtube_handle_returns_error() -> None:
    with patch("deps.settings_mgr") as fake_mgr:
        fake_mgr.get.return_value = SimpleNamespace(
            archive_smart_enrich=False,
            saved_channels=[{
                "id": "c1", "displayName": "kickonly",
                "kickSlug": "kickonly", "twitchSlug": "", "youtubeSlug": "",
            }],
        )
        resp = await archive_search_remote(q="x", channel="kickonly", limit=20)
    assert resp["hits"] == []
    assert "no YouTube handle" in resp["error"]


async def test_remote_search_unknown_channel_returns_error() -> None:
    with patch("deps.settings_mgr") as fake_mgr:
        fake_mgr.get.return_value = SimpleNamespace(
            archive_smart_enrich=False,
            saved_channels=[{
                "id": "c1", "displayName": "gaveta",
                "kickSlug": "", "twitchSlug": "", "youtubeSlug": "gaveta",
            }],
        )
        resp = await archive_search_remote(q="x", channel="nobody", limit=20)
    assert resp["hits"] == []
    assert resp["error"]
