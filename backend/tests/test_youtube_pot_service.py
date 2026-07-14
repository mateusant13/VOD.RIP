"""youtube_pot_service unit checks."""

from services.youtube_pot_service import (
    POT_DEFAULT_BASE,
    fetch_video_po_token,
    pot_service_ping,
    schedule_pot_service_warm,
)


def test_pot_ping_unreachable():
    assert pot_service_ping("http://127.0.0.1:1", timeout=0.2) is False


def test_fetch_po_token_empty_video_id():
    assert fetch_video_po_token("") is None
    assert fetch_video_po_token("dQw4w9WgXcQ", base="") is None


def test_pot_constants():
    assert POT_DEFAULT_BASE.startswith("http://")


def test_schedule_warm_idempotent():
    t1 = schedule_pot_service_warm()
    t2 = schedule_pot_service_warm()
    assert t1 is not None and t2 is not None
