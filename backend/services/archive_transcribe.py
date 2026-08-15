"""Transcription worker — parakeet (sherpa-onnx nemo_transducer) + Silero VAD.

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
  * Model cache: one process-global parakeet OfflineRecognizer by default
    (budget 1), lazy-loaded on first job, unloaded after
    VODRIP_WHISPER_IDLE_CLOSE seconds (default 600) without use. Multi-copy
    mode (budget > 1 — CPU workers or opt-in VODRIP_TRANSCRIBE_GPU_COPIES)
    gives each pool thread its own recognizer so inference runs in parallel.
  * Hybrid pool (CUDA hosts): the worker runs the GPU copy AND CPU threads
    at the same time — VODRIP_TRANSCRIBE_GPU_COPIES GPU slots (default 1)
    plus VODRIP_TRANSCRIBE_WORKERS CPU slots (default 2 on <16-thread boxes,
    3 on 16–31, 4 on 32+; 0 disables the CPU side and restores the
    exclusive-GPU worker). Each pool thread is pinned to its slot's device
    at thread start, so CPU threads never compete for VRAM. CPU-only hosts
    are unchanged (WORKERS, same dynamic default).
  * Engine: parakeet (sherpa-onnx, nemo_transducer TDT v3 int8) is the ONLY
    ASR engine — faster-whisper was removed. It covers 26 European language
    families (PARAKEET_LANG_CANDIDATES) plus unknown/auto-detect. A known
    language outside that set fails the job cleanly (explicit error naming
    the language + the 26-language coverage); a lane without usable parakeet
    (sherpa-onnx missing / VODRIP_PARAAKEET=0 / no CUDA wheel on a GPU slot /
    VRAM too tight) fails the job cleanly too. There is NO whisper fallback.
    CUDA-enabled sherpa-onnx wheels (>=1.13.x, see requirements.txt) let GPU
    slots run parakeet with provider='cuda', gated on the measured free-VRAM
    allowance. Model auto-downloads on first use into the sherpa cache
    (VODRIP_SHERRPA_CACHE or an AI-models-folder sibling).
  * Device: _real_gpu_info() — a COMPUTE-level probe (nvidia-smi memory
    query and/or the CUDA runtime's device count + context init), never
    adapter names — a Virtual Display Driver / name-spoofed adapter has no
    CUDA device and resolves to cpu. This machine has an NVIDIA RTX 5080
    (CUDA works via torch), so real runs are cuda; the CPU path exists for
    GPU-less hosts.

Opt-in by design: app.py does NOT import this module. Start the worker with
``python -m services.archive_transcribe`` or ``start_worker()`` from a launcher.
"""
from __future__ import annotations

import gc
import json
import logging
import math
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

from services import archive_db, transcript_fix
from services.archive_events import detect_events_video, events_enabled
from services.autostart import background_mode
from services.disk_hygiene import whisper_cache_dir
from services.os_services import _NO_WINDOW
from services.yt_gate import gate_remaining_sec, youtube_gate_active
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe, _resolve_ffprobe_exe

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Env knobs (all optional).
LANG_ENV = "VODRIP_WHISPER_LANGUAGE"
WORKERS_ENV = "VODRIP_TRANSCRIBE_WORKERS"  # CPU threads; 0 = GPU-only on CUDA hosts
IDLE_ENV = "VODRIP_WHISPER_IDLE_CLOSE"
GPU_COPIES_ENV = "VODRIP_TRANSCRIBE_GPU_COPIES"
PARAKEET_ENV = "VODRIP_PARAAKEET"          # "0" kills the parakeet lane (clean job failure)
PARAKEET_CACHE_ENV = "VODRIP_SHERRPA_CACHE"  # sherpa-onnx model cache override
CPU_CAP_ENV = "VODRIP_TRANSCRIBE_CPU_CAP"  # ASR CPU-thread fraction of logical threads (hard cap)
try:
    _CPU_CAP_FRAC = float((os.environ.get(CPU_CAP_ENV, "") or "0.4").strip() or "0.4")
except (ValueError, OverflowError):
    _CPU_CAP_FRAC = 0.4  # bad env value must not crash the import
if not math.isfinite(_CPU_CAP_FRAC):
    _CPU_CAP_FRAC = 0.4  # inf/nan would blow the budget to the whole box


def _cpu_thread_budget() -> int:
    """Hard ceiling of ASR CPU threads: 40% of logical threads (default), any machine.

    _CPU_CAP_FRAC (the import-time parse) is the unset-env fallback; a live
    env value is re-parsed per call so VODRIP_TRANSCRIBE_CPU_CAP changes
    apply without a reload. Bad values (unparsable, inf, NaN) fall back to
    the 0.4 default."""
    frac = _CPU_CAP_FRAC
    raw = os.environ.get(CPU_CAP_ENV, "").strip()
    if raw:
        try:
            frac = float(raw)
        except (ValueError, OverflowError):
            frac = 0.4
        if not math.isfinite(frac):
            frac = 0.4
    return max(1, int(frac * (os.cpu_count() or 4)))
# Music/no-speech verdict for captionless YouTube ASR: below this fraction
# of speech (speech_sec / total_sec) the audio is treated as instrumental
# music — the video is marked transcript_kind='music' (job done, no ASR
# run, never re-enqueued). Env-tunable.
MUSIC_SPEECH_FRAC = float(os.environ.get("VODRIP_MUSIC_SPEECH_FRAC", "0.03"))
# Re-check delay for a transcribe job whose caption question is still open
# (no captions AND no captions_unavailable_at marker — the ingest leg is
# extracting/retrying). Aligned with the scheduler's per-video YouTube
# retry backoff (YOUTUBE_RETRY_BACKOFF_S) so the job re-checks at the same
# cadence the ingest retry resolves.
CAPTION_WAIT_RETRY_S = 3600.0

# --- device / compute -----------------------------------------------------

# Real-GPU detection: NEVER trust adapter names. A "Virtual Display Driver"
# (or any name-spoofed virtual adapter) has no CUDA compute — the probes
# below ask the driver/runtime for actual devices, and their absence means
# no real GPU, so every caller keeps the degrade-to-CPU path.
_REAL_GPU_MIN_VRAM = int(1 * 1024 ** 3)  # below this it is not a compute GPU
_REAL_GPU_PROBE_TIMEOUT_S = 5.0


_smi_gpu_index = 0  # nvidia-smi index of the discrete compute GPU we picked


def _parse_smi_vram_rows(stdout: str) -> list[tuple[int, int, int]]:
    """[(index, total_bytes, free_bytes), ...] from csv noheader total,free lines."""
    rows: list[tuple[int, int, int]] = []
    for i, line in enumerate((stdout or "").strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            total_mib, free_mib = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append((i, total_mib * 1024 ** 2, free_mib * 1024 ** 2))
    return rows


def _nvidia_smi_vram() -> Optional[tuple[int, int]]:
    """(total, free) VRAM bytes of the largest NVIDIA GPU; None when absent.

    nvidia-smi exists only when the NVIDIA driver stack is real — a virtual
    display adapter has no SMI. When several NVIDIA devices are listed we
    pick the one with the most total VRAM (the discrete card), never GPU 0
    blindly (GPU 0 can be a tiny NVIDIA virtual display on some boxes)."""
    global _smi_gpu_index
    try:
        out = sp.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_REAL_GPU_PROBE_TIMEOUT_S,
            creationflags=_NO_WINDOW,
        )
        if out.returncode != 0:
            return None
        rows = _parse_smi_vram_rows(out.stdout or "")
        if not rows:
            return None
        idx, total, free = max(rows, key=lambda r: r[1])
        _smi_gpu_index = idx
        return total, free
    except (OSError, ValueError, sp.TimeoutExpired):
        return None


def _cuda_runtime_vram() -> Optional[tuple[int, int]]:
    """(total, free) VRAM bytes from the CUDA runtime via nvcuda.dll.

    cudaMemGetInfo forces the PRIMARY CONTEXT on device 0 — the true
    compute-init test. A fake adapter (no CUDA device) fails
    cudaGetDeviceCount==0 or the context init, so this returns None and
    callers see no GPU. Windows-only; POSIX returns None (nvidia-smi
    covers it there)."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        nv = ctypes.WinDLL("nvcuda.dll")
        count = ctypes.c_int(0)
        if nv.cudaGetDeviceCount(ctypes.byref(count)) != 0 or count.value <= 0:
            return None
        best: Optional[tuple[int, int]] = None
        for i in range(count.value):
            if nv.cudaSetDevice(i) != 0:
                continue
            free_b, total_b = ctypes.c_size_t(0), ctypes.c_size_t(0)
            if nv.cudaMemGetInfo(ctypes.byref(free_b), ctypes.byref(total_b)) != 0:
                continue
            cand = (int(total_b.value), int(free_b.value))
            if best is None or cand[0] > best[0]:
                best = cand
        return best
    except (OSError, AttributeError):
        return None


def _real_gpu_info() -> tuple[bool, int, int]:
    """(present, total_vram_bytes, free_vram_bytes) for a REAL CUDA GPU.

    Both probes are compute-level: the nvidia-smi memory query (the driver's
    own view) or the CUDA runtime's device count + context init. Either
    succeeding with total VRAM >= _REAL_GPU_MIN_VRAM -> present. A Virtual
    Display Driver / name-spoofed adapter fails both -> (False, 0, 0), and
    every caller keeps the graceful CPU fallback."""
    smi = _nvidia_smi_vram()
    if smi is not None:
        total, free = smi
    else:
        rt = _cuda_runtime_vram()
        if rt is None:
            return False, 0, 0
        total, free = rt
    if total < _REAL_GPU_MIN_VRAM:
        return False, 0, 0
    return True, total, free


@lru_cache(maxsize=1)
def _detect_device() -> tuple[str, str]:
    """(device, compute_type) — real nvidia GPU or honest CPU fallback.

    VODRIP_WHISPER_DEVICE=cpu|cuda forces the choice (used by tests/benchmarks;
    the env name is legacy but kept — it pins the DEVICE, not an engine).
    The presence gate is COMPUTE-based (_real_gpu_info), never adapter names:
    a Virtual Display Driver / name-spoofed adapter has no CUDA device and
    resolves to CPU here. compute_type is always 'int8' — parakeet's weights
    are int8 ONNX regardless of device; only the provider differs.
    """
    forced = os.environ.get("VODRIP_WHISPER_DEVICE", "").strip().lower()
    if forced:
        if forced == "cuda":
            return "cuda", "int8"
        if forced == "cpu":
            return "cpu", "int8"
        logger.warning("Unknown VODRIP_WHISPER_DEVICE=%r — ignoring", forced)
    if _real_gpu_info()[0]:
        return "cuda", "int8"
    return "cpu", "int8"


def _effective_device() -> tuple[str, str]:
    """(device, compute_type) for the current lane — pool pin when set,
    else the detected default."""
    return _thread_pin() or _detect_device()


def device_settings() -> tuple[str, str]:
    return _detect_device()


def _cache_dir() -> Path:
    # Shared resolver: VODRIP_WHISPER_CACHE env -> settings.whisper_model_cache
    # (AI-models folder) -> auto best-ROI drive + VOD.RIP-models -> appdata.
    return whisper_cache_dir()


# --- parallelism budget ---------------------------------------------------

# GPU lane budget (user hardware: RTX 5080 16 GiB; large-v3-turbo fp16 is
# ~5-6 GiB, NOT the old 1-2.5 GiB estimate). The card is a SHARED tenant:
# the desktop + the user's other ML project hold VRAM, so the measured
# allowance at claim time decides the lane — never a static count.
_GPU_VRAM_HEADROOM = int(2 * 1024 ** 3)   # must stay free for the tenants
_GPU_UTIL_SECOND_COPY = 0.70              # below this, a 2nd copy may add
# Thermal ceiling: sustained 100% util for hours heats the card and can
# destabilize the Windows driver (TDR resets, black screens). The decode
# loop paces batches so measured utilization stays at/below this fraction.
_GPU_MAX_UTIL = 0.90
_GPU_MAX_UTIL_WAIT_S = 30.0               # ceiling on the pacing wait per batch
_GPU_VRAM_MEDIAN_SAMPLES = 6              # reads spread over ~60 s
_GPU_VRAM_MEDIAN_GAP_S = 10.0


def _gpu_model_vram_est() -> int:
    """VRAM estimate (bytes) for one GPU copy of the ONLY engine — parakeet
    (sherpa-onnx nemo_transducer TDT v3 int8: ~0.7 GiB weights + the CUDA EP
    arena). The whisper model ladder is gone with the whisper engine."""
    return _PARAKEET_GPU_VRAM_EST


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
    """Free GPU VRAM in bytes of the selected compute GPU (0 = unknown).

    Instant nvidia-smi read of the discrete card (cached 5 s). The old
    60 s torch median blocked the worker at first plan (6 sleeps of 10 s)
    and sampled CUDA device 0, which is the wrong GPU when a virtual
    display is enumerated first. Tests patch this function directly."""
    global _vram_free_bytes, _vram_free_at
    now = time.monotonic()
    with _vram_lock:
        if _vram_free_at and now - _vram_free_at < 5.0:
            return _vram_free_bytes
    free = 0
    smi = _nvidia_smi_vram()
    if smi is not None:
        free = int(smi[1])
    if free <= 0:
        try:
            import torch

            if torch.cuda.is_available():
                free = int(torch.cuda.mem_get_info()[0])
        except Exception:
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


_HELD_VRAM_MIB = 256  # below this, WDDM compositors / our own probe are ignored


def _compute_apps_hold_gpu(stdout: str, mine: set[int]) -> bool:
    """True when a foreign process has a real CUDA allocation on this GPU."""
    for line in (stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        pid_s, mem_s = parts[0], parts[1]
        if not pid_s.isdigit() or int(pid_s) in mine:
            continue
        try:
            mem = int(mem_s.split()[0])
        except ValueError:
            continue  # [N/A] / Insufficient Permissions — not a CUDA tenant
        if mem >= _HELD_VRAM_MIB:
            return True
    return False


_gpu_held_cache = False
_gpu_held_at = 0.0
_gpu_held_lock = threading.Lock()


def _gpu_held_by_other() -> bool:
    """True when another process holds a CUDA model on this GPU.

    The live backend / another ML project / a worktree test may already hold
    a GPU model — stacking another on top risks evicting it. False when the
    probe fails (tasklist absent): the free-VRAM gate is still the primary
    guard. Cached 10 s; patched directly by tests.

    Probe: nvidia-smi compute-apps with a NUMERIC used-memory of at least
    256 MiB. Windows compute-apps lists every WDDM touch (explorer, Chrome,
    Discord, our own run.py) with memory [N/A] — those are NOT CUDA tenants.
    `tasklist /m nvcuda.dll` was worse: the app process loads nvcuda.dll
    just by probing CUDA, so the worker always saw "GPU held" and fell
    back to CPU on a box whose only real card is an RTX. Own pid / parent
    pid are ignored so our worker chain never blocks itself."""
    global _gpu_held_cache, _gpu_held_at
    now = time.monotonic()
    with _gpu_held_lock:
        if _gpu_held_at and now - _gpu_held_at < 10.0:
            return _gpu_held_cache
    held = False
    try:
        out = sp.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, creationflags=_NO_WINDOW,
        )
        if out.returncode == 0:
            held = _compute_apps_hold_gpu(out.stdout or "", {os.getpid(), os.getppid()})
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
            ["nvidia-smi", f"--id={_smi_gpu_index}",
             "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
        )
        if out.returncode == 0:
            util = float(out.stdout.strip().splitlines()[0]) / 100.0
    except Exception:
        util = None
    with _gpu_util_lock:
        _gpu_util_cache = util if util is not None else 0.0
        _gpu_util_at = now
    return util


def _gpu_thermal_guard() -> None:
    """Pace GPU decode so utilization stays <= _GPU_MAX_UTIL (0.90).

    The user's card must never sit at 100% util for long stretches —
    sustained full load heats the card and can destabilize the Windows
    driver (TDR/black-screen) while the queue keeps feeding batches. Called
    before each decode batch: when the measured utilization is above the
    ceiling, sleep in 1 s steps until it drops (bounded by
    _GPU_MAX_UTIL_WAIT_S so a foreign sustained load can't stall the queue
    forever — we degrade to the ceiling, not to zero). No-op when the
    provider isn't CUDA or utilization is unmeasurable (nvidia-smi absent:
    a fake adapter has no GPU work to throttle).
    """
    if _parakeet_provider() != "cuda":
        return
    util = _gpu_util()
    if util is None or util <= _GPU_MAX_UTIL:
        return
    waited = 0.0
    while util is not None and util > _GPU_MAX_UTIL and waited < _GPU_MAX_UTIL_WAIT_S:
        time.sleep(1.0)
        waited += 1.0
        util = _gpu_util()
    if util is not None and util > _GPU_MAX_UTIL:
        logger.warning(
            "GPU util %.0f%% still above %.0f%% after %.0fs — proceeding anyway",
            util * 100, _GPU_MAX_UTIL * 100, waited,
        )


# GPU sequential-dispatch gate (user requirement): GPU slots process ONE
# video at a time — batched decode serves one video's windows, videos
# finish in order, and DIFFERENT videos never interleave on the GPU. The
# gate is a pool-level claim on (platform, video_id) taken by the first
# GPU-pinned transcribe thread; any other GPU thread finding it held
# releases its claim (requeues with a short backoff — the claim SQL then
# skips the row until next_retry_at) instead of stacking a second video.
# CPU lanes never touch the gate — they keep claiming their own jobs in
# parallel. A live-caption session (their reservation) also blocks new GPU
# dispatch. ponytail: per-process only — cross-process serialization is
# already covered by the _gpu_held_by_other tasklist gate.
_gpu_gate_lock = threading.Lock()
_gpu_gate_video: Optional[tuple[str, str]] = None  # (platform, video_id) on the GPU now
_GPU_GATE_RECHECK_S = 15.0  # gated claim backoff — a few poll intervals


def _gpu_gate_try_acquire(platform: str, video_id: str) -> bool:
    """Take the GPU gate for (platform, video_id); False when another video
    is active on the GPU or a live-caption session holds it."""
    global _gpu_gate_video
    with _gpu_gate_lock:
        if _caption_session_held():
            return False
        if _gpu_gate_video is not None and _gpu_gate_video != (platform, video_id):
            return False
        _gpu_gate_video = (platform, video_id)
        return True


def _gpu_gate_release(platform: str, video_id: str) -> None:
    """Release the gate if this thread still holds it (only the holder)."""
    global _gpu_gate_video
    with _gpu_gate_lock:
        if _gpu_gate_video == (platform, video_id):
            _gpu_gate_video = None


def _gpu_gate_held() -> bool:
    """True while some video owns the GPU (a gate attempt would block)."""
    with _gpu_gate_lock:
        return _gpu_gate_video is not None


# Per-worker peak host-RAM estimates (system RAM, not VRAM). The real peak
# depends on model size, chunk length and ffmpeg decode buffers; the 20%
# headroom below is the safety net for estimate error.
# ponytail: estimates, not measurements — tuned for parakeet int8 on CPU and
# for host-side buffers when the model lives on VRAM. If a machine OOMs at
# budget 2, lower the env knob or bump these constants.
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


# GPU lane VRAM floor (parakeet-only): the GPU lane exists whenever the
# measured 60 s-median free-VRAM allowance is >= the parakeet CUDA
# recognizer's needs. Below the floor the pool is CPU-only. (The old
# whisper model/precision ladder — medium int8 on entry cards, fp16 on
# 8 GiB+ — is gone with the whisper engine: parakeet is one int8 model on
# every tier.)
_GPU_MIN_FREE_VRAM = int(2.0 * 1024 ** 3)


def _gpu_lane_plan() -> Optional[tuple[Optional[str], str]]:
    """(model, compute_type) for the GPU lane, or None -> CPU lane only.

    Parakeet-only: the lane is a single fixed plan ('int8' weights, CUDA
    provider); the tuple shape is kept for the pool plan slots and stats.
    Unknown allowance (0 = probe failed) -> (None, 'int8') — the legacy
    trust-the-env path keeps working."""
    allowance = _gpu_vram_allowance()
    if allowance <= 0:
        return None, "int8"
    if allowance < _GPU_MIN_FREE_VRAM:
        return None  # < 2 GiB — CPU lane only
    return None, "int8"


# --- live-caption session reservation ---------------------------------------
# The real-time captioner (services.live_captions) is the MAX-priority tenant
# while a livestream is watched: its parakeet ASR must never wait behind
# archive parakeet GPU copies (VRAM) or CPU lanes (decode threads).
# The captioner toggles this on first subscriber acquire / last release; the
# planner reads it at every re-plan (run_worker start + the 30 s plan-watch),
# so a caption session pauses the pool's GPU lane within ~_PLAN_RECHECK_S and
# caps its CPU lanes to one quiet thread. The GPU-dispatch code paths read
# caption_reserved_vram_bytes() for their free-VRAM budget.
_caption_session_lock = threading.Lock()
_caption_session_active = False


def set_caption_session_active(active: bool) -> None:
    """Declare (True) or clear (False) an active live-caption session.

    Called by services.live_captions from the SSE request path on the first
    subscriber acquire / last release. Idempotent and thread-safe."""
    global _caption_session_active
    with _caption_session_lock:
        _caption_session_active = bool(active)


def caption_session_active() -> bool:
    """True while a livestream caption session is live — the archive planner
    must yield the GPU to the captioner and keep only a quiet CPU lane."""
    with _caption_session_lock:
        return _caption_session_active


def caption_reserved_vram_bytes() -> int:
    """VRAM bytes the live captioner owns while a session is active, else 0.

    The captioner's CUDA parakeet footprint on the shared card: model
    weights (~0.7 GiB) + the CUDA EP arena, plus the standard tenant
    headroom — _PARAKEET_GPU_VRAM_EST + _GPU_VRAM_HEADROOM, evaluated at
    call time (both constants live in this module). The archive worker's
    VRAM-derived decisions (batch size, sequential GPU dispatch) subtract
    this from the measured free-VRAM allowance."""
    if not caption_session_active():
        return 0
    return _PARAKEET_GPU_VRAM_EST + _GPU_VRAM_HEADROOM


def _gpu_copies() -> int:
    """GPU model copies: VODRIP_TRANSCRIBE_GPU_COPIES (default 1) is a CEILING.

    Measured at claim time (the worker's claim gate) — NEVER static. The
    60 s-median free-VRAM allowance picks the ladder rung (fp16 -> int8 ->
    medium int8 -> CPU); below the 2 GiB floor -> 0 copies, the CPU side of
    the hybrid plan covers the queue. A GPU model held by another process
    (live backend / the user's other ML project) also forces 0 — never
    stack; the in-process live captioner's session does the same (it owns
    the card for real-time ASR while a livestream is watched). A second
    copy only when the GPU is idle-ish (<70% util) AND the
    allowance fits ~2x. Probe failure (no torch / no CUDA / nvidia-smi
    absent) degrades to trusting the env cap. 0/absent -> auto (1 copy)."""
    try:
        configured = int(os.environ.get(GPU_COPIES_ENV, "1") or "1")
    except ValueError:
        return 1
    if configured <= 0:
        configured = 1  # 0 == auto (same as absent)
    # Cached False (probed at worker boot): no +cuda wheel or no compute GPU
    # — never advertise CUDA slots that would fail jobs as ASR-unavailable.
    # None (unprobed, tests) keeps the env/VRAM path so mock plans still work.
    if _parakeet_cuda_ok is False:
        return 0
    # Held check FIRST: when another process holds a GPU model the lane is
    # forced off, so measuring free VRAM would be pure waste.
    if _gpu_held_by_other() or caption_session_active():
        return 0  # a foreign process OR the live captioner holds the GPU — don't stack
    if _gpu_lane_plan() is None:
        # Measured free VRAM < 2 GiB. When the dip is OUR OWN resident CUDA
        # recognizer (the ORT CUDA EP arena grows to ~90% of free VRAM at
        # session create), the model is already loaded and reusable — keep
        # one GPU copy so the next job waits on the sequential gate instead
        # of silently degrading to a CPU lane. Only a genuinely foreign VRAM
        # hog (checked above) or a cold process with no resident model below
        # the floor falls back to CPU.
        if _cuda_resident():
            return 1
        return 0  # no resident model — measured median free VRAM < 2 GiB, CPU lane only
    allowance = _gpu_vram_allowance()
    if configured > 1:
        util = _gpu_util()
        if util is not None and util >= _GPU_UTIL_SECOND_COPY:
            configured = 1  # GPU already busy — one copy is the ceiling
    if allowance > 0:
        configured = _clamp_cuda_copies(configured, allowance)
    return _ram_worker_clamp(configured, _GPU_COPY_RSS_EST)


def _gpu_compute_type() -> str:
    """Precision label for the GPU plan slots — always 'int8' (parakeet)."""
    lane = _gpu_lane_plan()
    return lane[1] if lane else "int8"


# System CPU load clamp: when the box is already contended (user's app
# transcoding, other agents), CPU ASR threads would only slow it down —
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

    Background (autostart) mode caps at 3 — nobody is at the keyboard, but
    the 3-lane footprint (~3 GB RSS total at the 1.5 GB/lane estimate +
    headroom) still fits the 22.5 GB-free reference box while draining the
    queue faster than 2; VODRIP_TRANSCRIBE_WORKERS raises or lowers it and
    the RAM/contention clamps still cap the actual slots."""
    if background_mode():
        return 3
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
    VODRIP_TRANSCRIBE_WORKERS or VODRIP_TRANSCRIBE_GPU_COPIES). CPU slots are
    additionally capped at the VODRIP_TRANSCRIBE_CPU_CAP thread budget (40%
    of logical threads by default), GPU slots included: the CPU side shrinks
    by the GPU slot count, so len(plan) x threads-per-slot (every recognizer
    spawns num_threads) never exceeds the CPU fraction, on any machine.

    A plan of exactly [("cuda","int8")] (gpu_slots==1 and cpu_slots==0) is
    the single-global-model path: budget 1, one recognizer. Any other plan
    -> multi-copy mode (per-thread model copies)."""
    device, _ = _effective_device()
    if device == "cpu":
        workers = _cpu_worker_ceiling() or _cpu_auto_workers()  # 0 == auto on CPU-only hosts
        slots = _ram_worker_clamp(workers, _CPU_WORKER_RSS_EST)
        slots = min(slots, _cpu_thread_budget())  # hard cap: each slot uses >= 1 thread
        if _cpu_load_high() or caption_session_active():
            slots = min(slots, 1)  # contended box / live captions — at most one quiet thread
        return [("cpu", "int8")] * slots
    gpu_slots = _gpu_copies()
    cpu_slots = _ram_worker_clamp(_cpu_worker_ceiling(), _CPU_WORKER_RSS_EST)
    # len(plan) <= budget: GPU recognizers spawn num_threads too, so the CPU
    # side must shrink below the budget by the GPU slot count (each slot
    # uses >= 1 thread; the floor-1 product then never exceeds the cap).
    cpu_slots = min(cpu_slots, max(0, _cpu_thread_budget() - gpu_slots))
    if _cpu_load_high() or caption_session_active():
        cpu_slots = min(cpu_slots, 1)
    plan: list[tuple[str, str]] = [("cpu", "int8")] * cpu_slots
    if gpu_slots:
        # Only reach the lane gate when the GPU lane is actually usable —
        # a held GPU (gpu_slots 0) must never trigger the ~60 s VRAM median.
        gpu_ct = _gpu_compute_type()  # always 'int8' for parakeet
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
    on the effective device (legacy semantics — the budget was a raw count),
    still clamped to the CPU hard cap so the 40% ceiling holds for overrides
    too (floor 1)."""
    if max_workers is None:
        return _worker_plan()
    return [_effective_device()] * min(max(1, int(max_workers)), _cpu_thread_budget())


# How often the plan-watch thread re-evaluates the pool plan while the worker
# runs (30 s sits between the 10 s held-GPU cache and the ~60 s VRAM median,
# so a GPU that frees up is noticed within ~40 s worst case and a recheck
# rarely pays the full median). The recheck runs in its OWN thread because
# _worker_plan() can block ~60 s on the first VRAM median after a transition
# (held -> free) — blocking the worker loop that long would stall heartbeats,
# refills and job monitoring. The main loop only ever does the cheap swap.
_PLAN_RECHECK_S = 30.0


def _make_pool(plan: list[tuple[str, str]], budget: int) -> ThreadPoolExecutor:
    """New transcribe executor pinned to ``plan`` (threads pin per-slot)."""
    return ThreadPoolExecutor(
        max_workers=budget, thread_name_prefix="transcribe",
        initializer=_worker_thread_init,
        initargs=(plan,),
    )


# --- model cache ----------------------------------------------------------

_model_lock = threading.Lock()


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


def close_model() -> None:
    """Unload the cached models, freeing RAM. Safe mid-transcription: workers
    hold a local reference, so the object lives until their last use.

    Also drops the cached VAD model (lazy-reloaded by the next job) and, in
    multi-copy mode, every pool thread's parakeet recognizer too (threads
    lazily reload on their next job — the registry is cleared, so a fresh
    slot is created). The process-global parakeet recognizer (single-model
    mode) is dropped as well. (The whisper model cache was removed with the
    faster-whisper engine.)"""
    global _vad, _parakeet_global, _cuda_recognizers_resident
    closed_any = False
    with _model_lock:
        for slot in _thread_slots.values():
            parakeet, slot.parakeet = slot.parakeet, None
            if parakeet is not None:
                logger.info("Unloading parakeet thread recognizer")
                del parakeet
                closed_any = True
            vad, slot.vad = slot.vad, None
            if vad is not None:
                logger.info("Unloading VAD thread model")
                del vad
                closed_any = True
        _thread_slots.clear()
        parakeet, _parakeet_global = _parakeet_global, None
        if parakeet is not None:
            logger.info("Unloading parakeet recognizer")
            del parakeet
            closed_any = True
    with _cuda_resident_lock:
        _cuda_recognizers_resident = 0  # every recognizer was dropped above
    with _vad_lock:
        vad, _vad = _vad, None
        if vad is not None:
            logger.info("Unloading VAD model")
            del vad
            closed_any = True
    if closed_any:
        gc.collect()


def _maybe_close_idle_model() -> None:
    """Close the process-global parakeet recognizer after
    VODRIP_WHISPER_IDLE_CLOSE seconds without use. Thread models die with
    the pool (close_model on worker shutdown)."""
    idle_sec = _idle_close_seconds()
    if _parakeet_global is not None and time.monotonic() - _parakeet_last_used > idle_sec:
        logger.info("Parakeet recognizer idle for %.0fs — unloading", idle_sec)
        close_model()


# --- per-thread model copies (multi-copy mode, budget > 1) ------------------
# Each pool thread owns one parakeet recognizer so inference runs truly in
# parallel. The registry is keyed by thread ident — the same per-thread
# keying CPython's threading.local uses internally. Model CREATION is
# serialized by _model_lock (shared model dir + download); inference never
# takes it. In hybrid mode _worker_thread_init pins each pool thread to its
# plan slot (GPU or CPU) at thread start, so a pinned CPU thread loads on
# CPU even though the box has a GPU.

_multi_tls = threading.local()  # per-thread: .active, .cpu_fallback, .pin


class _ThreadModelSlot:
    """One pool thread's lazy model state (parakeet recognizer + per-thread
    Silero VAD). The whisper model copy slot was removed with the engine."""
    __slots__ = ("parakeet", "vad")

    def __init__(self) -> None:
        self.parakeet: Any = None  # sherpa-onnx OfflineRecognizer (provider per slot pin)
        self.vad: Any = None       # per-thread Silero VAD (multi-copy mode only)


_thread_slots: dict[int, _ThreadModelSlot] = {}


def _in_multi_mode() -> bool:
    return bool(getattr(_multi_tls, "active", False))


def _thread_cpu_fallback() -> bool:
    return bool(getattr(_multi_tls, "cpu_fallback", False))


def _thread_mark_cpu_fallback() -> None:
    _multi_tls.cpu_fallback = True


# Plan-slot index handed to each new pool thread. run_worker resets this
# before every _make_pool() so a rebuilt pool realigns thread 0 -> plan[0]
# (a desynced seq pinned new-pool threads to the wrong slots — the 2nd real
# cause of 'twitch on CPU' with the GPU idle); the % len(plan) modulo in
# _worker_thread_init still guards direct _make_pool callers/tests that
# create two pools without run_worker.
_pool_thread_seq = count()


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


# --- Parakeet lane (sherpa-onnx) ------------------------------------------
    # A/B verdict (2026-08-07, 60 s pt-BR segments, i5-13600K): parakeet TDT v3
# int8 on CPU runs 2.5-5.2 RTFx vs whisper-large-v3-turbo cpu/int8 at
# 0.26-0.6 (7-15x), ~0.7 GB less peak RSS, and outputs nothing on silence
# (no hallucination). GPU: CUDA-enabled sherpa-onnx wheels exist since
# 1.13.x (sherpa-onnx==X+cuda12.cudnn9 — see requirements.txt); when one is
# importable, GPU slots run parakeet with provider='cuda', gated on the
# measured free-VRAM allowance.
PARAKEET_MODEL = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
_PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
_PARAAKEET_FEATURE_DIM = 128  # nemo_transducer default (80) fails — must match the model
# Model card: 26 European languages. Intersected at runtime with the lang
# tokens the model's tokens.txt actually carries (see _parakeet_langs), so a
# model/cache swap missing a language is a clean unsupported-language
# failure for that job (there is no whisper fallback).
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

# CUDA recognizers currently resident in THIS process (not a foreign tenant).
# The ORT CUDA EP arena grows to ~90% of free VRAM at session create, so a
# resident CUDA recognizer makes the measured free-VRAM allowance read below
# the GPU floor even though the model is already loaded and reusable. The
# planner and the GPU-slot gate must not exile the lane for OUR OWN arena —
# only a genuinely foreign VRAM hog (or no resident model below the floor)
# degrades the queue to CPU. Guarded by a DEDICATED lock: _load_parakeet
# runs inside _model_lock (non-reentrant), so the counter cannot touch it.
_cuda_recognizers_resident = 0
_cuda_resident_lock = threading.Lock()


def _cuda_resident() -> bool:
    """True when THIS process holds a CUDA parakeet recognizer.

    Set by _load_parakeet on a successful CUDA load, cleared by close_model
    which drops every recognizer. Lock is dedicated (see above)."""
    with _cuda_resident_lock:
        return _cuda_recognizers_resident > 0


def _parakeet_available() -> bool:
    """True when the parakeet lane can run.

    VODRIP_PARAAKEET=0 is a hard kill switch (no import probe). Otherwise
    the sherpa-onnx import is probed once per process and cached; an import
    failure makes the lane fail jobs cleanly (no whisper fallback)."""
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
    """True when the installed sherpa-onnx is a CUDA build AND a real GPU
    with compute is present.

    The +cuda wheels (see requirements.txt) bundle onnxruntime's CUDA EP and
    version as ``X.Y.Z+cuda<cuda-ver>.cudnn9``; the plain CPU wheels carry no
    tag. The sherpa-onnx Python bindings expose no provider-enumeration API,
    so the build tag is the cheap static probe and ``_load_parakeet(
    provider='cuda')`` is the AUTHORITATIVE runtime probe — it raises unless
    the CUDA EP actually initializes, then degrades to CPU and flips this
    flag (ponytail: if k2-fsa ever changes the tag scheme, the construction
    fallback still keeps behavior correct). The tag alone is NOT enough: a
    fake adapter (Virtual Display Driver — no CUDA device, no nvidia-smi)
    must never route GPU work, so the wheel tag is ANDed with the real-GPU
    compute probe. Probed once per process and cached; a probe failure means
    GPU slots fail jobs cleanly (_AsrLaneUnavailable). Tests/self-check pin
    ``_parakeet_cuda_ok`` directly so they never import sherpa-onnx."""
    if not _parakeet_available():
        return False
    global _parakeet_cuda_ok
    if _parakeet_cuda_ok is None:
        try:
            import sherpa_onnx

            _parakeet_cuda_ok = bool(
                "+cuda" in (getattr(sherpa_onnx, "__version__", "") or "")
            ) and _real_gpu_info()[0]
        except Exception:
            _parakeet_cuda_ok = False
    return _parakeet_cuda_ok


def _real_cuda_works() -> bool:
    """True when torch says CUDA is actually usable (driver + compute device).

    A Virtual Display Driver / broken driver has no CUDA compute, so
    torch.cuda.is_available() is False even when the +cuda wheel is
    installed — this is the discriminator that keeps onnxruntime's CUDA EP
    (and its crash-prone in-process CPU fallback) away from such boxes."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


_offpool_cuda_ok: Optional[bool] = None  # real-CUDA probe for off-pool callers (None = unprobed)


def _offpool_cuda_available() -> bool:
    """True when OFF-POOL callers (the live captioner) may load the CUDA EP.

    The +cuda wheel tag (_parakeet_cuda_available) is not enough: on a box
    whose "GPU" is a Virtual Display Driver / broken driver, onnxruntime's
    CUDA EP append raises at session-create AND the in-process CPU fallback
    access-violates the whole process (reproduced: the captioner's CUDA
    attempt crashed the API listener). A REAL NVIDIA GPU with a working CUDA
    runtime is the gate — torch CUDA up AND the vendor probe. VODRIP_CAPTION_CUDA=0
    is a hard kill switch. Probed once per process and cached; failure ->
    CPU, the legacy safe captioner path. Runs in the captioner's WORKER
    thread (never the API request path). Tests patch the cached flag or the
    helper probes directly so they never import torch."""
    if os.environ.get("VODRIP_CAPTION_CUDA", "1").strip() == "0":
        return False
    global _offpool_cuda_ok
    if _offpool_cuda_ok is None:
        ok = False
        try:
            from services.gpu_detect import detect_gpu_vendor  # local import: the GPU-batch merge renames archive_transcribe's own alias

            ok = bool(
                _parakeet_cuda_available()
                and _real_cuda_works()
                and detect_gpu_vendor() == "nvidia"
            )
        except Exception:
            ok = False
        _offpool_cuda_ok = ok
    return _offpool_cuda_ok


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
    time. Unknown allowance trusting the provider probe mirrors the
    probe-failure paths elsewhere (env cap trusted)."""
    if not _parakeet_cuda_available():
        return False
    if _cuda_resident():
        # Our own CUDA recognizer is already loaded — the floor guards the
        # LOAD, not the reuse: the next job rides the resident model (the
        # sequential gate serializes) and the batch sizes down to whatever
        # VRAM remains. Free VRAM below 2 GiB here is our own ORT arena.
        return True
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


# GPU batched decode (user requirement): decode_streams(streams) decodes a
# BATCH of the SAME video's speech windows in one call — the 16 GiB card is
# the point, videos finish sequentially. The batch is sized from the MEASURED
# free VRAM (fresh cache read) minus the caption-session reservation and a
# fixed safety margin, clamped [1, _PARAAKEET_BATCH_MAX]; unknown free VRAM
# degrades to sequential decode (never gamble a batch we cannot size).
PARAKEET_BATCH_ENV = "VODRIP_PARAAKEET_BATCH"  # optional cap (0/absent = VRAM-derived)
_PARAAKEET_BATCH_MAX = 32  # clamp ceiling — never more windows per call
_PARAAKEET_WINDOW_VRAM_EST = int(64 * 1024 ** 2)  # per 30 s window: features + activations (generous)
# Free VRAM never committed to the batch: 2 GiB headroom (raised from 512 MiB
# after the 2026-08-15 BFCArena AllocateRawInternal crash allocating 2.5 GiB —
# a spike on top of the old margin crossed the card's ceiling and killed the
# process; the halved-batch retry in _transcribe_batch_parakeet is the
# graceful second line of defense).
_PARAAKEET_BATCH_VRAM_SAFETY = int(2 * 1024 ** 3)


def _caption_session_held() -> bool:
    """True when a live-caption session is active.

    Seam to the caption-priority work (WorkerCaptionPriority2): their
    ``caption_session_active()`` lands in this module; while it returns True
    the GPU sequential gate refuses new GPU dispatch. Absent (their changes
    not merged) -> False. Named _caption_session_held (NOT _caption_session_active)
    because that name is the reservation flag itself (bool, set by
    set_caption_session_active) — a def here would shadow it."""
    try:
        return bool(globals().get("caption_session_active", lambda: False)())
    except Exception:
        return False


def _caption_reserved_vram_bytes() -> int:
    """GPU bytes the live-caption session reserves (0 when idle).

    Seam to the caption-priority work (WorkerCaptionPriority2): their
    ``caption_reserved_vram_bytes()`` lands in this module; while active it
    returns the caption recognizer's footprint, which every VRAM read here
    subtracts so the archive batch never crowds the caption session.
    Absent -> 0 — the batch sizes purely on measured free VRAM."""
    try:
        return int(globals().get("caption_reserved_vram_bytes", lambda: 0)() or 0)
    except Exception:
        return 0


def _parakeet_batch_size() -> int:
    """Windows per decode_streams call on GPU slots, sized from free VRAM.

    budget = fresh-cache free VRAM - caption reservation - parakeet model/
    arena estimate - safety margin; batch = clamp(budget // per-window est,
    1, _PARAAKEET_BATCH_MAX). CPU provider -> 1 (the A/B-measured sequential
    sweet spot — byte-identical to the pre-batch path). Unknown free VRAM
    (0) -> 1. VODRIP_PARAAKEET_BATCH caps the result (0/absent = VRAM-
    derived)."""
    if _parakeet_provider() != "cuda":
        return 1
    now = time.monotonic()
    with _vram_lock:
        fresh = (
            _vram_free_at
            and now - _vram_free_at < _GPU_VRAM_MEDIAN_GAP_S * _GPU_VRAM_MEDIAN_SAMPLES
        )
        free = _vram_free_bytes if fresh else 0
    if free <= 0:
        return 1
    budget = (
        free
        - _caption_reserved_vram_bytes()
        - _PARAKEET_GPU_VRAM_EST
        - _PARAAKEET_BATCH_VRAM_SAFETY
    )
    if budget <= 0:
        return 1
    try:
        cap = int(os.environ.get(PARAKEET_BATCH_ENV, "0") or "0")
    except ValueError:
        cap = 0
    batch = budget // _PARAAKEET_WINDOW_VRAM_EST
    if cap > 0:
        batch = min(batch, cap)
    return max(1, min(batch, _PARAAKEET_BATCH_MAX))


def _parakeet_cache_dir() -> Path:
    """Sherpa model cache: VODRIP_SHERRPA_CACHE override, else a subdir of
    the AI-models folder (<models root>/parakeet-models) so every model
    weight lives under the models folder. Falls back to the legacy drive-root
    sibling (<models parent>/parakeet-models — the pre-ownership-fix layout)
    while it still holds the model (see _migrated_model_dir)."""
    override = os.environ.get(PARAKEET_CACHE_ENV, "").strip()
    if override:
        return Path(override)
    from services.disk_hygiene import _migrated_model_dir

    base = _cache_dir()
    return _migrated_model_dir(
        base / "parakeet-models", base.parent / "parakeet-models", "parakeet"
    )


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
    swapped model missing a language is a clean unsupported-language failure
    for that job — there is no whisper fallback). When the model isn't
    downloaded yet the candidate set is trusted — routing is correct either
    way (the guard only ever narrows)."""
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
    """sherpa-onnx decode threads per recognizer: the FULL pool slot count
    (GPU + CPU — every recognizer spawns num_threads, so the 40% cap must
    count all lanes), the box's cores divided by that, capped at the
    A/B-measured 8-thread sweet spot AND the hard thread budget. Called from
    _load_parakeet under _model_lock; _worker_plan() takes only leaf locks
    (_vram_lock/_cpu_load_lock — never _model_lock), so no lock cycle.
    Machine-aware: a 20-thread box with a 4-slot plan (1 GPU + 3 CPU) gets
    2 -> 8 ASR threads total = 40% (was 6 CPU-only -> 18/20, 90%)."""
    lanes = len(_worker_plan())  # every slot's recognizer spawns num_threads threads
    cores = os.cpu_count() or 4
    return max(1, min(_PARAAKEET_MAX_THREADS, cores // lanes, _cpu_thread_budget() // lanes))


class _AsrRoutingError(Exception):
    """Base: a transcribe job cannot run on the only ASR engine (parakeet).

    Both subclasses are TERMINAL job failures: _process_job catches them,
    logs one warning line (no traceback) and lands the job 'failed' with the
    message (which carries the 'ASR unsupported' / 'ASR unavailable' marker
    archive_db.update_job treats as terminal — no backoff requeue)."""


class _AsrUnsupportedLanguage(_AsrRoutingError):
    """A language that is KNOWN (explicitly set) but outside parakeet's 26
    European-language coverage (ja/ko/zh/ar and anything else). No whisper
    fallback exists — this is the required clean failure."""


class _AsrLaneUnavailable(_AsrRoutingError):
    """The calling lane has no usable parakeet: sherpa-onnx not importable,
    VODRIP_PARAAKEET=0, no CUDA wheel on a GPU slot, or VRAM too tight."""


def _slot_engine(device: str) -> str:
    """Engine for a plan slot — always 'parakeet' (the only engine), raised
    as _AsrLaneUnavailable when the slot cannot run it: CUDA slots need a
    CUDA-enabled sherpa-onnx, CPU slots the plain import; VODRIP_PARAAKEET=0
    kills both."""
    if device == "cuda":
        if _parakeet_cuda_available():
            return "parakeet"
        raise _AsrLaneUnavailable(
            "ASR unavailable on the GPU slot: no CUDA-enabled sherpa-onnx "
            "(install requirements-gpu.txt or let the GPU auto-install run) — "
            "parakeet is the only ASR engine"
        )
    if _parakeet_available():
        return "parakeet"
    raise _AsrLaneUnavailable(
        "ASR unavailable on the CPU slot: the parakeet engine (sherpa-onnx) "
        "is not importable"
        + (" (VODRIP_PARAAKEET=0 kills the lane)" if os.environ.get(PARAKEET_ENV, "1").strip() == "0" else "")
    )


# The job id currently running on this thread — set by _process_job so the
# long silent phases (ffmpeg HLS fetch, yt-dlp download) can refresh the
# job row's heartbeat from inside (P1-2: a throttled fetch must never let
# the stale-reclaim window fire mid-download and hand the job to a second
# lane — both would download + transcribe the same VOD).
_job_id_tls = threading.local()


def _current_job_id() -> Optional[str]:
    """The job id being processed on this thread, or None."""
    return getattr(_job_id_tls, "job_id", None)


def _job_engine(language: Optional[str]) -> str:
    """Engine for THIS job on the calling lane — always 'parakeet' or raise.

    Parakeet (sherpa-onnx nemo_transducer) is the ONLY ASR engine. It covers
    the 26 European languages in PARAKEET_LANG_CANDIDATES (intersected with
    the model's actual tokens) plus unknown/auto-detect (None / '').

    Failures are clean, never a whisper fallback:
      * _AsrUnsupportedLanguage — a KNOWN language outside the covered set
        (e.g. ja/ko/zh/ar). Raised AFTER the lane checks: when parakeet is
        unavailable _parakeet_langs() is empty and the job is an
        engine-unavailable failure, not a language-coverage one.
      * _AsrLaneUnavailable — the lane cannot run parakeet at all (no
        sherpa-onnx import / VODRIP_PARAAKEET=0 / no CUDA wheel on a GPU
        slot / free VRAM below the parakeet floor).
    Pure: no settings read here (the module self-check runs at import;
    reading settings then could touch real appdata)."""
    device, _ = _thread_pin() or _effective_device()
    if device == "cuda":
        if not _parakeet_cuda_available():
            raise _AsrLaneUnavailable(
                "ASR unavailable on the GPU slot: no CUDA-enabled sherpa-onnx "
                "(install requirements-gpu.txt or let the GPU auto-install "
                "run) — parakeet is the only ASR engine"
            )
        if not _parakeet_gpu_allowed():
            raise _AsrLaneUnavailable(
                "ASR unavailable on the GPU slot: free VRAM is below the "
                "parakeet recognizer floor (~2 GiB) — close other GPU apps "
                "or run the worker CPU-only"
            )
    elif not _parakeet_available():
        raise _AsrLaneUnavailable(
            "ASR unavailable: the parakeet engine (sherpa-onnx) is not "
            "importable"
            + (" (VODRIP_PARAAKEET=0 kills the lane)" if os.environ.get(PARAKEET_ENV, "1").strip() == "0" else "")
        )
    if language and language not in _parakeet_langs():
        covered = ", ".join(sorted(PARAKEET_LANG_CANDIDATES))
        raise _AsrUnsupportedLanguage(
            f"ASR unsupported: language {language!r} is not covered by the "
            f"parakeet engine (26 European languages: {covered})"
        )
    return "parakeet"


def _asr_model_name() -> str:
    """The model id reported/written for a run — always the parakeet repo id
    (the only ASR engine; the whisper-model resolver is gone)."""
    return PARAKEET_MODEL


def _parakeet_provider() -> str:
    """'cuda' on a CUDA-pinned pool slot with CUDA sherpa present, else 'cpu'.

    Mirrors the pool slot's device pin. Off-pool callers (the live
    captions worker, tests, any direct call) get the CUDA EP ONLY on a
    verified real NVIDIA GPU (_offpool_cuda_available — torch CUDA up AND
    vendor nvidia): on a box where CUDA cannot actually load (no real GPU —
    e.g. a Virtual Display Driver), the EP append raises AND the in-process
    CPU fallback access-violates the process (reproduced: the captioner's
    CUDA attempt crashed the whole API listener). The probe keeps the
    real-time captioner on CPU on such boxes while letting it use the card
    on a real GPU (the RTX 5080 class the pool already targets). Pool
    threads keep their pinned slot device."""
    pin = _thread_pin()
    if pin is None:
        return "cuda" if _offpool_cuda_available() else "cpu"
    device, _ = pin
    if device == "cuda" and _parakeet_cuda_available():
        return "cuda"
    return "cpu"


def _load_parakeet(provider: str = "cpu") -> Any:
    """Build one sherpa-onnx OfflineRecognizer (nemo_transducer).

    provider='cuda' on GPU slots with a CUDA-enabled sherpa-onnx (the +cuda
    wheels bundle a CUDA onnxruntime); 'cpu' everywhere else. A CUDA load
    failure degrades THIS recognizer to CPU and flips the cached probe, so
    later jobs route per reality instead of failing."""
    global _cuda_recognizers_resident
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
        if provider != "cpu":
            with _cuda_resident_lock:
                _cuda_recognizers_resident += 1
    except Exception as exc:
        if provider == "cpu":
            raise
        global _parakeet_cuda_ok
        _parakeet_cuda_ok = False
        kwargs.pop("provider", None)
        logger.warning(
            "parakeet CUDA recognizer failed to load (%s) — falling back to CPU "
            "(GPU slots stay CPU for the rest of this process)", exc,
        )
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
        provider = "cpu"
    logger.info(
        "Parakeet recognizer loaded in %.1fs (provider=%s threads=%d, cache=%s)",
        time.monotonic() - t0, provider, _parakeet_threads(), _parakeet_cache_dir(),
    )
    return rec


def _parakeet_model() -> Any:
    """The ONLY engine's recognizer for the current context: the calling
    thread's own copy in multi-copy mode, else the process-global one.
    Creation is serialized by _model_lock (shared model dir + download);
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


def _parakeet_words(
    tokens: list[str],
    timestamps: list[float],
    log_probs: Optional[list[float]] = None,
) -> list[dict]:
    """Word-level items from the recognizer's per-token timestamps.

    The HF-converted vocab marks word-initial pieces with a leading space
    (the source SentencePiece ▁); the lone-space piece is a space INSIDE a
    word ('de 10'), so a new word starts only on a piece that begins with a
    space and is longer than one char. Word end = the last token's start
    time (monotonic; matches whisper's word shape). Validated on real audio:
    concatenating the pieces reconstructs result.text exactly.

    log_probs (sherpa-onnx ``ys_log_probs``, parallel to tokens; absent on
    old builds) aggregates per word: word_conf = exp(mean(token log-probs)).
    The sign convention is sanity-checked here — sherpa may emit costs
    (positive) instead of log-probs (negative); positive values are inverted
    so the confidence stays in (0, 1]. No conf key when log_probs is absent,
    which silently disables the transcript-fix weak path on old sherpa."""
    def finalize(word: str, start: Optional[float], end: Optional[float],
                 lps: list[float]) -> dict:
        item = {
            "word": word,
            "start": round(start or 0.0, 3),
            "end": round(end or 0.0, 3),
        }
        if lps:
            mean = sum(lps) / len(lps)
            if mean > 0:
                mean = -mean  # costs (positive) -> log-probs (sign sanity)
            item["conf"] = math.exp(mean)
        return item

    words: list[dict] = []
    cur = ""
    start: Optional[float] = None
    end: Optional[float] = None
    cur_lps: list[float] = []
    for ti, (tok, ts) in enumerate(zip(tokens, timestamps)):
        word_start = tok.startswith(" ") and len(tok) > 1
        if word_start and cur:
            words.append(finalize(cur, start, end, cur_lps))
            cur, start, cur_lps = tok.lstrip(" "), ts, []
        elif word_start:
            cur, start, cur_lps = tok.lstrip(" "), ts, []
        elif not cur:
            cur, start, cur_lps = tok, ts, []
        else:
            cur += tok
        end = ts
        if log_probs is not None and ti < len(log_probs):
            cur_lps.append(log_probs[ti])
    if cur:
        words.append(finalize(cur, start, end, cur_lps))
    return words


def _transcribe_batch_parakeet(
    rec: Any,
    audio: "Any",
    chunks: list[tuple[float, float]],
    language: Optional[str],
    *,
    clip_offsets: Optional[list[float]] = None,
    batch_size: int = 1,
) -> list[tuple[list[dict], Optional[str]]]:
    """Decode [start,end] clips with the sherpa-onnx parakeet recognizer.

    One OfflineStream per clip. batch_size > 1 (GPU slots) decodes the clips
    in batches via ``decode_streams(streams)`` — a single call over the
    batch, sized from free VRAM (see _parakeet_batch_size) — so the 16 GiB
    card is kept busy on ONE video's windows instead of one stream per call.
    The recognizer's per-stream results map 1:1 to the input stream order,
    so the returned list is in the same order as ``chunks`` and the
    per-clip insert/manifest/resume contract is unchanged. batch_size 1 (the
    CPU default) keeps the legacy sequential decode_stream loop byte-identical.

    Segments carry absolute video timestamps and the same JSON shape the
    whisper batch decoder produced (one segment per clip with word-level
    timestamps — the shape is the transcript contract, not an engine
    detail). An empty transcript (silence -> no words — the parakeet
    no-hallucination behavior) yields no segment for that clip.
    ponytail: whisper split segments on its own sentence boundaries; here
    one VAD chunk is one segment. Upgrade path: split a segment at word
    gaps > 1 s if the UI ever needs finer granularity."""
    global _parakeet_last_used
    # The idle-closer keys off this timestamp — but the decode loop holds
    # the recognizer it already loaded and never re-enters _parakeet_model,
    # so a long job (> idle timeout) looked "idle" and got unloaded mid-run
    # (plan collapse to CPU + crashed process). Any inference IS use.
    _parakeet_last_used = time.monotonic()
    def _clip_items(stream: Any, cs: float, ce: float, base: float) -> list[dict]:
        res = stream.result
        text = (res.text or "").strip()
        if not text:
            return []
        words = _parakeet_words(
            getattr(res, "tokens", []) or [],
            getattr(res, "timestamps", []) or [],
            getattr(res, "ys_log_probs", None),
        )
        # The recognizer only ever sees the per-clip slice, so its word
        # timestamps are relative to the CLIP, not the video. The absolute
        # clip start is cs+base (sharded: concat-relative cs + absolute
        # offset; full-audio: cs is already absolute, base is 0). Without
        # this offset every clip past the first stored end_sec = the first
        # clip's speech end and clip-relative word times.
        clip_start = cs + base
        last_word_end = (words[-1]["end"] if words else float(ce - cs)) + clip_start
        return [{
            "start_sec": round(clip_start, 3),
            "end_sec": round(min(ce + base, last_word_end + 0.3), 3),
            "text": text,
            "words": [
                {**w, "start": round(w["start"] + clip_start, 3),
                 "end": round(w["end"] + clip_start, 3)}
                for w in words
            ],
        }]

    out: list[tuple[list[dict], Optional[str]]] = []
    if batch_size > 1:
        # Batched GPU path: decode_streams over sub-batches; results are
        # read per stream IN INPUT ORDER, so timestamps stay monotonic. A
        # VRAM allocation failure (BFCArena AllocateRawInternal / CUDA OOM —
        # observed 2026-08-15) halves the sub-batch instead of killing the
        # process: results stay 1:1 with input chunks because the left
        # half's output precedes the right half's.
        _alloc_markers = (
            "Failed to allocate", "out of memory", "OOM", "CudaError",
            "cudaErrorMemoryAllocation", "AllocateRaw",
        )

        def _decode_streams_safe(sub: list[tuple[float, float]], offset: int) -> list[tuple[list[dict], Optional[str]]]:
            """Decode one sub-batch (create streams, decode, read results,
            extract text), retrying in halves on allocation/OOM errors. A
            single chunk that still fails re-raises (the job fails cleanly)."""
            streams = []
            for (cs, ce) in sub:
                s0, s1 = int(cs * SAMPLE_RATE), int(ce * SAMPLE_RATE)
                stream = rec.create_stream()
                stream.accept_waveform(SAMPLE_RATE, audio[s0:s1])
                streams.append(stream)
            try:
                rec.decode_streams(streams)
                items = []
                for j, (cs, ce) in enumerate(sub):
                    base = 0.0 if clip_offsets is None else clip_offsets[offset + j]
                    items.append((_clip_items(streams[j], cs, ce, base), language))
                return items
            except Exception as exc:
                if len(sub) <= 1 or not any(m in str(exc) for m in _alloc_markers):
                    raise
                logger.warning(
                    "parakeet GPU batch decode OOM at batch %d (%d windows) — retrying in halves",
                    batch_size, len(sub),
                )
                # Release the failed batch's streams (features/audio pin VRAM)
                # BEFORE the halved retries allocate fresh ones.
                del streams, stream
                mid = len(sub) // 2
                return _decode_streams_safe(sub[:mid], offset) + _decode_streams_safe(sub[mid:], offset + mid)

        for i in range(0, len(chunks), batch_size):
            sub = chunks[i:i + batch_size]
            out.extend(_decode_streams_safe(sub, i))
        return out
    for i, (cs, ce) in enumerate(chunks):
        base = 0.0 if clip_offsets is None else clip_offsets[i]
        s0, s1 = int(cs * SAMPLE_RATE), int(ce * SAMPLE_RATE)
        clip = audio[s0:s1]
        stream = rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, clip)
        rec.decode_stream(stream)
        out.append((_clip_items(stream, cs, ce, base), language))
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
# VAD / the ASR engine / events. Peak RAM is bounded by a shard window, never
# by the media length (a 13.5 h VOD = ~3.1 GB decoded — not resident at once).

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
    """Fixed-duration int16 PCM shards on disk plus absolute range reads.

    Shard i covers samples [_shard_sample_bounds(i)); read() returns any
    absolute [start, end) window as one array, so VAD / the ASR engine /
    events consume shards without ever holding more than one window in RAM."""

    __slots__ = ("files", "shard_sec", "total_samples", "total_sec")

    def __init__(self, files: list, shard_sec: float) -> None:
        self.files = list(files)
        self.shard_sec = float(shard_sec)
        self.total_samples = sum(Path(f).stat().st_size // 2 for f in self.files)
        self.total_sec = self.total_samples / SAMPLE_RATE

    def read(self, start_sec: float, end_sec: float) -> Any:
        """Concatenated float32 16 kHz samples for an absolute window.

        Shards are stored as int16 PCM (half the disk of float32, no speech
        precision loss); reads convert to float32 in [-1, 1] for the
        consumers (VAD / ASR / events)."""
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
            parts.append(
                np.fromfile(fpath, dtype=np.int16, count=hi - lo, offset=lo * 2)
                .astype(np.float32)
                / 32768.0
            )
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def _decode_to_shards(
    path: str,
    ffmpeg_bin: Optional[str] = None,
    shard_sec: Optional[float] = None,
    out_dir: Optional[str] = None,
) -> Iterator[tuple[float, Any]]:
    """Decode ONCE to fixed-duration int16 PCM shard files; yield (start_sec, np.ndarray).

    One ffmpeg process pipes mono 16 kHz s16 (int16 — half the disk of
    float32, no speech precision loss) to stdout; stdout is sliced into
    shard-sized buffers and each spilled to ``<out_dir>/shard_%06d.i16``.
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
    shard_bytes = int(shard_sec * SAMPLE_RATE) * np.dtype(np.int16).itemsize
    cmd = [
        ffmpeg_bin, "-nostdin", "-v", "error", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
    ]
    proc: Optional[sp.Popen] = None
    try:
        proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, creationflags=_NO_WINDOW)
        idx = 0
        while True:
            raw = proc.stdout.read(shard_bytes)
            if not raw:
                break
            fpath = tmpdir / f"shard_{idx:06d}.i16"
            fpath.write_bytes(raw)
            # int16 on disk, float32 [-1, 1] to consumers (astype makes a
            # fresh writable array — torch.from_numpy safe).
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
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

# _vad_lock serializes ONLY the lazy VAD load. Inference never holds it:
# multi-copy lanes each own a per-thread Silero instance (stateful, not
# thread-safe — see _ThreadModelSlot.vad / _get_vad) and the off-pool
# callers that share the global instance (live captions) re-serialize in
# vad_speech_seconds. Silero is ~2 MB per copy, so per-lane duplicates are
# cheap vs the 3-lane throughput they unlock.
_vad_lock = threading.Lock()
_vad: Any = None

# torch intra-op threads per VAD-carrying lane: 3 lanes x torch's default
# 20 threads would oversubscribe the box; 4/lane is the A/B-sane ceiling
# (VAD is CPU-bound and mostly sequential anyway — the batched pass is
# ~170x realtime, far below any ASR decode).
_VAD_TORCH_THREADS = 4

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


def _load_vad() -> Any:
    """Build one Silero VAD model (torch .jit by default, ONNX opt-in).

    Multi-lane workers pin torch intra-op threads to _VAD_TORCH_THREADS so
    N lanes don't each grab all the box's cores (torch.set_num_threads is a
    process-global knob — pinning it from the first loading lane covers the
    others). Single-lane callers keep the torch default.
    """
    if os.environ.get("VODRIP_VAD_ONNX", "").strip() == "1":
        return _load_onnx_vad()
    from silero_vad import load_silero_vad

    if _in_multi_mode():
        import torch

        torch.set_num_threads(_VAD_TORCH_THREADS)
    return load_silero_vad(onnx=False)


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

    Multi-copy mode (budget > 1) gives each pool thread its OWN instance —
    the model is STATEFUL and not thread-safe, and a single shared copy
    would serialize every lane's VAD pre-pass (plus live captions). The
    per-thread copy lives in the thread's model slot (cleared by
    close_model) and _vad_lock guards only the lazy load. Off-pool callers
    (live captions, tests, direct transcribe_video calls) share the
    process-global instance exactly as before.
    """
    if _in_multi_mode():
        slot = _thread_slot()
        if slot.vad is None:
            with _vad_lock:
                if slot.vad is None:
                    slot.vad = _load_vad()
        return slot.vad
    global _vad
    with _vad_lock:
        if _vad is None:
            _vad = _load_vad()
        return _vad

class _OnnxVad:
    """Minimal stateful wrapper over the bundled silero ONNX session.

    Mirrors silero's OnnxWrapper (LSTM state + 64-sample context carried
    across per-window calls) but numpy-only — no torch conversion or .item()
    in the loop. Not thread-safe: off-pool callers that share the global
    instance re-serialize in vad_speech_seconds; lanes never share."""

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
    # The model is STATEFUL and not thread-safe: two workers sharing one
    # instance corrupt each other's state (select() out-of-range crash,
    # reproduced with 2 concurrent jobs). Multi-copy lanes now each own a
    # per-thread instance (_get_vad) so VAD runs in parallel — no lock on
    # the lane path. Only the off-pool callers that share the global
    # instance (live captions, direct calls) re-serialize here.
    if _in_multi_mode():
        if isinstance(vad, _OnnxVad):
            probs = _vad_probs_onnx(audio, vad)
        else:
            probs = _vad_probs_torch(audio, vad)
    else:
        with _vad_lock:
            if isinstance(vad, _OnnxVad):
                probs = _vad_probs_onnx(audio, vad)
            else:
                probs = _vad_probs_torch(audio, vad)
    return _vad_regions(probs, len(audio))


_MAX_CHUNK_SEC = 30.0  # chunking contract — resume granularity + batch window


def _plan_chunks(
    speech: list[tuple[float, float]],
    merge_gap: float = 0.8,
    min_len: float = 0.25,
) -> list[tuple[float, float]]:
    """Merge nearby speech regions into transcribe chunks; drop sub-minimum ones.

    Chunks are capped at _MAX_CHUNK_SEC (30 s): it was faster-whisper's mel
    window (an uncapped run silently transcribed only the first 30 s), and
    it is kept as the chunking contract — GPU batches decode 30 s windows
    and resume granularity stays fine.

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
    path: Path, chunks: list[tuple[float, float]], engine: str = "parakeet"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "chunks": chunks,
            "model": _asr_model_name(),
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
    engine: str = "parakeet",
) -> tuple[list[int], int]:
    """Return (chunk indices to transcribe, next free seg_idx).

    Entries are trusted only when the header matches the current plan, model
    AND engine (an engine/model change invalidates the manifest; pre-engine
    manifests carry no 'engine' key and read as 'parakeet' — the only
    engine). A chunk is also re-transcribed when any row in its recorded
    seg_idx range is missing (manual delete / partial write), and the next
    free index is the LOWEST gap in existing — so deleted rows are restored
    at their old index and the seg_idx sequence stays contiguous."""
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
        or header.get("engine", "parakeet") != engine
        or header.get("model") != _asr_model_name()
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



def _transcribe_youtube_captionless(
    video_id: str,
    *,
    language: Optional[str] = None,
    progress_cb: Optional[Callable[[float, float, int, int], None]] = None,
    events_cb: Optional[Callable[..., Optional[dict]]] = None,
    audio_stash: Optional[dict] = None,
) -> dict:
    """ASR for a captionless YouTube video (archived metadata-only, no
    local archive_path).

    Downloads bestaudio at transcribe time via the app's yt-dlp session
    (cookies + po_token — this machine sits behind the YouTube bot gate,
    so the download must respect the gate), decodes, VADs and transcribes,
    then deletes the temp audio. Music/no-speech is decided inside
    _transcribe_audio_source (speech fraction below VODRIP_MUSIC_SPEECH_FRAC
    -> transcript_kind='music', done, no ASR).

    *audio_stash* is the TASK9 retry cache: a dict the caller passes to
    EVERY engine attempt. The first call downloads the audio, stashes the
    wav path + its owning temp dir, and keeps the dir alive; the retry
    call (parakeet -> parakeet, same engine) finds the stash and REUSES
    the download — no 350 MB re-fetch for an engine retry. The retry call
    (the last consumer) removes the stashed dir. A direct call with no
    stash keeps the old create-and-clean behavior.

    Download failures:
      - bot-gate classified -> _YoutubeGateRequeue (caller requeues the
        job, never fails — no retry storm),
      - permanent (DRM / age-gated / deleted / private / geo-blocked) ->
        videos.transcript_kind='blocked' (terminal — the scheduler never
        re-enqueues) and a yt_dlp DownloadError with the real reason
        (update_job treats DownloadError as terminal: no auto-retry),
      - _YtDownloadTimedOut (wall-clock cap) / anything else propagates to
        the normal job retry machinery (transient -> requeue with backoff)."""
    t0 = time.monotonic()
    cached = (audio_stash or {}).get("wav")
    outdir = None
    try:
        if cached is not None:
            path = str(cached)
        else:
            from services.archive_ytdlp import (
                _is_gate_error,
                _is_permanent_download_error,
                download_bestaudio,
            )

            def _dl_progress(_d: dict) -> None:
                # P1-2: yt-dlp bytes flowing (or not) — refresh the job row
                # so the stale-reclaim window never fires mid-download.
                job_id = _current_job_id()
                if job_id is not None:
                    try:
                        archive_db.update_job(job_id)
                    except Exception:
                        logger.debug(
                            "yt-dlp heartbeat failed for %s", job_id, exc_info=True
                        )

            try:
                path = download_bestaudio(
                    video_id, outdir := Path(tempfile.mkdtemp(prefix=f"vodrip-transcribe-youtube-{video_id}-")),
                    progress_hook=_dl_progress,
                )
            except Exception as exc:
                # Permanent FIRST: an age-gate error ("Sign in to confirm
                # your age") contains the IP-gate marker "sign in to
                # confirm" — a per-video verdict must not be misread as the
                # IP-level freeze.
                if _is_permanent_download_error(exc):
                    archive_db.mark_video_transcript_kind("youtube", video_id, "blocked")
                    logger.warning(
                        "youtube %s audio unavailable (terminal, marked blocked): %s",
                        video_id, exc,
                    )
                    from yt_dlp.utils import DownloadError

                    raise DownloadError(
                        f"youtube audio download failed permanently for {video_id}: {exc}"
                    ) from exc
                if _is_gate_error(exc):
                    raise _YoutubeGateRequeue(str(exc)[:400]) from exc
                raise
            logger.info("youtube %s audio downloaded for ASR: %s", video_id, path)
            if audio_stash is not None:
                # P2-4: keep the wav alive for the engine retry; the caller
                # (or the retry call) cleans it up.
                audio_stash["wav"] = Path(path)
                audio_stash["dir"] = outdir
        if _should_shard(str(path)):
            # Bounded-RAM path (same route as transcribe_video): decode ONCE
            # into fixed-duration PCM shards and consume them one window at
            # a time. The temp dir is removed in finally.
            shard_dir = Path(tempfile.mkdtemp(prefix="vodrip-shards-"))
            try:
                return _transcribe_audio_source(
                    "youtube", video_id, str(path), language,
                    progress_cb, events_cb, t0,
                    sharded=True, shard_dir=shard_dir,
                )
            finally:
                shutil.rmtree(shard_dir, ignore_errors=True)
        return _transcribe_audio_source(
            "youtube", video_id, str(path), language,
            progress_cb, events_cb, t0,
            sharded=False, shard_dir=None,
        )
    finally:
        if cached is not None and audio_stash is not None:
            # P2-4 retry call — the last consumer: release the stashed wav.
            shutil.rmtree(str(audio_stash.pop("dir", "")), ignore_errors=True)
        elif outdir is not None and (audio_stash is None or "wav" not in audio_stash):
            # No stash (direct call) OR the download failed before stashing
            # — the temp dir is ours to remove.
            shutil.rmtree(outdir, ignore_errors=True)
        # else: first call with a stash — the dir stays alive for the retry
        # (the caller's job-level finally removes it).


# Wall-clock bound for an at-transcribe-time HLS audio fetch: a 6h VOD is
# ~350 MB of audio, and a dead/slow CDN must not pin a lane forever. The
# failure propagates to the job retry machinery (transient -> retry with
# backoff; permanent -> 'blocked' verdict + failed job).
_REMOTE_AUDIO_FETCH_TIMEOUT_S = 30 * 60.0
# Watchdog cadence for the fetch heartbeat (P1-2): refresh the job row every
# 5 min while the blocking ffmpeg run is in flight, far under the 45 min
# reclaim window. Tests shrink this to make the watchdog observable.
_FETCH_HEARTBEAT_INTERVAL_S = 300.0


def _is_remote_permanent_error(exc: Exception) -> bool:
    """True when the VOD is definitively gone/restricted (never retry).

    Twitch: TwitchVodUnavailable (sub-only / geo / removed) and the
    no-playable-variants runtime error. Kick: the video API returning no
    HLS source (deleted/unavailable VOD). Everything else (network blips,
    gate/rate limits, the fetch timeout) is transient and retries."""
    from services.twitch_gql_service import TwitchVodUnavailable

    if isinstance(exc, TwitchVodUnavailable):
        return True
    msg = str(exc).lower()
    return "no playable variant" in msg or "no hls source" in msg


def _fetch_remote_audio_wav(platform: str, video_id: str, channel: str, out_wav: Path) -> None:
    """Download the archived VOD's audio to a mono 16 kHz wav at transcribe time.

    Twitch: GQL PlaybackAccessToken + usher VOD master (the same fast path
    the preview proxy uses — twitch_gql_service.get_vod_playback_sync); the
    audio-only variant is preferred so ffmpeg never pulls video packets,
    else the LOWEST-bandwidth video variant (most Twitch VODs expose no
    audio-only rendition) with -vn discarding the picture track.
    Kick: the channel videos API resolves the VOD m3u8 (kick_api_service.
    get_video_info_api) — Kick HLS has no audio-only variant, so ffmpeg
    discards the video track (-vn). Both are bounded by
    _REMOTE_AUDIO_FETCH_TIMEOUT_S. ffmpeg headers mirror the live-captions
    rule: Twitch edge CDNs 403 an Origin header on segment fetches (the
    usher master needs it, the nauth-signed segments must not carry it).
    """
    ffmpeg = _resolve_ffmpeg_exe()
    headers: dict = {}
    if platform == "twitch":
        from services.twitch_gql_service import get_vod_playback_sync

        master_url, headers, variants = get_vod_playback_sync(video_id)
        audio_url = master_url
        best_tbr = -1.0
        fallback_url, fallback_tbr = None, float("inf")
        for v in variants or []:
            name = str(v.get("name") or "").lower()
            tbr = float(v.get("tbr") or 0.0)
            url = v.get("url")
            if not url:
                continue
            if name.startswith("audio"):
                if tbr > best_tbr:
                    best_tbr, audio_url = tbr, url
            elif tbr and tbr < fallback_tbr:
                fallback_tbr, fallback_url = tbr, url
        if best_tbr < 0 and fallback_url:
            # No audio-only rendition (the common Twitch case): pull the
            # lowest-bandwidth video variant; -vn discards the picture.
            audio_url = fallback_url
        headers = {k: val for k, val in headers.items() if k.lower() != "origin"}
    else:
        from services.kick_api_service import _BASE, get_video_info_api

        url = f"{_BASE}/{channel}/videos/{video_id}"
        info = get_video_info_api(url)
        if not info.m3u8_url:
            raise RuntimeError(f"Kick VOD {video_id} has no HLS source")
        audio_url = info.m3u8_url
        headers = {"referer": url, "origin": _BASE}
    cmd = [ffmpeg, "-y", "-v", "error"]
    for key, value in (headers or {}).items():
        cmd += ["-headers", f"{key}: {value}"]
    cmd += ["-i", audio_url, "-vn", "-ac", "1", "-ar", "16000",
            "-f", "wav", str(out_wav)]
    # P1-2: sp.run is ONE blocking call with no progress signal — a slow/
    # throttled CDN would let the job's heartbeat go stale (the fetch
    # timeout == the 30 min reclaim window) and a second lane would claim
    # the row, downloading + transcribing the same VOD twice. A daemon
    # watchdog refreshes the job row every 5 min while the fetch is in
    # flight, so the reclaim window never fires mid-download; the fetch
    # itself stays bounded by _REMOTE_AUDIO_FETCH_TIMEOUT_S.
    watchdog_stop = threading.Event()

    def _fetch_heartbeat(job_id: Optional[str]) -> None:
        # job_id is passed in, NOT re-read from _job_id_tls: threading.local
        # is per-thread, so the watchdog's own thread would always see None.
        while not watchdog_stop.wait(_FETCH_HEARTBEAT_INTERVAL_S):
            if job_id:
                try:
                    archive_db.update_job(job_id)
                except Exception:
                    logger.debug("fetch heartbeat failed for %s", job_id, exc_info=True)

    watchdog = threading.Thread(
        target=_fetch_heartbeat, args=(_current_job_id(),),
        name="fetch-heartbeat", daemon=True,
    )
    watchdog.start()
    try:
        proc = sp.run(cmd, capture_output=True, timeout=_REMOTE_AUDIO_FETCH_TIMEOUT_S)
    except sp.TimeoutExpired as exc:
        raise TimeoutError(
            f"{platform} VOD audio fetch exceeded "
            f"{int(_REMOTE_AUDIO_FETCH_TIMEOUT_S)}s for {video_id}"
        ) from exc
    finally:
        watchdog_stop.set()
        watchdog.join(timeout=2.0)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(
            f"ffmpeg HLS audio fetch failed for {platform}/{video_id}: {stderr}"
        )
    if not out_wav.is_file() or out_wav.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced no audio for {platform}/{video_id}")


def _has_local_archive(platform: str, video_id: str) -> bool:
    """True when the videos row owns an archive file on disk.

    Mirrors transcribe_video's file gate so the dispatcher can route
    file-less Twitch/Kick rows to the at-transcribe-time downloader."""
    rows = archive_db.query(
        "SELECT archive_path FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if not rows:
        return False
    path = rows[0]["archive_path"] or ""
    return bool(path.strip()) and os.path.isfile(path)


def _transcribe_remote_twitch_kick(
    platform: str,
    video_id: str,
    *,
    language: Optional[str] = None,
    progress_cb: Optional[Callable[[float, float, int, int], None]] = None,
    events_cb: Optional[Callable[..., Optional[dict]]] = None,
    audio_stash: Optional[dict] = None,
) -> dict:
    """ASR for a Twitch/Kick VOD archived metadata-only (no local file).

    Downloads the audio at transcribe time via the platform's HLS (GQL +
    usher for Twitch, the channel videos API for Kick — see
    _fetch_remote_audio_wav), decodes to a temp wav, then runs the shared
    _transcribe_audio_source core (sharded when long). Permanent failures
    (VOD deleted / sub-only / geo) mark the video transcript_kind='blocked'
    so the scheduler never re-enqueues it (same contract as YouTube's
    captionless route). The temp dir is removed in finally — also on
    failure.

    *audio_stash* is the TASK9 retry cache (see _transcribe_youtube_captionless):
    the first call downloads + stashes the wav, the parakeet->parakeet retry
    reuses it — no second 350 MB HLS fetch for an engine retry.
    """
    rows = archive_db.query(
        "SELECT channel FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if not rows:
        raise FileNotFoundError(f"no archived video {platform}/{video_id}")
    channel = rows[0]["channel"] or ""
    cached = (audio_stash or {}).get("wav")
    outdir = None
    try:
        if cached is not None:
            wav = cached
        else:
            outdir = Path(tempfile.mkdtemp(prefix=f"vodrip-transcribe-{platform}-{video_id}-"))
            wav = outdir / "audio.wav"
            try:
                _fetch_remote_audio_wav(platform, video_id, channel, wav)
            except Exception as exc:
                if _is_remote_permanent_error(exc):
                    archive_db.mark_video_transcript_kind(platform, video_id, "blocked")
                    logger.warning(
                        "%s %s audio unavailable (terminal, marked blocked): %s",
                        platform, video_id, exc,
                    )
                raise
            logger.info("%s %s audio downloaded for ASR: %s", platform, video_id, wav)
            if audio_stash is not None:
                audio_stash["wav"] = wav
                audio_stash["dir"] = outdir
        t0 = time.monotonic()
        if _should_shard(str(wav)):
            shard_dir = Path(tempfile.mkdtemp(prefix="vodrip-shards-"))
            try:
                return _transcribe_audio_source(
                    platform, video_id, str(wav), language,
                    progress_cb, events_cb, t0,
                    sharded=True, shard_dir=shard_dir,
                )
            finally:
                shutil.rmtree(shard_dir, ignore_errors=True)
        return _transcribe_audio_source(
            platform, video_id, str(wav), language,
            progress_cb, events_cb, t0,
            sharded=False, shard_dir=None,
        )
    finally:
        if cached is not None and audio_stash is not None:
            # P2-4 retry call — the last consumer: release the stashed wav.
            shutil.rmtree(str(audio_stash.pop("dir", "")), ignore_errors=True)
        elif outdir is not None and (audio_stash is None or "wav" not in audio_stash):
            # No stash (direct call) OR the download failed before stashing.
            shutil.rmtree(outdir, ignore_errors=True)
        # else: first call with a stash — kept alive for the engine retry.


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
        files = sorted(shard_dir.glob("shard_*.i16"))
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

    # Music / no-speech verdict (captionless YouTube ASR only): an
    # instrumental video has almost no speech — below
    # VODRIP_MUSIC_SPEECH_FRAC of the runtime the ASR has nothing to hear
    # (parakeet would emit no useful words on silence; music-with-lyrics
    # still has speech and transcribes normally). Persist
    # transcript_kind='music' so the scheduler never re-enqueues the video,
    # and report done WITHOUT loading the model or writing a resume
    # manifest.
    if platform == "youtube" and total_sec > 0:
        frac = speech_sec / total_sec
        if frac < MUSIC_SPEECH_FRAC:
            archive_db.mark_video_transcript_kind("youtube", video_id, "music")
            wall = time.monotonic() - t0
            stats = {
                "platform": platform,
                "video_id": video_id,
                "total_sec": round(total_sec, 3),
                "speech_sec": round(speech_sec, 3),
                "dead_air_sec": round(dead_air_sec, 3),
                "dead_air_pct": round(dead_air_pct, 1),
                "speech_frac": round(frac, 4),
                "segments": 0,
                "words": 0,
                "wall_sec": round(wall, 3),
                "skipped": "music",
            }
            logger.info(
                "transcribe youtube/%s marked music: %.1fs speech of %.1fs "
                "(%.2f%% < %.1f%%) — no ASR",
                video_id, speech_sec, total_sec, frac * 100,
                MUSIC_SPEECH_FRAC * 100,
            )
            return stats

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
            "model": _asr_model_name(),
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
    # slot's choice for this job's language: always 'parakeet' — the ONLY
    # ASR engine — or a clean _AsrRoutingError (unsupported language /
    # unavailable lane) is raised here, before any audio is consumed.
    engine = _job_engine(language)
    model = _parakeet_model()
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
    # Champion-name post-fix (transcript_fix): runs at the ONE choke point
    # of the engine, right before rows land. Per-job stats merge
    # into the returned dict under 'transcript_fix' and are logged at info.
    fix_stats = transcript_fix.new_stats()
    fix_on = transcript_fix.enabled()
    missing_set = set(missing)
    ci = 0
    n_chunks = len(chunks)
    twin_won = False  # higher-priority twin transcribed mid-run — abort
    # Per-call decode batch: parakeet on GPU slots sizes decode_streams
    # from free VRAM (one run == one batched call); CPU slots keep batch 1
    # (the sequential decode_stream loop inside _transcribe_batch_parakeet,
    # byte-identical to pre-batch runs).
    engine_batch = _parakeet_batch_size()
    engine_kwargs = {"batch_size": engine_batch}
    while ci < n_chunks:
        cs, ce = chunks[ci]
        if ci not in missing_set:
            speech_done += ce - cs
            ci += 1
            continue
        # Thermal ceiling: never feed the next batch while the card is
        # pinned above 90% util (driver/Windows stability, user requirement).
        _gpu_thermal_guard()
        # Batch consecutive missing clips into one GPU call; resume gaps only
        # shrink the run, never break the per-clip insert/manifest contract.
        run: list[tuple[int, tuple[float, float]]] = []
        while ci < n_chunks and ci in missing_set and len(run) < engine_batch:
            run.append((ci, chunks[ci]))
            ci += 1
        if sharded_audio is not None:
            batch_audio, concat_clips, clip_offsets = _clips_to_audio(sharded_audio, run)
            batch_out = _transcribe_batch_parakeet(
                model, batch_audio, concat_clips, language, clip_offsets=clip_offsets,
                **engine_kwargs,
            )
        else:
            batch_out = _transcribe_batch_parakeet(
                model, audio, [c for _, c in run], language, **engine_kwargs,
            )
        for (ci2, _), (chunk_segs, detected) in zip(run, batch_out):
            # _transcribe_batch_parakeet echoes the requested language back
            # (parakeet has no detection); first non-None wins.
            if detected_lang is None and detected:
                detected_lang = detected  # first batch's language wins
            lang = language or detected_lang  # explicit wins; else echoed; else None
            # Batch insert: one insert_transcript() call per chunk (it accepts
            # a list); a crash loses at most the in-flight chunk.
            first_idx = seg_idx
            batch_rows = []
            for seg in chunk_segs:
                if seg_idx in existing:
                    seg_idx += 1
                    continue
                if fix_on:
                    transcript_fix.fix_segment(
                        seg, engine=engine, language=lang, stats=fix_stats,
                    )
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
            "model": _asr_model_name(),
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
        "model": _asr_model_name(),
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
        # Parakeet has no language DETECTION: the stored rows carry the
        # explicit job language, and lang stays None for auto-detect runs
        # (the done-time channel-language correction stamps the family).
        "lang": detected_lang if language is None else None,
    }
    if fix_on:
        stats["transcript_fix"] = fix_stats
        logger.info(
            "transcribe %s/%s transcript-fix: %d segments touched, %d strong, "
            "%d weak, %d blocklisted near-misses",
            platform, video_id,
            fix_stats["segments_touched"], fix_stats["strong_replaced"],
            fix_stats["weak_replaced"], fix_stats["blocked_hits"],
        )
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

# Reclaim window for a 'running' transcribe job: 45 min. The max silent
# phase is bounded by the 30 min audio-fetch timeout (_REMOTE_AUDIO_FETCH_
# TIMEOUT_S — ffmpeg/yt-dlp downloads now heartbeat during the fetch, P1-2)
# and by a single CPU decode chunk (~30 min); 45 min sits comfortably past
# both, so the boundary-instant race (fetch exactly at the window edge)
# cannot hand a live job to a second lane.
_STALE_JOB_TIMEDELTA = timedelta(minutes=45)
# Chat-history backfills run one yt-dlp/GQL pass per video; a 13.5h VOD's
# live-chat replay can legitimately exceed the transcribe reclaim window,
# so 'chat' jobs get a 2h grace before a dead executor is assumed.
_CHAT_STALE_TIMEDELTA = timedelta(hours=2)
# Twitch chat backfills heartbeat their job row before every page fetch
# and after every stored page (P2-3), so a live executor's heartbeat is
# at most one stormed page old (~4-5 min of 429 backoff). A running job
# whose heartbeat stalls past this window is a dead or wedged executor —
# reclaim it instead of letting it hold the row for the flat 2h
# _CHAT_STALE_TIMEDELTA. 20 min = margin over a multi-page storm plus
# clock skew. The YouTube leg never heartbeats during its long yt-dlp
# download (heartbeat stays NULL) and keeps the 2h window.
_CHAT_HEARTBEAT_STALE = timedelta(minutes=20)

# YouTube chat-backfill pacing: min gap between chat video STARTS. A single
# worker starts ≤5/min (burst 2 requests each: extract + chat download); the
# 3-thread pool can run up to 3 concurrently, still under the measured
# Two lanes (user requirement): the interactive lane
# (preview/download/click-chat/search/watch) NEVER consults this pace or the
# bot gate — pacing exists only in the worker's background lane. While the
# app is alive (an 'app-activity' heartbeat stamped by the app every 30s)
# the interval is _YOUTUBE_CHAT_ACTIVE_INTERVAL_S so background volume stays
# under the radar and interactive traffic is never collateral damage; when
# the app is closed the worker falls back to slow-and-steady volume
# (_YOUTUBE_CHAT_QUIET_INTERVAL_S) — the machine is the user's, so the
# quota-sensitive YouTube chat fetch throttles to a crawl rather than
# racing. ponytail: per-process only — cross-process pacing needs a shared
# lock file if worker_server and the in-process worker ever overlap on one
# box.
_YOUTUBE_CHAT_ACTIVE_INTERVAL_S = 30.0
_YOUTUBE_CHAT_QUIET_INTERVAL_S = 60.0
_APP_ACTIVITY_AGE_S = 60.0
_youtube_chat_last_start = 0.0
_youtube_chat_pace_lock = threading.Lock()


def _youtube_chat_interval() -> float:
    """Pacing interval: longer while the app's interactive lane is active,
    longer still in background (autostart) mode, and longest of all when
    the app is closed — the quota-sensitive YouTube chat fetch backs off
    to ~2.5x the interactive gap in autostart, and to 2x in slow-and-steady
    closed-app mode."""
    if not archive_db.worker_live(age_s=_APP_ACTIVITY_AGE_S, tag="app-activity"):
        base = _YOUTUBE_CHAT_QUIET_INTERVAL_S
    else:
        base = _YOUTUBE_CHAT_ACTIVE_INTERVAL_S
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
    window: 45 min for transcribe/events (a single decode chunk can take
    ~30 min on CPU, and the audio fetch is bounded by its own 30 min
    timeout with a heartbeat watchdog — P1-2), 2 h for 'chat' on YouTube
    (a 13.5h VOD's live-chat replay download heartbeats nothing mid-run).
    Twitch 'chat' jobs heartbeat before every page fetch and after every
    stored page, so a running one whose heartbeat went stale past
    _CHAT_HEARTBEAT_STALE (20 min) is a dead or wedged executor —
    reclaimed long before the flat 2h window; NULL heartbeats
    (pre-heartbeat rows, YouTube) fall back to updated_at."""
    now = datetime.now(timezone.utc)
    transcribe_cutoff = (now - _STALE_JOB_TIMEDELTA).isoformat(timespec="seconds")
    twitch_chat_cutoff = (now - _CHAT_HEARTBEAT_STALE).isoformat(timespec="seconds")
    yt_chat_cutoff = (now - _CHAT_STALE_TIMEDELTA).isoformat(timespec="seconds")
    now_iso = now.isoformat(timespec="seconds")
    # String comparison is valid: both sides come from _now_iso (UTC, same width).
    rows = archive_db.query(
        """SELECT * FROM archive_jobs
           WHERE kind IN ('transcribe','events','chat')
             AND ((status = 'queued' AND (next_retry_at IS NULL OR next_retry_at <= ?))
                  OR (status = 'running' AND
                  COALESCE(heartbeat, updated_at) <
                  CASE WHEN kind = 'chat' AND platform = 'twitch' THEN ?
                       WHEN kind = 'chat' THEN ?
                       ELSE ? END))
           ORDER BY priority DESC, created_at ASC
           LIMIT 8""",
        (now_iso, twitch_chat_cutoff, yt_chat_cutoff, transcribe_cutoff),
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
                "WHERE id = ? AND status = 'queued' "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?)",
                (_now_iso(), _now_iso(), row["id"], now_iso),
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


class _YoutubeGateRequeue(Exception):
    """Raised by the captionless download when the YouTube bot gate arms
    mid-download. _process_job requeues (never fails) so the job drains
    once the freeze lifts — same contract as the claim-time gate check."""


def _youtube_transcribe_verdict(
    platform: str, video_id: str, *, subtitles_first: Optional[bool] = None
) -> str:
    """ASR decision for one YouTube transcribe job (non-YouTube -> 'run-asr').

    Decision matrix (captions-first, settings.yt_subtitles_first, default
    True):
      'skip-captions' — captions-first ON and transcript rows exist: the
          captions ARE the transcript — resolve the job done, never ASR.
      'music'         — terminal VAD verdict (speech fraction below
          VODRIP_MUSIC_SPEECH_FRAC): the video is instrumental — done,
          never ASR, never re-enqueued.
      'blocked'       — terminal download verdict (DRM/age-gated/deleted/
          private): the audio can never be fetched — done, never ASR,
          never re-enqueued.
      'wait-caption'  — no captions AND no captions_unavailable_at marker:
          the ingest leg is still extracting/retrying, so the caption
          question is undetermined — requeue, never run ASR, never resolve
          done. (The audio download would fail identically while the
          extract fails.)
      'run-asr'       — captions_unavailable_at marker set (permanent
          caption unavailability -> ASR candidate) OR the subtitles_first
          override is OFF (explicit user override: always ASR, captions
          included).
    ponytail: there is no force-transcribe path (archive_jobs has no force
    flag) — add one there if a job ever needs to bypass this.
    """
    if platform != "youtube":
        return "run-asr"
    if subtitles_first is None:
        try:
            from deps import settings_mgr  # lazy: archive_transcribe is opt-in by design

            subtitles_first = bool(getattr(settings_mgr.get(), "yt_subtitles_first", True))
        except Exception:
            subtitles_first = True
    kind = archive_db.video_transcript_kind(platform, video_id) or ""
    if kind == "music":
        return "music"
    if kind == "blocked":
        return "blocked"
    has_rows = bool(archive_db.transcript_for(platform, video_id))
    if has_rows and subtitles_first:
        return "skip-captions"
    if not has_rows and archive_db.captions_unavailable_at(platform, video_id) is None:
        return "wait-caption"
    return "run-asr"


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
        # ASR auto-detect (None) misfiring on a channel whose language
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
    thread's own recognizer copy — no global lock. Default False keeps the
    single-global-model path for direct callers and tests."""
    job_id = job["id"]
    platform, video_id = job["platform"], job["video_id"]
    # P2-4 retry cache (see _run_transcribe): the audio download functions
    # stash the wav path + owner dir here; the job-level finally releases it.
    audio_stash: dict = {}
    gpu_gate_held = False  # this thread holds the GPU sequential gate
    if multi:
        _multi_tls.active = True
    try:
        _job_id_tls.job_id = job_id
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

        if platform == "youtube":
            verdict = _youtube_transcribe_verdict(platform, video_id)
            if verdict == "skip-captions":
                logger.info(
                    "youtube %s/%s skipped — captions-first (captions are the transcript)",
                    platform, video_id,
                )
                archive_db.update_job(job_id, status="done", progress=1.0)
                return {
                    "job_id": job_id,
                    "platform": platform,
                    "video_id": video_id,
                    "skipped": "captions-first",
                }
            if verdict in ("music", "blocked"):
                # Terminal verdicts (VAD music/no-speech or DRM-blocked):
                # the video can never produce a useful ASR transcript —
                # resolve done, no model load, no re-enqueue.
                logger.info(
                    "youtube %s/%s skipped — terminal transcript verdict (%s)",
                    platform, video_id, verdict,
                )
                archive_db.update_job(job_id, status="done", progress=1.0)
                return {
                    "job_id": job_id,
                    "platform": platform,
                    "video_id": video_id,
                    "skipped": verdict,
                }
            if verdict == "wait-caption":
                # No captions AND no availability marker: the ingest leg is
                # still extracting/retrying, so the caption question is
                # undetermined. Requeue with a delay — never run ASR (the
                # download would fail identically) and never resolve done —
                # the job re-checks once the ingest retry resolves.
                err = (
                    "waiting for caption decision — no captions and no "
                    f"availability verdict (retry in {int(CAPTION_WAIT_RETRY_S)}s)"
                )
                archive_db.execute(
                    "UPDATE archive_jobs SET status='queued', error=?, updated_at=?, "
                    "heartbeat=?, next_retry_at=? WHERE id=?",
                    (
                        err,
                        _now_iso(),
                        _now_iso(),
                        (datetime.now(timezone.utc) + timedelta(seconds=CAPTION_WAIT_RETRY_S))
                        .isoformat(timespec="seconds"),
                        job_id,
                    ),
                )
                logger.info(
                    "youtube %s/%s requeued: waiting for caption decision",
                    platform, video_id,
                )
                return {
                    "job_id": job_id,
                    "platform": platform,
                    "video_id": video_id,
                    "requeued": "waiting-caption",
                }
            # verdict == 'run-asr': captions_unavailable_at marker set
            # (permanent caption unavailability -> ASR candidate) or the
            # yt_subtitles_first override is OFF (always ASR).

        if archive_db.transcribed_on_higher_priority_platform(platform, video_id):
            # The same live/VOD exists on a higher-priority platform
            # (youtube > twitch > kick) with transcript rows already — the
            # Kick (or Twitch) copy needs no ASR. Mirrors the download
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

        # GPU sequential gate: one video at a time on the GPU. A GPU-pinned
        # thread finding another video active (or a live-caption session
        # holding the GPU) releases its claim — requeue with a short backoff
        # so the claim SQL skips the row until next_retry_at instead of
        # hot-looping. The next video drains once the active one completes;
        # queue priority (user kicks at 100-200) still wins because claims
        # stay ordered priority DESC. CPU-pinned threads and non-transcribe
        # kinds never touch the gate — CPU lanes keep draining in parallel.
        _pin = _thread_pin()
        if job.get("kind") == "transcribe" and _pin is not None and _pin[0] == "cuda":
            if not _gpu_gate_try_acquire(platform, video_id):
                archive_db.execute(
                    "UPDATE archive_jobs SET status='queued', error=?, progress=0, "
                    "updated_at=?, heartbeat=?, next_retry_at=? "
                    "WHERE id=? AND status='running'",
                    (
                        "GPU sequential gate — another video is transcribing "
                        "on the GPU",
                        _now_iso(), _now_iso(),
                        (datetime.now(timezone.utc) + timedelta(seconds=_GPU_GATE_RECHECK_S))
                        .isoformat(timespec="seconds"),
                        job_id,
                    ),
                )
                logger.info(
                    "transcribe job %s (%s/%s) requeued: GPU sequential gate",
                    job_id, platform, video_id,
                )
                return {
                    "job_id": job_id,
                    "platform": platform,
                    "video_id": video_id,
                    "requeued": "gpu-gate",
                }
            gpu_gate_held = True

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
        # The engine is re-evaluated inside transcribe_video per run — with
        # parakeet as the only engine it always resolves to 'parakeet' or a
        # clean _AsrRoutingError.

        # TASK9: the retry re-runs _run_transcribe — the downloaded audio
        # must be fetched ONCE and reused (a retry must not re-download
        # ~350 MB). The download functions stash the wav path + owner dir
        # here; the retry call finds the stash and the job-level finally
        # releases whatever is left.

        def _run_transcribe() -> dict:
            if platform == "youtube":
                # Captionless YouTube (no archive_path): download bestaudio
                # at transcribe time via the app's yt-dlp session.
                return _transcribe_youtube_captionless(
                    video_id,
                    language=resolved_lang,
                    progress_cb=_progress, events_cb=events_cb,
                    audio_stash=audio_stash,
                )
            if not _has_local_archive(platform, video_id):
                # FIX A: Twitch/Kick VODs archived metadata-only (no local
                # file — ingest is metadata-only for Twitch, and evicted/
                # relocated rows can point at a deleted file): download the
                # audio at transcribe time instead of failing on the path.
                return _transcribe_remote_twitch_kick(
                    platform, video_id,
                    language=resolved_lang,
                    progress_cb=_progress, events_cb=events_cb,
                    audio_stash=audio_stash,
                )
            return transcribe_video(
                platform, video_id,
                language=resolved_lang,
                progress_cb=_progress, events_cb=events_cb,
            )

        engine = _job_engine(resolved_lang)
        try:
            stats = _run_transcribe()
        except _YoutubeGateRequeue as exc:
            # The bot gate armed DURING the audio download (it was
            # clear at claim time) — requeue, never fail: the freeze
            # lifts with one probe and the job drains then.
            archive_db.update_job(
                job_id, status="queued",
                error=f"youtube bot-gate cooldown active — requeued ({exc})"[:400],
            )
            logger.info(
                "youtube job %s requeued: bot-gate during audio download",
                job_id,
            )
            return {
                "job_id": job_id, "platform": platform, "video_id": video_id,
                "requeued": "youtube-gate",
            }
        except _AsrRoutingError:
            raise  # routing failures are terminal — never engine-retried
        except Exception as exc:
            # TASK9: parakeet is the ONLY engine — a mid-job failure
            # (decode error, recognizer crash) retries the SAME job once
            # with parakeet before giving up. Terminal failures (missing
            # archive file, yt-dlp download error) are NOT retried: they
            # cannot succeed on a second engine run. A wall-clock
            # download timeout (_YtDownloadTimedOut) is also not retried
            # — the failure is in the FETCH phase, not the engine; the
            # normal retry machinery requeues the job with backoff
            # instead.
            from yt_dlp.utils import DownloadError as _DLError

            try:
                from services.archive_ytdlp import _YtDownloadTimedOut as _DLTimeout
            except Exception:
                _DLTimeout = None  # pragma: no cover — archive_ytdlp is importable
            if isinstance(exc, (FileNotFoundError, _DLError)):
                raise
            if _DLTimeout is not None and isinstance(exc, _DLTimeout):
                raise
            logger.warning(
                "parakeet failed for %s/%s (%s: %s) — retrying job once",
                platform, video_id, type(exc).__name__, exc,
            )
            stats = _run_transcribe()
        archive_db.update_job(job_id, status="done", progress=1.0)
        # New transcript evidence -> re-aggregate the channel language
        # (throttled; best-effort — a failure must never fail the job).
        # When the job ran on auto-detection and the channel's now-known
        # family disagrees with the stored rows' language, the rows are
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
    except _AsrRoutingError as exc:
        # Clean routing failure — unsupported language or engine/lane
        # unavailable. ONE warning line, no traceback; the job lands
        # 'failed' immediately (the message carries the 'ASR unsupported' /
        # 'ASR unavailable' marker archive_db.update_job treats as
        # terminal — no backoff requeue; the scheduler's hourly requeue
        # still re-runs the job up to 3 attempts, so a later engine
        # install re-drains the row).
        logger.warning("transcribe job %s routing failure: %s", job_id, exc)
        archive_db.update_job(job_id, status="failed", error=str(exc)[:400])
        return {"job_id": job_id, "error": str(exc)}
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
        try:
            from yt_dlp.utils import DownloadError
            is_expected = isinstance(exc, DownloadError)
        except Exception:
            is_expected = False
        if is_expected:
            # Age-gated / deleted / geo-blocked videos are expected yt-dlp
            # failures — one clean line, no traceback spam.
            logger.warning("transcribe job %s failed (yt-dlp): %s", job_id, exc)
        else:
            logger.exception("transcribe job %s failed", job_id)
        archive_db.update_job(job_id, status="failed",
                              error=f"{type(exc).__name__}: {exc}"[:400])
        return {"job_id": job_id, "error": str(exc)}
    finally:
        _job_id_tls.job_id = None
        # P2-4: the first engine attempt's download dir is kept alive for a
        # possible retry — release it here if no retry consumed it.
        if audio_stash.get("dir"):
            shutil.rmtree(str(audio_stash.pop("dir")), ignore_errors=True)
        if gpu_gate_held:
            _gpu_gate_release(platform, video_id)
        if multi:
            _multi_tls.active = False


def _reap_stale_shard_dirs(max_age_s: float = 24 * 3600.0) -> None:
    """Delete orphaned shard scratch dirs in the system temp dir.

    Per-job cleanup runs in the job's own process (finally: rmtree); a
    violently killed job (taskkill /T, crash) never reaches it and Windows
    does not collect temp dirs, so orphans accumulate (observed a 1.2 GB
    dir from one dead job). A live job touches its shards continuously, so
    mtime older than max_age_s means the owner is gone. Runs once per
    worker start; harmless when nothing is stale."""
    import shutil
    import tempfile
    import time

    tdir = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_s
    try:
        for p in tdir.glob("vodrip-shards-*"):
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    logger.info("reaped stale shard dir %s", p.name)
            except OSError:
                continue
    except OSError:
        pass


def run_worker(
    *,
    once: bool = False,
    poll_interval: float = 2.0,
    max_workers: Optional[int] = None,
) -> None:
    """Blocking worker loop over the transcribe queue.

    Hybrid pool: the plan (_worker_plan()) is [("cuda","int8")]*gpu_slots +
    [("cpu","int8")]*cpu_slots on CUDA hosts (GPU copy + dynamic CPU lanes),
    [("cpu","int8")]*auto on CPU-only hosts. Each pool thread is
    pinned to its slot by the executor initializer, so a pinned CPU thread
    loads its recognizer on CPU even though the box has a GPU. The engine is
    ALWAYS parakeet (the only ASR engine): GPU slots run it with
    provider='cuda' when a CUDA sherpa-onnx is installed and VRAM allows,
    CPU slots run it int8. The shared queue stays FIFO (_claim_next_job)
    with no duration routing: the GPU thread claims the next job when it
    finishes one, CPU threads pick up queued VODs in the meantime.

    A plan of exactly one CUDA slot (VODRIP_TRANSCRIBE_WORKERS=0) is the
    single-global-model path: budget 1, one recognizer. max_workers
    overrides the plan for tests/launchers (all threads on the effective
    device, legacy raw-count semantics).

    DYNAMIC PLAN (natural plan only, max_workers=None): the pool plan is
    re-evaluated every _PLAN_RECHECK_S by a daemon plan-watch thread. When
    the GPU frees up (another app closed) or gets grabbed, the watch
    proposes the new plan and the main loop swaps the executor: in-flight
    jobs run out on the old pool (shutdown(wait=False)), fresh claims go to
    the new one — a GPU that becomes free turns the worker GPU-on without a
    max_workers plans are static (tests/launchers pin them).
    """
    global _pool_thread_seq
    _reap_stale_shard_dirs()  # one cleanup pass per boot; orphaned shards only
    _parakeet_cuda_available()  # cache wheel+compute before the first plan
    plan = _pool_plan(max_workers)
    budget = len(plan)
    multi = budget > 1
    logger.info("archive transcribe worker: plan=[%s] workers=%d",
                ", ".join(f"{d}/{ct}" for d, ct in plan), budget)
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
            with _proposal_lock:
                plan_proposal[:] = [new_plan]

    watch = threading.Thread(target=_plan_watch, name="plan-watch", daemon=True)
    if max_workers is None:
        watch.start()

    _pool_thread_seq = count()  # realign: new pool pins thread 0 -> plan[0]
    pool = _make_pool(plan, budget)
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
                    (new_plan,) = proposed
                    old_pool = pool
                    old_pool.shutdown(wait=False)  # in-flight run out there
                    plan, budget, multi = new_plan, len(new_plan), len(new_plan) > 1
                    _pool_thread_seq = count()  # realign pins to the new plan
                    pool = _make_pool(plan, budget)
                    logger.info(
                        "archive transcribe worker: plan changed -> [%s] "
                        "workers=%d (old pool draining)",
                        ", ".join(f"{d}/{ct}" for d, ct in plan), budget,
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
                        result = fut.result()
                    except Exception:
                        logger.exception("worker future crashed")  # belt & braces
                        result = None
                    _refill()
                    # --once must not spin on a requeued job: the YouTube
                    # bot-gate cooldown requeues the same row until the
                    # freeze lifts, so the queue is NOT drained. Exit 0 and
                    # let the next invocation retry later.
                    if once and isinstance(result, dict) and result.get("requeued"):
                        return
                if once and not pending:
                    break
    finally:
        if max_workers is None:
            watch_stop.set()
            watch.join(timeout=2.0)
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
    if not _real_gpu_info()[0]:  # compute probe — never trust adapter names
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
    # BOOT-03: never pip-install (or stamp a 24h lockout) while a live
    # worker holds sherpa DLLs — swapping the wheel under it is fatal,
    # and a failed install used to stamp first so the next 24h was dead.
    try:
        if archive_db.worker_live(age_s=45):
            logger.debug("GPU sherpa auto-install skipped (worker live)")
            return
    except Exception:
        logger.debug("GPU sherpa auto-install skipped (worker_live probe failed)")
        return

    def _stamp() -> None:
        path = _gpu_autoinstall_stamp_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            logger.debug("GPU auto-install stamp write failed: %s", exc)

    if not _gpu_autoinstall_needed():
        _stamp()  # nothing to do — don't re-probe every boot
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
            _stamp()
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


_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # Windows priority class


def _set_worker_low_priority() -> None:
    """Lower this worker's Windows priority class to BelowNormal.

    Archive transcription is background work: at Normal priority its decode
    threads compete evenly with the user's interactive apps and the machine
    stutters. BelowNormal keeps the queue draining while Windows schedules
    foreground work first. No-op on non-Windows / non-frozen runs. ponytail:
    per-process call at the worker entry points (module __main__, frozen
    launcher); the supervisor could also set it at spawn — this covers every
    spawn path with one call."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # 64-bit safety: HANDLE is pointer-sized. Without restype/argtypes,
        # ctypes truncates the -1 pseudo-handle to 32 bits and the call
        # fails silently (verified live — priority stayed Normal).
        get_cur = kernel32.GetCurrentProcess
        get_cur.restype = ctypes.c_void_p
        set_prio = kernel32.SetPriorityClass
        set_prio.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        set_prio.restype = ctypes.c_int
        set_prio(get_cur(), _BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass  # best-effort — never fail a worker start over priority


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
    _set_worker_low_priority()  # background work — don't stutter the box
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.once:
        # Belt-and-suspenders on top of worker_server's own guard: a live
        # worker heartbeat means someone else is draining the queue — exit
        # rc 0 quietly instead of double-loading the ASR engine.
        from services import archive_db

        if archive_db.worker_live(age_s=45):
            logging.getLogger(__name__).info(
                "archive worker already running — nothing to do (exit 0)"
            )
            raise SystemExit(0)
    run_worker(once=args.once, poll_interval=max(0.1, args.poll_interval))

def _run_module_selfcheck() -> None:
    """Import-time invariants (pure logic; no model load, no GPU, no
    downloads). Gated behind VODRIP_TRANSCRIBE_SELFCHECK=1 so pytest and
    app imports stay cheap — the block used to run unconditionally,
    spawning an nvidia-smi probe + a scratch mkdtemp on every import."""
    global _cpu_load_high, _cuda_runtime_vram, _free_system_ram_bytes
    global _cuda_recognizers_resident
    global _gpu_free_vram_bytes, _gpu_held_by_other, _gpu_util
    global _nvidia_smi_vram, _parakeet_cuda_ok, _parakeet_ok
    global _parakeet_provider, _vram_free_at, _vram_free_bytes
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
    ], "chunks must be capped at the 30 s chunking window (uncapped clips truncate)"
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
    assert _detect_device() in (("cuda", "int8"), ("cpu", "int8")), (
        "device settings must be a known pair (nvidia -> cuda/int8, else cpu/int8)"
    )
    assert _sanitize_key("abc/def:123") == "abc_def_123"
    _header = {"chunks": [(0.0, 5.0), (10.0, 15.0), (20.0, 25.0)], "model": PARAKEET_MODEL}
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
    _saved_pin_ov, _saved_workers = getattr(_multi_tls, "pin", None), os.environ.get(WORKERS_ENV)
    _saved_free_ram = _free_system_ram_bytes
    _saved_vram = _gpu_free_vram_bytes
    _saved_cpu = _cpu_load_high
    try:
        _multi_tls.pin = ("cpu", "int8")
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
        _multi_tls.pin = _saved_pin_ov
        _free_system_ram_bytes = _saved_free_ram
        _gpu_free_vram_bytes = _saved_vram
        _cpu_load_high = _saved_cpu
        if _saved_workers is None:
            os.environ.pop(WORKERS_ENV, None)
        else:
            os.environ[WORKERS_ENV] = _saved_workers
    # hybrid pool plan: CUDA host -> 1 GPU copy + 2 CPU threads by default;
    # WORKERS=0 disables the CPU side (the exact single-model plan);
    # WORKERS=3 -> 1 GPU + 3 CPU slots. RAM is patched ample so the clamp never
    # binds; the VRAM probe is patched per tier so the GPU-lane VRAM floor
    # decides whether the GPU lane exists at all (parakeet is one int8 model on
    # every tier — the old whisper model/precision ladder is gone).
    _saved_plan_pin, _saved_plan_w, _saved_plan_g = (
        getattr(_multi_tls, "pin", None), os.environ.get(WORKERS_ENV), os.environ.get(GPU_COPIES_ENV),
    )
    _saved_plan_vram, _saved_plan_cpu = _gpu_free_vram_bytes, _cpu_load_high
    _saved_plan_held = _gpu_held_by_other
    _saved_plan_util = _gpu_util
    try:
        _multi_tls.pin = ("cuda", "int8")
        _free_system_ram_bytes = lambda: 64 * 1024 ** 3
        _gpu_free_vram_bytes = lambda: 64 * 1024 ** 3  # ample VRAM — clamp must not bind
        _gpu_held_by_other = lambda: False
        _gpu_util = lambda: None
        _cpu_load_high = lambda: False
        os.environ.pop(WORKERS_ENV, None)
        os.environ.pop(GPU_COPIES_ENV, None)
        assert _worker_plan() == [("cuda", "int8")] + [("cpu", "int8")] * _cpu_auto_workers(), (
            "CUDA host defaults to 1 GPU copy + dynamic CPU lanes"
        )
        os.environ[WORKERS_ENV] = "0"
        assert _worker_plan() == [("cuda", "int8")], "WORKERS=0 -> exclusive-GPU plan"
        os.environ[WORKERS_ENV] = "3"
        assert _worker_plan() == [
            ("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8"), ("cpu", "int8"),
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
        # OUR OWN resident CUDA recognizer (the ORT arena is the VRAM hog):
        # the lane stays even below the 2 GiB floor — the model is already
        # loaded and the sequential gate reuses it; a fresh-cache read of the
        # resident arena must not exile the queue to CPU.
        _saved_cuda_resident = _cuda_recognizers_resident
        try:
            with _cuda_resident_lock:
                _cuda_recognizers_resident = 1
            assert _worker_plan() == [("cuda", "int8")] + [("cpu", "int8")] * _cpu_auto_workers(), (
                "own resident CUDA recognizer keeps the GPU lane under the floor"
            )
            os.environ[WORKERS_ENV] = "0"
            assert _worker_plan() == [("cuda", "int8")], (
                "WORKERS=0 + resident CUDA recognizer -> exclusive-GPU plan"
            )
            os.environ.pop(WORKERS_ENV, None)
        finally:
            with _cuda_resident_lock:
                _cuda_recognizers_resident = _saved_cuda_resident
        # GPU-lane VRAM floor (parakeet-only): 2 GiB floor -> lane off; at/above
        # it the lane is the fixed int8 plan on every tier.
        _gpu_free_vram_bytes = lambda: int(3.0 * 1024 ** 3)
        assert _gpu_lane_plan() == (None, "int8"), ">= 2 GiB -> GPU lane usable (int8)"
        _gpu_free_vram_bytes = lambda: int(5.0 * 1024 ** 3)
        assert _gpu_lane_plan() == (None, "int8"), "5 GiB -> same int8 lane"
        _gpu_free_vram_bytes = lambda: int(8.0 * 1024 ** 3)
        assert _gpu_lane_plan() == (None, "int8"), "8 GiB+ -> same int8 lane (no fp16 tier)"
        _cpu_slots = [("cpu", "int8")] * _cpu_auto_workers()
        assert _worker_plan() == [("cuda", "int8")] + _cpu_slots, (
            "8 GiB tier -> GPU lane + dynamic CPU lanes"
        )
        _gpu_free_vram_bytes = lambda: int(3.0 * 1024 ** 3)
        assert _worker_plan() == [("cuda", "int8")] + _cpu_slots, (
            "3 GiB tier -> the same GPU slot + dynamic CPU lanes"
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
        assert _worker_plan() == [("cuda", "int8")] + _cpu_slots, (
            "busy GPU caps copies at 1"
        )
        _gpu_util = lambda: 0.4
        assert _worker_plan() == [
            ("cuda", "int8"), ("cuda", "int8"), ("cuda", "int8"),
        ] + _cpu_slots, "idle GPU + ample VRAM allows the configured 3 copies"
        os.environ.pop(GPU_COPIES_ENV, None)
        # contended box: at most 1 CPU slot
        _gpu_free_vram_bytes = lambda: 64 * 1024 ** 3
        _cpu_load_high = lambda: True
        assert _worker_plan() == [("cuda", "int8"), ("cpu", "int8")], (
            "contended box must keep at most 1 CPU slot"
        )
        _gpu_free_vram_bytes = lambda: 1 * 1024 ** 3
        assert _worker_plan() == [("cpu", "int8")], (
            "tight VRAM + busy box -> 1 CPU slot floor"
        )
    finally:
        _multi_tls.pin = _saved_plan_pin
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

    # engine routing — parakeet is the ONLY ASR engine (pure logic: the import
    # probe is pinned via the cached _parakeet_ok flag, sherpa-onnx is never
    # imported here and nothing downloads; the sherpa cache is pointed at a
    # scratch dir with a controlled tokens.txt for the intersection check).
    _saved_pok, _saved_pin = _parakeet_ok, getattr(_multi_tls, "pin", None)
    _saved_pcache = os.environ.get(PARAKEET_CACHE_ENV)
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
        _multi_tls.pin = ("cpu", "int8")
        assert _job_engine("pt") == "parakeet", "pt routes to parakeet on a CPU lane"
        assert _job_engine("en") == "parakeet", "en routes to parakeet on a CPU lane"
        assert _job_engine("es") == "parakeet", "es routes to parakeet on a CPU lane"
        assert _job_engine(None) == "parakeet", "parakeet is the DEFAULT for unknown language"
        assert _job_engine("") == "parakeet", "empty language is auto-detect"

        def _expect_unsupported(lang: str) -> None:
            try:
                _job_engine(lang)
            except _AsrUnsupportedLanguage as _e:
                assert "ASR unsupported" in str(_e) and lang in str(_e), (
                    "the clean failure must name the language and the marker"
                )
                assert "26 European languages" in str(_e), (
                    "the clean failure must state the coverage"
                )
                return
            raise AssertionError(f"{lang!r} must fail cleanly as unsupported language")

        _expect_unsupported("ja")
        _expect_unsupported("ko")
        _expect_unsupported("zh")
        _expect_unsupported("ar")

        def _expect_lane_unavailable(fn: Any) -> None:
            try:
                fn()
            except _AsrLaneUnavailable as _e:
                assert "ASR unavailable" in str(_e), (
                    "the clean failure must carry the terminal marker"
                )
                return
            raise AssertionError("must fail cleanly as lane unavailable")

        _multi_tls.pin = ("cuda", "int8")
        assert _slot_engine("cuda") == "parakeet", "CUDA sherpa -> GPU slots run parakeet"
        assert _job_engine("pt") == "parakeet", (
            "GPU slot + CUDA sherpa + ample VRAM + supported lang -> parakeet"
        )
        _expect_unsupported("ja")  # GPU slots fail cleanly too — no whisper
        _parakeet_cuda_ok = False
        _expect_lane_unavailable(lambda: _slot_engine("cuda"))
        _expect_lane_unavailable(lambda: _job_engine("pt"))
        _parakeet_cuda_ok = True
        _vram_free_bytes = 1 * 1024 ** 3  # tight VRAM — fresh cache read
        _expect_lane_unavailable(lambda: _job_engine("pt"))
        _vram_free_bytes = 64 * 1024 ** 3
        _multi_tls.pin = ("cpu", "int8")
        os.environ[PARAKEET_ENV] = "0"
        _expect_lane_unavailable(lambda: _slot_engine("cpu"))
        _expect_lane_unavailable(lambda: _job_engine("pt"))
        os.environ.pop(PARAKEET_ENV, None)
        _parakeet_ok = False
        _expect_lane_unavailable(lambda: _job_engine("pt"))
        assert _parakeet_langs() == frozenset(), "import-fail lane routes no languages"
        _parakeet_ok = True
        assert _parakeet_langs() == PARAKEET_LANG_CANDIDATES, (
            "the candidate set is authoritative until the model dir exists"
        )
        assert 1 <= _parakeet_threads() <= _PARAAKEET_MAX_THREADS, (
            "thread budget must be positive and capped at the A/B sweet spot"
        )
        assert _parakeet_threads() * len(_worker_plan()) <= _cpu_thread_budget(), (
            "slots (GPU + CPU) x threads per slot must never exceed the CPU hard cap"
        )
        # CPU hard cap env: VODRIP_TRANSCRIBE_CPU_CAP scales the budget with
        # the box's logical threads; a bad value falls back to the 0.4 default.
        _saved_cap_env = os.environ.get(CPU_CAP_ENV)
        try:
            os.environ[CPU_CAP_ENV] = "0.5"
            assert _cpu_thread_budget() == max(1, int(0.5 * (os.cpu_count() or 4))), (
                "the CPU cap env must scale the thread budget"
            )
            os.environ[CPU_CAP_ENV] = "banana"
            assert _cpu_thread_budget() == max(1, int(0.4 * (os.cpu_count() or 4))), (
                "a bad CPU cap value must fall back to the 0.4 default"
            )
        finally:
            if _saved_cap_env is None:
                os.environ.pop(CPU_CAP_ENV, None)
            else:
                os.environ[CPU_CAP_ENV] = _saved_cap_env
        # tokens.txt present -> the candidate set must narrow to the model's
        # actual lang tokens (a swapped model missing a language is a clean
        # unsupported-language failure for that job).
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
        assert _job_engine("pt") == "parakeet", "narrowed model still runs pt"
        _expect_unsupported("en")  # model swap dropped en -> clean failure
        # word assembly: the real vocab convention (space-prefixed word-initial
        # pieces, lone-space piece inside a word) must produce transcript-shaped words.
        _toks = [" N", "eg", "an", " de", " ", "1", "0", " minut", "os", ",", " né", "?"]
        _ts = [0.32, 0.48, 0.56, 0.8, 1.04, 1.12, 1.12, 1.2, 1.28, 1.36, 2.08, 2.24]
        _ws = _parakeet_words(_toks, _ts)
        assert [w["word"] for w in _ws] == ["Negan", "de 10", "minutos,", "né?"], _ws
        assert _ws[0] == {"word": "Negan", "start": 0.32, "end": 0.56}, _ws[0]
    finally:
        _parakeet_ok = _saved_pok
        _parakeet_cuda_ok = _saved_pcuda
        _vram_free_bytes, _vram_free_at = _saved_pvram
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

    # real-GPU detection: a fake adapter (no nvidia-smi, no CUDA runtime
    # device) must read as NO GPU; a real compute probe (>= 1 GiB total) is
    # present — pure probes patched, no subprocess.
    _saved_smi, _saved_rt = _nvidia_smi_vram, _cuda_runtime_vram
    try:
        _nvidia_smi_vram = lambda: None
        _cuda_runtime_vram = lambda: None
        assert _real_gpu_info() == (False, 0, 0), (
            "a fake adapter (no compute probe) must not look like a GPU"
        )
        _nvidia_smi_vram = lambda: (int(8 * 1024 ** 3), int(4 * 1024 ** 3))
        assert _real_gpu_info() == (True, int(8 * 1024 ** 3), int(4 * 1024 ** 3)), (
            "a real SMI probe must report the GPU present"
        )
        _nvidia_smi_vram = lambda: (int(512 * 1024 ** 2), int(400 * 1024 ** 2))
        assert _real_gpu_info() == (False, 0, 0), (
            "sub-1 GiB total VRAM is not a compute GPU"
        )
    finally:
        _nvidia_smi_vram = _saved_smi
        _cuda_runtime_vram = _saved_rt

    # parakeet GPU batch size: VRAM-derived minus the caption reservation,
    # clamped [1, 32]; the CPU provider and unknown free VRAM stay sequential.
    _saved_bs_prov, _saved_bs_env = _parakeet_provider, os.environ.get(PARAKEET_BATCH_ENV)
    _saved_bs_vram = (_vram_free_bytes, _vram_free_at)
    try:
        _parakeet_provider = lambda: "cuda"
        _vram_free_bytes = int(8 * 1024 ** 3)
        _vram_free_at = time.monotonic()
        os.environ.pop(PARAKEET_BATCH_ENV, None)
        _bs_exp = (
            _vram_free_bytes
            - _caption_reserved_vram_bytes()
            - _PARAKEET_GPU_VRAM_EST
            - _PARAAKEET_BATCH_VRAM_SAFETY
        ) // _PARAAKEET_WINDOW_VRAM_EST
        assert _parakeet_batch_size() == max(1, min(_bs_exp, _PARAAKEET_BATCH_MAX)), (
            "GPU batch must be derived from free VRAM"
        )
        _parakeet_provider = lambda: "cpu"
        assert _parakeet_batch_size() == 1, (
            "the CPU provider must keep sequential decode"
        )
        _parakeet_provider = lambda: "cuda"
        _vram_free_bytes = 0
        assert _parakeet_batch_size() == 1, (
            "unknown free VRAM must not gamble a batch"
        )
        os.environ[PARAKEET_BATCH_ENV] = "4"
        _vram_free_bytes = int(16 * 1024 ** 3)
        assert _parakeet_batch_size() == 4, "the env cap must win over the estimate"
    finally:
        _parakeet_provider = _saved_bs_prov
        _vram_free_bytes, _vram_free_at = _saved_bs_vram
        if _saved_bs_env is None:
            os.environ.pop(PARAKEET_BATCH_ENV, None)
        else:
            os.environ[PARAKEET_BATCH_ENV] = _saved_bs_env

    # GPU sequential gate: one video at a time; release frees the gate.
    assert _gpu_gate_try_acquire("twitch", "v1") is True, "the first video takes the gate"
    assert _gpu_gate_try_acquire("twitch", "v2") is False, (
        "a second video must not stack on the GPU"
    )
    assert _gpu_gate_try_acquire("kick", "v3") is False, (
        "a different platform is blocked too"
    )
    assert _gpu_gate_try_acquire("twitch", "v1") is True, (
        "the holder re-acquires its own video"
    )
    _gpu_gate_release("twitch", "v1")
    assert _gpu_gate_try_acquire("kick", "v2") is True, (
        "release must free the gate for the next video"
    )
    _gpu_gate_release("kick", "v2")

    # GPU thermal guard: paces batches so util stays <= 90% — a no-op on
    # CPU lanes, sleeps while the card is pinned above the ceiling, and
    # gives up (degrades, never stalls) after the bounded wait.
    _saved_guard_prov, _saved_guard_util = _parakeet_provider, _gpu_util
    _saved_guard_sleep = time.sleep
    _guard_util_reads: list[float] = []

    def _guard_util_seq() -> Optional[float]:
        return _guard_util_reads.pop(0) if _guard_util_reads else None

    try:
        _parakeet_provider = lambda: "cuda"
        _gpu_util = _guard_util_seq
        _guard_sleep_calls: list[float] = []
        time.sleep = lambda s: _guard_sleep_calls.append(s)  # no real waiting

        _guard_util_reads[:] = [0.95, 0.95, 0.80]  # pinned, pinned, drops
        _gpu_thermal_guard()
        assert _guard_sleep_calls == [1.0, 1.0], (
            "a hot GPU must pace 1 s per poll until util drops under the ceiling"
        )

        _guard_sleep_calls.clear()
        _guard_util_reads[:] = [0.85]  # already under the ceiling
        _gpu_thermal_guard()
        assert _guard_sleep_calls == [], "under the ceiling no sleep happens"

        _guard_sleep_calls.clear()
        _guard_util_reads[:] = [0.99] * 100  # never drops below the ceiling
        _gpu_thermal_guard()
        assert len(_guard_sleep_calls) == int(_GPU_MAX_UTIL_WAIT_S), (
            "the pacing wait must be bounded — a foreign sustained load "
            "degrades to the ceiling instead of stalling the queue"
        )

        _parakeet_provider = lambda: "cpu"
        _guard_sleep_calls.clear()
        _guard_util_reads[:] = [0.99, 0.99]
        _gpu_thermal_guard()
        assert _guard_sleep_calls == [], "CPU lanes never pace GPU utilization"
    finally:
        _parakeet_provider = _saved_guard_prov
        _gpu_util = _saved_guard_util
        time.sleep = _saved_guard_sleep

if os.environ.get("VODRIP_TRANSCRIBE_SELFCHECK", "").strip().lower() in ("1", "true", "yes", "on"):
    _run_module_selfcheck()
