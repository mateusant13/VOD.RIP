"""Archive VOD retention — keep the newest N archived video FILES per platform.

User decision: archived VODs "grow only temporary, by days or by last 5".
Only the video file is deleted — DB rows, transcripts, and chat stay forever.
Evicted rows keep all metadata but flip back to status='known' with
archive_path cleared (the UI shows the VOD as not archived, so a re-archive
is possible). Idempotent: running it repeatedly deletes nothing extra.
"""

from __future__ import annotations

import logging
import os

from services import archive_db

logger = logging.getLogger(__name__)

_DEFAULT_KEEP = 5  # tolerance for pre-settings state (tests, older builds)


def _keep_count() -> int:
    from deps import settings_mgr

    return int(getattr(settings_mgr.get(), "archive_vod_keep_count", _DEFAULT_KEEP) or _DEFAULT_KEEP)


def enforce_archive_vod_retention(keep_count: int | None = None) -> dict:
    """Delete archived video files beyond the newest `keep_count` per platform.

    For each platform, rows with archive_path NOT NULL are sorted newest-first
    (started_at DESC, as list_videos returns them); every row past the count
    has its file unlinked (best-effort, existence-checked) and is then
    upserted with archive_path=None + status='known'. A row whose file is
    already gone still gets cleared — the path would lie otherwise. Rows
    within the count are never touched. Returns {"deleted_files", "cleared_rows"}.
    """
    if keep_count is None:
        keep_count = _keep_count()
    keep_count = max(0, int(keep_count))
    deleted_files = 0
    cleared_rows = 0
    for platform in archive_db.PLATFORMS:
        archived = [v for v in archive_db.list_videos(platform) if v.get("archive_path")]
        for row in archived[keep_count:]:
            path = row["archive_path"]
            if os.path.exists(path):
                try:
                    os.unlink(path)
                    deleted_files += 1
                except OSError:
                    logger.warning(
                        "archive_retention: could not delete %s (%s)", path, platform
                    )
            evicted = dict(row)
            evicted["archive_path"] = None
            evicted["status"] = "known"
            archive_db.upsert_video(evicted)
            cleared_rows += 1
            logger.info(
                "archive_retention: evicted %s file %s (row kept, path cleared)",
                platform,
                row["video_id"],
            )
    return {"deleted_files": deleted_files, "cleared_rows": cleared_rows}
