"""Liveness must not depend on readiness.

Measured 2026-10-14: 5 of 40 /api/health probes timed out (12.5% failure rate).
The probe runs four sqlite reads plus a JSONL parse in ONE worker thread, and
those reads can block for the full sqlite busy_timeout (10s) - 4x the dev
supervisor's own 2.5s probe timeout (scripts/dev-all.mjs:215). Three missed
probes in a row and the watchdog taskkills a LIVE api mid-request.

The supervisor only needs the 200; every field in the answer is already
best-effort and documented to degrade to None/False.

RED ARM: change _HEALTH_PROBE_BUDGET_SEC to something above the supervisor
timeout (e.g. 30) and test_budget_is_short_of_the_supervisor_timeout fails.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _budget():
    text = (BACKEND / "routers" / "system.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("_HEALTH_PROBE_BUDGET_SEC"):
            return float(line.split("=")[1].strip())
    raise AssertionError("_HEALTH_PROBE_BUDGET_SEC is not defined in system.py")


def test_budget_is_short_of_the_supervisor_timeout():
    """RED: set the budget to 30 and this fails - the supervisor kills first."""
    supervisor_timeout = 2.5
    budget = _budget()
    assert budget < supervisor_timeout, (
        "health probe budget %.1fs is not under the supervisor probe timeout"
        " %.1fs - three misses and the watchdog kills a live API"
        % (budget, supervisor_timeout)
    )


def test_the_probe_is_wrapped_in_a_deadline():
    """RED: drop the wait_for and the budget is a knob that never fires."""
    text = (BACKEND / "routers" / "system.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for(" in text and "HEALTH_EXECUTOR, _probe" in text
    assert "except asyncio.TimeoutError:" in text


def test_a_timeout_degrades_instead_of_raising():
    """The degraded tuple is the documented contract: None/False, never 500."""
    text = (BACKEND / "routers" / "system.py").read_text(encoding="utf-8")
    i = text.find("except asyncio.TimeoutError:")
    assert i > 0
    tail = text[i: i + 600]
    assert "None, False, False, None, None" in tail
