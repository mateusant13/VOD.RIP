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
    monkeypatch.setattr(at, "_device_override", ("cpu", "int8"))


def _force_cuda(monkeypatch) -> None:
    monkeypatch.setattr(at, "_device_override", ("cuda", "float16"))


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
    # 0 -> auto (2) with the RAM clamp applied.
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    _set_free_ram(monkeypatch, 3 * GIB)
    assert at._worker_budget() == 1  # auto 2 clamped to 1 at 3 GiB free
    # absent -> auto (2), clamped the same way.
    monkeypatch.delenv(at.WORKERS_ENV)
    _set_free_ram(monkeypatch, 3 * GIB)
    assert at._worker_budget() == 1
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._worker_budget() == 2  # absent + ample RAM -> default 2


def test_cpu_budget_floor_with_low_ram(monkeypatch):
    _force_cpu(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._worker_budget() == 1  # floor, never 0 workers


# --- _gpu_copies ------------------------------------------------------------

def test_gpu_copies_default_and_env_one(monkeypatch):
    _force_cuda(monkeypatch)
    # No env -> 1 copy, no probes (VRAM/RAM are irrelevant).
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._gpu_copies() == 1
    # env = 1 -> exactly 1 even with tiny RAM.
    monkeypatch.setenv(at.GPU_COPIES_ENV, "1")
    assert at._gpu_copies() == 1
    # env = 0 -> auto (1 copy).
    monkeypatch.setenv(at.GPU_COPIES_ENV, "0")
    assert at._gpu_copies() == 1


def test_gpu_copies_host_ram_clamp_only(monkeypatch):
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    # No CUDA visible -> VRAM probe skipped; host-RAM clamp is the only one.
    _set_free_ram(monkeypatch, 6 * GIB)
    assert at._gpu_copies() == 4  # usable 4.8 GiB // 1.0 GiB = 4
    _set_free_ram(monkeypatch, 1 * GIB)
    assert at._gpu_copies() == 1  # floor


def test_gpu_copies_vram_then_ram_clamp(monkeypatch):
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    # VRAM binds first: min(8, 12 GiB // 2 GiB) = 6, host RAM ample -> 6.
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda: (12 * GIB, 24 * GIB))
    _set_free_ram(monkeypatch, 64 * GIB)
    assert at._gpu_copies() == 6
    # Host RAM is the binding constraint when VRAM is ample: usable
    # 4 GiB * 0.8 = 3.2 GiB // 1.0 GiB = 3.
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda: (40 * GIB, 48 * GIB))
    _set_free_ram(monkeypatch, 4 * GIB)
    assert at._gpu_copies() == 3
    # Both clamps stacked: VRAM cap min(8, 4//2) = 2, RAM cap 6 -> 2.
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda: (4 * GIB, 24 * GIB))
    _set_free_ram(monkeypatch, 8 * GIB)
    assert at._gpu_copies() == 2


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
