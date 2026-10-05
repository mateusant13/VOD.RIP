"""The age-gate park must outlive the process that created it.

The defect: an age-gated video is PARKED, not blocked (dc01ea3) — but both
things the frontend reads lived only in memory. `_age_parked` (the set of
parked ids) and the sweep's `age_parked` counter were process-lifetime state, so
after a restart — and for ANY deep-search job recovered from the database
rather than freshly started — the API reported `age_parked: 0` and every
video's `captions_parked_reason` was simply absent. The UI rendered the parked
state during a live sweep and then quietly forgot it. The status endpoint even
documented 0-for-recovered-jobs as the contract, which made the hole look
intentional.

What the park actually is: the per-video no-captions marker
(videos.captions_unavailable_at) plus its age-gate classification
(videos.captions_unavailable_kind) on the video row. That is the durable state
and it is what these tests read back after a simulated restart. Two invariants
the fix must not lose:

  * REVERSIBILITY. A park that survives a restart but cannot be RELEASED would
    be worse than the bug: the user is told the video is waiting on their
    sign-in, signs in, and nothing happens. So the release path is exercised
    across a restart, not just the park.
  * NOT A TERMINAL VERDICT. The park deliberately rides the reversible marker
    and never transcript_kind='blocked'.

"Simulated restart" here means exactly what a process restart destroys: the
module-global registries (the in-memory deep-job map and the age-park set) are
dropped, and the deep_jobs schema cache is cleared so the migration path
re-runs as it would in a fresh process. The database is left alone — it is what
survives a restart, and that is the whole point.

Seams monkeypatched (_deep_enumerate / _deep_fetch_transcript) — no network.

Run from backend/: python -m pytest tests/test_age_park_survives_restart.py
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from routers import archive
from services import archive_db

AGE_GATE_ERROR = (
    "[youtube] VzuPKrGl0z8: Sign in to confirm your age. This video may be "
    "inappropriate for some users. Use --cookies-from-browser or --cookies "
    "for the authentication."
)
# The wire contract, written as literals on purpose: these are the values a
# stored row and the API carry, so the test pins the CONTRACT and not whatever
# name the module happens to give the constant.
NO_SESSION = "age_gate_no_session"
REJECTED_SESSION = "age_gate_session_rejected"
GATED = "VzuPKrGl0z8"
PLAIN = "dQw4w9WgXcQ"  # a video that was never gated
_OWNED_IDS = (GATED, PLAIN)  # the only video rows this module creates


def _video(vid: str, created: str = "2024-01-03T00:00:00+00:00") -> dict:
    return {
        "id": vid,
        "title": "age gated" if vid == GATED else "ordinary",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "created_at": created,
        "channel": "deepchan",
        "content_kind": "video",
        "duration": 60,
        "duration_string": "1:00",
        "views": 10,
        "thumbnail_url": None,
    }


def _simulate_restart() -> None:
    """Drop everything a process restart drops; keep the database.

    Before the fix the park ALSO lived in a process-lifetime dict. A restart
    destroys whatever in-process park state exists, so clear it if this build
    has one — otherwise these tests would pass on a build that keeps the park
    in memory and fail for the wrong reason on one that does not."""
    with archive._deep_jobs_lock:
        archive._deep_jobs.clear()
    legacy_park = getattr(archive, "_age_parked", None)
    if isinstance(legacy_park, dict):
        with archive._age_park_lock:  # pre-fix builds only
            legacy_park.clear()
    # The schema cache is process lifetime too — a fresh process re-runs
    # _ensure_deep_jobs_table (CREATE IF NOT EXISTS + the additive age_parked
    # column) against the SAME file, exactly as it would on boot.
    archive._deep_jobs_tables_ok.clear()


def _reset_markers() -> None:
    """Clear every per-video no-captions marker between tests.

    Two statements so the cleanup still does its job on a build whose
    videos table has no captions_unavailable_kind column (the pre-fix
    schema): a cleanup that raised would turn every behavioural assertion in
    this file into a setup error and hide the defect it is here to catch."""
    archive_db.execute(
        "UPDATE videos SET captions_unavailable_at = NULL WHERE platform = 'youtube'"
    )
    try:
        archive_db.execute(
            "UPDATE videos SET captions_unavailable_kind = NULL WHERE platform = 'youtube'"
        )
    except Exception:  # noqa: BLE001 — column absent on a pre-fix schema
        pass


def _drop_seeded_rows() -> None:
    """Delete the rows this module created — the session scratch DB is shared.

    Another module asserts on EVERY youtube video (queue_policy's
    latest-per-channel candidates counts marker-free, transcript-free rows), so
    a leftover row seeded here is another module's failure, and which run trips
    it depends on which files happen to share the session. This module owns
    exactly these two ids and removes them, transcripts first (there is no
    FK cascade; the FTS sync is trigger-driven)."""
    ph = ",".join("?" * len(_OWNED_IDS))
    for sql in (
        f"DELETE FROM transcripts WHERE platform='youtube' AND video_id IN ({ph})",
        f"DELETE FROM videos WHERE platform='youtube' AND video_id IN ({ph})",
    ):
        try:
            archive_db.execute(sql, tuple(_OWNED_IDS))
        except Exception:  # noqa: BLE001 — table may not exist in a scratch DB
            pass


@pytest.fixture(autouse=True)
def clean_park_state():
    """Every test starts with no markers, no jobs, no in-memory park state."""
    _simulate_restart()
    _reset_markers()
    _drop_seeded_rows()
    yield
    with archive._deep_jobs_lock:
        for j in archive._deep_jobs.values():
            j["cancel"].set()
    _simulate_restart()
    _reset_markers()
    _drop_seeded_rows()


@pytest.fixture()
def fast_pace(monkeypatch):
    """No pacing sleep in tests — the gate/pacing itself is covered elsewhere."""
    monkeypatch.setattr(archive, "_DEEP_MIN_GAP_S", 0.0)


@pytest.fixture()
def no_session(monkeypatch):
    """No signed-in YouTube session (the state the 24h events were recorded in)."""
    import services.youtube_session as ys

    monkeypatch.setattr(ys, "youtube_session_configured", lambda: False, raising=False)
    return False


@pytest.fixture()
def signed_in(monkeypatch):
    """An authenticated YouTube session exists."""
    import services.youtube_session as ys

    monkeypatch.setattr(ys, "youtube_session_configured", lambda: True, raising=False)
    return True


def _seed(vids=()) -> None:
    """Video rows exist (the park marker is UPDATE-only — it rides a row)."""
    for v in vids:
        archive_db.upsert_channel_video(
            {
                "platform": "youtube",
                "video_id": str(v["id"]),
                "channel": v["channel"],
                "title": v["title"],
                "kind": "vod",
                "started_at": v["created_at"],
            }
        )


def _parked_reason_from_api(video_id: str) -> str | None:
    body = asyncio.run(archive.archive_videos(platform="youtube"))
    row = [v for v in body["videos"] if v.get("video_id") == video_id]
    assert row, f"{video_id} missing from the videos API"
    return row[0].get("captions_parked_reason")


def _parked_code_from_api(video_id: str) -> str | None:
    body = asyncio.run(archive.archive_videos(platform="youtube"))
    row = [v for v in body["videos"] if v.get("video_id") == video_id]
    assert row, f"{video_id} missing from the videos API"
    return row[0].get("captions_parked_reason_code")


def _run_sweep(monkeypatch, videos, fetcher) -> dict:
    """Patch the seams, start the sweep through the endpoint, return the job."""
    monkeypatch.setattr(
        archive, "_deep_enumerate", lambda handle: (videos, False, len(videos))
    )
    monkeypatch.setattr(archive, "_deep_fetch_transcript", fetcher)
    job_id = asyncio.run(
        archive.archive_search_deep_start(
            archive.DeepSearchRequest(channel="deepchan", query="cesar")
        )
    )["job_id"]
    for _ in range(400):
        with archive._deep_jobs_lock:
            job = dict(archive._deep_jobs[job_id])
            job["id"] = job_id  # the in-memory dict is keyed by id, not stored
        if job["status"] != "running":
            return job
        time.sleep(0.025)
    raise AssertionError("deep job did not settle in 10s")


# --- the park survives -----------------------------------------------------


def test_parked_video_still_reports_its_reason_after_a_restart(
    monkeypatch, fast_pace, no_session
):
    """THE defect. A restart must not make the park — or the reason for it —
    disappear from the videos API."""
    videos = [_video(GATED), _video(PLAIN)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        raise RuntimeError(AGE_GATE_ERROR)

    _run_sweep(monkeypatch, videos, fetcher)
    reason_before = _parked_reason_from_api(GATED)
    assert reason_before, "a live sweep must already report the park reason"

    _simulate_restart()

    assert _parked_reason_from_api(GATED) == reason_before, (
        "the parked reason did not survive the restart — the UI will render "
        "the parked state and then silently forget it"
    )
    assert GATED in archive._age_parked_snapshot()


def test_a_video_that_was_never_parked_reports_no_reason_after_a_restart(
    monkeypatch, fast_pace, no_session
):
    """The park must not become a blanket: only gated videos carry a reason."""
    videos = [_video(GATED), _video(PLAIN)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        if vid == GATED:
            raise RuntimeError(AGE_GATE_ERROR)
        return {"segments": [{"offset": 0.0, "text": "hello"}], "lang": "en"}

    _run_sweep(monkeypatch, videos, fetcher)
    _simulate_restart()

    assert _parked_reason_from_api(GATED), "the gated video lost its reason"
    assert _parked_reason_from_api(PLAIN) is None, (
        "a video that was never gated must not be reported as parked"
    )
    assert PLAIN not in archive._age_parked_map()


def test_an_ordinary_no_captions_verdict_is_not_reported_as_an_age_gate_park():
    """captions_unavailable_at alone must stay an ordinary captionless verdict.

    The marker is shared: archive_ytdlp stamps it for a plain zero-segment
    ingest too. Reading the park off the marker without its classification
    would tell the user to go sign in for a video that simply has no captions.
    """
    _seed([_video(PLAIN)])
    archive_db.mark_captions_unavailable("youtube", PLAIN)
    _simulate_restart()

    assert archive_db.captions_unavailable_at("youtube", PLAIN), "marker stamped"
    assert _parked_reason_from_api(PLAIN) is None
    assert _parked_code_from_api(PLAIN) is None
    assert PLAIN not in archive._age_parked_map()


def test_a_plain_verdict_replaces_a_stale_age_gate_code():
    """A re-fetch that ends in an ordinary captionless verdict must clear the
    code with the stamp — a dead age-gate code must never outlive its cause."""
    _seed([_video(GATED)])
    archive_db.mark_captions_unavailable("youtube", GATED, kind=NO_SESSION)
    assert _parked_reason_from_api(GATED)

    archive_db.mark_captions_unavailable("youtube", GATED)  # no kind

    assert archive_db.captions_unavailable_at("youtube", GATED)
    assert archive_db.captions_unavailable_kind("youtube", GATED) is None
    assert _parked_reason_from_api(GATED) is None


def test_a_successful_ingest_clears_the_park_and_its_code(monkeypatch, fast_pace, no_session):
    """Captions arrived: the video is unmarked, unclassified, not parked."""
    videos = [_video(GATED)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        return {"segments": [{"offset": 0.0, "text": "hi"}], "lang": "en"}

    _run_sweep(monkeypatch, videos, fetcher)
    assert archive_db.captions_unavailable_kind("youtube", GATED) is None
    _simulate_restart()
    assert _parked_reason_from_api(GATED) is None


# --- the park is still reversible, across a restart ------------------------


def test_signing_in_releases_a_park_that_predates_the_restart(
    monkeypatch, fast_pace, no_session, signed_in
):
    """THE half of the bug that would be worse than the bug.

    Before the fix the release walked an in-memory set, so a park written
    before the restart was never released: the user is told the video is
    waiting on their sign-in, signs in, and nothing happens."""
    videos = [_video(GATED)]
    _seed(videos)
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    _run_sweep(monkeypatch, videos, fetcher)
    assert len(calls) == 1
    assert archive_db.captions_unavailable_at("youtube", GATED)

    # The process dies and comes back. Nothing about the park is in memory now
    # — only the video row, which is what the release has to read.
    _simulate_restart()
    assert archive_db.captions_unavailable_at("youtube", GATED), (
        "precondition: the park is on the row, not in this process"
    )

    assert archive._unpark_age_gated_if_authenticated() == 1, (
        "signing in must release a park that was written before the restart"
    )
    assert archive_db.captions_unavailable_at("youtube", GATED) is None
    assert archive_db.captions_unavailable_kind("youtube", GATED) is None
    assert archive._age_parked_snapshot() == {}
    assert _parked_reason_from_api(GATED) is None

    # ...so the next sweep genuinely re-attempts it.
    _run_sweep(monkeypatch, videos, fetcher)
    assert len(calls) == 2, "an authenticated sweep must re-attempt the un-parked video"


def test_a_park_predating_the_restart_stays_put_while_no_session_exists(
    monkeypatch, fast_pace, no_session
):
    """The release must not fire on a failed/absent session probe."""
    videos = [_video(GATED)]
    _seed(videos)
    calls: list[str] = []

    def fetcher(vid: str) -> dict:
        calls.append(vid)
        raise RuntimeError(AGE_GATE_ERROR)

    _run_sweep(monkeypatch, videos, fetcher)
    _simulate_restart()

    assert archive._unpark_age_gated_if_authenticated() == 0
    assert archive_db.captions_unavailable_at("youtube", GATED)
    assert _parked_code_from_api(GATED) == NO_SESSION, "the park must survive with no session"

    _run_sweep(monkeypatch, videos, fetcher)
    assert len(calls) == 1, "a still-signed-out sweep must not re-attempt it"


def test_an_expired_park_stops_being_reported_as_a_park():
    """A stamp older than the no-captions cooldown is a candidate again.

    The sweep re-attempts it on its own, so continuing to tell the user it is
    parked on their sign-in would be a claim the archive no longer makes."""
    from services.archive_scheduler import CAPTIONS_UNAVAILABLE_FRESH_S

    _seed([_video(GATED)])
    archive_db.mark_captions_unavailable("youtube", GATED, kind=NO_SESSION)
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=CAPTIONS_UNAVAILABLE_FRESH_S + 3600
    )
    archive_db.execute(
        "UPDATE videos SET captions_unavailable_at = ? "
        "WHERE platform='youtube' AND video_id = ?",
        (stale.isoformat(timespec="seconds"), GATED),
    )
    _simulate_restart()

    assert archive_db.captions_unavailable_at("youtube", GATED), "the row is still marked"
    assert archive._age_parked_map() == {}, "an expired park is not a park"
    assert _parked_reason_from_api(GATED) is None
    assert _parked_code_from_api(GATED) is None


def test_an_expired_park_is_still_released_once_a_session_exists(no_session, signed_in):
    """Expiry stops the REPORT; a later sign-in still clears the stale stamp."""
    from services.archive_scheduler import CAPTIONS_UNAVAILABLE_FRESH_S

    _seed([_video(GATED)])
    archive_db.mark_captions_unavailable("youtube", GATED, kind=NO_SESSION)
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=CAPTIONS_UNAVAILABLE_FRESH_S + 3600
    )
    archive_db.execute(
        "UPDATE videos SET captions_unavailable_at = ? "
        "WHERE platform='youtube' AND video_id = ?",
        (stale.isoformat(timespec="seconds"), GATED),
    )
    _simulate_restart()

    assert archive._unpark_age_gated_if_authenticated() == 1
    assert archive_db.captions_unavailable_at("youtube", GATED) is None
    assert archive_db.captions_unavailable_kind("youtube", GATED) is None


def test_park_is_not_widened_into_a_terminal_verdict(
    monkeypatch, fast_pace, no_session
):
    """The park deliberately reuses the reversible marker, not 'blocked'.

    transcript_kind='blocked' is the irreversible ASR verdict — a park that
    wrote it would never be re-attempted, and signing in could not release it.
    """
    videos = [_video(GATED)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        raise RuntimeError(AGE_GATE_ERROR)

    _run_sweep(monkeypatch, videos, fetcher)
    row = archive_db.query(
        "SELECT transcript_kind FROM videos WHERE platform='youtube' AND video_id=?",
        (GATED,),
    )
    assert not row or row[0]["transcript_kind"] != "blocked", (
        "an age-gate park must stay reversible — it is credential-bound"
    )


# --- a DB-recovered job reports the truth -----------------------------------


def test_db_recovered_job_reports_the_persisted_age_parked_count(
    monkeypatch, fast_pace, no_session
):
    """The polled status field, read back for a job with no in-memory state.

    Before the fix this returned a hardcoded 0 with a comment claiming the
    number was not recoverable — so a sweep the app restarted mid-flight
    reported 'nothing is parked' while the video rows said otherwise."""
    videos = [_video(GATED), _video(PLAIN)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        if vid == GATED:
            raise RuntimeError(AGE_GATE_ERROR)
        return {"segments": [{"offset": 0.0, "text": "hello"}], "lang": "en"}

    job = _run_sweep(monkeypatch, videos, fetcher)
    assert job["age_parked"] == 1, "precondition: the live sweep counted the park"
    live = asyncio.run(archive.archive_search_deep_status(job["id"]))
    assert live["age_parked"] == 1

    _simulate_restart()  # the process died; only the row survives

    recovered = asyncio.run(archive.archive_search_deep_status(job["id"]))
    assert recovered["status"] == "done"
    assert recovered["resumed"] is True
    assert recovered["age_parked"] == 1, (
        "a DB-recovered job must report what the run actually parked, not 0"
    )
    # The other recovered fields are unchanged by this fix — a count that is
    # truthful only because the rest of the row is not would be a new hole.
    assert recovered["no_transcript"] == live["no_transcript"]
    assert recovered["scanned"] == live["scanned"]


def test_db_recovered_job_reports_zero_when_nothing_was_parked(
    monkeypatch, fast_pace, no_session
):
    """Zero must still mean zero: a recovered job must not invent a number."""
    videos = [_video(PLAIN)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        return {"segments": [{"offset": 0.0, "text": "hello"}], "lang": "en"}

    job = _run_sweep(monkeypatch, videos, fetcher)
    _simulate_restart()

    recovered = asyncio.run(archive.archive_search_deep_status(job["id"]))
    assert recovered["age_parked"] == 0


def test_recovered_job_count_survives_a_fresh_process_boot(monkeypatch, fast_pace, no_session):
    """The count lives in the deep_jobs row, not in a process-lifetime set."""
    videos = [_video(GATED)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        raise RuntimeError(AGE_GATE_ERROR)

    job = _run_sweep(monkeypatch, videos, fetcher)
    _simulate_restart()

    # A fresh process re-runs the schema path; a deep_jobs table that predates
    # the age_parked column must gain it additively, and the row must survive.
    rows = archive_db.query("PRAGMA table_info(deep_jobs)")
    assert "age_parked" in {str(r["name"]) for r in rows}
    stored = archive_db.query(
        "SELECT age_parked FROM deep_jobs WHERE id=?", (job["id"],)
    )
    assert stored and int(stored[0]["age_parked"] or 0) == 1
    assert asyncio.run(archive.archive_search_deep_status(job["id"]))["age_parked"] == 1


def test_deep_jobs_table_gains_the_column_when_it_predates_it(tmp_path, monkeypatch):
    """A deep_jobs table created before the column must migrate, not fail.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so the
    additive ALTER is the only thing standing between an upgrading install and
    an OperationalError on the first sweep that persists a park count."""
    import sqlite3

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(str(legacy))
    con.executescript(
        """CREATE TABLE deep_jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'deep',
            handle TEXT NOT NULL, handle_norm TEXT NOT NULL, query TEXT NOT NULL,
            status TEXT NOT NULL, scanned INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0, cursor INTEGER,
            truncated INTEGER NOT NULL DEFAULT 0,
            no_transcript INTEGER NOT NULL DEFAULT 0, error TEXT,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    con.commit()
    con.close()

    # Rebind the archive DB the supported way (conftest documents the same
    # mechanism for per-module scratch DBs) and forget the per-path table cache
    # so the app takes the fresh-boot path against the legacy schema.
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(legacy))
    archive._deep_jobs_tables_ok.clear()

    archive._deep_jobs_put(
        {
            "id": "legacy1", "kind": "deep", "handle": "h", "handle_norm": "h",
            "query": "q", "status": "done", "scanned": 3, "total": 3,
            "no_transcript": 1, "age_parked": 2,
        }
    )

    con = sqlite3.connect(str(legacy))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT age_parked FROM deep_jobs WHERE id='legacy1'").fetchone()
    con.close()
    assert row is not None and int(row["age_parked"]) == 2

    # A row that predates the column defaults to 0 — that run's per-run count
    # was never persisted, and inventing one would be worse than saying 0. The
    # per-video parks themselves are untouched by this and still reported
    # through /api/archive/videos.
    con = sqlite3.connect(str(legacy))
    con.execute(
        "INSERT INTO deep_jobs (id, kind, handle, handle_norm, query, status, "
        "started_at, updated_at) VALUES ('old1','deep','h','h','q','done',"
        "'2024-01-01T00:00:00+00:00','2024-01-01T00:00:00+00:00')"
    )
    con.commit()
    legacy_row = con.execute("SELECT age_parked FROM deep_jobs WHERE id='old1'").fetchone()
    con.close()
    assert legacy_row is not None and int(legacy_row[0]) == 0


# --- the reason code is a contract, not prose ------------------------------


def test_parked_video_exposes_a_stable_reason_code(
    monkeypatch, fast_pace, no_session
):
    """The frontend must be able to branch on a CODE.

    captions_parked_reason is English prose; a client that has to substring-match
    it to tell 'no session' from 'a rejected session' breaks on a wording edit.
    """
    videos = [_video(GATED)]
    _seed(videos)

    def fetcher(vid: str) -> dict:
        raise RuntimeError(AGE_GATE_ERROR)

    _run_sweep(monkeypatch, videos, fetcher)
    _simulate_restart()

    assert _parked_code_from_api(GATED) == NO_SESSION
    assert _parked_reason_from_api(GATED), "the prose reason is still served"
    assert archive._age_parked_map()[GATED] == NO_SESSION
    assert archive_db.CAPTIONS_PARK_AGE_GATE_KINDS == (NO_SESSION, REJECTED_SESSION), (
        "the stored vocabulary must be exactly the two codes the API serves"
    )


def test_the_reason_text_is_derived_from_the_stored_code(no_session):
    """A rejected-session park keeps its text even if the probe would differ.

    The code records what was true when the video was gated; the sentence is
    rendered from it, so the UI never describes a state the row does not hold.
    """
    _seed([_video(GATED)])
    archive_db.mark_captions_unavailable("youtube", GATED, kind=REJECTED_SESSION)
    _simulate_restart()

    # The live session probe says "not configured"; the stored code is the
    # rejected-session one, and the text must follow the code.
    assert _parked_code_from_api(GATED) == REJECTED_SESSION
    reason = _parked_reason_from_api(GATED)
    assert "rejected" in reason.lower(), reason
    assert reason == archive._age_gate_park_text(REJECTED_SESSION)
    assert reason != archive._age_gate_park_text(NO_SESSION), (
        "the two codes must be distinguishable without matching prose"
    )
