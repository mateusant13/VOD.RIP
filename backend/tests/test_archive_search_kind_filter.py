"""videos.kind filter tests for the search passes, focused on the virtual
'video' kind (the FE's VIDEO chip = YouTube /videos-tab uploads).

The chip is virtual because a YouTube /videos-tab upload reaches the index as
kind='vod' (deep-sweep path: routers/archive.py maps content_kind 'video' ->
'vod') or as literal kind='video' (channel-index path: routers/channels.py),
so 'video' cannot be a plain equality on videos.kind. Before this fix the virtual clause read
`youtube AND kind NOT IN ('short','clip')`, which also admitted 'live' and
'stream' — selecting VIDEO on the live archive returned recorded broadcasts
('#OUTRA LIVE AI', 'AO VIVO | MAC 16'), the bug these tests pin.

Every path that can emit a hit is exercised: the title pass (source='video'),
the transcripts FTS loop, the messages FTS loop, the chat-author-only mode,
and the cross-segment span pass (the span pass and the semantic concept pass
share _append_content_filters; the semantic pass needs an embedding backend,
so it is covered through that shared filter).

Run from backend/: python -m pytest tests/test_archive_search_kind_filter.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="vodrip-archive-search-kind-"))
_DB = _TMP / "archive.db"
sqlite3.connect(str(_DB)).close()

# The env var MUST be set before the first services.archive_db import in the
# session (module-level self-check opens the DB on import); conftest.py
# guarantees the real %APPDATA% archive.db is never the target.
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402

WORD = "kindchip"
CHAN = "kindchipchan"

# (video_id, platform, stored kind) — one YouTube row per stored kind, a
# Twitch VOD and a Kick row that carries the literal 'video' kind (the
# virtual token must never match platform-agnostically).
CASES = [
    ("kind-yt-video", "youtube", "video"),
    ("kind-yt-vod", "youtube", "vod"),
    ("kind-yt-stream", "youtube", "stream"),
    ("kind-yt-live", "youtube", "live"),
    ("kind-yt-short", "youtube", "short"),
    ("kind-yt-clip", "youtube", "clip"),
    ("kind-tw-vod", "twitch", "vod"),
    ("kind-ki-video", "kick", "video"),
]
AUTHOR = "kindchipuser"


@pytest.fixture(scope="module", autouse=True)
def _kind_scratch_db():
    """Rebind the global connection to THIS module's scratch DB regardless of
    import or collection order (later modules clobber the env var at import
    time)."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def _seed() -> None:
    """Idempotent: wipe this module's rows, then re-insert the matrix.

    Each case carries a title token, one transcript segment and one chat row,
    so every emitting pass has content to hit for the same query word."""
    archive_db.execute("DELETE FROM messages WHERE video_id LIKE 'kind-%'")
    archive_db.execute("DELETE FROM transcripts WHERE video_id LIKE 'kind-%'")
    archive_db.execute("DELETE FROM videos WHERE video_id LIKE 'kind-%'")
    for vid, platform, kind in CASES:
        archive_db.upsert_video({
            "platform": platform,
            "video_id": vid,
            "channel": CHAN,
            "title": f"{WORD} title {vid}",
            "started_at": "2026-08-01T12:00:00Z",
            "kind": kind,
        })
        archive_db.insert_transcript(platform, vid, [{
            "seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0,
            "text": f"{WORD} spoken in {vid}",
        }])
        archive_db.insert_messages(platform, vid, [{
            "offset_sec": 1.0, "username": AUTHOR,
            "text": f"{WORD} typed in {vid}",
        }])


def _ids(hits: list[dict]) -> set[str]:
    return {h["video_id"] for h in hits}


# The virtual chip's membership over the seeded matrix: YouTube uploads only
# (stored 'video' or the long-form default 'vod'), never shorts/clips/lives/
# streams, never another platform's VOD.
YT_UPLOADS = {"kind-yt-video", "kind-yt-vod"}


def test_kind_video_returns_only_youtube_uploads():
    _seed()
    hits = archive_db.search(WORD, kind="video", limit=100)
    assert hits, "kind=video must still find YouTube uploads"
    assert _ids(hits) == YT_UPLOADS
    # Belt and braces on the stored kind of every returned hit.
    assert {h["video_kind"] for h in hits} <= {"video", "vod"}
    for leaked in ("kind-yt-stream", "kind-yt-live", "kind-yt-short",
                   "kind-yt-clip", "kind-tw-vod", "kind-ki-video"):
        assert leaked not in _ids(hits), f"{leaked} must never match kind=video"


@pytest.mark.parametrize(
    "source", ["video", "transcript", "chat"], ids=["title", "transcripts", "messages"]
)
def test_kind_video_holds_on_every_hit_emitting_pass(source):
    """Each source subset isolates one emitting pass; all three leaked
    stream/live rows before the shared clause was narrowed."""
    _seed()
    hits = archive_db.search(WORD, kind="video", source=source, limit=100)
    assert _ids(hits) == YT_UPLOADS


def test_kind_video_holds_in_username_only_mode():
    """Empty q + username = chat-author history; it shares the same clause."""
    _seed()
    hits = archive_db.search("", kind="video", username=AUTHOR, limit=100)
    assert _ids(hits) == YT_UPLOADS


def test_span_pass_respects_virtual_video_kind():
    """Phrase split across two ADJACENT transcript segments (the span scan).

    Called directly: inside search() the span pass only fires when no
    within-row phrase matched, and the semantic concept pass shares its
    _append_content_filters fragment (no embedding backend offline, so the
    span scan is the runnable proof of that shared filter)."""
    _seed()
    for vid, platform, kind in CASES:
        archive_db.insert_transcript(platform, vid, [
            # seg N must END with the phrase prefix, seg N+1 START with the
            # remainder.
            {"seg_idx": 1, "start_sec": 10.0, "end_sec": 11.0,
             "text": f"intro {WORD}"},
            {"seg_idx": 2, "start_sec": 12.0, "end_sec": 13.0,
             "text": "spantail tail"},
        ])
    try:
        rows = archive_db._phrase_span_rows(
            [WORD, "spantail"], 100,
            platforms=[], video_id=None, channel=CHAN, kinds=["video"],
            date_from=None, date_to=None, lang=None, want_yt_video=True,
        )
        assert _ids(rows) == YT_UPLOADS
        # Unfiltered control: the same scan finds every kind.
        every = archive_db._phrase_span_rows(
            [WORD, "spantail"], 100,
            platforms=[], video_id=None, channel=CHAN, kinds=[],
            date_from=None, date_to=None, lang=None, want_yt_video=False,
        )
        assert _ids(every) == {vid for vid, _p, _k in CASES}
    finally:
        archive_db.execute(
            "DELETE FROM transcripts WHERE text LIKE '%intro %' OR text LIKE 'spantail%'"
        )


def test_virtual_token_without_flag_is_still_a_filter():
    """kind='video' arriving as a stored-kind token alone must not degrade to
    "no kind predicate" (the token is stripped from the IN() list, so the
    virtual clause has to be derived from it)."""
    sql, params = archive_db._kind_match_sql(["video"], False, "v")
    assert sql is not None and "'youtube'" in sql and "stream" in sql
    assert params == []


def test_kind_vod_still_excludes_lives_and_streams():
    _seed()
    hits = archive_db.search(WORD, kind="vod", limit=100)
    assert _ids(hits) == {"kind-yt-vod", "kind-tw-vod"}
    assert "kind-ki-video" not in _ids(hits)


def test_kind_vod_video_union_is_youtube_uploads_plus_other_vods():
    _seed()
    hits = archive_db.search(WORD, kind="vod,video", limit=100)
    assert _ids(hits) == YT_UPLOADS | {"kind-tw-vod"}


def test_kind_multi_list_excludes_video_and_broadcasts():
    _seed()
    hits = archive_db.search(WORD, kind="vod,clip,short", limit=100)
    assert _ids(hits) == {"kind-yt-vod", "kind-yt-short", "kind-yt-clip", "kind-tw-vod"}


def test_kind_unset_returns_every_kind_unchanged():
    """No filter = no behavior change: every seeded kind still surfaces."""
    _seed()
    assert _ids(archive_db.search(WORD, limit=100)) == {vid for vid, _p, _k in CASES}
    # An empty-string kind is the same no-op contract.
    assert _ids(archive_db.search(WORD, kind="", limit=100)) == {
        vid for vid, _p, _k in CASES
    }


async def test_router_search_video_kind_excludes_broadcasts():
    """End-to-end contract through the route that served the owner's repro."""
    from routers.archive import archive_search

    _seed()
    resp = await archive_search(q=WORD, kind="video", limit=100, semantic=False)
    assert resp["hits"], "kind=video must not come back empty for a YouTube upload"
    assert _ids(resp["hits"]) == YT_UPLOADS
