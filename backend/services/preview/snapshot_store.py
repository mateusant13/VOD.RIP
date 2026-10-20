"""Disk tier for the warm session-snapshot cache.

The warm builds a complete session shell — session id, cache dir, master/entry
URL, the whole variant ladder, headers, hosts, mux bounds — and stashes it in
``_SESSION_SNAPSHOT``. That dict is RAM only: after a restart (or a code
reload) the shell is gone even though the muxed head chunks are still sitting
in ``preview_root()/preflight``. So the first click of every process paid a
full resolve again — measured 1,639-2,402 ms cold through browser + Vite proxy
on 2026-10-20, all of it metadata, none of it media.

This module persists that shell as JSON next to the media, so the click after
a restart finds it exactly like a warm click does: ``_reuse_youtube_snapshot``
rebuilds the session and ``resolve_stream_info`` is never called.

Two invariants:

* **A stale shell must MISS, never serve.** The upstream URLs inside it carry
  expiring signatures (googlevideo ~6 h, platform HLS similar). The TTL is the
  same one the RAM cache uses, so the RAM and disk tiers can never disagree
  about freshness.
* **This module never raises into the click path.** Every operation is
  best-effort: a missing dir, a half-written file, a video id with a path
  separator, a read-only disk — all degrade to "cache miss".

Type fidelity matters: ``allowed_hosts`` is a ``set`` and ``variant_entries``
is a list of ``(height, url)`` tuples, and the session builder consumes both.
JSON turns them into list/list-of-list, so :func:`_decode` restores them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from services.preview._state import preview_root

logger = logging.getLogger(__name__)

_SUBDIR = "snapshots"
_MAX_FILES = 800  # ~8 per (platform, channel) on this box, with headroom
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def _dir() -> Path:
    return preview_root() / _SUBDIR


def _path(vid: str, height: int) -> Path:
    return _dir() / f"{_SAFE_ID.sub('_', vid)}_{int(height or 0)}.json"


def _encode(snapshot: dict) -> dict:
    """JSON-safe copy. ``explore_yt_info`` is dropped on purpose: the reuse
    path never reads it, and it is the field most likely to hold something
    json cannot serialise."""
    payload = {k: v for k, v in snapshot.items() if k != "explore_yt_info"}
    if "allowed_hosts" in payload:
        payload["allowed_hosts"] = sorted(str(h) for h in (payload["allowed_hosts"] or []))
    if "variant_entries" in payload:
        payload["variant_entries"] = [
            [int(h or 0), str(u or "")] for h, u in (payload["variant_entries"] or [])
        ]
    return payload


def _decode(payload: dict) -> dict:
    if "allowed_hosts" in payload:
        payload["allowed_hosts"] = set(payload["allowed_hosts"] or [])
    if "variant_entries" in payload:
        payload["variant_entries"] = [
            (int(pair[0] or 0), str(pair[1] or ""))
            for pair in (payload["variant_entries"] or [])
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ]
    payload.setdefault("mux_status", "pending")
    return payload


def save(vid: str, height: int, snapshot: dict) -> None:
    """Persist one shell. Atomic: a crash mid-write leaves the old file."""
    if not vid or not snapshot:
        return
    try:
        target = _path(vid, height)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = {"saved_at": time.time(), "vid": vid, "height": int(height or 0),
                "snapshot": _encode(snapshot)}
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, target)  # atomic on Windows and POSIX
    except Exception:  # noqa: BLE001 — a cache write must never fail a preview
        logger.debug("snapshot_store: save failed vid=%s h=%s", vid, height, exc_info=True)


def load(vid: str, height: int, ttl_sec: float) -> Optional[dict]:
    """Disk tier for ``_get_session_snapshot``. Expired files are deleted."""
    if not vid:
        return None
    try:
        target = _path(vid, height)
        if not target.is_file():
            return None
        body = json.loads(target.read_text(encoding="utf-8"))
        if (time.time() - float(body.get("saved_at") or 0)) >= ttl_sec:
            target.unlink(missing_ok=True)
            return None
        snapshot = body.get("snapshot")
        return _decode(snapshot) if isinstance(snapshot, dict) else None
    except Exception:  # noqa: BLE001
        logger.debug("snapshot_store: load failed vid=%s h=%s", vid, height, exc_info=True)
        return None


def load_closest(vid: str, want_height: int, ttl_sec: float) -> Optional[tuple[int, dict]]:
    """Disk tier for ``_get_session_snapshot_closest``.

    Same scoring as the RAM version: smallest distance to ``want_height``,
    ties break toward the lower height (never upscale the first frame)."""
    if not vid:
        return None
    try:
        candidates = list(_dir().glob(f"{_SAFE_ID.sub('_', vid)}_*.json"))
    except Exception:  # noqa: BLE001
        return None
    want = int(want_height or 0)
    best: Optional[tuple[tuple[int, int, int], int, dict]] = None
    for path in candidates:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if (time.time() - float(body.get("saved_at") or 0)) >= ttl_sec:
                path.unlink(missing_ok=True)
                continue
            stored = int(body.get("height") or 0)
            snapshot = body.get("snapshot")
            if not isinstance(snapshot, dict):
                continue
        except Exception:  # noqa: BLE001
            continue
        score = (abs(stored - want), 0 if stored <= want else 1, -stored)
        if best is None or score < best[0]:
            best = (score, stored, _decode(snapshot))
    if best is None:
        return None
    return best[1], best[2]


def delete(vid: str, height: Optional[int] = None) -> None:
    """Drop one height, or every height, for *vid*."""
    if not vid:
        return
    try:
        targets = (
            [_path(vid, height)] if height is not None
            else list(_dir().glob(f"{_SAFE_ID.sub('_', vid)}_*.json"))
        )
        for target in targets:
            target.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug("snapshot_store: delete failed vid=%s", vid, exc_info=True)


def prune(keep_vids: set[str], ttl_sec: float) -> int:
    """Delete shells for videos outside *keep_vids* plus every expired file.

    Called by the retention pass so the disk tier follows the same "last N per
    (platform, channel)" rule as the media files. Returns the file count."""
    removed = 0
    keep = {_SAFE_ID.sub("_", v) for v in keep_vids}
    try:
        files = sorted(_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:  # noqa: BLE001
        return 0
    for index, path in enumerate(files):
        stem = path.name.rsplit("_", 1)[0]
        try:
            expired = (time.time() - path.stat().st_mtime) >= ttl_sec
        except OSError:
            expired = False
        if expired or index >= _MAX_FILES or (keep and stem not in keep):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
