"""Download manager — manages download queue, progress, cancellation, and pause.

Persists finished downloads (Completed / Failed / Cancelled) to
``appdata/VOD.RIP/history.json`` so the queue tab survives refreshes,
restarts, and crashes. Active downloads are not persisted: they are
re-derivable from the OS processes and user input, and a half-finished
``DownloadState`` on disk would be misleading (it would show 47% but
no live worker is touching it).
"""

import json
import logging
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from models.schemas import DownloadState
from services import ytdlp_service
from services.download_cleanup import delete_partial_output
from services.settings import _get_appdata_dir

if TYPE_CHECKING:
    from services.settings import SettingsManager

logger = logging.getLogger(__name__)

DOWNLOAD_PROGRESS_CAP = 90
_DONE_STATUSES = frozenset({"Completed", "Failed", "Cancelled"})
_HISTORY_MAX_ENTRIES = 200  # Bound on-disk growth; UI also caps at 50.




def _hook_progress_percent(d: dict) -> Optional[int]:
    if d.get("status") != "downloading":
        return None

    raw: Optional[float] = None
    if d.get("percent") is not None:
        raw = float(d["percent"])
    elif d.get("_percent") is not None:
        raw = float(d["_percent"])
    elif d.get("_percent_str"):
        try:
            raw = float(str(d["_percent_str"]).replace("%", "").strip())
        except ValueError:
            raw = None
    if raw is None:
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        if total and total > 0:
            raw = downloaded / float(total) * 100.0

    if raw is None:
        return None

    raw = max(0.0, min(100.0, raw))
    # FFmpeg concat/encode phases emit 91–100; do not rescale those to ~83%.
    if d.get("status") == "postprocessing" or raw >= 91.0:
        return min(99, round(raw))
    pct = round(raw * DOWNLOAD_PROGRESS_CAP / 100.0)
    if pct == 0 and raw > 0:
        pct = 1
    return min(DOWNLOAD_PROGRESS_CAP, pct)


class DownloadManager:
    def __init__(self, max_workers: int = 4):
        self._downloads: Dict[str, DownloadState] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
        self._pause_events: Dict[str, threading.Event] = {}
        self._abort_fns: Dict[str, List[Callable[[], None]]] = {}
        self._cleanup_info: Dict[str, dict] = {}
        self._worker_params: Dict[str, dict] = {}
        self._sse_queues: Dict[str, list] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        # Persistent history (Completed / Failed / Cancelled). Loaded on
        # construction so the queue tab is populated immediately on startup
        # — there is no flicker, no extra round-trip.
        self._history_dir = _get_appdata_dir()
        self._history_file = self._history_dir / "history.json"
        self._history_lock = threading.Lock()
        self._history: List[dict] = self._load_history()

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    def _load_history(self) -> List[dict]:
        """Load persisted history from disk. Corrupt or missing file -> []."""
        try:
            if not self._history_file.is_file():
                return []
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            # Drop entries that don't look like a valid DownloadState — that
            # way a hand-edited or partially-corrupt file is self-healing.
            valid: List[dict] = []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if "download_id" not in entry or "status" not in entry:
                    continue
                if entry.get("status") not in _DONE_STATUSES:
                    # An entry that was active when the server crashed is
                    # better surfaced as Cancelled than silently dropped —
                    # users can then see "it was running, what happened?".
                    entry["status"] = "Cancelled"
                valid.append(entry)
            return valid[:_HISTORY_MAX_ENTRIES]
        except Exception:
            logger.exception("Failed to load download history; starting empty")
            return []

    def _save_history(self) -> None:
        """Atomic write of self._history to disk. Errors are logged, not raised.

        We swallow failures so a write error never breaks the running
        download worker. Worst case the user loses the most recent entry.
        """
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
                except Exception:
                    if os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                    raise
            except Exception:
                logger.exception("Failed to persist download history")

    def _record_history(self, state: DownloadState) -> None:
        """Insert/replace `state` in the in-memory history and flush to disk."""
        if state.status not in _DONE_STATUSES:
            return
        payload = state.model_dump(mode="json")
        with self._history_lock:
            # Drop any prior entry for the same id (e.g. we updated the
            # status of an existing record from Cancelled -> ... ).
            self._history = [e for e in self._history if e.get("download_id") != state.download_id]
            self._history.insert(0, payload)
            self._history = self._history[:_HISTORY_MAX_ENTRIES]
        self._save_history()

    def _drop_history(self, download_id: str) -> None:
        """Remove an entry from history and persist."""
        with self._history_lock:
            before = len(self._history)
            self._history = [e for e in self._history if e.get("download_id") != download_id]
            if len(self._history) == before:
                return
        self._save_history()

    def start_download(
        url: str,
        output_file: str,
        quality: Optional[str] = None,
        oauth: Optional[str] = None,
        crop_start: Optional[float] = None,
        crop_end: Optional[float] = None,
        download_type: str = "video",
        download_func: Optional[Callable[..., str]] = None,
        settings_mgr: Optional["SettingsManager"] = None,
        title: Optional[str] = None,
        channel: Optional[str] = None,
        thumbnail: Optional[str] = None,
        duration: Optional[float] = None,
        duration_string: Optional[str] = None,
    ) -> str:
        download_id = f"dl_{uuid.uuid4().hex[:12]}"
        platform = ytdlp_service.detect_platform(url)
        state = DownloadState(
            download_id=download_id,
            url=url,
            type=download_type,
            platform=platform,
            status="Starting...",
            output_file=output_file,
            started_at=datetime.now(timezone.utc).isoformat(),
            quality=quality,
            crop_start=crop_start,
            crop_end=crop_end,
            title=title,
            channel=channel,
            thumbnail=thumbnail,
            duration=duration,
            duration_string=duration_string,
        )

        cancel_event = threading.Event()
        pause_event = threading.Event()
        output_existed = os.path.isfile(output_file)
        abort_fns: List[Callable[[], None]] = []

        worker_params = {
            "url": url,
            "output_file": output_file,
            "quality": quality,
            "oauth": oauth,
            "crop_start": crop_start,
            "crop_end": crop_end,
            "download_type": download_type,
            "download_func": download_func,
            "settings_mgr": settings_mgr,
            "title": title,
            "channel": channel,
            "thumbnail": thumbnail,
            "duration": duration,
            "duration_string": duration_string,
        }

        with self._lock:
            self._downloads[download_id] = state
            self._cancel_events[download_id] = cancel_event
            self._pause_events[download_id] = pause_event
            self._abort_fns[download_id] = abort_fns
            self._cleanup_info[download_id] = {
                "output_file": output_file,
                "output_existed": output_existed,
            }
            self._worker_params[download_id] = worker_params

        self._spawn_worker(download_id)
        return download_id

    def _spawn_worker(self, download_id: str) -> None:
        with self._lock:
            state = self._downloads.get(download_id)
            cancel_event = self._cancel_events.get(download_id)
            pause_event = self._pause_events.get(download_id)
            params = self._worker_params.get(download_id)
            if not state or not cancel_event or not pause_event or not params:
                return
            abort_fns: List[Callable[[], None]] = []
            self._abort_fns[download_id] = abort_fns
            output_file = params["output_file"]
            output_existed = self._cleanup_info[download_id]["output_existed"]

        def _register_abort(fn: Callable[[], None]) -> None:
            abort_fns.append(fn)

        def _progress_hook(d):
            if cancel_event.is_set():
                raise ytdlp_service.CancelledError("Download cancelled by user")
            if pause_event.is_set():
                raise ytdlp_service.PausedError("Download paused by user")

            if d.get("status") in ("downloading", "postprocessing"):
                pct = _hook_progress_percent(d)
                if pct is not None:
                    label = "Encoding" if d.get("status") == "postprocessing" else "Downloading"
                    with self._lock:
                        state.progress = pct
                        state.status = f"{label} {pct}%"
                    self._notify_sse(download_id, "progress", pct)
                    self._notify_sse(download_id, "status", state.status)
            elif d.get("status") == "finished":
                with self._lock:
                    state.status = "Merging..."
                    state.progress = 95
                self._notify_sse(download_id, "progress", 95)
                self._notify_sse(download_id, "status", "Merging...")

        def _cleanup_output():
            delete_partial_output(output_file, output_existed)

        def _download_worker():
            try:
                if cancel_event.is_set():
                    raise ytdlp_service.CancelledError("Download cancelled by user")
                if pause_event.is_set():
                    raise ytdlp_service.PausedError("Download paused by user")

                state.status = "Downloading..."
                self._notify_sse(download_id, "status", "Downloading...")

                download_func = params.get("download_func")
                if download_func is not None:
                    import inspect
                    sig = inspect.signature(download_func)
                    kwargs = {
                        "url": params["url"],
                        "output_path": output_file,
                        "quality": params.get("quality"),
                        "oauth": params.get("oauth"),
                        "crop_start": params.get("crop_start"),
                        "crop_end": params.get("crop_end"),
                        "progress_hook": _progress_hook,
                        "cancel_event": cancel_event,
                        "pause_event": pause_event,
                        "register_abort": _register_abort,
                        "settings_mgr": params.get("settings_mgr"),
                    }
                    kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
                    output_file_result = download_func(**kwargs)
                else:
                    output_file_result = ytdlp_service.download_video_sync(
                        url=params["url"],
                        output_path=output_file,
                        quality=params.get("quality"),
                        oauth=params.get("oauth"),
                        crop_start=params.get("crop_start"),
                        crop_end=params.get("crop_end"),
                        progress_hook=_progress_hook,
                        settings_mgr=params.get("settings_mgr"),
                        cancel_event=cancel_event,
                        pause_event=pause_event,
                        register_abort=_register_abort,
                    )

                if cancel_event.is_set():
                    raise ytdlp_service.CancelledError("Download cancelled by user")
                if pause_event.is_set():
                    raise ytdlp_service.PausedError("Download paused by user")

                if output_file_result and os.path.isfile(output_file_result):
                    size = os.path.getsize(output_file_result)
                    if size < ytdlp_service.MIN_VALID_OUTPUT_BYTES:
                        raise RuntimeError(
                            f"Output file too small ({size} bytes); download incomplete"
                        )
                    with self._lock:
                        state.status = "Completed"
                        state.progress = 100
                    self._notify_sse(download_id, "complete", 100)
                else:
                    raise RuntimeError("Download finished but output file is missing")
            except ytdlp_service.PausedError:
                with self._lock:
                    state.status = "Paused"
                self._notify_sse(download_id, "status", "Paused")
            except ytdlp_service.CancelledError:
                with self._lock:
                    if state.status != "Cancelled":
                        state.status = "Cancelled"
                        self._notify_sse(download_id, "status", "Cancelled")
                _cleanup_output()
            except Exception as e:
                if pause_event.is_set() and not cancel_event.is_set():
                    with self._lock:
                        state.status = "Paused"
                    self._notify_sse(download_id, "status", "Paused")
                    return
                with self._lock:
                    state.status = "Failed"
                    state.error = str(e)
                self._notify_sse(download_id, "error", str(e))
                _cleanup_output()
            except BaseException as e:
                if pause_event.is_set() and not cancel_event.is_set():
                    with self._lock:
                        state.status = "Paused"
                    self._notify_sse(download_id, "status", "Paused")
                    return
                logger.exception(
                    "Fatal download worker failure for %s", download_id, exc_info=e
                )
                with self._lock:
                    state.status = "Failed"
                    state.error = f"{type(e).__name__}: {e}"
                self._notify_sse(download_id, "error", state.error)
                _cleanup_output()
            finally:
                with self._lock:
                    final_state = (
                        self._downloads.get(download_id)
                        or DownloadState(
                            download_id=download_id,
                            url=params.get("url", ""),
                            type=params.get("download_type", "video"),
                            platform="Unknown",
                            status=state.status,
                            output_file=params.get("output_file", ""),
                            started_at=state.started_at,
                        )
                    )
                    if state.status == "Paused":
                        self._abort_fns[download_id] = []
                        return
                    self._sse_queues.pop(download_id, None)
                    self._cancel_events.pop(download_id, None)
                    self._pause_events.pop(download_id, None)
                    self._abort_fns.pop(download_id, None)
                    self._cleanup_info.pop(download_id, None)
                    self._worker_params.pop(download_id, None)
                    if state.status not in _DONE_STATUSES:
                        self._downloads.pop(download_id, None)
                # Persist outside the lock — disk write should never hold the
                # download state lock.
                if final_state.status in _DONE_STATUSES:
                    self._record_history(final_state)

    def pause(self, download_id: str) -> bool:
        abort_fns: List[Callable[[], None]] = []
        with self._lock:
            pause_event = self._pause_events.get(download_id)
            state = self._downloads.get(download_id)
            if not pause_event or not state:
                return False
            if state.status in _DONE_STATUSES or state.status == "Paused":
                return False
            pause_event.set()
            state.status = "Paused"
            abort_fns = list(self._abort_fns.get(download_id, []))

        for fn in abort_fns:
            try:
                fn()
            except Exception:
                pass
        self._notify_sse(download_id, "status", "Paused")
        return True

    def resume(self, download_id: str) -> bool:
        with self._lock:
            pause_event = self._pause_events.get(download_id)
            state = self._downloads.get(download_id)
            if not pause_event or not state or state.status != "Paused":
                return False
            pause_event.clear()
            state.status = "Starting..."
            state.error = None

        self._notify_sse(download_id, "status", "Starting...")
        self._spawn_worker(download_id)
        return True

    def cancel_all(self) -> int:
        with self._lock:
            active_ids = [
                download_id
                for download_id, state in self._downloads.items()
                if state.status not in _DONE_STATUSES
            ]
        cancelled = 0
        for download_id in active_ids:
            if self.cancel(download_id):
                cancelled += 1
        return cancelled

    def cancel(self, download_id: str) -> bool:
        abort_fns: List[Callable[[], None]] = []
        cleanup: Optional[dict] = None
        snapshot: Optional[DownloadState] = None
        with self._lock:
            event = self._cancel_events.get(download_id)
            pause_event = self._pause_events.get(download_id)
            state = self._downloads.get(download_id)
            if not event or not state or state.status in _DONE_STATUSES:
                return False
            if pause_event:
                pause_event.clear()
            event.set()
            state.status = "Cancelled"
            state.progress = 0
            # Snapshot before we pop — the worker's finally block will
            # re-record this, but if the worker is mid-cancel-during-shutdown
            # or otherwise never reaches its finally, this is our only
            # chance to write a Cancelled entry to history.
            snapshot = state.model_copy(deep=True)
            abort_fns = list(self._abort_fns.get(download_id, []))
            cleanup = self._cleanup_info.get(download_id)

        for fn in abort_fns:
            try:
                fn()
            except Exception:
                pass
        if cleanup:
            delete_partial_output(cleanup["output_file"], cleanup["output_existed"])
        with self._lock:
            self._downloads.pop(download_id, None)
            self._cancel_events.pop(download_id, None)
            self._pause_events.pop(download_id, None)
            self._abort_fns.pop(download_id, None)
            self._cleanup_info.pop(download_id, None)
            self._worker_params.pop(download_id, None)
            self._sse_queues.pop(download_id, None)
        self._notify_sse(download_id, "status", "Cancelled")
        if snapshot is not None:
            # Worker finally will _also_ try to record this; ``_record_history``
            # dedupes by id, so a double-write is a no-op rather than a duplicate.
            self._record_history(snapshot)
        return True

    def get(self, download_id: str) -> Optional[DownloadState]:
        with self._lock:
            return self._downloads.get(download_id)

    def get_all(self) -> list:
        with self._lock:
            return sorted(
                self._downloads.values(),
                key=lambda d: d.started_at,
                reverse=True,
            )[:50]

    def get_active_and_history(self) -> dict:
        """Return active + history lists, with history merged from disk.

        In-memory history is the source of truth for entries the current
        process touched; the on-disk file is the source of truth for
        entries from previous runs. We union by ``download_id`` and sort
        the union by ``started_at`` descending so the UI sees a single
        continuous list across restarts.
        """
        with self._lock:
            in_memory = list(self._downloads.values())
        active = [d for d in in_memory if d.status not in _DONE_STATUSES][:50]

        # Merge in-memory + on-disk, deduping by download_id (in-memory
        # wins for a given id so the freshest record is shown).
        in_memory_done = {d.download_id: d for d in in_memory if d.status in _DONE_STATUSES}
        with self._history_lock:
            disk_entries = list(self._history)
        merged: Dict[str, DownloadState] = dict(in_memory_done)
        for entry in disk_entries:
            did = entry.get("download_id")
            if not did or did in merged:
                continue
            try:
                merged[did] = DownloadState(**entry)
            except Exception:
                logger.debug("Skipping malformed history entry %s", did, exc_info=True)

        history = sorted(
            merged.values(),
            key=lambda d: d.started_at or "",
            reverse=True,
        )[:50]
        return {"active": active, "history": history}

    def remove_history(self, download_id: str) -> bool:
        removed = False
        with self._lock:
            state = self._downloads.get(download_id)
            if state and state.status in _DONE_STATUSES:
                self._downloads.pop(download_id, None)
                removed = True
        # Always try the disk side — even if the in-memory hit missed
        # (e.g. the entry only exists on disk), the user expects Delete
        # to be a hard remove.
        with self._history_lock:
            had_on_disk = any(e.get("download_id") == download_id for e in self._history)
        if had_on_disk:
            self._drop_history(download_id)
            removed = True
        return removed

    def unregister_sse(self, download_id: str, queue):
        with self._lock:
            queues = self._sse_queues.get(download_id)
            if queues and queue in queues:
                queues.remove(queue)
            if not queues:
                self._sse_queues.pop(download_id, None)

    def register_sse(self, download_id: str, queue) -> bool:
        with self._lock:
            state = self._downloads.get(download_id)
            if state is None:
                return False
            queues = self._sse_queues.setdefault(download_id, [])
            if queue in queues:
                return True
            if state.status in _DONE_STATUSES:
                snapshot = (
                    ("progress", 100 if state.status == "Completed" else state.progress),
                    ("status", state.status),
                )
            else:
                queues.append(queue)
                snapshot = (
                    ("progress", state.progress),
                    ("status", state.status),
                )
        for event_type, data in snapshot:
            try:
                queue.put_nowait({"type": event_type, "data": data})
            except Exception:
                pass
        return True

    def set_max_workers(self, max_workers: int) -> None:
        max_workers = max(1, min(16, int(max_workers)))
        if max_workers != self._executor._max_workers:
            self._executor.shutdown(wait=False)
            self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def apply_settings(self, settings_mgr: "SettingsManager") -> None:
        self.set_max_workers(settings_mgr.get().download_threads)

    def _notify_sse(self, download_id: str, event_type: str, data):
        with self._lock:
            queues = list(self._sse_queues.get(download_id, []))
        for q in queues:
            try:
                q.put_nowait({"type": event_type, "data": data})
            except Exception:
                pass
