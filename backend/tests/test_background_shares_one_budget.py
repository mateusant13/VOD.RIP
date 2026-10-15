"""The background waves share ONE budget, because the browser has 6 sockets.

A per-lane ceiling is not enough. poll ceiling 2 + sync ceiling 3 + warm
ceiling 4 = up to 9 requests held by the app, against a browser that allows
6 connections per origin. The preview player then queues behind its own app.

Measured on the FROZEN app (UI and API on one origin - the real user path),
tmp/ab_frozen.txt, 6 reps:
  inflight at click  2  ->  playable  795 / 807 / 942 ms   3/3 under 3 s
  inflight at click  4  ->  playable 3322 ms
  inflight at click  6  ->  playable 5913 / 6326 ms

HONEST ABOUT THE PROOF: the A/B of the budget itself (0 vs 3 vs 2, same page,
poll and sync identical, Latin-square) did NOT separate the arms - medians
6511 / 5159 / 6686 ms on n=3-4 with a 1152-11769 ms spread, and every fast
rep appeared under all three. So this test pins the MECHANISM (a shared
gate, and the seam that lets an A/B move it), not a proven win. The win is
the principle: the app may not hold more than a third of the browser
connections whatever mix of waves is running.

RED ARM: make run() skip the shared gate and
test_every_lane_acquires_the_shared_gate fails.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FILE = REPO / "src" / "lib" / "backgroundBudget.ts"


def test_the_shared_budget_exists_and_is_bounded():
    """RED: remove the shared gate and each wave again holds its own ceiling."""
    text = FILE.read_text(encoding="utf-8")
    assert "DEFAULT_SHARED_BUDGET" in text
    assert "function sharedAcquire" in text
    assert "function sharedRelease" in text


def test_every_lane_acquires_the_shared_gate():
    """RED: drop the shared acquire from run() and the waves no longer share.

    The budget is read through _priorityBudget(), not straight from the
    setting: the preview priority window (0bd6a07) lowers it to 1 while a
    preview is opening. _priorityBudget() falls back to the seam and then to
    the default, so pinning the call site alone would pin the wrong thing -
    this test used to assert the older literal and went stale the moment the
    window landed. Pin the chain, not one call.
    """
    text = FILE.read_text(encoding="utf-8")
    assert "sharedAcquire(_priorityBudget())" in text
    assert "previewPriorityActive() ? 1 : (_seamShared() ?? _sharedBudget)" in text
    assert "task().finally(() => { release(); sharedRelease(); })" in text


def test_the_seam_lets_an_ab_move_the_budget():
    """RED: no seam and every A/B needs a rebuild to change the number."""
    text = FILE.read_text(encoding="utf-8")
    assert "function _seamShared()" in text
    assert "__VODRIP_BG_LIMITS__" in text
    assert "export function sharedInFlight()" in text


def test_the_budget_leaves_sockets_for_the_player():
    """The browser ceiling is 6; the player needs 2-3. 3 is the measured value."""
    text = FILE.read_text(encoding="utf-8")
    assert "DEFAULT_SHARED_BUDGET = 3" in text
