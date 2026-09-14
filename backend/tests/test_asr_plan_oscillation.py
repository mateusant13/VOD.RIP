"""Anti-oscillation regression for the ASR device-plan selection (b3a2b45).

The 'vodapi wedge' class: the transcribe plan watch re-evaluates the device
plan every `_PLAN_RECHECK_S` and, on any difference, drains the in-flight
inference, closes every recognizer and rebuilds the pools. A probe that flips
cuda<->cpu (or a CPU-load sample straddling the clamp threshold) therefore
fires the most expensive action the worker has, repeatedly — and the
drain/rebuild burst biases the next sample, so the loop self-sustains and
starves the event loop.

b3a2b45 guarantees three things. This file pins them end-to-end through the
REAL planner (`_pool_plan` -> `_worker_plan`) and the REAL `_plan_watch` loop
inside `run_worker`:

  1. Debounce (`_plan_debounce_step`, `_PLAN_CONFIRM_OBS = 2`): a differing
     plan must be observed `_PLAN_CONFIRM_OBS` times IN A ROW before it is
     proposed. Perfectly alternating input never produces two identical
     differing observations -> exactly zero plan switches, however long the
     thrash lasts.
  2. Schmitt band on the CPU-load probe (`_cpu_load_high`, enter 0.70 / exit
     0.60): load hovering at the cutoff cannot flip the probe's own output, so
     the plan input never changes at all.
  3. Sustained truth still gets through: the filters DELAY a real device
     change, they do not veto it (one switch, exactly once).

Seams used (all pre-existing module-level indirections — no production change
was needed to write this test): `_detect_device` (the GPU probe),
`_measure_cpu_load` (the load probe), plus the leaf VRAM/RAM/thread-count
probes stubbed only to keep the real planner deterministic and off real
hardware. Timing: `_PLAN_CONFIRM_OBS` and the 0.70/0.60 band stay at production
values; only `_PLAN_RECHECK_S` (30 s -> 0.03 s) and the load readout TTL
(15 s -> 0, which *removes* a layer of protection: every tick re-samples) are
shortened, per the worst-case requirement.
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time

import pytest

from services import archive_transcribe as at

# The device answers, run through the real planner with the stubbed hardware
# probes below (VODRIP_TRANSCRIBE_WORKERS=2, 4-thread CPU budget, 8 GiB free
# VRAM, 64 GiB free RAM): a CUDA host plans 1 GPU lane + 2 CPU lanes, a CPU
# host plans 2 CPU lanes, and the CPU-load clamp folds the CUDA plan to 1+1.
CUDA_PLAN = [("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8")]
CUDA_PLAN_CLAMPED = [("cuda", "int8"), ("cpu", "int8")]
CPU_PLAN = [("cpu", "int8"), ("cpu", "int8")]

_GIB = 1024**3
_FREE_VRAM = 8 * _GIB   # above _GPU_MIN_FREE_VRAM -> the GPU lane is live
_FREE_RAM = 64 * _GIB   # never binding for a 2-slot plan
_THREAD_BUDGET = 4
_TICK_S = 0.03          # patched _PLAN_RECHECK_S (production: 30.0)
_POLL_S = 0.01


# --- fake GPU probe ----------------------------------------------------------

class DeviceProbe:
    """Worst-case device probe: alternates cuda/cpu on every single call.

    `hold` freezes the answer, modelling a device that genuinely went away
    (cuda dead) rather than one that is merely being mis-read."""

    def __init__(self, first: str) -> None:
        self._lock = threading.Lock()
        self._next = first
        self._hold: str | None = None
        self.calls = 0
        self.answered: list[str] = []

    def __call__(self) -> tuple[str, str]:
        with self._lock:
            self.calls += 1
            device = self._next
            self.answered.append(device)
            self._next = self._hold if self._hold is not None else (
                "cpu" if device == "cuda" else "cuda"
            )
            return device, "int8"

    def hold(self, device: str) -> None:
        with self._lock:
            self._hold = self._next = device


class LoadProbe:
    """Fake `_measure_cpu_load`: a one-shot prefix, then a STRICT periodic
    cycle. The period is exact on purpose — a sloppy wrap would hand the watch
    two consecutive identical samples and let the debounce confirm, which is a
    fake-probe artifact, not a production bug."""

    def __init__(self, prefix: list[float] = (), cycle: list[float] | None = None) -> None:
        self._lock = threading.Lock()
        self._prefix = list(prefix)
        self._cycle = list(cycle) if cycle else []
        self.calls = 0

    def __call__(self) -> float:
        with self._lock:
            i = self.calls
            self.calls += 1
            if i < len(self._prefix):
                return self._prefix[i]
            if not self._cycle:
                return self._prefix[-1] if self._prefix else 0.10
            j = i - len(self._prefix)
            return self._cycle[j % len(self._cycle)]


# --- worker harness ----------------------------------------------------------

class Harness:
    """Runs the real `run_worker` with faked probes and counted pool swaps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mp = monkeypatch
        self.pools: list[list[tuple[str, str]]] = []
        self.swaps: list[str] = []
        self._thread: threading.Thread | None = None
        self._handler: logging.Handler | None = None
        self._saved_level: int | None = None
        self._saved_load_state: tuple | None = None

    # -- planner inputs: hardware probes only, never the decision logic ------
    def _patch_planner_inputs(self, *, device_probe, load_probe=None) -> None:
        mp = self.mp
        mp.setattr(at, "_detect_device", device_probe)
        mp.setattr(at, "_measure_cpu_load", load_probe or (lambda: 0.10))
        # The 15 s load readout cache is a third anti-oscillation layer;
        # collapse it so every tick re-samples — worst case, band-only defense.
        mp.setattr(at, "_CPU_LOAD_TTL_S", 0.0)
        mp.setenv(at.WORKERS_ENV, "2")
        mp.setattr(at, "_GOVERNOR_AVAILABLE", False)
        mp.setattr(at, "_parakeet_cuda_ok", True)
        mp.setattr(at, "_parakeet_cuda_available", lambda: True)
        mp.setattr(at, "caption_session_active", lambda: False)
        mp.setattr(at, "_gpu_held_by_other", lambda: False)
        mp.setattr(at, "_gpu_free_vram_bytes", lambda: _FREE_VRAM)
        mp.setattr(at, "_free_system_ram_bytes", lambda: _FREE_RAM)
        mp.setattr(at, "_cpu_thread_budget", lambda: _THREAD_BUDGET)
        mp.setattr(at, "_PLAN_RECHECK_S", _TICK_S)
        # worker-loop plumbing that must not touch disk or the shared DB
        mp.setattr(at, "_reap_stale_shard_dirs", lambda *a, **k: None)
        mp.setattr(at, "_claim_next_job", lambda: None)
        mp.setattr(at, "_maybe_close_idle_model", lambda: None)
        mp.setattr(at, "close_model", lambda: None)
        mp.setattr(at, "_rss_cap_bytes", lambda: 0)
        mp.setattr(at, "_transcription_worker_owner", contextlib.nullcontext)
        # isolate the process-global band state (restored by stop())
        self._saved_load_state = (at._cpu_load_high_cache, at._cpu_load_at)
        at._cpu_load_high_cache = False
        at._cpu_load_at = 0.0

    def arm(self, device: str = "cuda") -> DeviceProbe:
        probe = DeviceProbe(device)
        self._patch_planner_inputs(device_probe=probe)
        return probe

    def arm_load(self, load: LoadProbe, device: str = "cuda") -> LoadProbe:
        fixed = DeviceProbe(device)
        fixed.hold(device)
        self._patch_planner_inputs(device_probe=fixed, load_probe=load)
        return load

    # -- observable outputs --------------------------------------------------
    def _count_swaps(self) -> None:
        real_make_pool = at._make_pool

        def counting_make_pool(plan, budget):
            self.pools.append(list(plan))
            return real_make_pool(plan, budget)

        self.mp.setattr(at, "_make_pool", counting_make_pool)

        swaps = self.swaps

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                msg = record.getMessage()
                if "plan changed" in msg:
                    swaps.append(msg)

        logger = at.logger
        self._saved_level = logger.level
        self._handler = _Capture()
        logger.addHandler(self._handler)
        logger.setLevel(logging.INFO)

    def start(self) -> None:
        self._count_swaps()
        at._WORKER_STOP.clear()
        self._thread = threading.Thread(
            target=at.run_worker,
            kwargs={"once": False, "poll_interval": _POLL_S},
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        at._WORKER_STOP.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            assert not self._thread.is_alive(), "run_worker did not stop"
            self._thread = None
        if self._handler is not None:
            at.logger.removeHandler(self._handler)
            at.logger.setLevel(self._saved_level)
            self._handler = None
        if self._saved_load_state is not None:
            at._cpu_load_high_cache, at._cpu_load_at = self._saved_load_state
            self._saved_load_state = None
        # leave the module-global stop flag as start() found it, so a later
        # worker test in the same session is not born already-stopped
        at._WORKER_STOP.clear()

    def wait_for(self, predicate, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        assert predicate(), f"timed out after {timeout}s waiting for {what}"


@pytest.fixture()
def harness(monkeypatch):
    h = Harness(monkeypatch)
    yield h
    h.stop()


@pytest.fixture()
def _clean_load_cache():
    saved = (at._cpu_load_high_cache, at._cpu_load_at)
    at._cpu_load_high_cache = False
    at._cpu_load_at = 0.0
    yield
    at._cpu_load_high_cache, at._cpu_load_at = saved


# --- precondition: the fake really does thrash the REAL planner --------------

def test_alternating_device_probe_really_alternates_the_planner(
    monkeypatch, _clean_load_cache
):
    """The input is genuinely worst case, not a vacuous stub: driving the
    production `_pool_plan(None)` with the alternating probe must hand the
    watch a plan that differs from the previous one on EVERY tick."""
    h = Harness(monkeypatch)
    h._patch_planner_inputs(device_probe=DeviceProbe("cuda"))
    try:
        observed = [at._pool_plan(None) for _ in range(6)]
    finally:
        h.stop()
    assert observed == [
        CUDA_PLAN, CPU_PLAN, CUDA_PLAN, CPU_PLAN, CUDA_PLAN, CPU_PLAN,
    ], observed
    assert all(a != b for a, b in zip(observed, observed[1:])), (
        "the planner must thrash on every tick, else the zero-switch asserts "
        "below would pass for the wrong reason"
    )


def test_load_duty_cycle_alternates_only_without_the_band(monkeypatch, _clean_load_cache):
    """The 0.71/0.68/0.68 threshold duty cycle (readout cache collapsed, so
    every tick re-samples): the banded compare never leaves HIGH — once HIGH,
    0.68 >= 0.60 keeps it HIGH. The pre-fix plain `>= 0.70` compare flips
    three times over the same six samples, and its differing runs are two
    ticks long: exactly `_PLAN_CONFIRM_OBS`. That is what makes the band
    load-bearing here rather than redundant with the debounce."""
    monkeypatch.setattr(at, "_CPU_LOAD_TTL_S", 0.0)
    load = LoadProbe(prefix=[0.71], cycle=[0.68, 0.68, 0.71])
    monkeypatch.setattr(at, "_measure_cpu_load", load)
    banded = [at._cpu_load_high() for _ in range(6)]
    assert banded == [True] * 6, banded

    monkeypatch.setattr(at, "_CPU_LOAD_HIGH_EXIT", at._CPU_LOAD_HIGH)  # collapse the band
    at._cpu_load_high_cache = False
    load.calls = 0
    unbanded = [at._cpu_load_high() for _ in range(6)]
    assert unbanded == [True, False, False, True, False, False], unbanded
    longest_run = max(
        len(list(g)) for _, g in itertools.groupby(unbanded)
    )
    assert longest_run >= at._PLAN_CONFIRM_OBS, (
        "unbanded chatter must produce a confirmation-length run, else the "
        "band test below proves nothing about the band"
    )


# --- 1. cuda<->cpu thrash every tick: zero switches --------------------------

def test_alternating_device_never_switches_the_plan(harness):
    """(a)+(b): N ticks of perfect cuda/cpu alternation -> zero pool swaps and
    zero 'plan changed' events. Confirmation needs 2 consecutive identical
    differing observations; alternation can never produce one."""
    probe = harness.arm("cuda")
    harness.start()
    # >= 8 full alternation cycles: 4x the confirmation window, every tick
    harness.wait_for(lambda: probe.calls >= 16, 4.0, "the watch to take 16 ticks")
    time.sleep(2 * _TICK_S)
    assert probe.calls >= 16, "the watch never ran — a zero-switch pass would be vacuous"
    assert set(probe.answered) == {"cuda", "cpu"}, (
        f"the probe must have answered both ways; got {sorted(probe.answered)}"
    )
    assert len(harness.pools) == 1, (
        f"alternating device must not swap pools; built {harness.pools}"
    )
    assert harness.swaps == [], f"plan-change events published: {harness.swaps}"


def test_sustained_device_change_still_switches(harness):
    """(c) feature sensitivity: after the same thrash, freezing the probe on
    the opposite device DOES switch the pool — exactly once. Red here while
    the test above stays green means the filter became a veto."""
    probe = harness.arm("cuda")
    harness.start()
    harness.wait_for(lambda: probe.calls >= 12, 4.0, "the thrash phase")
    assert len(harness.pools) == 1, "the thrash phase must not have switched"

    probe.hold("cpu")
    harness.wait_for(lambda: len(harness.pools) > 1, 4.0, "the sustained cuda->cpu swap")
    time.sleep(6 * _TICK_S)  # more held ticks: it must publish ONCE

    assert harness.pools[0] == CUDA_PLAN, harness.pools
    assert harness.pools[1] == CPU_PLAN, harness.pools
    assert len(harness.pools) == 2, f"a sustained device switches once; {harness.pools}"
    assert len(harness.swaps) == 1, harness.swaps


# --- 2. the CPU-load clamp: same contract, other input -----------------------

def test_load_chatter_at_the_threshold_never_switches_the_plan(harness):
    """Load hovering at the 0.70 cutoff (first sample HIGH, then 0.68 forever):
    the band keeps the probe HIGH, so the observed plan never differs from the
    live one and the debounce is not even needed."""
    load = harness.arm_load(LoadProbe(prefix=[0.71], cycle=[0.68, 0.68]))
    harness.start()
    harness.wait_for(lambda: load.calls >= 12, 4.0, "12 load samples")
    time.sleep(2 * _TICK_S)

    assert harness.pools[0] == CUDA_PLAN_CLAMPED, harness.pools
    assert len(harness.pools) == 1, f"threshold chatter must not swap pools; {harness.pools}"
    assert harness.swaps == [], harness.swaps


def test_alternating_load_beyond_the_band_is_debounced(harness):
    """Load wide enough to punch through the band (0.72 <-> 0.55 crosses BOTH
    thresholds, so the probe genuinely alternates every tick and the observed
    plan alternates with it) still must not swap: the debounce is the
    plan-level filter and it holds where the band cannot help."""
    load = harness.arm_load(LoadProbe(prefix=[0.72], cycle=[0.55, 0.72]))
    harness.start()
    harness.wait_for(lambda: load.calls >= 12, 4.0, "12 load samples")
    time.sleep(2 * _TICK_S)

    assert harness.pools[0] == CUDA_PLAN_CLAMPED, harness.pools
    assert len(harness.pools) == 1, f"alternating load must not swap pools; {harness.pools}"
    assert harness.swaps == [], harness.swaps


def test_sustained_load_clamp_still_switches(harness):
    """The other side of the same coin: load that truly enters the band (stuck
    at 0.72) switches the plan exactly once."""
    load = harness.arm_load(LoadProbe(prefix=[0.55], cycle=[0.72]))
    harness.start()
    harness.wait_for(lambda: len(harness.pools) > 1, 4.0, "the sustained high-load swap")
    time.sleep(4 * _TICK_S)

    assert harness.pools[0] == CUDA_PLAN, harness.pools
    assert harness.pools[1] == CUDA_PLAN_CLAMPED, harness.pools
    assert len(harness.pools) == 2, f"expected one swap; built {harness.pools}"


# --- 3. permanent teeth: the harness catches a weakened filter ---------------

def test_teeth_collapsed_band_lets_load_chatter_switch_the_plan(harness):
    """Identical input to `test_load_chatter_at_the_threshold_never_switches_the_plan`
    with the Schmitt band collapsed to the pre-fix `>= _CPU_LOAD_HIGH` compare.
    The probe now alternates with runs of two, which clears the debounce, and
    the pool DOES swap — so the passing test above pins the band, not luck."""
    harness.mp.setattr(at, "_CPU_LOAD_HIGH_EXIT", at._CPU_LOAD_HIGH)
    harness.arm_load(LoadProbe(prefix=[0.71], cycle=[0.68, 0.68]))
    harness.start()
    harness.wait_for(lambda: len(harness.pools) > 1, 4.0, "a swap under the collapsed band")
    assert harness.swaps, "a collapsed band must surface as a published plan change"


def test_teeth_single_tick_confirm_lets_device_thrash_switch_the_plan(harness):
    """Identical input to `test_alternating_device_never_switches_the_plan` with
    `_PLAN_CONFIRM_OBS` back to the pre-fix 1: every tick confirms, so the
    pools thrash."""
    harness.mp.setattr(at, "_PLAN_CONFIRM_OBS", 1)
    harness.arm("cuda")
    harness.start()
    harness.wait_for(lambda: len(harness.pools) > 1, 4.0, "a swap under 1-tick confirm")
    assert harness.swaps, "a 1-tick debounce must surface as a published plan change"


# --- 4. the constants the guarantee is stated in -----------------------------

def test_production_timing_constants_are_pinned():
    """The ~60 s confirmation window is the product of two constants, and the
    band is a pair of thresholds; a silent retune of any of them changes the
    anti-oscillation contract, so pin them."""
    assert at._PLAN_CONFIRM_OBS == 2
    assert at._PLAN_RECHECK_S == 30.0
    assert (at._CPU_LOAD_HIGH, at._CPU_LOAD_HIGH_EXIT) == (0.70, 0.60)
    assert at._CPU_LOAD_TTL_S == 15.0
    assert at._CPU_LOAD_HIGH_EXIT < at._CPU_LOAD_HIGH, "the band must have width"
