"""GPU capability ladder unit tests — simulated free-VRAM tiers.

The lane planner (services.archive_transcribe._gpu_lane_plan / _gpu_copies /
_worker_plan) is a PURE function of the measured 60 s-median free-VRAM
allowance + the nvidia-smi compute-apps/util readouts. No real hardware, no
network, no model load: every probe is patched, so the whole ladder (6 ->
32 GiB cards + the sub-2 GiB floor) is pinned without a GPU.

Tiers (user requirement):
  >= 6.5 GiB -> active model fp16 (turbo fp16 on defaults); 2nd copy only
                when util < 70% AND the allowance fits ~2x
  >= 3.5 GiB -> active model int8 (the 6-8 GiB card sweet spot)
  >= 2.0 GiB -> medium int8 (entry cards)
  <  2.0 GiB -> CPU lane only (1-2 int8 copies by cores/RAM)
The CPU lane exists at EVERY tier. int8 is the default precision; fp16 only
when VRAM clearly allows.
"""
import os
from importlib import reload

from services import archive_transcribe as at


def _patched(
    free_vram_gb, *, held=False, util=None, workers="2", gpu_copies="1"
):
    """Patch the planner's probes and return (lane, copies, plan)."""
    at._gpu_free_vram_bytes = lambda: int(free_vram_gb * 1024 ** 3)
    at._gpu_held_by_other = lambda: held
    at._gpu_util = lambda: util
    at._cpu_load_high = lambda: False
    at._free_system_ram_bytes = lambda: 64 * 1024 ** 3  # RAM never binds
    at._device_override = ("cuda", "float16")
    at._worker_lane_model = None
    at.model_name = lambda: "large-v3-turbo"  # pin est=6 GiB -> per-copy 8 GiB
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
        at._device_override = None


def test_ladder_32gb_fp16():
    lane, copies, plan = _patched(32)
    assert lane == (None, "float16"), lane
    assert copies == 1, copies
    assert plan == [("cuda", "float16"), ("cpu", "int8"), ("cpu", "int8")], plan


def test_ladder_16gb_fp16():
    lane, copies, plan = _patched(16)
    assert lane == (None, "float16"), lane
    assert plan[0] == ("cuda", "float16"), plan


def test_ladder_8gb_fp16():
    lane, copies, plan = _patched(8)
    assert lane == (None, "float16"), lane
    assert plan[0] == ("cuda", "float16"), plan


def test_ladder_6gb_int8_sweet_spot():
    lane, copies, plan = _patched(6)
    assert lane == (None, "int8"), lane
    assert plan[0] == ("cuda", "int8"), plan


def test_ladder_3gb_medium_int8_entry():
    lane, copies, plan = _patched(3)
    assert lane == ("medium", "int8"), lane
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


def test_held_gpu_model_forces_cpu_lane():
    """nvidia-smi compute-apps shows another process -> never stack."""
    lane, copies, plan = _patched(16, held=True)
    assert copies == 0, copies
    assert plan == [("cpu", "int8"), ("cpu", "int8")], plan


def test_second_copy_needs_idle_gpu_and_vram():
    """util >= 70% caps copies at 1; idle + ample VRAM allows 2+ copies.

    Per-copy VRAM budget is model_est (6 GiB for turbo) + 2 GiB headroom:
    16 GiB fits 2 copies, 32 GiB fits the configured 3."""
    _, copies, _ = _patched(16, util=0.85, gpu_copies="3")
    assert copies == 1, copies
    _, copies, plan = _patched(16, util=0.3, gpu_copies="3")
    assert copies == 2, copies
    assert plan == [
        ("cuda", "float16"), ("cuda", "float16"),
        ("cpu", "int8"), ("cpu", "int8"),
    ], plan
    _, copies, plan = _patched(32, util=0.3, gpu_copies="3")
    assert copies == 3, copies
    assert plan == [
        ("cuda", "float16"), ("cuda", "float16"), ("cuda", "float16"),
        ("cpu", "int8"), ("cpu", "int8"),
    ], plan


def test_vram_estimate_by_model_name():
    at._worker_lane_model = None
    for name, gb in (
        ("large-v3-turbo", 6.0), ("large-v3", 10.0), ("medium", 5.0),
        ("small", 2.0), ("base", 1.0), ("tiny", 0.6), ("weird-name", 6.0),
    ):
        try:
            at.model_name = lambda _n=name: _n
            est = at._gpu_model_vram_est()
            assert abs(est / 1024 ** 3 - gb) < 0.01, (name, est)
        finally:
            reload(at)


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
    """Windows nvidia-smi compute-apps lists every WDDM GPU touch (dwm,
    explorer, browsers) — the gate must only count nvcuda.dll loaders."""
    wddm_only = """Nome da imagem    Identifi M\u00f3dulos
================= ======== ============================================
dwm.exe              2168 [N/A]
explorer.exe         13024 [N/A]
chrome.exe           8168 [N/A]
Discord.exe          20384 [N/A]
"""
    assert _fake_tasklist(wddm_only) is False


def test_gpu_held_counts_cuda_loader_other_pid():
    """A python holding nvcuda.dll (ComfyUI/BrandOps) -> held, never stack."""
    with_cuda = """Nome da imagem    Identifi M\u00f3dulos
================= ======== ============================================
python.exe           27004 nvcuda.dll
"""
    assert _fake_tasklist(with_cuda, mine="99999") is True


def test_gpu_held_ignores_own_pid():
    """The worker's own nvcuda.dll load must not count as 'other'."""
    own_only = """Nome da imagem    Identifi M\u00f3dulos
================= ======== ============================================
python.exe           12345 nvcuda.dll
"""
    assert _fake_tasklist(own_only, mine="12345") is False


def test_run_worker_swaps_pool_when_gpu_frees():
    """plan-watch turns a CPU-only worker GPU-on without restart.

    _pool_plan returns CPU-only first (GPU held), then hybrid once the GPU
    frees — run_worker must create a SECOND pool pinned to the CUDA plan
    and drain the old one, instead of keeping the static CPU plan for the
    life of the process."""
    import threading
    import time as _time

    import pytest

    mp = pytest.MonkeyPatch()
    state = {"i": 0}
    calls = []

    def _fake_plan(_mw):
        if state["i"] < 2:  # initial plan + one watch pass: CPU-only
            state["i"] += 1
            return [("cpu", "int8"), ("cpu", "int8")]
        return [("cuda", "float16"), ("cpu", "int8")]  # GPU freed: hybrid

    real_tpe = at.ThreadPoolExecutor

    class RecordingTPE(real_tpe):
        def __init__(self, *a, **kw):
            calls.append((kw.get("max_workers"), kw.get("initargs")))
            super().__init__(*a, **kw)

    mp.setattr(at, "_pool_plan", _fake_plan)
    mp.setattr(at, "ThreadPoolExecutor", RecordingTPE)
    mp.setattr(at, "_PLAN_RECHECK_S", 0.05)
    mp.setattr(at, "_claim_next_job", lambda: None)
    mp.setattr(at, "_maybe_close_idle_model", lambda: None)
    mp.setattr(at, "close_model", lambda: None)
    mp.setattr(at, "_gpu_lane_plan", lambda: ("medium", "int8"))
    mp.setattr(at.archive_db, "worker_heartbeat", lambda *a, **k: None)
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
    budget1, initargs1 = calls[1]
    assert budget1 == 2 and initargs1[0] == [
        ("cuda", "float16"), ("cpu", "int8"),
    ], calls[1]
    assert initargs1[1] == "medium"  # lane model pinned on the new pool
    assert at._worker_lane_model is None  # finally reset the pin
