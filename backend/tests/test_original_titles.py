"""WS-4 original-title tests — migration, backfill, display preference.

Covers: fresh + existing-DB migration (PRAGMA asserts), backfill with a
stubbed innertube fetch on a scratch DB, throttle/failure-cooldown behavior,
upsert preserve rules, search display preference, and the WS-3 column
contract (videos.original_language). Stored titles are never modified.
"""
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from routers import channels
from services import channel_cache
from services.archive_db import (
    get_conn,
    insert_transcript,
    search,
    set_original_title,
    upsert_channel_video,
)
from services.archive_ytdlp import backfill_original_titles


@pytest.fixture(autouse=True)
def _scratch_archive_db(monkeypatch, tmp_path):
    """Route tests use their own archive DB + a cold L1 cache."""
    db = tmp_path / "archive.db"
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db))
    with channel_cache._cache._lock:
        channel_cache._cache._cache.clear()
    yield db


@pytest.fixture(autouse=True)
def _fast_backfill(monkeypatch):
    """No inter-fetch sleep in tests; stub the innertube fetch globally."""
    import services.archive_ytdlp as ytdlp_mod
    import services.youtube_innertube as yt_innertube

    monkeypatch.setattr(ytdlp_mod, "_ORIGINAL_MIN_GAP_S", 0.0)
    monkeypatch.setattr(ytdlp_mod, "_ORIGINAL_FAIL_COOLDOWN_S", 3600.0)
    calls: list[str] = []

    def _fake_meta(video_id):
        calls.append(video_id)
        return {"title": f"Titulo Original de {video_id}", "language": "pt"}

    monkeypatch.setattr(yt_innertube, "innertube_original_meta", _fake_meta)
    monkeypatch.setattr(ytdlp_mod, "_original_last_fetch", 0.0)
    monkeypatch.setattr(ytdlp_mod, "_original_failed_at", {})
    yield calls


def _seed_youtube_video(channel="gaveta", video_id="LshkYHjQzXw", title="The END of Physical Media on PlayStation | Gaveta"):
    upsert_channel_video({
        "platform": "youtube",
        "video_id": video_id,
        "channel": channel,
        "title": title,
        "started_at": "2026-07-20T00:00:00Z",
        "duration_sec": 3600,
        "kind": "vod",
    })


# --- migration ---------------------------------------------------------------

def test_fresh_db_has_original_columns():
    cols = {row[1] for row in get_conn().execute("PRAGMA table_info(videos)")}
    assert "original_title" in cols
    # WS-3 contract: the language column name is exactly this.
    assert "original_language" in cols


def test_existing_db_migration_adds_columns_preserving_rows(tmp_path):
    """A pre-WS-4 DB (no original columns) migrates in place, rows intact."""
    old = tmp_path / "old-schema.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE videos (
          platform TEXT NOT NULL,
          video_id TEXT NOT NULL,
          channel TEXT,
          title TEXT,
          started_at TEXT,
          ended_at TEXT,
          duration_sec REAL,
          archive_path TEXT,
          canonical_key TEXT,
          content_sha256 TEXT,
          status TEXT NOT NULL DEFAULT 'known',
          kind TEXT NOT NULL DEFAULT 'vod',
          created_at TEXT,
          updated_at TEXT,
          PRIMARY KEY (platform, video_id)
        );
        INSERT INTO videos (platform, video_id, channel, title, status, kind)
        VALUES ('youtube', 'LshkYHjQzXw', 'gaveta', 'The END of Physical Media', 'known', 'vod');
    """)
    conn.commit()
    from services.archive_db import _ensure_original_columns

    _ensure_original_columns(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    assert "original_title" in cols and "original_language" in cols
    row = conn.execute(
        "SELECT title, original_title, original_language FROM videos WHERE video_id='LshkYHjQzXw'"
    ).fetchone()
    assert row[0] == "The END of Physical Media"
    assert row[1] is None and row[2] is None
    conn.close()


# --- backfill ----------------------------------------------------------------

def test_backfill_stores_original_pt_title(_scratch_archive_db, _fast_backfill):
    _seed_youtube_video()
    report = backfill_original_titles("gaveta", limit=5)
    assert report == {"candidates": 1, "fetched": 1, "skipped": 0, "no_language": 0}
    row = get_conn().execute(
        "SELECT title, original_title, original_language FROM videos WHERE video_id='LshkYHjQzXw'"
    ).fetchone()
    assert row[0] == "The END of Physical Media on PlayStation | Gaveta"  # stored untouched
    assert row[1] == "Titulo Original de LshkYHjQzXw"
    assert row[2] == "pt"


def test_backfill_en_channel_reuses_stored_title(_scratch_archive_db, _fast_backfill):
    import services.archive_ytdlp as ytdlp_mod
    import services.youtube_innertube as yt_innertube

    _seed_youtube_video(video_id="diz-r3wGk3c", title="A VERDADEIRA FORÇA É CONTINUAR | BOGUR")
    yt_innertube.innertube_original_meta = lambda v: {"title": "A VERDADEIRA FORÇA É CONTINUAR | BOGUR", "language": "en"}
    report = backfill_original_titles("gaveta", limit=5)
    assert report["fetched"] == 1
    row = get_conn().execute(
        "SELECT original_title, original_language FROM videos WHERE video_id='diz-r3wGk3c'"
    ).fetchone()
    # Serving lang is pt; the en original == what the walk (hl=en) stored.
    assert row[0] == "A VERDADEIRA FORÇA É CONTINUAR | BOGUR"
    assert row[1] == "en"


def test_backfill_unknown_lang_records_language_only(_scratch_archive_db, _fast_backfill):
    import services.archive_ytdlp as ytdlp_mod
    import services.youtube_innertube as yt_innertube

    _seed_youtube_video(video_id="dQw4w9WgXcQ", title="Rick Astley - Never Gonna Give You Up")
    yt_innertube.innertube_original_meta = lambda v: {"title": "Rick Astley - Never Gonna Give You Up", "language": "de"}
    report = backfill_original_titles("gaveta", limit=5)
    assert report["fetched"] == 1
    row = get_conn().execute(
        "SELECT original_title, original_language FROM videos WHERE video_id='dQw4w9WgXcQ'"
    ).fetchone()
    # Neither the pt player title nor the en walk title is the de original —
    # record the language clue, leave the title alone.
    assert row[0] is None
    assert row[1] == "de"


def test_backfill_no_language_marks_failed_and_skips(_scratch_archive_db, _fast_backfill):
    import services.archive_ytdlp as ytdlp_mod
    import services.youtube_innertube as yt_innertube

    _seed_youtube_video(video_id="aaaaaaaaaaa", title="Stored")
    calls: list[str] = []
    yt_innertube.innertube_original_meta = lambda v: calls.append(v) or {"title": "X", "language": None}
    first = backfill_original_titles("gaveta", limit=5)
    assert first["no_language"] == 1 and first["fetched"] == 0
    second = backfill_original_titles("gaveta", limit=5)
    # Cooldown: the failing video is skipped without another fetch.
    assert second["skipped"] == 1 and second["no_language"] == 0
    assert calls == ["aaaaaaaaaaa"]
    assert ytdlp_mod._original_failed_recently("aaaaaaaaaaa") is True


def test_backfill_skips_non_youtube_ids(_scratch_archive_db, _fast_backfill):
    _seed_youtube_video(video_id="y1", title="Fake")
    report = backfill_original_titles("gaveta", limit=5)
    assert report["skipped"] == 1 and report["fetched"] == 0
    row = get_conn().execute(
        "SELECT original_title FROM videos WHERE video_id='y1'"
    ).fetchone()
    assert row[0] is None


def test_upsert_never_clobbers_backfilled_original(_scratch_archive_db):
    _seed_youtube_video()
    set_original_title("youtube", "LshkYHjQzXw", "O FIM da Mídia Física no Playstation | Gaveta", "pt")
    # A plain channel-walk refresh carries no original fields.
    _seed_youtube_video()
    row = get_conn().execute(
        "SELECT original_title, original_language, title FROM videos WHERE video_id='LshkYHjQzXw'"
    ).fetchone()
    assert row[0] == "O FIM da Mídia Física no Playstation | Gaveta"
    assert row[1] == "pt"
    assert row[2] == "The END of Physical Media on PlayStation | Gaveta"


# --- display preference ------------------------------------------------------

def test_search_title_hit_prefers_original_and_matches_original_tokens(_scratch_archive_db):
    _seed_youtube_video()
    set_original_title("youtube", "LshkYHjQzXw", "O FIM da Mídia Física no Playstation | Gaveta", "pt")
    hits = search("fim")
    title_hits = [h for h in hits if h["kind"] == "title"]
    assert len(title_hits) >= 1
    assert title_hits[0]["title"] == "O FIM da Mídia Física no Playstation | Gaveta"
    assert title_hits[0]["text"] == "O FIM da Mídia Física no Playstation | Gaveta"


def test_search_content_hit_title_prefers_original(_scratch_archive_db):
    _seed_youtube_video()
    set_original_title("youtube", "LshkYHjQzXw", "O FIM da Mídia Física no Playstation | Gaveta", "pt")
    insert_transcript(
        "youtube", "LshkYHjQzXw",
        [{"seg_idx": 0, "start_sec": 1.0, "end_sec": 2.0, "text": "conteudo unico de transcricao aqui"}],
        lang="pt",
    )
    hits = search("conteudo")
    content_hits = [h for h in hits if h["kind"] == "transcript"]
    assert len(content_hits) >= 1
    assert content_hits[0]["title"] == "O FIM da Mídia Física no Playstation | Gaveta"


# --- channel payload ---------------------------------------------------------

@pytest.fixture(autouse=True)
def _fake_platform_services(monkeypatch):
    """Offline fakes for the three platform fetchers + preview warmer."""
    calls: list[str] = []

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

    monkeypatch.setattr(channels, "youtube_list_channel_videos_sync", fake_youtube)
    monkeypatch.setattr(channels, "_warm_youtube_previews", no_warm)
    return calls


@pytest.mark.asyncio
async def test_channel_payload_prefers_original_title(_fake_platform_services, _scratch_archive_db):
    """A fresh walk surfaces the backfilled original (fetched items win the
    merge, so _overlay_original_titles must copy the index original onto them)."""
    params = {
        "url": "gaveta", "limit": "100", "days": "0", "platforms": "YouTube",
        "content": "vods", "youtube_slug": "@gaveta",
    }
    url = "/api/channel/videos?" + "&".join(f"{k}={v}" for k, v in params.items())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        first = await ac.get(url)
        assert first.status_code == 200
        # Simulate a previous sync's backfill on the index row.
        set_original_title("youtube", "y1", "O Titulo Original", "pt")
        second = await ac.get(url + "&force=1")
        items = {v["id"]: v for v in second.json()["videos"]}
        assert items["y1"]["title"] == "O Titulo Original"
        assert items["y1"]["original_title"] == "O Titulo Original"
        assert items["y1"]["original_language"] == "pt"
        # Un-backfilled rows keep their walk title.
        assert items["y2"]["title"] == "YT 2"
