"""Photon (Moondream 2.6.1) GPU ASR accelerator — sidecar CLIENT.

An OPT-IN accelerator that runs beside the guaranteed engine, never instead
of it. sherpa-onnx Parakeet Redux stays the default and the guaranteed path;
this module is only ever asked for a GPU slot's batch, and every failure mode
returns ``None`` so the caller falls straight back to sherpa-onnx.

Why a subprocess and not an import (the measurements this is built on, taken
on this box's RTX 5080 by the probe lane):

  * 8:00 of audio in 3.51 s (RTF 137x) with 1.75 GB VRAM after load — worth
    having, but only on a GPU slot;
  * the engine HANGS, it does not raise: 2 of 3 launches hung during engine
    creation and 1 hung on its third call, with 0% CPU, a frozen resident set
    and no exception. A supervisor watching the worker process sees a live
    process that never returns. In-process this is a wedged app; out-of-process
    it is one killed child and one fallback;
  * there is NO CPU path (kestrel's native extension reports
    ``ternary_gemm_isa() == "scalar"`` and raises NotImplementedError), so this
    can never be the only engine;
  * the venv is 5.29 GB with its own torch — the app interpreter runs a live
    API and a live transcription worker against sherpa-onnx and must not be
    touched by that install.

A hang is therefore bounded twice over: a hard per-run wall-clock timeout
(kill + tree kill), and a circuit breaker that takes Photon out of the running
process after repeated failures, so one bad engine costs one batch instead of
the job.

Env knobs (all optional; Photon is OFF unless VODRIP_PHOTON_ASR=1):

    VODRIP_PHOTON_ASR      "1" enables the accelerator. Default "0" — the
                           guaranteed path is the default on purpose.
    VODRIP_PHOTON_VENV     Photon venv root. Default: the probe-lane venv.
    VODRIP_PHOTON_PYTHON   Full path to the sidecar interpreter, overriding
                           the venv layout (used by the tests).
    VODRIP_PHOTON_RUNNER   Full path to photon_runner.py (used by the tests).
    VODRIP_PHOTON_MODEL    Model id. Default "moondream/parakeet-redux".
    VODRIP_PHOTON_TIMEOUT  Hard per-run wall clock, seconds. Default 120.
    VODRIP_PHOTON_MIN_VRAM_BYTES
                           Free-VRAM floor before the accelerator is even
                           asked. Default 2.5 GiB (measured 1.75 GB resident
                           + 2.26 GiB peak reserved).
    VODRIP_PHOTON_MAX_FAILURES
                           Consecutive failures that open the circuit
                           breaker. Default 2.
    VODRIP_PHOTON_BREAKER_COOLDOWN
                           Seconds the breaker stays open. Default 900.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess as sp
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from services.os_services import _NO_WINDOW

logger = logging.getLogger(__name__)

# --- knobs ----------------------------------------------------------------

ENABLED_ENV = "VODRIP_PHOTON_ASR"
VENV_ENV = "VODRIP_PHOTON_VENV"
PYTHON_ENV = "VODRIP_PHOTON_PYTHON"
RUNNER_ENV = "VODRIP_PHOTON_RUNNER"
MODEL_ENV = "VODRIP_PHOTON_MODEL"
TIMEOUT_ENV = "VODRIP_PHOTON_TIMEOUT"
MIN_VRAM_ENV = "VODRIP_PHOTON_MIN_VRAM_BYTES"
MAX_FAILURES_ENV = "VODRIP_PHOTON_MAX_FAILURES"
BREAKER_COOLDOWN_ENV = "VODRIP_PHOTON_BREAKER_COOLDOWN"

DEFAULT_VENV = r"G:\Temp\photonprobe\venv"
DEFAULT_MODEL = "moondream/parakeet-redux"
# Load 10.9 s warm / 22.0 s cold + a 5x-slower first call (16.0 s, RTF 30)
# + 3.51 s per 8 min of audio. 120 s covers the cold path with room to spare
# and still caps a hang at two minutes.
DEFAULT_TIMEOUT_S = 120.0
# Measured: 1.75 GB resident after load, 2.26 GiB peak reserved.
DEFAULT_MIN_VRAM_BYTES = int(2.5 * 1024 ** 3)
DEFAULT_MAX_FAILURES = 2
DEFAULT_BREAKER_COOLDOWN_S = 900.0
# A batch shorter than this that returns no segments at all is a broken
# accelerator, not silence: the clips handed here are VAD speech regions.
_MIN_MEANINGFUL_AUDIO_SEC = 1.0
_KILL_GRACE_S = 5.0


class PhotonUnavailable(RuntimeError):
    """Photon cannot be used at all here (off, no venv, breaker open)."""


class PhotonTimeout(PhotonUnavailable):
    """The sidecar exceeded its hard wall clock and was killed."""

    def __init__(self, message: str, pid: Optional[int] = None) -> None:
        super().__init__(message)
        self.pid = pid


class PhotonProtocolError(PhotonUnavailable):
    """The sidecar answered with something we refuse to treat as a transcript."""


# --- module state ---------------------------------------------------------

_lock = threading.Lock()
_state: dict[str, Any] = {
    "attempts": 0,
    "ok": 0,
    "timeouts": 0,
    "errors": 0,
    "rejected": 0,
    "consecutive_failures": 0,
    "breaker_until": 0.0,
    "last_error": "",
    "last_ok_at": 0.0,
    "last_duration_s": 0.0,
    "audio_sec": 0.0,
}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number - using %s", name, raw, default)
        return default
    return val if val > 0 else default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def enabled() -> bool:
    """The VODRIP_PHOTON_ASR kill switch. Default OFF."""
    return os.environ.get(ENABLED_ENV, "0").strip() == "1"


def min_vram_bytes() -> int:
    """Free-VRAM floor before the accelerator is asked for anything."""
    return int(_env_float(MIN_VRAM_ENV, DEFAULT_MIN_VRAM_BYTES))


def breaker_open() -> bool:
    """True while the circuit breaker is suppressing attempts."""
    with _lock:
        return time.monotonic() < _state["breaker_until"]


def stats() -> dict[str, Any]:
    """Counters for the log/status surface - a silent accelerator is the bug
    this module exists to avoid, so every attempt is counted."""
    with _lock:
        snap = dict(_state)
    snap["enabled"] = enabled()
    snap["breaker_open"] = time.monotonic() < snap["breaker_until"]
    snap["python"] = str(python_path() or "")
    snap["model"] = model_name()
    return snap


def reset_stats() -> None:
    """Test hook: clear counters and close the breaker."""
    with _lock:
        _state.update({
            "attempts": 0, "ok": 0, "timeouts": 0, "errors": 0,
            "rejected": 0, "consecutive_failures": 0, "breaker_until": 0.0,
            "last_error": "", "last_ok_at": 0.0, "last_duration_s": 0.0,
            "audio_sec": 0.0,
        })


def _note_success(duration_s: float, audio_sec: float) -> None:
    with _lock:
        _state["ok"] += 1
        _state["consecutive_failures"] = 0
        _state["breaker_until"] = 0.0
        _state["last_ok_at"] = time.monotonic()
        _state["last_duration_s"] = duration_s
        _state["audio_sec"] += audio_sec
        _state["last_error"] = ""


def _note_failure(kind: str, message: str) -> None:
    """Count a failure and open the breaker once it repeats.

    The measured hang rate (2 of 3 launches) means a single failure is not
    evidence of a broken install - but repeated failures are, and continuing to
    pay a two-minute timeout per batch after the second one is worse than
    falling back for the rest of the process.
    """
    threshold = _env_int(MAX_FAILURES_ENV, DEFAULT_MAX_FAILURES)
    cooldown = _env_float(BREAKER_COOLDOWN_ENV, DEFAULT_BREAKER_COOLDOWN_S)
    with _lock:
        _state[kind] = _state.get(kind, 0) + 1
        _state["consecutive_failures"] += 1
        _state["last_error"] = message[:400]
        if _state["consecutive_failures"] >= threshold:
            _state["breaker_until"] = time.monotonic() + cooldown
            logger.warning(
                "photon accelerator disabled for %.0fs after %d consecutive "
                "failures (last: %s) - sherpa-onnx carries the GPU slot",
                cooldown, _state["consecutive_failures"], message[:200],
            )


# --- sidecar resolution ---------------------------------------------------

def venv_root() -> Path:
    return Path(os.environ.get(VENV_ENV, "").strip() or DEFAULT_VENV)


def model_name() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def runner_path() -> Path:
    """The sidecar script. Ships next to this module in the app tree; the
    venv does not need the app on sys.path (the script is stdlib+numpy only)."""
    override = os.environ.get(RUNNER_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "photon_runner.py"


def python_path() -> Optional[Path]:
    """The interpreter that owns the Photon install, or None when absent."""
    override = os.environ.get(PYTHON_ENV, "").strip()
    if override:
        return Path(override) if Path(override).is_file() else None
    exe = venv_root() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.is_file() else None


def available() -> bool:
    """Cheap check: the switch is on, the breaker is closed, and the sidecar's
    interpreter + script are both on disk. Says nothing about CUDA working -
    only a real run can, and that is what the timeout is for."""
    if not enabled() or breaker_open():
        return False
    return python_path() is not None and runner_path().is_file()


def unavailable_reason() -> str:
    """Why the accelerator is not being used, for logs and status."""
    if not enabled():
        return f"{ENABLED_ENV}=0 (default)"
    if breaker_open():
        with _lock:
            left = max(0.0, _state["breaker_until"] - time.monotonic())
        return f"circuit breaker open for another {left:.0f}s (last: {_state['last_error'][:160]})"
    if python_path() is None:
        return f"no sidecar interpreter (set {PYTHON_ENV} or {VENV_ENV})"
    if not runner_path().is_file():
        return f"sidecar script missing: {runner_path()}"
    return ""


# --- process control ------------------------------------------------------

def _process_gone(pid: int) -> bool:
    """True when the pid is no longer a live process.

    os.kill(pid, 0) is NOT usable on Windows - there it calls TerminateProcess
    with the signal as the exit code, so a liveness probe with signal 0 would
    KILL the very process we are checking.

    Two Windows traps this avoids, both found by asserting the pid was gone
    after a real kill (it was not):
      * ctypes' default restype is c_int, so a 64-bit HANDLE gets truncated -
        WaitForSingleObject on a truncated handle returns WAIT_FAILED for a
        perfectly live process. The signatures are declared explicitly.
      * OpenProcess still SUCCEEDS for a terminated process whose handle is
        still open (Popen holds one until it is reaped), so a successful open
        is NOT liveness. The exit code is: STILL_ACTIVE (259) only while the
        process is running.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
            ]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return True  # no such process
            code = wintypes.DWORD()
            try:
                ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            finally:
                kernel.CloseHandle(handle)
            if not ok:
                return True  # cannot query -> treat as gone; we cannot reach it
            return code.value != 259  # STILL_ACTIVE
        except Exception:
            return False  # probe unavailable: assume alive (fail-safe, never
                          # report a live orphan as dead)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def kill_tree(pid: int) -> bool:
    """Kill a sidecar and anything it started; True when the pid is gone.

    The Photon engine runs its CUDA worker in a THREAD of the sidecar process,
    so the child is the only process to kill - but a tree kill is the honest
    default for a child we do not control the shape of, and costs one call.
    """
    gone = _process_gone(pid)
    if gone:
        return True
    if os.name == "nt":
        try:
            sp.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=_KILL_GRACE_S,
                creationflags=_NO_WINDOW,
            )
        except Exception:
            pass
    try:
        # 9 == SIGKILL on POSIX; on Windows os.kill ignores the signal for
        # termination purposes and calls TerminateProcess(handle, 9). Not
        # signal.SIGKILL — that name does not exist on Windows.
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    # Bounded settle: the caller's budget is one wait, never an unbounded spin.
    deadline = time.monotonic() + _KILL_GRACE_S
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.05)
    return _process_gone(pid)


# --- scratch --------------------------------------------------------------

def _scratch_dir() -> "Any":
    """A writable scratch root for the PCM handoff.

    AGENTS.md: heavy project data lives on the stable model drive, never C: and
    never G:\\Temp (pytest reaps vodrip-* dirs at session end). The AI-models
    root is the G: stable root; if it is unusable, fall back to the platform
    temp dir rather than failing the batch.
    """
    try:
        from services.disk_hygiene import whisper_cache_dir

        root = Path(whisper_cache_dir()) / "photon-asr" / "scratch"
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return root
    except Exception:
        return Path(tempfile.mkdtemp(prefix="vodrip-photon-"))


# --- the run --------------------------------------------------------------

def run_sidecar(
    *,
    pcm: Any,
    clips: list[tuple[float, float]],
    sample_rate: int,
    audio_sec: float = 0.0,
    python: Optional[Path] = None,
    runner: Optional[Path] = None,
    timeout_s: Optional[float] = None,
) -> list[dict]:
    """Run one sidecar process over one batch. Raises on every failure mode.

    Returns one validated entry per clip: ``{"language": str|None,
    "segments": [...]}`` with CLIP-RELATIVE timestamps, in input order.

    Raises PhotonUnavailable / PhotonTimeout / PhotonProtocolError. The caller
    treats any of those as "use sherpa-onnx".
    """
    exe = python if python is not None else python_path()
    script = runner if runner is not None else runner_path()
    if exe is None or not Path(exe).is_file():
        raise PhotonUnavailable(f"sidecar interpreter not found: {exe}")
    if not Path(script).is_file():
        raise PhotonUnavailable(f"sidecar script not found: {script}")
    if not clips:
        raise PhotonUnavailable("no clips to transcribe")

    budget = timeout_s if timeout_s is not None else _env_float(
        TIMEOUT_ENV, DEFAULT_TIMEOUT_S
    )
    workdir = Path(_scratch_dir()) / f"run-{os.getpid()}-{threading.get_ident()}"
    pcm_path = workdir / "pcm.f32"
    req_path = workdir / "req.json"
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        pcm.tofile(str(pcm_path))  # float32 mono, exactly kestrel's raw-PCM input
        req_path.write_text(
            json.dumps({
                "model": model_name(),
                "sample_rate": int(sample_rate),
                "timestamps": "word",
                "pcm_path": str(pcm_path),
                "pcm_dtype": "float32",
                "clips": [
                    {"start": float(cs), "end": float(ce)} for cs, ce in clips
                ],
            }),
            encoding="utf-8",
        )
    except Exception as exc:
        raise PhotonUnavailable(f"sidecar scratch write failed: {exc}") from exc

    flags = _NO_WINDOW
    if os.name == "nt":
        # Own process group: a console Ctrl+C aimed at the worker must not
        # race our timeout path, and the child never shares a console.
        flags |= getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)

    started = time.monotonic()
    try:
        proc = sp.Popen(
            [str(exe), str(script), str(req_path)],
            stdin=sp.DEVNULL,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            creationflags=flags,
        )
    except Exception as exc:
        raise PhotonUnavailable(f"sidecar spawn failed: {exc}") from exc

    try:
        out, err = proc.communicate(timeout=budget)
    except sp.TimeoutExpired:
        # The hang path. Kill BEFORE reporting: an orphan holding ~2 GB of
        # VRAM is exactly the failure this module has to prevent.
        pid = proc.pid
        gone = kill_tree(pid)
        try:
            proc.communicate(timeout=_KILL_GRACE_S)
        except Exception:
            pass
        _note_failure(
            "timeouts",
            f"no result within {budget:.0f}s (pid {pid}, killed={gone})",
        )
        raise PhotonTimeout(
            f"photon sidecar hung: no result within {budget:.0f}s "
            f"(pid {pid}, terminated={gone})",
            pid=pid,
        ) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    duration = time.monotonic() - started
    stderr_tail = (err or b"").decode("utf-8", "replace").strip()[-600:]
    if audio_sec <= 0:
        # The empty-transcript guard below is only honest if it knows how much
        # audio it was given; derive it here rather than trusting every caller
        # to pass it (forgetting it would silently disable the guard).
        audio_sec = float(getattr(pcm, "size", 0)) / float(sample_rate or 1)
    if proc.returncode != 0 or not out:
        _note_failure(
            "errors",
            f"sidecar exit {proc.returncode}: {stderr_tail or 'no stdout'}",
        )
        raise PhotonUnavailable(
            f"photon sidecar exit {proc.returncode}: {stderr_tail or 'no stdout'}"
        )
    try:
        payload = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception as exc:
        _note_failure("errors", f"unparseable response: {exc}")
        raise PhotonProtocolError(f"photon response is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        msg = str(payload.get("error") if isinstance(payload, dict) else payload)
        _note_failure("errors", f"sidecar reported failure: {msg}")
        raise PhotonUnavailable(f"photon sidecar reported: {msg}")
    clips_out = payload.get("clips")
    if not isinstance(clips_out, list) or len(clips_out) != len(clips):
        _note_failure("rejected", "clip count mismatch in response")
        raise PhotonProtocolError(
            f"photon returned {len(clips_out) if isinstance(clips_out, list) else '?'} "
            f"clips for {len(clips)} requests"
        )
    if audio_sec > _MIN_MEANINGFUL_AUDIO_SEC and not any(
        (c.get("segments") if isinstance(c, dict) else None) for c in clips_out
    ):
        # A whole batch of VAD'd speech returning nothing is a broken
        # accelerator, not silence. Falling back costs a slower, correct pass.
        _note_failure("rejected", "empty transcript for a non-empty batch")
        raise PhotonProtocolError(
            f"photon returned no segments for {audio_sec:.1f}s of speech"
        )
    _note_success(duration, audio_sec)
    logger.info(
        "photon sidecar ok: %d clips, %.1fs audio in %.2fs (%.1fx realtime)",
        len(clips_out), audio_sec, duration,
        (audio_sec / duration) if duration > 0 else 0.0,
    )
    return clips_out


def transcribe_batch(
    audio: Any,
    clips: list[tuple[float, float]],
    *,
    sample_rate: int,
    python: Optional[Path] = None,
    runner: Optional[Path] = None,
    timeout_s: Optional[float] = None,
) -> list[dict]:
    """Public entry: one Photon batch, or an exception. Never returns None -
    the fallback decision belongs to the caller, where the sherpa-onnx call
    it guards is visible."""
    with _lock:
        _state["attempts"] += 1
    return run_sidecar(
        pcm=audio,
        clips=list(clips),
        sample_rate=sample_rate,
        audio_sec=float(getattr(audio, "size", 0)) / float(sample_rate or 1),
        python=python,
        runner=runner,
        timeout_s=timeout_s,
    )
