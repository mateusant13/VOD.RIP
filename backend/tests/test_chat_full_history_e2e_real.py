"""Real-network full-chat sweep — proves the offset-cursor walk runs PAST
the old ~6-minute head on a real multi-hour Twitch VOD.

Uses the archived 'caedrel' VOD 2837735467 (4.87 h, real chat) — the exact
VOD whose archive on the user's machine held ONLY the first ~16 min
(782 rows, offsets 14..947) after a pre-publication backfill run. The
sweep (the same _post_comments_page the app uses) must advance past
600 s (10 min) on real GQL pages. Bounded: max_messages caps the sweep at
~600 rows ≈ 30 pages ≈ 1 min with the 0.5 s page pacing.

Opt-in like every live suite: pytest -m real (the default config adds
`-m "not real"`). Skips inside the test when gql.twitch.tv is unreachable
(the probe also avoids slowing `-m not real` collections — no import-time
network).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="chat-full-history-real-"))
_DB = _TMP / "archive.db"

# Empty scratch DB: the app's own DDL is applied on first connect.
sqlite3.connect(str(_DB)).close()

from services import archive_db  # noqa: E402
from services import archive_twitch  # noqa: E402

pytestmark = pytest.mark.real

# A real multi-hour Twitch VOD with dense chat: caedrel's 4.87 h broadcast
# of 2026-08-05 (partnered → inside the 90-day retention window). The
# user's own archive held only offsets 14..947 for it — the exact partial
# capture this fix resumes.
REAL_VIDEO_ID = "2837735467"
REAL_CHANNEL = "caedrel"
REAL_DURATION_S = 17526.0


def _gql_reachable() -> bool:
    """Cheap probe of the public GQL endpoint (short timeout)."""
    import json
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "query": "query { user(login: \"caedrel\") { id } }",
    }).encode("utf-8")
    req = urllib.request.Request(
        archive_twitch.twitch_gql_service.TWITCH_GQL_URL,
        data=payload,
        headers={
            "Client-Id": archive_twitch.twitch_gql_service.TWITCH_GQL_CLIENT_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return not body.get("errors")
    except Exception:
        return False


@pytest.fixture(scope="module")
def _real_env():
    """Point archive_db at the scratch DB only when the test actually runs
    (a module-level write would leak VODRIP_ARCHIVE_DB into every pytest
    session at collection)."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def test_real_sweep_passes_10_minutes_of_chat(_real_env):
    """The full-chat sweep on a real multi-hour VOD must store chat well
    beyond the old ~6-minute head (bounded by max_messages)."""
    if not _gql_reachable():
        pytest.skip("gql.twitch.tv unreachable — live suite")

    archive_db.execute("DELETE FROM messages WHERE platform='twitch' AND video_id=?", (REAL_VIDEO_ID,))
    archive_db.execute("DELETE FROM archive_jobs WHERE video_id=?", (REAL_VIDEO_ID,))
    archive_db.upsert_video({
        "platform": "twitch",
        "video_id": REAL_VIDEO_ID,
        "channel": REAL_CHANNEL,
        "title": "LCK BRO VS NS (real full-chat sweep)",
        "started_at": "2026-08-05T07:24:51Z",
        "duration_sec": REAL_DURATION_S,
        "kind": "vod",
    })
    try:
        out = archive_twitch.backfill_chat(
            REAL_CHANNEL, REAL_VIDEO_ID, max_messages=600,
        )
        hi = archive_db.query(
            "SELECT MAX(offset_sec) m FROM messages "
            "WHERE platform='twitch' AND video_id=?",
            (REAL_VIDEO_ID,),
        )[0]["m"]
    finally:
        archive_db.execute("DELETE FROM archive_jobs WHERE video_id=?", (REAL_VIDEO_ID,))
        archive_db.execute("DELETE FROM messages WHERE platform='twitch' AND video_id=?", (REAL_VIDEO_ID,))
    assert out["inserted"] > 0, "the real sweep must store chat rows"
    assert out["stopped"] == "max_messages", "the test's own page cap, not the API"
    assert out["pages"] <= 40, "bounded: ~600 rows at ~20/page ≈ 30 pages"
    assert hi is not None and float(hi) > 600.0, (
        f"chat must cover past 10 min on the real 4.87 h VOD (got {hi}) — "
        "the old ~6-minute head cap is gone"
    )
