"""Cross-platform transcription skip: a Kick/Twitch VOD whose mirrored live
exists on a higher-priority platform (youtube > twitch > kick) with transcript
rows already gets its whisper job skipped — the same canonical_key rule the
kick download dedupe uses (archive_kick.dedupe_decision).

Also covers the language-aware dedupe half of the same canonical-key family:
search hits carry `platforms` (every platform the canonical VOD exists on),
and transcript rows whose lang family differs from the channel's effective
language are hidden from search when that language is known (channel-language
restricted caption ingest stores only the channel family).

Run from backend/: python -m pytest tests/test_transcribe_cross_platform.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_CROSS_DB = str(Path(tempfile.mkdtemp(prefix="transcribe-cross-")) / "archive.db")
# VODRIP_APP_DATA must point at an EMPTY scratch dir too: get_conn() runs
# _migrate_db_to_data_dir(), which copies the DB from the appdata dir into a
# fresh target path. If a previous module left VODRIP_APP_DATA at a real-data
# copy (test_channel_language seeds its temp with a live-archive copy), the
# 'fresh' scratch DB gets polluted with production rows and limit-cut searches
# (top-N newest-first) stop surfacing this module's fixtures.
_CROSS_APP = str(Path(tempfile.mkdtemp(prefix="transcribe-cross-app-")))
os.environ["VODRIP_ARCHIVE_DB"] = _CROSS_DB
os.environ["VODRIP_APP_DATA"] = _CROSS_APP

from services import archive_db  # noqa: E402  (env must be set before import)
from services import archive_transcribe  # noqa: E402
from services import archive_ytdlp  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _cross_scratch_db():
    # Force THIS module's scratch DB + appdata (not whatever the previous
    # module left in env): get_conn() keys on the env path, so a batch run
    # that imports another module last would otherwise rebind here and leak
    # into it; VODRIP_APP_DATA must stay on the empty scratch dir or the
    # migration copy above pulls foreign data into the scratch DB.
    prev_db = os.environ.get("VODRIP_ARCHIVE_DB")
    prev_app = os.environ.get("VODRIP_APP_DATA")
    os.environ["VODRIP_ARCHIVE_DB"] = _CROSS_DB
    os.environ["VODRIP_APP_DATA"] = _CROSS_APP
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    for var, prev in (("VODRIP_ARCHIVE_DB", prev_db), ("VODRIP_APP_DATA", prev_app)):
        if prev is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def _seed_video(platform: str, video_id: str, key: str) -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": video_id,
        "channel": "titiltei",
        "title": f"{platform} mirror {video_id}",
        "canonical_key": key,
        "started_at": "2026-08-03T17:24:00Z",
        "kind": "vod",
    })


def _seed_transcript(platform: str, video_id: str) -> None:
    archive_db.insert_transcript(
        platform, video_id,
        [{"seg_idx": 0, "text": "crosstalk", "start_sec": 0.0, "end_sec": 2.0}],
    )


def test_kick_skipped_when_youtube_mirror_transcribed():
    _seed_video("kick", "k1", "ck-shared")
    _seed_video("youtube", "y1", "ck-shared")
    _seed_transcript("youtube", "y1")
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k1") is True


def test_kick_skipped_when_twitch_mirror_transcribed():
    _seed_video("kick", "k2", "ck-t2")
    _seed_video("twitch", "t2", "ck-t2")
    _seed_transcript("twitch", "t2")
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k2") is True


def test_higher_priority_never_skipped_by_lower():
    # A youtube row is never blocked by a transcribed kick row (one-way rule).
    _seed_video("kick", "k3", "ck-oneway")
    _seed_video("youtube", "y3", "ck-oneway")
    _seed_transcript("kick", "k3")
    assert archive_db.transcribed_on_higher_priority_platform("youtube", "y3") is False


def test_no_group_or_no_transcript_is_false():
    _seed_video("kick", "k4", "ck-lonely")
    _seed_video("youtube", "y4", "ck-untranscribed")
    _seed_transcript("youtube", "y4")
    # Same key, but nothing transcribed yet on the youtube side.
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k-lonely") is False
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k4") is False
    # Different keys: no group match.
    _seed_video("kick", "k5", "ck-different")
    _seed_transcript("youtube", "y4")
    assert archive_db.transcribed_on_higher_priority_platform("kick", "k5") is False


def test_unknown_platform_is_false():
    assert archive_db.transcribed_on_higher_priority_platform("soundcloud", "s1") is False


# --- worker-level: the job completes done + skipped, whisper never runs ---

def _seed_job(platform: str, video_id: str) -> str:
    job_id = f"job-{platform}-{video_id}"
    archive_db.enqueue_job(job_id, "transcribe", platform, video_id)
    return job_id


def _job_status(job_id: str) -> str:
    rows = archive_db.query("SELECT status FROM archive_jobs WHERE id = ?", (job_id,))
    return rows[0]["status"] if rows else None


def test_process_job_skips_kick_when_mirror_transcribed(monkeypatch):
    _seed_video("kick", "k6", "ck-worker")
    _seed_video("youtube", "y6", "ck-worker")
    _seed_transcript("youtube", "y6")
    job_id = _seed_job("kick", "k6")
    with patch.object(
        archive_transcribe, "transcribe_video",
        side_effect=AssertionError("whisper must not run"),
    ) as tv:
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "kick", "video_id": "k6"}
        )
    assert stats["skipped"] == "dedupe-transcribed"
    tv.assert_not_called()
    assert _job_status(job_id) == "done"


def test_process_job_runs_whisper_when_no_higher_priority_mirror(monkeypatch):
    _seed_video("kick", "k7", "ck-nomirror")
    job_id = _seed_job("kick", "k7")
    # FIX A: a kick row with no local archive file downloads the audio at
    # transcribe time (_transcribe_remote_twitch_kick) instead of failing
    # on the missing file.
    with patch.object(
        archive_transcribe, "_transcribe_remote_twitch_kick",
        return_value={"segments": 1},
    ) as remote:
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "kick", "video_id": "k7"}
        )
    assert "skipped" not in stats
    remote.assert_called_once()
    assert _job_status(job_id) == "done"


def test_process_job_uses_local_file_when_present(monkeypatch):
    """A kick row WITH an archive file keeps the fast local path — the
    remote downloader only fires for file-less rows."""
    media = Path(tempfile.mkdtemp(prefix="tk-local-")) / "k.mp4"
    media.write_bytes(b"fake media")
    archive_db.upsert_video({
        "platform": "kick", "video_id": "k9", "channel": "titiltei",
        "title": "kick local k9", "canonical_key": "ck-local",
        "started_at": "2026-08-03T17:24:00Z", "kind": "vod",
        "status": "ready", "archive_path": str(media),
    })
    job_id = _seed_job("kick", "k9")
    with patch.object(
        archive_transcribe, "transcribe_video",
        return_value={"segments": 1},
    ) as tv, patch.object(
        archive_transcribe, "_transcribe_remote_twitch_kick",
        side_effect=AssertionError("remote download must not run with a file"),
    ):
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "kick", "video_id": "k9"}
        )
    assert "skipped" not in stats
    tv.assert_called_once()
    assert _job_status(job_id) == "done"


def test_process_job_youtube_not_skipped_by_kick_mirror(monkeypatch):
    _seed_video("kick", "k8", "ck-ytjob")
    _seed_video("youtube", "y8", "ck-ytjob")
    _seed_transcript("kick", "k8")
    archive_db.mark_captions_unavailable("youtube", "y8")
    job_id = _seed_job("youtube", "y8")
    # Toggle OFF so the captions-first skip does not preempt the dedupe
    # check: with whisper explicitly allowed on YouTube, the one-way rule
    # must still hold — a lower-priority kick mirror never blocks a YouTube
    # job (the DB-level transcribed_on_higher_priority_platform guard is
    # covered by test_higher_priority_never_skipped_by_lower).
    with patch("deps.settings_mgr") as mgr, \
         patch.object(
        archive_transcribe, "_transcribe_youtube_captionless",
        return_value={"segments": 1},
    ) as tv:
        mgr.get.return_value = SimpleNamespace(yt_subtitles_first=False)
        stats = archive_transcribe._process_job(
            {"id": job_id, "platform": "youtube", "video_id": "y8"}
        )
    assert "skipped" not in stats
    tv.assert_called_once()
    assert _job_status(job_id) == "done"


# --- hit.platforms: every platform the canonical VOD exists on ------------

def test_search_hits_carry_platforms():
    # Two platforms sharing one canonical_key: hits on EITHER side must
    # report both platforms, with `platform` staying the actual source.
    _seed_video("youtube", "p-y", "ck-platforms")
    _seed_video("twitch", "p-t", "ck-platforms")
    archive_db.insert_transcript(
        "youtube", "p-y",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra platform probe"}],
    )
    archive_db.insert_messages(
        "twitch", "p-t",
        [{"offset_sec": 1.0, "username": "u", "text": "zebra platform chat"}],
    )
    tr = next(
        h for h in archive_db.search("zebra platform probe")
        if h["kind"] == "transcript" and h["video_id"] == "p-y"
    )
    assert tr["platform"] == "youtube"
    assert set(tr["platforms"]) == {"youtube", "twitch"}, tr["platforms"]
    msg = next(
        h for h in archive_db.search("zebra platform chat")
        if h["kind"] == "message" and h["video_id"] == "p-t"
    )
    assert msg["platform"] == "twitch"
    assert set(msg["platforms"]) == {"youtube", "twitch"}, msg["platforms"]


def test_search_hits_platforms_alone_video():
    # A video with no dedupe group gets [platform] — never an empty list.
    _seed_video("youtube", "p-solo", "ck-solo")
    archive_db.insert_transcript(
        "youtube", "p-solo",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra solo probe"}],
    )
    tr = next(
        h for h in archive_db.search("zebra solo probe")
        if h["kind"] == "transcript" and h["video_id"] == "p-solo"
    )
    assert tr["platforms"] == ["youtube"]


# --- language-aware transcript search exclusion (non-destructive) ---------

def test_search_excludes_other_family_when_channel_language_known():
    # maranguape-style channel, effective language pt: the en-family rows
    # the old caption ingest stored next to pt must not surface; pt rows and
    # untagged (whisper, no detection) rows stay.
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "lg-pt", "channel": "maranguape",
        "title": "lang mirror", "canonical_key": "ck-lg-pt",
        "started_at": "2026-08-03T17:24:00Z", "kind": "vod",
    })
    archive_db.set_channel_language("youtube", "maranguape", "pt")
    archive_db.insert_transcript(
        "youtube", "lg-pt",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra en row"}],
        lang="en",
    )
    archive_db.insert_transcript(
        "youtube", "lg-pt",
        [{"seg_idx": 1, "start_sec": 1.0, "end_sec": 2.0, "text": "zebra pt row"}],
        lang="pt",
    )
    archive_db.insert_transcript(
        "youtube", "lg-pt",
        [{"seg_idx": 2, "start_sec": 2.0, "end_sec": 3.0, "text": "zebra untagged row"}],
    )
    texts = {
        h["text"] for h in archive_db.search("zebra")
        if h["kind"] == "transcript" and h["video_id"] == "lg-pt"
    }
    assert texts == {"zebra pt row", "zebra untagged row"}, (
        f"en-family row must be hidden for a known-pt channel, got {texts}"
    )


def test_search_keeps_other_family_when_channel_language_unknown():
    # Unknown channel language: non-destructive — every family surfaces
    # (the exclusion only fires when the channel language is KNOWN).
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "lg-un", "channel": "langchan",
        "title": "lang mirror", "canonical_key": "ck-lg-un",
        "started_at": "2026-08-03T17:24:00Z", "kind": "vod",
    })
    archive_db.insert_transcript(
        "youtube", "lg-un",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "zebra en2 row"}],
        lang="en",
    )
    archive_db.insert_transcript(
        "youtube", "lg-un",
        [{"seg_idx": 1, "start_sec": 1.0, "end_sec": 2.0, "text": "zebra pt2 row"}],
        lang="pt",
    )
    texts = {
        h["text"] for h in archive_db.search("zebra")
        if h["kind"] == "transcript" and h["video_id"] == "lg-un"
    }
    assert texts == {"zebra en2 row", "zebra pt2 row"}, texts


# --- channel-language-restricted caption ingest ---------------------------

def test_pick_captions_family_restricts_to_channel_language():
    info = {
        "automatic_captions": {
            "pt": [{"ext": "vtt", "url": "https://cap/pt.vtt"}],
            "en": [{"ext": "vtt", "url": "https://cap/en.vtt"}],
        }
    }
    # Known channel family: ONLY that family's track is picked — the old
    # rule stored pt AND en rows for the same segment.
    assert archive_ytdlp._pick_captions_for(info, "vtt", family="pt") == [
        ("pt", "https://cap/pt.vtt")
    ]
    assert archive_ytdlp._pick_captions_for(info, "vtt", family="en") == [
        ("en", "https://cap/en.vtt")
    ]
    # Unknown channel language: legacy rule keeps both families.
    assert {l for l, _ in archive_ytdlp._pick_captions_for(info, "vtt")} == {"pt", "en"}


def test_ingest_video_stores_only_channel_family_captions(monkeypatch, tmp_path):
    # End-to-end: a channel whose effective language is pt-BR (stored as
    # family 'pt') ingests ONLY pt captions even when the video serves en.
    from contextlib import contextmanager
    import io

    archive_db.upsert_video({
        "platform": "youtube", "video_id": "lg-ingest-0", "channel": "titiltei",
        "title": "lang probe", "canonical_key": "ck-lg-ingest",
        "started_at": "2026-08-03T17:24:00Z", "kind": "vod",
    })
    archive_db.set_channel_language("youtube", "titiltei", "pt")

    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nOla mundo.\n"

    class _FakeYdl:
        def extract_info(self, url, download=False):
            return {
                "id": "lg-ingest", "title": "Lang Probe", "channel": "titiltei",
                "timestamp": 1754256240, "duration": 30,
                "automatic_captions": {
                    "pt": [{"ext": "vtt", "url": "https://cap/pt.vtt"}],
                    "en": [{"ext": "vtt", "url": "https://cap/en.vtt"}],
                },
            }

        def urlopen(self, url):
            return io.BytesIO(vtt)

    @contextmanager
    def _fake_guard(outdir, *, video_id=None):
        yield _FakeYdl()

    monkeypatch.setattr(archive_ytdlp, "_guarded_youtube_dl", _fake_guard)
    report = archive_ytdlp.ingest_video("https://www.youtube.com/watch?v=lg-ingest")
    assert report["video_id"] == "lg-ingest"
    rows = archive_db.transcript_for("youtube", "lg-ingest")
    assert rows, "captions must be ingested"
    assert {r["lang"] for r in rows} == {"pt"}, (
        "known-pt channel must store only pt captions, got "
        f"{sorted({r['lang'] for r in rows})}"
    )
