"""Hybrid GPU+CPU transcription pool — archive_transcribe._worker_plan + pins.

Pure-plan tests (no model load, no GPU): pin _multi_tls.pin / free RAM /
torch probes exactly like test_worker_budget_ram.py, then assert the slot
lists. The pin tests stub _load_parakeet and assert the per-thread
provider _parakeet_model passes it (parakeet is the ONLY engine — the
faster-whisper _thread_model tests are gone with it).
"""

from __future__ import annotations

import sys
import threading

from services import archive_transcribe as at

GIB = 1024 ** 3


def _force_cuda(monkeypatch) -> None:
    """Idle, unheld GPU with ample VRAM: the plan's GPU lane is usable."""
    # raising=False: pin is a per-thread attribute of threading.local and
    # never exists on the pytest thread — default raising=True AttributeErrors.
    monkeypatch.setattr(at._multi_tls, "pin", ("cuda", "int8"), raising=False)
    # Fixed thread count: the CPU cap (budget = 0.4 x threads) must be
    # deterministic on any runner — 20 threads -> budget 8, auto lanes 3.
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: 64 * GIB)  # ample VRAM
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)       # no foreign tenant
    monkeypatch.setattr(at, "_gpu_util", lambda: 0.1)                  # idle GPU
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)           # not contended


def _force_cpu(monkeypatch) -> None:
    monkeypatch.setattr(at._multi_tls, "pin", ("cpu", "int8"), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)  # not contended


# --- _worker_plan shapes ----------------------------------------------------

def test_plan_nvidia_default_hybrid(monkeypatch):
    """CUDA host, no env: 1 GPU copy + the dynamic CPU lane default."""
    _force_cuda(monkeypatch)
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    assert at._worker_plan() == [("cuda", "int8")] + [
        ("cpu", "int8"),
    ] * at._cpu_auto_workers()


def test_plan_gpu_copies_two(monkeypatch):
    """GPU_COPIES=2 -> 2 GPU slots in front of the dynamic default CPU slots."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "2")
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    assert at._worker_plan() == [
        ("cuda", "int8"), ("cuda", "int8"),
    ] + [("cpu", "int8")] * at._cpu_auto_workers()


def test_plan_workers_zero_restores_exclusive_gpu(monkeypatch):
    """WORKERS=0 disables the CPU side -> the exact legacy single-model plan."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    assert at._worker_plan() == [("cuda", "int8")]


def test_plan_workers_three(monkeypatch):
    """WORKERS=3 -> 1 GPU + 3 CPU slots."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "3")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    assert at._worker_plan() == [
        ("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8"), ("cpu", "int8"),
    ]


def test_plan_cpu_only_host(monkeypatch):
    """GPU-less host: dynamic default CPU lanes (WORKERS unset)."""
    _force_cpu(monkeypatch)
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    assert at._worker_plan() == [("cpu", "int8")] * at._cpu_auto_workers()


def test_plan_env_forced_cpu_matches_cpu_host(monkeypatch):
    """VODRIP_WHISPER_DEVICE=cpu forces the CPU plan even on a CUDA box."""
    # test_parakeet_e2e_real.py sets WORKERS_ENV at module level; clear it so
    # the plan length matches the auto ladder (order-independent). No pin:
    # the env itself must drive the plan (a leaked pin would override it).
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    monkeypatch.delattr(at._multi_tls, "pin", raising=False)
    monkeypatch.setenv("VODRIP_WHISPER_DEVICE", "cpu")
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)  # deterministic under suite load
    monkeypatch.setattr("os.cpu_count", lambda: 20)  # budget 8, auto lanes 3 — any runner
    at._detect_device.cache_clear()
    try:
        assert at._worker_plan() == [("cpu", "int8")] * at._cpu_auto_workers()
    finally:
        at._detect_device.cache_clear()


def test_legacy_single_model_invariant(monkeypatch):
    """gpu_slots==1 AND cpu_slots==0 -> the exact legacy plan (budget 1)."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    plan = at._worker_plan()
    assert plan == [("cuda", "int8")] and len(plan) == 1
    assert at._worker_budget() == 1  # run_worker's multi = len(plan) > 1 -> False
    # max_workers override keeps the legacy raw-count semantics for tests.
    assert at._pool_plan(max_workers=1) == [("cuda", "int8")]


def test_plan_ram_clamp_binds_cpu_slots(monkeypatch):
    """CPU slots share the host-RAM budget (conservative by design)."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 4 * GIB)
    # usable 3.2 GiB // 1.5 GiB per CPU worker -> 2 slots (not 8); the GPU
    # copy (default 1, no VRAM probe) passes through untouched.
    assert at._worker_plan() == [("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8")]


# --- per-thread pin ---------------------------------------------------------

def _stub_parakeet_load(monkeypatch, calls: list) -> None:
    """Stub _load_parakeet so _parakeet_model() records the provider a
    pinned pool thread requests (no sherpa-onnx import, no model load)."""
    def fake_load(provider: str = "cpu"):
        calls.append(provider)
        return object()

    monkeypatch.setattr(at, "_load_parakeet", fake_load)


def test_parakeet_model_honors_pin(monkeypatch):
    """A pool thread loads its recognizer on its pinned device, not the host's."""
    calls: list = []
    _stub_parakeet_load(monkeypatch, calls)
    monkeypatch.setattr(at, "_parakeet_cuda_ok", True)
    monkeypatch.setattr(at, "_parakeet_ok", True)
    tid = threading.get_ident()
    try:
        at._multi_tls.pin = ("cpu", "int8")
        at._multi_tls.active = True
        at._thread_slots.pop(tid, None)  # fresh slot -> reload with a new pin
        at._parakeet_model()
        assert calls == ["cpu"], "pinned CPU thread must load the CPU provider"
        at._thread_slots.pop(tid, None)
        at._multi_tls.pin = ("cuda", "int8")
        at._parakeet_model()
        assert calls == ["cpu", "cuda"], (
            "pinned CUDA thread must load the CUDA provider"
        )
        # CUDA unavailable (CPU wheel) -> the CUDA pin still degrades to CPU
        at._thread_slots.pop(tid, None)
        monkeypatch.setattr(at, "_parakeet_cuda_ok", False)
        at._parakeet_model()
        assert calls == ["cpu", "cuda", "cpu"], "no CUDA wheel -> CPU provider"
    finally:
        # Restore to None — never delattr. threading.local + monkeypatch.setattr
        # in a later test file requires the attribute to exist (observed:
        # test_worker_budget_ram AttributeError after this test in a merged run).
        at._multi_tls.pin = None
        at._multi_tls.active = False
        at._thread_slots.pop(tid, None)


def test_parakeet_model_without_pin_uses_effective_device(monkeypatch):
    """No pin (direct callers) -> the off-pool provider gate decides."""
    calls: list = []
    _stub_parakeet_load(monkeypatch, calls)
    monkeypatch.setattr(at, "_thread_pin", lambda: None)
    monkeypatch.setattr(at, "_offpool_cuda_available", lambda: True)
    tid = threading.get_ident()
    try:
        at._multi_tls.active = True
        at._thread_slots.pop(tid, None)
        at._parakeet_model()
        assert calls == ["cuda"], "real-GPU off-pool caller gets the CUDA provider"
        at._thread_slots.pop(tid, None)
        monkeypatch.setattr(at, "_offpool_cuda_available", lambda: False)
        at._parakeet_model()
        assert calls == ["cuda", "cpu"], "no real GPU -> CPU provider"
    finally:
        at._multi_tls.active = False
        at._thread_slots.pop(tid, None)
