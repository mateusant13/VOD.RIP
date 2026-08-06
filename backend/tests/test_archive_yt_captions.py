"""YT captions-first slice — /api/settings roundtrip for yt_subtitles_first,
the whisper skip guard (captions present + toggle on -> no model load), and
the caption fetch format fallback (vtt 429 -> json3) with json3/srv3 parsing.

Scratch env only: the shared settings manager is redirected to a temp file,
the archive DB is rebound to a temp DB per module, and whisper never runs
(transcribe_video / _get_model are patched).

Run from backend/: python -m pytest tests/test_archive_yt_captions.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from deps import settings_mgr
from models.schemas import AppSettings
from services import archive_db, archive_transcribe, archive_ytdlp


@pytest.fixture(scope="module", autouse=True)
def _captions_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB (same
    pattern as test_archive_retention) and restore the env after."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="yt-captions-test-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path):
    """Redirect the shared settings manager to a scratch file (no real
    %APPDATA% writes; mirrors test_whisper_model_settings)."""
    original_file = settings_mgr._settings_file
    original_dir = settings_mgr._settings_dir
    scratch_dir = tmp_path / "VOD.RIP"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    settings_mgr._settings_dir = scratch_dir
    settings_mgr._settings_file = scratch_dir / "settings.json"
    settings_mgr._settings = AppSettings()
    yield
    settings_mgr._settings_file = original_file
    settings_mgr._settings_dir = original_dir


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- /api/settings roundtrip ------------------------------------------------

@pytest.mark.asyncio
async def test_yt_subtitles_first_roundtrip(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["yt_subtitles_first"] is True, "default must be captions-first"

    resp = await client.post("/api/settings", json={"yt_subtitles_first": False})
    assert resp.status_code == 200
    assert resp.json()["yt_subtitles_first"] is False

    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["yt_subtitles_first"] is False, "toggle must persist"

    resp = await client.post("/api/settings", json={"yt_subtitles_first": True})
    assert resp.status_code == 200
    assert resp.json()["yt_subtitles_first"] is True


# --- captions-first skip guard ---------------------------------------------

def _seed_job(platform: str, video_id: str) -> str:
    job_id = f"job-{platform}-{video_id}"
    archive_db.enqueue_job(job_id, "transcribe", platform, video_id)
    return job_id


def _job_status(job_id: str) -> str:
    rows = archive_db.query(
        "SELECT status FROM archive_jobs WHERE id = ?", (job_id,))
    return rows[0]["status"] if rows else None


def _caption_rows(_platform: str, video_id: str) -> list[dict]:
    return [{"seg_idx": 0, "text": "Não sei."}]


def test_captions_first_skips_whisper_when_captions_exist(monkeypatch):
    """Toggle on + youtube + non-empty transcripts -> job done, whisper never runs."""
    job_id = _seed_job("youtube", "vid1")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_db, "transcript_for", side_effect=_caption_rows) as tf, \
         patch.object(archive_transcribe, "transcribe_video",
                      side_effect=AssertionError("whisper must not run")) as tv:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        stats = archive_transcribe._process_job({"id": job_id, "platform": "youtube", "video_id": "vid1"})
    assert stats["skipped"] == "captions-first"
    tf.assert_called_once_with("youtube", "vid1")
    tv.assert_not_called()
    assert _job_status(job_id) == "done"


def test_captions_first_toggle_off_runs_whisper(monkeypatch):
    job_id = _seed_job("youtube", "vid2")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_db, "transcript_for", side_effect=_caption_rows), \
         patch.object(archive_transcribe, "transcribe_video",
                      return_value={"segments": 1}) as tv:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=False)
        stats = archive_transcribe._process_job({"id": job_id, "platform": "youtube", "video_id": "vid2"})
    assert "skipped" not in stats
    tv.assert_called_once()
    assert _job_status(job_id) == "done"


def test_captions_first_defaults_on_when_setting_absent(monkeypatch):
    """Old settings objects lack yt_subtitles_first -> treated as True."""
    job_id = _seed_job("youtube", "vid3")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_db, "transcript_for", return_value=[{"seg_idx": 0}]), \
         patch.object(archive_transcribe, "transcribe_video",
                      side_effect=AssertionError("whisper must not run")) as tv:
        mgr.get.return_value = SimpleNamespace()
        stats = archive_transcribe._process_job({"id": job_id, "platform": "youtube", "video_id": "vid3"})
    assert stats["skipped"] == "captions-first"
    tv.assert_not_called()


def test_captions_first_non_youtube_still_transcribes(monkeypatch):
    job_id = _seed_job("twitch", "vid4")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_db, "transcript_for", return_value=[{"seg_idx": 0}]) as tf, \
         patch.object(archive_transcribe, "transcribe_video",
                      return_value={"segments": 1}) as tv:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        archive_transcribe._process_job({"id": job_id, "platform": "twitch", "video_id": "vid4"})
    tf.assert_not_called()
    tv.assert_called_once()
    assert _job_status(job_id) == "done"


def test_captions_first_no_captions_still_transcribes(monkeypatch):
    job_id = _seed_job("youtube", "vid5")
    with patch("deps.settings_mgr") as mgr, \
         patch.object(archive_db, "transcript_for", return_value=[]) as tf, \
         patch.object(archive_transcribe, "transcribe_video",
                      return_value={"segments": 1}) as tv:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=True)
        archive_transcribe._process_job({"id": job_id, "platform": "youtube", "video_id": "vid5"})
    tf.assert_called_once_with("youtube", "vid5")
    tv.assert_called_once()


# --- caption fetch format fallback + parsing -------------------------------

_JSON3 = (
    '{"events": ['
    '{"tStartMs": 18800, "dDurationMs": 7160, "segs": ['
    '{"utf8": "Não ", "tOffsetMs": 0},'
    '{"utf8": "somos ", "tOffsetMs": 346},'
    '{"utf8": "estranhos", "tOffsetMs": 692}]},'
    '{"tStartMs": 25960, "dDurationMs": 3000, "segs": [{"utf8": " "}]}'
    ']}'
)
_SRV3 = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<timedtext format="3"><body>'
    '<p t="18800" d="7160"><s ac="0">Não </s><s t="346" ac="0">somos </s>'
    '<s t="692" ac="0">estranhos</s></p>'
    '<p t="25960" d="3000">Ih.</p>'
    '</body></timedtext>'
)
_INFO = {
    "id": "vid1",
    "automatic_captions": {
        "pt": [
            {"ext": "vtt", "url": "https://cap/vtt"},
            {"ext": "json3", "url": "https://cap/json3"},
        ],
        "en": [{"ext": "srv3", "url": "https://cap/srv3"}],
    },
}


class _Resp:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class _HTTP429(Exception):
    code = 429


class _FakeYdl:
    """url -> bytes payload or exception; records every urlopen call."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def urlopen(self, url: str):
        self.calls.append(url)
        got = self.responses[url]
        if isinstance(got, Exception):
            raise got
        return _Resp(got)


def test_caption_fetch_vtt_429_retries_then_falls_back_to_json3(monkeypatch):
    sleeps = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", sleeps.append)
    ydl = _FakeYdl({
        "https://cap/vtt": _HTTP429(),
        "https://cap/json3": _JSON3.encode("utf-8"),
        "https://cap/srv3": _HTTP429(),
    })
    lang, fmt, data = archive_ytdlp._fetch_caption(ydl, _INFO)
    assert (lang, fmt) == ("pt", "json3"), "429 on vtt must fall through to json3"
    assert ydl.calls.count("https://cap/vtt") == 2, "429 must be retried once"
    assert sleeps == [1.0], "backoff must run between the 429 attempts"
    segs = archive_ytdlp._parse_caption(fmt, data)
    assert segs[0]["start_sec"] == 18.8 and segs[0]["end_sec"] == 25.96
    assert segs[0]["text"] == "Não somos estranhos"


def test_caption_fetch_prefers_vtt_when_it_serves(monkeypatch):
    sleeps = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", sleeps.append)
    ydl = _FakeYdl({"https://cap/vtt": b"WEBVTT\n\n00:00:03.000 --> 00:00:20.470\nOi.\n"})
    lang, fmt, data = archive_ytdlp._fetch_caption(ydl, _INFO)
    assert (lang, fmt) == ("pt", "vtt")
    assert ydl.calls == ["https://cap/vtt"]
    assert sleeps == []
    segs = archive_ytdlp._parse_caption(fmt, data)
    assert segs[0]["text"] == "Oi."


def test_parse_json3_segments():
    segs = archive_ytdlp._parse_json3(_JSON3)
    assert len(segs) == 1, "filler events must be dropped"
    assert segs[0]["text"] == "Não somos estranhos"
    assert segs[0]["words"] == [
        {"word": "Não", "start": 18.8, "end": 19.146},
        {"word": "somos", "start": 19.146, "end": 19.492},
        {"word": "estranhos", "start": 19.492, "end": 25.96},
    ]


def test_parse_srv3_segments():
    segs = archive_ytdlp._parse_srv3(_SRV3)
    assert len(segs) == 2
    assert segs[0]["text"] == "Não somos estranhos"
    assert segs[0]["words"][0] == {"word": "somos", "start": 19.146, "end": 19.492}
    assert segs[1]["text"] == "Ih." and segs[1]["start_sec"] == 25.96


def test_caption_speaker_markers_stripped():
    """YouTube ASR '>>' speaker markers never reach stored transcript text.

    The VTT payload ships them XML-escaped ('&gt;&gt;', '&amp;'); json3/srv3
    fallbacks can carry the marker raw. All three parsers must unescape and
    strip it at segment AND word level, keeping genuine '&' text.
    """
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "&gt;&gt; E aí maranguap.\n\n"
        "00:00:04.000 --> 00:00:07.000\n"
        "olha &gt;&gt; isso, R&amp;B &gt;&gt; e aí?\n"
    )
    segs = archive_ytdlp._parse_vtt(vtt)
    assert [s["text"] for s in segs] == ["E aí maranguap.", "olha isso, R&B e aí?"]

    json3 = (
        '{"events": [{"tStartMs": 1000, "dDurationMs": 3000, "segs": ['
        '{"utf8": ">> ", "tOffsetMs": 0}, {"utf8": "oi", "tOffsetMs": 200}]}]}'
    )
    segs = archive_ytdlp._parse_json3(json3)
    assert len(segs) == 1
    assert segs[0]["text"] == "oi"
    assert segs[0]["words"] == [{"word": "oi", "start": 1.2, "end": 4.0}]

    srv3 = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<timedtext format="3"><body>'
        '<p t="1000" d="3000">&gt;&gt; <s t="200">oi</s></p>'
        "</body></timedtext>"
    )
    segs = archive_ytdlp._parse_srv3(srv3)
    assert len(segs) == 1
    assert segs[0]["text"] == "oi"
    assert segs[0]["words"] == [{"word": "oi", "start": 1.2, "end": 4.0}]
