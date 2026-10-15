"""POST /api/preview/timing must accept a batch AND a single event.

The client COALESCES its milestones (a click used to spend 4-6 of the
browser ~6 per-origin connections on best-effort log lines; measured 26 posts
over 6 reps, 3.1-8.0s, all on the click path). It now sends one post per flush,
and a flush carrying several events posts them as a list under `events`.

Both shapes must keep working: the flat one is what an older client and the
module tests use, and dropping it would break every other caller.

Verified live: single -> ok true; batch -> ok true accepted 2.

RED ARM: delete the events branch and a batch silently loses every event but
the first - the client still pays one socket, but the backend logs one line
instead of six.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "backend" / "models" / "schemas.py"
ROUTE = REPO / "backend" / "routers" / "preview.py"


def test_the_schema_declares_the_events_list():
    """RED: drop the field and pydantic rejects the client payload."""
    text = SCHEMA.read_text(encoding="utf-8")
    assert "events: List[PreviewTimingEvent]" in text


def test_a_single_event_still_has_the_flat_shape():
    """RED: make events required and every old caller 422s."""
    text = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"class PreviewTimingRequest\(PreviewTimingEvent\):", text)
    assert m, "PreviewTimingRequest must stay a PreviewTimingEvent subclass"


def test_the_route_handles_both_shapes():
    """RED: remove the branch and a batch loses every event but the first."""
    text = ROUTE.read_text(encoding="utf-8")
    assert "if req.events:" in text
    assert "for ev in req.events:" in text
    assert "accepted" in text and "len(req.events)" in text
    assert "log_preview_timing(" in text
