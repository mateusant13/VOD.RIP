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
import time
from services import archive_transcribe as at
from transcribe_plan_isolation import isolate_worker_plan

GIB = 1024 ** 3


def _force_cuda(monkeypatch) -> None:
    """Idle, unheld GPU with ample VRAM: the plan's GPU lane is usable."""
    isolate_worker_plan(monkeypatch)
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
    isolate_worker_plan(monkeypatch)
    monkeypatch.setattr(at._multi_tls, "pin", ("cpu", "int8"), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)  # not contended



def test_cpu_cap_env_above_40_percent_is_clamped(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    monkeypatch.setenv(at.CPU_CAP_ENV, "0.9")
    assert at._cpu_thread_budget() == 8


def test_gpu_copies_share_the_total_lane_budget(monkeypatch):
    _force_cuda(monkeypatch)
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    monkeypatch.delenv(at.CPU_CAP_ENV, raising=False)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    monkeypatch.delattr(at._multi_tls, "plan", raising=False)
    monkeypatch.delattr(at._multi_tls, "asr_threads", raising=False)
    plan = at._worker_plan()
    assert plan == [("cuda", "int8")]
    assert len(plan) * at._parakeet_threads() <= at._cpu_thread_budget() == 1


def test_shared_cpu_limiter_serializes_offpool_stages(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    monkeypatch.delenv(at.CPU_CAP_ENV, raising=False)
    active = 0
    peak = 0
    lock = threading.Lock()

    def run_stage():
        nonlocal active, peak
        with at.transcription_cpu_limiter(1):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=run_stage) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


def test_limiter_rejects_stage_from_stale_plan(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    monkeypatch.setenv(at.CPU_CAP_ENV, "0.4")
    with at.transcription_cpu_limiter(2):
        monkeypatch.setenv(at.CPU_CAP_ENV, "0.05")
        try:
            with at.transcription_cpu_limiter(2):
                raise AssertionError("stale plan entered the limiter")
        except RuntimeError:
            pass
        assert at._cpu_limiter_active == 2
    assert at._cpu_limiter_active == 0

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
    """GPU_COPIES=2 is ignored: shared-model mode always produces 1 GPU slot."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "2")
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)
    assert at._worker_plan() == [("cuda", "int8")] + [
        ("cpu", "int8"),
    ] * at._cpu_auto_workers()


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
    """CPU slots account for the GPU copy's shared host-RAM estimate."""
    _force_cuda(monkeypatch)
    monkeypatch.setenv(at.WORKERS_ENV, "8")
    monkeypatch.delenv(at.GPU_COPIES_ENV, raising=False)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 4 * GIB)
    # usable 3.2 GiB - 1 GiB GPU host estimate leaves 2.2 GiB: one
    # 1.5 GiB CPU lane, not the unsafe two-lane plan.
    assert at._worker_plan() == [("cuda", "int8"), ("cpu", "int8")]


def test_pool_initializer_keeps_one_stable_thread_plan(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    monkeypatch.delenv(at.CPU_CAP_ENV, raising=False)
    monkeypatch.delattr(at._multi_tls, "plan", raising=False)
    monkeypatch.delattr(at._multi_tls, "asr_threads", raising=False)
    plan = [("cuda", "int8")] * 8  # max_workers override, cap budget is 8
    at._worker_thread_init(plan)
    try:
        monkeypatch.setattr(
            at, "_worker_plan", lambda: (_ for _ in ()).throw(AssertionError("replanned"))
        )
        assert at._parakeet_threads() == 1
    finally:
        at._multi_tls.plan = None
        at._multi_tls.asr_threads = None
        at._multi_tls.pin = None


# --- per-thread pin ---------------------------------------------------------

def _stub_parakeet_load(monkeypatch, calls: list) -> None:
    """Stub _load_parakeet so _parakeet_model() records the provider a
    pinned pool thread requests (no sherpa-onnx import, no model load)."""
    def fake_load(provider: str = "cpu"):
        calls.append(provider)
        return object()

    monkeypatch.setattr(at, "_load_parakeet", fake_load)


def test_parakeet_model_honors_pin(monkeypatch):
    """Shared model: the pin's device key populates _shared_models, not
    per-thread slots.  Same shared object is returned for a second call."""
    calls: list = []
    _stub_parakeet_load(monkeypatch, calls)
    monkeypatch.setattr(at, "_parakeet_cuda_ok", True)
    monkeypatch.setattr(at, "_parakeet_ok", True)
    tid = threading.get_ident()
    try:
        at._multi_tls.pin = ("cpu", "int8")
        at._multi_tls.active = True
        at._shared_models.clear()
        at._parakeet_model()
        assert calls == ["cpu"], "pinned CPU thread must load the CPU provider"
        assert "cpu" in at._shared_models
        # Same device key: shared model returned (no second load)
        obj = at._parakeet_model()
        assert obj is at._shared_models["cpu"]
        assert calls == ["cpu"], "no second load for same device"
        # CUDA pin: loads "cuda" into shared cache
        at._multi_tls.pin = ("cuda", "int8")
        at._parakeet_model()
        assert calls == ["cpu", "cuda"], "pinned CUDA thread must load the CUDA provider"
        assert "cuda" in at._shared_models
        # CUDA unavailable (CPU wheel) -> degrades to "cpu" key
        monkeypatch.setattr(at, "_parakeet_cuda_ok", False)
        at._shared_models.clear()
        at._parakeet_model()
        assert calls[-1] == "cpu", "no CUDA wheel -> CPU provider"
    finally:
        at._multi_tls.pin = None
        at._multi_tls.active = False
        at._shared_models.clear()


def test_parakeet_model_without_pin_uses_effective_device(monkeypatch):
    """No pin -> _detect_device() keys the shared model (not _parakeet_global)."""
    calls: list = []
    _stub_parakeet_load(monkeypatch, calls)
    monkeypatch.setattr(at, "_thread_pin", lambda: None)
    monkeypatch.setattr(at, "_offpool_cuda_available", lambda: True)
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: True)
    tid = threading.get_ident()
    try:
        at._multi_tls.active = True
        at._shared_models.clear()
        at._parakeet_model()
        # _detect_device() returns ("cuda", "int8") on GPU hosts; shared model keyed "cuda"
        assert at._shared_models, "shared model must be cached"
    finally:
        at._multi_tls.active = False
        at._shared_models.clear()
