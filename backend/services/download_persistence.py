"""Download history and queue persistence layer.

Handles loading/saving download history and resumable queue entries
to disk, so paused or interrupted jobs survive server restarts.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from models.schemas import DownloadState
from services.download_utils import (
    _DONE_STATUSES,
    _HISTORY_MAX_ENTRIES,
    _QUEUE_PERSIST_INTERVAL,
)
from services.settings import _get_appdata_dir
from services.url_validation import is_sensible_vod_url

if TYPE_CHECKING:
    from services.settings import SettingsManager

logger = logging.getLogger(__name__)


class DownloadPersistence:
    """JSON-backed persistence for download history and resumable queue.

    Each ``DownloadManager`` creates one ``DownloadPersistence`` instance.
    All I/O is thread-safe via dedicated locks. Write failures are logged
    but never raised, so a disk error never interrupts a running download.
    """

    def __init__(self) -> None:
        self._history_dir = _get_appdata_dir()
        self._history_file = self._history_dir / "history.json"
        self._queue_file = self._history_dir / "queue.json"
        self._history_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._history, history_dirty = self._load_history()
        self._queue, queue_dirty = self._load_queue()
        self._queue_last_persist: Dict[str, float] = {}
        if history_dirty:
            self._save_history()
        if queue_dirty:
            self._save_queue()
        self._reconcile_queue_on_startup()

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    def _load_history(self) -> tuple[List[dict], bool]:
        """Load persisted history from disk. Corrupt or missing file -> ([], False)."""
        try:
            if not self._history_file.is_file():
                return [], False
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return [], False
            valid: List[dict] = []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if "download_id" not in entry or "status" not in entry:
                    continue
                if entry.get("status") not in _DONE_STATUSES:
                    entry["status"] = "Cancelled"
                valid.append(entry)
            filtered = [
                e for e in valid if is_sensible_vod_url(e.get("url", ""))
            ]
            dirty = len(filtered) < len(valid)
            if dirty:
                logger.info(
                    "Dropped %d invalid history entries on load",
                    len(valid) - len(filtered),
                )
            return filtered[:_HISTORY_MAX_ENTRIES], dirty
        # ponytail: I/O + JSON decode errors only
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            logger.exception("Failed to load download history; starting empty")
            return [], False

    def _save_history(self) -> None:
        with self._history_lock:
            snapshot = self._history[:_HISTORY_MAX_ENTRIES]
            try:
                self._history_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self._history_dir),
                    prefix="history_",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(snapshot, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, str(self._history_file))
                # ponytail: I/O errors only — re-raises to outer handler
                except (OSError, TypeError, ValueError):
                    if os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        # ponytail: cleanup survival — os.unlink
                        except OSError:
                            pass
                    raise
            # ponytail: I/O + serialization errors only
            except (OSError, TypeError, ValueError):
                logger.exception("Failed to persist download history")

    def record_history(self, state: DownloadState) -> None:
        """Insert/replace `state` in the in-memory history and flush to disk."""
        if state.status not in _DONE_STATUSES:
            return
        # ponytail: skip junk URLs that slipped into downloads (test VODs, single-digit ids)
        if not is_sensible_vod_url(state.url):
            logger.info("Skipping history entry for non-sensible URL: %s", state.url[:80])
            return
        payload = state.model_dump(mode="json")
        with self._history_lock:
            self._history = [
                e for e in self._history if e.get("download_id") != state.download_id
            ]
            self._history.insert(0, payload)
            self._history = self._history[:_HISTORY_MAX_ENTRIES]
        self._save_history()

    def drop_history(self, download_id: str) -> None:
        """Remove an entry from history and persist."""
        with self._history_lock:
            before = len(self._history)
            self._history = [
                e for e in self._history if e.get("download_id") != download_id
            ]
            if len(self._history) == before:
                return
        self._save_history()

    # ------------------------------------------------------------------
    # Queue persistence
    # ------------------------------------------------------------------

    def _load_queue(self) -> tuple[List[dict], bool]:
        try:
            if not self._queue_file.is_file():
                return [], False
            data = json.loads(self._queue_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return [], False
            valid: List[dict] = []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if "download_id" not in entry or "url" not in entry:
                    continue
                valid.append(entry)
            filtered = [
                e for e in valid if is_sensible_vod_url(e.get("url", ""))
            ]
            dirty = len(filtered) < len(valid)
            if dirty:
                logger.info(
                    "Dropped %d invalid queue entries on load",
                    len(valid) - len(filtered),
                )
            return filtered[:_HISTORY_MAX_ENTRIES], dirty
        # ponytail: I/O + JSON decode errors only
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            logger.exception("Failed to load download queue; starting empty")
            return [], False

    def _save_queue(self) -> None:
        with self._queue_lock:
            snapshot = self._queue[:_HISTORY_MAX_ENTRIES]
            try:
                self._history_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self._history_dir),
                    prefix="queue_",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(snapshot, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, str(self._queue_file))
                # ponytail: I/O errors only — re-raises to outer handler
                except (OSError, TypeError, ValueError):
                    if os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        # ponytail: cleanup survival — os.unlink
                        except OSError:
                            pass
                    raise
            # ponytail: I/O + serialization errors only
            except (OSError, TypeError, ValueError):
                logger.exception("Failed to persist download queue")

    def _serializable_worker_params(self, params: dict) -> dict:
        skip = {"download_func", "settings_mgr"}
        out: dict[str, Any] = {}
        for key, value in params.items():
            if key in skip:
                continue
            if callable(value):
                continue
            out[key] = value
        return out

    def upsert_queue_entry(
        self,
        state: DownloadState,
        worker_params: dict | None = None,
    ) -> None:
        if state.status == "Completed":
            return
        # ponytail: skip junk URLs that slipped into the queue
        if not is_sensible_vod_url(state.url):
            logger.info("Skipping queue entry for non-sensible URL: %s", state.url[:80])
            return
        payload = state.model_dump(mode="json")
        if worker_params:
            payload["_params"] = self._serializable_worker_params(worker_params)
        with self._queue_lock:
            self._queue = [
                e for e in self._queue if e.get("download_id") != state.download_id
            ]
            self._queue.insert(0, payload)
            self._queue = self._queue[:_HISTORY_MAX_ENTRIES]
        self._save_queue()

    def remove_queue_entry(self, download_id: str) -> None:
        with self._queue_lock:
            before = len(self._queue)
            self._queue = [
                e for e in self._queue if e.get("download_id") != download_id
            ]
            if len(self._queue) == before:
                return
        self._queue_last_persist.pop(download_id, None)
        self._save_queue()

    def maybe_persist_queue_progress(
        self, download_id: str, state: DownloadState,
        worker_params: dict | None = None,
    ) -> None:
        """Persist queue progress at most every ``_QUEUE_PERSIST_INTERVAL``.

        Pass *worker_params* to preserve the download config in the
        persisted entry so interrupted downloads remain resumable.
        """
        now = time.monotonic()
        last = self._queue_last_persist.get(download_id, 0.0)
        if now - last < _QUEUE_PERSIST_INTERVAL:
            return
        self._queue_last_persist[download_id] = now
        self.upsert_queue_entry(state, worker_params)

    def _reconcile_queue_on_startup(self) -> None:
        """Mark orphaned in-flight jobs as Interrupted so the UI can resume."""
        changed = False
        with self._queue_lock:
            for entry in self._queue:
                status = entry.get("status", "")
                if status in _DONE_STATUSES or status in ("Paused", "Interrupted"):
                    continue
                entry["status"] = "Interrupted"
                changed = True
        if changed:
            self._save_queue()

    def queue_entry_to_state(self, entry: dict) -> DownloadState | None:
        try:
            data = {k: v for k, v in entry.items() if k != "_params"}
            return DownloadState(**data)
        # ponytail: Pydantic validation errors only
        except (ValueError, TypeError, RuntimeError):
            logger.debug("Skipping malformed queue entry", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Read helpers for queue / history
    # ------------------------------------------------------------------

    @property
    def history(self) -> List[dict]:
        with self._history_lock:
            return list(self._history)

    @property
    def queue(self) -> List[dict]:
        with self._queue_lock:
            return list(self._queue)
