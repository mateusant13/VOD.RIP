"""
System routes — focus, exit, info, version, update, ytdlp status, local media.
"""

import asyncio
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from deps import (
    LIVENESS_EXECUTOR,
    HEALTH_EXECUTOR,
    INFO_EXECUTOR,
    OS_EXECUTOR,
    settings_mgr,
)
from utils import media_type_for_path, validate_local_media_path

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.get("/api/local/media")
async def local_media(path: str):
    """Stream a completed download from disk (Range-aware)."""
    try:
        loop = asyncio.get_running_loop()
        file_path = await loop.run_in_executor(
            OS_EXECUTOR,
            lambda: validate_local_media_path(path, settings_mgr),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return FileResponse(
        str(file_path),
        media_type=media_type_for_path(file_path),
        filename=file_path.name,
    )


@router.post("/api/focus")
async def focus_app():
    """Bring the desktop window to the foreground (second-instance launch)."""
    from services.app_lifecycle import show_window
    show_window()
    return {"ok": True}


@router.post("/api/exit")
async def exit_app():
    """Shut down all processes and kill the server."""
    logger.warning("Exit requested via API — shutting down (caller traceback follows)")
    import traceback as _tb
    logger.warning("".join(_tb.format_stack(limit=8)))
    from services.app_lifecycle import request_app_exit
    request_app_exit()
    return {"ok": True, "message": "Shutting down"}


@router.get("/api/info")
async def server_info():
    # include features so /api/info reflects opt-in state
    def _features() -> dict:
        # Both imports are inside the worker, not the handler body: a lazy
        # `import` in an async def is synchronous work ON THE EVENT LOOP,
        # and this route is in the supervisor probe rotation, so the first
        # probe paid the module loads inline. Same rule as
        # asr_runtime_status below.
        from services.feature_registry import get_enabled_map

        # get_enabled_map() is memoized; cold cache reads settings_mgr.get()
        # (in-memory snapshot under the manager lock, may probe ffmpeg) —
        # INFO_EXECUTOR, its own named pool. It must NOT share LIVENESS:
        # the ffmpeg probe is a process spawn, not a sub-second read, and
        # LIVENESS is the pool the lock-free /api/asr/runtime depends on
        # (see HEALTH_EXECUTOR above for the same head-of-line reasoning).
        # The liveness test already documents this route as INFO_EXEC.
        return get_enabled_map()

    try:
        _feats = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR, _features,
        )
    except Exception:
        _feats = {}
    try:
        from services._version import __version__ as app_version
    except ImportError:
        app_version = "0.0.0"
    return {
        "version": app_version,
        "name": "VOD.RIP 🪦",
        "desktop": os.environ.get("KICK_SERVE_UI", "").strip() == "1",
        "engine": "yt-dlp (Python)",
        "description": "Kick & Twitch VOD and clip downloader",
        "python_version": platform.python_version(),
        "features": _feats,
    }


# ---------------------------------------------------------------------------
# ASR runtime status: the guaranteed engine, and the accelerator beside it.
#
# This route already answered "is the optional speech runtime installed?".
# It could not answer the question the GPU accelerator was added for: is
# Photon actually ON, and when it is not, WHICH of {off, misconfigured,
# retired by the breaker, degraded} is it. services/photon_asr knows all of
# that - status_summary() is documented as "everything needed to answer 'is
# Photon on, and why not?' in one call ... safe to call from a health/status
# route" - but nothing called it, so the state existed only as a one-shot log
# line from available(). The accelerator was invisible to the only interface
# the user has.
#
# TWO RULES THIS BLOCK IS BUILT AROUND, both paid for in this repo already:
#
#   1. UNMEASURED IS NOT ZERO. A counter that has never incremented is
#      reported as null with a reason, never as 0 - "0 timeouts" reads as a
#      clean run when it in fact means the accelerator was never consulted.
#      An unreadable check is unknown, never a plausible default.
#   2. THE PROBE NEVER STARTS THE ACCELERATOR. Proving CUDA needs a real
#      sidecar run on a GPU shared with another workload, so health is read
#      from the counters the engine already keeps, never by launching it.

# A check that could not be read says so. Never a zero, never a False.
_UNKNOWN_CHECK = "not measured: the check could not be read"
# The accelerator's OFF state is a decision, not a defect, and is reported
# as its own state so it is never mistaken for a broken install.
_PHOTON_DISABLED = "disabled"
# Proving CUDA means running the sidecar. The GPU on this box is shared and
# a status probe is not a workload, so this stays unverified and says why.
_PHOTON_CUDA_UNVERIFIED = (
    "unverified: proving CUDA needs a real sidecar run, and a status probe "
    "must not take the GPU to find out"
)
# The per-job device became honest in archive_transcribe._ran_device
# (4f6da7e), but it is recorded in the archive WORKER process and is not
# persisted, so this process cannot read it. Reporting the plan slot here
# would be exactly the lie that fix removed.
_DEVICE_UNREACHABLE = (
    "unknown: the per-job device is recorded in the archive worker process "
    "and is not persisted, so the API cannot read it"
)
# The int8 parakeet is deliberately kept on disk as a revert path. It is not
# a runtime choice - a revert is a constant change - so its presence says
# nothing about what will run, and this note says so where it is reported.
_INT8_PARAKEET_DIR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
_INT8_FALLBACK_NOTE = (
    "kept on disk as a revert path, not selectable at runtime - a revert is "
    "a constant change, so this says nothing about what will run"
)


def _counter(snap: dict, key: str) -> Optional[int]:
    """One engine counter as an int, or None when the engine never wrote it.

    None ("never measured") and 0 ("measured, nothing happened") are
    different facts; flattening them is how a counter that was never
    exercised ends up reading as a clean run.
    """
    val = snap.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return int(val)


def _env_number(name: str, default: float) -> float:
    """photon_asr's own env rule: blank, invalid or non-positive -> default.

    Mirrored rather than imported so the breaker threshold and cooldown
    reported here are the ones the breaker will actually use; the module's
    parser is private and this must not reach past its public surface.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def _photon_runtime_status() -> dict:
    """The opt-in accelerator's state, degrading to explicit unknowns.

    Never raises. Every check answers for itself and admits its own gaps, so
    one unreadable probe cannot cost the user the rest of the picture - a
    status endpoint that 500s is worse than one that names what it could not
    find out.
    """
    out: dict[str, Any] = {
        "state": "unknown",
        "enabled": None,
        "on": None,
        "reason": _UNKNOWN_CHECK,
        "switch_env": "VODRIP_PHOTON_ASR",
        "switch_default": "0",
        "model": None,
        "venv": {
            "path": None,
            "interpreter_present": None,
            "is_scratch_path": None,
        },
        "cuda": {"verified": None, "reason": _PHOTON_CUDA_UNVERIFIED},
        "weights": {"ok": None, "dir": None, "reason": _UNKNOWN_CHECK},
        "breaker": {
            "open": None,
            "seconds_remaining": None,
            "consecutive_failures": None,
            "failure_threshold": None,
            "cooldown_s": None,
        },
        "counters": {
            "measured": None,
            "attempts": None,
            "ok": None,
            "timeouts": None,
            "killed": None,
            "errors": None,
            "rejected": None,
            "weights_missing": None,
            "fallbacks": None,
        },
        "last_fallback_reason": None,
        "last_success_age_s": None,
    }
    try:
        from services import photon_asr
    except Exception as exc:  # pragma: no cover - the module is in-tree
        out["reason"] = f"{_UNKNOWN_CHECK}: {exc}"
        return out

    # Threshold and cooldown are configuration, not observation: they can be
    # read even when the live state cannot, so they are filled in first.
    out["breaker"]["failure_threshold"] = int(
        _env_number(photon_asr.MAX_FAILURES_ENV, photon_asr.DEFAULT_MAX_FAILURES)
    )
    out["breaker"]["cooldown_s"] = float(
        _env_number(
            photon_asr.BREAKER_COOLDOWN_ENV, photon_asr.DEFAULT_BREAKER_COOLDOWN_S
        )
    )

    try:
        snap = photon_asr.stats()
    except Exception as exc:
        out["reason"] = f"{_UNKNOWN_CHECK}: {exc}"
        return out
    if not isinstance(snap, dict):
        out["reason"] = f"{_UNKNOWN_CHECK}: stats() returned {type(snap).__name__}"
        return out

    # The switch, and the reason it is not being used. unavailable_reason()
    # names the one thing to fix, and distinguishes "off by design" from
    # "on and unusable" on its own - which is why it is read separately and
    # not inferred from the counters.
    try:
        reason = str(photon_asr.unavailable_reason() or "")
    except Exception as exc:
        reason = f"{_UNKNOWN_CHECK}: {exc}"
    enabled = snap.get("enabled")
    out["enabled"] = bool(enabled) if isinstance(enabled, bool) else None
    out["reason"] = reason or "usable"

    on = snap.get("enabled") is True and not reason
    out["on"] = bool(on)
    out["model"] = snap.get("model") or None

    # Venv + sidecar interpreter. A blank interpreter is a real measurement
    # (the module stats a file that is not there), not an unknown.
    py = str(snap.get("python") or "")
    out["venv"] = {
        "path": snap.get("venv") or None,
        "interpreter_present": bool(py),
        "is_scratch_path": snap.get("venv_is_sandbox"),
    }
    if not out["venv"]["is_scratch_path"] and "is_scratch_path" not in snap:
        try:
            out["venv"]["is_scratch_path"] = bool(photon_asr.venv_is_sandbox())
        except Exception:
            out["venv"]["is_scratch_path"] = None

    weights = snap.get("weights")
    if isinstance(weights, dict):
        out["weights"] = {
            "ok": weights.get("ok") if isinstance(weights.get("ok"), bool) else None,
            "dir": weights.get("dir") or None,
            "reason": weights.get("reason") or "",
        }

    # The breaker. seconds_remaining is a real measurement in both
    # directions: 0.0 means closed right now, which is a fact, not a gap.
    breaker_open = snap.get("breaker_open")
    left: Optional[float] = None
    until = snap.get("breaker_until")
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        left = max(0.0, float(until) - time.monotonic())
    out["breaker"].update({
        "open": bool(breaker_open) if isinstance(breaker_open, bool) else None,
        "seconds_remaining": left,
        "consecutive_failures": _counter(snap, "consecutive_failures"),
    })

    attempts = _counter(snap, "attempts")
    ok = _counter(snap, "ok")
    if attempts is None:
        # No attempts key at all: nothing here has been measured.
        out["counters"]["measured"] = None
    elif attempts == 0:
        # The switch may be off, the accelerator may simply never have been
        # consulted. Either way no failure count was measured, and 0 would
        # claim it was.
        out["counters"].update({"measured": False})
        out["counters"].update({
            k: None for k in (
                "attempts", "ok", "timeouts", "errors", "rejected",
                "weights_missing", "fallbacks",
            )
        })
    else:
        out["counters"].update({
            "measured": True,
            "attempts": attempts,
            "ok": ok,
            "timeouts": _counter(snap, "timeouts"),
            "errors": _counter(snap, "errors"),
            "rejected": _counter(snap, "rejected"),
            "weights_missing": _counter(snap, "weights_missing"),
            # Every attempt that did not succeed is a batch the caller
            # re-ran on sherpa-onnx. This is the number a "GPU" label can
            # quietly hide, so it is computed here rather than left to the
            # reader to infer from attempts-minus-ok.
            "fallbacks": (attempts - ok) if ok is not None else None,
        })
    out["counters"]["killed"] = None
    out["counters"]["killed_reason"] = (
        "not a separate counter: the sidecar is killed inside the timeout "
        "path, so kills are counted as timeouts"
    )

    last_error = str(snap.get("last_error") or "").strip()
    if last_error:
        out["last_fallback_reason"] = last_error
    elif out["counters"]["measured"] is True:
        out["last_fallback_reason"] = ""
    else:
        out["last_fallback_reason"] = None

    last_ok = snap.get("last_ok_at")
    if isinstance(last_ok, (int, float)) and not isinstance(last_ok, bool) and last_ok > 0:
        out["last_success_age_s"] = max(0.0, time.monotonic() - float(last_ok))

    # The state, as one word. Disabled is NOT broken and is never collapsed
    # into it; "degraded" is the one that matters for the UI, because it is
    # the state where the accelerator is on and jobs still went to the CPU.
    if out["enabled"] is None:
        out["state"] = "unknown"
    elif not out["enabled"]:
        out["state"] = _PHOTON_DISABLED
    elif _UNKNOWN_CHECK in reason:
        # The reason itself could not be read, so no state can be named from
        # it. Guessing "misconfigured" here would invent a fault.
        out["state"] = "unknown"
    elif "circuit breaker open" in reason:
        out["state"] = "retired"
    elif reason:
        out["state"] = "misconfigured"
    elif out["counters"]["fallbacks"]:
        out["state"] = "degraded"
    else:
        out["state"] = "ready"
    return out


def _parakeet_cache_root() -> Optional[Path]:
    """The sherpa model cache, resolved exactly as live_captions resolves it.

    A second, LIGHT copy on purpose: this route is in the supervisor probe
    rotation and must not import the 6k-line archive worker to stat a
    directory - the same reason services/live_captions keeps its own copy,
    which a test pins in lockstep with the worker's constants.
    """
    try:
        from services import live_captions as lc

        override = os.environ.get(lc._PARAAKEET_CACHE_ENV, "").strip()
        if override:
            return Path(override)
        from services.disk_hygiene import _migrated_model_dir, whisper_cache_dir

        base = whisper_cache_dir()
        return _migrated_model_dir(
            base / "parakeet-models", base.parent / "parakeet-models", "parakeet",
        )
    except Exception as exc:
        logger.debug("parakeet cache root unresolved: %s", exc)
        return None


def _engine_runtime_status() -> dict:
    """The GUARANTEED path: what actually transcribes when Photon is off.

    The accelerator made this question urgent - "so what is transcribing my
    VODs?" - and the answer has to come from facts this process can see.
    It shares its interpreter with the archive worker (worker_server spawns
    it with sys.executable), so sherpa-onnx availability and the model on
    disk are honest facts here. The DEVICE a job ran on is not: it is
    per-job state in the worker process, so it is reported as unknown rather
    than filled in with the plan slot.
    """
    out: dict[str, Any] = {
        "name": "parakeet",
        "runtime": "sherpa-onnx",
        "guaranteed": True,
        "model": None,
        "model_present": None,
        "model_present_reason": _UNKNOWN_CHECK,
        "model_dir": None,
        "missing_files": [],
        "model_selection": (
            "code constant (archive_transcribe.PARAKEET_MODEL); there is no "
            "runtime knob to change it"
        ),
        "sherpa_onnx": None,
        "sherpa_onnx_reason": _UNKNOWN_CHECK,
        "int8_fallback_on_disk": None,
        "int8_fallback_dir": None,
        "int8_fallback_note": _INT8_FALLBACK_NOTE,
        "device": {"known": False, "value": None, "reason": _DEVICE_UNREACHABLE},
    }
    try:
        from services import live_captions as lc
    except Exception as exc:
        out["model_selection"] = f"unreadable: {exc}"
        return out
    out["model"] = lc._PARAAKEET_MODEL

    # find_spec does NOT import the module: a status probe must not pay a
    # CUDA-wheel import to learn whether one is installed.
    try:
        import importlib.util

        found_spec = importlib.util.find_spec("sherpa_onnx") is not None
        out["sherpa_onnx"] = found_spec
        out["sherpa_onnx_reason"] = (
            "importable in this interpreter" if found_spec
            else "not installed in this interpreter"
        )
    except Exception as exc:
        out["sherpa_onnx_reason"] = f"unreadable: {exc}"

    found: Optional[Path] = None
    probe_failed = False
    try:
        found = lc._parakeet_model_dir_probe()
        out["model_present"] = found is not None
        out["model_present_reason"] = (
            "all four model files present" if found is not None
            else "the model files are not all on disk"
        )
    except Exception as exc:
        probe_failed = True
        out["model_present_reason"] = f"unreadable: {exc}"
    if found is not None:
        out["model_dir"] = str(found)

    root = _parakeet_cache_root()
    if root is not None and not probe_failed:
        # The probe answers "is it there", not "what is absent" - the missing
        # filename is the part that lets someone fix it.
        expected = root / lc._PARAAKEET_DIR_NAME
        out["missing_files"] = sorted(
            f for f in lc._PARAAKEET_FILES if not (expected / f).is_file()
        )
        int8 = root / _INT8_PARAKEET_DIR
        on_disk = int8.is_dir()
        out["int8_fallback_on_disk"] = on_disk
        out["int8_fallback_dir"] = str(int8) if on_disk else None
    else:
        out["int8_fallback_on_disk"] = None
        out["int8_fallback_dir"] = None
    return out


@router.get("/api/asr/runtime")
async def asr_runtime_status() -> dict:
    """Report the ASR runtime: the guaranteed engine AND the opt-in GPU
    accelerator beside it, in one honest snapshot.

    Three things are deliberately NOT reported here, each for a reason the
    caller can see in the payload: the accelerator is never launched, the
    per-job device is not persisted so it is unknown rather than guessed,
    and a counter that was never exercised is null rather than 0.
    """

    def _status() -> dict:
        # The import goes HERE, not in the handler body. A lazy `import`
        # inside an async def is synchronous work ON THE EVENT LOOP: the
        # first probe of this route paid the whole module load inline and
        # was measured at 255ms while every other probe answered in 3-4ms.
        # This endpoint is in the supervisor probe rotation, so its first
        # call is exactly the one a watchdog sees at boot. Keeping the
        # import lazy (it is deliberately not module-level) but moving it
        # onto the worker thread preserves the light-import design.
        from services.asr_runtime import runtime_status

        # Reads the install marker file + stats the exe — blocking FS IO;
        # liveness pool (this endpoint is in the supervisor probe rotation).
        base = runtime_status()
        try:
            photon = _photon_runtime_status()
        except Exception as exc:  # pragma: no cover - the helper is total
            logger.warning("photon runtime status degraded: %s", exc)
            photon = {"state": "unknown", "reason": f"unreadable: {exc}"}
        try:
            engine = _engine_runtime_status()
        except Exception as exc:  # pragma: no cover - the helper is total
            logger.warning("asr engine status degraded: %s", exc)
            engine = {"name": "parakeet", "model_present_reason": f"unreadable: {exc}"}
        return {
            **base,
            "engine": engine,
            "photon": photon,
            # The headline number, hoisted out of photon.counters so it
            # cannot be missed: a silent fallback to CPU while the page
            # says "GPU" is the exact class of lie this endpoint ends.
            # One computed value in two places, so the two cannot drift.
            "photon_fallbacks": (
                photon.get("counters", {}).get("fallbacks")
                if isinstance(photon.get("counters"), dict) else None
            ),
        }

    return await asyncio.get_running_loop().run_in_executor(
        LIVENESS_EXECUTOR, _status,
    )


@router.post("/api/asr/runtime")
async def install_asr_runtime() -> dict:
    """Download and install the optional speech runtime on explicit request."""
    from services.asr_runtime import ensure_runtime, runtime_status

    loop = asyncio.get_running_loop()
    try:
        # Minutes-long blocking download + multi-GB verify/extract (serialized
        # by its own _install_lock anyway) — default pool, never LIVENESS:
        # 4 install calls would saturate the 4 liveness workers and queue
        # /api/health behind app work, the exact starvation the pool exists
        # to prevent.
        await loop.run_in_executor(None, ensure_runtime)
    except Exception as exc:
        logger.warning("ASR runtime installation failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Sub-second FS stat after the install — back on the liveness pool.
    return await loop.run_in_executor(LIVENESS_EXECUTOR, runtime_status)


@router.get("/api/errors/latest")
async def latest_errors(limit: int = Query(20, ge=1, le=500)) -> dict:
    """Latest server/application errors (bounded ring, no secrets).

    Mirrors the live-captions error ring: unauthenticated but bounded (max
    500 entries, sanitized — cookies/tokens are stripped on ingest).
    """
    from services.error_log import get_error_ring

    # ponytail: unauthenticated but bounded (500 entries, sanitized); gate
    # behind auth when app auth lands (same caveat as live_captions_errors).
    return {"errors": get_error_ring(limit)}


@router.get("/api/health")
async def health():
    """Aggregate liveness for external supervisors (dev-all, launcher watchdog).

    Answering 200 is itself the liveness proof; the fields let a supervisor
    tell the app's real state: queue backlog, detached worker/background
    daemons, and the age of the app's own 30s heartbeat (stale heartbeat
    with a live process = hung app). Best-effort — a DB hiccup degrades
    fields to None/False, never raises."""
    from services import archive_db

    def _probe() -> tuple:
        # Each sqlite read keeps its own degrade-to-None/False; the whole
        # probe runs on one worker thread so a WAL-busy first-touch (the
        # shared connection serialises behind the write lock) can never
        # stall the event loop — /api/health is what supervisors watch.
        # It runs on its OWN pool (HEALTH_EXECUTOR), not LIVENESS: these
        # reads are the one liveness path that can block for the full
        # busy_timeout, and sharing a 4-worker pool with the lock-free
        # endpoints let a stalled probe queue /api/asr/runtime behind it.
        # Off-loop protects the loop; a separate pool protects the peers.
        try:
            pending = archive_db.has_pending_jobs()
        except Exception:
            pending = None
        try:
            worker = archive_db.worker_live(age_s=45, tag="transcribe")
        except Exception:
            worker = False
        try:
            background = archive_db.worker_live(age_s=90, tag="background")
        except Exception:
            background = False
        try:
            activity_age = archive_db.worker_heartbeat_age("app-activity")
        except Exception:
            activity_age = None
        try:
            # SUBS_PO_TOKEN_POLICY monitor (event-driven; see youtube_diag).
            # A rollout of the subtitles PO-Token policy silently discards
            # caption tracks, so supervisors need a greppable 'is it firing
            # yet' signal — last sighting + count in the trailing hour.
            # Best-effort like the rest of health: a hiccup degrades to None,
            # never a 500.
            # Off-loop for the same reason as the sqlite reads above: the
            # FIRST call rehydrates the ring from the pot-policy JSONL
            # (services/youtube_diag._pot_rehydrate), which is a real file
            # read + parse of a growing log; on an already-loaded box that
            # is exactly the kind of blocking IO the 23c600f9 pass moved.
            from services.youtube_diag import subs_pot_policy_status

            subs_pot = subs_pot_policy_status()
        except Exception:
            subs_pot = None
        return pending, worker, background, activity_age, subs_pot

    pending, worker, background, activity_age, subs_pot = (
        await asyncio.get_running_loop().run_in_executor(
            HEALTH_EXECUTOR, _probe,
        )
    )
    return {
        "ok": True,
        "name": "VOD.RIP",
        "queue_pending": pending,
        "worker_alive": worker,
        "background_alive": background,
        "app_activity_age_s": activity_age,
        "subs_pot_policy": subs_pot,
    }


@router.get("/api/app/version")
async def app_version():
    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"
    return {"version": __version__}


@router.get("/api/update/check")
async def update_check(force: bool = False):
    from services.settings import _get_appdata_dir
    from services.updater import UpdateChecker
    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"
    checker = UpdateChecker(__version__, _get_appdata_dir())
    release = checker.check(force=force)
    return {"current": __version__, "update": release}


@router.post("/api/update/apply")
async def update_apply():
    from services.settings import _get_appdata_dir
    from services.updater import UpdateChecker
    try:
        from services._version import __version__
    except ImportError:
        __version__ = "0.0.0"
    checker = UpdateChecker(__version__, _get_appdata_dir())
    pending = checker.get_pending() or checker.check(force=True)
    if not pending:
        raise HTTPException(status_code=404, detail="No update available")
    result = checker.download_and_install(pending)
    if not result.ok:
        raise HTTPException(status_code=500, detail=result.message or "Update failed")
    return {"ok": True, "message": result.message or "Installing update"}


@router.get("/api/ytdlp/status")
async def ytdlp_status():
    try:
        import yt_dlp
        return {"available": True, "version": yt_dlp.version.__version__}
    except ImportError:
        return {"available": False, "version": None}


@router.get("/api/system/window-state")
async def window_state():
    """Current VOD.RIP window state for runtime policy decisions."""
    from services.app_lifecycle import is_window_active, is_window_minimized, get_window_policy
    return {
        "active": is_window_active(),
        "minimized": is_window_minimized(),
        "policy": get_window_policy(),
    }


@router.post("/api/presence")
async def presence(body: dict):
    """Presence heartbeat: POST {foreground: bool} toggles governor ceiling 40%<->80%."""
    fg = bool(body.get("foreground", True)) if isinstance(body, dict) else True
    try:
        from services.resource_governor import get_governor
        target = get_governor().set_foreground(fg)
    except Exception:
        target = 0.80 if fg else 0.40
    return {"foreground": fg, "effective_target": target}


@router.get("/api/features")
async def list_features():
    """Canonical feature list — manifest + current enabled map."""
    from services.feature_registry import get_manifest, get_enabled_map
    return {"manifest": get_manifest(), "features": get_enabled_map()}


@router.get("/api/info/features")
async def info_features():
    """Deprecated alias for /api/features — kept for backward compat."""
    from services.feature_registry import get_manifest, get_enabled_map
    return {"manifest": get_manifest(), "features": get_enabled_map()}
