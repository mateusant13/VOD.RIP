"""Download manager — manages download queue, progress, and cancellation."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Optional

from models.schemas import DownloadState
from services import ytdlp_service


class DownloadManager:
    def __init__(self, max_workers: int = 4):
        self._downloads: Dict[str, DownloadState] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
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
        )

        cancel_event = threading.Event()

        with self._lock:
            self._downloads[download_id] = state
            self._cancel_events[download_id] = cancel_event

        def _progress_hook(d):
            # Check cancellation from progress hook
            if cancel_event.is_set():
                raise ytdlp_service.CancelledError("Download cancelled by user")

            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                pct = int(downloaded / total * 100) if total > 0 else 0
                with self._lock:
                    state.progress = pct
                    state.status = f"Downloading {pct}%"
                self._notify_sse(download_id, "progress", pct)
            elif d.get("status") == "finished":
                with self._lock:
                    state.status = "Merging..."
                    state.progress = 99
                self._notify_sse(download_id, "status", "Merging...")

        def _cleanup_output():
            """Remove orphaned output and temp files on failure/cancellation."""
            if output_existed:
                return  # Don't touch files the user already had
            # Remove partial output file
            for candidate in (output_file, output_file + ".part", output_file + ".ytdl"):
                try:
                    if os.path.isfile(candidate):
                        os.remove(candidate)
                except OSError:
                    pass

        output_existed = os.path.isfile(output_file)

        def _download_worker():
            try:
                state.status = "Downloading..."
                self._notify_sse(download_id, "status", "Downloading...")
                output_file_result = ytdlp_service.download_video_sync(
                    url=url,
                    output_path=output_file,
                    quality=quality,
                    oauth=oauth,
                    crop_start=crop_start,
                    crop_end=crop_end,
                    progress_hook=_progress_hook,
                )
                if output_file_result and os.path.isfile(output_file_result):
                    with self._lock:
                        state.status = "Completed"
                        state.progress = 100
                    self._notify_sse(download_id, "complete", 100)
                else:
                    raise RuntimeError("Download finished but output file is missing")
            except ytdlp_service.CancelledError:
                with self._lock:
                    state.status = "Cancelled"
                self._notify_sse(download_id, "status", "Cancelled")
                _cleanup_output()
            except Exception as e:
                with self._lock:
                    state.status = "Failed"
                    state.error = str(e)
                self._notify_sse(download_id, "error", str(e))
                _cleanup_output()
            finally:
                with self._lock:
                    self._sse_queues.pop(download_id, None)
                    self._downloads.pop(download_id, None)
                    self._cancel_events.pop(download_id, None)

        self._executor.submit(_download_worker)
        return download_id

    def cancel(self, download_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(download_id)
            state = self._downloads.get(download_id)
            if event and state and state.status not in ("Completed", "Failed", "Cancelled"):
                event.set()
                state.status = "Cancelling..."
                return True
        return False

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
        if max_workers != self._executor._max_workers:
            self._executor.shutdown(wait=False)
            self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def _notify_sse(self, download_id: str, event_type: str, data):
        with self._lock:
            queues = list(self._sse_queues.get(download_id, []))
        for q in queues:
            try:
                q.put_nowait({"type": event_type, "data": data})
            except Exception:
                pass
