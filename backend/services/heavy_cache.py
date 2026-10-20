"""Heavy per-channel cache lifecycle (owner policy, 2026-10-20).

A channel removed from `saved_channels` does NOT lose its heavy cache
immediately. The heavy artifact for a channel is the paired instant-preview
media (`<data_dir>/previews/<channel_id>.mp4` + `.json`, measured 4.6-7.0 MiB
each on this box). It is kept for a GRACE window so a removal by mistake can be
undone without paying the download again.

Policy, exactly as specified by the owner:

  * heavy cache survives **48 hours** after the channel is removed;
  * light cache (session snapshots / descriptors / DB rows / transcripts) is
    NOT part of this policy - it is never touched here;
  * re-adding the channel at any point **cancels** the pending deletion;
  * if the 48 hours have ALREADY elapsed when the app starts, a **one-hour
    countdown** starts at that moment and the cache is deleted when it expires.

Why the countdown exists: the 48h clock needs somebody to check it. On a box
that was closed for a week, "48h elapsed" is true for every orphan at once, and
deleting them on boot would be a surprise attack; the hour gives the owner a
window to re-add a channel and keep its media.

Scope note: per-VOD muxed media (kd_preview session dirs, preflight heads) is
governed by archive_retention's newest-8-per-(platform, channel) rule, which is
already bounded and is not channel-scoped. This ledger only owns the
channel-keyed artifact, so it cannot delete more than the channel preview pair.

The ledger is a small JSON next to the previews it describes, so it moves with
the data drive. Every operation is best-effort: a corrupt ledger, a read-only
disk or a missing directory degrades to "no orphans" and never raises into a
settings save or the boot path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

HEAVY_GRACE_SEC = 48 * 3600.0       # channel removed -> heavy cache must survive this
HEAVY_COUNTDOWN_SEC = 3600.0        # ...then, if already elapsed at boot, wait this long
HEAVY_SWEEP_INTERVAL_SEC = 300.0    # an open app must act when the countdown expires

_LEDGER_NAME = ".heavy-orphans.json"
_LOCK = threading.Lock()
_SWEEPER_THREAD: Optional[threading.Thread] = None


def previews_dir() -> Path:
    from services.instant_preview import previews_dir as _pd

    return _pd()


def ledger_path() -> Path:
    return previews_dir() / _LEDGER_NAME


def _now(now: Optional[float] = None) -> float:
    return float(now) if now is not None else time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _read() -> dict[str, dict[str, Any]]:
    """Ledger contents. Any failure is an empty ledger, never an exception."""
    path = ledger_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for cid, entry in raw.items():
        if not isinstance(cid, str) or not isinstance(entry, dict):
            continue
        try:
            orphaned_at = float(entry.get("orphaned_at") or 0)
        except (TypeError, ValueError):
            continue
        if orphaned_at <= 0:
            continue
        purge_at = entry.get("purge_at")
        try:
            purge_at = float(purge_at) if purge_at is not None else None
        except (TypeError, ValueError):
            purge_at = None
        out[cid] = {"orphaned_at": orphaned_at, "purge_at": purge_at}
    return out


def _write(entries: dict[str, dict[str, Any]]) -> None:
    """Atomic best-effort write. An empty ledger removes the file."""
    path = ledger_path()
    try:
        if not entries:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(entries, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("heavy cache ledger write failed", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001 - best effort
            pass


def _saved_channel_ids() -> set[str]:
    """Ids currently in saved_channels. Empty on any failure - callers that
    need certainty must not treat an empty set as 'nothing is saved'."""
    try:
        from deps import settings_mgr

        saved = getattr(settings_mgr.get(), "saved_channels", None) or []
    except Exception:  # noqa: BLE001 - settings unavailable (boot, tests)
        return set()
    return {str(c.get("id") or "").strip() for c in saved} - {""}


# --- policy surface ---------------------------------------------------------


def note_removed(channel_id: str, now: Optional[float] = None) -> None:
    """A channel left saved_channels: start (or keep) its 48h grace window.

    Idempotent: removing twice does not restart the clock, and an entry that is
    already counting down is left alone.
    """
    cid = str(channel_id or "").strip()
    if not cid:
        return
    with _LOCK:
        entries = _read()
        if cid not in entries:
            entries[cid] = {"orphaned_at": _now(now), "purge_at": None}
            _write(entries)
            logger.info("heavy cache: %s orphaned, kept for %.0fh", cid, HEAVY_GRACE_SEC / 3600)


def note_added(channel_id: str, now: Optional[float] = None) -> None:
    """The channel is back in saved_channels: cancel any pending deletion."""
    cid = str(channel_id or "").strip()
    if not cid:
        return
    with _LOCK:
        entries = _read()
        if entries.pop(cid, None) is not None:
            _write(entries)
            logger.info("heavy cache: %s re-added, deletion cancelled", cid)


def purge(channel_id: str) -> int:
    """Delete this channel's heavy files. Returns files removed."""
    cid = str(channel_id or "").strip()
    if not cid:
        return 0
    try:
        from services.instant_preview import _preview_paths

        mp4, sidecar = _preview_paths(cid)
    except Exception:  # noqa: BLE001 - bad id / no previews dir
        return 0
    removed = 0
    for path in (mp4, sidecar):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            logger.debug("heavy cache purge failed for %s", path, exc_info=True)
    if removed:
        logger.info("heavy cache: purged %s (%d file(s))", cid, removed)
    return removed


def sweep(now: Optional[float] = None) -> dict[str, Any]:
    """Advance every orphan one step. Called at boot and periodically.

    Returned shape is deliberate: callers (boot log, API, tests) need to see
    that a countdown STARTED before anything was deleted.
    """
    ts = _now(now)
    started: list[str] = []
    purged: list[str] = []
    cancelled: list[str] = []
    waiting: list[str] = []
    files_removed = 0

    with _LOCK:
        entries = _read()
        if not entries:
            return {"started": [], "purged": [], "cancelled": [], "waiting": [],
                    "files_removed": 0}
        saved = _saved_channel_ids()
        dirty = False
        for cid in sorted(entries):
            entry = entries[cid]
            # Defensive cancel: a channel that is saved must never be purged,
            # even if a note_added() was missed (settings rolled back by hand,
            # a ledger restored from a backup, a crash between the two writes).
            if cid in saved:
                entries.pop(cid, None)
                cancelled.append(cid)
                dirty = True
                logger.info("heavy cache: %s is saved again - deletion cancelled", cid)
                continue
            purge_at = entry.get("purge_at")
            if purge_at is not None:
                if ts >= purge_at:
                    removed = purge(cid)
                    files_removed += removed
                    entries.pop(cid, None)
                    purged.append(cid)
                    dirty = True
                else:
                    waiting.append(cid)
                continue
            if ts - float(entry["orphaned_at"]) >= HEAVY_GRACE_SEC:
                # 48h already gone: the app just opened, so the countdown
                # starts NOW, not retroactively.
                entry["purge_at"] = ts + HEAVY_COUNTDOWN_SEC
                started.append(cid)
                dirty = True
                logger.info(
                    "heavy cache: %s past the %.0fh grace - deleting in %.0f min",
                    cid, HEAVY_GRACE_SEC / 3600, HEAVY_COUNTDOWN_SEC / 60,
                )
            else:
                waiting.append(cid)
        if dirty:
            _write(entries)

    return {
        "started": started,
        "purged": purged,
        "cancelled": cancelled,
        "waiting": waiting,
        "files_removed": files_removed,
    }


def pending_ids() -> set[str]:
    """Channel ids the policy is currently protecting from an eager sweep.

    `instant_preview._drop_orphan_previews` deletes previews of channels that
    are not saved; without this the 48h grace would be dead code, because the
    next preview pass would delete the file minutes after the removal.
    """
    return set(_read())


def status(now: Optional[float] = None) -> list[dict[str, Any]]:
    """One row per orphan, oldest first - for the API/UI."""
    ts = _now(now)
    rows = []
    for cid, entry in sorted(_read().items(), key=lambda kv: kv[1]["orphaned_at"]):
        purge_at = entry.get("purge_at")
        rows.append({
            "channel_id": cid,
            "orphaned_at": _iso(entry["orphaned_at"]),
            "grace_remaining_sec": max(0.0, float(entry["orphaned_at"]) + HEAVY_GRACE_SEC - ts) if purge_at is None else 0.0,
            "purge_at": _iso(purge_at),
            "delete_in_sec": max(0.0, float(purge_at) - ts) if purge_at is not None else None,
        })
    return rows


# --- boot wiring ------------------------------------------------------------


def _sweeper_loop() -> None:
    while True:
        try:
            sweep()
        except Exception:  # noqa: BLE001 - the sweeper must outlive one bad pass
            logger.debug("heavy cache sweep failed", exc_info=True)
        time.sleep(HEAVY_SWEEP_INTERVAL_SEC)


def start_sweeper() -> bool:
    """Start the boot sweep + periodic sweeper. Idempotent; returns True when
    this call started it."""
    global _SWEEPER_THREAD
    with _LOCK:
        if _SWEEPER_THREAD is not None and _SWEEPER_THREAD.is_alive():
            return False
        thread = threading.Thread(
            target=_sweeper_loop, daemon=True, name="heavy-cache-sweeper"
        )
        _SWEEPER_THREAD = thread
    thread.start()
    return True
