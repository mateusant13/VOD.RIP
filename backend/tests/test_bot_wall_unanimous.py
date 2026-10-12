"""A bot wall is unanimous - two refusals is not an IP-level wall.

Measured 2026-10-12: the arm site in _collect_merged_innertube_info fired on
bot_hits >= 2 anywhere in a sequential walk over many clients, and
note_youtube_gate() arms a PROCESS-WIDE 30-minute freeze (longest-wins) that
stops the archive worker and channel previews. 160 of the 162 gate arms in
the API log came from that line, all in one ~80-minute burst, and no
production code could clear the freeze (clear_youtube_gate had test-only
callers). Two clients that do not serve a video is not a wall.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.youtube_innertube import (  # noqa: E402
    _BOT_GATE_UNANIMOUS_MIN,
    _bot_wall_is_unanimous,
)


def test_two_refusals_out_of_two_are_not_a_wall():
    """RED on the pre-fix rule: bot_hits >= 2 armed the gate right here."""
    assert _bot_wall_is_unanimous(2, 2) is False, (
        "two gated clients armed a process-wide 30-minute freeze again"
    )


def test_a_unanimous_wall_still_arms():
    """The control: the fix must not make a real wall impossible to detect."""
    assert _bot_wall_is_unanimous(_BOT_GATE_UNANIMOUS_MIN, _BOT_GATE_UNANIMOUS_MIN) is True
    assert _bot_wall_is_unanimous(_BOT_GATE_UNANIMOUS_MIN + 2, _BOT_GATE_UNANIMOUS_MIN + 2) is True


def test_a_mixed_failure_breaks_unanimity():
    """A client that failed for a non-bot reason means we do not know."""
    assert _bot_wall_is_unanimous(3, 5) is False
    assert _bot_wall_is_unanimous(1, 1) is False
    assert _bot_wall_is_unanimous(0, 0) is False


def test_the_arm_site_uses_the_predicate_and_not_a_bare_threshold():
    """A predicate nobody calls is not a rule - pin the call site itself."""
    import services.youtube_innertube as mod

    src = Path(mod.__file__).read_text(encoding="utf-8", errors="replace")
    start = src.index("def _collect_merged_innertube_info")
    body = src[start : src.index("def ", src.index("note_youtube_gate", start))]
    assert "_bot_wall_is_unanimous(bot_hits, client_attempts)" in body, (
        "the merged walk no longer consults the unanimity predicate"
    )
    assert not re.search(r"bot_hits\s*>=\s*2\b", body), (
        "a bare bot_hits >= 2 threshold is back at the arm site"
    )
