"""Transcription worker — faster-whisper (CTranslate2) + Silero VAD for the local archive.

Consumes ``archive_jobs`` rows with kind='transcribe' (and kind='events' —
the PANNs acoustic-event stage, see services.archive_events) and writes
word-timestamped segments into the ``transcripts`` table (see
archive_db.insert_transcript).

Design decisions:
  * VAD pre-pass: Silero VAD splits the audio into speech regions; ONLY those are
    fed to the model. Non-speech never reaches the model and is NOT counted in
    progress — progress = speech seconds transcribed / total speech seconds.
  * Resume: each run writes a JSONL manifest next to the archive DB mapping
    chunk -> seg_idx range; re-runs skip chunks whose range is fully present
    (verified against transcript_for()). A chunk's segments are inserted with
    ONE insert_transcript() batch call, so a crash loses at most the
    in-flight chunk. A FULL re-run (no manifest, rows already present)
    replaces the old rows instead of appending a duplicate copy.
  * Model cache: one process-global WhisperModel by default (budget 1),
    lazy-loaded on first job, unloaded after VODRIP_WHISPER_IDLE_CLOSE
    seconds (default 600) without use. Multi-copy mode (budget > 1 — CPU
    workers or opt-in VODRIP_TRANSCRIBE_GPU_COPIES) gives each pool thread
    its own model so inference runs in parallel.
  * Device: detect_gpu_vendor() — 'nvidia' -> cuda/float16, everything else
    cpu/int8. This machine has an NVIDIA RTX 5080 (CUDA works via torch),
    so real runs are cuda/float16; the CPU path exists for GPU-less hosts.
  * Default model 'large-v3-turbo'; override with env VODRIP_WHISPER_MODEL
    (e.g. 'freds0/distil-whisper-large-v3-ptbr' or 'small').

Opt-in by design: app.py does NOT import this module. Start the worker with
``python -m services.archive_transcribe`` or ``start_worker()`` from a launcher.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import re
import shutil
import subprocess as sp
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from services import archive_db
from services.archive_events import detect_events_video, events_enabled
from services.disk_hygiene import active_whisper_model_id, whisper_cache_dir
from services.gpu_detect import detect_gpu_vendor
from services.os_services import _NO_WINDOW
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe, _resolve_ffprobe_exe

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
SAMPLE_RATE = 16000

# Env knobs (all optional).
LANG_ENV = "VODRIP_WHISPER_LANGUAGE"
WORKERS_ENV = "VODRIP_TRANSCRIBE_WORKERS"
IDLE_ENV = "VODRIP_WHISPER_IDLE_CLOSE"
GPU_COPIES_ENV = "VODRIP_TRANSCRIBE_GPU_COPIES"
BEAM_ENV = "VODRIP_WHISPER_BEAM"
BATCH_ENV = "VODRIP_WHISPER_BATCH"

# --- device / compute -----------------------------------------------------

@lru_cache(maxsize=1)
def _detect_device() -> tuple[str, str]:
    """(device, compute_type) — nvidia GPU or honest CPU fallback.

    VODRIP_WHISPER_DEVICE=cpu|cuda forces the choice (used by tests/benchmarks).
    """
    forced = os.environ.get("VODRIP_WHISPER_DEVICE", "").strip().lower()
    if forced:
        if forced == "cuda":
            return "cuda", "float16"
        if forced == "cpu":
            return "cpu", "int8"
        logger.warning("Unknown VODRIP_WHISPER_DEVICE=%r — ignoring", forced)
    vendor = detect_gpu_vendor()
    if vendor == "nvidia":
        return "cuda", "float16"
    return "cpu", "int8"


_device_override: Optional[tuple[str, str]] = None  # set after a CUDA failure
_model_device: Optional[str] = None  # device of the loaded _model (drives override reloads)


def _effective_device() -> tuple[str, str]:
    return _device_override or _detect_device()


def device_settings() -> tuple[str, str]:
    return _detect_device()


def model_name() -> str:
    # Shared resolver: VODRIP_WHISPER_MODEL env override -> settings.whisper_model -> default.
    return active_whisper_model_id()


def _cache_dir() -> Path:
    # Shared resolver: VODRIP_WHISPER_CACHE env -> settings.whisper_model_cache -> appdata.
    return whisper_cache_dir()


# --- parallelism budget ---------------------------------------------------

def _clamp_cuda_copies(copies: int, free_vram_bytes: int) -> int:
    """min(copies, max(1, free_vram // 2 GiB)) — the GPU copy budget clamp.

    Pure shape so the module self-check can pin it without a GPU: env 1 -> 1
    (never probe), env >1 -> VRAM-capped but never below 1 copy."""
    if copies <= 1:
        return 1
    vram_cap = max(1, free_vram_bytes // (2 * 1024 ** 3))
    return max(1, min(copies, vram_cap))


# Per-worker peak host-RAM estimates (system RAM, not VRAM). The real peak
# depends on model size, chunk length and ffmpeg decode buffers; the 20%
# headroom below is the safety net for estimate error.
# ponytail: estimates, not measurements — tuned for faster-whisper base/small
# int8 on CPU and for host-side buffers when the model lives on VRAM. If a
# machine OOMs at budget 2, lower the env knob or bump these constants.
# Upgrade path: track per-job RSS (psutil.Process().memory_info().rss around
# transcribe_video) and replace the constants with a rolling EMA.
_CPU_WORKER_RSS_EST = int(1.5 * 1024 ** 3)  # model + VAD + audio buffers
_GPU_COPY_RSS_EST = int(1.0 * 1024 ** 3)    # model on VRAM; audio + I/O host-side
_RAM_HEADROOM = 0.20  # fraction of free RAM never committed to the worker budget
_RAM_TTL_S = 5.0      # free-RAM readout cache TTL (not a syscall per job)

_ram_free_bytes = 0
_ram_free_at = 0.0
_ram_lock = threading.Lock()


def _free_system_ram_bytes() -> int:
    """Free system RAM in bytes (0 = unknown), cached for _RAM_TTL_S.

    Windows: kernel32 GlobalMemoryStatusEx via ctypes (psutil is not a
    declared dependency). POSIX fallback: sysconf(SC_AVPHYS_PAGES). This is
    the single call site the budget clamp uses, so tests patch it directly.
    """
    global _ram_free_bytes, _ram_free_at
    now = time.monotonic()
    with _ram_lock:
        if _ram_free_at and now - _ram_free_at < _RAM_TTL_S:
            return _ram_free_bytes
    free = 0
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                free = int(status.ullAvailPhys)
        else:
            free = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        free = 0  # probe failed — caller treats 0 as "unknown"
    with _ram_lock:
        _ram_free_bytes = free
        _ram_free_at = now
    return free


def _ram_worker_clamp(configured: int, per_worker_est: int) -> int:
    """min(configured, max(1, usable_free_ram // per_worker_est)).

    usable = free * (1 - _RAM_HEADROOM) — the headroom fraction of free RAM
    is never committed to the worker budget. Returns `configured` unchanged
    when free RAM is unknown (0), mirroring the VRAM probe-failure path.
    Never drops below 1; a configured 1 passes through untouched so the
    legacy single-model path is exact regardless of RAM."""
    if configured <= 1:
        return configured
    free = _free_system_ram_bytes()
    if free <= 0:
        return configured
    usable = int(free * (1.0 - _RAM_HEADROOM))
    return max(1, min(configured, usable // per_worker_est))


def _gpu_copies() -> int:
    """GPU model copies: VODRIP_TRANSCRIBE_GPU_COPIES (default 1) is a CEILING.

    Clamped by free VRAM (probed via torch.cuda.mem_get_info() — torch is
    already imported by the VAD path; probe failure degrades to trusting the
    env cap) AND by host RAM (_GPU_COPY_RSS_EST per copy — the model lives
    on VRAM, but audio decode + inference buffers are host-side). Never
    below 1 when the env configures > 0; 0/absent -> auto (1 copy)."""
    try:
        configured = int(os.environ.get(GPU_COPIES_ENV, "1") or "1")
    except ValueError:
        return 1
    if configured <= 0:
        configured = 1  # 0 == auto (same as absent)
    if configured == 1:
        return 1  # exact single-copy path — no probes at all
    free_vram = 0
    try:
        import torch

        if torch.cuda.is_available():
            free_vram = int(torch.cuda.mem_get_info()[0])
    except Exception:
        pass  # probe failed — trust the env cap
    if free_vram > 0:
        configured = _clamp_cuda_copies(configured, free_vram)
    return _ram_worker_clamp(configured, _GPU_COPY_RSS_EST)


def _worker_budget() -> int:
    """Max concurrent transcribe jobs: GPU model copies or CPU threads.

    The env knobs are CEILINGS, never floors: VODRIP_TRANSCRIBE_WORKERS
    (CPU, default 2) / VODRIP_TRANSCRIBE_GPU_COPIES (CUDA, default 1) cap
    the budget, and a system-RAM clamp (_ram_worker_clamp, 20% headroom)
    can only reduce it further. env == 1 always wins so the legacy
    single-model path is exact; 0/absent -> auto (CPU 2, GPU 1).

    budget == 1 is the EXACT legacy path: one process-global model,
    _infer_lock serializing inference. budget > 1 (opt-in GPU copies, or the
    CPU default) gives each pool thread its own model copy so inference
    truly runs in parallel."""
    device, _ = _effective_device()
    if device == "cpu":
        try:
            workers = int(os.environ.get(WORKERS_ENV, "2") or "2")
        except ValueError:
            return 2
        if workers <= 0:
            workers = 2  # 0 == auto (same as absent)
        return _ram_worker_clamp(workers, _CPU_WORKER_RSS_EST)
    return _gpu_copies()


# --- model cache ----------------------------------------------------------

_model_lock = threading.Lock()
_model: Any = None
_model_name: Optional[str] = None
_model_last_used = 0.0
_word_ts_ok = True  # distil models have no alignment heads; flipped per process


def _idle_close_seconds() -> float:
    try:
        return float(os.environ.get(IDLE_ENV, "600") or "600")
    except ValueError:
        return 600.0


def _ensure_cuda_libs() -> None:
    """Expose pip-installed NVIDIA runtime DLLs (nvidia-*-cu12 wheels) on PATH.

    ctranslate2 loads cublas64_12.dll lazily at first CUDA inference; machines
    with a CUDA-13-era driver but no full CUDA 12 toolkit otherwise fail with
    "Library cublas64_12.dll is not found". Set VODRIP_NO_CUDA_LIBS to skip.
    """
    if os.environ.get("VODRIP_NO_CUDA_LIBS"):
        return
    try:
        import site as _site
    except Exception:
        return
    for lib in ("cublas", "cuda_runtime"):
        try:
            for root in _site.getsitepackages():
                d = Path(root) / "nvidia" / lib / "bin"
                if d.is_dir() and str(d) not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            # ponytail: best-effort — missing wheels just mean CPU fallback
            pass


def _get_model() -> Any:
    """Return the process-global WhisperModel, lazy-loading on first use.

    Re-loads if VODRIP_WHISPER_MODEL changed since last load. Not thread-safe
    for *concurrent* transcribe() calls — callers serialize via _infer_lock.
    """
    global _model, _model_name, _model_last_used, _device_override, _model_device
    name = model_name()
    with _model_lock:
        if (
            _model is not None
            and _model_name == name
            and _model_device == _effective_device()[0]
        ):
            _model_last_used = time.monotonic()
            return _model
        _close_model_unlocked()  # different model env -> drop the old one first
        _ensure_cuda_libs()  # must precede the ctranslate2 import/DLL load
        from faster_whisper import WhisperModel

        device, compute_type = _effective_device()
        t0 = time.monotonic()
        logger.info(
            "Loading whisper model %r (device=%s compute_type=%s, cache=%s)...",
            name, device, compute_type, _cache_dir(),
        )
        try:
            _model = WhisperModel(
                name,
                device=device,
                compute_type=compute_type,
                download_root=str(_cache_dir()),
            )
        except Exception as exc:
            if device == "cuda":
                # Driver/GPU hiccup — fall back to CPU for the process lifetime
                # instead of failing every queued job.
                logger.warning("CUDA model load failed (%s) — falling back to CPU", exc)
                _device_override = ("cpu", "int8")
                device, compute_type = _device_override
                _model = WhisperModel(
                    name,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(_cache_dir()),
                )
            else:
                raise
        _model_name = name
        _model_device = device
        _model_last_used = time.monotonic()
        logger.info("Whisper model %r loaded in %.1fs", name, time.monotonic() - t0)
        return _model


def _close_model_unlocked() -> None:
    """Drop the cached model — caller MUST hold _model_lock."""
    global _model, _model_name, _model_device
    model, _model = _model, None
    _model_name = None
    _model_device = None
    if model is not None:
        logger.info("Unloading whisper model")
        del model
        gc.collect()


def close_model() -> None:
    """Unload the cached model, freeing its RAM. Safe mid-transcription: workers
    hold a local reference, so the object lives until their last use.

    Also drops the cached VAD model (lazy-reloaded by the next job) and, in
    multi-copy mode, every pool thread's model too; threads lazily reload on
    their next job (the registry is cleared, so a fresh slot is created)."""
    global _vad
    closed_any = False
    with _model_lock:
        _close_model_unlocked()
        for slot in _thread_slots.values():
            model, slot.model = slot.model, None
            if model is not None:
                logger.info("Unloading whisper thread model")
                del model
                closed_any = True
        _thread_slots.clear()
    with _vad_lock:
        vad, _vad = _vad, None
        if vad is not None:
            logger.info("Unloading VAD model")
            del vad
            closed_any = True
    if closed_any:
        gc.collect()


def _maybe_close_idle_model() -> None:
    """Close the model after VODRIP_WHISPER_IDLE_CLOSE seconds without use.

    Applies to the process-global model only: thread models die with the
    pool (close_model on worker shutdown)."""
    if _model is None:
        return
    idle = time.monotonic() - _model_last_used
    if idle > _idle_close_seconds():
        logger.info("Model idle for %.0fs — unloading", idle)
        close_model()


# --- per-thread model copies (multi-copy mode, budget > 1) ------------------
# Each pool thread owns one WhisperModel instance so inference runs truly in
# parallel (no global _infer_lock). The registry is keyed by thread ident —
# the same per-thread keying CPython's threading.local uses internally.
# Model CREATION is serialized by _model_lock (shared hub download);
# inference never takes it. A thread whose CUDA inference OOM'd marks itself
# cpu_fallback and reloads on CPU — only that thread degrades.

_multi_tls = threading.local()  # per-thread: .active, .cpu_fallback


class _ThreadModelSlot:
    """One pool thread's lazy model state."""
    __slots__ = ("model", "model_name")

    def __init__(self) -> None:
        self.model: Any = None
        self.model_name: Optional[str] = None


_thread_slots: dict[int, _ThreadModelSlot] = {}


def _in_multi_mode() -> bool:
    return bool(getattr(_multi_tls, "active", False))


def _thread_cpu_fallback() -> bool:
    return bool(getattr(_multi_tls, "cpu_fallback", False))


def _thread_mark_cpu_fallback() -> None:
    _multi_tls.cpu_fallback = True


def _thread_slot() -> _ThreadModelSlot:
    """The calling thread's model slot, created on first use."""
    tid = threading.get_ident()
    slot = _thread_slots.get(tid)
    if slot is None:
        with _model_lock:
            slot = _thread_slots.get(tid)
            if slot is None:
                slot = _ThreadModelSlot()
                _thread_slots[tid] = slot
    return slot


def _thread_model() -> Any:
    """Lazy per-thread WhisperModel copy (multi-copy mode only).

    Mirrors _get_model's lazy load, keyed to the calling thread; creation is
    serialized by _model_lock, inference is not. A thread marked cpu_fallback
    loads on CPU even when CUDA is healthy (its copy OOM'd earlier)."""
    slot = _thread_slot()
    name = model_name()
    if slot.model is not None and slot.model_name == name:
        return slot.model
    with _model_lock:
        if slot.model is not None and slot.model_name == name:
            return slot.model
        if slot.model is not None:
            del slot.model  # stale model env / degraded copy — drop first
            slot.model = None
        _ensure_cuda_libs()
        from faster_whisper import WhisperModel

        device, compute_type = _effective_device()
        if _thread_cpu_fallback() and device == "cuda":
            device, compute_type = "cpu", "int8"
        t0 = time.monotonic()
        logger.info(
            "Loading whisper thread model %r (device=%s compute_type=%s)...",
            name, device, compute_type,
        )
        try:
            slot.model = WhisperModel(
                name,
                device=device,
                compute_type=compute_type,
                download_root=str(_cache_dir()),
            )
        except Exception as exc:
            if device == "cuda":
                # This thread's copy cannot load on CUDA (driver hiccup) —
                # degrade only this thread to CPU, not the whole process.
                logger.warning(
                    "thread CUDA model load failed (%s) — thread falls back to CPU", exc
                )
                _thread_mark_cpu_fallback()
                slot.model = WhisperModel(
                    name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(_cache_dir()),
                )
            else:
                raise
        slot.model_name = name
        logger.info("Thread whisper model %r loaded in %.1fs", name, time.monotonic() - t0)
        return slot.model


def _current_model() -> Any:
    """The model for the current context: the calling thread's own copy in
    multi-copy mode, else the process-global one. Direct callers of
    transcribe_video()/_get_model() (tests, API) always get the global path."""
    if _in_multi_mode():
        return _thread_model()
    return _get_model()


# --- audio decode ---------------------------------------------------------

def decode_audio(path: str, ffmpeg_bin: Optional[str] = None) -> "Any":
    """Decode any media file to mono 16 kHz float32 samples via ffmpeg.

    Returns a numpy float32 1-D array (16k samples/sec).
    """
    import numpy as np

    if ffmpeg_bin is None:
        ffmpeg_bin = _resolve_ffmpeg_exe()
    cmd = [
        ffmpeg_bin, "-nostdin", "-v", "error", "-i", str(path),
        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
    ]
    proc = sp.run(cmd, capture_output=True, timeout=3600, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"ffmpeg decode failed for {path}: {stderr}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError(f"ffmpeg produced no audio from {path}")
    return samples.copy()  # writable copy: torch.from_numpy needs writable memory


# --- sharded decode (bounded-RAM path) ------------------------------------
# Long media is decoded ONCE into fixed-duration float32 shards on disk (one
# ffmpeg pass piping 16 kHz mono PCM), then consumed one window at a time by
# VAD / whisper / events. Peak RAM is bounded by a shard window, never by the
# media length (a 13.5 h VOD = ~3.1 GB decoded — not resident all at once).

SHARD_SEC_ENV = "VODRIP_TRANSCRIBE_SHARD_SEC"
SHARD_MIN_SEC_ENV = "VODRIP_TRANSCRIBE_SHARD_MIN_SEC"
DEFAULT_SHARD_SEC = 300.0  # ~19 MB of float32 16 kHz PCM per shard
SHARD_THRESHOLD_SEC = 15 * 60.0  # decoded length above which transcribe_video shards
_VAD_OVERLAP_SEC = 1.5  # VAD context on each side of a shard
_VAD_MERGE_GAP_SEC = 0.5  # cross-shard speech regions closer than this merge


def _shard_seconds() -> float:
    """Fixed shard duration (VODRIP_TRANSCRIBE_SHARD_SEC, default 300 s)."""
    try:
        return max(1.0, float(os.environ.get(SHARD_SEC_ENV, "") or DEFAULT_SHARD_SEC))
    except ValueError:
        return DEFAULT_SHARD_SEC


def _shard_threshold_sec() -> float:
    """Decoded-length threshold that routes to the sharded path. The env knob
    is a test hook — real runs always use the 15 min default."""
    try:
        return max(1.0, float(os.environ.get(SHARD_MIN_SEC_ENV, "") or SHARD_THRESHOLD_SEC))
    except ValueError:
        return SHARD_THRESHOLD_SEC


def _probe_duration_sec(path: str, ffmpeg_bin: Optional[str] = None) -> Optional[float]:
    """Best-effort media duration via ffprobe; None when it cannot be told."""
    if ffmpeg_bin is None:
        ffmpeg_bin = _resolve_ffmpeg_exe()
    ffprobe = _resolve_ffprobe_exe(ffmpeg_bin)
    if not ffprobe:
        return None
    try:
        out = sp.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
        )
        if out.returncode != 0:
            return None
        return float(out.stdout.strip())
    except (OSError, ValueError):
        return None


def _should_shard(path: str, ffmpeg_bin: Optional[str] = None) -> bool:
    """Route to the sharded decode path when the decoded PCM would exceed the
    RAM budget. An unknown duration (no ffprobe) shards rather than risk a
    multi-GB allocation — a small file on the sharded path behaves identically
    (it fits inside one shard, so VAD sees the same single window)."""
    duration = _probe_duration_sec(path, ffmpeg_bin)
    return duration is None or duration >= _shard_threshold_sec()


def _shard_sample_bounds(i: int, shard_sec: float) -> tuple[int, int]:
    """Sample range [lo, hi) of shard i; consecutive shards are contiguous,
    so range reads across boundaries stay exact."""
    return int(i * shard_sec * SAMPLE_RATE), int((i + 1) * shard_sec * SAMPLE_RATE)


class _ShardedAudio:
    """Fixed-duration float32 shards on disk plus absolute range reads.

    Shard i covers samples [_shard_sample_bounds(i)); read() returns any
    absolute [start, end) window as one array, so VAD / whisper / events
    consume shards without ever holding more than one window in RAM."""

    __slots__ = ("files", "shard_sec", "total_samples", "total_sec")

    def __init__(self, files: list, shard_sec: float) -> None:
        self.files = list(files)
        self.shard_sec = float(shard_sec)
        self.total_samples = sum(Path(f).stat().st_size // 4 for f in self.files)
        self.total_sec = self.total_samples / SAMPLE_RATE

    def read(self, start_sec: float, end_sec: float) -> Any:
        """Concatenated float32 16 kHz samples for an absolute window."""
        import numpy as np

        s0 = max(0, int(start_sec * SAMPLE_RATE))
        e0 = min(self.total_samples, int(end_sec * SAMPLE_RATE))
        if e0 <= s0:
            return np.zeros(0, dtype=np.float32)
        parts: list[Any] = []
        for i, fpath in enumerate(self.files):
            fs, fe = _shard_sample_bounds(i, self.shard_sec)
            if e0 <= fs or s0 >= fe:
                continue
            lo = max(s0, fs) - fs
            hi = min(e0, fe) - fs
            parts.append(np.fromfile(fpath, dtype=np.float32, count=hi - lo, offset=lo * 4))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def _decode_to_shards(
    path: str,
    ffmpeg_bin: Optional[str] = None,
    shard_sec: Optional[float] = None,
    out_dir: Optional[str] = None,
) -> Iterator[tuple[float, Any]]:
    """Decode ONCE to fixed-duration float32 shard files; yield (start_sec, np.ndarray).

    One ffmpeg process pipes mono 16 kHz f32 to stdout; stdout is sliced into
    shard-sized buffers and each spilled to ``<out_dir>/shard_%06d.f32``.
    Peak RAM is a couple of shard buffers, independent of media length.
    With out_dir=None a temp dir is created and removed when the iterator is
    exhausted or closed (also on failure); callers that need the files after
    the loop (transcribe_video) pass their own dir and own its lifecycle."""
    import numpy as np

    if shard_sec is None:
        shard_sec = _shard_seconds()
    if ffmpeg_bin is None:
        ffmpeg_bin = _resolve_ffmpeg_exe()
    own_dir = out_dir is None
    tmpdir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="vodrip-shards-"))
    shard_bytes = int(shard_sec * SAMPLE_RATE) * np.dtype(np.float32).itemsize
    cmd = [
        ffmpeg_bin, "-nostdin", "-v", "error", "-i", str(path),
        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
    ]
    proc: Optional[sp.Popen] = None
    try:
        proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, creationflags=_NO_WINDOW)
        idx = 0
        while True:
            raw = proc.stdout.read(shard_bytes)
            if not raw:
                break
            fpath = tmpdir / f"shard_{idx:06d}.f32"
            fpath.write_bytes(raw)
            arr = np.frombuffer(raw, dtype=np.float32).copy()  # writable copy
            yield idx * shard_sec, arr
            idx += 1
        proc.wait()
        if proc.returncode != 0:
            stderr = (proc.stderr.read() or b"").decode("utf-8", "replace")[-300:]
            raise RuntimeError(f"ffmpeg decode failed for {path}: {stderr}")
    finally:
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if proc.poll() is None:  # abandoned mid-yield — stop the decode
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
        if own_dir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _merge_speech_regions(
    regions: list[tuple[float, float]], gap: float = _VAD_MERGE_GAP_SEC
) -> list[tuple[float, float]]:
    """Merge regions closer than ``gap`` (sorted by start; absolute offsets).

    Also collapses duplicates — adjacent shards both report a region that
    straddles their boundary, and the merge must fuse them into one. The gap
    (0.5 s) sits below _plan_chunks' merge gap (0.8 s), so the final chunk
    plan matches the full-array path whenever region edges agree."""
    merged: list[tuple[float, float]] = []
    for s, e in sorted(regions):
        if merged and s - merged[-1][1] <= gap:
            ps, pe = merged[-1]
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _vad_speech_seconds_sharded(sharded_audio: _ShardedAudio) -> list[tuple[float, float]]:
    """Silero VAD over fixed-duration shards, one window at a time.

    Each shard is VAD'd on [i*S - overlap, (i+1)*S + overlap) so the
    stateless-per-call model warms up before the shard's own zone; regions
    intersecting the authoritative zone are kept with ABSOLUTE offsets, then
    merged across boundaries (gap <= 0.5 s). VAD internals (vad_speech_seconds)
    are called unchanged, per shard."""
    regions: list[tuple[float, float]] = []
    for i in range(len(sharded_audio.files)):
        zone_lo = i * sharded_audio.shard_sec
        zone_hi = min(sharded_audio.total_sec, (i + 1) * sharded_audio.shard_sec)
        lo = max(0.0, zone_lo - _VAD_OVERLAP_SEC)
        hi = min(sharded_audio.total_sec, (i + 1) * sharded_audio.shard_sec + _VAD_OVERLAP_SEC)
        if hi <= lo:
            continue
        for local_s, local_e in vad_speech_seconds(sharded_audio.read(lo, hi)):
            s, e = lo + local_s, lo + local_e
            if e > zone_lo and s < zone_hi:  # keep regions touching this shard's zone
                regions.append((s, e))
    return _merge_speech_regions(regions)


def _clips_to_audio(
    sharded_audio: _ShardedAudio, run: list
) -> tuple[Any, list[tuple[float, float]], list[float]]:
    """One contiguous array for a batch of clips: the clips' SPEECH
    concatenated (not their wall-clock span — a batch can span hours of a
    13.5 h VOD), plus concat-relative clip timestamps and absolute offsets.

    Batched inference sees the same per-clip windows as the full-audio path
    (clip-local mel extraction), while peak RAM is bounded by the batch's
    speech seconds."""
    import numpy as np

    parts: list[Any] = []
    clips: list[tuple[float, float]] = []
    offsets: list[float] = []
    pos = 0
    for _, (cs, ce) in run:
        part = sharded_audio.read(cs, ce)
        parts.append(part)
        clips.append((pos / SAMPLE_RATE, (pos + part.size) / SAMPLE_RATE))
        offsets.append(cs)
        pos += part.size
    audio = parts[0] if len(parts) == 1 else np.concatenate(parts)
    return audio, clips, offsets


# --- VAD pre-pass ---------------------------------------------------------

_vad_lock = threading.Lock()
_vad: Any = None

# Silero v5.1 (16 kHz) — mirror the legacy get_speech_timestamps call exactly:
# threshold 0.5, neg threshold 0.35 (= threshold - 0.15), min speech 250 ms,
# min silence 200 ms, speech pad 30 ms, max speech inf (never splits).
_VAD_WINDOW = 512        # samples per window
_VAD_CONTEXT = 64        # look-back samples the model prepends to each window
_VAD_HOP = 128           # STFT hop inside the model (filter_length 256)
_VAD_LSTM_HIDDEN = 128   # LSTM hidden size (state carried across windows)
_VAD_STFT_FREQS = 129    # filter_length // 2 + 1 (basis rows are real+imag halves)
_VAD_ENCODER_STRIDES = (1, 2, 2, 1)
_VAD_CHUNK_WINDOWS = 4096  # ~2 min of audio per encoder pass (memory bound)
_VAD_THRESHOLD = 0.5
_VAD_NEG_THRESHOLD = 0.35
_VAD_MIN_SPEECH_MS = 250
_VAD_MIN_SILENCE_MS = 200
_VAD_PAD_MS = 30


def _get_vad() -> Any:
    """Lazy Silero VAD model (bundled in the package — no download).

    Default: the torch .jit model, driven by a stateful BATCHED
    reimplementation of the per-window loop (see _vad_probs_torch). The old
    get_speech_timestamps loop cost one model() call + a .item() sync per
    512-sample window and was the dominant per-VOD cost (~26x realtime vs
    118-164x inference); the batched pass measures ~170x realtime on CPU on
    a 10.8s clip (~3x the legacy loop) with the same regions — validated
    window-by-window against get_speech_timestamps.

    VODRIP_VAD_ONNX=1 switches to the bundled ONNX model via onnxruntime
    (CUDA ExecutionProvider when usable, CPU fallback) — same regions,
    still per-window because the ONNX graph runs one LSTM step per call.
    """
    global _vad
    with _vad_lock:
        if _vad is None:
            if os.environ.get("VODRIP_VAD_ONNX", "").strip() == "1":
                _vad = _load_onnx_vad()
            else:
                from silero_vad import load_silero_vad

                _vad = load_silero_vad(onnx=False)
        return _vad

class _OnnxVad:
    """Minimal stateful wrapper over the bundled silero ONNX session.

    Mirrors silero's OnnxWrapper (LSTM state + 64-sample context carried
    across per-window calls) but numpy-only — no torch conversion or .item()
    in the loop. Not thread-safe: vad_speech_seconds serializes on
    _vad_lock, same as the torch model."""

    def __init__(self, session: Any) -> None:
        import numpy as np

        self.session = session
        self._np = np
        self.reset_states()

    def reset_states(self) -> None:
        self._state = self._np.zeros((2, 1, _VAD_LSTM_HIDDEN), dtype=self._np.float32)
        self._context = self._np.zeros((1, _VAD_CONTEXT), dtype=self._np.float32)
    def prob(self, window: "Any") -> float:
        """Speech probability for one 512-sample float32 window."""
        chunk = self._np.asarray(window, dtype=self._np.float32).reshape(1, -1)
        if chunk.shape[1] < _VAD_WINDOW:
            chunk = self._np.pad(chunk, ((0, 0), (0, _VAD_WINDOW - chunk.shape[1])))
        inp = self._np.concatenate([self._context, chunk], axis=1)
        out, state = self.session.run(
            None,
            {
                "input": inp,
                "state": self._state,
                "sr": self._np.array(SAMPLE_RATE, dtype="int64"),
            },
        )
        self._state = state
        self._context = inp[:, -_VAD_CONTEXT:]
        return float(out[0, 0])


def _load_onnx_vad() -> _OnnxVad:
    """Silero VAD via onnxruntime (VODRIP_VAD_ONNX=1).

    Uses the .onnx bundled with the silero_vad package (same v5.1 weights as
    the .jit — no export needed). CUDA ExecutionProvider is requested first;
    onnxruntime itself drops it when the CUDA DLLs are missing and we retry
    CPU-only if session creation still raises. Imports are local: the
    default path never touches onnxruntime."""
    import importlib.resources as ir
    import onnxruntime

    path = ir.files("silero_vad.data").joinpath("silero_vad.onnx")
    opts = onnxruntime.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    try:
        session = onnxruntime.InferenceSession(
            str(path),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            sess_options=opts,
        )
    except Exception:
        session = onnxruntime.InferenceSession(
            str(path), providers=["CPUExecutionProvider"], sess_options=opts
        )
    logger.info("VAD onnxruntime providers: %s", session.get_providers())
    return _OnnxVad(session)


def _vad_probs_torch(audio: "Any", vad: Any) -> "Any":
    """Per-window speech probabilities — stateful batched silero v5.1 pass.

    Exact replication of silero's per-window loop, split at the model's one
    sequential point: the encoder (STFT conv + 4 conv blocks) is parallel
    over windows, so it runs as a few big tensor ops per chunk of windows;
    the stateful LSTM stays a per-window recurrence, but over precomputed
    linear projections (one 128x128 matmul per step instead of a full model
    call). Max |Δprob| vs the legacy loop is ~4e-6 on real speech (float
    reassociation only). The LSTM state carries across encoder chunks, so
    long audio needs no big (n, 576) matrix — the per-chunk one is ~9 MB.
    """
    import numpy as np
    import torch
    import torch.nn.functional as F

    weights = vad.state_dict()
    basis = weights["_model.stft.forward_basis_buffer"]
    n = (len(audio) + _VAD_WINDOW - 1) // _VAD_WINDOW
    padded = np.concatenate(
        [
            np.zeros(_VAD_CONTEXT, dtype=np.float32),
            audio,
            np.zeros(n * _VAD_WINDOW - len(audio), dtype=np.float32),
        ]
    )
    probs = np.empty(n, dtype=np.float32)
    with torch.no_grad():
        w_ih = weights["_model.decoder.rnn.weight_ih"]
        w_hh = weights["_model.decoder.rnn.weight_hh"]
        b_ih = weights["_model.decoder.rnn.bias_ih"]
        b_hh = weights["_model.decoder.rnn.bias_hh"]
        dec_w = weights["_model.decoder.decoder.2.weight"]
        dec_b = weights["_model.decoder.decoder.2.bias"]
        h = torch.zeros(_VAD_LSTM_HIDDEN)
        c = torch.zeros(_VAD_LSTM_HIDDEN)
        row = np.arange(_VAD_CONTEXT + _VAD_WINDOW)[None, :]
        for a in range(0, n, _VAD_CHUNK_WINDOWS):
            b = min(a + _VAD_CHUNK_WINDOWS, n)
            # windows a..b-1 as (m, 576): 64-sample look-back + 512-sample
            # window, strided 512 — the model's own context concatenation.
            idx = row + _VAD_WINDOW * np.arange(a, b)[:, None]
            windows = torch.from_numpy(padded[idx].copy())
            spec = F.conv1d(
                F.pad(windows, (0, 64), "reflect").unsqueeze(1),
                basis,
                stride=_VAD_HOP,
            )
            feat = torch.sqrt(spec[:, :_VAD_STFT_FREQS] ** 2 + spec[:, _VAD_STFT_FREQS:] ** 2)
            for i in range(4):
                feat = F.relu(
                    F.conv1d(
                        feat,
                        weights[f"_model.encoder.{i}.reparam_conv.weight"],
                        weights[f"_model.encoder.{i}.reparam_conv.bias"],
                        stride=_VAD_ENCODER_STRIDES[i],
                        padding=1,
                    )
                )
            feat = feat[:, :, -1].contiguous()  # (m, 128) — LSTM input per window
            gates_lin = F.linear(feat, w_ih, b_ih)
            H = torch.empty(b - a, _VAD_LSTM_HIDDEN)
            for w in range(b - a):
                i_, f_, g_, o_ = (gates_lin[w] + F.linear(h, w_hh, b_hh)).chunk(4)
                c = torch.sigmoid(f_) * c + torch.sigmoid(i_) * torch.tanh(g_)
                h = torch.sigmoid(o_) * torch.tanh(c)
                H[w] = h
            # decoder: relu -> 1x1 conv -> sigmoid — a per-window map, vectorized
            dec = F.conv1d(F.relu(H.unsqueeze(-1)), dec_w, dec_b)
            probs[a:b] = torch.sigmoid(dec.squeeze(1).mean(dim=1)).numpy()
    return probs


def _vad_probs_onnx(audio: "Any", vad: _OnnxVad) -> "Any":
    """Per-window probabilities via the onnxruntime session.

    The ONNX graph runs one LSTM step per call with explicit state, so this
    path is still per-window — but pure numpy + ORT kernels (no torch
    dispatch, no .item() on a torch tensor), so it is faster than the legacy
    loop and needs no export. Matches the torch path to ~2e-6 on real speech
    (same exported weights)."""
    import numpy as np

    vad.reset_states()
    n = (len(audio) + _VAD_WINDOW - 1) // _VAD_WINDOW
    probs = np.empty(n, dtype=np.float32)
    for i in range(n):
        probs[i] = vad.prob(audio[i * _VAD_WINDOW:(i + 1) * _VAD_WINDOW])
    return probs


def _vad_regions(probs: "Any", audio_len: int) -> list[tuple[float, float]]:
    """(start_sec, end_sec) regions from per-window probabilities.

    Port of silero's get_speech_timestamps post-processing for the
    parameters vad_speech_seconds uses (threshold/neg threshold, min speech
    250 ms, min silence 200 ms, pad 30 ms, max speech inf): the triggered /
    temp_end state machine runs over runs of non-speech windows (a close
    needs a neg window at least 200 ms after the first neg window of the
    run), then the padding and second-rounding rules are copied verbatim."""
    import numpy as np

    speech = probs >= _VAD_THRESHOLD
    neg = probs < _VAD_NEG_THRESHOLD
    n = len(probs)
    min_sil = SAMPLE_RATE * _VAD_MIN_SILENCE_MS / 1000   # 3200 samples
    min_speech = SAMPLE_RATE * _VAD_MIN_SPEECH_MS / 1000  # 4000 samples
    pad = SAMPLE_RATE * _VAD_PAD_MS / 1000                # 480 samples
    close_delay = int(np.ceil(min_sil / _VAD_WINDOW))     # 7 windows

    regions: list[tuple[int, int]] = []
    triggered = False
    start = 0
    i = 0
    while i < n:
        if speech[i]:  # prob >= threshold: start a region or keep it running
            if not triggered:
                triggered = True
                start = i * _VAD_WINDOW
            i += 1
            continue
        # run of non-speech windows [i, j): hysteresis + silence-close logic
        j = i
        while j < n and not speech[j]:
            j += 1
        if triggered:
            first_neg = next((k for k in range(i, j) if neg[k]), None)
            if first_neg is not None:
                for k in range(first_neg + close_delay, j):
                    if neg[k]:
                        if first_neg * _VAD_WINDOW - start > min_speech:
                            regions.append((start, first_neg * _VAD_WINDOW))
                        triggered = False
                        break
        i = j
    if triggered and audio_len - start > min_speech:
        regions.append((start, audio_len))

    # speech_pad_ms padding — verbatim port of get_speech_timestamps.
    for i_ in range(len(regions)):
        st, en = regions[i_]
        if i_ == 0:
            st = max(0, st - pad)
        if i_ != len(regions) - 1:
            gap = regions[i_ + 1][0] - en
            if gap < 2 * pad:
                en += int(gap // 2)
                regions[i_ + 1] = (max(0, regions[i_ + 1][0] - gap // 2), regions[i_ + 1][1])
            else:
                en = min(audio_len, en + pad)
                regions[i_ + 1] = (max(0, regions[i_ + 1][0] - pad), regions[i_ + 1][1])
        else:
            en = min(audio_len, en + pad)
        regions[i_] = (st, en)

    audio_len_sec = audio_len / SAMPLE_RATE
    return [
        (
            max(round(st / SAMPLE_RATE, 1), 0.0),
            min(round(en / SAMPLE_RATE, 1), audio_len_sec),
        )
        for st, en in regions
    ]


def vad_speech_seconds(audio: "Any") -> list[tuple[float, float]]:
    """Return [(start_sec, end_sec), ...] speech regions from Silero VAD.

    Stateful batched pass: windows are scored in chunks of ~2 min with a
    handful of tensor ops each (the STFT + conv encoder is parallel over
    windows; only the 128-dim LSTM recurrence stays sequential), so the
    per-window .item() sync and model dispatch of the legacy
    get_speech_timestamps loop are gone. Regions come out of the batched
    probability vector with the same state machine, padding and rounding —
    same regions on real audio (validated to the window on TTS speech)."""
    if audio is None or len(audio) == 0:
        return []  # empty audio -> no speech
    vad = _get_vad()
    # The model is STATEFUL and not thread-safe: two workers running VAD
    # concurrently corrupt each other's state (select() out-of-range crash,
    # reproduced with 2 concurrent jobs). The whisper path builds per-thread
    # model copies for the same reason; here one shared lock is enough —
    # VAD is CPU-bound, so serializing it costs no throughput.
    with _vad_lock:
        if isinstance(vad, _OnnxVad):
            probs = _vad_probs_onnx(audio, vad)
        else:
            probs = _vad_probs_torch(audio, vad)
    return _vad_regions(probs, len(audio))


_MAX_CHUNK_SEC = 30.0  # faster-whisper mel window — longer clips get truncated


def _plan_chunks(
    speech: list[tuple[float, float]],
    merge_gap: float = 0.8,
    min_len: float = 0.25,
) -> list[tuple[float, float]]:
    """Merge nearby speech regions into transcribe chunks; drop sub-minimum ones.

    Chunks are capped at _MAX_CHUNK_SEC: faster-whisper's batched pipeline
    trims every clip's mel features to 30 s (pad_or_trim to 3000 frames), so
    an uncapped continuous-speech run would silently transcribe ONLY its
    first 30 s ("Segment N is longer than 30 seconds" warning).

    Deterministic — resume relies on identical chunks across runs.
    """
    chunks: list[tuple[float, float]] = []
    cur_s = cur_e = None
    for s, e in speech:
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s - cur_e <= merge_gap:
            cur_e = max(cur_e, e)
        else:
            _close_chunk(chunks, cur_s, cur_e, min_len)
            cur_s, cur_e = s, e
        while cur_e - cur_s > _MAX_CHUNK_SEC:
            _close_chunk(chunks, cur_s, cur_s + _MAX_CHUNK_SEC, min_len)
            cur_s = cur_s + _MAX_CHUNK_SEC
    if cur_s is not None:
        _close_chunk(chunks, cur_s, cur_e, min_len)
    return chunks


def _close_chunk(
    chunks: list[tuple[float, float]],
    s: float,
    e: float,
    min_len: float,
) -> None:
    if e - s >= min_len:
        chunks.append((s, e))


# --- resume manifest (JSONL) ----------------------------------------------
# One line per completed chunk: {"ci": i, "first": first_seg_idx, "count": n}.
# Header line (rewritten each run) carries the chunk plan so stale entries
# (audio changed / model changed) are ignored instead of misapplied.

def _sanitize_key(key: str) -> str:
    return re.sub(r"[^\w.-]", "_", key)


def _manifest_path(platform: str, video_id: str) -> Path:
    base = Path(archive_db._db_path()).parent / "whisper_manifest"
    return base / f"{platform}__{_sanitize_key(video_id)}.jsonl"


def _read_manifest(path: Path) -> tuple[Optional[dict], dict[int, dict]]:
    """Return (header, {ci: entry}) — missing/corrupt file yields empty plan."""
    header: Optional[dict] = None
    entries: dict[int, dict] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "chunks" in data:
                    header = data
                elif isinstance(data.get("ci"), int):
                    entries[data["ci"]] = data
    except OSError:
        pass
    return header, entries


def _write_manifest_header(path: Path, chunks: list[tuple[float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"chunks": chunks, "model": model_name()}) + "\n")


def _append_manifest_entry(path: Path, ci: int, first: int, count: int) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ci": ci, "first": first, "count": count}) + "\n")


def _resume_plan(
    chunks: list[tuple[float, float]],
    header: Optional[dict],
    entries: dict[int, dict],
    existing: set[int],
) -> tuple[list[int], int]:
    """Return (chunk indices to transcribe, next free seg_idx).

    Entries are trusted only when the header matches the current plan and model.
    A chunk is also re-transcribed when any row in its recorded seg_idx range is
    missing (manual delete / partial write), and the next free index is the
    LOWEST gap in existing — so deleted rows are restored at their old index
    and the seg_idx sequence stays contiguous."""
    if not chunks:
        return [], 0
    next_idx = 0
    while next_idx in existing:
        next_idx += 1
    if not (header and entries):
        return list(range(len(chunks))), next_idx
    # JSON round-trip turns the plan's tuples into lists — compare shapes.
    if [tuple(c) for c in header.get("chunks", [])] != chunks or header.get("model") != model_name():
        return list(range(len(chunks))), next_idx
    missing: list[int] = []
    for ci in range(len(chunks)):
        entry = entries.get(ci)
        if entry is None:
            missing.append(ci)
            continue
        rng = range(entry["first"], entry["first"] + entry["count"])
        if any(i not in existing for i in rng):
            missing.append(ci)  # rows deleted since the last run
    return missing, next_idx


# --- transcription --------------------------------------------------------

_infer_lock = threading.Lock()  # CTranslate2 model instances are not thread-safe


def _beam_size() -> int:
    """Whisper beam width: VODRIP_WHISPER_BEAM (default 1).

    Measured on PT-BR VOD slices (corpus-anchored word overlap) beam 1 ==
    beam 5 for this workload, and greedy halves decoder work; raise via env
    when max quality is wanted."""
    try:
        return max(1, int(os.environ.get(BEAM_ENV, "1") or "1"))
    except ValueError:
        return 1


def _batch_size() -> int:
    """Clips decoded per batched call: VODRIP_WHISPER_BATCH (CUDA 16, CPU 4).

    CUDA default 16 keeps a 5080-class GPU saturated; CPU (int8) defaults to
    4 so the batched features stay small. The env cap applies to both."""
    default = 16 if _effective_device()[0] == "cuda" else 4
    try:
        return max(1, int(os.environ.get(BATCH_ENV, str(default)) or str(default)))
    except ValueError:
        return default


def _transcribe_batch(
    model: Any,
    audio: "Any",
    chunks: list[tuple[float, float]],
    language: Optional[str],
    *,
    clip_offsets: Optional[list[float]] = None,
) -> list[tuple[list[dict], Optional[str]]]:
    """Batch-decode [start,end] clips via faster-whisper's batched pipeline.

    BatchedInferencePipeline decodes many 30 s windows in one GPU call, so
    thousands of VAD clips stop paying per-call launch overhead (measured
    26.9x -> ~118x realtime on a 5080; 164x with beam 1). Segments carry
    absolute video timestamps; each clip's segments are returned in input
    order, so the per-clip insert/manifest/resume contract is unchanged.

    clip_offsets: per-clip absolute offset added to the output timestamps.
    The sharded path feeds concatenated clip audio (times relative to the
    array) and maps segments back to video time here; None keeps the legacy
    full-audio behavior (times already absolute) byte-identical."""
    from faster_whisper import BatchedInferencePipeline

    global _word_ts_ok, _device_override
    clips = [{"start": s, "end": e} for s, e in chunks]
    kwargs: dict[str, Any] = dict(
        language=language,
        beam_size=_beam_size(),
        vad_filter=False,  # our own VAD pre-pass already gated the audio
        batch_size=_batch_size(),
        without_timestamps=False,  # keep timestamp-driven segment splitting
    )
    if _word_ts_ok:
        kwargs["word_timestamps"] = True

    def run(pipe: Any) -> tuple[list[Any], Any]:
        seg_iter, info = pipe.transcribe(audio, clip_timestamps=clips, **kwargs)
        return list(seg_iter), info

    if _in_multi_mode():
        # Multi-copy mode: the model is THIS thread's own copy — no global
        # lock needed. A CUDA OOM degrades only this thread to CPU.
        try:
            raw, info = run(BatchedInferencePipeline(model))
        except ValueError as exc:
            if not _word_ts_ok:
                raise
            # distil models lack alignment heads — fall back to plain segments.
            logger.info("Word timestamps unsupported (%s) — falling back", exc)
            _word_ts_ok = False
            kwargs.pop("word_timestamps", None)
            raw, info = run(BatchedInferencePipeline(model))
        except RuntimeError as exc:
            if _effective_device()[0] != "cuda" or _thread_cpu_fallback():
                raise
            # This thread's copy hit a CUDA inference failure (OOM, driver
            # hiccup): degrade ONLY this thread to CPU and retry once.
            logger.warning("CUDA inference failed (%s) — degrading this thread to CPU", exc)
            _thread_mark_cpu_fallback()
            raw, info = run(BatchedInferencePipeline(_thread_model()))
    else:
        with _infer_lock:
            try:
                raw, info = run(BatchedInferencePipeline(model))
            except ValueError as exc:
                if not _word_ts_ok:
                    raise
                # distil models lack alignment heads — fall back to plain segments.
                logger.info("Word timestamps unsupported (%s) — falling back", exc)
                _word_ts_ok = False
                kwargs.pop("word_timestamps", None)
                raw, info = run(BatchedInferencePipeline(model))
            except RuntimeError as exc:
                if _effective_device()[0] != "cuda" or _device_override is not None:
                    raise
                # GPU present but inference broken (missing cuBLAS, driver hiccup):
                # drop to CPU for the process lifetime and retry this batch once.
                logger.warning("CUDA inference failed (%s) — falling back to CPU", exc)
                _device_override = ("cpu", "int8")
                raw, info = run(BatchedInferencePipeline(_get_model()))

    detected_lang = getattr(info, "language", None) or None
    # Attribute each segment to its clip by start time (clips are disjoint
    # and sorted; the pipeline offsets segments by their clip start).
    per: list[list[Any]] = [[] for _ in chunks]
    idx = 0
    for seg in raw:
        s = float(seg.start)
        while idx < len(chunks) - 1 and s >= chunks[idx][1]:
            idx += 1  # advance past clips that ended before this segment
        per[idx].append(seg)
    out: list[tuple[list[dict], Optional[str]]] = []
    for i, segs in enumerate(per):
        base = 0.0 if clip_offsets is None else clip_offsets[i]
        items: list[dict] = []
        for seg in segs:
            words = [
                {
                    "word": w.word,
                    "start": round(float(w.start) + base, 3),
                    "end": round(float(w.end) + base, 3),
                }
                for w in (seg.words or [])
            ]
            items.append({
                "start_sec": round(float(seg.start) + base, 3),
                "end_sec": round(float(seg.end) + base, 3),
                "text": (seg.text or "").strip(),
                "words": words,
            })
        out.append((items, detected_lang))
    return out


def transcribe_video(
    platform: str,
    video_id: str,
    *,
    language: Optional[str] = None,
    progress_cb: Optional[Callable[[float, float, int, int], None]] = None,
    events_cb: Optional[Callable[..., Optional[dict]]] = None,
) -> dict:
    """Transcribe one archived video into the transcripts table (resume-aware).

    progress_cb(speech_done_sec, speech_total_sec, chunk_done, chunk_total) —
    non-speech time is deliberately excluded from the denominator.
    events_cb(audio, speech, shards=None) — optional post-transcribe stage
    hook (PANNs event detection) that reuses THIS run's decoded audio and
    VAD regions instead of decoding the whole file again; its stats merge
    into the returned dict under 'events'. The hook may raise — transcription
    result is never rolled back or failed by it. shards carries the
    _ShardedAudio when the bounded-RAM path ran (audio is None then).
    Returns a stats dict (also suitable for job reporting).

    Long media (>= SHARD_THRESHOLD_SEC of decoded PCM) is decoded ONCE into
    fixed-duration disk shards and consumed one window at a time, so peak
    RAM is bounded regardless of VOD length; small files keep the legacy
    full-array path byte-for-byte."""
    rows = archive_db.query(
        "SELECT * FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if not rows:
        raise KeyError(f"no archived video {platform}/{video_id}")
    path = rows[0]["archive_path"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"archive file missing for {platform}/{video_id}: {path}")

    if language is None:
        language = os.environ.get(LANG_ENV, "").strip() or None

    t0 = time.monotonic()
    if _should_shard(path):
        # Bounded-RAM path: decode ONCE into fixed-duration PCM shards on
        # disk and consume them one window at a time. The temp dir is
        # removed in finally — also when the job fails.
        shard_dir = Path(tempfile.mkdtemp(prefix="vodrip-shards-"))
        try:
            return _transcribe_audio_source(
                platform, video_id, path, language, progress_cb, events_cb, t0,
                sharded=True, shard_dir=shard_dir,
            )
        finally:
            shutil.rmtree(shard_dir, ignore_errors=True)
    return _transcribe_audio_source(
        platform, video_id, path, language, progress_cb, events_cb, t0,
        sharded=False, shard_dir=None,
    )


def _transcribe_audio_source(
    platform: str,
    video_id: str,
    path: str,
    language: Optional[str],
    progress_cb: Optional[Callable[[float, float, int, int], None]],
    events_cb: Optional[Callable[..., Optional[dict]]],
    t0: float,
    *,
    sharded: bool,
    shard_dir: Optional[Path],
) -> dict:
    """Shared transcription core over one audio source (full array or shards).

    sharded=True reads the media once into _ShardedAudio (caller owns the
    temp dir lifecycle); sharded=False decodes the whole file as before. The
    resume/manifest contract, per-clip inserts and stats are identical."""
    if sharded:
        shard_sec = _shard_seconds()
        for _start_sec, _arr in _decode_to_shards(path, shard_sec=shard_sec, out_dir=shard_dir):
            pass  # decode pass — PCM is spilled to disk, never held whole
        files = sorted(shard_dir.glob("shard_*.f32"))
        if not files:
            raise RuntimeError(f"ffmpeg produced no audio from {path}")
        sharded_audio = _ShardedAudio(files, shard_sec)
        if sharded_audio.total_samples == 0:
            raise RuntimeError(f"ffmpeg produced no audio from {path}")
        audio = None
        total_sec = sharded_audio.total_sec
        speech = _vad_speech_seconds_sharded(sharded_audio)
    else:
        audio = decode_audio(path)
        total_sec = audio.size / SAMPLE_RATE
        speech = vad_speech_seconds(audio)
        sharded_audio = None
    chunks = _plan_chunks(speech)
    speech_sec = sum(e - s for s, e in chunks)
    dead_air_sec = max(0.0, total_sec - speech_sec)
    dead_air_pct = (dead_air_sec / total_sec * 100.0) if total_sec > 0 else 0.0

    logger.info(
        "VAD: %.1fs total, %.1fs speech (%.0f%%), %.1fs dead air skipped",
        total_sec, speech_sec, speech_sec / total_sec * 100 if total_sec else 0,
        dead_air_sec,
    )

    # No-speech skip: less than 3 s of planned speech is noise — report it
    # WITHOUT loading the model and WITHOUT a resume manifest (nothing to
    # resume). _process_job maps this to status 'done' (captions-first
    # precedent).
    if speech_sec < 3.0:
        wall = time.monotonic() - t0
        stats = {
            "platform": platform,
            "video_id": video_id,
            "model": model_name(),
            "device": _effective_device()[0],
            "compute_type": _effective_device()[1],
            "total_sec": round(total_sec, 3),
            "speech_sec": round(speech_sec, 3),
            "dead_air_sec": round(dead_air_sec, 3),
            "dead_air_pct": round(dead_air_pct, 1),
            "segments": 0,
            "words": 0,
            "resumed_chunks": 0,
            "wall_sec": round(wall, 3),
            "speed_x": 0.0,
            "skipped": "no-speech",
        }
        logger.info(
            "transcribe %s/%s skipped: planned speech %.1fs < 3s — no model load",
            platform, video_id, speech_sec,
        )
        return stats

    # Model load happens only now: VAD + planning are model-free, so a
    # no-speech video never pays the (large) load cost.
    model = _current_model()
    existing = {int(r["seg_idx"]) for r in archive_db.transcript_for(platform, video_id)}
    header, entries = _read_manifest(_manifest_path(platform, video_id))
    missing, seg_idx = _resume_plan(chunks, header, entries, existing)
    if len(missing) == len(chunks) and existing:
        # Full re-run of an already-transcribed video: the manifest is gone
        # (previous run finished and cleaned it), so the old rows are stale
        # output of an earlier transcription — replace, never append beside.
        logger.info("Re-transcribe %s/%s: replacing %d existing rows",
                    platform, video_id, len(existing))
        archive_db.delete_transcripts(platform, video_id)
        existing = set()
        seg_idx = 0
    if missing != list(range(len(chunks))):
        logger.info("Resume: %d/%d chunks already transcribed — skipping",
                    len(chunks) - len(missing), len(chunks))

    manifest = _manifest_path(platform, video_id)
    _write_manifest_header(manifest, chunks)

    segments = 0
    words = 0
    speech_done = 0.0
    detected_lang: Optional[str] = None
    missing_set = set(missing)
    ci = 0
    n_chunks = len(chunks)
    while ci < n_chunks:
        cs, ce = chunks[ci]
        if ci not in missing_set:
            speech_done += ce - cs
            ci += 1
            continue
        # Batch consecutive missing clips into one GPU call; resume gaps only
        # shrink the run, never break the per-clip insert/manifest contract.
        run: list[tuple[int, tuple[float, float]]] = []
        while ci < n_chunks and ci in missing_set and len(run) < _batch_size():
            run.append((ci, chunks[ci]))
            ci += 1
        if sharded_audio is not None:
            batch_audio, concat_clips, clip_offsets = _clips_to_audio(sharded_audio, run)
            batch_out = _transcribe_batch(
                model, batch_audio, concat_clips, language, clip_offsets=clip_offsets,
            )
        else:
            batch_out = _transcribe_batch(model, audio, [c for _, c in run], language)
        for (ci2, _), (chunk_segs, detected) in zip(run, batch_out):
            if detected_lang is None and detected:
                detected_lang = detected  # first batch's detection wins
            lang = language or detected_lang  # env wins; else detected; else None
            # Batch insert: one insert_transcript() call per chunk (it accepts
            # a list); a crash loses at most the in-flight chunk.
            first_idx = seg_idx
            batch_rows = []
            for seg in chunk_segs:
                if seg_idx in existing:
                    seg_idx += 1
                    continue
                batch_rows.append({
                    "seg_idx": seg_idx,
                    "start_sec": seg["start_sec"],
                    "end_sec": seg["end_sec"],
                    "text": seg["text"],
                    "words": seg["words"],
                })
                words += len(seg["words"])
                seg_idx += 1
            if batch_rows:
                archive_db.insert_transcript(platform, video_id, batch_rows, lang=lang)
            segments += len(batch_rows)
            _append_manifest_entry(manifest, ci2, first_idx, len(chunk_segs))
            speech_done += chunks[ci2][1] - chunks[ci2][0]
            if progress_cb:
                progress_cb(speech_done, speech_sec, ci2 + 1, n_chunks)

    # Disk hygiene: the job finished — the crash-resume manifest has served
    # its purpose. Best-effort: a failed unlink just leaves it for the next
    # run (which would resume into an empty plan and rewrite it anyway).
    try:
        manifest.unlink(missing_ok=True)
    except OSError:
        pass

    wall = time.monotonic() - t0
    stats = {
        "platform": platform,
        "video_id": video_id,
        "model": model_name(),
        "device": _effective_device()[0],
        "compute_type": _effective_device()[1],
        "total_sec": round(total_sec, 3),
        "speech_sec": round(speech_sec, 3),
        "dead_air_sec": round(dead_air_sec, 3),
        "dead_air_pct": round(dead_air_pct, 1),
        "segments": segments,
        "words": words,
        "resumed_chunks": len(chunks) - len(missing),
        "wall_sec": round(wall, 3),
        "speed_x": round(speech_sec / wall, 2) if wall > 0 else 0.0,
    }
    logger.info(
        "transcribe %s/%s done: %d segs, %d words, %.1f%% dead air skipped, "
        "%.2fx realtime on %s/%s",
        platform, video_id, segments, words, dead_air_pct,
        stats["speed_x"], stats["device"], stats["compute_type"],
    )
    if events_cb is not None:
        # Optional enrichment stage (VODRIP_EVENTS_ENABLED=1): reuses this
        # run's audio + VAD regions — no second decode. Best-effort: a
        # failing stage never fails the transcribe job.
        try:
            if sharded_audio is not None:
                ev = events_cb(None, speech, shards=sharded_audio) or {}
            else:
                ev = events_cb(audio, speech) or {}
            if ev.get("events") is not None:
                stats["events"] = ev["events"]
        except Exception:
            logger.exception("events stage failed for %s/%s", platform, video_id)
    return stats


# --- queue worker ---------------------------------------------------------

_STALE_JOB_TIMEDELTA = timedelta(minutes=30)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim_next_job() -> Optional[dict]:
    """Atomically claim the newest queued transcribe/events job (crash-stale too).

    A 'running' job is reclaimed only if untouched for 30 min — a single
    chunk can legitimately take that long on CPU."""
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - _STALE_JOB_TIMEDELTA).isoformat(timespec="seconds")
    # String comparison is valid: both sides come from _now_iso (UTC, same width).
    rows = archive_db.query(
        """SELECT * FROM archive_jobs
           WHERE kind IN ('transcribe','events')
             AND (status = 'queued' OR (status = 'running' AND updated_at < ?))
           ORDER BY priority DESC, created_at ASC
           LIMIT 8""",
        (stale_cutoff,),
    )
    for row in rows:
        if row["status"] == "queued":
            cur = archive_db.execute(
                "UPDATE archive_jobs SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (_now_iso(), row["id"]),
            )
        else:
            cur = archive_db.execute(
                "UPDATE archive_jobs SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (_now_iso(), row["id"]),
            )
        if cur.rowcount == 1:
            return dict(row)
    return None


def _process_events_job(job_id: str, platform: str, video_id: str) -> dict:
    """Run one kind='events' job (PANNs acoustic-event detection).

    Same shape as the transcribe path: progress tracks speech seconds, the
    job ends 'done' with a stats dict (or 'failed' with an error message)."""
    def _progress(done: float, total: float) -> None:
        if total > 0:
            archive_db.update_job(job_id, progress=min(0.999, done / total))

    stats = detect_events_video(platform, video_id, progress_cb=_progress)
    archive_db.update_job(job_id, status="done", progress=1.0)
    return stats


def _captions_first_skip(platform: str, video_id: str) -> bool:
    """True when YouTube captions already cover the video and the user opted
    into captions-first (settings.yt_subtitles_first, default True).

    Whisper still runs when the toggle is off, the platform isn't YouTube,
    or no caption rows exist. ponytail: there is no force-transcribe path
    (archive_jobs has no force flag) — add one there if a job ever needs to
    bypass this.
    """
    if platform != "youtube":
        return False
    from deps import settings_mgr  # lazy: archive_transcribe is opt-in by design

    if not getattr(settings_mgr.get(), "yt_subtitles_first", True):
        return False
    return bool(archive_db.transcript_for(platform, video_id))


def _resolve_job_language(platform: str, video_id: str) -> Optional[str]:
    """ASR language for one transcribe job: override > channel > default.

    Precedence (WS-3): per-channel override in settings
    (channel_asr_languages) > persisted channel language
    (videos.channel_language, fed by platform clues + transcript
    aggregation) > default ASR language setting (asr_language) > auto
    detect (None). 'auto'/'' at any level falls through to the next."""
    from services.channel_language import normalize_language

    channel = archive_db.video_channel(platform, video_id)
    try:
        from deps import settings_mgr  # lazy: archive_transcribe is opt-in by design

        settings = settings_mgr.get()
        overrides = getattr(settings, "channel_asr_languages", None) or {}
    except Exception:
        settings = None
        overrides = {}
    if channel:
        lang = overrides.get(channel) or overrides.get(channel.lower())
        hint = normalize_language(lang)
        if hint:
            return hint
    stored = archive_db.video_channel_language(platform, video_id)
    if stored:
        return normalize_language(stored)
    if settings is not None:
        hint = normalize_language(getattr(settings, "asr_language", "auto"))
        if hint:
            return hint
    return None


def _process_job(job: dict, *, multi: bool = False) -> dict:
    """Run one claimed job; never raises — failures land in archive_jobs.error.

    multi=True (worker with budget > 1): the job runs on the calling pool
    thread's own model copy — no global _infer_lock, thread-local CUDA
    fallback. Default False keeps the single-global-model path for direct
    callers and tests."""
    job_id = job["id"]
    platform, video_id = job["platform"], job["video_id"]
    if multi:
        _multi_tls.active = True
    try:
        archive_db.update_job(job_id, status="running", progress=0.0)

        if job.get("kind") == "events":
            return _process_events_job(job_id, platform, video_id)

        if _captions_first_skip(platform, video_id):
            logger.info("captions already present for %s/%s — skipping whisper", platform, video_id)
            archive_db.update_job(job_id, status="done", progress=1.0)
            return {
                "job_id": job_id,
                "platform": platform,
                "video_id": video_id,
                "skipped": "captions-first",
            }

        if archive_db.transcribed_on_higher_priority_platform(platform, video_id):
            # The same live/VOD exists on a higher-priority platform
            # (youtube > twitch > kick) with transcript rows already — the
            # Kick (or Twitch) copy needs no whisper. Mirrors the download
            # dedupe rule (archive_kick.dedupe_decision).
            logger.info(
                "same VOD already transcribed on a higher-priority platform — "
                "skipping %s/%s",
                platform, video_id,
            )
            archive_db.update_job(job_id, status="done", progress=1.0)
            return {
                "job_id": job_id,
                "platform": platform,
                "video_id": video_id,
                "skipped": "dedupe-transcribed",
            }

        _last_progress = [0.0]

        def _progress(done: float, total: float, _ci: int, _n: int) -> None:
            if total <= 0:
                return
            # Throttle: progress rows churn the SQLite lock — at most one
            # UPDATE every 2 s is plenty for a 0-99.9% progress bar.
            now = time.monotonic()
            if now - _last_progress[0] < 2.0 and done < total:
                return
            _last_progress[0] = now
            archive_db.update_job(job_id, progress=min(0.999, done / total))

        events_cb = None
        if events_enabled():
            def events_cb(audio: Any, speech: list, shards: Any = None) -> Optional[dict]:
                return detect_events_video(
                    platform, video_id, audio=audio, speech=speech, shards=shards,
                )

        stats = transcribe_video(
            platform, video_id,
            language=_resolve_job_language(platform, video_id),
            progress_cb=_progress, events_cb=events_cb,
        )
        archive_db.update_job(job_id, status="done", progress=1.0)
        # New transcript evidence -> re-aggregate the channel language
        # (throttled; best-effort — a failure must never fail the job).
        try:
            from services.channel_language import on_transcribe_done
            on_transcribe_done(platform, video_id)
        except Exception:
            logger.debug("channel language re-aggregation failed", exc_info=True)
        if "skipped" not in stats:
            # Heavy batch writes just finished — merge the FTS b-tree
            # segments so search stays fast (best-effort inside). Skipped
            # for tiny jobs: a few segments don't fragment anything.
            if stats.get("segments", 0) >= 50:
                archive_db.optimize_fts()
        return stats
    except Exception as exc:  # job-level failure — worker keeps going
        logger.exception("transcribe job %s failed", job_id)
        archive_db.update_job(job_id, status="failed",
                              error=f"{type(exc).__name__}: {exc}"[:400])
        return {"job_id": job_id, "error": str(exc)}
    finally:
        if multi:
            _multi_tls.active = False


def run_worker(
    *,
    once: bool = False,
    poll_interval: float = 2.0,
    max_workers: Optional[int] = None,
) -> None:
    """Blocking worker loop over the transcribe queue.

    Parallelism budget: _worker_budget() — 1 on CUDA by default (the legacy
    single-global-model path with _infer_lock, byte-for-byte today's
    behavior); >1 (CPU workers or opt-in VODRIP_TRANSCRIBE_GPU_COPIES) gives
    each pool thread its own model copy so inference truly runs in parallel.
    max_workers overrides the budget (used by tests/launchers).
    """
    device, compute_type = _effective_device()
    budget = _worker_budget() if max_workers is None else max(1, int(max_workers))
    multi = budget > 1
    logger.info("archive transcribe worker: device=%s compute_type=%s workers=%d",
                device, compute_type, budget)
    try:
        with ThreadPoolExecutor(max_workers=budget, thread_name_prefix="transcribe") as pool:
            # Per-slot claim loop: each finished future frees a slot and a
            # refill claims the next job immediately. The old await-all
            # barrier idled every worker behind the slowest job of the batch
            # (a 10-min clip sat ~1h behind a 13.5h VOD).
            pending: dict = {}  # Future -> claimed job

            def _refill() -> None:
                while len(pending) < budget:
                    job = _claim_next_job()
                    if job is None:
                        return
                    pending[pool.submit(_process_job, job, multi=multi)] = job

            _refill()
            while True:
                _maybe_close_idle_model()
                archive_db.worker_heartbeat("transcribe")
                if not pending:
                    if once:
                        break
                    time.sleep(poll_interval)
                    _refill()
                    continue
                done, _ = wait(pending, timeout=poll_interval)
                for fut in done:
                    pending.pop(fut, None)
                    try:
                        fut.result()
                    except Exception:
                        logger.exception("worker future crashed")  # belt & braces
                    _refill()
                if once and not pending:
                    break
    finally:
        close_model()


def start_worker(**kwargs: Any) -> threading.Thread:
    """Spawn the worker loop in a daemon thread (never blocks the caller)."""
    thread = threading.Thread(
        target=run_worker, kwargs=kwargs, name="archive-transcribe", daemon=True
    )
    thread.start()
    return thread


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_worker()

# --- module self-check (pure logic — no model load, no GPU, no downloads) --

_speech = [(0.0, 5.0), (5.8, 6.2), (20.0, 30.0)]
assert _plan_chunks(_speech, merge_gap=1.0, min_len=0.25) == [(0.0, 6.2), (20.0, 30.0)], (
    "speech regions within the merge gap must fuse"
)
assert _plan_chunks([(0.0, 0.2)]) == [], "sub-minimum chunks must be dropped"
assert _plan_chunks([]) == [], "empty VAD output must plan no chunks"
assert _plan_chunks([(0.0, 5.0), (10.0, 15.0)]) == [(0.0, 5.0), (10.0, 15.0)], (
    "wide gaps must stay separate chunks"
)
assert _plan_chunks([(0.0, 95.0)]) == [
    (0.0, 30.0), (30.0, 60.0), (60.0, 90.0), (90.0, 95.0),
], "chunks must be capped at the 30 s whisper window (uncapped clips truncate)"
assert _plan_chunks([(0.0, 25.0), (25.4, 70.0)]) == [
    (0.0, 30.0), (30.0, 60.0), (60.0, 70.0),
], "cap must apply across merged regions"
# sharded decode: cross-shard merge + shard sample contiguity (VAD regions
# are per-shard with overlap; the merge gap stays below _plan_chunks' so the
# final chunk plan matches the full-array path).
assert _merge_speech_regions([(0.0, 5.0), (5.2, 6.0)]) == [(0.0, 6.0)], (
    "regions within the 0.5 s merge gap must fuse"
)
assert _merge_speech_regions([(0.0, 5.0), (5.6, 6.0)]) == [(0.0, 5.0), (5.6, 6.0)], (
    "wider gaps must stay separate"
)
assert _merge_speech_regions([(4.9, 5.1), (4.9, 5.1)]) == [(4.9, 5.1)], (
    "duplicate cross-boundary regions must collapse"
)
_b1 = _shard_sample_bounds(1, 5.0)
assert _b1 == (80000, 160000) and _b1[0] == _shard_sample_bounds(0, 5.0)[1], (
    "shards must tile the timeline contiguously"
)
assert _detect_device() in (("cuda", "float16"), ("cpu", "int8")), (
    "device settings must be a known pair (nvidia -> cuda/float16, else cpu/int8)"
)
assert _sanitize_key("abc/def:123") == "abc_def_123"
_header = {"chunks": [(0.0, 5.0), (10.0, 15.0), (20.0, 25.0)], "model": model_name()}
_entries = {0: {"ci": 0, "first": 0, "count": 2}, 1: {"ci": 1, "first": 2, "count": 3}}
_missing, _next = _resume_plan(_header["chunks"], _header, _entries, {0, 1, 2, 3, 4})
assert _missing == [2] and _next == 5, "manifest-matched chunks must be skipped"
_missing, _next = _resume_plan(_header["chunks"], _header, _entries, {0, 3, 4})
assert _missing == [0, 1, 2] and _next == 1, "deleted rows must re-mark their chunk and reuse the lowest gap"
_missing, _next = _resume_plan(_header["chunks"], None, _entries, {0, 1})
assert _missing == [0, 1, 2] and _next == 2, "missing manifest -> full re-run"
_stale = dict(_header, model="different-model")
_missing, _ = _resume_plan(_header["chunks"], _stale, _entries, {0, 1})
assert _missing == [0, 1, 2], "model change must invalidate stale manifest entries"
_missing, _next = _resume_plan([], None, {}, set())
assert _missing == [] and _next == 0, "no chunks -> nothing to do"
# worker budget: no GPU_COPIES env -> 1 copy regardless of VRAM; the VRAM
# clamp caps copies at free_vram // 2 GiB (never below 1); CPU honors
# VODRIP_TRANSCRIBE_WORKERS and defaults to 2.
assert _clamp_cuda_copies(1, 100 << 30) == 1, "no GPU copies env -> 1 (probe skipped)"
assert _clamp_cuda_copies(4, 10 << 30) == 4, "env within the VRAM budget passes through"
assert _clamp_cuda_copies(8, 5 << 30) == 2, "VRAM budget clamps copies (5 GiB -> 2)"
assert _clamp_cuda_copies(8, 1 << 30) == 1, "VRAM budget never drops below 1"
_saved_override, _saved_workers = _device_override, os.environ.get(WORKERS_ENV)
_saved_free_ram = _free_system_ram_bytes
try:
    _device_override = ("cpu", "int8")
    _free_system_ram_bytes = lambda: 64 * 1024 ** 3  # RAM clamp must not bind here
    os.environ[WORKERS_ENV] = "4"
    assert _worker_budget() == 4, "CPU budget must honor VODRIP_TRANSCRIBE_WORKERS"
    os.environ.pop(WORKERS_ENV, None)
    assert _worker_budget() == 2, "CPU budget defaults to 2 workers"
    # system-RAM clamp: 3 GiB free + 1.5 GiB/worker -> usable 2.4 GiB -> 1
    # (2 workers would use 100% of free RAM; the 20% headroom forbids it);
    # 4 GiB free -> usable 3.2 GiB -> 2; 1 GiB free -> floor 1.
    _free_system_ram_bytes = lambda: 3 * 1024 ** 3
    assert _ram_worker_clamp(8, _CPU_WORKER_RSS_EST) == 1, "headroom clamps 3 GiB free to 1 worker"
    _free_system_ram_bytes = lambda: 4 * 1024 ** 3
    assert _ram_worker_clamp(8, _CPU_WORKER_RSS_EST) == 2, "4 GiB free fits 2 workers"
    _free_system_ram_bytes = lambda: 1 * 1024 ** 3
    assert _ram_worker_clamp(8, _CPU_WORKER_RSS_EST) == 1, "RAM clamp never drops below 1"
finally:
    _device_override = _saved_override
    _free_system_ram_bytes = _saved_free_ram
    if _saved_workers is None:
        os.environ.pop(WORKERS_ENV, None)
    else:
        os.environ[WORKERS_ENV] = _saved_workers
