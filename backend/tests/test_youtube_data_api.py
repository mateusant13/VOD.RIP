"""Official-API hybrid — YouTube Data API layer (issue #4).

Unit tests (no network): key-present routes to the Data API, key-absent
keeps the unofficial path, quota guard degrades at 80% of the daily quota,
and the ingest/search/captions call sites fall back silently on failure.

Run from backend/: python -m pytest tests/test_youtube_data_api.py
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from models.schemas import AppSettings

# Isolate appdata (quota file + settings) and the archive DB before any
# singleton binds (mirrors conftest).
_TMP = Path(tempfile.mkdtemp(prefix="yt-dataapi-"))
os.environ.setdefault("VODRIP_APP_DATA", str(_TMP / "VOD.RIP"))
os.environ.setdefault("VODRIP_ARCHIVE_DB", str(_TMP / "archive.db"))

from services import youtube_data_api as yda  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_quota_cache():
    """_usage_cache is a module global; without a per-test reset one test's
    charges/degrade-seed leak into the next."""
    yda._usage_cache = None
    yield
    yda._usage_cache = None


class _FakeMgr:
    def __init__(self, settings=None):
        self._s = settings if settings is not None else AppSettings()
        self.saved: list[AppSettings] = []

    def get(self):
        return self._s

    def save(self, s):
        self.saved.append(s)
        self._s = s


def _mgr_with_key(key: str) -> _FakeMgr:
    s = AppSettings()
    s.youtube_data_api_key = key
    return _FakeMgr(s)


def _write_usage(monkeypatch, tmp_path, key, units, day=None):
    """Seed the persisted per-key quota file directly."""
    path = tmp_path / "VOD.RIP" / yda._QUOTA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": day or date.today().isoformat(),
        "keys": {key: units},
    }), encoding="utf-8")
    yda._usage_cache = None


SEARCH_JSON = {"items": [
    {"id": {"videoId": "abc123"}, "snippet": {
        "title": "Vale da Estranheza",
        "publishedAt": "2024-05-11T00:00:00Z",
        "channelTitle": "Cellbit",
        "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/abc123/mqdefault.jpg"}},
    }},
]}

VIDEOS_BATCH_JSON = {"items": [{
    "id": "abc123",
    "contentDetails": {"duration": "PT1H2M3S"},
    "statistics": {"viewCount": "12345"},
}]}

CAPTIONS_LIST_JSON = {"items": [
    {"id": "track-pt", "snippet": {"language": "pt", "trackKind": "asr"}},
    {"id": "track-en", "snippet": {"language": "en", "trackKind": "standard"}},
    {"id": "track-pt-br", "snippet": {"language": "pt-BR", "trackKind": "standard"}},
]}

SRT = (
    "1\n00:00:01,000 --> 00:00:03,500\nOlá mundo!\n\n"
    "2\n00:00:03,500 --> 00:00:05,000\nSegunda linha\n\n"
)


# --- routing + quota guard -------------------------------------------------

def test_available_requires_key(monkeypatch):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(""))
    assert yda.available() is False
    assert yda.degraded() is False
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    assert yda.available() is True


def test_quota_guard_degrades_at_80_percent(monkeypatch, tmp_path):
    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    # 7999 units -> still available; 8000 (80% of 10000) -> degraded.
    _write_usage(monkeypatch, tmp_path, key, yda.DEGRADE_AT - 1)
    assert yda.degraded() is False and yda.available() is True
    _write_usage(monkeypatch, tmp_path, key, yda.DEGRADE_AT)
    assert yda.degraded() is True and yda.available() is False


def test_quota_rolls_over_daily(monkeypatch, tmp_path):
    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    _write_usage(monkeypatch, tmp_path, key, 9000, day="2000-01-01")
    assert yda.quota_used() == 0 and yda.degraded() is False


def test_quota_charges_on_success(monkeypatch, tmp_path):
    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    yda._usage_cache = None
    yda._charge(yda.COST_VIDEOS)
    yda._charge(yda.COST_SEARCH)
    assert yda.quota_used() == 101
    # persisted on disk, survives a cache reset
    yda._usage_cache = None
    assert yda.quota_used() == 101


# --- search -----------------------------------------------------------------

def test_search_routes_to_data_api(monkeypatch, tmp_path):
    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    calls = []

    def _fake_get_json(path, params):
        calls.append((path, dict(params)))
        if path == "/channels":
            return {"items": [{"id": "UCcellbit"}]}
        if path == "/search":
            return SEARCH_JSON
        if path == "/videos":
            return VIDEOS_BATCH_JSON
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(yda, "_http_get_json", _fake_get_json)
    rows = yda.search_videos("@cellbit", "vale da estranheza", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "abc123"
    assert row["title"] == "Vale da Estranheza"
    assert row["duration"] == 3723
    assert row["duration_string"] == "1:02:03"
    assert row["views"] == 12345
    assert row["created_at"] == "2024-05-11T00:00:00Z"
    assert row["channel"] == "Cellbit"
    # search.list (100) + batched videos.list (1) + channels.list (1)
    assert yda.quota_used() == 102
    paths = [p for p, _ in calls]
    assert "/search" in paths and "/videos" in paths and "/channels" in paths
    search_params = dict(calls[[p for p, _ in calls].index("/search")][1])
    assert search_params["channelId"] == "UCcellbit"
    assert search_params["q"] == "vale da estranheza"


def test_search_uc_handle_skips_channels_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    calls = []

    def _fake_get_json(path, params):
        calls.append(path)
        if path == "/search":
            return SEARCH_JSON
        if path == "/videos":
            return VIDEOS_BATCH_JSON
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(yda, "_http_get_json", _fake_get_json)
    yda.search_videos("UCcellbit1234567890abcdef", "x", limit=5)
    assert "/channels" not in calls, "UC id needs no channels.list"
    assert yda.quota_used() == 101  # search + videos only


def test_search_degraded_raises(monkeypatch, tmp_path):
    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    _write_usage(monkeypatch, tmp_path, key, yda.DEGRADE_AT)
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not hit the API")))
    import pytest

    with pytest.raises(RuntimeError):
        yda.search_videos("@cellbit", "x", limit=5)


def test_youtube_service_search_hybrid_falls_back(monkeypatch, tmp_path):
    """youtube_service.search_channel_videos_sync: Data API first, silent
    yt-dlp fallback on failure; [] when the unofficial path also fails."""
    from services import youtube_service as ys

    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403")))

    import services.ytdlp_guard as yg

    def _fake_guard(opts):
        class _Ydl:
            def __enter__(self_):
                return self_

            def __exit__(self_, *exc):
                return False

            def extract_info(self_, *a, **k):
                raise RuntimeError("yt-dlp bot gate")

        return _Ydl()

    monkeypatch.setattr(yg, "guarded_youtube_dl_channel", _fake_guard)
    monkeypatch.setattr(
        "services.youtube_session.youtube_session_from_settings",
        lambda: SimpleNamespace(
            visitor_data=None, po_token=None, cookies_file="", tokens_file="",
            cookies_from_browser="", cookie_file=None, http_session=None,
        ),
    )
    rows = ys.search_channel_videos_sync("@cellbit", "x", limit=5)
    assert rows == []


def test_youtube_service_search_key_absent_keeps_unofficial(monkeypatch):
    """No key -> yt-dlp path runs (Data API must not be touched)."""
    from services import youtube_service as ys

    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(""))
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no key -> no Data API")))

    import services.ytdlp_guard as yg

    def _fake_guard(opts):
        class _Ydl:
            def __enter__(self_):
                return self_

            def __exit__(self_, *exc):
                return False

            def extract_info(self_, *a, **k):
                raise RuntimeError("yt-dlp bot gate")

        return _Ydl()

    monkeypatch.setattr(yg, "guarded_youtube_dl_channel", _fake_guard)
    monkeypatch.setattr(
        "services.youtube_session.youtube_session_from_settings",
        lambda: SimpleNamespace(
            visitor_data=None, po_token=None, cookies_file="", tokens_file="",
            cookies_from_browser="", cookie_file=None, http_session=None,
        ),
    )
    assert ys.search_channel_videos_sync("@cellbit", "x", limit=5) == []


# --- metadata ---------------------------------------------------------------

def test_video_metadata_parses_iso_duration(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda path, params: {"items": [{
        "snippet": {"title": "T", "channelTitle": "C", "publishedAt": "2026-01-01T00:00:00Z"},
        "contentDetails": {"duration": "PT2H3M45S"},
        "statistics": {"viewCount": "99"},
    }]})
    meta = yda.video_metadata("abc123")
    assert meta == {
        "title": "T",
        "channel": "C",
        "started_at": "2026-01-01T00:00:00Z",
        "duration_sec": 2 * 3600 + 3 * 60 + 45,
        "views": 99,
    }
    assert yda.quota_used() == yda.COST_VIDEOS


def test_video_metadata_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda path, params: {"items": []})
    import pytest

    with pytest.raises(RuntimeError):
        yda.video_metadata("gone")


# --- captions ----------------------------------------------------------------

def test_fetch_captions_picks_by_preference(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda path, params: CAPTIONS_LIST_JSON)
    downloads: list[str] = []
    monkeypatch.setattr(yda, "_http_get_raw", lambda path, params: (
        downloads.append(path) or SRT
    ))
    tracks = yda.fetch_captions(
        "abc123",
        prefer=("pt", "pt-br", "en"),
        families=("pt", "en"),
        one_per_family=True,
    )
    # Repo convention (mirrors routers/subtitles._track_key): family first,
    # then EXACT pref code, then manual — so pt-asr beats pt-BR-standard.
    # en is the second family -> one track per family.
    assert [(t[0], t[1]) for t in tracks] == [("pt", "asr"), ("en", "standard")]
    assert len(downloads) == 2
    assert yda.quota_used() == yda.COST_CAPTIONS_LIST + 2 * yda.COST_CAPTIONS_DOWNLOAD


def test_fetch_captions_max_tracks(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda path, params: CAPTIONS_LIST_JSON)
    monkeypatch.setattr(yda, "_http_get_raw", lambda path, params: SRT)
    tracks = yda.fetch_captions(
        "abc123",
        prefer=("pt", "pt-br", "en"),
        families=None,
        one_per_family=False,
        max_tracks=1,
    )
    assert len(tracks) == 1 and tracks[0][0] == "pt"


def test_fetch_captions_list_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403 OAuth required")))
    import pytest

    with pytest.raises(RuntimeError):
        yda.fetch_captions("abc123")


def test_fetch_captions_download_failure_skips_track(monkeypatch, tmp_path):
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda path, params: CAPTIONS_LIST_JSON)

    def _fail_raw(path, params):
        if "track-pt-br" in path:
            raise RuntimeError("HTTP 500")
        return SRT

    monkeypatch.setattr(yda, "_http_get_raw", _fail_raw)
    tracks = yda.fetch_captions("abc123", prefer=("pt", "pt-br", "en"), families=("pt",), one_per_family=False)
    assert [(t[0], t[1]) for t in tracks] == [("pt", "asr")]


def test_parse_srt_segments_shape():
    segs = yda.parse_srt(SRT)
    assert len(segs) == 2
    assert segs[0]["seg_idx"] == 1
    assert segs[0]["start_sec"] == 1 and segs[0]["end_sec"] == 3
    assert segs[0]["text"] == "Olá mundo!"
    assert segs[0]["words"] == []
    assert segs[1]["text"] == "Segunda linha"


# --- ingest call site (archive_ytdlp) ---------------------------------------

def _ytdlp_extract_raising(monkeypatch):
    """Make the unofficial ingest path fail at extraction (bot gate)."""
    import services.archive_ytdlp as ayt

    class _Ydl:
        def __enter__(self_):
            return self_

        def __exit__(self_, *exc):
            return False

        def extract_info(self_, *a, **k):
            raise RuntimeError("yt-dlp bot gate")

    monkeypatch.setattr(ayt, "_guarded_youtube_dl", lambda outdir, **k: _Ydl())


def test_ingest_routes_to_data_api_when_key_present(monkeypatch, tmp_path):
    """Key present -> ingest goes through videos.list + captions; the yt-dlp
    path must NOT run; the archive row + transcript land in the DB."""
    from services import archive_ytdlp as ayt
    from services import archive_db

    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda path, params: (
        {"items": [{
            "snippet": {"title": "Vale da Estranheza", "channelTitle": "Cellbit",
                        "publishedAt": "2024-05-11T00:00:00Z"},
            "contentDetails": {"duration": "PT1H2M3S"},
            "statistics": {"viewCount": "5"},
        }]} if path == "/videos" else CAPTIONS_LIST_JSON
    ))
    monkeypatch.setattr(yda, "_http_get_raw", lambda path, params: SRT)

    def _fail_ydl(*a, **k):
        raise AssertionError("yt-dlp path must not run when the Data API serves")

    monkeypatch.setattr(ayt, "_guarded_youtube_dl", _fail_ydl)

    report = ayt.ingest_video("https://www.youtube.com/watch?v=abc123")
    assert report["video_id"] == "abc123"
    assert report["title"] == "Vale da Estranheza"
    assert report["channel"] == "Cellbit"
    assert report["duration_sec"] == 3723
    # two families (pt + en) x two SRT cues each
    assert report["transcript_segments"] == 4
    assert report["caption_lang"] == "pt"
    assert report["chat"] == "none"  # chat is never fetched here (by design)
    assert archive_db.video_channel("youtube", "abc123") == "Cellbit"
    assert archive_db.has_transcript("youtube", "abc123")


def test_ingest_data_api_failure_falls_back_to_ytdlp(monkeypatch, tmp_path):
    """Data API fails (quota/key) -> the yt-dlp path runs (and its error is
    the one surfaced, proving the fallback was taken)."""
    from services import archive_ytdlp as ayt

    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key("AIza-key"))
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403")))

    def _fail_ydl(*a, **k):
        raise AssertionError("yt-dlp must run after the Data API failure")

    # first branch (data api) must fail BEFORE touching yt-dlp; the yt-dlp
    # branch then hits its own guarded extract failure.
    _ytdlp_extract_raising(monkeypatch)
    import pytest

    with pytest.raises(RuntimeError, match="yt-dlp bot gate"):
        ayt.ingest_video("https://www.youtube.com/watch?v=abc124")


def test_ingest_key_absent_keeps_unofficial(monkeypatch, tmp_path):
    """No key -> the yt-dlp path runs; Data API never called."""
    from services import archive_ytdlp as ayt

    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(""))
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no key -> no Data API")))
    _ytdlp_extract_raising(monkeypatch)
    import pytest

    with pytest.raises(RuntimeError, match="yt-dlp bot gate"):
        ayt.ingest_video("https://www.youtube.com/watch?v=abc125")


def test_ingest_degraded_skips_data_api(monkeypatch, tmp_path):
    """Quota degraded -> ingest skips the official path entirely."""
    from services import archive_ytdlp as ayt

    key = "AIza-key"
    monkeypatch.setattr("deps.settings_mgr", _mgr_with_key(key))
    _write_usage(monkeypatch, tmp_path, key, yda.DEGRADE_AT)
    monkeypatch.setattr(yda, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("degraded -> no Data API")))
    _ytdlp_extract_raising(monkeypatch)
    import pytest

    with pytest.raises(RuntimeError, match="yt-dlp bot gate"):
        ayt.ingest_video("https://www.youtube.com/watch?v=abc126")
