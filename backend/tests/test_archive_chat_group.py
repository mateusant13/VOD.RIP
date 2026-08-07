"""Per-platform chat group endpoint tests against a scratch archive DB.

A canonical dedupe group (videos sharing one canonical_key, video_aliases
overrides included) merges every member's chat into one offset-ordered
stream; each row keeps its platform/video_id, pagination is a per-platform
keyset (`next_offsets` → `offsets` echo), and single-platform groups behave
exactly like the pre-group endpoint.

Run from backend/: python -m pytest tests/test_archive_chat_group.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="archive-chat-group-"))
_DB = _TMP / "archive.db"

# Empty scratch DB: the app's own DDL is applied on first connect.
sqlite3.connect(str(_DB)).close()

os.environ["VODRIP_ARCHIVE_DB"] = str(_DB)

from services import archive_db  # noqa: E402  (env must be set first)


@pytest.fixture(scope="module", autouse=True)
def _chat_scratch_db():
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


def _seed_group() -> None:
    """One canonical VOD mirrored on twitch + kick, 3 chat rows each,
    interleaved offsets (10,20,30,40,50,60)."""
    archive_db.execute("DELETE FROM messages")
    archive_db.execute("DELETE FROM video_aliases")
    archive_db.execute("DELETE FROM videos")
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "t1", "channel": "chan",
        "title": "VOD", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
        "canonical_key": "ck-1",
    })
    archive_db.upsert_video({
        "platform": "kick", "video_id": "k1", "channel": "chan",
        "title": "VOD", "started_at": "2026-08-01T00:00:00Z", "kind": "vod",
        "canonical_key": "ck-1",
    })
    archive_db.insert_messages("twitch", "t1", [
        {"offset_sec": 10.0, "username": "a", "text": "twitch 10"},
        {"offset_sec": 30.0, "username": "a", "text": "twitch 30"},
        {"offset_sec": 50.0, "username": "a", "text": "twitch 50"},
    ])
    archive_db.insert_messages("kick", "k1", [
        {"offset_sec": 20.0, "username": "b", "text": "kick 20"},
        {"offset_sec": 40.0, "username": "b", "text": "kick 40"},
        {"offset_sec": 60.0, "username": "b", "text": "kick 60"},
    ])


async def test_chat_group_merges_members_and_keeps_platform():
    from routers.archive import archive_chat_window

    _seed_group()
    resp = await archive_chat_window("twitch", "t1", offset=0.0, half=0, limit=5000)
    # Requested platform first, then the rest in dedupe order.
    assert resp["platforms"] == ["twitch", "kick"]
    assert [m["offset_sec"] for m in resp["messages"]] == [10, 20, 30, 40, 50, 60]
    for m in resp["messages"]:
        assert m["platform"] in ("twitch", "kick")
        assert m["video_id"] in ("t1", "k1")
    assert [m["platform"] for m in resp["messages"]] == [
        "twitch", "kick", "twitch", "kick", "twitch", "kick",
    ]
    assert resp["truncated"] is False
    assert resp["next_offsets"] == {"twitch": 50.0, "kick": 60.0}


async def test_chat_group_requesting_any_member_resolves_the_same_group():
    from routers.archive import archive_chat_window

    _seed_group()
    resp = await archive_chat_window("kick", "k1", offset=0.0, half=0, limit=5000)
    assert resp["platforms"] == ["kick", "twitch"]  # requested first
    assert [m["offset_sec"] for m in resp["messages"]] == [10, 20, 30, 40, 50, 60]


async def test_chat_group_per_platform_keyset_pagination_no_gaps():
    from routers.archive import archive_chat_window

    _seed_group()
    collected: list[tuple[str, float, str, str]] = []
    resume = ""
    truncated = True
    page = 0
    while truncated and page < 10:
        page += 1
        resp = await archive_chat_window(
            "twitch", "t1", offset=0.0, half=0, limit=3, offsets=resume or None,
        )
        for m in resp["messages"]:
            row = (m["platform"], m["offset_sec"], m["username"], m["text"])
            if row not in collected:
                collected.append(row)
        truncated = resp["truncated"]
        resume = ",".join(f"{p}:{o}" for p, o in resp["next_offsets"].items())
    # Deduped union covers the whole merged stream, offset-ordered, and the
    # page key always advanced (no infinite loop on a same-offset run).
    assert [r[1] for r in collected] == [10, 20, 30, 40, 50, 60]
    assert [r[0] for r in collected] == [
        "twitch", "kick", "twitch", "kick", "twitch", "kick",
    ]
    assert page > 1


async def test_chat_group_single_member_unchanged():
    from routers.archive import archive_chat_window

    _seed_group()
    archive_db.execute("DELETE FROM video_aliases")
    archive_db.execute("DELETE FROM videos WHERE platform='kick'")
    resp = await archive_chat_window("twitch", "t1", offset=25.0, half=0, limit=5000)
    assert resp["platforms"] == ["twitch"]
    assert [m["offset_sec"] for m in resp["messages"]] == [30, 50]
    assert all(m["platform"] == "twitch" and m["video_id"] == "t1" for m in resp["messages"])
    assert resp["truncated"] is False
    assert resp["next_offsets"] == {"twitch": 50.0}


async def test_chat_group_window_mode_merges_per_member():
    from routers.archive import archive_chat_window

    _seed_group()
    resp = await archive_chat_window("twitch", "t1", offset=25.0, half=10.0, limit=5000)
    # ±10s around 25 → twitch 30 + kick 20 only (10 and 40+ fall outside).
    assert {m["offset_sec"] for m in resp["messages"]} == {20, 30}
    assert resp["truncated"] is False
    assert resp["platforms"] == ["twitch", "kick"]


async def test_chat_group_honors_video_aliases_override():
    from routers.archive import archive_chat_window

    _seed_group()
    # The alias re-maps kick to a DIFFERENT canonical key — the group must
    # follow the override, so twitch is alone again.
    archive_db.set_alias("kick", "k1", "ck-other")
    resp = await archive_chat_window("twitch", "t1", offset=0.0, half=0, limit=5000)
    assert resp["platforms"] == ["twitch"]
    assert all(m["platform"] == "twitch" for m in resp["messages"])


async def test_chat_group_orphan_video_is_single_member():
    from routers.archive import archive_chat_window

    _seed_group()
    archive_db.execute("DELETE FROM video_aliases")
    archive_db.execute("UPDATE videos SET canonical_key = NULL")
    resp = await archive_chat_window("twitch", "t1", offset=0.0, half=0, limit=5000)
    assert resp["platforms"] == ["twitch"]
    assert all(m["platform"] == "twitch" for m in resp["messages"])


async def test_chat_group_unknown_offsets_segments_are_dropped():
    from routers.archive import archive_chat_window

    _seed_group()
    # Malformed / unknown-platform segments must not break the request; the
    # known kick segment still applies (kick resumes at 40 → 40,60 delivered
    # merged with twitch rows, sliced to the 3-row page).
    resp = await archive_chat_window(
        "twitch", "t1", offset=0.0, half=0, limit=3,
        offsets="youtube:5,garbage,kick:40",
    )
    assert [m["offset_sec"] for m in resp["messages"]] == [10, 30, 40]


def test_chat_for_and_slice_keep_platform_and_video_id():
    _seed_group()
    rows = archive_db.chat_for("twitch", "t1")
    assert len(rows) == 3
    assert all(r["platform"] == "twitch" and r["video_id"] == "t1" for r in rows)
    slice_rows, total = archive_db.chat_slice_for("twitch", "t1", None)
    assert total == 3
    assert all(r["platform"] == "twitch" and r["video_id"] == "t1" for r in slice_rows)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:cacheprovider", "--tb=short"])
