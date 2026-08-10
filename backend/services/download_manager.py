"""Download manager — manages download queue, progress, cancellation, and pause.

Persists finished downloads to ``history.json`` and resumable queue entries
to ``queue.json`` via the ``DownloadPersistence`` helper class.
"""

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from models.schemas import DownloadState
from services import ytdlp_service
from services.download_cleanup import (
    delete_download_output,
    delete_partial_output,
    remove_temp_dirs,
)
from services.download_persistence import DownloadPersistence
from services.url_validation import is_sensible_vod_url
from services.download_utils import (
    DOWNLOAD_PROGRESS_CAP,
    POSTPROCESS_PROGRESS_FLOOR,
    POSTPROCESS_PROGRESS_SPAN,
    _DONE_STATUSES,
    _UNFINISHED_STATUSES,
    _RESUMABLE_STATUSES,
    _QUEUE_PERSIST_INTERVAL,
    _TRANSCODE_TIMEOUT_SEC,
    _download_timeout_seconds,
    _hook_progress_percent,
)
from services.oauth_crypto import decrypt_token
from services.ytdlp_download import kill_pp_state_procs, sanitize_download_error
from services.ytdlp_ffmpeg import (
    kill_download_ffmpeg_pids,
    reset_download_context,
    set_download_context,
)

if TYPE_CHECKING:
    from services.settings import SettingsManager

logger = logging.getLogger(__name__)

# Stall watchdog — a download that emits no *byte* progress for this long is
# dead (0 B/s). A true stall emits no progress hooks, so the hook-driven
# deadline can never fire for it; without this watchdog the process-wide
# yt-dlp lock stays held forever, blocking every other guarded yt-dlp op
# (preview extracts, other downloads, enrich).
STALL_WATCHDOG_SEC = 90.0
STALL_CHECK_INTERVAL_SEC = 10.0


def _stall_state(
    stall: dict, now: float, active: bool, stall_sec: float = STALL_WATCHDOG_SEC
) -> Optional[str]:
    """Return a stall error message when an *active* download has made no
    byte progress for >= ``stall_sec``, else None (latching the clock on the
    first armed check).

    Pure decision helper — unit tests drive it with a fake holder + fake
    clock, no network. ``stall`` mirrors the per-download holder written by
    the progress hook: ``armed`` (a hook has fired — startup extract does not
    count as stalled), ``last_bytes`` / ``last_move_wall`` (byte progress),
    ``error`` (latched once detected).
    """
    if not active or not stall.get("armed"):
        return None
    last = stall.get("last_move_wall")
    if last is None:
        stall["last_move_wall"] = now
        return None
    if (now - last) >= stall_sec:
        msg = stall.get("error") or "download stalled (0 B/s)"
        stall["error"] = msg
        return msg
    return None


def _stall_tick(
    stall: dict,
    now: float,
    active: bool,
    cancel_event: threading.Event,
    abort_fns: List[Callable[[], None]],
    stall_sec: float = STALL_WATCHDOG_SEC,
) -> bool:
    """One stall-watchdog interval action: returns True when a stall was
    detected and the download was cancelled (cancel event set + abort fns
    fired so ffmpeg children / the yt-dlp call unwind and the global yt-dlp
    lock frees). Mirrors the daemon loop's body so tests can drive the
    cancel wiring with a fake holder + fake events (no network)."""
    if not _stall_state(stall, now, active, stall_sec):
        return False
    cancel_event.set()
    for fn in abort_fns:
        try:
            fn()
        # ponytail: survival guarantee for arbitrary abort callbacks
        except Exception:
            pass
    return True


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
        # Persistent history + queue — loaded on construction so the queue
        # tab is populated immediately on startup.
        self._db = DownloadPersistence()

    def start_download(
        self,
        url: str,
        output_file: str,
        quality: Optional[str] = None,
        oauth: Optional[str] = None,
        crop_start: Optional[float] = None,
        crop_end: Optional[float] = None,
        download_type: str = "video",
        audio_only: bool = False,
        download_func: Optional[Callable[..., str]] = None,
        settings_mgr: Optional["SettingsManager"] = None,
        title: Optional[str] = None,
        channel: Optional[str] = None,
        thumbnail: Optional[str] = None,
        duration: Optional[float] = None,
        duration_string: Optional[str] = None,
        download_id: Optional[str] = None,
        estimated_bytes: Optional[int] = None,
        platform: Optional[str] = None,
    ) -> str:
        download_id = download_id or f"dl_{uuid.uuid4().hex[:12]}"
        resolved_platform = platform or ytdlp_service.detect_platform(url)
        state = DownloadState(
            download_id=download_id,
            url=url,
            type=download_type,
            platform=resolved_platform,
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
            "audio_only": audio_only,
            "download_func": download_func,
            "settings_mgr": settings_mgr,
            "title": title,
            "channel": channel,
            "thumbnail": thumbnail,
            "duration": duration,
            "duration_string": duration_string,
            "estimated_bytes": estimated_bytes,
            "platform": resolved_platform,
        }

        with self._lock:
            self._downloads[download_id] = state
            self._cancel_events[download_id] = cancel_event
            self._pause_events[download_id] = pause_event
            self._abort_fns[download_id] = abort_fns
            self._cleanup_info[download_id] = {
                "output_file": output_file,
                "output_existed": output_existed,
                "temp_dirs": [],
                "expected_duration": (
                    (crop_end - crop_start)
                    if (crop_start is not None and crop_end is not None
                        and crop_end > crop_start)
                    else None
                ),
            }
            self._worker_params[download_id] = worker_params

        self._db.upsert_queue_entry(state, worker_params)
        from services.youtube_diag import log_download
        trim = ""
        if crop_start is not None or crop_end is not None:
            trim = f"trim={crop_start}-{crop_end}"
        log_download(
            download_id,
            "queued",
            url=url,
            platform=platform,
            detail=f"type={download_type} quality={quality or 'default'} {trim}".strip(),
        )
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

        timeout_sec = _download_timeout_seconds(params.get("estimated_bytes"))
        job_started = time.monotonic()
        deadline_holder: dict[str, Optional[float]] = {
            "deadline": (job_started + timeout_sec) if timeout_sec else None,
        }
        # Stall watchdog state — written by the progress hook, read by a
        # daemon thread. Armed on the first hook event so a slow startup
        # extract (20-120s, no bytes) is never mistaken for a stall.
        stall_holder: dict = {
            "armed": False,
            "last_bytes": 0.0,
            "last_move_wall": None,
            "error": None,
        }
        stall_stop = threading.Event()

        def _enforce_deadline() -> None:
            deadline = deadline_holder["deadline"]
            if deadline is None or time.monotonic() <= deadline:
                return
            cancel_event.set()
            for fn in list(abort_fns):
                try:
                    fn()
                # ponytail: survival guarantee — deadline enforcement errors must not block cleanup
                except Exception:
                    pass
            elapsed_min = max(1, int((time.monotonic() - job_started) // 60))
            raise ytdlp_service.DownloadTimeoutError(
                f"Download timed out after {elapsed_min} minutes"
            )

        def _maybe_tighten_transcode_deadline(d: dict) -> None:
            enc = d.get("concat_encoder")
            if not enc or enc == "copy":
                return
            cap = time.monotonic() + _TRANSCODE_TIMEOUT_SEC
            cur = deadline_holder["deadline"]
            if cur is None or cap < cur:
                deadline_holder["deadline"] = cap
                logger.info(
                    "Download %s: transcode detected (%s) — %ds wall-clock cap",
                    download_id, enc, _TRANSCODE_TIMEOUT_SEC,
                )

        def _progress_hook(d):
            _maybe_tighten_transcode_deadline(d)
            _enforce_deadline()
            if cancel_event.is_set():
                raise ytdlp_service.CancelledError("Download cancelled by user")
            if pause_event.is_set():
                raise ytdlp_service.PausedError("Download paused by user")

            # Stall watchdog bookkeeping: any hook event arms the clock; only
            # *byte growth* (or a phase change) resets it — a retry loop that
            # keeps emitting 0-byte "downloading" events is still a stall.
            stall_holder["armed"] = True
            if d.get("status") == "downloading":
                db = d.get("downloaded_bytes")
                if (
                    isinstance(db, (int, float))
                    and db > 0
                    and float(db) != stall_holder.get("last_bytes")
                ):
                    stall_holder["last_bytes"] = float(db)
                    stall_holder["last_move_wall"] = time.monotonic()
            elif d.get("status") in ("finished", "postprocessing"):
                # Phase change / mux activity counts as progress; mux hangs
                # are bounded by the transcode deadline, not this watchdog.
                stall_holder["last_move_wall"] = time.monotonic()

            if d.get("status") in ("downloading", "postprocessing"):
                pct = _hook_progress_percent(d)
                if pct is not None:
                    if d.get("status") == "postprocessing":
                        label = str(d.get("phase") or "Encoding")
                    else:
                        label = "Downloading"
                    extras: list[str] = []
                    sp = d.get("speed")
                    if sp:
                        extras.append(str(sp))
                    eta = d.get("eta_seconds")
                    if isinstance(eta, (int, float)) and eta >= 0 and eta < 24 * 3600:
                        if eta >= 60:
                            mm, ss = divmod(int(eta), 60)
                            extras.append(f"ETA {mm}m{ss:02d}s")
                        else:
                            extras.append(f"ETA {int(eta)}s")
                    sep = " \u2022 "
                    suffix = f"{sep}{sep.join(extras)}" if extras else ""
                    with self._lock:
                        if pct < state.progress:
                            pct = state.progress
                        state.progress = pct
                        state.status = f"{label} {pct}%{suffix}"
                    self._notify_sse(download_id, "progress", pct)
                    self._notify_sse(download_id, "status", state.status)
                    self._db.maybe_persist_queue_progress(
                        download_id, state,
                        self._worker_params.get(download_id),
                    )
            elif d.get("status") == "finished":
                with self._lock:
                    state.status = "Finalising\u2026"
                    # Clip path emits finished after ffmpeg; jump to 100 if output exists.
                    out = params.get("output_file") or ""
                    if out and os.path.isfile(out) and os.path.getsize(out) >= 1024:
                        state.progress = 100
                    else:
                        state.progress = 99
                self._notify_sse(download_id, "progress", state.progress)
                self._notify_sse(download_id, "status", state.status)

        def _cleanup_output():
            expected_duration = None
            with self._lock:
                info = self._cleanup_info.get(download_id) or {}
                expected_duration = info.get("expected_duration")
            delete_partial_output(
                output_file, output_existed, expected_duration=expected_duration,
            )

        def _register_temp_dir(path: str) -> None:
            with self._lock:
                info = self._cleanup_info.setdefault(
                    download_id,
                    {"output_file": output_file, "output_existed": output_existed,
                     "temp_dirs": [], "expected_duration": None},
                )
                info.setdefault("temp_dirs", []).append(path)

        # PP-state progress poller
        pp_holder: dict = {}
        pp_stop = threading.Event()
        last_emit_pct = -1.0
        last_emit_wall = time.monotonic()
        poller_thread: Optional[threading.Thread] = None

        def _pp_progress_poller():
            nonlocal last_emit_pct, last_emit_wall
            while not pp_stop.is_set():
                pp_state = pp_holder.get("state")
                if pp_state is None:
                    pp_stop.wait(0.25)
                    continue
                with pp_state["lock"]:
                    pct = pp_state.get("last_percent") or 0.0
                    speed = pp_state.get("last_speed") or ""
                    eta = pp_state.get("last_eta_seconds")
                if pct > 0.0:
                    ui_pct = POSTPROCESS_PROGRESS_FLOOR + min(
                        POSTPROCESS_PROGRESS_SPAN, pct * POSTPROCESS_PROGRESS_SPAN,
                    )
                    now = time.monotonic()
                    d = None
                    if (
                        int(ui_pct) != int(last_emit_pct)
                        or (now - last_emit_wall) > 0.5
                    ):
                        last_emit_pct = ui_pct
                        last_emit_wall = now
                        d = {
                            "status": "postprocessing",
                            "percent": ui_pct,
                            "phase": "Muxing",
                            "phase_id": "encoding",
                            "speed": speed or None,
                            "eta_seconds": eta,
                        }
                    if d is not None:
                        try:
                            _progress_hook(d)
                        # ponytail: survival guarantee for PP poller
                        except Exception:
                            pp_stop.set()
                            return
                else:
                    now = time.monotonic()
                    if (now - last_emit_wall) > 3.0:
                        last_emit_wall = now
                        with self._lock:
                            hb_pct = max(1, state.progress)
                        d = {
                            "status": "postprocessing",
                            "percent": hb_pct,
                            "phase": "Muxing\u2026",
                            "phase_id": "encoding",
                            "heartbeat": True,
                        }
                        try:
                            _progress_hook(d)
                        # ponytail: survival guarantee for PP poller
                        except Exception:
                            pp_stop.set()
                            return
                pp_stop.wait(0.25)

        def _start_poller() -> None:
            nonlocal poller_thread
            if poller_thread is not None and poller_thread.is_alive():
                return
            poller_thread = threading.Thread(
                target=_pp_progress_poller, daemon=True,
                name=f"pp-poller-{download_id[:8]}",
            )
            poller_thread.start()

        def _stop_poller() -> None:
            pp_stop.set()
            if poller_thread is not None:
                poller_thread.join(timeout=2)

        def _register_pp_state(state: dict) -> None:
            pp_holder["state"] = state
            with self._lock:
                info = self._cleanup_info.get(download_id)
                if info is not None:
                    info["pp_state"] = state
            _start_poller()

        def _stall_watchdog_loop() -> None:
            """Cancel a download whose bytes have not moved for
            STALL_WATCHDOG_SEC — a true stall emits no progress hooks, so
            neither the hook-driven deadline nor a user cancel can ever fire.
            Cancelling frees the process-wide yt-dlp lock (held for the whole
            download) for preview extracts / other downloads."""
            while not stall_stop.wait(STALL_CHECK_INTERVAL_SEC):
                with self._lock:
                    st = self._downloads.get(download_id)
                active = (
                    st is not None
                    and st.status not in _DONE_STATUSES
                    and st.status != "Paused"
                )
                if not active:
                    continue
                if not _stall_tick(
                    stall_holder, time.monotonic(), True, cancel_event, abort_fns
                ):
                    continue
                logger.warning(
                    "Download %s: %s — cancelling to free the yt-dlp lock",
                    download_id, stall_holder["error"],
                )

        def _start_stall_watchdog() -> None:
            threading.Thread(
                target=_stall_watchdog_loop, daemon=True,
                name=f"stall-watchdog-{download_id[:8]}",
            ).start()

        def _download_worker():
            ctx_token = set_download_context(download_id)
            try:
                _download_worker_body()
            finally:
                reset_download_context(ctx_token)
                cleanup_snapshot = None
                with self._lock:
                    cleanup_snapshot = dict(self._cleanup_info.get(download_id) or {})
                kill_pp_state_procs(cleanup_snapshot.get("pp_state"))
                kill_download_ffmpeg_pids(download_id)

        def _download_worker_body():
            try:
                _enforce_deadline()
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
                        "register_temp_dir": _register_temp_dir,
                        "settings_mgr": params.get("settings_mgr"),
                        "audio_only": params.get("audio_only"),
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
                        register_temp_dir=_register_temp_dir,
                        register_pp_state=_register_pp_state,
                        audio_only=bool(params.get("audio_only")),
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
                    from services.youtube_diag import log_download
                    log_download(
                        download_id,
                        "completed",
                        url=params.get("url", ""),
                        platform=state.platform,
                        detail=f"bytes={size}",
                    )
                    with self._lock:
                        state.status = "Completed"
                        state.progress = 100
                    self._notify_sse(download_id, "complete", 100)
                else:
                    raise RuntimeError("Download finished but output file is missing")
            except ytdlp_service.DownloadTimeoutError as e:
                from services.youtube_diag import log_download
                log_download(download_id, "timeout", url=params.get("url", ""), platform=state.platform, detail=str(e))
                with self._lock:
                    state.status = "Failed"
                    state.error = str(e)
                self._notify_sse(download_id, "error", str(e))
                _cleanup_output()
            except ytdlp_service.PausedError:
                with self._lock:
                    state.status = "Paused"
                    params_snapshot = dict(self._worker_params.get(download_id) or {})
                self._db.upsert_queue_entry(state, params_snapshot or None)
                self._notify_sse(download_id, "status", "Paused")
            except ytdlp_service.CancelledError:
                stall_msg = stall_holder.get("error")
                if stall_msg:
                    with self._lock:
                        state.status = "Failed"
                        state.error = stall_msg
                    self._notify_sse(download_id, "error", stall_msg)
                    _cleanup_output()
                else:
                    with self._lock:
                        if state.status != "Cancelled":
                            state.status = "Cancelled"
                            self._notify_sse(download_id, "status", "Cancelled")
                    _cleanup_output()
            # ponytail: survival guarantee for download worker
            except Exception as e:
                stall_msg = stall_holder.get("error")
                if stall_msg and not pause_event.is_set():
                    # The stall watchdog cancelled this download — surface its
                    # message, not the (usually cryptic) abort-time exception.
                    logger.warning(
                        "Download %s failed by stall watchdog: %s", download_id, stall_msg,
                    )
                    with self._lock:
                        state.status = "Failed"
                        state.error = stall_msg
                    self._notify_sse(download_id, "error", stall_msg)
                    _cleanup_output()
                    return
                if pause_event.is_set() and not cancel_event.is_set():
                    with self._lock:
                        state.status = "Paused"
                        params_snapshot = dict(self._worker_params.get(download_id) or {})
                    self._db.upsert_queue_entry(state, params_snapshot or None)
                    self._notify_sse(download_id, "status", "Paused")
                    return
                logger.exception(
                    "Download worker failure for %s", download_id, exc_info=e
                )
                from services.youtube_diag import log_download
                log_download(
                    download_id,
                    "failed",
                    url=params.get("url", ""),
                    platform=state.platform,
                    detail=sanitize_download_error(e),
                )
                with self._lock:
                    state.status = "Failed"
                    state.error = sanitize_download_error(e)
                self._notify_sse(download_id, "error", state.error)
                _cleanup_output()
            # ponytail: bare except for catastrophic shutdown
            except Exception:
                import sys
                _exc_type, _exc_val, _exc_tb = sys.exc_info()
                logger.exception(
                    "Fatal download worker failure for %s", download_id,
                )
                with self._lock:
                    state.status = "Failed"
                    state.error = sanitize_download_error(_exc_val) if _exc_val else "Unknown fatal error"
                self._notify_sse(download_id, "error", state.error)
                _cleanup_output()
            finally:
                stall_stop.set()
                with self._lock:
                    # Zombie-row guard: a worker that exits WITHOUT a terminal
                    # status (BaseException escape like SystemExit/KeyboardInterrupt,
                    # or thread death) would leave the persisted queue entry at
                    # "Finalising… 99/100%" forever — the in-memory row is popped
                    # below but the queue entry survives and get_active_and_history
                    # revives it as non-terminal (no worker, no SSE, no cancel
                    # event). Mark it Failed so the _DONE_STATUSES branch below
                    # persists it to history and removes the queue entry.
                    if state.status not in _DONE_STATUSES and state.status != "Paused":
                        state.status = "Failed"
                        state.error = "Download worker exited unexpectedly"
                        state.progress = min(state.progress, 99)
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
                    cleanup = self._cleanup_info.get(download_id) or {}
                    temp_dirs: List[str] = list(cleanup.get("temp_dirs") or [])
                    if state.status == "Paused":
                        self._abort_fns[download_id] = []
                        with self._lock:
                            params_snapshot = dict(self._worker_params.get(download_id) or {})
                        self._db.upsert_queue_entry(final_state, params_snapshot or None)
                        return
                    self._sse_queues.pop(download_id, None)
                    self._cancel_events.pop(download_id, None)
                    self._pause_events.pop(download_id, None)
                    self._abort_fns.pop(download_id, None)
                    self._cleanup_info.pop(download_id, None)
                    self._worker_params.pop(download_id, None)
                    if state.status not in _DONE_STATUSES:
                        self._downloads.pop(download_id, None)
                try:
                    if final_state.status != "Completed" and temp_dirs:
                        remove_temp_dirs(temp_dirs)
                # ponytail: cleanup survival — remove_temp_dirs can raise OSError
                except OSError:
                    pass
                _stop_poller()
                if final_state.status in _DONE_STATUSES:
                    self._db.remove_queue_entry(download_id)
                    self._db.record_history(final_state)

        _start_stall_watchdog()
        self._executor.submit(_download_worker)

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
            params_snapshot = dict(self._worker_params.get(download_id) or {})

        for fn in abort_fns:
            try:
                fn()
            # ponytail: survival guarantee for arbitrary abort callbacks
            except Exception:
                pass
        with self._lock:
            state = self._downloads.get(download_id)
            if state:
                self._db.upsert_queue_entry(state, params_snapshot or None)
        self._notify_sse(download_id, "status", "Paused")
        return True

    def resume(
        self,
        download_id: str,
        oauth: Optional[str] = None,
        download_func: Optional[Callable[..., str]] = None,
        settings_mgr: Optional["SettingsManager"] = None,
    ) -> Optional[str]:
        live_resume = False
        with self._lock:
            pause_event = self._pause_events.get(download_id)
            state = self._downloads.get(download_id)
            if pause_event and state and state.status == "Paused":
                pause_event.clear()
                state.status = "Starting..."
                state.error = None
                params_snapshot = dict(self._worker_params.get(download_id) or {})
                live_resume = True
        if live_resume:
            self._db.upsert_queue_entry(state, params_snapshot or None)
            self._notify_sse(download_id, "status", "Starting...")
            self._spawn_worker(download_id)
            return download_id
        return self._restart_resumable(
            download_id,
            oauth=oauth,
            download_func=download_func,
            settings_mgr=settings_mgr,
        )

    def _restart_resumable(
        self,
        download_id: str,
        oauth: Optional[str] = None,
        download_func: Optional[Callable[..., str]] = None,
        settings_mgr: Optional["SettingsManager"] = None,
    ) -> Optional[str]:
        entry = self.get_resumable_entry(download_id)
        if not entry:
            return None
        params = entry.get("_params") or entry
        self._db.remove_queue_entry(download_id)
        self.remove_history(download_id)
        return self.start_download(
            download_id=download_id,
            url=params["url"],
            output_file=params["output_file"],
            quality=params.get("quality"),
            oauth=oauth if oauth is not None else params.get("oauth"),
            crop_start=params.get("crop_start"),
            crop_end=params.get("crop_end"),
            download_type=params.get("download_type", params.get("type", "video")),
            download_func=download_func,
            settings_mgr=settings_mgr,
            title=params.get("title"),
            channel=params.get("channel"),
            thumbnail=params.get("thumbnail"),
            duration=params.get("duration"),
            duration_string=params.get("duration_string"),
            estimated_bytes=params.get("estimated_bytes"),
        )

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
        snapshot: Optional[DownloadState] = None
        with self._lock:
            state = self._downloads.get(download_id)
            if not state or state.status in _DONE_STATUSES:
                return False
            state.status = "Cancelled"
            state.progress = 0
            snapshot = state.model_copy(deep=True)
        cleanup = self._force_stop_download(download_id)
        if cleanup:
            expected_duration = cleanup.get("expected_duration")
            temp_dirs = list(cleanup.get("temp_dirs") or [])
            delete_partial_output(
                cleanup["output_file"], cleanup["output_existed"],
                expected_duration=expected_duration,
            )
            if temp_dirs:
                remove_temp_dirs(temp_dirs)
        self._notify_sse(download_id, "status", "Cancelled")
        self._purge_download_runtime(download_id)
        if snapshot is not None:
            self._db.remove_queue_entry(download_id)
            self._db.record_history(snapshot)
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
        """Return unified queue + completed history lists.

        Active queue: in-memory + persisted queue entries that are still
        running / paused / not yet terminal. Failed / Cancelled / Interrupted
        belong in the *recent* section of history (with a Resume button)
        — promoting them back into the queue created runaway duplicate rows
        after a handful of retries, so keep them in history only.
        """
        with self._lock:
            in_memory = list(self._downloads.values())

        queue_map: Dict[str, DownloadState] = {}
        for state in in_memory:
            if state.status not in _DONE_STATUSES:
                queue_map[state.download_id] = state

        for entry in self._db.queue:
            did = entry.get("download_id")
            if not did or did in queue_map:
                continue
            state = self._db.queue_entry_to_state(entry)
            if state and state.status not in _DONE_STATUSES:
                queue_map[did] = state

        in_memory_done = {d.download_id: d for d in in_memory if d.status in _DONE_STATUSES}
        merged_history: Dict[str, DownloadState] = dict(in_memory_done)
        for entry in self._db.history:
            did = entry.get("download_id")
            if not did or did in merged_history:
                continue
            try:
                merged_history[did] = DownloadState(**entry)
            # ponytail: Pydantic validation errors only
            except (ValueError, TypeError, RuntimeError):
                logger.debug("Skipping malformed history entry %s", did, exc_info=True)

        # Dedupe by (url, status in {_UNFINISHED_STATUSES_}) so the same
        # failed URL doesn't appear N times after repeated retries.
        queue_list = sorted(
            queue_map.values(),
            key=lambda d: d.started_at or "",
            reverse=True,
        )
        seen_unfinished: set[tuple[str, str]] = set()
        deduped_queue: list[DownloadState] = []
        for d in queue_list:
            if d.status in _UNFINISHED_STATUSES:
                key = (d.url, d.status)
                if key in seen_unfinished:
                    continue
                seen_unfinished.add(key)
            deduped_queue.append(d)
            if len(deduped_queue) >= 50:
                break

        recent_history = sorted(
            merged_history.values(),
            key=lambda d: d.started_at or "",
            reverse=True,
        )[:50]

        # Split recent into resumable vs completed so the UI can show
        # a "Recent" section with Resume buttons and a "Completed" section.
        recent_unfinished = [
            d for d in recent_history
            if d.status in _UNFINISHED_STATUSES and is_sensible_vod_url(d.url)
        ]
        completed_history = [
            d for d in recent_history
            if d.status == "Completed" and is_sensible_vod_url(d.url)
        ]

        return {
            "queue": [d for d in deduped_queue if is_sensible_vod_url(d.url)],
            "recent": recent_unfinished,
            "history": completed_history,
        }

    @staticmethod
    def _decrypt_oauth_params(entry: dict) -> None:
        """Decrypt oauth in-place from _params so callers get plaintext."""
        params = entry.get("_params")
        if params and params.get("oauth"):
            params["oauth"] = decrypt_token(params["oauth"])

    def get_resumable_entry(self, download_id: str) -> Optional[dict]:
        """Return a resumable entry from memory, queue.json, or history."""
        with self._lock:
            state = self._downloads.get(download_id)
            if state and state.status in _RESUMABLE_STATUSES:
                payload = state.model_dump(mode="json")
                params = self._worker_params.get(download_id)
                if params:
                    payload["_params"] = self._db._serializable_worker_params(params)
                    self._decrypt_oauth_params(payload)
                return payload
        for entry in self._db.queue:
            if entry.get("download_id") == download_id:
                entry = dict(entry)
                self._decrypt_oauth_params(entry)
                return entry
        for entry in self._db.history:
            if entry.get("download_id") != download_id:
                continue
            if entry.get("status") in _RESUMABLE_STATUSES | _UNFINISHED_STATUSES:
                entry = dict(entry)
                self._decrypt_oauth_params(entry)
                return entry
            return None
        return None

    def get_history_entry(self, download_id: str) -> Optional[dict]:
        entry = self.get_resumable_entry(download_id)
        if not entry:
            return None
        status = entry.get("status")
        if status in _RESUMABLE_STATUSES | _UNFINISHED_STATUSES:
            return entry
        return None

    def retry_download(
        self,
        download_id: str,
        oauth: Optional[str] = None,
        download_func: Optional[Callable[..., str]] = None,
        settings_mgr: Optional["SettingsManager"] = None,
    ) -> str:
        new_id = self._restart_resumable(
            download_id,
            oauth=oauth,
            download_func=download_func,
            settings_mgr=settings_mgr,
        )
        if not new_id:
            raise ValueError("Download not found or not retryable")
        return new_id

    def _has_active_runtime(self, download_id: str) -> bool:
        with self._lock:
            state = self._downloads.get(download_id)
            if state and state.status not in _DONE_STATUSES:
                return True
            if download_id in self._worker_params:
                return True
            if self._cancel_events.get(download_id):
                return True
            return False

    def _force_stop_download(self, download_id: str) -> Optional[dict]:
        """Cancel worker, run abort hooks, kill tracked ffmpeg. Returns cleanup info."""
        cleanup: Optional[dict] = None
        pp_state = None
        deadline = time.monotonic() + 2.5
        while True:
            abort_fns: List[Callable[[], None]] = []
            with self._lock:
                pause_event = self._pause_events.get(download_id)
                event = self._cancel_events.get(download_id)
                if pause_event:
                    pause_event.clear()
                if event:
                    event.set()
                abort_fns = list(self._abort_fns.get(download_id, []))
                raw_cleanup = self._cleanup_info.get(download_id)
                if raw_cleanup:
                    cleanup = dict(raw_cleanup)
                    pp_state = cleanup.get("pp_state")
            for fn in abort_fns:
                try:
                    fn()
                # ponytail: survival guarantee for arbitrary abort callbacks
                except Exception:
                    pass
            kill_pp_state_procs(pp_state)
            kill_download_ffmpeg_pids(download_id, remove=False)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        kill_download_ffmpeg_pids(download_id, remove=True)
        return cleanup

    def _purge_download_runtime(self, download_id: str) -> None:
        with self._lock:
            self._downloads.pop(download_id, None)
            self._cancel_events.pop(download_id, None)
            self._pause_events.pop(download_id, None)
            # abort_fns + cleanup_info: worker thread finally-block still needs these
            # while ffmpeg winds down after cancel/remove.
            self._worker_params.pop(download_id, None)
            self._sse_queues.pop(download_id, None)

    def discard_from_queue(self, download_id: str) -> bool:
        if self._has_active_runtime(download_id):
            cleanup = self._force_stop_download(download_id)
            if cleanup:
                delete_partial_output(
                    cleanup["output_file"],
                    cleanup["output_existed"],
                    expected_duration=cleanup.get("expected_duration"),
                )
                temp_dirs = list(cleanup.get("temp_dirs") or [])
                if temp_dirs:
                    remove_temp_dirs(temp_dirs)
            self._purge_download_runtime(download_id)
            self._db.remove_queue_entry(download_id)
            return True
        return self.remove_history(download_id)

    def remove_history(self, download_id: str) -> bool:
        cleanup = self._force_stop_download(download_id)
        if self._has_active_runtime(download_id):
            if cleanup:
                delete_partial_output(
                    cleanup["output_file"],
                    cleanup["output_existed"],
                    expected_duration=cleanup.get("expected_duration"),
                )
                temp_dirs = list(cleanup.get("temp_dirs") or [])
                if temp_dirs:
                    remove_temp_dirs(temp_dirs)
            self._purge_download_runtime(download_id)
            self._db.remove_queue_entry(download_id)
        output_file: Optional[str] = None
        status: Optional[str] = None
        cleanup: Optional[dict] = None
        removed = False
        with self._lock:
            state = self._downloads.get(download_id)
            if state:
                output_file = state.output_file or None
                status = state.status
            cleanup = self._cleanup_info.get(download_id)
            if state and state.status in _DONE_STATUSES:
                self._downloads.pop(download_id, None)
                removed = True
        if not output_file:
            for entry in self._db.history:
                if entry.get("download_id") == download_id:
                    output_file = entry.get("output_file") or None
                    status = status or entry.get("status")
                    break
        if not output_file:
            for entry in self._db.queue:
                if entry.get("download_id") == download_id:
                    output_file = entry.get("output_file") or None
                    status = status or entry.get("status")
                    break
        had_on_disk = any(e.get("download_id") == download_id for e in self._db.history)
        if had_on_disk:
            self._db.drop_history(download_id)
            removed = True
        had_queue = any(e.get("download_id") == download_id for e in self._db.queue)
        if had_queue:
            self._db.remove_queue_entry(download_id)
            removed = True
        if output_file:
            self._schedule_output_delete(output_file, status, cleanup)
        return removed

    def _schedule_output_delete(
        self,
        output_file: str,
        status: Optional[str],
        cleanup: Optional[dict],
    ) -> None:
        """ponytail: drop DB first, delete files on executor so /remove returns fast."""

        def _run() -> None:
            try:
                if status in ("Failed", "Cancelled"):
                    output_existed = bool(cleanup and cleanup.get("output_existed"))
                    expected_duration = cleanup.get("expected_duration") if cleanup else None
                    delete_partial_output(
                        output_file,
                        output_existed,
                        expected_duration=expected_duration,
                    )
                delete_download_output(output_file)
            except Exception as exc:
                logger.warning("background delete failed for %s: %s", output_file, exc)

        try:
            self._executor.submit(_run)
        except RuntimeError:
            _run()

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
            # ponytail: survival guarantee for SSE notification
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
            # ponytail: survival guarantee for SSE notification
            except Exception:
                pass
