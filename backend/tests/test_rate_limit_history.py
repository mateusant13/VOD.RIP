"""rate_limit_events — the durable rate-limit history (foundation lane).

What this pins, in the order the foundation had to be trustworthy:

  1. the table exists on a FRESH db and on an OLD (pre-change) db, and the
     migration to it never touches videos/messages/transcripts;
  2. record -> recent round-trips, including the auto/user split that the
     whole adaptive-throttle goal rests on;
  3. the summary computes a real mean/p95 and stays per-platform;
  4. a DB failure while recording NEVER reaches the network caller;
  5. retention keeps a 24/7 table bounded;
  6. yt_gate/kick_gate arm EXACTLY as before (timing, longest-wins, the
     3-event escalation) and additionally leave a row behind;
  7. the two read-only endpoints answer.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="rate-limit-history-")) / "archive.db")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from services import archive_db, kick_gate, yt_gate  # noqa: E402


# --- fixtures --------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _scratch_db():
    """Pin the shared archive connection to THIS module's scratch DB so the
    assertions cannot see (or be polluted by) another module's rows."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="rate-limit-history-")) / "archive.db")
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
def _clean_events():
    """Empty rate_limit_events around every test (the gates write into it)."""
    archive_db.execute("DELETE FROM rate_limit_events")
    yt_gate.clear_youtube_gate()
    kick_gate.clear_kick_gate()
    yield
    # Teardown must tolerate a test that monkeypatched the write path
    # itself (that is the point of one of them).
    try:
        archive_db.execute("DELETE FROM rate_limit_events")
    except Exception:
        pass
    yt_gate.clear_youtube_gate()
    kick_gate.clear_kick_gate()


def _rows() -> list[dict]:
    return archive_db.recent_rate_limits(since_hours=24 * 30, limit=2000)


def _table_count() -> int:
    return archive_db.query("SELECT COUNT(*) FROM rate_limit_events")[0][0]


# --- 1. schema / migration -------------------------------------------------

def test_table_created_with_full_column_set():
    cols = {r[1] for r in archive_db.query("PRAGMA table_info(rate_limit_events)")}
    assert cols == {c for c, _ in archive_db._RL_EVENT_COLUMNS}, (
        "the recorder and the summary both read these by name"
    )


def test_events_survive_a_reopen(tmp_path):
    """Persist across a process restart — the whole point of the table."""
    db = tmp_path / "reopen.db"
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(db)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        assert archive_db.record_rate_limit(
            "youtube", "bot_gate", surface="download", origin="user",
            context="Sign in to confirm you're not a bot", backoff_s=1800.0,
        )
        # Simulate the restart: drop the connection, reopen the same file.
        archive_db._conn = None
        archive_db._schema_ready = False
        rows = archive_db.recent_rate_limits(platform="youtube")
        assert len(rows) == 1, "the event must outlive the process that saw it"
        assert rows[0]["kind"] == "bot_gate"
        assert rows[0]["origin"] == "user"
        assert rows[0]["backoff_s"] == 1800.0
    finally:
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
        archive_db._conn = None
        archive_db._schema_ready = False


# The pre-change schema: SCHEMA verbatim minus the rate_limit_events block.
# Derived from the live SCHEMA so the fixture cannot drift from it, and
# asserted below that the marker is still where this lane put it.
_SCHEMA_HEAD, _MARKER, _ = archive_db.SCHEMA.partition(
    "-- Rate-limit / bot-gate HISTORY")
assert _MARKER, "SCHEMA layout moved — update the old-schema fixture below"
_OLD_SCHEMA = _SCHEMA_HEAD
assert "rate_limit_events" not in _OLD_SCHEMA, (
    "the fixture must be the PRE-change schema, table absent"
)


def test_migrates_an_old_db_without_touching_content(tmp_path):
    """A DB written by the PREVIOUS build gains the table, loses nothing.

    Built with raw sqlite3 (not through archive_db) so the file genuinely
    has the old shape, then opened with the new code."""
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(_OLD_SCHEMA)
    conn.executescript(
        "INSERT INTO videos (platform, video_id, channel, title,"
        "                    created_at, updated_at) "
        "VALUES ('twitch', 'old-vid', 'chan', 'Old VOD',"
        "        '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00');"
        "INSERT INTO messages (platform, video_id, offset_sec, username, text) "
        "VALUES ('twitch', 'old-vid', 1.0, 'someone', 'pre-migration chat');"
        "INSERT INTO transcripts (platform, video_id, seg_idx, start_sec, end_sec, text) "
        "VALUES ('twitch', 'old-vid', 0, 0.0, 1.0, 'pre-migration transcript');"
    )
    conn.commit()
    assert "rate_limit_events" not in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }, "fixture must not already have the new table"
    conn.close()

    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(old_db)
    archive_db._conn = None
    archive_db._schema_ready = False
    try:
        # Touch the DB the way the app does.
        assert archive_db.record_rate_limit("kick", "http_403", surface="metadata")

        tables = {r[0] for r in archive_db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "rate_limit_events" in tables
        assert archive_db.video_channel("twitch", "old-vid") == "chan"
        assert archive_db.count_messages("twitch", "old-vid") == 1
        assert len(archive_db.transcript_for("twitch", "old-vid")) == 1, (
            "the migration must not rewrite transcripts"
        )
        assert len(_rows()) == 1
    finally:
        if prev is None:
            os.environ.pop("VODRIP_ARCHIVE_DB", None)
        else:
            os.environ["VODRIP_ARCHIVE_DB"] = prev
        archive_db._conn = None
        archive_db._schema_ready = False


def test_ensure_adds_a_column_to_an_earlier_build(monkeypatch):
    """The additive guard: a table from an earlier build gains the new field
    with an ALTER, not a rebuild."""
    with archive_db._lock:
        conn = archive_db.get_conn()
        conn.execute("DROP TABLE rate_limit_events")
        conn.execute(
            "CREATE TABLE rate_limit_events ("
            " id INTEGER PRIMARY KEY, ts TEXT NOT NULL, platform TEXT NOT NULL,"
            " kind TEXT NOT NULL)"
        )
        conn.commit()
    try:
        archive_db._ensure_rate_limit_events(conn=archive_db.get_conn())
        archive_db.get_conn().commit()
        cols = {r[1] for r in archive_db.query("PRAGMA table_info(rate_limit_events)")}
        assert {c for c, _ in archive_db._RL_EVENT_COLUMNS} <= cols
    finally:
        with archive_db._lock:
            conn = archive_db.get_conn()
            conn.executescript(archive_db.SCHEMA)
            conn.commit()


# --- 2. record / read round-trip -------------------------------------------

def test_record_and_recent_round_trip():
    assert archive_db.record_rate_limit(
        "youtube", "http_429", surface="metadata", origin="auto",
        context="too many requests", backoff_s=60.0, recent_requests=120,
        in_flight=3,
    )
    assert archive_db.record_rate_limit(
        "twitch", "proactive_low", surface="chat", origin="user",
        context="Ratelimit-Remaining: 2", recent_requests=7,
    )
    rows = _rows()
    assert len(rows) == 2
    yt = next(r for r in rows if r["platform"] == "youtube")
    assert yt["kind"] == "http_429" and yt["surface"] == "metadata"
    assert yt["origin"] == "auto"
    assert yt["recent_requests"] == 120 and yt["in_flight"] == 3
    assert yt["backoff_s"] == 60.0
    assert yt["context"] == "too many requests"
    assert yt["ts"]


def test_origin_distinction_is_queryable():
    """auto vs user is the field the adaptive-throttle goal rests on."""
    archive_db.record_rate_limit("youtube", "bot_gate", origin="auto")
    archive_db.record_rate_limit("youtube", "bot_gate", origin="user")
    assert [r["origin"] for r in _rows()] == ["user", "auto"], "newest first"

    summary = archive_db.rate_limit_summary(since_hours=24)
    auto = next(g for g in summary["groups"] if g["origin"] == "auto")
    user = next(g for g in summary["groups"] if g["origin"] == "user")
    assert auto["count"] == 1 and user["count"] == 1
    assert summary["totals"]["auto"] == 1
    assert summary["totals"]["user"] == 1


def test_recent_filters_by_platform_and_window():
    archive_db.record_rate_limit("youtube", "http_429")
    archive_db.record_rate_limit("kick", "http_403")
    assert len(archive_db.recent_rate_limits(platform="kick")) == 1
    assert len(archive_db.recent_rate_limits(platform="youtube")) == 1
    assert archive_db.recent_rate_limits(platform="twitch") == []
    assert len(archive_db.recent_rate_limits(since_hours=24)) == 2


def test_unknown_values_normalize_instead_of_raising():
    """A CHECK-constrained table must not throw on a surprise value — a new
    platform or a typo'd kind is recorded as 'other'."""
    assert archive_db.record_rate_limit("tiktok", "mystery", surface="weird")
    row = _rows()[0]
    assert row["platform"] == "other" and row["kind"] == "other"
    assert row["surface"] == "other" and row["origin"] == "auto"
    assert archive_db.record_rate_limit("twitch", "bot_gate", surface="live_status")
    assert _rows()[0]["surface"] == "live-status", "alias normalized"


# --- 3. the aggregate ------------------------------------------------------

def test_summary_mean_p95_and_per_platform():
    for load in (10, 20, 30, 100):
        archive_db.record_rate_limit(
            "youtube", "http_429", origin="auto", recent_requests=load,
        )
    archive_db.record_rate_limit("kick", "http_403", origin="auto", recent_requests=5)

    summary = archive_db.rate_limit_summary(since_hours=24)
    yt = next(g for g in summary["groups"] if g["platform"] == "youtube")
    assert yt["count"] == 4
    assert yt["requests_at_limit_mean"] == 40.0, "mean of 10/20/30/100"
    assert yt["requests_at_limit_p95"] == 100.0, "nearest-rank p95"
    assert yt["kinds"] == {"http_429": 4}
    assert yt["events_per_hour"] == round(4 / 24, 3)
    assert yt["first_seen"] <= yt["last_seen"]

    # per-platform: the Kick row must not be folded into the YouTube group.
    assert summary["totals"]["by_platform"] == {"kick": 1, "youtube": 4}
    assert next(
        g for g in summary["groups"] if g["platform"] == "kick"
    )["count"] == 1
    assert len(archive_db.rate_limit_summary(platform="kick")["groups"]) == 1


def test_summary_handles_unmeasured_load_honestly():
    """A gate cannot see request counts. That must read as 'unknown', never
    as a clean window of zero requests."""
    archive_db.record_rate_limit("youtube", "bot_gate", origin="auto")
    group = archive_db.rate_limit_summary()["groups"][0]
    assert group["count"] == 1
    assert group["requests_at_limit_mean"] is None
    assert group["requests_at_limit_p95"] is None
    assert group["observed_rate_per_min"] is None


# --- 4. a DB failure must never reach the caller --------------------------

def test_record_failure_does_not_propagate(monkeypatch):
    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(archive_db, "execute", _boom)
    assert archive_db.record_rate_limit("youtube", "http_429") is False


def test_gate_still_arms_when_recording_fails(monkeypatch):
    """The network caller survives a broken DB — and the gate still freezes."""
    from services import archive_db as adb

    monkeypatch.setattr(adb, "record_rate_limit", _raise_on_record)
    yt_gate.note_youtube_gate("Sign in to confirm you're not a bot", freeze_sec=120)
    assert yt_gate.youtube_gate_active() is True
    assert 119 <= yt_gate.gate_remaining_sec() <= 121
    assert _rows() == [], "the write failed, so nothing landed — and no raise"

    kick_gate.note_kick_gate_event("429 rate-limited on /api/v2/x")
    assert kick_gate.kick_gate_active() is True, "Kick gate is unaffected too"


def _raise_on_record(*_a, **_k):
    raise RuntimeError("archive_db is on fire")


# --- 5. retention ----------------------------------------------------------

def test_prune_keeps_the_table_bounded():
    for _ in range(5):
        archive_db.record_rate_limit("youtube", "http_429")
    assert _table_count() == 5
    # Backdate 3 of the 5 past the retention horizon.
    stale = [
        r["id"] for r in archive_db.query(
            "SELECT id FROM rate_limit_events ORDER BY id ASC LIMIT 3")
    ]
    archive_db.execute(
        "UPDATE rate_limit_events SET ts = '2000-01-01T00:00:00+00:00' WHERE id IN "
        f"({','.join('?' * len(stale))})",
        stale,
    )
    assert len(_rows()) == 2, "backdated rows drop out of the read window"
    assert _table_count() == 5, "but they are still on disk until pruned"
    assert archive_db.prune_rate_limit_events(max_age_days=30) == 3
    assert _table_count() == 2, "only the backdated rows went"
    assert archive_db.prune_rate_limit_events(max_age_days=30) == 0, "idempotent"


def test_prune_runs_amortized_not_per_event(monkeypatch):
    """Retention must not sit on the hot path: the DELETE is amortized to
    one call per _RL_PRUNE_EVERY recorded events."""
    import itertools

    calls = []
    monkeypatch.setattr(
        archive_db, "prune_rate_limit_events",
        lambda *a, **k: calls.append(1) or 0,
    )
    archive_db._rl_prune_counter = itertools.count(1)
    for _ in range(archive_db._RL_PRUNE_EVERY - 1):
        archive_db.record_rate_limit("youtube", "http_429")
    assert calls == [], "no prune in the first N-1 events"
    archive_db.record_rate_limit("youtube", "http_429")
    assert calls == [1], "exactly one prune on the Nth event"


# --- 6. the gates: same behaviour, plus a row ------------------------------

def test_yt_gate_arms_exactly_as_before_and_records():
    assert yt_gate.youtube_gate_active() is False
    yt_gate.note_youtube_gate("Sign in to confirm you're not a bot", freeze_sec=120)
    assert yt_gate.youtube_gate_active() is True
    assert 119 <= yt_gate.gate_remaining_sec() <= 121

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["platform"] == "youtube"
    assert rows[0]["kind"] == "bot_gate", "the marker maps to the bot-gate class"
    assert rows[0]["backoff_s"] == 120.0
    assert "Sign in to confirm" in rows[0]["context"]
    assert rows[0]["origin"] == "auto", "the conservative default"


def test_yt_gate_longest_wins_is_untouched():
    yt_gate.note_youtube_gate("first", freeze_sec=600)
    long_remaining = yt_gate.gate_remaining_sec()
    yt_gate.note_youtube_gate("shorter", freeze_sec=60)
    assert yt_gate.gate_remaining_sec() >= long_remaining - 1, (
        "a shorter freeze must not shorten an active one"
    )
    yt_gate.note_youtube_gate("longer", freeze_sec=900)
    assert yt_gate.gate_remaining_sec() > long_remaining, "a longer freeze extends"
    assert yt_gate.youtube_gate_active() is True


def test_yt_gate_kind_classification():
    cases = {
        "HTTP Error 429: Too Many Requests": "http_429",
        "rate-limited by youtube for an hour": "http_429",
        "Sign in to confirm you're not a bot": "bot_gate",
        "ERROR: [youtube] abc: Sign in to confirm you are not a bot": "bot_gate",
        "Preview unavailable for this video": "soft_neg",
        "unavailable due to captcha": "captcha",
    }
    for reason, kind in cases.items():
        assert yt_gate._classify_gate_kind(reason) == kind, reason
    yt_gate.clear_youtube_gate()
    yt_gate.note_youtube_gate("some localized playability wall", freeze_sec=60)
    assert _rows()[0]["kind"] == "bot_gate", "an unclassified gate signal is a gate"
    assert yt_gate.youtube_gate_active() is True


def test_yt_gate_surface_and_origin_are_threaded():
    yt_gate.note_youtube_gate(
        "Sign in to confirm you're not a bot",
        surface="metadata", origin="user", freeze_sec=60,
    )
    row = _rows()[0]
    assert row["surface"] == "metadata" and row["origin"] == "user"


def test_kick_gate_escalation_is_untouched_and_recorded():
    assert kick_gate.kick_gate_active() is False
    # Event 1 and 2: short cooldown, consecutive climbs to 2.
    kick_gate.note_kick_gate_event("403 on /api/v2/channels/x", kind="http_403")
    assert 59 <= kick_gate.gate_remaining_sec() <= 61, "first event is a short cooldown"
    assert _rows()[0]["kind"] == "http_403"
    assert _rows()[0]["backoff_s"] == 60.0
    kick_gate.note_kick_gate_event("403 on /api/v2/channels/y", kind="http_403")
    assert len(_rows()) == 2, "a second consecutive event is still recorded"

    # Event 3: escalates to the long freeze and resets the streak.
    kick_gate.note_kick_gate_event("429 rate-limited on /api/v2/z", kind="http_429")
    assert kick_gate.gate_remaining_sec() > 1000, "third consecutive event freezes"
    assert len(_rows()) == 3
    assert _rows()[0]["kind"] == "http_429"
    assert _rows()[0]["backoff_s"] == kick_gate._GATE_FREEZE_SEC

    # A success resets the streak, so the next event is a SHORT cooldown.
    kick_gate.note_kick_success()
    kick_gate.clear_kick_gate()
    kick_gate.note_kick_gate_event("403 on /api/v2/channels/z", kind="http_403")
    assert 59 <= kick_gate.gate_remaining_sec() <= 61


def test_kick_gate_classification():
    cases = {
        "403 on /api/v2/channels/x": "http_403",
        "429 rate-limited on /api/v2/channels/x": "http_429",
        "Cloudflare block": "http_403",
        "something else entirely": "other",
    }
    for reason, kind in cases.items():
        assert kick_gate._classify_gate_kind(reason) == kind, reason
    kick_gate.clear_kick_gate()
    kick_gate.note_kick_gate_event("an unlabelled kick failure")
    assert _rows()[0]["kind"] == "other"


# --- 7. read-only API ------------------------------------------------------

@pytest.fixture
def _client():
    from app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _get(client, url):
    # No `async with`: the ASGI transport needs no teardown and the same
    # client is reused across several calls in one test.
    r = await client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.anyio
async def test_recent_endpoint(_client):
    archive_db.record_rate_limit(
        "youtube", "bot_gate", surface="captions", origin="user", context="ctx",
    )
    body = await _get(_client, "/api/archive/rate-limits/recent?since_hours=24")
    assert body["count"] == 1 and body["since_hours"] == 24
    assert body["events"][0]["origin"] == "user"
    assert body["events"][0]["surface"] == "captions"

    scoped = await _get(
        _client, "/api/archive/rate-limits/recent?platform=kick&since_hours=24")
    assert scoped["count"] == 0, "platform filter is honored"

    # An unknown platform is a 400 (the router's _require_platform), not a 500.
    r = await _client.get(
        "/api/archive/rate-limits/recent?platform=nope&since_hours=24")
    assert r.status_code == 400, r.text


@pytest.mark.anyio
async def test_summary_endpoint(_client):
    archive_db.record_rate_limit(
        "youtube", "http_429", origin="auto", recent_requests=50)
    archive_db.record_rate_limit(
        "youtube", "http_429", origin="user", recent_requests=5)
    archive_db.record_rate_limit("kick", "http_403", origin="auto")

    body = await _get(_client, "/api/archive/rate-limits/summary?since_hours=24")
    assert body["since_hours"] == 24 and body["generated_at"]
    assert body["totals"]["events"] == 3
    assert body["totals"]["auto"] == 2 and body["totals"]["user"] == 1
    yt_auto = next(
        g for g in body["groups"]
        if g["platform"] == "youtube" and g["origin"] == "auto")
    assert yt_auto["count"] == 1
    assert yt_auto["requests_at_limit_mean"] == 50.0
    assert yt_auto["kinds"] == {"http_429": 1}

    scoped = await _get(
        _client, "/api/archive/rate-limits/summary?platform=youtube")
    assert {g["platform"] for g in scoped["groups"]} == {"youtube"}


@pytest.fixture
def anyio_backend():
    return "asyncio"
