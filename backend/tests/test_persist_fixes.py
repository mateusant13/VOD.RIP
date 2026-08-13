"""Persistence-gap fixes for the background machinery (no network).

Covers (each fix asserts on DB rows only):
  - Twitch re-ingest preserves status='ready' + archive_path (mirror of the
    Kick re-run guard), while metadata (title) still updates.
  - Scheduler Twitch leg is gated by channel_snapshots: a channel fetched
    within the freshness window is skipped; a stale one is re-fetched.
  - videos.captions_unavailable_at: stamped when ingest_video stores zero
    caption segments, cleared when a later ingest finds captions, and the
    scheduler's _youtube_covered skips videos whose marker is fresh.
  - YouTube re-ingest preserves fetched chat (_clear_video_data keeps
    messages when the video already has chat).
  - videos.original_fetch_failed_at: _mark_original_failed persists the
    cooldown; _original_failed_recently reads it from SQL (with the
    in-memory dict as fast-path).
  - A stale failed transcribe job (>= FAILED_JOB_FRESH_S) is requeued
    IN PLACE by _enqueue_transcriptions; a fresh failure stays failed.
"""

import contextlib
import os
import pathlib
import tempfile

import pytest

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="persist-fixes-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from services import archive_scheduler  # noqa: E402
from services import archive_twitch  # noqa: E402
from services import archive_ytdlp  # noqa: E402

_VID = "aaaaaaaaaaa"  # valid 11-char YouTube id


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


# --- 1. Twitch re-ingest preserves ready/archive_path -----------------------

def test_twitch_reingest_preserves_ready_state(scratch_db, monkeypatch):
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": "1234567890",
        "channel": "cellbit",
        "title": "Old title",
        "started_at": "2026-08-01T00:00:00Z",
        "kind": "vod",
        "canonical_key": "old-key",
        "archive_path": "H:/VODs/cellbit/1234567890.mp4",
        "status": "ready",
    })
    monkeypatch.setattr(archive_twitch, "list_recent_vods", lambda ch, limit=3: [{
        "id": "1234567890",
        "title": "New title",
        "created_at": "2026-08-02T00:00:00Z",
        "duration": 3600,
    }])

    results = archive_twitch.ingest_channel_vods("cellbit", limit=3)

    assert results and results[0]["video_id"] == "1234567890"
    row = archive_db.query(
        "SELECT status, archive_path, title, started_at FROM videos "
        "WHERE platform='twitch' AND video_id='1234567890'"
    )[0]
    assert row["status"] == "ready", "re-ingest must keep status='ready'"
    assert row["archive_path"] == "H:/VODs/cellbit/1234567890.mp4", (
        "re-ingest must keep archive_path"
    )
    assert row["title"] == "New title", "metadata must still refresh"
    assert row["started_at"] == "2026-08-02T00:00:00Z"


def test_twitch_reingest_metadata_only_row_stays_known(scratch_db, monkeypatch):
    """A non-ready row keeps the plain metadata-refresh behavior."""
    monkeypatch.setattr(archive_twitch, "list_recent_vods", lambda ch, limit=3: [{
        "id": "1234567890",
        "title": "T",
        "created_at": "2026-08-01T00:00:00Z",
        "duration": 3600,
    }])
    archive_twitch.ingest_channel_vods("cellbit", limit=3)
    row = archive_db.query(
        "SELECT status, archive_path FROM videos "
        "WHERE platform='twitch' AND video_id='1234567890'"
    )[0]
    assert row["status"] == "known" and row["archive_path"] is None


# --- 2. Snapshot gate on the scheduler Twitch leg ---------------------------

def test_scheduler_twitch_snapshot_gate(scratch_db, monkeypatch):
    calls = {"n": 0}

    def _fake_ingest(slug, limit=3):
        calls["n"] += 1
        return [{"video_id": "1234567890", "channel": slug}]

    monkeypatch.setattr(archive_twitch, "ingest_channel_vods", _fake_ingest)
    channel = {"twitchSlug": "cellbit"}

    # Never fetched -> fetch; then the non-empty result touches the snapshot.
    archive_scheduler._ingest_twitch(channel)
    assert calls["n"] == 1
    assert archive_db.channel_snapshot_age_sec("twitch", "cellbit") is not None

    # Fresh snapshot (< freshness window) -> this pass skips the fetch.
    archive_scheduler._ingest_twitch(channel)
    assert calls["n"] == 1, "fresh snapshot must skip the GQL fetch"

    # Stale snapshot -> fetched again.
    archive_db.execute(
        "UPDATE channel_snapshots SET fetched_at='2020-01-01T00:00:00Z' "
        "WHERE platform='twitch' AND channel_key='cellbit'"
    )
    archive_scheduler._ingest_twitch(channel)
    assert calls["n"] == 2, "stale snapshot must re-fetch"


# --- 3. captions_unavailable_at: stamp / clear / scheduler skip -------------

_VTT_WITH_CUE = (
    "WEBVTT\nKind: captions\nLanguage: pt\n\n"
    "00:00:01.000 --> 00:00:02.000 align:start position:0%\n"
    "Hello world.\n"
)


class _FakeYdl:
    def __init__(self, info: dict):
        self.info = info

    def extract_info(self, url: str, download: bool = False) -> dict:
        return self.info


def _fake_guarded_ydl(info: dict):
    @contextlib.contextmanager
    def _guard(outdir, *, video_id=None):
        yield _FakeYdl(info)

    return _guard


def _ingest_info():
    return {
        "id": _VID,
        "title": "caption test",
        "channel": "chan",
        "duration": 3600,
        "timestamp": 1785600000,
    }


def test_ingest_stamps_and_clears_captions_marker(scratch_db, monkeypatch):
    monkeypatch.setattr(
        archive_ytdlp, "_guarded_youtube_dl", _fake_guarded_ydl(_ingest_info())
    )

    # First ingest: no caption tracks -> zero segments -> marker stamped.
    monkeypatch.setattr(
        archive_ytdlp, "_fetch_captions", lambda ydl, info, family=None: []
    )
    report = archive_ytdlp.ingest_video(_VID)
    assert report["transcript_segments"] == 0
    assert archive_db.captions_unavailable_at("youtube", _VID) is not None, (
        "captionless ingest must stamp captions_unavailable_at"
    )

    # Second ingest finds captions -> segments stored -> marker cleared.
    archive_db.execute(
        "DELETE FROM archive_jobs WHERE id LIKE 'yt-ingest-{}%'".format(_VID)
    )  # stable per-second job id would collide otherwise
    monkeypatch.setattr(
        archive_ytdlp, "_fetch_captions",
        lambda ydl, info, family=None: [("pt", "vtt", _VTT_WITH_CUE)],
    )
    report = archive_ytdlp.ingest_video(_VID)
    assert report["transcript_segments"] > 0
    assert archive_db.captions_unavailable_at("youtube", _VID) is None, (
        "successful caption ingest must clear the marker"
    )


def test_youtube_covered_skips_fresh_marker(scratch_db):
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": _VID,
        "channel": "chan",
        "title": "t",
        "kind": "vod",
    })
    # No row -> not covered; bare row without captions -> not covered.
    assert archive_scheduler._youtube_covered("bbbbbbbbbbb") is False
    assert archive_scheduler._youtube_covered(_VID) is False

    # Fresh marker -> covered (skip re-extract).
    archive_db.mark_captions_unavailable("youtube", _VID)
    assert archive_scheduler._youtube_covered(_VID) is True

    # Stale marker -> not covered again (re-extract candidate).
    archive_db.execute(
        "UPDATE videos SET captions_unavailable_at='2020-01-01T00:00:00Z' "
        "WHERE platform='youtube' AND video_id=?", (_VID,)
    )
    assert archive_scheduler._youtube_covered(_VID) is False


# --- 4. Re-ingest preserves fetched chat ------------------------------------

def test_clear_video_data_preserves_chat(scratch_db):
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": _VID,
        "channel": "chan",
        "title": "t",
        "kind": "stream",
    })
    archive_db.insert_messages("youtube", _VID, [
        {"offset_sec": 1.0, "username": "u", "text": "hi", "badges": [], "emotes": []},
    ])
    archive_db.insert_transcript("youtube", _VID, [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "hello"},
    ])
    archive_db.set_alias("youtube", _VID, "some-key")

    archive_ytdlp._clear_video_data(_VID)

    assert archive_db.count_messages("youtube", _VID) == 1, (
        "re-ingest must NOT wipe fetched chat"
    )
    assert archive_db.query(
        "SELECT 1 FROM transcripts WHERE platform='youtube' AND video_id=? LIMIT 1",
        (_VID,),
    ) == [], "transcripts are re-derived and must be wiped"
    assert archive_db.query(
        "SELECT 1 FROM video_aliases WHERE platform='youtube' AND video_id=? LIMIT 1",
        (_VID,),
    ) == [], "aliases are re-derived and must be wiped"


def test_clear_video_data_chatless_video_loses_nothing(scratch_db):
    """A chat-less video has no messages to keep — the delete is a no-op."""
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": _VID,
        "channel": "chan",
        "title": "t",
        "kind": "vod",
    })
    archive_db.insert_transcript("youtube", _VID, [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "hello"},
    ])
    archive_ytdlp._clear_video_data(_VID)
    assert archive_db.count_messages("youtube", _VID) == 0
    assert archive_db.has_transcript("youtube", _VID) is False


# --- 5. original_fetch_failed_at persistence --------------------------------

def test_original_failed_marker_persists_and_expires(scratch_db):
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": _VID,
        "channel": "chan",
        "title": "t",
        "kind": "vod",
    })
    archive_ytdlp._original_failed_at.clear()

    # _mark_original_failed writes the durable column.
    archive_ytdlp._mark_original_failed(_VID)
    row = archive_db.query(
        "SELECT original_fetch_failed_at FROM videos WHERE video_id=?", (_VID,)
    )[0]
    assert row["original_fetch_failed_at"] is not None, (
        "failure cooldown must be persisted to videos.original_fetch_failed_at"
    )
    assert archive_ytdlp._original_failed_recently(_VID) is True

    # SQL-backed read: fresh column with EMPTY in-memory dict still counts.
    archive_ytdlp._original_failed_at.clear()
    archive_db.execute(
        "UPDATE videos SET original_fetch_failed_at=? WHERE video_id=?",
        (archive_db._now_iso(), _VID),
    )
    assert archive_ytdlp._original_failed_recently(_VID) is True

    # Stale column -> expired cooldown (and no fast-path re-arm).
    archive_ytdlp._original_failed_at.clear()
    archive_db.execute(
        "UPDATE videos SET original_fetch_failed_at='2020-01-01T00:00:00Z' "
        "WHERE video_id=?", (_VID,)
    )
    assert archive_ytdlp._original_failed_recently(_VID) is False

    # NULL column -> never failed.
    archive_ytdlp._original_failed_at.clear()
    archive_db.execute(
        "UPDATE videos SET original_fetch_failed_at=NULL WHERE video_id=?", (_VID,)
    )
    assert archive_ytdlp._original_failed_recently(_VID) is False


# --- 6. Stale failed transcribe jobs are requeued ---------------------------

def _seed_ready_youtube(vid: str, path: pathlib.Path) -> None:
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": vid,
        "channel": "chan",
        "title": "t",
        "kind": "vod",
        "status": "ready",
        "archive_path": str(path),
        "duration_sec": 120.0,
    })


def test_scheduler_youtube_transcribe_job_requires_marker(scratch_db, tmp_path):
    """Captions-first policy: the scheduler creates a YouTube transcribe job
    ONLY when the video has NO transcript rows AND captions_unavailable_at
    is set (permanent caption unavailability -> ASR candidate, audio
    downloaded at transcribe time). Never while captions are pending."""
    media = tmp_path / "v.mp4"
    media.write_bytes(b"not really media")
    _seed_ready_youtube(_VID, media)

    # No marker -> captions pending -> never created.
    archive_scheduler._enqueue_transcriptions()
    assert archive_db.latest_job("youtube", _VID, kind="transcribe") is None, (
        "no captions + no marker must never create a transcribe job"
    )

    # Marker set -> ASR candidate -> created with the stable job id.
    archive_db.mark_captions_unavailable("youtube", _VID)
    archive_scheduler._enqueue_transcriptions()
    job = archive_db.latest_job("youtube", _VID, kind="transcribe")
    assert job is not None, "captions_unavailable_at + no transcripts -> ASR job"
    assert job["id"] == f"transcribe-youtube-{_VID}", "stable job id"
    assert job["status"] == "queued"


def test_scheduler_still_enqueues_twitch_transcribe_job(scratch_db, tmp_path):
    """Twitch/Kick ASR stays unchanged: a ready Twitch VOD without
    transcripts still gets a transcribe job from the scheduler."""
    media = tmp_path / "tw.mp4"
    media.write_bytes(b"not really media")
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": "tw-vid-0001",
        "channel": "chan",
        "title": "t",
        "kind": "vod",
        "status": "ready",
        "archive_path": str(media),
        "duration_sec": 120.0,
    })

    archive_scheduler._enqueue_transcriptions()

    job = archive_db.latest_job("twitch", "tw-vid-0001", kind="transcribe")
    assert job is not None, "Twitch must still get ASR jobs"
    assert job["id"] == "transcribe-twitch-tw-vid-0001", "stable job id"
    assert job["status"] == "queued"


def test_caption_ingest_flips_queued_transcribe_job_to_done(scratch_db, monkeypatch):
    """Race: a transcribe job queued before the captions land is resolved to
    done the moment ingest_video stores caption segments — no ASR work is
    ever spent on the video."""
    archive_db.upsert_video({
        "platform": "youtube",
        "video_id": _VID,
        "channel": "chan",
        "title": "t",
        "kind": "vod",
    })
    job_id = f"transcribe-youtube-{_VID}"
    archive_db.enqueue_job(job_id, "transcribe", "youtube", _VID, priority=0)
    assert archive_db.latest_job("youtube", _VID, kind="transcribe")["status"] == "queued"

    monkeypatch.setattr(
        archive_ytdlp, "_guarded_youtube_dl", _fake_guarded_ydl(_ingest_info())
    )
    monkeypatch.setattr(
        archive_ytdlp, "_fetch_captions",
        lambda ydl, info, family=None: [("pt", "vtt", _VTT_WITH_CUE)],
    )
    report = archive_ytdlp.ingest_video(_VID)
    assert report["transcript_segments"] > 0, "captions must have been stored"
    job = archive_db.latest_job("youtube", _VID, kind="transcribe")
    assert job is not None and job["status"] == "done", (
        "queued transcribe job must flip to done when captions land"
    )


def test_stale_failed_transcribe_job_requeued(scratch_db, tmp_path):
    media = tmp_path / "v.mp4"
    media.write_bytes(b"not really media")
    _seed_ready_youtube(_VID, media)

    job_id = f"transcribe-youtube-{_VID}"
    archive_db.enqueue_job(job_id, "transcribe", "youtube", _VID, priority=0)
    archive_db.update_job(job_id, status="failed", error="whisper oom")
    # Stale: failed longer than FAILED_JOB_FRESH_S ago.
    archive_db.execute(
        "UPDATE archive_jobs SET updated_at='2020-01-01T00:00:00Z' WHERE id=?", (job_id,)
    )

    archive_scheduler._enqueue_transcriptions()

    job = archive_db.latest_job("youtube", _VID, kind="transcribe")
    assert job["status"] == "queued", "stale failed transcribe job must be requeued"
    assert job["id"] == job_id, "requeue must happen IN PLACE (stable job id)"


def test_fresh_failed_transcribe_job_stays_queued_as_retry(scratch_db, tmp_path):
    """TASK10: a transient transcribe failure is auto-requeued by
    update_job (status='queued', attempts=1, next_retry_at deadline) — the
    scheduler must NOT enqueue a duplicate for it, and the retry owns the
    row until the deadline passes."""
    media = tmp_path / "v.mp4"
    media.write_bytes(b"not really media")
    _seed_ready_youtube(_VID, media)

    job_id = f"transcribe-youtube-{_VID}"
    archive_db.enqueue_job(job_id, "transcribe", "youtube", _VID, priority=0)
    archive_db.update_job(job_id, status="failed", error="whisper oom")

    archive_scheduler._enqueue_transcriptions()

    job = archive_db.latest_job("youtube", _VID, kind="transcribe")
    assert job["status"] == "queued", (
        "transient failure must be requeued, not left terminal"
    )
    assert job["attempts"] == 1, "one prior failure recorded"
    assert job["next_retry_at"] is not None, "retry scheduled with a deadline"
    rows = archive_db.query(
        "SELECT id FROM archive_jobs WHERE kind='transcribe' "
        "AND platform='youtube' AND video_id=?", (_VID,)
    )
    assert len(rows) == 1 and rows[0]["id"] == job_id, (
        "the scheduler must not enqueue a duplicate while a retry owns the row"
    )
