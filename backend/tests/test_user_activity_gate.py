"""Background work must yield to the person using the app.

Measured 2026-10-20 on the live app: a cold preview click that resolves in 2.6 s
in isolation answered in 8-23 s inside the app. py-spy caught, at the same
moment, eight ``innertube_*`` threads doing TLS + InnerTube POSTs,
``archive-scheduler`` inside a yt-dlp YouTube-tab extraction and
``yt-display-warm`` inside ``yt_dlp/extractor/youtube/_video.py`` - several
holding the GIL while the click waited.

``services.user_activity`` is the gate: user-blocking routes stamp it, and
background chokepoints wait for a quiet moment (bounded, so the archive still
advances when the app is merely left open).
"""
from __future__ import annotations

import asyncio

import pytest

from services import user_activity


@pytest.fixture(autouse=True)
def _clean_gate():
    user_activity.reset_for_tests()
    yield
    user_activity.reset_for_tests()


def test_a_fresh_gate_is_idle():
    assert user_activity.seconds_since_user_activity() == float("inf")
    assert user_activity.user_is_active() is False


def test_marking_activity_makes_the_app_busy():
    user_activity.mark_user_activity()

    assert user_activity.user_is_active() is True
    assert user_activity.seconds_since_user_activity() < 1.0


def test_the_gate_expires_after_the_quiet_window():
    user_activity.mark_user_activity()

    assert user_activity.user_is_active(quiet_sec=0.05) is True
    import time

    time.sleep(0.12)
    assert user_activity.user_is_active(quiet_sec=0.05) is False


def test_wait_until_idle_returns_immediately_when_nobody_is_using_the_app():
    assert user_activity.wait_until_idle(max_wait_s=5.0) is True


def test_wait_until_idle_is_bounded_and_never_starves_the_archive():
    user_activity.mark_user_activity()

    assert user_activity.wait_until_idle(max_wait_s=0.1, poll_s=0.02) is False, (
        "a busy app must not block background work forever - the caller proceeds"
    )


def test_wait_until_idle_honours_a_stop_event():
    user_activity.mark_user_activity()
    stop = __import__("threading").Event()
    stop.set()

    assert user_activity.wait_until_idle(max_wait_s=5.0, stop=stop) is False


def test_the_archive_scheduler_treats_a_click_as_background_mode(monkeypatch):
    from services import archive_scheduler

    monkeypatch.setattr(
        "services.autostart.background_mode", lambda: False, raising=False
    )

    assert archive_scheduler._background() is False
    user_activity.mark_user_activity()
    assert archive_scheduler._background() is True, (
        "while the owner is clicking, passes must run with the small budgets"
    )


def test_health_reports_busy_so_a_supervisor_can_wait(monkeypatch):
    from routers import system as system_router

    monkeypatch.setattr(
        "services.archive_db.has_pending_jobs", lambda: 0, raising=False
    )
    monkeypatch.setattr(
        "services.archive_db.worker_live", lambda **kw: True, raising=False
    )
    monkeypatch.setattr(
        "services.archive_db.worker_heartbeat_age", lambda tag: 1.0, raising=False
    )

    user_activity.mark_user_activity()
    payload = asyncio.run(system_router.health())

    assert payload["ok"] is True
    assert payload["busy"] is True
