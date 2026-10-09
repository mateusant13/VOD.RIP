"""VOD.RIP background remediation: backoff, probe gate, dedupe, pacing.

Covers the four measured defects and their fixes:
  (a) a rate-limited platform does not retry immediately (exponential backoff);
  (b) an expensive probe does not loop when there is no new VOD;
  (c) VODRIP_BACKGROUND=1 pacing is respected and cadence only ever LOWERS;
  (d) Twitch/Kick dedupe picks ONE source;
  (e) YouTube caption reuse short-circuits transcription.

DB isolation follows tests/test_rate_budget.py: a scratch VODRIP_ARCHIVE_DB is
bound at module import, and the governor's history priming is disabled so the
whole file is deterministic and touches no real data.
"""
from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

_DB = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-remediation-")) / "archive.db"
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
os.environ["VODRIP_RATE_BUDGET_PRIME"] = "0"

from services import archive_journal, probe_gate, rate_budget  # noqa: E402
from services import archive_db  # noqa: E402


class FakeClock:
    """Monotonic-by-construction test clock. Nothing sleeps for real."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


@pytest.fixture()
def clock():
    c = FakeClock()
    rate_budget.set_clock(c)
    probe_gate.set_clock(c)
    rate_budget.reset()
    yield c
    rate_budget.set_clock(__import__("time").monotonic)
    probe_gate.set_clock(__import__("time").monotonic)
    rate_budget.reset()


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    archive_journal.configure(tmp_path / "journal.jsonl")
    probe_gate.configure(tmp_path / "probe_gate.json")
    archive_journal.reset()
    probe_gate.reset()
    yield
    archive_journal.configure(None)
    probe_gate.configure(None)


# --- (a) backoff: a limited platform does not retry immediately ---------------

def test_limit_event_arms_exponential_backoff(clock):
    rate_budget.note_limit("youtube", kind="yt_channel_list", status=429)
    assert rate_budget.in_backoff("youtube") is True
    first = rate_budget.retry_after_s("youtube")
    assert first == pytest.approx(rate_budget._BACKOFF_BASE_S, abs=0.01)

    # A second trip doubles the delay (exponential, never flat).
    rate_budget.note_limit("youtube", kind="yt_channel_list", status=429)
    second = rate_budget.retry_after_s("youtube")
    assert second > first

    # Time alone clears it; a refused request never extends it.
    clock.advance(second + 1.0)
    assert rate_budget.in_backoff("youtube") is False
    assert rate_budget.retry_after_s("youtube") == 0.0


def test_governor_refuses_during_backoff_without_sleeping(clock, monkeypatch):
    from services import archive_ytdlp

    rate_budget.note_limit("youtube", kind="yt_channel_list", status=429)
    sleeps: list = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(archive_ytdlp.YtGovernorExhausted):
        archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_channel_list")

    # The whole point: no sub-second wait-then-retry loop while backed off.
    assert sleeps == [], "a backed-off platform must be refused immediately"


# --- (b) probe gate: no loop when there are no new VODs -----------------------

def test_explicit_no_new_vod_suppresses_the_probe(clock):
    assert probe_gate.should_probe("youtube", "chan", latest_id="v1") is True
    probe_gate.note_probe("youtube", "chan", found_new=False, latest_id="v1")
    # Same newest id = proof of no new VOD -> the expensive probe is skipped.
    for _ in range(5):
        assert probe_gate.should_probe("youtube", "chan", latest_id="v1") is False
    # A genuinely new id opens the gate again.
    assert probe_gate.should_probe("youtube", "chan", latest_id="v2") is True
    probe_gate.note_probe("youtube", "chan", found_new=True, latest_id="v2")
    assert probe_gate.state("youtube", "chan")["no_new_streak"] == 0


def test_time_gated_probe_backs_off_exponentially(clock):
    assert probe_gate.should_probe("twitch", "c") is True
    probe_gate.note_probe("twitch", "c", found_new=False)
    assert probe_gate.backoff_remaining("twitch", "c") == pytest.approx(
        probe_gate.BACKOFF_BASE_S
    )
    clock.advance(probe_gate.BACKOFF_BASE_S - 1)
    assert probe_gate.should_probe("twitch", "c") is False
    clock.advance(2)
    assert probe_gate.should_probe("twitch", "c") is True
    # Second no-new result doubles the window.
    probe_gate.note_probe("twitch", "c", found_new=False)
    assert probe_gate.backoff_remaining("twitch", "c") == pytest.approx(
        probe_gate.BACKOFF_BASE_S * probe_gate.BACKOFF_FACTOR
    )


def test_probe_gate_persists_and_reloads(tmp_path):
    path = tmp_path / "pg.json"
    probe_gate.configure(path)
    probe_gate.note_probe("kick", "k", found_new=False, latest_id="x")
    probe_gate.configure(None)  # clears memory, keeps the file configured
    probe_gate.configure(path)
    assert probe_gate.should_probe("kick", "k", latest_id="x") is False
    assert probe_gate.should_probe("kick", "k", latest_id="y") is True


# --- (c) VODRIP_BACKGROUND pacing + cadence only ever lowers ------------------

def test_background_pacing_and_cadence_lowering(clock, monkeypatch):
    from services import archive_scheduler

    monkeypatch.delenv("VODRIP_BACKGROUND", raising=False)
    assert archive_scheduler._pass_interval() == archive_scheduler.PASS_INTERVAL_SEC
    assert archive_scheduler._yt_ingest_budget() == archive_scheduler.YOUTUBE_INGEST_PER_PASS

    monkeypatch.setenv("VODRIP_BACKGROUND", "1")
    assert archive_scheduler._pass_interval() == 360.0
    assert archive_scheduler._yt_ingest_budget() == 1
    assert archive_scheduler._transcribe_budget() == 1

    # A dry/backed-off platform can only LOWER the effective cadence.
    assert rate_budget.cadence_multiplier() == 1.0
    rate_budget.note_limit("youtube", status=429)
    mult = rate_budget.cadence_multiplier()
    assert 1.0 < mult <= rate_budget._MAX_CADENCE_MULTIPLIER
    assert rate_budget.cadence_multiplier("youtube") == mult
    # An untouched platform is unaffected by another platform's backoff.
    assert rate_budget.cadence_multiplier("twitch") == 1.0


# --- (d) Twitch/Kick dedupe picks ONE source ---------------------------------

def test_kick_dedupe_picks_the_higher_priority_source():
    from services import archive_kick

    key = "ultimo-dia|2026-01-01"
    group = {"canonical_key": key, "videos": [{"platform": "kick"}, {"platform": "youtube"}]}
    assert archive_kick.dedupe_decision(key, [group]) == ("skip", "youtube")
    twitch_only = {"canonical_key": key, "videos": [{"platform": "twitch"}]}
    assert archive_kick.dedupe_decision(key, [twitch_only]) == ("skip", "twitch")
    # Kick-exclusive content is still downloaded (never blocked by itself).
    assert archive_kick.dedupe_decision(key, [{"canonical_key": key,
                                              "videos": [{"platform": "kick"}]}]) == (
        "download", None,
    )


def test_twin_transcribed_on_higher_priority_platform_skips_kick():
    key = "mirror|2026-02-02"
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "tw1", "channel": "c",
        "title": "Mirror", "canonical_key": key,
    })
    archive_db.upsert_video({
        "platform": "kick", "video_id": "ki1", "channel": "c",
        "title": "Mirror", "canonical_key": key,
    })
    assert archive_db.transcribed_on_higher_priority_platform("kick", "ki1") is False
    archive_db.insert_transcript(
        "twitch", "tw1",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "ola", "words": []}],
    )
    assert archive_db.transcribed_on_higher_priority_platform("kick", "ki1") is True
    # The one source picked for display is the Twitch twin.
    assert archive_db.transcript_source("kick", "ki1") == ("twitch", "tw1")


# --- (e) YouTube caption reuse short-circuits transcription ------------------

def test_youtube_captions_short_circuit_asr():
    from services import queue_policy

    archive_db.upsert_video({
        "platform": "youtube", "video_id": "yt1", "channel": "c",
        "title": "Captioned", "kind": "video",
    })
    archive_db.insert_transcript(
        "youtube", "yt1",
        [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "hi", "words": []}],
    )
    assert queue_policy.transcript_route_verdict(
        "youtube", "yt1", subtitles_first=True
    ) == queue_policy.VERDICT_SKIP_CAPTIONS
    # A captionless video whose captions are still pending waits instead of
    # burning GPU on ASR.
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "yt2", "channel": "c",
        "title": "Pending", "kind": "video",
    })
    assert queue_policy.transcript_route_verdict(
        "youtube", "yt2", subtitles_first=True
    ) == queue_policy.VERDICT_WAIT_CAPTION


# --- (3) SQLite read consolidation -------------------------------------------

def test_hot_reader_snapshot_coalesces_repeated_reads():
    archive_db.worker_heartbeat("transcribe")
    before = archive_db.read_ops_total()
    for _ in range(50):
        archive_db.worker_live(age_s=45, tag="transcribe")
    after = archive_db.read_ops_total()
    # At most one SQL read serves the whole burst (the snapshot), never 50.
    assert after - before <= 1, f"expected <=1 read, got {after - before}"

    # A write invalidates the snapshot, so the next read is fresh.
    archive_db.worker_heartbeat("transcribe")
    archive_db.worker_live(age_s=45, tag="transcribe")
    assert archive_db.read_ops_total() > after


# --- journal: best-effort, deduped -------------------------------------------

def test_journal_emit_writes_and_dedupes(tmp_path):
    path = tmp_path / "j.jsonl"
    archive_journal.configure(path)
    assert archive_journal.emit("cadence", platform="youtube", value=2) is True
    assert archive_journal.emit(
        "cadence", platform="youtube", value=2,
        dedupe_key="cadence:youtube", min_interval_s=300,
    ) is True
    # Same key inside the window is suppressed.
    assert archive_journal.emit(
        "cadence", platform="youtube", value=2,
        dedupe_key="cadence:youtube", min_interval_s=300,
    ) is False
    rows = archive_journal.read_tail(10)
    assert len(rows) == 2
    assert rows[-1]["kind"] == "cadence"
    assert rows[-1]["platform"] == "youtube"
