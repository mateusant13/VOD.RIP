"""Content-hash dedup for archived media files (SHA-256, reference-counted).

The archive already dedupes by canonical_key (normalized title+date); this
is a second layer for byte-identical files under DISTINCT video rows (same
VOD downloaded twice under different ids, re-imports, re-ingests). When a
freshly written media file hashes equal to one already archived, the second
copy is dropped and both rows reference the ONE file.

Deletion is reference-counted at the DB layer (archive_db.release_archive_
path): a file is unlinked only when no videos row points at it. Archive rows
are the source of truth for references — this module only hashes and re-links.
"""

from __future__ import annotations

import hashlib
import logging
import os

from services import archive_db

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB streaming-hash chunks: constant memory on multi-GB VODs


def sha256_file(path: str) -> str:
    """Streaming SHA-256 of a media file (never loads it whole)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def register_archive_file(path: str, *, platform: str, video_id: str) -> dict:
    """Register a freshly written media file with content-hash dedup.

    Streams the hash, then either reuses an existing row's archive_path
    (the fresh copy is unlinked — both rows reference the ONE file) or
    keeps *path* as the file's home. The caller persists the returned
    {archive_path, content_sha256} on its own row.

    Returns {"archive_path", "content_sha256", "deduplicated": bool}.
    On a hash failure the file is stored as-is with content_sha256=None
    (dedup is best-effort, never blocks the archive)."""
    try:
        sha = sha256_file(path)
    except OSError as exc:
        logger.warning(
            "content dedup: cannot hash %s (%s); storing without hash", path, exc
        )
        return {"archive_path": path, "content_sha256": None, "deduplicated": False}
    existing = archive_db.find_content_duplicate(sha)
    if existing and os.path.isfile(existing["archive_path"]):
        try:
            os.unlink(path)
        except OSError:
            # ponytail: Windows can hold a lock on a just-written file;
            # keep the fresh copy (still hashed) rather than losing data —
            # content_duplicates() then surfaces the byte-identical pair.
            logger.warning(
                "content dedup: could not remove duplicate copy %s; keeping it", path
            )
            return {"archive_path": path, "content_sha256": sha, "deduplicated": False}
        logger.info(
            "content dedup: %s/%s shares bytes with %s/%s -> reuse %s",
            platform, video_id, existing["platform"], existing["video_id"],
            existing["archive_path"],
        )
        return {"archive_path": existing["archive_path"], "content_sha256": sha,
                "deduplicated": True}
    return {"archive_path": path, "content_sha256": sha, "deduplicated": False}
