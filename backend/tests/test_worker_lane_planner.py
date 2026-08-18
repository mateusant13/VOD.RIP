"""GPU lane plan unit tests — parakeet-only, 2 GiB floor.

The lane planner (services.archive_transcribe._gpu_lane_plan / _gpu_copies /
_worker_plan) is a PURE function of the measured 60 s-median free-VRAM
allowance + the nvidia-smi compute-apps/util readouts. No real hardware, no
network, no model load: every probe is patched, so the whole ladder (1.5 ->
32 GiB cards + the sub-2 GiB floor) is pinned without a GPU.

The old whisper model/precision ladder is GONE with the engine: parakeet is
ONE int8 model on every tier. The only ladder left is the VRAM floor —
  >= 2 GiB free allowance -> the GPU lane runs parakeet int8
  <  2 GiB -> CPU lane only (1-2 int8 copies by cores/RAM)
Unknown allowance (probe failure) trusts the env cap like everywhere else.
"""
import contextlib
import os

from services import archive_transcribe as at


def _patched(
    free_vram_gb, *, held=False, util=None, workers="2", gpu_copies="1"
):
    """Patch the planner's probes and return (lane, copies, plan).

    Every patched probe is RESTORED (the module-level _gpu_held_by_other
    must stay the real tasklist probe — the held-process tests call it)."""
    saved = {name: getattr(at, name) for name in (
        "_gpu_free_vram_bytes", "_gpu_held_by_other", "_gpu_util",
        "_cpu_load_high", "_free_system_ram_bytes",
    )}
    at._gpu_free_vram_bytes = lambda: int(free_vram_gb * 1024 ** 3)
    at._gpu_held_by_other = lambda: held
    at._gpu_util = lambda: util
    at._cpu_load_high = lambda: False
    at._free_system_ram_bytes = lambda: 64 * 1024 ** 3  # RAM never binds
    at._multi_tls.pin = ("cuda", "int8")
    os.environ["VODRIP_TRANSCRIBE_WORKERS"] = workers
    os.environ["VODRIP_TRANSCRIBE_GPU_COPIES"] = gpu_copies
    try:
        lane = at._gpu_lane_plan()
        copies = at._gpu_copies()
        plan = at._worker_plan()
        return lane, copies, plan
    finally:
        os.environ.pop("VODRIP_TRANSCRIBE_WORKERS", None)
        os.environ.pop("VODRIP_TRANSCRIBE_GPU_COPIES", None)
        at._multi_tls.pin = None
        for name, fn in saved.items():
            setattr(at, name, fn)


def test_ladder_32gb_int8():
    lane, copies, plan = _patched(32)
    assert lane == (None, "int8"), lane
    assert copies == 1, copies
    assert plan == [("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8")], plan


def test_ladder_16gb_int8():
    lane, copies, plan = _patched(16)
    assert lane == (None, "int8"), lane
    assert plan[0] == ("cuda", "int8"), plan


def test_ladder_8gb_int8():
    lane, copies, plan = _patched(8)
    assert lane == (None, "int8"), lane
    assert plan[0] == ("cuda", "int8"), plan


def test_ladder_6gb_int8():
    lane, copies, plan = _patched(6)
    assert lane == (None, "int8"), lane
    assert plan[0] == ("cuda", "int8"), plan


def test_ladder_3gb_int8_entry():
    """The old 3-6 GiB 'medium int8 / fp16' rungs are one int8 plan now."""
    lane, copies, plan = _patched(3)
    assert lane == (None, "int8"), lane
    assert plan[0] == ("cuda", "int8"), plan


def test_ladder_1_5gb_cpu_only():
    lane, copies, plan = _patched(1.5)
    assert lane is None, lane
    assert copies == 0, copies
    assert plan == [("cpu", "int8"), ("cpu", "int8")], plan


def test_cpu_lane_exists_at_every_tier():
    for gb in (2.0, 3.0, 6.0, 8.0, 16.0, 32.0):
        _, _, plan = _patched(gb)
        assert ("cpu", "int8") in plan, (gb, plan)


def test_background_cpu_default_is_three_lanes(monkeypatch):
    """FIX E: background (autostart) CPU lanes 2 -> 3 — the default plan is
    [cpu,int8] x 3 (~3 GB RSS at the 1.5 GB/lane estimate vs the 22.5 GB
    free target). The env override VODRIP_TRANSCRIBE_WORKERS keeps winning
    over the default."""
    at._multi_tls.pin = ("cpu", "int8")
    monkeypatch.setenv("VODRIP_TRANSCRIBE_WORKERS", "")
    monkeypatch.setattr(at, "background_mode", lambda: True)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)
    try:
        assert at._cpu_auto_workers() == 3, at._cpu_auto_workers()
        plan = at._worker_plan()
        assert plan == [
            ("cpu", "int8"), ("cpu", "int8"), ("cpu", "int8"),
        ], plan
        # Env override still wins in background mode.
        monkeypatch.setenv("VODRIP_TRANSCRIBE_WORKERS", "1")
        assert at._worker_plan() == [("cpu", "int8")], at._worker_plan()
    finally:
        at._multi_tls.pin = None


def test_background_three_lanes_ram_clamped(monkeypatch):
    """3-lane default is RAM-clamped on a tight box (usable free < 3x the
    per-lane estimate) — never overshoots the machine."""
    at._multi_tls.pin = ("cpu", "int8")
    monkeypatch.setenv("VODRIP_TRANSCRIBE_WORKERS", "")
    monkeypatch.setattr(at, "background_mode", lambda: True)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 3 * 1024 ** 3)  # 3 GB free
    try:
        plan = at._worker_plan()
        assert len(plan) <= 1, plan  # usable = 3*0.8 = 2.4 GB -> 1 lane
    finally:
        at._multi_tls.pin = None


def test_held_gpu_model_forces_cpu_lane():
    """nvidia-smi compute-apps shows another process -> never stack."""
    lane, copies, plan = _patched(16, held=True)
    assert copies == 0, copies
    assert plan == [("cpu", "int8"), ("cpu", "int8")], plan


def test_second_copy_needs_idle_gpu_and_vram():
    """Shared model: _gpu_copies() always returns 0 or 1, regardless of
    GPU_COPIES env or VRAM headroom."""
    _, copies, _ = _patched(16, util=0.85, gpu_copies="3")
    assert copies == 1, copies
    _, copies, plan = _patched(16, util=0.3, gpu_copies="3")
    assert copies == 1, copies
    assert plan[0] == ("cuda", "int8"), plan
    _, copies, plan = _patched(32, util=0.3, gpu_copies="3")
    assert copies == 1, copies
    assert plan[0] == ("cuda", "int8"), plan


def test_vram_estimate_fixed_parakeet():
    """The whisper model ladder is gone: one constant 2 GiB parakeet VRAM
    estimate + 1 GiB headroom = 3 GiB per GPU copy."""
    assert at._gpu_model_vram_est() == at._PARAKEET_GPU_VRAM_EST
    assert at._gpu_model_vram_est() + at._GPU_VRAM_HEADROOM == 3 * 1024 ** 3


def _fake_tasklist(stdout, mine="12345"):
    """Stub sp.run so _gpu_held_by_other parses a fake tasklist payload."""
    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(at.os, "getpid", lambda: int(mine))
    mp.setattr(
        at.sp, "run",
        lambda *a, **k: type("FakeOut", (), {
            "returncode": 0, "stdout": stdout, "stderr": "",
        })(),
    )
    try:
        at._gpu_held_at = 0.0  # bust the 10 s cache
        return at._gpu_held_by_other()
    finally:
        mp.undo()


def test_gpu_held_ignores_wddm_processes():
    """Windows nvidia-smi compute-apps lists every WDDM GPU touch with
    memory [N/A] — those are not CUDA tenants and must not trip the gate."""
    wddm_only = (
        "2168, [N/A]\n"
        "13024, [N/A]\n"
        "8168, [N/A]\n"
        "20384, [N/A]\n"
    )
    assert _fake_tasklist(wddm_only) is False


def test_gpu_held_counts_cuda_loader_other_pid():
    """A python with a real CUDA allocation (ComfyUI) -> held, never stack."""
    with_cuda = "27004, 4096\n"
    assert _fake_tasklist(with_cuda, mine="99999") is True


def test_gpu_held_ignores_own_pid():
    """The worker's own CUDA allocation must not count as 'other'."""
    own_only = "12345, 4096\n"
    assert _fake_tasklist(own_only, mine="12345") is False


def test_run_worker_swaps_pool_when_gpu_frees():
    """plan-watch turns a CPU-only worker GPU-on without restart.

    _pool_plan returns CPU-only first (GPU held), then hybrid once the GPU
    frees — run_worker must create a SECOND pool pinned to the CUDA plan,
    fully drain and close the old executor before creating the replacement,
    and close the replacement on exit."""
    import threading
    import time as _time

    import pytest

    mp = pytest.MonkeyPatch()
    state = {"i": 0}
    calls = []
    lifecycle = []

    def _fake_plan(_mw):
        if state["i"] < 2:  # initial plan + one watch pass: CPU-only
            state["i"] += 1
            return [("cpu", "int8"), ("cpu", "int8")]
        return [("cuda", "int8"), ("cpu", "int8")]  # GPU freed: hybrid

    real_tpe = at.ThreadPoolExecutor

    class RecordingTPE(real_tpe):
        def __init__(self, *a, **kw):
            calls.append((kw.get("max_workers"), kw.get("initargs")))
            lifecycle.append(("create", id(self)))
            super().__init__(*a, **kw)

        def shutdown(self, wait=True, *, cancel_futures=False):
            lifecycle.append(("shutdown", id(self), wait))
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    mp.setattr(at, "_pool_plan", _fake_plan)
    mp.setattr(at, "ThreadPoolExecutor", RecordingTPE)
    mp.setattr(at, "_PLAN_RECHECK_S", 0.05)
    mp.setattr(at, "_claim_next_job", lambda: None)
    mp.setattr(at, "_maybe_close_idle_model", lambda: None)
    mp.setattr(at, "close_model", lambda: None)
    mp.setattr(at, "_parakeet_cuda_available", lambda: False)
    mp.setattr(at.archive_db, "worker_heartbeat", lambda *a, **k: None)
    # Bypass the Windows named mutex (Local\VODRIP.archive-transcribe) that
    # blocks when another worker process already holds it.
    mp.setattr(at, "_transcription_worker_owner", contextlib.nullcontext)
    try:
        t = threading.Thread(
            target=at.run_worker,
            kwargs={"once": False, "poll_interval": 0.01},
            daemon=True,
        )
        t.start()
        deadline = _time.monotonic() + 5.0
        while len(calls) < 2 and _time.monotonic() < deadline:
            _time.sleep(0.01)
        at._WORKER_STOP.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_worker did not stop after _WORKER_STOP"
    finally:
        at._WORKER_STOP.clear()
        mp.undo()

    assert len(calls) == 2, calls
    budget0, initargs0 = calls[0]
    assert budget0 == 2 and initargs0[0] == [
        ("cpu", "int8"), ("cpu", "int8"),
    ], calls[0]

    assert [event[0] for event in lifecycle] == [
        "create", "shutdown", "create", "shutdown",
    ], lifecycle
    assert all(event[-1] is True for event in lifecycle if event[0] == "shutdown"), lifecycle
    assert lifecycle[0][1] != lifecycle[2][1], lifecycle
    budget1, initargs1 = calls[1]
    assert budget1 == 2 and initargs1[0] == [
        ("cuda", "int8"), ("cpu", "int8"),
    ], calls[1]
    assert len(initargs1) == 1, "pool initargs carry only the plan (lane model gone)"
