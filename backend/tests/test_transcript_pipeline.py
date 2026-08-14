"""Batch-3 transcript pipeline fixes — all mocked, no network.

Covers:
  * entity backfill: legacy '&gt;&gt;'/'&amp;' rows unescaped + turn markers
    stripped, idempotent (second run touches nothing);
  * lang backfill: NULL transcript rows stamped with the owning video's
    channel language family (pt/en/es), unknown channels stay NULL;
  * read-time dedupe: +10 ms duplicate cue dropped, legit >= 1 s repeat kept
    (transcript_for + transcript_offsets);
  * cross-platform fallback: a transcript-less Twitch VOD serves its
    YouTube twin's rows (transcript_source / transcript_offsets /
    transcript_available / the archive transcript router), priority
    youtube > twitch > kick;
  * search default pt: off-family rows hidden, NULL rows kept;
  * done-time language correction: whisper detection disagreeing with the
    now-known channel language re-stamps the stored rows; an explicit job
    language never triggers the rewrite;
  * claim CAS: two workers cannot both claim the same stale 'running' job;
  * mid-run twin race: the higher-priority twin finishing during a run
    aborts before the first insert.

Run from backend/: python -m pytest tests/test_transcript_pipeline.py -q
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

_PIPELINE_DB = str(Path(tempfile.mkdtemp(prefix="transcript-pipeline-")) / "archive.db")
_PIPELINE_APP = str(Path(tempfile.mkdtemp(prefix="transcript-pipeline-app-")))
os.environ["VODRIP_ARCHIVE_DB"] = _PIPELINE_DB
os.environ["VODRIP_APP_DATA"] = _PIPELINE_APP

import pytest  # noqa: E402

from services import archive_db  # noqa: E402  (env must be set before import)
from services import archive_transcribe as at  # noqa: E402
from services import channel_language  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _pipeline_scratch_db():
    prev_db = os.environ.get("VODRIP_ARCHIVE_DB")
    prev_app = os.environ.get("VODRIP_APP_DATA")
    os.environ["VODRIP_ARCHIVE_DB"] = _PIPELINE_DB
    os.environ["VODRIP_APP_DATA"] = _PIPELINE_APP
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


def _seed_video(platform: str, video_id: str, key: str, channel: str = "titiltei") -> None:
    archive_db.upsert_video({
        "platform": platform,
        "video_id": video_id,
        "channel": channel,
        "title": f"{platform} mirror {video_id}",
        "canonical_key": key,
        "started_at": "2026-08-03T17:24:00Z",
        "kind": "vod",
    })


def _seed_rows(platform: str, video_id: str, rows: list[dict], lang: str | None = None) -> None:
    archive_db.insert_transcript(
        platform, video_id,
        [
            {
                "seg_idx": i,
                "start_sec": r[0],
                "end_sec": r[1],
                "text": r[2],
                "words": [],
            }
            for i, r in enumerate(rows)
        ],
        lang=lang,
    )


# --- 1. entity backfill ----------------------------------------------------

def test_entity_backfill_unescapes_and_strips_markers():
    conn = archive_db.get_conn()
    archive_db.insert_transcript(
        "youtube", "eb1",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
          "text": "&gt;&gt; Olá &amp; bem-vindo", "words": []}],
    )
    # A row with no entities must be left alone (the WHERE clause targets
    # only legacy entity rows).
    archive_db.insert_transcript(
        "youtube", "eb1",
        [{"seg_idx": 1, "start_sec": 2.0, "end_sec": 3.0,
          "text": "plain text > arrow", "words": []}],
    )
    archive_db._migrate_transcript_data(conn)
    rows = {r["seg_idx"]: r["text"] for r in archive_db.transcript_for("youtube", "eb1", raw=True)}
    assert rows[0] == "Olá & bem-vindo", rows[0]
    assert rows[1] == "plain text > arrow", rows[1]
    # Idempotent: a second pass touches nothing (still one row per seg_idx,
    # same text — no double-unescape turning '&' into '&amp;').
    before = {r["seg_idx"]: (r["text"], r["lang"]) for r in archive_db.transcript_for("youtube", "eb1", raw=True)}
    archive_db._migrate_transcript_data(conn)
    after = {r["seg_idx"]: (r["text"], r["lang"]) for r in archive_db.transcript_for("youtube", "eb1", raw=True)}
    assert after == before


# --- 2. lang backfill ------------------------------------------------------

def test_lang_backfill_stamps_channel_family_keeps_unknown_null():
    _seed_video("youtube", "lb-pt", "ck-lb-pt", channel="canalpt")
    archive_db.set_channel_language("youtube", "canalpt", "pt-BR")
    _seed_video("youtube", "lb-ja", "ck-lb-ja", channel="canalja")
    archive_db.set_channel_language("youtube", "canalja", "ja")
    _seed_rows("youtube", "lb-pt", [(0.0, 1.0, "ola"), (1.5, 2.5, "mundo")])
    _seed_rows("youtube", "lb-ja", [(0.0, 1.0, "konnichiwa")])
    # An already-tagged row must never be overwritten by the backfill.
    archive_db.insert_transcript(
        "youtube", "lb-pt",
        [{"seg_idx": 2, "start_sec": 3.0, "end_sec": 4.0, "text": "tagged", "words": []}],
        lang="en",
    )
    archive_db._migrate_transcript_data(archive_db.get_conn())
    pt_langs = {r["lang"] for r in archive_db.transcript_for("youtube", "lb-pt", raw=True)}
    assert pt_langs == {"pt", "en"}, pt_langs  # NULL rows stamped pt; explicit en kept
    ja_langs = {r["lang"] for r in archive_db.transcript_for("youtube", "lb-ja", raw=True)}
    assert ja_langs == {None}, ja_langs  # unknown family stays NULL


# --- 3. read-time dedupe ---------------------------------------------------

def test_read_dedupe_drops_overlap_keeps_legit_repeat():
    rows = [
        (0.0, 1.0, "same words"),
        (0.01, 1.01, "same words"),   # +10 ms ASR overlap duplicate
        (3.3, 4.3, "same words"),     # legit re-spoken repeat (>= threshold)
        (3.4, 4.4, "different"),
    ]
    _seed_rows("youtube", "dd1", rows)
    kept = archive_db.transcript_for("youtube", "dd1")
    assert [r["start_sec"] for r in kept] == [0.0, 3.3, 3.4], [r["start_sec"] for r in kept]
    kept_off = archive_db.transcript_offsets("youtube", "dd1")
    assert [r["offset_sec"] for r in kept_off] == [0.0, 3.3, 3.4], [r["offset_sec"] for r in kept_off]
    # The resume path still sees every stored row.
    assert len(archive_db.transcript_for("youtube", "dd1", raw=True)) == 4


# --- 4. cross-platform read fallback ---------------------------------------

def test_twitch_video_serves_youtube_twin_transcript():
    _seed_video("twitch", "tw1", "ck-fallback")
    _seed_video("youtube", "yt1", "ck-fallback")
    _seed_rows("youtube", "yt1", [(0.0, 1.0, "twin text")])
    assert archive_db.transcript_source("twitch", "tw1") == ("youtube", "yt1")
    assert archive_db.transcript_available("twitch", "tw1") is True
    assert archive_db.has_transcript("twitch", "tw1") is False  # raw guard stays own-rows
    off = archive_db.transcript_offsets("twitch", "tw1")
    assert [r["text"] for r in off] == ["twin text"]
    # Own rows always win over the twin.
    _seed_rows("twitch", "tw1", [(0.0, 1.0, "own text")])
    assert archive_db.transcript_source("twitch", "tw1") == ("twitch", "tw1")
    assert [r["text"] for r in archive_db.transcript_offsets("twitch", "tw1")] == ["own text"]


def test_fallback_priority_youtube_over_twitch_over_kick():
    _seed_video("kick", "k-fb", "ck-prio")
    _seed_video("twitch", "t-fb", "ck-prio")
    _seed_video("youtube", "y-fb", "ck-prio")
    _seed_rows("youtube", "y-fb", [(0.0, 1.0, "youtube wins")])
    _seed_rows("twitch", "t-fb", [(0.0, 1.0, "twitch second")])
    assert archive_db.transcript_source("kick", "k-fb") == ("youtube", "y-fb")
    # Remove the youtube rows: the twitch member becomes the best source.
    archive_db.delete_transcripts("youtube", "y-fb")
    assert archive_db.transcript_source("kick", "k-fb") == ("twitch", "t-fb")


async def test_archive_transcript_router_serves_twin():
    from routers.archive import archive_transcript

    _seed_video("twitch", "tw2", "ck-router")
    _seed_video("youtube", "yt2", "ck-router")
    _seed_rows("youtube", "yt2", [(0.0, 1.0, "router twin")])
    resp = await archive_transcript("twitch", "tw2")
    assert resp["source_platform"] == "youtube"
    assert resp["source_video_id"] == "yt2"
    assert [s["text"] for s in resp["segments"]] == ["router twin"]


# --- 5. search: default pt keeps NULL, hides off-family --------------------

def test_search_pt_keeps_null_hides_en_on_pt_channel():
    _seed_video("youtube", "sp1", "ck-sp1", channel="canalsem")
    archive_db.set_channel_language("youtube", "canalsem", "pt")
    _seed_rows("youtube", "sp1", [(0.0, 1.0, "zebra pt row")], lang="pt")
    _seed_rows("youtube", "sp1", [(2.0, 3.0, "zebra en row")], lang="en")
    _seed_rows("youtube", "sp1", [(4.0, 5.0, "zebra null row")], lang=None)
    hits = archive_db.search("zebra", lang="pt")
    texts = {h["text"] for h in hits}
    assert "zebra pt row" in texts
    assert "zebra null row" in texts  # untagged rows flow through the pt filter
    assert "zebra en row" not in texts  # channel family pt hides the en rows


# --- 6. done-time language correction --------------------------------------

def test_on_transcribe_done_restamps_wrong_detection():
    _seed_video("kick", "dl1", "ck-dl1", channel="canaldl")
    archive_db.set_channel_language("kick", "canaldl", "pt")
    _seed_rows("kick", "dl1", [(0.0, 1.0, "wrong lang row")], lang="en")
    channel_language._last_run.clear()
    lang = channel_language.on_transcribe_done("kick", "dl1", detected_lang="en")
    assert lang == "pt"
    stored = {r["lang"] for r in archive_db.transcript_for("kick", "dl1", raw=True)}
    assert stored == {"pt"}, stored
    # Matching detection -> nothing rewritten, same outcome.
    _seed_rows("kick", "dl1", [(2.0, 3.0, "pt row")], lang="pt")
    channel_language._last_run.clear()
    lang = channel_language.on_transcribe_done("kick", "dl1", detected_lang="pt")
    stored = {r["lang"] for r in archive_db.transcript_for("kick", "dl1", raw=True)}
    assert stored == {"pt", "pt"}, stored


def test_on_transcribe_done_explicit_language_never_rewrites():
    _seed_video("kick", "dl2", "ck-dl2", channel="canaldl2")
    archive_db.set_channel_language("kick", "canaldl2", "en")
    _seed_rows("kick", "dl2", [(0.0, 1.0, "forced pt row")], lang="pt")
    channel_language._last_run.clear()
    # detected_lang=None is the explicit-job-language signal (the worker
    # passes stats["lang"] only for auto-detection runs).
    lang = channel_language.on_transcribe_done("kick", "dl2", detected_lang=None)
    assert lang == "en"
    stored = {r["lang"] for r in archive_db.transcript_for("kick", "dl2", raw=True)}
    assert stored == {"pt"}, stored  # the forced pt tag survives


# --- 7. claim CAS ----------------------------------------------------------

def _seed_running_job(job_id: str, platform: str, video_id: str, updated_at: str) -> None:
    archive_db.enqueue_job(job_id, "transcribe", platform, video_id)
    # The merged claim predicate reads COALESCE(heartbeat, updated_at): the
    # seed must stale BOTH fields, since enqueue_job stamps a fresh heartbeat.
    archive_db.execute(
        "UPDATE archive_jobs SET status = 'running', updated_at = ?, heartbeat = ? WHERE id = ?",
        (updated_at, updated_at, job_id),
    )


def test_fresh_running_job_not_reclaimed():
    _seed_running_job("cas-fresh", "youtube", "cf1", at._now_iso())
    assert at._claim_next_job() is None


def test_stale_reclaim_cas_single_winner():
    _seed_running_job("cas-stale", "youtube", "cs1", "2000-01-01T00:00:00+00:00")
    # Worker A claims the stale job; the claim refreshes updated_at.
    job = at._claim_next_job()
    assert job is not None and job["video_id"] == "cs1"
    # Worker B, which SELECTed the same stale row BEFORE A's claim, runs its
    # reclaim UPDATE with the same stale-window cutoff it computed — it must
    # match zero rows because A already refreshed updated_at.
    stale_cutoff = "2000-06-01T00:00:00+00:00"
    cur = archive_db.execute(
        "UPDATE archive_jobs SET status = 'running', updated_at = ? "
        "WHERE id = ? AND status = 'running' AND updated_at < ?",
        (at._now_iso(), job["id"], stale_cutoff),
    )
    assert cur.rowcount == 0
    # And the job is not claimable again (its updated_at is fresh now).
    assert at._claim_next_job() is None


def test_queued_claim_still_works():
    archive_db.enqueue_job("cas-queued", "transcribe", "youtube", "cq1")
    job = at._claim_next_job()
    assert job is not None and job["id"] == "cas-queued"
    assert at._claim_next_job() is None


# --- 8. mid-run twin race --------------------------------------------------

def test_twin_transcribed_while_running_helper():
    _seed_video("kick", "k-mr", "ck-mr")
    _seed_video("youtube", "y-mr", "ck-mr")
    _seed_rows("youtube", "y-mr", [(0.0, 1.0, "twin")])
    assert at._twin_transcribed_while_running("kick", "k-mr") is True
    assert at._twin_transcribed_while_running("youtube", "y-mr") is False


def test_midrun_twin_win_aborts_before_insert(tmp_path):
    """When the higher-priority twin finishes mid-run, the partial rows are
    deleted and no further insert happens — the job reports skipped."""
    class _FakeAudio:
        size = 100 * 16000  # 100 s

    manifest = tmp_path / "manifest.json"
    inserted: list = []
    deleted: list = []
    with (
        patch.object(at, "decode_audio", return_value=_FakeAudio()),
        patch.object(at, "vad_speech_seconds", return_value=[(0.0, 10.0)]),
        patch.object(at, "_plan_chunks", return_value=[(0.0, 10.0)]),
        patch.object(at, "_job_engine", return_value="whisper"),
        patch.object(at, "_current_model", return_value=object()),
        patch.object(at, "_transcribe_batch", return_value=[
            ([{"start_sec": 0.0, "end_sec": 5.0, "text": "hi", "words": []}], "en")
        ]),
        patch.object(at, "_read_manifest", return_value=([], [])),
        patch.object(at, "_resume_plan", return_value=([0], 0)),
        patch.object(at, "_write_manifest_header", return_value=None),
        patch.object(at, "_append_manifest_entry", return_value=None),
        patch.object(at, "_manifest_path", return_value=manifest),
        patch.object(at, "_effective_device", return_value=("cpu", "int8")),
        patch.object(at, "_asr_model_name", return_value="small"),
        patch.object(archive_db, "transcribed_on_higher_priority_platform", return_value=True),
        patch.object(archive_db, "insert_transcript", side_effect=lambda *a, **k: inserted.append(a) or 0),
        patch.object(archive_db, "delete_transcripts", side_effect=lambda p, v: deleted.append((p, v)) or 1),
    ):
        stats = at._transcribe_audio_source(
            "kick", "k-abort", str(tmp_path / "audio.mp4"), None, None, None, 0.0,
            sharded=False, shard_dir=None,
        )
    assert stats.get("skipped") == "dedupe-transcribed"
    assert inserted == [], "no insert may happen after the twin won"
    assert deleted == [("kick", "k-abort")]
