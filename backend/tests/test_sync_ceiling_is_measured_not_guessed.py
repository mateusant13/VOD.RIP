"""The sync ceiling must stay at 3 - the paired A/B measured 2 to be 2.5x worse.

2026-10-15, same protocol as the browser measurement (CONFIGURACOES -> CANAIS
opened every rep), 5 cycles, Latin-square rotated, poll lane at 2 in both arms
(tmp/ab_sync.mjs, tmp/ab_sync.txt):

  sync3  n=5  median  6120 ms
  sync2  n=5  median 15606 ms

Lowering the incremental sync's concurrency also starves the grid of
connections it shares with the preview: with 2 workers the 39-channel wave
holds its fetches longer, so the preview still queues - just for a different
reason. Raising it above 3 is what saturated the 6-connection pool in the
first place.

This test pins the measured value AND the seam the A/B needs, so a future
experiment can move the ceiling without a code change.

RED ARM: set SYNC_MAX_IN_FLIGHT to 2 and test_sync_ceiling_is_the_measured_value fails.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "src" / "App.tsx"


def _ceiling() -> int:
    text = APP.read_text(encoding="utf-8")
    m = re.search(r"^const SYNC_MAX_IN_FLIGHT = (\d+);", text, re.M)
    assert m, "SYNC_MAX_IN_FLIGHT is not defined in App.tsx"
    return int(m.group(1))


def test_sync_ceiling_is_the_measured_value():
    """RED: set SYNC_MAX_IN_FLIGHT = 2 and this fails - the A/B measured it worse."""
    assert _ceiling() == 3, (
        "SYNC_MAX_IN_FLIGHT is %d; the paired A/B measured 3 (median 6120ms) vs 2 (median 15606ms) - 2 is 2.5x worse" % _ceiling()
    )


def test_the_sync_wave_goes_through_the_seam():
    """RED: hardcode Math.min(3, ...) again and the A/B can no longer move it."""
    text = APP.read_text(encoding="utf-8")
    assert "Math.min(syncMaxInFlight(), tasks.length)" in text
    assert "function syncMaxInFlight(): number {" in text


def test_the_seam_reads_the_measurement_hook():
    """RED: drop the __VODRIP_BG_LIMITS__ read and only a rebuild changes it."""
    text = APP.read_text(encoding="utf-8")
    i = text.find("function syncMaxInFlight(): number {")
    assert i > 0
    body = text[i : i + 500]
    assert "__VODRIP_BG_LIMITS__" in body
    assert ".sync" in body
