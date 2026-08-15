"""RAM-aware worker budget clamp — services.archive_transcribe budget fns.

The free-RAM readout (_free_system_ram_bytes) is the single call site the
budget clamp uses, so these tests monkeypatch it to fixed values and pin the
clamp math: env knobs stay ceilings, RAM only ever reduces, floor is 1.
"""

from __future__ import annotations

import torch

from services import archive_transcribe as at

GIB = 1024 ** 3


def _set_free_ram(monkeypatch, free_bytes: int) -> None:
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: free_bytes)


def _force_cpu(monkeypatch) -> None:
    # raising=False: pin is a per-thread attribute of threading.local and
    # never exists on the pytest thread — default raising=True AttributeErrors.
    monkeypatch.setattr(at._multi_tls, "pin", ("cpu", "int8"), raising=False)
    # Fixed thread count: the CPU cap (budget = 0.4 x threads) must be
    # deterministic on any runner — 20 threads -> budget 8, auto lanes 3.
    monkeypatch.setattr("os.cpu_count", lambda: 20)


def _force_cuda(monkeypatch) -> None:
    monkeypatch.setattr(at._multi_tls, "pin", ("cuda", "int8"), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 20)


# --- _ram_worker_clamp (pure clamp math) ------------------------------------

def test_ram_clamp_clamps_by_free_ram(monkeypatch):
    # 8 configured, 3 GiB free + 1.5 GiB/worker estimate: usable = 3 * 0.8 =
    # 2.4 GiB -> 1 worker. (Unheaded math would give 2; 2 workers * 1.5 GiB =
    # exactly 100% of free RAM, which is what the 20% headroom forbids.)
    _set_free_ram(monkeypatch, 3 * GIB)
    assert at._ram_worker_clamp(8, at._CPU_WORKER_RSS_EST) == 1
    # 4 GiB free -> usable 3.2 GiB -> 2 workers.
    _set_free_ram(monkeypatch, 4 * GIB)
    assert at._ram_worker_clamp(8, at._CPU_WORKER_RSS_EST) == 2
    # 32 GiB free -> RAM is a non-issue, configured passes through.
    _set_free_ram(monkeypatch, 32 * GIB)
    assert at._ram_worker_clamp(8, at._CPU_WORKER_RSS_EST) == 8


def test_ram_clamp_floor_and_unknown(monkeypatch):
    # 1 GiB free -> usable 0.8 GiB -> 0 -> floor 1.
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._ram_worker_clamp(8, at._CPU_WORKER_RSS_EST) == 1
    # Unknown RAM (0) -> configured passes through untouched, like the VRAM
    # probe-failure path (trust the env cap).
    _set_free_ram(monkeypatch, 0)
    assert at._ram_worker_clamp(8, at._CPU_WORKER_RSS_EST) == 8
    # configured 1 -> exactly 1 regardless of RAM (legacy path, no probe).
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._ram_worker_clamp(1, at._CPU_WORKER_RSS_EST) == 1


# --- _worker_budget (CPU path) ----------------------------------------------

def test_cpu_budget_clamps_with_ram(monkeypatch):
    _force_cpu(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    _set_free_ram(monkeypatch, 4 * GIB)
    assert at._worker_budget() == 2  # 4 GiB free, usable 3.2 GiB -> 2
    _set_free_ram(monkeypatch, 32 * GIB)
    assert at._worker_budget() == 8  # env ceiling honored when RAM is ample


def test_cpu_budget_env_override_wins(monkeypatch):
    _force_cpu(monkeypatch)
    # VODRIP_TRANSCRIBE_WORKERS=1 -> exactly 1 even with huge RAM.
    monkeypatch.setenv(at.WORKERS_ENV, "1")
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._worker_budget() == 1
    # 0 -> auto (dynamic CPU default) with the RAM clamp applied.
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    _set_free_ram(monkeypatch, 3 * GIB)
    assert at._worker_budget() == 1  # auto lanes clamped to 1 at 3 GiB free
    # absent -> auto (dynamic CPU default), clamped the same way.
    monkeypatch.delenv(at.WORKERS_ENV)
    _set_free_ram(monkeypatch, 3 * GIB)
    assert at._worker_budget() == 1
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._worker_budget() == at._cpu_auto_workers()  # absent + ample RAM -> ladder


def test_cpu_budget_floor_with_low_ram(monkeypatch):
    _force_cpu(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._worker_budget() == 1  # floor, never 0 workers


# --- _gpu_copies ------------------------------------------------------------

def _allow_cuda(monkeypatch, vram: int) -> None:
    """Idle, unheld GPU with a measured free-VRAM allowance."""
    _force_cuda(monkeypatch)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: vram)
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)
    monkeypatch.setattr(at, "_gpu_util", lambda: 0.1)


def test_gpu_copies_default_and_env_one(monkeypatch):
    # No env -> 1 copy; ample VRAM/RAM make the clamps no-ops.
    _allow_cuda(monkeypatch, 64 * GIB)
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._gpu_copies() == 1
    # env = 1 -> exactly 1 even with tiny RAM (floor 1).
    monkeypatch.setenv(at.GPU_COPIES_ENV, "1")
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._gpu_copies() == 1
    # env = 0 -> auto (1 copy).
    monkeypatch.setenv(at.GPU_COPIES_ENV, "0")
    assert at._gpu_copies() == 1


def test_gpu_copies_host_ram_clamp_only(monkeypatch):
    # VRAM never binds (64 GiB allowance -> per-copy budget 8 GiB leaves the
    # env cap alone); host-RAM clamp is the only reducer.
    _allow_cuda(monkeypatch, 64 * GIB)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    _set_free_ram(monkeypatch, 6 * GIB)
    assert at._gpu_copies() == 4  # usable 4.8 GiB // 1.0 GiB = 4
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._gpu_copies() == 1  # floor


def test_gpu_copies_vram_then_ram_clamp(monkeypatch):
    per = at._gpu_model_vram_est() + at._GPU_VRAM_HEADROOM  # 4 GiB for parakeet
    # VRAM binds first: min(8, 40 GiB // 4 GiB) = 8, host RAM ample -> 8.
    _allow_cuda(monkeypatch, 40 * GIB)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._gpu_copies() == 8
    # Host RAM is the binding constraint when VRAM is ample: usable
    # 4 GiB * 0.8 = 3.2 GiB // 1.0 GiB = 3.
    _allow_cuda(monkeypatch, 40 * GIB)
    _set_free_ram(monkeypatch, 4 * GIB)
    assert at._gpu_copies() == 3
    # Tight VRAM: one per-copy budget fits -> floor 1, RAM ample keeps it.
    _allow_cuda(monkeypatch, per + 1)
    _set_free_ram(monkeypatch, 8 * GIB)
    assert at._gpu_copies() == 1


def test_gpu_copies_held_gpu_force_zero(monkeypatch):
    """nvidia-smi compute-apps shows another process -> never stack."""
    _force_cuda(monkeypatch)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: True)
    monkeypatch.setattr(at, "_gpu_util", lambda: 0.1)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._gpu_copies() == 0


def test_gpu_copies_busy_gpu_caps_at_one(monkeypatch):
    """util >= 70% -> one copy is the ceiling, never two."""
    _force_cuda(monkeypatch)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)
    monkeypatch.setattr(at, "_gpu_util", lambda: 0.85)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._gpu_copies() == 1


# --- _free_system_ram_bytes cache -------------------------------------------

def test_free_ram_readout_cached_within_ttl(monkeypatch):
    import ctypes

    monkeypatch.setattr(at, "_ram_free_at", 0.0)  # drop any earlier cache
    real_call = ctypes.windll.kernel32.GlobalMemoryStatusEx
    calls = {"n": 0}

    def counting(*args):
        calls["n"] += 1
        return real_call(*args)

    monkeypatch.setattr(ctypes.windll.kernel32, "GlobalMemoryStatusEx", counting)
    at._free_system_ram_bytes()
    at._free_system_ram_bytes()
    assert calls["n"] == 1, "second read within the TTL must come from the cache"
