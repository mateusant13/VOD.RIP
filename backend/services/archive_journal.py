"""Best-effort structured journal for background cadence / rate-limit decisions.

VOD.RIP's background work (scheduler, watchdog, transcribe worker, yt-dlp
egress) makes decisions the plain text log buries: a probe skipped because the
last check proved no new VOD, a platform cadence lowered because the learned
budget is dry, an exponential backoff armed after a 429. Reconstructing *why*
the archive went quiet from ``logs/background.log`` alone is a grep exercise.

This module is the durable, machine-readable record of exactly those decisions:
one JSON object per line, UTC timestamp, rotating at ~4 MiB. It is deliberately
NOT a general logger:

  * :func:`emit` is best-effort and never raises. A journal that can break the
    tick it observes is a new failure mode, not observability. Every failure is
    swallowed and (at most) debug-logged.
  * It is cheap on the hot path. A caller may pass ``dedupe_key`` +
    ``min_interval_s`` so a decision that repeats every pass (e.g. "budget
    still dry") lands once per window instead of once per tick.
  * It never holds a lock across IO longer than one small append.

The live journal path is ``<appdata>/logs/journal.jsonl`` (the same root
``services.error_log`` uses, so the log directory is not the data disk), with
``VODRIP_JOURNAL`` as the whole-path override and :func:`configure` for tests.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

_MAX_BYTES = 4 * 1024 * 1024
_TAIL_BYTES = 128 * 1024

_lock = threading.Lock()
_path_override: Optional[Path] = None
_path_cache: Optional[Path] = None
_last_emit: Dict[str, float] = {}


def configure(path: Optional[os.PathLike | str]) -> None:
    """Pin the journal path (tests / portable runs). None restores the default."""
    global _path_override, _path_cache
    with _lock:
        _path_override = Path(path) if path else None
        _path_cache = None


def journal_path() -> Path:
    """Resolve the journal file path. Cheap; cached after first resolution."""
    global _path_cache
    env = os.environ.get("VODRIP_JOURNAL", "").strip()
    if env:
        return Path(env)
    if _path_override is not None:
        return _path_override
    if _path_cache is not None:
        return _path_cache
    try:
        from services.settings import _get_appdata_dir

        resolved = _get_appdata_dir() / "logs" / "journal.jsonl"
    except Exception:  # noqa: BLE001 — never let path resolution break a tick
        resolved = Path("logs") / "journal.jsonl"
    _path_cache = resolved
    return resolved


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _deduped(dedupe_key: str, min_interval_s: float) -> bool:
    """True when this key fired inside its window and must be suppressed.

    Caller holds ``_lock``. Uses the monotonic clock so a wall-clock jump
    cannot silence (or un-silence) a repeating decision."""
    now = time.monotonic()
    if min_interval_s <= 0:
        _last_emit[dedupe_key] = now
        return False
    last = _last_emit.get(dedupe_key)
    if last is not None and now - last < float(min_interval_s):
        return True
    _last_emit[dedupe_key] = now
    return False


def _rotate(path: Path) -> None:
    """Keep one previous file once the journal passes the size ceiling."""
    try:
        if path.stat().st_size < _MAX_BYTES:
            return
        prev = path.with_suffix(path.suffix + ".1")
        try:
            prev.unlink()
        except OSError:
            pass
        path.replace(prev)
    except OSError:
        pass


def emit(
    kind: str,
    *,
    dedupe_key: Optional[str] = None,
    min_interval_s: float = 0.0,
    **fields: Any,
) -> bool:
    """Append one decision record. Returns True when a line was written.

    Never raises and never blocks the caller for more than one small append.
    ``dedupe_key`` + ``min_interval_s`` suppress a repeated decision inside the
    window (the record is dropped, not deferred)."""
    try:
        record = {"ts": _now_iso(), "kind": str(kind)}
        for key, value in fields.items():
            record[key] = value
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            if dedupe_key is not None and _deduped(str(dedupe_key), float(min_interval_s)):
                return False
            path = journal_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _rotate(path)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                return False
        return True
    except Exception:  # noqa: BLE001 — observability must never break the observed
        _log.debug("journal emit failed for %s", kind, exc_info=True)
        return False


def read_tail(limit: int = 200) -> List[Dict[str, Any]]:
    """Newest ``limit`` records, oldest-first. Best-effort; [] on any failure.

    Reads only a bounded tail (never the whole file), matching the project's
    hard rule for large journals."""
    try:
        path = journal_path()
        if not path.is_file():
            return []
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # drop a partial first line
            raw = fh.read().decode("utf-8", "replace").splitlines()
        out: List[Dict[str, Any]] = []
        for ln in raw[-max(1, int(limit)):]:
            try:
                out.append(json.loads(ln))
            except (ValueError, TypeError):
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


def reset() -> None:
    """Drop dedupe state (tests). Does not touch the file."""
    with _lock:
        _last_emit.clear()
