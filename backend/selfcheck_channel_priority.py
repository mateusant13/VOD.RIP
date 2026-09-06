"""Runnable self-check for archive-scheduler top-priority channel ordering.

Pure asserts, no framework. Uses a throwaway temp DB (VODRIP_ARCHIVE_DB) so
the live archive.db is never touched. Exercises the REAL mark/expiry
persistence, the pass-ordering helper with the live priority set, and the
REAL twitch/youtube backfill candidate queries (the _enqueue_chat_job seam
is patched to record pick order instead of queueing jobs).

Run: python selfcheck_channel_priority.py   (from backend/)
"""
import os
import tempfile
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
_db_file = Path(_tmp.name) / "archive.db"
# Pre-create so _migrate_db_to_data_dir treats the target as present and
# skips copying the real %APPDATA% archive (143MB) into the temp dir.
_db_file.touch()
os.environ["VODRIP_ARCHIVE_DB"] = str(_db_file)

from services import archive_db  # noqa: E402
from services import archive_scheduler as sched  # noqa: E402

_real_enqueue = sched._enqueue_chat_job

try:
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

    # 4. REAL twitch backfill candidate query: the NEWER prio VOD must be
    #    picked before the OLDER backlog VOD (ORDER BY priority DESC,
    #    started_at ASC — dates chosen so started_at alone picks the WRONG
    #    one, and the channels list is passed backlog-FIRST so an
    #    implementation that ignored the SQL ORDER BY and followed channel
    #    order would also fail). Since 880213f the scheduler queues chat
    #    jobs instead of fetching in-thread (_backfill_one is gone), so the
    #    pick order is observable at _enqueue_chat_job.
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "1111111111", "channel": "priochan",
        "title": "prio", "started_at": "2025-01-01T00:00:00+00:00",
    })
    archive_db.upsert_video({
        "platform": "twitch", "video_id": "2222222222", "channel": "backlogchan",
        "title": "backlog", "started_at": "2020-01-01T00:00:00+00:00",
    })
    enqueued: list[str] = []
    def _record(platform, video_id, **kw):
        enqueued.append(video_id)
        return True
    sched._enqueue_chat_job = _record
    sched._backfill_twitch_chat([channels[1], channels[2], channels[0]])  # hot NOT first
    assert enqueued == ["1111111111", "2222222222"], (
        f"priority candidate must be enqueued before backlog, got {enqueued}"
    )

    # 5. REAL youtube backfill candidate query (LEFT JOIN on channel_priorities
    #    made the bare 'platform' column ambiguous — this probe guards that
    #    exact regression). The join must match a YOUTUBE mark: newer prio
    #    stream picked before older backlog one (same inverted-date trick).
    archive_db.mark_channel_priority("youtube", "priochan")
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "aaaaaaaaaaa", "channel": "priochan",
        "title": "prio stream", "kind": "stream", "started_at": "2025-01-01T00:00:00+00:00",
    })
    archive_db.upsert_video({
        "platform": "youtube", "video_id": "bbbbbbbbbbb", "channel": "backlogchan",
        "title": "backlog stream", "kind": "stream", "started_at": "2020-01-01T00:00:00+00:00",
    })
    yt_enqueued: list[str] = []
    def _record_yt(platform, video_id, **kw):
        yt_enqueued.append(video_id)
        return True
    sched._enqueue_chat_job = _record_yt
    sched._backfill_youtube_chat()
    assert yt_enqueued == ["aaaaaaaaaaa", "bbbbbbbbbbb"], (
        f"youtube priority stream must be enqueued first, got {yt_enqueued}"
    )
finally:
    sched._enqueue_chat_job = _real_enqueue
    # Release the DB handles so the throwaway temp dir can be removed
    # (Windows locks open files). Reads use per-thread connections
    # (threading.local in archive_db) — the candidate queries above opened
    # one on THIS thread; closing only the global write conn leaves the WAL
    # locked. The check is single-threaded (enqueue seam patched, no real
    # worker spawned), so this thread's handles are all there are.
    with archive_db._lock:
        _conn = archive_db._conn
        archive_db._conn = None
        if _conn is not None:
            _conn.close()
    _rc = getattr(archive_db._local, "read_conn", None)
    if _rc is not None:
        _rc.close()
        archive_db._local.read_conn = None
        archive_db._local.read_conn_path = None
    _tmp.cleanup()

print("selfcheck_channel_priority: OK")
