"""The preview opening gets a bounded priority window over the background waves.

PRINCIPLE: the browser allows 6 sockets per origin and queues FIFO. A capped
budget limits how many sockets the app HOLDS, not how long it holds them, so
a preview that starts while the waves own the sockets still waits. During the
session create - the first of three requests the player needs - the waves step
aside to one socket.

HONEST VERDICT, and it is the point of this test being structural rather than
performance: the paired A/B did NOT separate the arms (tmp/ab_prio.txt, frozen
app, poll and sync identical):
  OFF  median 3917 ms  [846, 862, 3917, 7095, 8197]
  ON   median 3499 ms  [736, 846, 862, 1027, 2959, 3499, 3917, 7095, 8197, 11149]
Overlapping - both arms hold the same fast and the same slow reps. And the
data says why: when the session POST is fast (266 ms) the preview still takes
11.1 s, so the variance lives AFTER it (master -> segment -> canplay), not in
the socket the create was waiting for.

So the window is kept for the principle and because it is bounded and
self-closing, NOT for a measured win. Do not cite it as a latency fix.

RED ARM: remove the priority branch from _priorityBudget and
test_the_window_caps_the_budget fails.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FILE = REPO / "src" / "lib" / "backgroundBudget.ts"


def test_the_window_exists_and_is_bounded():
    """RED: drop the window and the waves never step aside for the preview."""
    text = FILE.read_text(encoding="utf-8")
    assert "beginPreviewPriorityWindow" in text
    assert "PRIORITY_WINDOW_MS = 2500" in text


def test_the_window_caps_the_budget():
    """RED: no priority branch and the cap never drops to 1."""
    text = FILE.read_text(encoding="utf-8")
    assert "previewPriorityActive() ? 1 :" in text


def test_the_window_self_closes():
    """RED: a window that never expires would silence the lanes forever."""
    text = FILE.read_text(encoding="utf-8")
    assert "_priorityUntil" in text
    assert "Date.now() < _priorityUntil" in text


def test_the_control_arm_can_disable_it():
    """An A/B needs a control arm, or the measurement cannot fail."""
    text = FILE.read_text(encoding="utf-8")
    assert "noprio" in text
