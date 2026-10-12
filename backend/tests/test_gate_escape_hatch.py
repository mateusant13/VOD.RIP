"""The bot-gate freeze must be liftable from production code.

Measured 2026-10-12: clear_youtube_gate() described itself as the "tests /
operator escape hatch" (services/yt_gate.py:154) and had TEST-ONLY callers.
The freeze is process-wide, longest-wins, and stops the archive worker and
channel previews for up to 30 minutes, so a wrong arm was also a
self-extending one that nothing could lift.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import yt_gate  # noqa: E402


def test_an_armed_freeze_is_actually_lifted():
    yt_gate.clear_youtube_gate()
    yt_gate.note_youtube_gate("unit test arm", freeze_sec=600)
    assert yt_gate.youtube_gate_active() is True
    yt_gate.clear_youtube_gate()
    assert yt_gate.youtube_gate_active() is False
    assert yt_gate.gate_remaining_sec() == 0.0


def test_the_route_lifts_the_freeze_and_reports_what_it_cleared():
    from routers.cookie_bridge import session_cookies_gate_clear

    yt_gate.clear_youtube_gate()
    yt_gate.note_youtube_gate("unit test arm", freeze_sec=600)
    res = asyncio.run(session_cookies_gate_clear())
    assert res["ok"] is True
    assert res["was_active"] is True, "the route did not see the armed freeze"
    assert res["active"] is False, "the route did not lift the freeze"
    assert res["cleared_sec"] > 0


def test_the_escape_hatch_has_a_non_test_caller():
    """RED before this change: every caller of clear_youtube_gate was a test."""
    root = Path(__file__).resolve().parents[1]
    callers = []
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if "tests" in parts or "node_modules" in parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "clear_youtube_gate(" in text:
            callers.append(path.relative_to(root).as_posix())
    assert callers, (
        "clear_youtube_gate has no production caller again - the freeze is "
        "unliftable"
    )
