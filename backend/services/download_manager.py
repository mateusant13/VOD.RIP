"""Download manager — manages download queue, progress, and cancellation."""

import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TYPE_CHECKING
from models.schemas import DownloadState
from services import ytdlp_service
from services.download_cleanup import delete_partial_output

if TYPE_CHECKING:
    from services.settings import SettingsManager

logger = logging.getLogger(__name__)

# Reserve the last 10% for merge/finish so progress never sits at 100% early.
DOWNLOAD_PROGRESS_CAP = 90


def _hook_progress_percent(d: dict) -> Optional[int]:
    """Normalize yt-dlp / ffmpeg / HLS hooks to 0–90% (reserve 10% for merge/done)."""
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
    pct = round(raw * DOWNLOAD_PROGRESS_CAP / 100.0)
    if pct == 0 and raw > 0:
        pct = 1
    return min(DOWNLOAD_PROGRESS_CAP, pct)


class DownloadManager:
    def __init__(self, max_workers: int = 4):
        self._downloads: Dict[str, DownloadState] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
        self._abort_fns: Dict[str, List[Callable[[], None]]] = {}
        self._cleanup_info: Dict[str, dict] = {}
        self._sse_queues: Dict[str, list] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()

    def start_download(
        self,
        url: str,
        output_file: str,
        quality: Optional[str] = None,
        oauth: Optional[str] = None,
        crop_start: Optional[float] = None,
        crop_end: Optional[float] = None,
        download_type: str = "video",
        download_func: Optional[Callable[..., str]] = None,
        settings_mgr: Optional["SettingsManager"] = None,
        # Pre-fetched metadata (so the queue UI can show the VOD info
        # immediately without a second round-trip).
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

        output_existed = os.path.isfile(output_file)
        abort_fns: List[Callable[[], None]] = []

        with self._lock:
            self._downloads[download_id] = state
            self._cancel_events[download_id] = cancel_event
            self._abort_fns[download_id] = abort_fns
            self._cleanup_info[download_id] = {
                "output_file": output_file,
                "output_existed": output_existed,
            }

        def _register_abort(fn: Callable[[], None]) -> None:
            abort_fns.append(fn)

        def _progress_hook(d):
            # Check cancellation from progress hook
            if cancel_event.is_set():
                raise ytdlp_service.CancelledError("Download cancelled by user")

            if d.get("status") == "downloading":
                pct = _hook_progress_percent(d)
                if pct is not None:
                    with self._lock:
                        state.progress = pct
                        state.status = f"Downloading {pct}%"
                    self._notify_sse(download_id, "progress", pct)
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
                state.status = "Downloading..."
                self._notify_sse(download_id, "status", "Downloading...")
                # Use the platform-specific download function if provided,
                # otherwise fall back to yt-dlp. Only forward parameters the
                # custom function actually accepts, so platform-specific
                # implementations don't have to mirror the full yt-dlp API.
                if download_func is not None:
                    import inspect
                    sig = inspect.signature(download_func)
                    kwargs = {
                        "url": url,
                        "output_path": output_file,
                        "quality": quality,
                        "oauth": oauth,
                        "crop_start": crop_start,
                        "crop_end": crop_end,
                        "progress_hook": _progress_hook,
                        "cancel_event": cancel_event,
                        "register_abort": _register_abort,
                    }
                    kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
                    output_file_result = download_func(**kwargs)
                else:
                    output_file_result = ytdlp_service.download_video_sync(
                        url=url,
                        output_path=output_file,
                        quality=quality,
                        oauth=oauth,
                        crop_start=crop_start,
                        crop_end=crop_end,
                        progress_hook=_progress_hook,
                        settings_mgr=settings_mgr,
                        cancel_event=cancel_event,
                        register_abort=_register_abort,
                    )
                if cancel_event.is_set():
                    raise ytdlp_service.CancelledError("Download cancelled by user")
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
            except ytdlp_service.CancelledError:
                with self._lock:
                    if state.status != "Cancelled":
                        state.status = "Cancelled"
                        self._notify_sse(download_id, "status", "Cancelled")
                _cleanup_output()
            except Exception as e:
                with self._lock:
                    state.status = "Failed"
                    state.error = str(e)
                self._notify_sse(download_id, "error", str(e))
                _cleanup_output()
            except BaseException as e:
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
                    self._sse_queues.pop(download_id, None)
                    self._cancel_events.pop(download_id, None)
                    self._abort_fns.pop(download_id, None)
                    self._cleanup_info.pop(download_id, None)
                    # Keep completed/failed downloads for querying; only remove cancelled
                    if state.status not in ("Completed", "Failed"):
                        self._downloads.pop(download_id, None)

        self._executor.submit(_download_worker)
        return download_id

    def cancel(self, download_id: str) -> bool:
        abort_fns: List[Callable[[], None]] = []
        cleanup: Optional[dict] = None
        with self._lock:
            event = self._cancel_events.get(download_id)
            state = self._downloads.get(download_id)
            if not event or not state or state.status in ("Completed", "Failed", "Cancelled"):
                return False
            event.set()
            state.status = "Cancelled"
            state.progress = 0
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
        self._notify_sse(download_id, "status", "Cancelled")
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
        _DONE = frozenset({"Completed", "Failed", "Cancelled"})
        with self._lock:
            items = sorted(
                self._downloads.values(),
                key=lambda d: d.started_at,
                reverse=True,
            )
        active = [d for d in items if d.status not in _DONE][:50]
        history = [d for d in items if d.status in _DONE][:50]
        return {"active": active, "history": history}

    def remove_history(self, download_id: str) -> bool:
        """Drop a finished download from the in-memory history list."""
        _DONE = frozenset({"Completed", "Failed", "Cancelled"})
        with self._lock:
            state = self._downloads.get(download_id)
            if not state or state.status not in _DONE:
                return False
            self._downloads.pop(download_id, None)
            return True

    def unregister_sse(self, download_id: str, queue):
        with self._lock:
            queues = self._sse_queues.get(download_id)
            if queues and queue in queues:
                queues.remove(queue)
            if not queues:
                self._sse_queues.pop(download_id, None)

    def register_sse(self, download_id: str, queue):
        with self._lock:
            if download_id in self._sse_queues:
                self._sse_queues[download_id].append(queue)

    def set_max_workers(self, max_workers: int) -> None:
        """Update the thread pool size. Existing tasks continue; new tasks use the new pool."""
        max_workers = max(1, min(16, int(max_workers)))
        if max_workers != self._executor._max_workers:
            self._executor.shutdown(wait=False)
            self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def apply_settings(self, settings_mgr: "SettingsManager") -> None:
        """Resize the download pool from saved settings."""
        self.set_max_workers(settings_mgr.get().download_threads)

    def _notify_sse(self, download_id: str, event_type: str, data):
        with self._lock:
            queues = list(self._sse_queues.get(download_id, []))
        for q in queues:
            try:
                q.put_nowait({"type": event_type, "data": data})
            except Exception:
                pass
