"""Plan-watch debounce + CPU-load Schmitt band (the ASR plan-oscillation fix).

Two temporal filters were missing between a noisy plan input and the most
destructive action the transcribe worker can take (drain in-flight inference +
close_model + rebuild pools, once per flip at the 30 s recheck cadence):

  1. `_plan_debounce_step` (used by `_plan_watch`): a differing plan must be
     observed `_PLAN_CONFIRM_OBS` times in a row before it is proposed. The
     helper is PURE — no threads, no sleeps, no IO — so the gate is pinned
     here instead of by racing a daemon thread.
  2. `_cpu_load_high`: the 0.2 s sample enters the HIGH band at
     `_CPU_LOAD_HIGH` (0.70) and only exits below `_CPU_LOAD_HIGH_EXIT`
     (0.60). This is the specific input the sweep saw flip the plan.

Zero file IO by design (scratch dirs are not needed); the only process-global
state touched is the load cache, restored by fixture.
"""
from __future__ import annotations

import contextlib

import pytest

from services import archive_transcribe as at

BASE = [("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8")]
ALT = [("cuda", "int8"), ("cpu", "int8")]


@pytest.fixture()
def _clean_load_cache():
    """Isolate (and restore) the module-global load cache/band state."""
    saved = (at._cpu_load_high_cache, at._cpu_load_at)
    at._cpu_load_high_cache = False
    at._cpu_load_at = 0.0
    yield
    at._cpu_load_high_cache, at._cpu_load_at = saved


# --- 1. debounce gate --------------------------------------------------------

def test_confirm_gate_requires_two_observations():
    """[base, differ, differ]: published ONLY on the second consecutive one.

    Pre-fix the watch published on the FIRST differing observation — the
    None at index 1 below is exactly that regression."""
    steps = []
    last, streak = None, 0  # (last_proposal, streak)
    for observed in (BASE, ALT, ALT):
        publish, last, streak = at._plan_debounce_step(BASE, last, streak, observed)
        steps.append(publish)
    assert steps == [None, None, ALT], steps


def test_first_differ_does_not_publish():
    """The regression itself: ONE differing observation must not swap pools."""
    proposal, last, streak = at._plan_debounce_step(BASE, None, 0, ALT)
    assert proposal is None, "single differing observation must not publish"
    assert last == ALT and streak == 1, (last, streak)


def test_second_differ_publishes_once_and_clears_streak():
    """The confirming observation publishes and clears the streak, so the same
    evidence never double-publishes on the next recheck (a surviving stale
    proposal is a no-op: the swap block already guards new_plan != plan)."""
    _, last, streak = at._plan_debounce_step(BASE, None, 0, ALT)
    publish, last, streak = at._plan_debounce_step(BASE, last, streak, ALT)
    assert publish == ALT, publish
    assert last is None and streak == 0, (last, streak)
    publish2, last2, streak2 = at._plan_debounce_step(BASE, last, streak, ALT)
    assert publish2 is None, "cleared streak must re-confirm, not republish"
    assert last2 == ALT and streak2 == 1, (last2, streak2)


def test_matching_observation_resets_streak():
    """differ -> match -> differ never reaches 2 consecutive, so a blip that
    straddles a normal observation cannot swap the pool."""
    _, last, streak = at._plan_debounce_step(BASE, None, 0, ALT)
    publish, last, streak = at._plan_debounce_step(BASE, last, streak, BASE)
    assert publish is None and last is None and streak == 0, (publish, last, streak)
    publish, last, streak = at._plan_debounce_step(BASE, last, streak, ALT)
    assert publish is None, "post-reset single differ must not publish"
    assert last == ALT and streak == 1, (last, streak)


def test_alternating_proposals_restart_the_streak():
    """Two DIFFERENT proposals in a row are not confirmation of either — the
    streak must belong to one identical observation."""
    publish, last, streak = at._plan_debounce_step(BASE, None, 0, ALT)
    assert publish is None and last == ALT
    other = [("cpu", "int8")]
    publish, last, streak = at._plan_debounce_step(BASE, last, streak, other)
    assert publish is None and last == other and streak == 1, (publish, last, streak)
    publish, _, _ = at._plan_debounce_step(BASE, last, streak, other)
    assert publish == other, "second consecutive identical `other` confirms"


def test_no_plan_published_yet_never_proposes():
    """current_plan is None before the first publish: every observation is the
    initial plan, not a change."""
    publish, last, streak = at._plan_debounce_step(None, None, 0, ALT)
    assert publish is None and last is None and streak == 0, (publish, last, streak)


def test_gate_constant_is_two():
    """The cadence contract the fix depends on (2 x 30 s recheck = ~60 s)."""
    assert at._PLAN_CONFIRM_OBS == 2


# --- 2. CPU-load Schmitt band -------------------------------------------------

def _sample(monkeypatch, load: float) -> bool:
    """One fresh band evaluation: force the 15 s cache to expire, feed `load`
    as the 0.2 s sample, return _cpu_load_high()'s decision."""
    at._cpu_load_at = 0.0
    monkeypatch.setattr(at, "_measure_cpu_load", lambda: load)
    return at._cpu_load_high()


def test_schmitt_enter_exit_band(_clean_load_cache, monkeypatch):
    """0.70 enters HIGH; 0.65/0.61 keep HIGH (a plain >=0.70 compare would
    have dropped them and re-drawn the CPU lane); only <0.60 exits, and the
    next band needs a fresh 0.70."""
    assert _sample(monkeypatch, 0.70) is True, "0.70 enters the high band"
    assert _sample(monkeypatch, 0.65) is True, "0.65 must stay high (hysteresis)"
    assert _sample(monkeypatch, 0.61) is True, "0.61 must stay high (hysteresis)"
    assert _sample(monkeypatch, 0.599) is False, "load under 0.60 exits the band"
    assert _sample(monkeypatch, 0.68) is False, "0.68 while low stays low (needs 0.70)"


def test_band_is_stateful_across_cache_expiry(_clean_load_cache, monkeypatch):
    """The band decision survives cache expiry — the pre-fix behavior
    (threshold-only compare) flipped on every fresh 0.2 s sample."""
    assert at._CPU_LOAD_HIGH_EXIT == 0.60 and at._CPU_LOAD_HIGH == 0.70
    monkeypatch.setattr(at, "_measure_cpu_load", lambda: 0.65)
    at._cpu_load_high_cache = True  # in-band from a previous high sample
    at._cpu_load_at = 0.0           # force a fresh measurement
    assert at._cpu_load_high() is True, "0.65 while already high must stay high"
    at._cpu_load_high_cache = False
    at._cpu_load_at = 0.0
    assert at._cpu_load_high() is False, "0.65 while low must stay low"


def test_cache_ttl_short_circuits_measurement(_clean_load_cache, monkeypatch):
    """The 15 s cache path is preserved: a fresh sample is not taken while the
    cached band decision is valid."""
    calls = []
    monkeypatch.setattr(at, "_measure_cpu_load", lambda: calls.append(1) or 0.80)
    assert at._cpu_load_high() is True
    assert at._cpu_load_high() is True
    assert len(calls) == 1, "second call inside the TTL must not re-sample"


# --- wiring: the real _plan_watch loop through run_worker --------------------

def test_watch_loop_does_not_publish_alternating_observations(monkeypatch):
    """LOGIC-06: drives the ACTUAL `_plan_watch` closure, not the pure helper.

    `_pool_plan` alternates BASE/ALT every call, so no differing observation
    is ever confirmed twice in a row. Correct wiring (carry last_proposal +
    streak across iterations, pass the *live* plan as current_plan) must never
    publish -> exactly one pool. A mis-wire that publishes on the first differ
    (pre-fix) or drops the carried streak swaps the pool immediately and fails.
    The pure-helper tests alone stay green under both mis-wires; this one
    cannot."""
    import threading
    import time as _time

    cycle = iter([BASE, ALT] * 300)
    pools = []
    real_tpe = at.ThreadPoolExecutor

    class CountingTPE(real_tpe):
        def __init__(self, *a, **kw):
            pools.append(kw.get("initargs"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(at, "_pool_plan", lambda _mw: next(cycle))
    monkeypatch.setattr(at, "ThreadPoolExecutor", CountingTPE)
    monkeypatch.setattr(at, "_PLAN_RECHECK_S", 0.02)
    monkeypatch.setattr(at, "_claim_next_job", lambda: None)
    monkeypatch.setattr(at, "_maybe_close_idle_model", lambda: None)
    monkeypatch.setattr(at, "close_model", lambda: None)
    monkeypatch.setattr(at, "_transcription_worker_owner", contextlib.nullcontext)
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: False)
    at._WORKER_STOP.clear()
    try:
        t = threading.Thread(
            target=at.run_worker,
            kwargs={"once": False, "poll_interval": 0.01},
            daemon=True,
        )
        t.start()
        deadline = _time.monotonic() + 4.0
        # let the watch complete >= 2 full BASE/ALT cycles, then stop
        while _time.monotonic() < deadline:
            _time.sleep(0.3)
            if len(pools) > 1:
                break  # mis-wire: published a single differing observation
        at._WORKER_STOP.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "run_worker did not stop after _WORKER_STOP"
    finally:
        at._WORKER_STOP.clear()
    assert len(pools) == 1, (
        f"alternating (never twice-confirmed) observations must not swap pools; "
        f"got {len(pools)}"
    )



# --- probe failure keeps the documented fail-open contract -------------------

def test_failed_probe_exits_high_band_fail_open(monkeypatch, _clean_load_cache):
    """LOGIC-04: `_measure_cpu_load` signals failure as 0.0 (idle), not unknown,
    so the banded compare EXITS a sticky HIGH band on a failed read. Pin the
    fail-open semantics the docstring now states (unmeasurable -> no clamp); a
    future 'preserve last decision' change must update this test on purpose."""
    monkeypatch.setattr(at, "_measure_cpu_load", lambda: 0.0)
    at._cpu_load_high_cache = True  # previously HIGH
    at._cpu_load_at = 0.0           # force a fresh evaluation
    assert at._cpu_load_high() is False, (
        "failed read (0.0) must lift the clamp — fail-open by design"
    )
