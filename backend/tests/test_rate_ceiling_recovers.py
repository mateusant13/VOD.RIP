"""The learned rate ceiling must be able to come back up.

Measured 2026-10-13 against the live app (/api/archive/rate-budget):

    youtube: ceiling_rpm 6.797 ... learning: {events: 0, ramp_pending: false,
             seconds_since_event: null}

The ceiling had been learned down (and primed from history) to 6.8 rpm, and it was
NEVER coming back: _ramp_up opened with `if st.events == 0: return`, so a platform
with no events - exactly the clean platform the ramp exists for - returned early
forever. That contradicts the module contract a few lines above it: "After
CLEAN_WINDOW_S with no event, the ceiling grows by _UP_FACTOR".

Consequence, observed live: every YouTube channel walk was refused with "YouTube
rate limit budget still exhausted", the channel endpoint returned 0 YouTube videos,
and the owner could never open a YouTube preview.

The ratchet is the defect. A ceiling that can only fall is not adaptive.
"""
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from services import rate_budget  # noqa: E402


def _fresh_state(platform: str, ceiling: float, *, events: int, age_s: float):
    """A platform state whose last event and last ramp are `age_s` seconds old."""
    r = rate_budget.reset()
    st = rate_budget._state(platform) if hasattr(rate_budget, "_state") else None
    assert st is not None, "rate_budget._state is the seam this test pins"
    now = rate_budget._now()
    st.ceiling_rpm = ceiling
    st.events = events
    st.last_ramp_ts = now - age_s
    st.last_event_ts = now - age_s
    return st


def test_a_clean_platform_ramps_back_up():
    """RED before the fix: events == 0 returned early and the ceiling stayed put."""
    st = _fresh_state("youtube", 6.797, events=0, age_s=rate_budget._CLEAN_WINDOW_S * 3)
    before = st.ceiling_rpm
    rate_budget._ramp_up(st, rate_budget._now())
    assert st.ceiling_rpm > before, (
        f"ceiling stuck at {before} rpm with no events - a ceiling that can only",
        "fall never recovers, and every YouTube channel walk is refused forever"
    )


def test_it_does_not_ramp_inside_the_clean_window():
    """The control: recovery must still be slow, not instant."""
    st = _fresh_state("youtube", 6.797, events=0, age_s=1.0)
    before = st.ceiling_rpm
    rate_budget._ramp_up(st, rate_budget._now())
    assert st.ceiling_rpm == before


def test_it_never_climbs_past_the_hard_cap():
    """The other bound: ramping must not become unbounded growth."""
    st = _fresh_state("youtube", 20.0, events=0, age_s=rate_budget._CLEAN_WINDOW_S * 50)
    rate_budget._ramp_up(st, rate_budget._now())
    assert st.ceiling_rpm <= rate_budget._hard_cap_rpm("youtube")


def test_the_old_early_return_is_gone():
    """Pins the exact line, so a revert is caught by reading, not only by behaviour."""
    src = (BACKEND / "services" / "rate_budget.py").read_text(encoding="utf-8", errors="replace")
    body = src[src.index("def _ramp_up("):]
    tail = body.find("\ndef ", 1)
    body = body[: tail if tail != -1 else len(body)]
    # The fix NAMES the removed line in a comment explaining why it went, so the
    # comment must not count as code. A law that cannot tell prose from a call
    # fails on correct code - the same trap this repo hit twice before.
    code = "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "if st.events == 0:" not in code, (
        "the events==0 early return is back - a clean platform can no longer recover"
    )


def test_priming_only_looks_at_recent_history():
    """RED before the fix: priming read a 24 h window and pinned the ceiling.

    Measured 2026-10-13 on the live archive (H:\\VOD.RIP-data\\archive.db,
    rate_limit_events, 331 rows): the newest YouTube row was
    2026-10-12T21:49:54 - ~15 h old - a bot_gate burst with backoff_s=1800.
    Priming found it every boot and set the ceiling to 6.8 rpm, throwing away
    whatever the ramp had recovered. A limit event older than the window is not
    current pressure.
    """
    src = (BACKEND / "services" / "rate_budget.py").read_text(encoding="utf-8", errors="replace")
    # Comments may NAME the old window while explaining what changed (the module
    # contract block above does exactly that). A law that cannot tell prose from a
    # call fails on correct code - this file already carries that lesson twice.
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "since_hours=24" not in code, (
        "priming reads a 24 h window again - an ancient burst can pin the ceiling"
    )
    assert "_PRIME_WINDOW_HOURS" in src
    assert "since_hours=_PRIME_WINDOW_HOURS" in src
    assert rate_budget._PRIME_WINDOW_HOURS <= 6.0, (
        "the priming window grew back; measure the events table before changing it"
    )