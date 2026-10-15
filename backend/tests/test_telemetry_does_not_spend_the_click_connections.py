"""Telemetry must not spend the preview's own connections measuring it.

Measured 2026-10-15 (tmp/ab_phases.txt, 6 reps, click -> playable): the slowest
single request in the click path was POST /api/preview/timing in 3 of 6 reps --
4070 ms, 4093 ms and 2501 ms -- beside the hls manifest it competed with for a
socket. A browser allows ~6 connections per origin, so a "fire-and-forget"
telemetry POST is not free: it is a queue entry.

NOTE ON THE RED ARM. The behavioural arm was tried and did NOT reproduce: with
the lane removed, the module own tests still passed 6/6, because they install
an immediate lane via __setTimingLaneForTests and therefore cannot see the
difference. So this law is STRUCTURAL -- it pins the code shape, and the
behavioural proof is the measurement in the commit message instead.

RED ARM: replace the lane call with a bare apiPost and both structural tests
fail.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FILE = REPO / "src" / "previewTiming.ts"


def test_emit_timing_goes_through_a_lane():
    """RED: a bare apiPost here spends the click path connections on telemetry."""
    text = FILE.read_text(encoding="utf-8")
    assert "createConcurrencyLane(1)" in text, (
        "previewTiming.ts must emit through a lane of ceiling 1"
    )
    assert "_timingLane.run(" in text, "emitTiming must go through the lane"


def test_the_click_path_never_awaits_telemetry():
    """RED: await the lane and the click waits behind the upload."""
    text = FILE.read_text(encoding="utf-8")
    assert "void _timingLane.run(" in text, (
        "the lane call must be void -- awaiting it puts the click path behind"
        " the telemetry upload"
    )


def test_the_lane_is_not_shared_with_the_background_wave():
    """The background wave lane keeps its own cap; telemetry has its own."""
    app = (REPO / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "createConcurrencyLane(livePollMaxInFlight())" in app
    text = FILE.read_text(encoding="utf-8")
    assert "createConcurrencyLane(1)" in text
