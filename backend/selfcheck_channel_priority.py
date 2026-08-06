"""Runnable self-check for archive-scheduler top-priority channel ordering.

Pure asserts, no framework. Uses a throwaway temp DB (VODRIP_ARCHIVE_DB) so
the live archive.db is never touched. Exercises the REAL mark/expiry
persistence, the pass-ordering helper with the live priority set, and the
REAL twitch backfill candidate query (worker patched to record spawn order
instead of fetching chat).

Run: python selfcheck_channel_priority.py   (from backend/)
"""
import os
import tempfile
import time
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
_db_file = Path(_tmp.name) / "archive.db"
# Pre-create so _migrate_db_to_data_dir treats the target as present and
# skips copying the real %APPDATA% archive (143MB) into the temp dir.
_db_file.touch()
os.environ["VODRIP_ARCHIVE_DB"] = str(_db_file)

from services import archive_db  # noqa: E402
from services import archive_scheduler as sched  # noqa: E402

# 1. mark -> inside the window AND persisted as a row (survives restarts).
archive_db.mark_channel_priority("twitch", "PrioChan")  # key lowercased
assert ("twitch", "priochan") in archive_db.priority_channel_keys()
rows = archive_db.query("SELECT platform, channel_key FROM channel_priorities")
assert any(r["platform"] == "twitch" and r["channel_key"] == "priochan" for r in rows), (
    "priority mark must be persisted in channel_priorities"
)

# 2. expiry: a negative window never enters the live set; lazy prune drops it.
archive_db.mark_channel_priority("kick", "gone", window_s=-1)
assert ("kick", "gone") not in archive_db.priority_channel_keys(), (
    "expired priority must not be in the live set"
)
assert archive_db.expire_channel_priorities() >= 1, "lazy prune must drop expired rows"

# 3. pass ordering fed by the live priority set: priority channel leads.
channels = [
    {"id": "b1", "twitchSlug": "backlogchan"},
    {"id": "hot", "twitchSlug": "priochan"},
    {"id": "b2", "twitchSlug": "zzbacklog"},
]
ordered = sched._ordered_channels(channels)
assert [c["id"] for c in ordered] == ["hot", "b1", "b2"], (
    "priority channel must lead the pass, backlog keeps saved order"
)

# 4. REAL twitch backfill candidate query: the OLDER prio VOD must be picked
#    before the NEWER backlog VOD (ORDER BY priority DESC, started_at ASC).
archive_db.upsert_video({
    "platform": "twitch", "video_id": "1111111111", "channel": "priochan",
    "title": "prio", "started_at": "2020-01-01T00:00:00+00:00",
})
archive_db.upsert_video({
    "platform": "twitch", "video_id": "2222222222", "channel": "backlogchan",
    "title": "backlog", "started_at": "2025-01-01T00:00:00+00:00",
})
spawned: list[str] = []
sched._backfill_one = lambda vid, ch: spawned.append(vid)  # record, don't fetch
sched._backfill_twitch_chat(channels)
deadline = time.monotonic() + 5.0
while len(spawned) < 2 and time.monotonic() < deadline:
    time.sleep(0.05)
assert spawned == ["1111111111", "2222222222"], (
    f"priority candidate must be backfilled before backlog, got {spawned}"
)

# 5. REAL youtube backfill candidate query (LEFT JOIN on channel_priorities
#    made the bare 'platform' column ambiguous — this probe guards that
#    exact regression). Older prio stream picked before newer backlog one.
with sched._backfill_lock:
    sched._backfill_inflight.clear()
archive_db.upsert_video({
    "platform": "youtube", "video_id": "aaaaaaaaaaa", "channel": "priochan",
    "title": "prio stream", "kind": "stream", "started_at": "2020-01-01T00:00:00+00:00",
})
archive_db.upsert_video({
    "platform": "youtube", "video_id": "bbbbbbbbbbb", "channel": "backlogchan",
    "title": "backlog stream", "kind": "stream", "started_at": "2025-01-01T00:00:00+00:00",
})
yt_spawned: list[str] = []
sched._backfill_one_youtube = lambda vid: yt_spawned.append(vid)
sched._backfill_youtube_chat()
deadline = time.monotonic() + 5.0
while len(yt_spawned) < 2 and time.monotonic() < deadline:
    time.sleep(0.05)
assert yt_spawned == ["aaaaaaaaaaa", "bbbbbbbbbbb"], (
    f"youtube priority stream must be backfilled first, got {yt_spawned}"
)

# Release the DB handle so the throwaway temp dir can be removed on exit
# (Windows locks open files).
with archive_db._lock:
    _conn = archive_db._conn
    archive_db._conn = None
    if _conn is not None:
        _conn.close()

print("selfcheck_channel_priority: OK")
