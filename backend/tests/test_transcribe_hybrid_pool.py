"""Hybrid GPU+CPU transcription pool — archive_transcribe._worker_plan + pins.

Pure-plan tests (no model load, no GPU): pin _device_override / free RAM /
torch probes exactly like test_worker_budget_ram.py, then assert the slot
lists. The pin tests stub faster_whisper.WhisperModel and assert the
per-thread device/compute_type args _thread_model passes to it.
"""

from __future__ import annotations

import sys
import threading
import types

import torch

from services import archive_transcribe as at

GIB = 1024 ** 3


def _force_cuda(monkeypatch) -> None:
    """Idle, unheld GPU with ample VRAM: the plan's GPU lane is usable."""
    monkeypatch.setattr(at, "_device_override", ("cuda", "float16"))
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: 64 * GIB)  # ample VRAM
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)       # no foreign tenant
    monkeypatch.setattr(at, "_gpu_util", lambda: 0.1)                  # idle GPU
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)           # not contended


def _force_cpu(monkeypatch) -> None:
    monkeypatch.setattr(at, "_device_override", ("cpu", "int8"))
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)


# --- _worker_plan shapes ----------------------------------------------------

def test_plan_nvidia_default_hybrid(monkeypatch):
    """CUDA host, no env: 1 GPU copy + 2 CPU threads (the new default)."""
    _force_cuda(monkeypatch)
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    assert at._worker_plan() == [("cuda", "float16"), ("cpu", "int8"), ("cpu", "int8")]


def test_plan_gpu_copies_two(monkeypatch):
    """GPU_COPIES=2 -> 2 GPU slots in front of the default 2 CPU slots."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "2")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda: (64 * GIB, 80 * GIB))
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    assert at._worker_plan() == [
        ("cuda", "float16"), ("cuda", "float16"),
        ("cpu", "int8"), ("cpu", "int8"),
    ]


def test_plan_workers_zero_restores_exclusive_gpu(monkeypatch):
    """WORKERS=0 disables the CPU side -> the exact legacy single-model plan."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    assert at._worker_plan() == [("cuda", "float16")]


def test_plan_workers_three(monkeypatch):
    """WORKERS=3 -> 1 GPU + 3 CPU slots."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "3")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    assert at._worker_plan() == [
        ("cuda", "float16"), ("cpu", "int8"), ("cpu", "int8"), ("cpu", "int8"),
    ]


def test_plan_cpu_only_host(monkeypatch):
    """GPU-less host: unchanged [cpu, cpu] (WORKERS default 2)."""
    _force_cpu(monkeypatch)
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    assert at._worker_plan() == [("cpu", "int8"), ("cpu", "int8")]


def test_plan_env_forced_cpu_matches_cpu_host(monkeypatch):
    """VODRIP_WHISPER_DEVICE=cpu forces the CPU plan even on a CUDA box."""
    monkeypatch.setenv("VODRIP_WHISPER_DEVICE", "cpu")
    monkeypatch.setattr(at, "_device_override", None)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)
    at._detect_device.cache_clear()
    try:
        assert at._worker_plan() == [("cpu", "int8"), ("cpu", "int8")]
    finally:
        at._detect_device.cache_clear()


def test_legacy_single_model_invariant(monkeypatch):
    """gpu_slots==1 AND cpu_slots==0 -> the exact legacy plan (budget 1)."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    plan = at._worker_plan()
    assert plan == [("cuda", "float16")] and len(plan) == 1
    assert at._worker_budget() == 1  # run_worker's multi = len(plan) > 1 -> False
    # max_workers override keeps the legacy raw-count semantics for tests.
    assert at._pool_plan(max_workers=1) == [("cuda", "float16")]


def test_plan_ram_clamp_binds_cpu_slots(monkeypatch):
    """CPU slots share the host-RAM budget (conservative by design)."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 4 * GIB)
    # usable 3.2 GiB // 1.5 GiB per CPU worker -> 2 slots (not 8); the GPU
    # copy (default 1, no VRAM probe) passes through untouched.
    assert at._worker_plan() == [("cuda", "float16"), ("cpu", "int8"), ("cpu", "int8")]


# --- per-thread pin ---------------------------------------------------------

def _stub_faster_whisper(monkeypatch, calls: list) -> None:
    fw = types.ModuleType("faster_whisper")

    class _FakeModel:
        def __init__(self, name, device, compute_type, download_root):
            calls.append((device, compute_type))

    fw.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)


def test_thread_model_honors_pin(monkeypatch):
    """A pool thread loads its model on its pinned device, not the host's."""
    calls: list = []
    _stub_faster_whisper(monkeypatch, calls)
    monkeypatch.setattr(at, "_device_override", ("cuda", "float16"))
    tid = threading.get_ident()
    try:
        at._multi_tls.pin = ("cpu", "int8")
        at._thread_model()
        assert calls == [("cpu", "int8")], "pinned CPU thread must load on CPU"
        at._thread_slots.pop(tid, None)  # fresh slot -> reload with a new pin
        at._multi_tls.pin = ("cuda", "float16")
        at._thread_model()
        assert calls == [("cpu", "int8"), ("cuda", "float16")], (
            "pinned CUDA thread must load on CUDA"
        )
    finally:
        if hasattr(at._multi_tls, "pin"):
            delattr(at._multi_tls, "pin")
        at._thread_slots.pop(tid, None)


def test_thread_model_without_pin_uses_effective_device(monkeypatch):
    """No pin (direct callers / legacy path) -> _effective_device() as before."""
    calls: list = []
    _stub_faster_whisper(monkeypatch, calls)
    monkeypatch.setattr(at, "_device_override", ("cuda", "float16"))
    tid = threading.get_ident()
    try:
        at._thread_model()
        assert calls == [("cuda", "float16")]
    finally:
        at._thread_slots.pop(tid, None)
