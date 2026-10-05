"""A not-yet-started live must not cost a budget token, forever.

MEASURED DEFECT (window 2026-10-05 00:57 -> 05:03, file tmp/vodrip-devall-api.log,
4.19 MB): ``yt-dlp: ERROR: [youtube] ZvW6Id7tmHs: Este evento ao vivo comecar em
breve.`` appears 32 times, one every ~126 s, emitted by
``services.ytdlp_guard._YtdlpConsoleLogger.error``. Three facts make it waste:

  1. The phrase is Portuguese. ``ytdlp_outcomes._LIVE_UPCOMING_RE`` only matched
     the English "this live event will begin in a few moments", so a pt-BR
     YouTube rendered ``unknown`` and took the REAL-ERROR branch
     (``ytdlp_guard.py:173``) -- an expected condition evicting real defects
     from the 500-record error ring. Verified: ``classify(...)`` -> ``unknown``.
  2. The id is not in the live archive. ``H:\\VOD.RIP-data\\archive.db``
     (13,051 videos, read-only) has 0 rows for ``ZvW6Id7tmHs``.
  3. It recurs forever. The channel live-badge poll re-probes the same
     not-started live every backend TTL (60 s) with no backoff, so the token
     cost is unbounded in the wall-clock life of a scheduled stream.

These tests pin the two behaviours that stop it: the pt-BR phrase classifies as
``live_upcoming``, and a live that is not started is not re-probed until its
backoff expires.
"""
from __future__ import annotations

import time

import pytest

from services import ytdlp_outcomes as outcomes


# --- (A1) the pt-BR phrase must classify, not fall through to unknown -------

# The exact line shape measured in the log (yt-dlp prefixes "[youtube] <id>: ").
PT_BR_UPCOMING = "[youtube] ZvW6Id7tmHs: Este evento ao vivo começará em breve."
PT_BR_UPCOMING_ASCII = "[youtube] ZvW6Id7tmHs: Este evento ao vivo comecara em breve."


@pytest.mark.parametrize(
    "text", [PT_BR_UPCOMING, PT_BR_UPCOMING_ASCII], ids=["pt-br-utf8", "pt-br-ascii"]
)
def test_ptbr_not_started_live_is_live_upcoming_not_unknown(text: str) -> None:
    """RED on main: classify() returned UNKNOWN for both pt-BR spellings.

    That is the whole defect: an expected condition reported as a real error.
    """
    code = outcomes.classify(text)
    assert code == outcomes.LIVE_UPCOMING, (
        f"pt-BR 'live not started' classified as {code!r}; expected "
        f"{outcomes.LIVE_UPCOMING!r} so the console logger drops it instead of "
        f"writing a real-error record"
    )
    assert outcomes.is_expected(code), "live_upcoming must be an expected code"
    assert not outcomes.is_permanent(code), (
        "live_upcoming is TRANSIENT: it must never be learned as a permanent "
        "per-channel verdict, or a channel that recovers is skipped forever"
    )


def test_ptbr_upcoming_is_not_misread_as_a_defect() -> None:
    """The honest-direction check: unknown stays the default for real errors."""
    # A genuine defect must STILL be unknown -- widening the marker must not
    # swallow messages it does not describe.
    assert outcomes.classify("HTTP Error 500: Internal Server Error") == outcomes.UNKNOWN
    assert outcomes.classify("Unable to download webpage: timed out") == outcomes.UNKNOWN


# --- (A2) a not-started live must not be re-probed on a fixed cadence --------

def test_not_started_live_backs_off_instead_of_polling_forever() -> None:
    """A live that has not begun must escalate its wait, not poll every TTL.

    The measured waste is 32 probes of one never-starting live over ~4 h. This
    pins the escalation shape: successive refusals push the next attempt
    further out, so the token cost is bounded rather than linear in uptime.
    """
    from services.live_capture import live_upcoming_backoff

    backoff = live_upcoming_backoff.LIVE_UPCOMING_BACKOFF
    assert len(backoff) >= 3, (
        "a not-started live needs a real escalation ladder, not one fixed wait"
    )
    assert backoff == tuple(sorted(backoff)), "backoff ladder must be non-decreasing"
    assert backoff[0] >= 60.0, (
        "the first re-probe must be no sooner than the live-badge TTL (60s); a "
        "shorter wait re-probes faster than the poll that produced it"
    )
    assert backoff[-1] >= 900.0, (
        "a live that never starts must back off to >=15 min, else it is still "
        "a fixed-cadence poll measured over hours"
    )
    assert backoff[-1] < float("inf"), "the ladder must terminate, not grow forever"


def test_live_upcoming_state_tracks_attempts_and_expires() -> None:
    """The state that makes the backoff enforceable: count, then stop asking.

    ``attempts`` drives which rung of the ladder applies; ``next_allowed_at`` is
    what the caller checks before spending a token.
    """
    from services.live_capture import live_upcoming_backoff as lub

    lub.reset()
    try:
        vid = "ZvW6Id7tmHs"
        assert lub.is_backing_off(vid) is False, "a never-seen live must not be throttled"

        # A not-started live that is NOT in the archive is never worth re-probing.
        assert lub.note_not_started(vid, attempts=0) is True
        assert lub.is_backing_off(vid) is True
        state = lub.state(vid)
        assert state is not None
        assert state["attempts"] >= 1
        assert state["next_allowed_at"] > time.monotonic()

        # Ladder exhaustion must terminate the poll entirely.
        for n in range(1, 12):
            assert lub.note_not_started(vid, attempts=n) is True
        assert lub.attempts(vid) >= 12
        assert lub.is_exhausted(vid) is True, (
            "a live that has refused for hours must stop being polled at all; "
            "unbounded re-probing is the defect being fixed"
        )
        # An exhausted live is still 'backing off' -> no token, no probe.
        assert lub.is_backing_off(vid) is True
    finally:
        lub.reset()


def test_a_live_that_starts_clears_its_backoff() -> None:
    """The polarity that makes backoff safe to ship: it must not strand a live.

    A backoff that is never cleared would hide a stream that finally started.
    """
    from services.live_capture import live_upcoming_backoff as lub

    lub.reset()
    try:
        vid = "g1rlQ_jOCI4"
        for n in range(0, 4):
            lub.note_not_started(vid, attempts=n)
        assert lub.is_backing_off(vid) is True

        lub.note_started(vid)
        assert lub.is_backing_off(vid) is False
        assert lub.state(vid) is None, "a started live leaves no backoff state"
        assert lub.is_exhausted(vid) is False
    finally:
        lub.reset()
