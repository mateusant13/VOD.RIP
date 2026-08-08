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
  * Hybrid pool (CUDA hosts): the worker runs the GPU copy AND CPU threads
    at the same time — VODRIP_TRANSCRIBE_GPU_COPIES GPU slots (default 1)
    plus VODRIP_TRANSCRIBE_WORKERS CPU slots (default 2 on <16-thread boxes,
    3 on 16–31, 4 on 32+; 0 disables the CPU side and restores the
    exclusive-GPU worker). Each pool thread is pinned to its slot's device
    at thread start, so CPU threads never compete for VRAM. CPU-only hosts
    are unchanged (WORKERS, same dynamic default).
  * Parakeet lane: when sherpa-onnx is importable (and VODRIP_PARAAKEET is
    not 0), slots transcribe jobs whose language is in Parakeet TDT v3's 25
    European languages with sherpa-onnx (2.5-5.2 RTFx on CPU int8 vs
    whisper-large-v3-turbo cpu/int8 at 0.26-0.6, ~0.7 GB less RSS, no
    silence hallucination — A/B 2026-08-07). Known-other (ja, ...) and
    unknown languages stay whisper. CUDA-enabled sherpa-onnx wheels
    (>=1.13.x, see requirements.txt) let GPU slots run parakeet with
    provider='cuda', gated on the measured free-VRAM allowance; without a
    CUDA wheel GPU slots are whisper exactly as before. Model auto-downloads
    on first use into the sherpa cache (VODRIP_SHERRPA_CACHE or a
    whisper-cache sibling).
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
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import count
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from services import archive_db
from services.archive_events import detect_events_video, events_enabled
from services.autostart import background_mode
from services.disk_hygiene import active_whisper_model_id, whisper_cache_dir
from services.gpu_detect import detect_gpu_vendor
from services.os_services import _NO_WINDOW
from services.yt_gate import gate_remaining_sec, youtube_gate_active
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe, _resolve_ffprobe_exe

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
SAMPLE_RATE = 16000

# Env knobs (all optional).
LANG_ENV = "VODRIP_WHISPER_LANGUAGE"
WORKERS_ENV = "VODRIP_TRANSCRIBE_WORKERS"  # CPU threads; 0 = GPU-only on CUDA hosts
IDLE_ENV = "VODRIP_WHISPER_IDLE_CLOSE"
GPU_COPIES_ENV = "VODRIP_TRANSCRIBE_GPU_COPIES"
BEAM_ENV = "VODRIP_WHISPER_BEAM"
BATCH_ENV = "VODRIP_WHISPER_BATCH"
PARAKEET_ENV = "VODRIP_PARAAKEET"          # "0" kills the parakeet CPU lane (whisper int8)
PARAKEET_CACHE_ENV = "VODRIP_SHERRPA_CACHE"  # sherpa-onnx model cache override

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

# GPU lane budget (user hardware: RTX 5080 16 GiB; large-v3-turbo fp16 is
# ~5-6 GiB, NOT the old 1-2.5 GiB estimate). The card is a SHARED tenant:
# the desktop + the user's other ML project hold VRAM, so the measured
# allowance at claim time decides the lane — never a static count.
_GPU_VRAM_HEADROOM = int(2 * 1024 ** 3)   # must stay free for the tenants
_GPU_UTIL_SECOND_COPY = 0.70              # below this, a 2nd copy may add
_GPU_VRAM_MEDIAN_SAMPLES = 6              # reads spread over ~60 s
_GPU_VRAM_MEDIAN_GAP_S = 10.0


def _gpu_model_vram_est() -> int:
    """fp16 VRAM estimate (bytes) for the ACTIVE whisper model (name-prefixed).

    large-v3-turbo is the default (~6 GiB); large/distil-large ~10; medium/
    distil-medium ~5; small ~2; base ~1; tiny ~0.6. Unknown names fall back
    to 6 GiB (the default model) — conservative for the common case."""
    name = (model_name() or "").lower()
    gb = 6.0  # default: large-v3-turbo
    if name.startswith("large-v3-turbo") or name.startswith("distil-large-v3"):
        gb = 6.0
    elif name.startswith("large"):
        gb = 10.0
    elif name.startswith("medium") or name.startswith("distil-medium"):
        gb = 5.0
    elif name.startswith("small") or name.startswith("distil-small"):
        gb = 2.0
    elif name.startswith("base"):
        gb = 1.0
    elif name.startswith("tiny"):
        gb = 0.6
    return int(gb * 1024 ** 3)


def _clamp_cuda_copies(copies: int, free_vram_bytes: int) -> int:
    """min(copies, max(1, free_vram // (model_est + headroom))) — copy budget.

    Pure shape so the module self-check can pin it without a GPU: env 1 -> 1,
    env >1 -> VRAM-capped (never below 1; the caller's VRAM-floor gate owns
    the 0-copies decision)."""
    if copies <= 1:
        return 1
    per_copy = _gpu_model_vram_est() + _GPU_VRAM_HEADROOM
    vram_cap = max(1, free_vram_bytes // per_copy)
    return max(1, min(copies, vram_cap))


_vram_free_bytes = 0
_vram_free_at = 0.0
_vram_lock = threading.Lock()


def _gpu_free_vram_bytes() -> int:
    """Free GPU VRAM in bytes — MEDIAN of reads spread over ~60 s (0 = unknown).

    A single instant is a lie on a shared card: the user's other ML project
    spikes and dips, and a spike would flap the lane decision. The first
    call spreads _GPU_VRAM_MEDIAN_SAMPLES reads ~10 s apart and returns the
    median; later calls (within the TTL) reuse it. Tests patch it directly
    (mirrors _free_system_ram_bytes); probe failure (no torch / no CUDA /
    nvidia-smi absent) -> 0 -> env cap trusted."""
    global _vram_free_bytes, _vram_free_at
    now = time.monotonic()
    with _vram_lock:
        if _vram_free_at and now - _vram_free_at < _GPU_VRAM_MEDIAN_GAP_S * _GPU_VRAM_MEDIAN_SAMPLES:
            return _vram_free_bytes
    samples: list[int] = []
    for i in range(_GPU_VRAM_MEDIAN_SAMPLES):
        try:
            import torch

            if not torch.cuda.is_available():
                break  # no CUDA — nothing to sample
            samples.append(int(torch.cuda.mem_get_info()[0]))
        except Exception:
            break  # no torch — nothing to sample
        if len(samples) >= _GPU_VRAM_MEDIAN_SAMPLES:
            break
        if i < _GPU_VRAM_MEDIAN_SAMPLES - 1:
            time.sleep(_GPU_VRAM_MEDIAN_GAP_S)
    if samples:
        ordered = sorted(samples)
        free = ordered[len(ordered) // 2]  # median (upper-middle for even counts)
    else:
        free = 0
    with _vram_lock:
        _vram_free_bytes = free
        _vram_free_at = now
    return free


def _gpu_vram_allowance() -> int:
    """Free VRAM the worker may use (median measurement minus a manual reserve).

    ponytail: a future user setting 'reserve N GB of VRAM for other apps'
    becomes ONE line here (`max(0, free - N GiB)`) — the lane gate consumes
    the result, so the reserve never has to touch the decision logic."""
    return _gpu_free_vram_bytes()


_gpu_held_cache = False
_gpu_held_at = 0.0
_gpu_held_lock = threading.Lock()


def _gpu_held_by_other() -> bool:
    """True when another process holds a CUDA model on this GPU.

    The live backend / another ML project / a worktree test may already hold
    a GPU model — stacking another on top risks evicting it. False when the
    probe fails (tasklist absent): the free-VRAM gate is still the primary
    guard. Cached 10 s; patched directly by tests.

    Probe: `tasklist /m nvcuda.dll` — processes that LOADED the CUDA
    runtime. nvidia-smi's compute-apps is NOT usable on Windows: it lists
    every WDDM process that touches the GPU (dwm, explorer, browsers,
    Discord...), tripping the gate even when no compute app runs. Loading
    nvcuda.dll is the precise signal — real CUDA apps only."""
    global _gpu_held_cache, _gpu_held_at
    now = time.monotonic()
    with _gpu_held_lock:
        if _gpu_held_at and now - _gpu_held_at < 10.0:
            return _gpu_held_cache
    held = False
    try:
        out = sp.run(
            ["tasklist", "/m", "nvcuda.dll"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
        if out.returncode == 0:
            mine = str(os.getpid())
            for line in out.stdout.splitlines():
                # "python.exe  12345 nvcuda.dll" (image name may contain spaces)
                parts = line.split()
                if (
                    len(parts) >= 3
                    and parts[-1] == "nvcuda.dll"
                    and parts[-2].isdigit()
                    and parts[-2] != mine
                ):
                    held = True
                    break
    except Exception:
        held = False
    with _gpu_held_lock:
        _gpu_held_cache = held
        _gpu_held_at = now
    return held


_gpu_util_cache = 0.0
_gpu_util_at = 0.0
_gpu_util_lock = threading.Lock()


def _gpu_util() -> Optional[float]:
    """Current GPU utilization 0..1 (None when unmeasurable), cached 5 s.

    Second-copy decision input (a busy GPU adds nothing from another copy);
    also reported in acceptance runs. Patched directly by tests."""
    global _gpu_util_cache, _gpu_util_at
    now = time.monotonic()
    with _gpu_util_lock:
        if _gpu_util_at and now - _gpu_util_at < 5.0:
            return _gpu_util_cache
    util: Optional[float] = None
    try:
        out = sp.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            util = float(out.stdout.strip().splitlines()[0]) / 100.0
    except Exception:
        util = None
    with _gpu_util_lock:
        _gpu_util_cache = util if util is not None else 0.0
        _gpu_util_at = now
    return util


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


# GPU capability ladder (user requirement: cards range 6 -> 32 GiB; EVERY
# tier keeps a GPU path). Rungs keyed on the MEASURED 60 s-median free-VRAM
# allowance, read AFTER the compute-apps check. int8 is the default
# precision (project rule: 8 -> 16 bit only, no weird precisions); fp16 only
# when VRAM clearly allows. (model, compute_type); None model = the user's
# active model.
_GPU_LADDER_RUNGS = (
    (int(6.5 * 1024 ** 3), None, "float16"),   # active model fp16 — 8 GiB+ cards
    (int(3.5 * 1024 ** 3), None, "int8"),      # active model int8 — 6-8 GiB sweet spot
    (int(2.0 * 1024 ** 3), "medium", "int8"),  # entry cards (6 GiB class)
)


def _gpu_lane_plan() -> Optional[tuple[Optional[str], str]]:
    """(model, compute_type) for the GPU lane, or None -> CPU lane only.

    Rungs keyed on the measured 60 s-median free-VRAM allowance. Unknown
    allowance (0 = probe failed) -> (None, 'int8') — int8 default, the
    legacy trust-the-env path keeps working."""
    allowance = _gpu_vram_allowance()
    if allowance <= 0:
        return None, "int8"
    for threshold, model, compute_type in _GPU_LADDER_RUNGS:
        if allowance >= threshold:
            return model, compute_type
    return None  # < 2 GiB — CPU lane only


def _gpu_copies() -> int:
    """GPU model copies: VODRIP_TRANSCRIBE_GPU_COPIES (default 1) is a CEILING.

    Measured at claim time (the worker's claim gate) — NEVER static. The
    60 s-median free-VRAM allowance picks the ladder rung (fp16 -> int8 ->
    medium int8 -> CPU); below the 2 GiB floor -> 0 copies, the CPU side of
    the hybrid plan covers the queue. A GPU model held by another process
    (live backend / the user's other ML project) also forces 0 — never
    stack. A second copy only when the GPU is idle-ish (<70% util) AND the
    allowance fits ~2x. Probe failure (no torch / no CUDA / nvidia-smi
    absent) degrades to trusting the env cap. 0/absent -> auto (1 copy)."""
    try:
        configured = int(os.environ.get(GPU_COPIES_ENV, "1") or "1")
    except ValueError:
        return 1
    if configured <= 0:
        configured = 1  # 0 == auto (same as absent)
    # Held check FIRST: when another process holds a GPU model the lane is
    # forced off, so measuring the 60 s-median free VRAM would be pure waste.
    if _gpu_held_by_other():
        return 0  # another process holds a GPU model — don't stack
    if _gpu_lane_plan() is None:
        return 0  # measured median free VRAM < 2 GiB — CPU lane only
    allowance = _gpu_vram_allowance()
    if configured > 1:
        util = _gpu_util()
        if util is not None and util >= _GPU_UTIL_SECOND_COPY:
            configured = 1  # GPU already busy — one copy is the ceiling
    if allowance > 0:
        configured = _clamp_cuda_copies(configured, allowance)
    return _ram_worker_clamp(configured, _GPU_COPY_RSS_EST)


def _gpu_compute_type() -> str:
    """The ladder rung's precision for the GPU plan slots ('float16'|'int8')."""
    lane = _gpu_lane_plan()
    return lane[1] if lane else "int8"


# System CPU load clamp: when the box is already contended (user's app
# transcoding, other agents), CPU whisper threads would only slow it down —
# the jobs wait in SQLite and drain later. Measured via GetSystemTimes
# (kernel32, stdlib ctypes — psutil is not a declared dependency); POSIX
# uses os.getloadavg(). Probe failure/unknown -> False (no clamp).
_CPU_LOAD_HIGH = 0.85          # busy fraction of ALL cores at/above this
_CPU_LOAD_TTL_S = 15.0         # readout cache TTL (sampling sleeps ~0.2 s)
_cpu_load_high_cache = False
_cpu_load_at = 0.0
_cpu_load_lock = threading.Lock()


def _measure_cpu_load() -> float:
    """Busy fraction of all cores over a ~0.2 s window (0 = unknown)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        def _tot(ft: FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return 0.0
        i1, k1, u1 = _tot(idle), _tot(kernel), _tot(user)
        time.sleep(0.2)
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return 0.0
        i2, k2, u2 = _tot(idle), _tot(kernel), _tot(user)
        busy = (k2 - k1) + (u2 - u1)
        total = busy + (i2 - i1)
        return busy / total if total > 0 else 0.0
    try:
        avg1 = os.getloadavg()[0]
        n = os.cpu_count() or 1
        return max(0.0, min(1.0, avg1 / n))
    except (OSError, AttributeError):
        return 0.0


def _cpu_load_high() -> bool:
    """True when the box is already contended (cached; False if unmeasurable)."""
    global _cpu_load_high_cache, _cpu_load_at
    now = time.monotonic()
    with _cpu_load_lock:
        if _cpu_load_at and now - _cpu_load_at < _CPU_LOAD_TTL_S:
            return _cpu_load_high_cache
    load = _measure_cpu_load()
    with _cpu_load_lock:
        _cpu_load_high_cache = load >= _CPU_LOAD_HIGH
        _cpu_load_at = now
    return _cpu_load_high_cache


def _cpu_auto_workers() -> int:
    """Dynamic default CPU-lane count, sized to the box's threads.

    Whisper int8 needs ~1 thread per lane minimum before it becomes
    latency-bound, so the default scales with os.cpu_count(): 2 lanes below
    16 threads, 3 at 16–31, 4 at 32+. The RAM clamp (and the contention
    clamp when the box is busy) still caps the actual slots — this only
    raises the ceiling that used to be a flat 2. Env override
    (VODRIP_TRANSCRIBE_WORKERS) wins over this in _cpu_worker_ceiling.

    Background (autostart) mode caps at 2 — nobody is at the keyboard, so
    the box's threads go to the user's other work, not to extra model
    copies; transcription just runs longer."""
    if background_mode():
        return 2
    threads = os.cpu_count() or 4
    if threads >= 32:
        return 4
    if threads >= 16:
        return 3
    return 2


def _cpu_worker_ceiling() -> int:
    """VODRIP_TRANSCRIBE_WORKERS (default dynamic by CPU thread count);
    0 = CPU side OFF on CUDA hosts.

    On CUDA hosts 0 restores the exclusive-GPU worker; on CPU-only hosts 0
    means auto (the dynamic default), matching the legacy CPU budget."""
    raw = os.environ.get(WORKERS_ENV, "").strip()
    if not raw:
        return _cpu_auto_workers()
    try:
        workers = int(raw)
    except ValueError:
        return _cpu_auto_workers()
    return workers if workers > 0 else 0


def _worker_plan() -> list[tuple[str, str]]:
    """(device, compute_type) slots for the transcribe pool.

    CUDA host (not forced off): GPU copies first (VODRIP_TRANSCRIBE_GPU_COPIES,
    default 1, VRAM+RAM clamped) then CPU threads (VODRIP_TRANSCRIBE_WORKERS,
    dynamic CPU default; 0 disables the CPU side). CPU-only host: [("cpu","int8")] *
    WORKERS (same dynamic default). Every CPU slot is RAM-clamped; the clamp is
    conservative on purpose because CPU and GPU copies share the same host
    RAM (ponytail: per-slot RSS is an estimate — if a box OOMs, lower
    VODRIP_TRANSCRIBE_WORKERS or VODRIP_TRANSCRIBE_GPU_COPIES).

    A plan of exactly [("cuda","float16")] (gpu_slots==1 and cpu_slots==0) is
    the legacy single-global-model path: budget 1, _infer_lock serializing
    inference. Any other plan -> multi-copy mode (per-thread model copies)."""
    device, _ = _effective_device()
    if device == "cpu":
        workers = _cpu_worker_ceiling() or _cpu_auto_workers()  # 0 == auto on CPU-only hosts
        slots = _ram_worker_clamp(workers, _CPU_WORKER_RSS_EST)
        if _cpu_load_high():
            slots = min(slots, 1)  # box contended — at most one CPU thread
        return [("cpu", "int8")] * slots
    gpu_slots = _gpu_copies()
    cpu_slots = _ram_worker_clamp(_cpu_worker_ceiling(), _CPU_WORKER_RSS_EST)
    if _cpu_load_high():
        cpu_slots = min(cpu_slots, 1)
    plan: list[tuple[str, str]] = [("cpu", "int8")] * cpu_slots
    if gpu_slots:
        # Only reach the lane ladder when the GPU lane is actually usable —
        # a held GPU (gpu_slots 0) must never trigger the ~60 s VRAM median.
        gpu_ct = _gpu_compute_type()  # float16 when VRAM fits, else int8
        plan = [("cuda", gpu_ct)] * gpu_slots + plan
    # Both sides clamped away (tight VRAM + busy box): a single CPU slot is
    # the floor — jobs must drain eventually, one quiet thread is safer
    # than a permanently parked worker.
    return plan or [("cpu", "int8")]


def _worker_budget() -> int:
    """Max concurrent transcribe jobs: len(_worker_plan()).

    1 on a CUDA host with VODRIP_TRANSCRIBE_WORKERS=0 (the exact legacy
    single-model path), 1 GPU copy + the dynamic CPU lane count on a
    default hybrid CUDA host (2 lanes on <16-thread boxes, 3 on 16–31,
    4 on 32+), WORKERS/GPU_COPIES ceilings and RAM clamps as before."""
    return len(_worker_plan())


def _pool_plan(max_workers: Optional[int]) -> list[tuple[str, str]]:
    """The worker pool's device plan for run_worker.

    max_workers overrides the natural plan for tests/launchers: all threads
    on the effective device (legacy semantics — the budget was a raw count)."""
    if max_workers is None:
        return _worker_plan()
    return [_effective_device()] * max(1, int(max_workers))


# How often the plan-watch thread re-evaluates the pool plan while the worker
# runs (30 s sits between the 10 s held-GPU cache and the ~60 s VRAM median,
# so a GPU that frees up is noticed within ~40 s worst case and a recheck
# rarely pays the full median). The recheck runs in its OWN thread because
# _worker_plan() can block ~60 s on the first VRAM median after a transition
# (held -> free) — blocking the worker loop that long would stall heartbeats,
# refills and job monitoring. The main loop only ever does the cheap swap.
_PLAN_RECHECK_S = 30.0


def _make_pool(plan: list[tuple[str, str]], lane_model: Optional[str], budget: int) -> ThreadPoolExecutor:
    """New transcribe executor pinned to ``plan`` (threads pin per-slot)."""
    return ThreadPoolExecutor(
        max_workers=budget, thread_name_prefix="transcribe",
        initializer=_worker_thread_init,
        initargs=(plan, lane_model),
    )


# --- model cache ----------------------------------------------------------

_model_lock = threading.Lock()
_model: Any = None
_model_name: Optional[str] = None
_model_last_used = 0.0
_word_ts_ok = True  # distil models have no alignment heads; flipped per process
# GPU capability ladder pinning: run_worker resolves _gpu_lane_plan() once at
# plan time and pins (model, compute_type) here so the GPU slots load the
# ladder's model+precision (e.g. 'medium' int8 on entry cards) while CPU
# slots keep the user's active model. Reset in run_worker's finally; direct
# callers (tests/scripts) never see a stale pin.
_worker_lane_model: Optional[str] = None
_worker_lane_ct: Optional[str] = None


def _idle_close_seconds() -> float:
    try:
        return float(os.environ.get(IDLE_ENV, "600") or "600")
    except ValueError:
        return 600.0


def _ensure_cuda_libs() -> None:
    """Expose pip-installed NVIDIA runtime DLLs (nvidia-*-cu12 wheels) on PATH.

    ctranslate2 loads cublas64_12.dll lazily at first CUDA inference, and the
    sherpa-onnx +cuda wheels' bundled onnxruntime_providers_cuda.dll loads
    cublasLt64_12/cufft64_11/curand64_10/cudnn64_9 at session create —
    cublasLt64_12 in turn loads nvjitlink64_12.dll, so the nvidia-nvjitlink-cu12
    wheel's bin dir must be on PATH too. Machines with a CUDA-13-era driver but
    no full CUDA 12 toolkit otherwise fail with "Library ... is not found".
    Windows wheels ship DLLs under nvidia/<pkg>/bin (PATH); POSIX wheels ship
    .so under nvidia/<pkg>/lib (LD_LIBRARY_PATH) — both layouts are exposed.
    Set VODRIP_NO_CUDA_LIBS to skip.
    """
    if os.environ.get("VODRIP_NO_CUDA_LIBS"):
        return
    try:
        import site as _site
    except Exception:
        return
    env_name = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    subdir = "bin" if os.name == "nt" else "lib"
    # Frozen (PyInstaller) builds: the wheels' DLLs are collected into the
    # bundle, and site.getsitepackages() does not point there — sys._MEIPASS
    # (onefile) and sys.prefix (onedir) are the bundle roots to search too.
    roots = list(_site.getsitepackages())
    for extra in (getattr(sys, "_MEIPASS", None), sys.prefix):
        if extra and Path(extra) not in [Path(r) for r in roots]:
            roots.append(str(extra))
    for lib in ("cublas", "cuda_runtime", "cufft", "curand", "cudnn", "nvjitlink"):
        try:
            for root in roots:
                d = Path(root) / "nvidia" / lib / subdir
                if d.is_dir() and str(d) not in os.environ.get(env_name, ""):
                    os.environ[env_name] = str(d) + os.pathsep + os.environ.get(env_name, "")
        except Exception:
            # ponytail: best-effort — missing wheels just mean CPU fallback
            pass


def _get_model() -> Any:
    """Return the process-global WhisperModel, lazy-loading on first use.

    Re-loads if VODRIP_WHISPER_MODEL changed since last load. Not thread-safe
    for *concurrent* transcribe() calls — callers serialize via _infer_lock.
    """
    global _model, _model_name, _model_last_used, _device_override, _model_device
    name = _worker_lane_model or model_name()
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
        if device == "cuda" and _worker_lane_ct:
            compute_type = _worker_lane_ct  # ladder precision (int8 default)
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
    """Unload the cached models, freeing RAM. Safe mid-transcription: workers
    hold a local reference, so the object lives until their last use.

    Also drops the cached VAD model (lazy-reloaded by the next job) and, in
    multi-copy mode, every pool thread's whisper model AND parakeet
    recognizer too (threads lazily reload on their next job — the registry
    is cleared, so a fresh slot is created). The process-global parakeet
    recognizer (single-model mode) is dropped as well."""
    global _vad, _parakeet_global
    closed_any = False
    with _model_lock:
        _close_model_unlocked()
        for slot in _thread_slots.values():
            model, slot.model = slot.model, None
            if model is not None:
                logger.info("Unloading whisper thread model")
                del model
                closed_any = True
            parakeet, slot.parakeet = slot.parakeet, None
            if parakeet is not None:
                logger.info("Unloading parakeet thread recognizer")
                del parakeet
                closed_any = True
        _thread_slots.clear()
        parakeet, _parakeet_global = _parakeet_global, None
        if parakeet is not None:
            logger.info("Unloading parakeet recognizer")
            del parakeet
            closed_any = True
    with _vad_lock:
        vad, _vad = _vad, None
        if vad is not None:
            logger.info("Unloading VAD model")
            del vad
            closed_any = True
    if closed_any:
        gc.collect()


def _maybe_close_idle_model() -> None:
    """Close the process-global whisper model or parakeet recognizer after
    VODRIP_WHISPER_IDLE_CLOSE seconds without use. Thread models die with
    the pool (close_model on worker shutdown)."""
    idle_sec = _idle_close_seconds()
    if _model is not None and time.monotonic() - _model_last_used > idle_sec:
        logger.info("Model idle for %.0fs — unloading", idle_sec)
        close_model()
        return
    if _parakeet_global is not None and time.monotonic() - _parakeet_last_used > idle_sec:
        logger.info("Parakeet recognizer idle for %.0fs — unloading", idle_sec)
        close_model()


# --- per-thread model copies (multi-copy mode, budget > 1) ------------------
# Each pool thread owns one WhisperModel instance so inference runs truly in
# parallel (no global _infer_lock). The registry is keyed by thread ident —
# the same per-thread keying CPython's threading.local uses internally.
# Model CREATION is serialized by _model_lock (shared hub download);
# inference never takes it. A thread whose CUDA inference OOM'd marks itself
# cpu_fallback and reloads on CPU — only that thread degrades. In hybrid
# mode _worker_thread_init pins each pool thread to its plan slot (GPU or
# CPU) at thread start, so a pinned CPU thread loads on CPU even though the
# box has a GPU.

_multi_tls = threading.local()  # per-thread: .active, .cpu_fallback, .pin


class _ThreadModelSlot:
    """One pool thread's lazy model state (whisper copy + parakeet recognizer)."""
    __slots__ = ("model", "model_name", "parakeet")

    def __init__(self) -> None:
        self.model: Any = None
        self.model_name: Optional[str] = None
        self.parakeet: Any = None  # sherpa-onnx OfflineRecognizer (provider per slot pin)


_thread_slots: dict[int, _ThreadModelSlot] = {}


def _in_multi_mode() -> bool:
    return bool(getattr(_multi_tls, "active", False))


def _thread_cpu_fallback() -> bool:
    return bool(getattr(_multi_tls, "cpu_fallback", False))


def _thread_mark_cpu_fallback() -> None:
    _multi_tls.cpu_fallback = True


_pool_thread_seq = count()  # plan-slot index handed to each new pool thread


def _thread_pin() -> Optional[tuple[str, str]]:
    """The calling pool thread's pinned device slot, or None.

    Set once per pool thread by _worker_thread_init at thread creation;
    None for direct callers and the legacy single-model path (they fall
    back to _effective_device())."""
    return getattr(_multi_tls, "pin", None)


def _worker_thread_init(plan: list[tuple[str, str]], lane_model: Optional[str] = None) -> None:
    """ThreadPoolExecutor initializer — pin this pool thread to its slot.

    Threads are created one at a time in submission order, so thread i gets
    plan[i % len(plan)] (the modulo guards a second pool in the same
    process, whose threads keep counting past the first pool's). GPU-slot
    threads also pin the ladder model (e.g. 'medium' on entry cards); CPU
    slots keep the user's active model."""
    _multi_tls.pin = plan[next(_pool_thread_seq) % len(plan)]
    _multi_tls.lane_model = (
        lane_model if _multi_tls.pin and _multi_tls.pin[0] == "cuda" else None
    )


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
    serialized by _model_lock, inference is not. The thread loads on its
    pinned device slot when one was set by the pool initializer (hybrid
    mode), else on _effective_device(). A thread marked cpu_fallback loads
    on CPU even when CUDA is healthy (its copy OOM'd earlier)."""
    slot = _thread_slot()
    name = getattr(_multi_tls, "lane_model", None) or model_name()
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

        device, compute_type = _thread_pin() or _effective_device()
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


# --- Parakeet lane (sherpa-onnx) ------------------------------------------
# A/B verdict (2026-08-07, 60 s pt-BR segments, i5-13600K): parakeet TDT v3
# int8 on CPU runs 2.5-5.2 RTFx vs whisper large-v3-turbo cpu/int8 at
# 0.26-0.6 (7-15x), ~0.7 GB less peak RSS, and outputs nothing on silence
# (no hallucination). GPU: CUDA-enabled sherpa-onnx wheels exist since
# 1.13.x (sherpa-onnx==X+cuda12.cudnn9 — see requirements.txt); when one is
# importable, GPU slots run parakeet with provider='cuda', gated on the
# measured free-VRAM allowance; without it they stay whisper (graceful
# degradation — byte-identical to the pre-parakeet worker). The lane is
# opt-in end to end: sherpa-onnx missing OR VODRIP_PARAAKEET=0 -> whisper.
PARAKEET_MODEL = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
_PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
_PARAAKEET_FEATURE_DIM = 128  # nemo_transducer default (80) fails — must match the model
# Model card: 25 European languages. Intersected at runtime with the lang
# tokens the model's tokens.txt actually carries (see _parakeet_langs), so a
# model/cache swap missing a language falls back to whisper for that job.
PARAKEET_LANG_CANDIDATES = frozenset({
    "pt", "en", "es", "fr", "de", "it", "ru", "uk", "pl", "nl", "sv", "da",
    "no", "fi", "el", "tr", "hu", "cs", "ro", "bg", "hr", "sk", "sl", "et",
    "lv", "lt",
})
# A/B-measured sweet spot: 8 decode threads per lane on an i5-13600K; two
# concurrent streams on ONE recognizer added only +18% (CPU-bound), so lanes
# never share a recognizer — each pool thread owns its own.
_PARAAKEET_MAX_THREADS = 8

_parakeet_ok: Optional[bool] = None  # sherpa-onnx import probe (None = unprobed)
_parakeet_global: Any = None  # process-global recognizer (single-model mode)
_parakeet_last_used = 0.0


def _parakeet_available() -> bool:
    """True when the parakeet lane can run.

    VODRIP_PARAAKEET=0 is a hard kill switch (no import probe). Otherwise
    the sherpa-onnx import is probed once per process and cached; an import
    failure degrades lanes to whisper — exactly today's behavior."""
    if os.environ.get(PARAKEET_ENV, "1").strip() == "0":
        return False
    global _parakeet_ok
    if _parakeet_ok is None:
        try:
            import sherpa_onnx  # noqa: F401
            _parakeet_ok = True
        except Exception:
            _parakeet_ok = False
    return _parakeet_ok


_parakeet_cuda_ok: Optional[bool] = None  # CUDA-provider probe (None = unprobed)


def _parakeet_cuda_available() -> bool:
    """True when the installed sherpa-onnx is a CUDA build.

    The +cuda wheels (see requirements.txt) bundle onnxruntime's CUDA EP and
    version as ``X.Y.Z+cuda<cuda-ver>.cudnn9``; the plain CPU wheels carry no
    tag. The sherpa-onnx Python bindings expose no provider-enumeration API,
    so the build tag is the cheap static probe and ``_load_parakeet(
    provider='cuda')`` is the AUTHORITATIVE runtime probe — it raises unless
    the CUDA EP actually initializes, then degrades to CPU and flips this
    flag (ponytail: if k2-fsa ever changes the tag scheme, the construction
    fallback still keeps behavior correct). Probed once per process and
    cached; a probe failure means GPU slots stay whisper. Tests/self-check
    pin ``_parakeet_cuda_ok`` directly so they never import sherpa-onnx."""
    if not _parakeet_available():
        return False
    global _parakeet_cuda_ok
    if _parakeet_cuda_ok is None:
        try:
            import sherpa_onnx

            _parakeet_cuda_ok = bool(
                "+cuda" in (getattr(sherpa_onnx, "__version__", "") or "")
            )
        except Exception:
            _parakeet_cuda_ok = False
    return _parakeet_cuda_ok


# Parakeet int8 on GPU: ~0.7 GB of weights + the CUDA EP's arena. GPU slots
# route parakeet only when the measured free-VRAM allowance (fresh cache
# read — never re-triggers the ~60 s median probe) leaves this much free.
_PARAKEET_GPU_VRAM_EST = int(2.0 * 1024 ** 3)


def _parakeet_gpu_allowed() -> bool:
    """VRAM gate for GPU-slot parakeet routing.

    CUDA sherpa is a necessary but not sufficient condition: the measured
    free-VRAM allowance must cover the parakeet footprint. Reads the CACHED
    allowance only (a cold/stale cache reads as unknown -> 0 -> allowed): a
    GPU slot only exists when _gpu_copies() measured >= 2 GiB free at claim
    time, and parakeet is the SMALLER model — a lane that fit whisper fits
    it. Unknown allowance trusting the provider probe mirrors the
    probe-failure paths elsewhere (env cap trusted)."""
    if not _parakeet_cuda_available():
        return False
    now = time.monotonic()
    with _vram_lock:
        fresh = (
            _vram_free_at
            and now - _vram_free_at < _GPU_VRAM_MEDIAN_GAP_S * _GPU_VRAM_MEDIAN_SAMPLES
        )
        allowance = _vram_free_bytes if fresh else 0
    if allowance <= 0:
        return True  # unknown/cold allowance -> trust the provider probe
    return allowance >= _PARAKEET_GPU_VRAM_EST


def _parakeet_cache_dir() -> Path:
    """Sherpa model cache: VODRIP_SHERRPA_CACHE override, else a SIBLING of
    the whisper cache — never inside it (the disk-hygiene sweep would prune
    a non-whisper HF-style model dir as 'inactive')."""
    override = os.environ.get(PARAKEET_CACHE_ENV, "").strip()
    if override:
        return Path(override)
    base = _cache_dir()
    return base.parent / "parakeet-models"


def _parakeet_resolve_dir() -> Optional[Path]:
    """The model dir when all four files are present locally, else None.

    Accepts the files at the cache root or under the model-name subdir (the
    hf_hub_download(local_dir=...) layout); NEVER downloads — the caller
    decides whether a download is wanted."""
    cache = _parakeet_cache_dir()
    for d in (cache / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8", cache):
        if all((d / f).is_file() for f in _PARAKEET_FILES):
            return d
    return None


def _parakeet_model_dir() -> Path:
    """Ensure the parakeet model files exist locally (auto-download on first
    use into the sherpa cache via huggingface_hub) and return the dir."""
    d = _parakeet_resolve_dir()
    if d is not None:
        return d
    try:
        from huggingface_hub import hf_hub_download
    except Exception as exc:
        raise RuntimeError(
            "Parakeet model download needs huggingface_hub — install it or "
            "pre-seed the sherpa cache (VODRIP_SHERRPA_CACHE)"
        ) from exc
    target = _parakeet_cache_dir() / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    target.mkdir(parents=True, exist_ok=True)
    for f in _PARAKEET_FILES:
        logger.info("Downloading parakeet model file %s ...", f)
        hf_hub_download(repo_id=PARAKEET_MODEL, filename=f, local_dir=str(target))
    if not all((target / f).is_file() for f in _PARAKEET_FILES):
        raise RuntimeError(f"parakeet model download incomplete in {target}")
    return target


def _parakeet_langs() -> frozenset[str]:
    """Languages routed to parakeet: the candidate set intersected with the
    lang tokens the model actually carries (<|pt|> etc. in tokens.txt).

    The intersection is the runtime guard against a model/cache mismatch (a
    swapped model missing a language falls back to whisper for it). When the
    model isn't downloaded yet the candidate set is trusted — routing is
    correct either way (the guard only ever narrows)."""
    if not _parakeet_available():
        return frozenset()
    d = _parakeet_resolve_dir()
    if d is not None:
        found: set[str] = set()
        try:
            for line in (d / "tokens.txt").read_text(encoding="utf-8").splitlines():
                m = re.match(r"^<\|([a-z]{2})\|>\s", line)
                if m:
                    found.add(m.group(1))
        except OSError:
            found = set()
        if found:
            return frozenset(PARAKEET_LANG_CANDIDATES & found)
    return PARAKEET_LANG_CANDIDATES


def _parakeet_threads() -> int:
    """sherpa-onnx decode threads per CPU lane: the box's cores divided by
    the CPU lane count, capped at the A/B-measured 8-thread sweet spot.
    Machine-aware: a 20-thread box with 3 CPU lanes gets 6."""
    lanes = max(1, _cpu_worker_ceiling() or _cpu_auto_workers())  # 0 == auto on CPU-only hosts
    cores = os.cpu_count() or 4
    return max(1, min(_PARAAKEET_MAX_THREADS, cores // lanes))


def _slot_engine(device: str) -> str:
    """Engine for a plan slot: 'parakeet' when the slot can run it (CUDA
    slots need a CUDA-enabled sherpa-onnx, CPU slots the plain import;
    VODRIP_PARAAKEET=0 kills both), else 'whisper'."""
    if device == "cuda":
        return "parakeet" if _parakeet_cuda_available() else "whisper"
    return "parakeet" if _parakeet_available() else "whisper"


def _job_engine(language: Optional[str]) -> str:
    """Engine for THIS job on the calling lane: 'parakeet' when the lane can
    run it AND the job's language is in parakeet's supported set; known
    other languages (ja, ...) and UNKNOWN language stay whisper. GPU slots
    use parakeet only with CUDA sherpa present AND enough measured free
    VRAM; CPU slots need the plain import. Without a CUDA wheel the GPU slot
    is whisper exactly as before."""
    device, _ = _thread_pin() or _effective_device()
    if device == "cuda":
        if (
            _parakeet_cuda_available()
            and _parakeet_gpu_allowed()
            and language in _parakeet_langs()
        ):
            return "parakeet"
        return "whisper"
    if _parakeet_available() and language in _parakeet_langs():
        return "parakeet"
    return "whisper"


def _asr_model_name(engine: str) -> str:
    """The model id reported/written for a run's engine: the parakeet repo
    id for parakeet runs, the active whisper model otherwise."""
    return PARAKEET_MODEL if engine == "parakeet" else model_name()


def _parakeet_provider() -> str:
    """'cuda' on a CUDA-pinned slot with CUDA sherpa present, else 'cpu'.

    Mirrors the whisper thread's device pin: off-pool callers and CPU slots
    get the plain CPU provider (the int8 recognizer they always had)."""
    device, _ = _thread_pin() or _effective_device()
    if device == "cuda" and _parakeet_cuda_available():
        return "cuda"
    return "cpu"


def _load_parakeet(provider: str = "cpu") -> Any:
    """Build one sherpa-onnx OfflineRecognizer (nemo_transducer).

    provider='cuda' on GPU slots with a CUDA-enabled sherpa-onnx (the +cuda
    wheels bundle a CUDA onnxruntime); 'cpu' everywhere else. A CUDA load
    failure degrades THIS recognizer to CPU and flips the cached probe, so
    later jobs route per reality instead of failing."""
    import sherpa_onnx

    d = _parakeet_model_dir()
    t0 = time.monotonic()
    kwargs = dict(
        encoder=str(d / "encoder.int8.onnx"),
        decoder=str(d / "decoder.int8.onnx"),
        joiner=str(d / "joiner.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        num_threads=_parakeet_threads(),
        sample_rate=SAMPLE_RATE,
        feature_dim=_PARAAKEET_FEATURE_DIM,
        model_type="nemo_transducer",
    )
    if provider != "cpu":
        _ensure_cuda_libs()  # onnxruntime_providers_cuda.dll needs the cu12 DLLs on PATH
        kwargs["provider"] = provider
    try:
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
    except Exception as exc:
        if provider == "cpu":
            raise
        global _parakeet_cuda_ok
        _parakeet_cuda_ok = False
        kwargs.pop("provider", None)
        logger.warning(
            "parakeet CUDA recognizer failed to load (%s) — falling back to CPU "
            "(GPU slots will stay whisper for the rest of this process)", exc,
        )
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
        provider = "cpu"
    logger.info(
        "Parakeet recognizer loaded in %.1fs (provider=%s threads=%d, cache=%s)",
        time.monotonic() - t0, provider, _parakeet_threads(), _parakeet_cache_dir(),
    )
    return rec


def _parakeet_model() -> Any:
    """The recognizer for the current context: the calling thread's own copy
    in multi-copy mode, else the process-global one. Mirrors _current_model;
    creation is serialized by _model_lock (shared model dir + download);
    inference never takes a lock (each recognizer has one owner)."""
    global _parakeet_global, _parakeet_last_used
    _parakeet_last_used = time.monotonic()
    if _in_multi_mode():
        slot = _thread_slot()
        if slot.parakeet is None:
            with _model_lock:
                if slot.parakeet is None:
                    slot.parakeet = _load_parakeet(provider=_parakeet_provider())
        return slot.parakeet
    with _model_lock:
        if _parakeet_global is None:
            _parakeet_global = _load_parakeet(provider=_parakeet_provider())
        return _parakeet_global


def _parakeet_words(tokens: list[str], timestamps: list[float]) -> list[dict]:
    """Word-level items from the recognizer's per-token timestamps.

    The HF-converted vocab marks word-initial pieces with a leading space
    (the source SentencePiece ▁); the lone-space piece is a space INSIDE a
    word ('de 10'), so a new word starts only on a piece that begins with a
    space and is longer than one char. Word end = the last token's start
    time (monotonic; matches whisper's word shape). Validated on real audio:
    concatenating the pieces reconstructs result.text exactly."""
    words: list[dict] = []
    cur = ""
    start: Optional[float] = None
    end: Optional[float] = None
    for tok, ts in zip(tokens, timestamps):
        word_start = tok.startswith(" ") and len(tok) > 1
        if word_start and cur:
            words.append({
                "word": cur,
                "start": round(start or 0.0, 3),
                "end": round(end or 0.0, 3),
            })
            cur, start = tok.lstrip(" "), ts
        elif word_start:
            cur, start = tok.lstrip(" "), ts
        elif not cur:
            cur, start = tok, ts  # stream opened mid-word (not seen in practice)
        else:
            cur += tok
        end = ts
    if cur:
        words.append({
            "word": cur,
            "start": round(start or 0.0, 3),
            "end": round(end or 0.0, 3),
        })
    return words


def _transcribe_batch_parakeet(
    rec: Any,
    audio: "Any",
    chunks: list[tuple[float, float]],
    language: Optional[str],
    *,
    clip_offsets: Optional[list[float]] = None,
) -> list[tuple[list[dict], Optional[str]]]:
    """Decode [start,end] clips with the sherpa-onnx parakeet recognizer.

    One OfflineStream per clip (recognizers are stateless per stream; the
    A/B measured 2-stream concurrency at only +18% — CPU-bound — so clips
    decode sequentially). Segments carry absolute video timestamps and the
    same JSON shape _transcribe_batch produces: one segment per clip with
    word-level timestamps. An empty transcript (silence -> no words — the
    parakeet no-hallucination behavior) yields no segment for that clip.
    ponytail: whisper splits segments on its own sentence boundaries; here
    one VAD chunk is one segment. Upgrade path: split a segment at word
    gaps > 1 s if the UI ever needs finer granularity."""
    out: list[tuple[list[dict], Optional[str]]] = []
    for i, (cs, ce) in enumerate(chunks):
        base = 0.0 if clip_offsets is None else clip_offsets[i]
        s0, s1 = int(cs * SAMPLE_RATE), int(ce * SAMPLE_RATE)
        clip = audio[s0:s1]
        stream = rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, clip)
        rec.decode_stream(stream)
        res = stream.result
        text = (res.text or "").strip()
        if not text:
            out.append(([], language))
            continue
        words = _parakeet_words(
            getattr(res, "tokens", []) or [],
            getattr(res, "timestamps", []) or [],
        )
        last_word_end = (words[-1]["end"] if words else float(ce)) + base
        items = [{
            "start_sec": round(cs + base, 3),
            "end_sec": round(min(ce + base, last_word_end + 0.3), 3),
            "text": text,
            "words": words,
        }]
        out.append((items, language))
    return out


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


def _write_manifest_header(
    path: Path, chunks: list[tuple[float, float]], engine: str = "whisper"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "chunks": chunks,
            "model": _asr_model_name(engine),
            "engine": engine,
        }) + "\n")


def _append_manifest_entry(path: Path, ci: int, first: int, count: int) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ci": ci, "first": first, "count": count}) + "\n")


def _resume_plan(
    chunks: list[tuple[float, float]],
    header: Optional[dict],
    entries: dict[int, dict],
    existing: set[int],
    engine: str = "whisper",
) -> tuple[list[int], int]:
    """Return (chunk indices to transcribe, next free seg_idx).

    Entries are trusted only when the header matches the current plan, model
    AND engine (an engine switch — parakeet <-> whisper — invalidates the
    manifest like a model change does; pre-parakeet manifests carry no
    'engine' key and read as whisper). A chunk is also re-transcribed when
    any row in its recorded seg_idx range is missing (manual delete /
    partial write), and the next free index is the LOWEST gap in existing —
    so deleted rows are restored at their old index and the seg_idx sequence
    stays contiguous."""
    if not chunks:
        return [], 0
    next_idx = 0
    while next_idx in existing:
        next_idx += 1
    if not (header and entries):
        return list(range(len(chunks))), next_idx
    # JSON round-trip turns the plan's tuples into lists — compare shapes.
    if (
        [tuple(c) for c in header.get("chunks", [])] != chunks
        or header.get("engine", "whisper") != engine
        or header.get("model") != _asr_model_name(engine)
    ):
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
        # The thread that ran this job may be CPU-pinned in the hybrid pool —
        # report the actual device, not the global default.
        _ran = _thread_pin() or _effective_device()
        stats = {
            "platform": platform,
            "video_id": video_id,
            "model": model_name(),
            "device": _ran[0],
            "compute_type": _ran[1],
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
    # no-speech video never pays the (large) load cost. The engine is the
    # slot's choice for this job's language: parakeet (slots with a usable
    # sherpa — CUDA on GPU slots when CUDA sherpa + VRAM allow, int8 CPU
    # otherwise — for supported languages) or whisper (everything else).
    engine = _job_engine(language)
    model = _parakeet_model() if engine == "parakeet" else _current_model()
    existing = {int(r["seg_idx"]) for r in archive_db.transcript_for(platform, video_id, raw=True)}
    header, entries = _read_manifest(_manifest_path(platform, video_id))
    missing, seg_idx = _resume_plan(chunks, header, entries, existing, engine=engine)
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
    twin_won = False  # higher-priority twin transcribed mid-run — abort
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
        batch_fn = _transcribe_batch_parakeet if engine == "parakeet" else _transcribe_batch
        if sharded_audio is not None:
            batch_audio, concat_clips, clip_offsets = _clips_to_audio(sharded_audio, run)
            batch_out = batch_fn(
                model, batch_audio, concat_clips, language, clip_offsets=clip_offsets,
            )
        else:
            batch_out = batch_fn(model, audio, [c for _, c in run], language)
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
                if _twin_transcribed_while_running(platform, video_id):
                    # The higher-priority twin (youtube > twitch > kick)
                    # finished mid-run — the guard evaluated at claim time
                    # missed it. Drop the partial rows (the display
                    # fallback then serves the twin's transcript instead of
                    # these) and stop; the job still reports done+skipped.
                    archive_db.delete_transcripts(platform, video_id)
                    logger.info(
                        "twin transcribed on a higher-priority platform while "
                        "running — aborting %s/%s",
                        platform, video_id,
                    )
                    twin_won = True
                    break
                archive_db.insert_transcript(platform, video_id, batch_rows, lang=lang)
            segments += len(batch_rows)
            _append_manifest_entry(manifest, ci2, first_idx, len(chunk_segs))
            speech_done += chunks[ci2][1] - chunks[ci2][0]
            if progress_cb:
                progress_cb(speech_done, speech_sec, ci2 + 1, n_chunks)
        if twin_won:
            break

    # Disk hygiene: the job finished — the crash-resume manifest has served
    # its purpose. Best-effort: a failed unlink just leaves it for the next
    # run (which would resume into an empty plan and rewrite it anyway).
    try:
        manifest.unlink(missing_ok=True)
    except OSError:
        pass

    if twin_won:
        _ran = _thread_pin() or _effective_device()
        return {
            "platform": platform,
            "video_id": video_id,
            "model": _asr_model_name(engine),
            "engine": engine,
            "device": _ran[0],
            "compute_type": _ran[1],
            "segments": 0,
            "words": 0,
            "resumed_chunks": 0,
            "wall_sec": round(time.monotonic() - t0, 3),
            "skipped": "dedupe-transcribed",
        }

    wall = time.monotonic() - t0
    # Report the device that actually ran the job (hybrid pool threads may be
    # CPU-pinned); falls back to the global default off-pool.
    _ran = _thread_pin() or _effective_device()
    stats = {
        "platform": platform,
        "video_id": video_id,
        "model": _asr_model_name(engine),
        "engine": engine,
        "device": _ran[0],
        "compute_type": _ran[1],
        "total_sec": round(total_sec, 3),
        "speech_sec": round(speech_sec, 3),
        "dead_air_sec": round(dead_air_sec, 3),
        "dead_air_pct": round(dead_air_pct, 1),
        "segments": segments,
        "words": words,
        "resumed_chunks": len(chunks) - len(missing),
        "wall_sec": round(wall, 3),
        "speed_x": round(speech_sec / wall, 2) if wall > 0 else 0.0,
        # Whisper's own detection (None when the job ran with an explicit
        # language — then the stored rows carry the explicit tag, never the
        # detection). The done-time channel-language correction uses it.
        "lang": detected_lang if language is None else None,
    }
    logger.info(
        "transcribe %s/%s done: %d segs, %d words, %.1f%% dead air skipped, "
        "%.2fx realtime on %s/%s (%s)",
        platform, video_id, segments, words, dead_air_pct,
        stats["speed_x"], stats["device"], stats["compute_type"], engine,
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
# Chat-history backfills run one yt-dlp/GQL pass per video; a 13.5h VOD's
# live-chat replay can legitimately exceed the 30min transcribe reclaim
# window, so 'chat' jobs get a 2h grace before a dead executor is assumed.
_CHAT_STALE_TIMEDELTA = timedelta(hours=2)
# Twitch chat backfills heartbeat their job row after every stored page
# (backfill_chat's progress_cb), so a live executor's heartbeat is seconds
# old. A running job whose heartbeat stalls past this window is a dead or
# wedged fetch (urllib timeouts bound a single page to ~20s + the 429
# backoff chain) — reclaim it instead of letting it hold the row for the
# flat 2h _CHAT_STALE_TIMEDELTA. The YouTube leg never heartbeats during
# its long yt-dlp download (heartbeat stays NULL) and keeps the 2h window.
_CHAT_HEARTBEAT_STALE = timedelta(minutes=10)

# YouTube chat-backfill pacing: min gap between chat video STARTS. A single
# worker starts ≤5/min (burst 2 requests each: extract + chat download); the
# 3-thread pool can run up to 3 concurrently, still under the measured
# 4-6/min per-IP limiter. Two lanes (user requirement): the interactive lane
# (preview/download/click-chat/search/watch) NEVER consults this pace or the
# bot gate — pacing exists only in the worker's background lane. While the
# user is actively using the app (an 'app-activity' heartbeat stamped by the
# app middleware) the interval grows to _YOUTUBE_CHAT_ACTIVE_INTERVAL_S so
# background volume stays under the radar and interactive traffic is never
# collateral damage; when the app is idle the worker ramps back to heavy
# volume. ponytail: per-process only — cross-process pacing needs a shared
# lock file if worker_server and the in-process worker ever overlap on one
# box.
_YOUTUBE_CHAT_MIN_INTERVAL_S = 12.0
_YOUTUBE_CHAT_ACTIVE_INTERVAL_S = 30.0
_APP_ACTIVITY_AGE_S = 60.0
_youtube_chat_last_start = 0.0
_youtube_chat_pace_lock = threading.Lock()


def _youtube_chat_interval() -> float:
    """Pacing interval: longer while the app's interactive lane is active,
    longer still in background (autostart) mode — the quota-sensitive
    YouTube chat fetch backs off to ~2.5x the interactive gap."""
    base = _YOUTUBE_CHAT_ACTIVE_INTERVAL_S
    if not archive_db.worker_live(age_s=_APP_ACTIVITY_AGE_S, tag="app-activity"):
        base = _YOUTUBE_CHAT_MIN_INTERVAL_S
    if background_mode():
        return base * 2.5
    return base


def _pace_youtube_chat() -> None:
    """Sleep until the pacing budget allows another YouTube chat fetch."""
    global _youtube_chat_last_start
    with _youtube_chat_pace_lock:
        interval = _youtube_chat_interval()
        wait = interval - (time.monotonic() - _youtube_chat_last_start)
        if wait > 0:
            time.sleep(wait)
        _youtube_chat_last_start = time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim_next_job() -> Optional[dict]:
    """Atomically claim the newest queued transcribe/events/chat job (crash-stale too).

    A 'running' job is reclaimed only if untouched past its kind's stale
    window: 30 min for transcribe/events (a single chunk can legitimately
    take that long on CPU), 2 h for 'chat' on YouTube (a 13.5h VOD's
    live-chat replay download heartbeats nothing mid-run). Twitch 'chat'
    jobs heartbeat after every stored page, so a running one whose
    heartbeat went stale past _CHAT_HEARTBEAT_STALE (10 min) is a dead or
    wedged executor — reclaimed long before the flat 2h window; NULL
    heartbeats (pre-heartbeat rows, YouTube) fall back to updated_at."""
    now = datetime.now(timezone.utc)
    transcribe_cutoff = (now - _STALE_JOB_TIMEDELTA).isoformat(timespec="seconds")
    twitch_chat_cutoff = (now - _CHAT_HEARTBEAT_STALE).isoformat(timespec="seconds")
    yt_chat_cutoff = (now - _CHAT_STALE_TIMEDELTA).isoformat(timespec="seconds")
    # String comparison is valid: both sides come from _now_iso (UTC, same width).
    rows = archive_db.query(
        """SELECT * FROM archive_jobs
           WHERE kind IN ('transcribe','events','chat')
             AND (status = 'queued' OR (status = 'running' AND
                  COALESCE(heartbeat, updated_at) <
                  CASE WHEN kind = 'chat' AND platform = 'twitch' THEN ?
                       WHEN kind = 'chat' THEN ?
                       ELSE ? END))
           ORDER BY priority DESC, created_at ASC
           LIMIT 8""",
        (twitch_chat_cutoff, yt_chat_cutoff, transcribe_cutoff),
    )
    for row in rows:
        # Bot-gate freeze: never claim YouTube jobs while the gate is up.
        # The processors requeue gated YouTube jobs (never fail), so a
        # claim would flip running -> queued and the refill loop would
        # re-claim the same row ~2 ms later — a hot loop spamming
        # "requeued: bot-gate cooldown" for the whole freeze window.
        # Leave them queued untouched; they drain once the gate lifts.
        if youtube_gate_active() and row["platform"] == "youtube" and row["kind"] != "events":
            continue
        # The claim refreshes the heartbeat too: a re-claimed row must not
        # match the stale predicate again before the new executor's first
        # progress touch (that would let a third worker steal it mid-claim).
        if row["status"] == "queued":
            cur = archive_db.execute(
                "UPDATE archive_jobs SET status = 'running', updated_at = ?, heartbeat = ? "
                "WHERE id = ? AND status = 'queued'",
                (_now_iso(), _now_iso(), row["id"]),
            )
        else:
            # Stale-reclaim CAS: the UPDATE re-checks the same stale-window
            # condition the SELECT used, so two workers that both read the
            # same stale 'running' row cannot both claim it — the first
            # claim refreshes heartbeat/updated_at and the second's WHERE
            # matches zero rows (rowcount 0 -> skip). Without the condition
            # both UPDATEs would hit (status stays 'running' either way) and
            # two workers would transcribe the same video.
            cur = archive_db.execute(
                "UPDATE archive_jobs SET status = 'running', updated_at = ?, heartbeat = ? "
                "WHERE id = ? AND status = 'running' "
                "AND COALESCE(heartbeat, updated_at) < "
                "CASE WHEN kind = 'chat' AND platform = 'twitch' THEN ? "
                "WHEN kind = 'chat' THEN ? ELSE ? END",
                (_now_iso(), _now_iso(), row["id"], twitch_chat_cutoff, yt_chat_cutoff, transcribe_cutoff),
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


def _process_chat_job(job_id: str, platform: str, video_id: str) -> dict:
    """Run one kind='chat' job (chat-history backfill) — never raises.

    YouTube: the chat-only yt-dlp live-chat replay pass (no caption or
    metadata re-fetch). Twitch: the incremental GQL backfill, seeded from
    the deepest stored offset, tracking THIS job's row. Kick has no retro
    chat API — chat arrives only via live capture, so a kick 'chat' job
    fails fast (the scheduler never enqueues one)."""
    if platform == "youtube":
        if youtube_gate_active():
            # Requeue, never fail: the cooldown lifts with one probe and the
            # job drains then. A failed row would sit behind FAILED_JOB_FRESH_S.
            archive_db.update_job(
                job_id, status="queued",
                error=f"youtube bot-gate cooldown active ({int(gate_remaining_sec())}s) — requeued",
            )
            logger.info("youtube chat job %s requeued: bot-gate cooldown", job_id)
            return {
                "job_id": job_id, "platform": platform, "video_id": video_id,
                "requeued": "youtube-gate",
            }
        _pace_youtube_chat()
        from services.archive_ytdlp import backfill_live_chat

        result = backfill_live_chat(video_id)
        archive_db.update_job(job_id, status="done", progress=1.0)
        return {
            "job_id": job_id,
            "platform": platform,
            "video_id": video_id,
            "chat_messages": result.get("chat_messages", 0),
            "chat": result.get("chat"),
        }
    if platform == "twitch":
        from services.archive_scheduler import BACKFILL_MAX_MESSAGES  # noqa: PLC0415 — lazy: keeps this module opt-in light
        from services.archive_twitch import backfill_chat

        channel = (archive_db.video_channel(platform, video_id) or "").strip()
        if not channel:
            raise ValueError(f"no channel for twitch/{video_id} — cannot backfill chat")
        # progress_cb=None: backfill_chat stamps the job row (progress +
        # heartbeat) after every stored page in both lanes — the worker
        # lane's own per-page update would double-write the same row.
        result = backfill_chat(
            channel, video_id,
            max_messages=BACKFILL_MAX_MESSAGES,
            job_id=job_id,
        )
        archive_db.update_job(job_id, status="done", progress=1.0)
        return {
            "job_id": job_id,
            "platform": platform,
            "video_id": video_id,
            "chat_messages": result.get("inserted", 0),
            "chat": "replay",
        }
    raise ValueError(f"no retro chat API for platform {platform!r}")


def _twin_transcribed_while_running(platform: str, video_id: str) -> bool:
    """True when a higher-priority twin finished transcribing mid-run.

    The guard transcribed_on_higher_priority_platform is evaluated at claim
    time; this re-evaluates it right before each chunk INSERT so a twitch/
    kick job that started while the youtube twin was still running aborts
    instead of duplicating work. youtube itself is never blocked (it is the
    highest priority), so the check short-circuits for it."""
    if platform == "youtube" or platform not in archive_db._PLATFORM_TRANSCRIBE_PRIORITY:
        return False
    return archive_db.transcribed_on_higher_priority_platform(platform, video_id)


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
    if channel:
        # videos.channel_language is NULL for THIS video (no platform clue
        # stamped on it yet) — fall back to the channel-language aggregation,
        # which weighs every channel video's stored clue plus the WS-4
        # original_language evidence. This kills the wrong-language case:
        # whisper auto-detect (None) misfiring on a channel whose language
        # is known elsewhere (e.g. a twitch twin with a youtube clue).
        try:
            from services.channel_language import aggregate_channel_language

            hint = normalize_language(
                aggregate_channel_language(platform, channel).get("language")
            )
        except Exception:
            hint = None  # aggregation is best-effort — fall through to the default
        if hint:
            return hint
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

        if job.get("kind") == "chat":
            return _process_chat_job(job_id, platform, video_id)

        if platform == "youtube" and youtube_gate_active():
            # Bot-gate cooldown: requeue (not fail) so the job drains once
            # the freeze lifts; the gate check must precede every YouTube
            # network hop (caption skip, dedupe, audio download).
            archive_db.update_job(
                job_id, status="queued",
                error=f"youtube bot-gate cooldown active ({int(gate_remaining_sec())}s) — requeued",
            )
            logger.info("youtube job %s requeued: bot-gate cooldown", job_id)
            return {
                "job_id": job_id, "platform": platform, "video_id": video_id,
                "requeued": "youtube-gate",
            }

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

        resolved_lang = _resolve_job_language(platform, video_id)
        stats = transcribe_video(
            platform, video_id,
            language=resolved_lang,
            progress_cb=_progress, events_cb=events_cb,
        )
        archive_db.update_job(job_id, status="done", progress=1.0)
        # New transcript evidence -> re-aggregate the channel language
        # (throttled; best-effort — a failure must never fail the job).
        # When the job ran on auto-detection and the channel's now-known
        # family disagrees with what whisper heard, the stored rows are
        # re-stamped (see channel_language.on_transcribe_done). An explicit
        # job language is never overridden.
        try:
            from services.channel_language import on_transcribe_done
            on_transcribe_done(
                platform, video_id,
                detected_lang=stats.get("lang") if resolved_lang is None else None,
            )
        except Exception:
            logger.debug("channel language re-aggregation failed", exc_info=True)
        if "skipped" not in stats:
            # Heavy batch writes just finished — merge the FTS b-tree
            # segments so search stays fast (best-effort inside). Skipped
            # for tiny jobs: a few segments don't fragment anything.
            if stats.get("segments", 0) >= 50:
                archive_db.optimize_fts()
        return stats
    except FileNotFoundError as exc:
        # Archive file evicted/swept (or never written) — the job can never
        # succeed. Mark it failed with a warning, no traceback; the scheduler's
        # fresh-failure guard then stops re-enqueuing it.
        logger.warning(
            "transcribe job %s skipped — %s", job_id, exc
        )
        archive_db.update_job(job_id, status="failed",
                              error=f"{type(exc).__name__}: {exc}"[:400])
        return {
            "job_id": job_id,
            "skipped": "archive-file-missing",
            "error": str(exc),
        }
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

    Hybrid pool: the plan (_worker_plan()) is [("cuda","float16")]*gpu_slots +
    [("cpu","int8")]*cpu_slots on CUDA hosts (GPU copy + dynamic CPU lanes),
    [("cpu","int8")]*auto on CPU-only hosts. Each pool thread is
    pinned to its slot by the executor initializer, so a pinned CPU thread
    loads its model on CPU even though the box has a GPU. Engines are chosen
    PER JOB: GPU slots run parakeet with provider='cuda' for parakeet
    languages when a CUDA sherpa-onnx is installed and VRAM allows, else
    whisper fp16; CPU slots run parakeet int8 or whisper int8 as before. The
    shared queue stays FIFO (_claim_next_job) with no duration routing: the
    GPU thread claims the next job when it finishes one, CPU threads pick up
    queued VODs in the meantime.

    A plan of exactly one CUDA slot (VODRIP_TRANSCRIBE_WORKERS=0) is the
    legacy single-global-model path: budget 1, _infer_lock, the global
    _current_model(). max_workers overrides the plan for tests/launchers
    (all threads on the effective device, legacy raw-count semantics).

    DYNAMIC PLAN (natural plan only, max_workers=None): the pool plan is
    re-evaluated every _PLAN_RECHECK_S by a daemon plan-watch thread. When
    the GPU frees up (another app closed) or gets grabbed, the watch
    proposes the new plan and the main loop swaps the executor: in-flight
    jobs run out on the old pool (shutdown(wait=False)), fresh claims go to
    the new one — a GPU that becomes free turns the worker GPU-on without a
    restart. max_workers plans are static (tests/launchers pin them).
    """
    plan = _pool_plan(max_workers)
    budget = len(plan)
    multi = budget > 1
    # Capability-ladder pinning: the GPU lane's model+precision (e.g. the
    # user's model int8, or 'medium' int8 on entry cards) is resolved once
    # at claim time and pinned for this run — CPU slots keep the active
    # model. Only when the natural plan has CUDA slots: a CPU-only plan
    # (held GPU / tight VRAM) and the legacy max_workers path must never
    # pay the ~60 s VRAM median for a lane they cannot use. Reset in
    # finally so direct callers never inherit a stale pin.
    global _worker_lane_model, _worker_lane_ct
    _lane = (
        _gpu_lane_plan()
        if max_workers is None and any(d == "cuda" for d, _ in plan)
        else None
    )
    _worker_lane_model = _lane[0] if _lane else None
    _worker_lane_ct = _lane[1] if _lane else None
    logger.info("archive transcribe worker: plan=[%s] workers=%d lane=%s",
                ", ".join(f"{d}/{ct}" for d, ct in plan), budget,
                _worker_lane_model or "active-model")
    # Dynamic plan re-evaluation: a GPU that frees up (or gets grabbed) is
    # noticed within ~_PLAN_RECHECK_S and the pool is swapped. In-flight
    # jobs finish on the OLD executor (shutdown(wait=False) lets them run
    # out); the new executor only takes fresh claims, so no job is lost.
    # The watch runs in a daemon thread because a recheck can block ~60 s on
    # the VRAM median after a held->free transition — the main loop never
    # waits for that, it only consumes the cheap proposal.
    plan_proposal: list = []  # (new_plan, lane) published by the watch
    _proposal_lock = threading.Lock()
    watch_stop = threading.Event()

    def _plan_watch() -> None:
        while True:
            if watch_stop.wait(_PLAN_RECHECK_S) or _WORKER_STOP.is_set():
                return
            try:
                new_plan = _pool_plan(max_workers)
            except Exception:
                logger.exception("plan watch: recheck failed")  # keep old plan
                continue
            if new_plan == plan:
                continue
            _new_lane = (
                _gpu_lane_plan()
                if any(d == "cuda" for d, _ in new_plan)
                else None
            )
            with _proposal_lock:
                plan_proposal[:] = [new_plan, _new_lane]

    watch = threading.Thread(target=_plan_watch, name="plan-watch", daemon=True)
    if max_workers is None:
        watch.start()

    pool = _make_pool(plan, _worker_lane_model, budget)
    try:
        with pool:
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
            while not _WORKER_STOP.is_set():
                _maybe_close_idle_model()
                archive_db.worker_heartbeat("transcribe")
                with _proposal_lock:
                    proposed = tuple(plan_proposal)
                    if proposed:
                        plan_proposal.clear()
                if proposed:
                    new_plan, _new_lane = proposed
                    old_pool = pool
                    old_pool.shutdown(wait=False)  # in-flight run out there
                    plan, budget, multi = new_plan, len(new_plan), len(new_plan) > 1
                    _worker_lane_model = _new_lane[0] if _new_lane else None
                    _worker_lane_ct = _new_lane[1] if _new_lane else None
                    pool = _make_pool(plan, _worker_lane_model, budget)
                    logger.info(
                        "archive transcribe worker: plan changed -> [%s] "
                        "workers=%d lane=%s (old pool draining)",
                        ", ".join(f"{d}/{ct}" for d, ct in plan), budget,
                        _worker_lane_model or "active-model",
                    )
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
        if max_workers is None:
            watch_stop.set()
            watch.join(timeout=2.0)
        _worker_lane_model = None
        _worker_lane_ct = None
        close_model()


# In-process worker lifecycle: the app lifespan starts the loop and MUST be
# able to stop it on shutdown. Tests that boot the app via TestClient reuse
# the same module, so an un-stopped worker would keep claiming jobs from the
# shared test DB for the whole pytest session (and hold the _BACKFILL_SEM
# lanes) — stop_worker() is called by the lifespan teardown.
_WORKER_STOP = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def start_worker(**kwargs: Any) -> threading.Thread:
    """Spawn the worker loop in a daemon thread (never blocks the caller)."""
    global _worker_thread
    _WORKER_STOP.clear()
    thread = threading.Thread(
        target=run_worker, kwargs=kwargs, name="archive-transcribe", daemon=True
    )
    with _worker_lock:
        _worker_thread = thread
    thread.start()
    return thread


def stop_worker(*, timeout: float = 6.0) -> None:
    """Signal the in-process worker loop to exit and wait for it.

    Idempotent; safe to call when no worker was started (e.g. a detached
    worker owns the queue instead). The running job, if any, finishes
    first — the loop only stops claiming new work.
    """
    _WORKER_STOP.set()
    with _worker_lock:
        thread = _worker_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _WORKER_STOP.clear()


# ── GPU sherpa auto-provision (mirrors youtube_ytdlp_update) ────────────
# Users never run a command: on an NVIDIA host with the CPU sherpa-onnx
# wheel installed, the app upgrades to the +cuda wheel itself, once, at
# boot. Frozen builds skip (the bundle pins the wheel it ships — the yt-dlp
# self-update rule) and VODRIP_NO_GPU_AUTOINSTALL="1" is the escape hatch.
GPU_AUTOINSTALL_ENV = "VODRIP_NO_GPU_AUTOINSTALL"
_GPU_AUTOINSTALL_INTERVAL_SEC = 24 * 3600  # at most once a day
_GPU_AUTOINSTALL_TIMEOUT_S = 600  # 200 MB wheel + nvidia deps over slow links


def _gpu_autoinstall_stamp_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "/tmp"
    return Path(base) / "VOD.RIP" / "gpu_sherpa_last_check.txt"


def _gpu_autoinstall_due() -> bool:
    path = _gpu_autoinstall_stamp_path()
    if not path.is_file():
        return True
    try:
        last = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (time.time() - last) >= _GPU_AUTOINSTALL_INTERVAL_SEC


def _gpu_autoinstall_needed() -> bool:
    """True when THIS host should run the +cuda wheel but has the CPU one.

    Gate on the same probes the lane uses: NVIDIA GPU present, parakeet
    lane enabled, sherpa-onnx importable, and no '+cuda' in its version.
    A missing sherpa-onnx is NOT auto-installed (the lane is opt-in; the
    base requirements ship the CPU wheel) — only the CPU->CUDA swap is
    automatic, exactly the yt-dlp update posture."""
    if os.environ.get(GPU_AUTOINSTALL_ENV, "").strip() == "1":
        return False
    if os.environ.get(PARAKEET_ENV, "1").strip() == "0":
        return False
    if detect_gpu_vendor() != "nvidia":
        return False
    try:
        import sherpa_onnx

        return "+cuda" not in (getattr(sherpa_onnx, "__version__", "") or "")
    except Exception:
        return False  # not installed / import broken — leave the lane alone


def maybe_ensure_gpu_sherpa() -> None:
    """Swap the CPU sherpa-onnx for the +cuda wheel on NVIDIA hosts, once a day.

    No-op under a frozen install (the bundle ships its own wheel, and the
    install tree is read-only for self-update — same rule as the yt-dlp
    check). The upgrade happens in the background at boot; a running worker
    keeps its in-memory recognizer until the next process start, which is
    fine because the lane's CUDA probe runs at construction time."""
    if getattr(sys, "frozen", False):
        logger.debug("GPU sherpa auto-install skipped (frozen install)")
        return
    if not _gpu_autoinstall_due():
        return
    path = _gpu_autoinstall_stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        logger.debug("GPU auto-install stamp write failed: %s", exc)
    if not _gpu_autoinstall_needed():
        return
    logger.info("NVIDIA GPU detected with CPU sherpa-onnx — auto-installing the CUDA wheel")
    try:
        proc = sp.run(
            [
                sys.executable, "-m", "pip", "install",
                "sherpa-onnx==1.13.4+cuda12.cudnn9",
                "-f", "https://k2-fsa.github.io/sherpa/onnx/cuda.html",
                "nvidia-cufft-cu12", "nvidia-curand-cu12", "nvidia-cudnn-cu12",
            ],
            capture_output=True, text=True, timeout=_GPU_AUTOINSTALL_TIMEOUT_S,
            check=False,
        )
        if proc.returncode == 0:
            logger.info("sherpa-onnx CUDA wheel installed — GPU slots can run parakeet")
        else:
            logger.debug(
                "GPU sherpa auto-install exit %s: %s",
                proc.returncode, (proc.stderr or "")[:300],
            )
    except (OSError, sp.TimeoutExpired) as exc:
        logger.debug("GPU sherpa auto-install failed: %s", exc)


def schedule_gpu_sherpa_ensure() -> threading.Thread:
    """Daemon thread — never blocks API startup (mirrors the yt-dlp check)."""
    def _run() -> None:
        try:
            maybe_ensure_gpu_sherpa()
        except Exception as exc:
            logger.debug("GPU sherpa ensure thread failed: %s", exc)

    t = threading.Thread(target=_run, name="gpu-sherpa-ensure", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="VOD.RIP archive worker — drains transcribe/events/chat jobs"
    )
    ap.add_argument(
        "--once", action="store_true",
        help="exit rc 0 once the queue is drained (no pending/running jobs and "
             "no futures in flight); default runs forever",
    )
    ap.add_argument(
        "--poll-interval", type=float, default=2.0,
        help="seconds between idle queue polls (default 2.0)",
    )
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.once:
        # Belt-and-suspenders on top of worker_server's own guard: a live
        # worker heartbeat means someone else is draining the queue — exit
        # rc 0 quietly instead of double-loading the whisper model.
        from services import archive_db

        if archive_db.worker_live(age_s=45):
            logging.getLogger(__name__).info(
                "archive worker already running — nothing to do (exit 0)"
            )
            raise SystemExit(0)
    run_worker(once=args.once, poll_interval=max(0.1, args.poll_interval))

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
# clamp caps copies at free_vram // (model_est + 2 GiB headroom), never
# below 1 (the ladder's lane gate owns the 0-copies decision); CPU honors
# VODRIP_TRANSCRIBE_WORKERS and defaults to 2.
_per_copy = _gpu_model_vram_est() + _GPU_VRAM_HEADROOM
assert _clamp_cuda_copies(1, 100 << 30) == 1, "no GPU copies env -> 1 (probe skipped)"
assert _clamp_cuda_copies(4, 4 * _per_copy + 1) == 4, "env within the VRAM budget passes through"
assert _clamp_cuda_copies(8, 2 * _per_copy + 1) == 2, "VRAM budget clamps copies"
assert _clamp_cuda_copies(8, _per_copy - 1) == 1, "VRAM budget never drops below 1"
_saved_override, _saved_workers = _device_override, os.environ.get(WORKERS_ENV)
_saved_free_ram = _free_system_ram_bytes
_saved_vram = _gpu_free_vram_bytes
_saved_cpu = _cpu_load_high
try:
    _device_override = ("cpu", "int8")
    _free_system_ram_bytes = lambda: 64 * 1024 ** 3  # RAM clamp must not bind here
    _cpu_load_high = lambda: False
    os.environ[WORKERS_ENV] = "4"
    assert _worker_budget() == 4, "CPU budget must honor VODRIP_TRANSCRIBE_WORKERS"
    os.environ.pop(WORKERS_ENV, None)
    assert _worker_budget() == _cpu_auto_workers(), (
        "CPU budget must default to the thread-count ladder"
    )
    assert _cpu_auto_workers() >= 2, "dynamic default never below the legacy floor"
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
    _gpu_free_vram_bytes = _saved_vram
    _cpu_load_high = _saved_cpu
    if _saved_workers is None:
        os.environ.pop(WORKERS_ENV, None)
    else:
        os.environ[WORKERS_ENV] = _saved_workers
# hybrid pool plan: CUDA host -> 1 GPU copy + 2 CPU threads by default;
# WORKERS=0 disables the CPU side (the exact legacy single-model plan);
# WORKERS=3 -> 1 GPU + 3 CPU slots. RAM is patched ample so the clamp never
# binds; the VRAM probe is patched per tier so the capability ladder decides
# the GPU lane's model+precision.
_saved_plan_ov, _saved_plan_w, _saved_plan_g = (
    _device_override, os.environ.get(WORKERS_ENV), os.environ.get(GPU_COPIES_ENV),
)
_saved_plan_vram, _saved_plan_cpu = _gpu_free_vram_bytes, _cpu_load_high
_saved_plan_held = _gpu_held_by_other
_saved_plan_util = _gpu_util
try:
    _device_override = ("cuda", "float16")
    _free_system_ram_bytes = lambda: 64 * 1024 ** 3
    _gpu_free_vram_bytes = lambda: 64 * 1024 ** 3  # ample VRAM — clamp must not bind
    _gpu_held_by_other = lambda: False
    _gpu_util = lambda: None
    _cpu_load_high = lambda: False
    os.environ.pop(WORKERS_ENV, None)
    os.environ.pop(GPU_COPIES_ENV, None)
    assert _worker_plan() == [("cuda", "float16")] + [("cpu", "int8")] * _cpu_auto_workers(), (
        "CUDA host defaults to 1 GPU copy + dynamic CPU lanes"
    )
    os.environ[WORKERS_ENV] = "0"
    assert _worker_plan() == [("cuda", "float16")], "WORKERS=0 -> exclusive-GPU plan"
    os.environ[WORKERS_ENV] = "3"
    assert _worker_plan() == [
        ("cuda", "float16"), ("cpu", "int8"), ("cpu", "int8"), ("cpu", "int8"),
    ], "WORKERS=3 -> 1 GPU + 3 CPU slots"
    # machine-aware scale-down: tight VRAM (another app holds the GPU model)
    # -> GPU copy 0, CPU slots cover; busy box -> at most 1 CPU slot; both ->
    # the 1-CPU-slot floor keeps the queue draining. (WORKERS back to the
    # dynamic default for these — the 3-slot plan above was a ceiling check.)
    os.environ.pop(WORKERS_ENV, None)
    _gpu_free_vram_bytes = lambda: 1 * 1024 ** 3  # < 2 GiB floor
    assert _worker_plan() == [("cpu", "int8")] * _cpu_auto_workers(), (
        "sub-2 GiB VRAM must drop the GPU copy, CPU side covers"
    )
    # capability ladder rungs (simulated free VRAM -> lane model+precision)
    _gpu_free_vram_bytes = lambda: int(3.0 * 1024 ** 3)
    assert _gpu_lane_plan() == ("medium", "int8"), "2-3.5 GiB -> medium int8 entry rung"
    _gpu_free_vram_bytes = lambda: int(5.0 * 1024 ** 3)
    assert _gpu_lane_plan() == (None, "int8"), "3.5-6.5 GiB -> active model int8"
    _gpu_free_vram_bytes = lambda: int(8.0 * 1024 ** 3)
    assert _gpu_lane_plan() == (None, "float16"), ">= 6.5 GiB -> active model fp16"
    _cpu_slots = [("cpu", "int8")] * _cpu_auto_workers()
    assert _worker_plan() == [("cuda", "float16")] + _cpu_slots, (
        "8 GiB tier -> fp16 GPU lane + dynamic CPU lanes"
    )
    _gpu_free_vram_bytes = lambda: int(3.0 * 1024 ** 3)
    assert _worker_plan() == [("cuda", "int8")] + _cpu_slots, (
        "3 GiB tier -> medium int8 GPU slot + dynamic CPU lanes"
    )
    # compute-apps guard: another process holds a GPU model -> CPU only
    _gpu_free_vram_bytes = lambda: 16 * 1024 ** 3
    _gpu_held_by_other = lambda: True
    assert _worker_plan() == _cpu_slots, (
        "held GPU model must drop the GPU lane (never stack)"
    )
    _gpu_held_by_other = lambda: False
    _gpu_free_vram_bytes = lambda: 64 * 1024 ** 3  # ample VRAM restored
    # busy GPU: a second copy is capped at 1 when util >= 70%
    os.environ[GPU_COPIES_ENV] = "3"
    _gpu_util = lambda: 0.85
    assert _worker_plan() == [("cuda", "float16")] + _cpu_slots, (
        "busy GPU caps copies at 1"
    )
    _gpu_util = lambda: 0.4
    assert _worker_plan() == [
        ("cuda", "float16"), ("cuda", "float16"), ("cuda", "float16"),
    ] + _cpu_slots, "idle GPU + ample VRAM allows the configured 3 copies"
    os.environ.pop(GPU_COPIES_ENV, None)
    # contended box: at most 1 CPU slot
    _gpu_free_vram_bytes = lambda: 64 * 1024 ** 3
    _cpu_load_high = lambda: True
    assert _worker_plan() == [("cuda", "float16"), ("cpu", "int8")], (
        "contended box must keep at most 1 CPU slot"
    )
    _gpu_free_vram_bytes = lambda: 1 * 1024 ** 3
    assert _worker_plan() == [("cpu", "int8")], (
        "tight VRAM + busy box -> 1 CPU slot floor"
    )
finally:
    _device_override = _saved_plan_ov
    _free_system_ram_bytes = _saved_free_ram
    _gpu_free_vram_bytes = _saved_plan_vram
    _cpu_load_high = _saved_plan_cpu
    _gpu_held_by_other = _saved_plan_held
    _gpu_util = _saved_plan_util
    if _saved_plan_w is None:
        os.environ.pop(WORKERS_ENV, None)
    else:
        os.environ[WORKERS_ENV] = _saved_plan_w
    if _saved_plan_g is None:
        os.environ.pop(GPU_COPIES_ENV, None)
    else:
        os.environ[GPU_COPIES_ENV] = _saved_plan_g

# parakeet lane — engine routing (pure logic: the import probe is pinned
# via the cached _parakeet_ok flag, sherpa-onnx is never imported here and
# nothing downloads; the sherpa cache is pointed at a scratch dir with a
# controlled tokens.txt for the intersection check).
_saved_pok, _saved_pin = _parakeet_ok, getattr(_multi_tls, "pin", None)
_saved_peng, _saved_pcache = _device_override, os.environ.get(PARAKEET_CACHE_ENV)
_saved_penv = os.environ.get(PARAKEET_ENV)
_saved_pcuda = _parakeet_cuda_ok
_saved_pvram = (_vram_free_bytes, _vram_free_at)
_scratch_sherpa = Path(tempfile.mkdtemp(prefix="vodrip-parakeet-selfcheck-"))
try:
    os.environ[PARAKEET_CACHE_ENV] = str(_scratch_sherpa)  # no model dir yet
    _parakeet_ok = True
    _parakeet_cuda_ok = True
    _vram_free_bytes = 64 * 1024 ** 3
    _vram_free_at = time.monotonic()
    _device_override = ("cpu", "int8")
    assert _job_engine("pt") == "parakeet", "pt routes to parakeet on a CPU lane"
    assert _job_engine("en") == "parakeet", "en routes to parakeet on a CPU lane"
    assert _job_engine("es") == "parakeet", "es routes to parakeet on a CPU lane"
    assert _job_engine("ja") == "whisper", "known-other languages stay on whisper"
    assert _job_engine(None) == "whisper", "unknown language stays on whisper"
    _device_override = ("cuda", "float16")
    assert _slot_engine("cuda") == "parakeet", "CUDA sherpa -> GPU slots may run parakeet"
    assert _job_engine("pt") == "parakeet", (
        "GPU slot + CUDA sherpa + ample VRAM + supported lang -> parakeet"
    )
    assert _job_engine("ja") == "whisper", "GPU slots keep whisper for other languages"
    _parakeet_cuda_ok = False
    assert _slot_engine("cuda") == "whisper", (
        "no CUDA sherpa -> GPU slots stay whisper (graceful degradation)"
    )
    assert _job_engine("pt") == "whisper", "GPU slot without CUDA sherpa -> whisper"
    _parakeet_cuda_ok = True
    _vram_free_bytes = 1 * 1024 ** 3  # tight VRAM — fresh cache read
    assert _job_engine("pt") == "whisper", (
        "GPU slot + CUDA sherpa but tight VRAM falls back to whisper"
    )
    _vram_free_bytes = 64 * 1024 ** 3
    _parakeet_cuda_ok = True
    _device_override = ("cpu", "int8")
    os.environ[PARAKEET_ENV] = "0"
    assert _job_engine("pt") == "whisper", "VODRIP_PARAAKEET=0 kills the parakeet lane"
    os.environ.pop(PARAKEET_ENV, None)
    _parakeet_ok = False
    assert _job_engine("pt") == "whisper", "sherpa-onnx import failure -> whisper"
    assert _parakeet_langs() == frozenset(), "import-fail lane routes no languages"
    _parakeet_ok = True
    assert _parakeet_langs() == PARAKEET_LANG_CANDIDATES, (
        "the candidate set is authoritative until the model dir exists"
    )
    assert 1 <= _parakeet_threads() <= _PARAAKEET_MAX_THREADS, (
        "thread budget must be positive and capped at the A/B sweet spot"
    )
    # tokens.txt present -> the candidate set must narrow to the model's
    # actual lang tokens (a swapped model missing a language falls back).
    _pd = _scratch_sherpa / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    _pd.mkdir(parents=True, exist_ok=True)
    for _f in _PARAKEET_FILES:
        (_pd / _f).write_text("x", encoding="utf-8")  # existence is all the resolver checks
    (_pd / "tokens.txt").write_text(
        "<|pt|> 0\n<|ja|> 1\n<|zh|> 2\n", encoding="utf-8"
    )
    assert _parakeet_langs() == {"pt"}, (
        "routing must intersect the candidate set with the model's lang tokens"
    )
    # word assembly: the real vocab convention (space-prefixed word-initial
    # pieces, lone-space piece inside a word) must produce whisper-shaped words.
    _toks = [" N", "eg", "an", " de", " ", "1", "0", " minut", "os", ",", " né", "?"]
    _ts = [0.32, 0.48, 0.56, 0.8, 1.04, 1.12, 1.12, 1.2, 1.28, 1.36, 2.08, 2.24]
    _ws = _parakeet_words(_toks, _ts)
    assert [w["word"] for w in _ws] == ["Negan", "de 10", "minutos,", "né?"], _ws
    assert _ws[0] == {"word": "Negan", "start": 0.32, "end": 0.56}, _ws[0]
finally:
    _parakeet_ok = _saved_pok
    _parakeet_cuda_ok = _saved_pcuda
    _vram_free_bytes, _vram_free_at = _saved_pvram
    _device_override = _saved_peng
    _multi_tls.pin = _saved_pin
    if _saved_pcache is None:
        os.environ.pop(PARAKEET_CACHE_ENV, None)
    else:
        os.environ[PARAKEET_CACHE_ENV] = _saved_pcache
    if _saved_penv is None:
        os.environ.pop(PARAKEET_ENV, None)
    else:
        os.environ[PARAKEET_ENV] = _saved_penv
    shutil.rmtree(_scratch_sherpa, ignore_errors=True)
