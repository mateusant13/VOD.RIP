"""Archive VOD retention — keep the newest N archived video FILES per
(platform, channel), and only ONE file per VOD that exists on several platforms.

Policy (owner decision, 2026-10-20):
  * keep the newest 8 archived files for each (platform, channel) pair;
  * a VOD mirrored on two platforms keeps a single file — the highest-priority
    platform wins (youtube > twitch > kick), matching the ingest/transcribe
    priority in archive_db._PLATFORM_TRANSCRIBE_PRIORITY;
  * transcripts and chat are NEVER deleted.

Only the video FILE is deleted. DB rows, transcripts, and chat stay forever:
an evicted row keeps all its metadata but flips back to status='known' with
archive_path cleared (the UI then shows the VOD as not archived, so a
re-archive stays possible). Idempotent: running it repeatedly deletes nothing
extra.

Grouping is per (platform, channel) on purpose: the old policy grouped by
platform alone, so one very busy channel could evict every file of a quiet
channel on the same platform.
"""

from __future__ import annotations

import logging

from services import archive_db

logger = logging.getLogger(__name__)

_DEFAULT_KEEP = 8  # tolerance for pre-settings state (tests, older builds)


def _keep_count() -> int:
    from deps import settings_mgr

    return int(getattr(settings_mgr.get(), "archive_vod_keep_count", _DEFAULT_KEEP) or _DEFAULT_KEEP)


def _archived_rows() -> list[dict]:
    """Every row that currently references an archived file, newest first."""
    return [
        dict(r)
        for r in archive_db.query(
            "SELECT * FROM videos "
            "WHERE archive_path IS NOT NULL AND archive_path != '' "
            "ORDER BY started_at DESC"
        )
    ]


def _canonical_keys() -> dict[tuple[str, str], str]:
    """(platform, video_id) -> canonical key for archived rows, alias override
    included (COALESCE(a.canonical_key, v.canonical_key)). One query, so a
    large archive does not pay a lookup per row."""
    keys: dict[tuple[str, str], str] = {}
    for raw in archive_db.query(
        """SELECT v.platform, v.video_id,
                  COALESCE(a.canonical_key, v.canonical_key) AS key
           FROM videos v
           LEFT JOIN video_aliases a USING (platform, video_id)
           WHERE v.archive_path IS NOT NULL AND v.archive_path != ''"""
    ):
        row = dict(raw)
        if row.get("key"):
            keys[(row["platform"], row["video_id"])] = row["key"]
    return keys


def _evict(row: dict) -> bool:
    """Drop this row's reference to its file; unlink when nothing else points
    at it. Returns True when a file was actually deleted."""
    path = row["archive_path"]
    evicted = dict(row)
    evicted["archive_path"] = None
    evicted["status"] = "known"
    archive_db.upsert_video(evicted)  # row stops referencing FIRST
    deleted = archive_db.release_archive_path(path)
    logger.info(
        "archive_retention: evicted %s/%s file %s (row kept, path cleared)",
        row["platform"],
        row["video_id"],
        path,
    )
    return deleted


def enforce_archive_vod_retention(keep_count: int | None = None) -> dict:
    """Enforce the file-retention policy described in the module docstring.

    Returns {"deleted_files", "cleared_rows"}.
    """
    if keep_count is None:
        keep_count = _keep_count()
    keep_count = max(0, int(keep_count))

    rows = _archived_rows()
    keys = _canonical_keys()
    doomed: dict[tuple[str, str], dict] = {}

    # 1. One FILE per VOD across platforms: the highest-priority mirror keeps
    #    its file, every other mirror's file is released. Tie-break: newest.
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = keys.get((row["platform"], row["video_id"]))
        if key:
            groups.setdefault(key, []).append(row)
    for members in groups.values():
        if len({m["platform"] for m in members}) < 2:
            continue
        members.sort(key=lambda m: m.get("started_at") or "", reverse=True)
        members.sort(key=lambda m: archive_db._PLATFORM_TRANSCRIBE_PRIORITY.get(m["platform"], 9))
        for duplicate in members[1:]:
            doomed[(duplicate["platform"], duplicate["video_id"])] = duplicate

    # 2. Newest N files per (platform, channel).
    by_channel: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if (row["platform"], row["video_id"]) in doomed:
            continue
        by_channel.setdefault((row["platform"], row.get("channel") or ""), []).append(row)
    for group in by_channel.values():
        group.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        for row in group[keep_count:]:
            doomed[(row["platform"], row["video_id"])] = row

    deleted_files = 0
    for row in doomed.values():
        if _evict(row):
            deleted_files += 1
    return {"deleted_files": deleted_files, "cleared_rows": len(doomed)}
