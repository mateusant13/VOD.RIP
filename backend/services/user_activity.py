"""One question background work must ask: is the owner using the app?

Measured 2026-10-20 on the live app. A cold preview click that resolves in
**2.6 s** in isolation answered in **8-23 s** inside the app, with py-spy
catching, at the same moment: eight ``innertube_*`` threads doing
``ssl_wrap_socket`` and InnerTube POSTs, ``archive-scheduler`` inside a yt-dlp
YouTube-tab extraction, and ``yt-display-warm`` inside
``yt_dlp/extractor/youtube/_video.py`` - several of them holding the GIL.

None of that work was wrong; it just started at the same instant as the click.
The rule this module encodes is narrow and deliberate:

    the owner outranks the archive.

``mark_user_activity()`` is called by the routes a person waits on.
Background chokepoints call ``wait_until_idle()`` before starting more work, or
consult ``user_is_active()`` to shrink their own per-pass budgets.

Deliberately tiny and dependency-free: it imports nothing from the app, never
holds a lock across work, and never raises. Background work that cannot import
it must keep working exactly as before.
"""
from __future__ import annotations

import threading
import time

# How long after the last user action the app still counts as "in use". Long
# enough to cover a click plus the playlist/segment burst that follows it,
# short enough that a background pass is not starved by a stray request.
QUIET_SEC = 8.0

_lock = threading.Lock()
_last_active = 0.0


def mark_user_activity() -> None:
    """Record that a person is waiting on the app right now. Never raises."""
    global _last_active
    try:
        with _lock:
            _last_active = time.monotonic()
    except Exception:  # noqa: BLE001 - a gate must never fail a request
        pass


def seconds_since_user_activity() -> float:
    """Seconds since the last marked action, or ``inf`` if none yet."""
    with _lock:
        last = _last_active
    if last <= 0.0:
        return float("inf")
    return max(0.0, time.monotonic() - last)


def user_is_active(quiet_sec: float = QUIET_SEC) -> bool:
    return seconds_since_user_activity() < quiet_sec


def wait_until_idle(
    *,
    quiet_sec: float = QUIET_SEC,
    max_wait_s: float = 120.0,
    poll_s: float = 0.5,
    stop: threading.Event | None = None,
) -> bool:
    """Block until the owner has been quiet for ``quiet_sec``.

    Returns True when background work may start now, False when ``max_wait_s``
    or ``stop`` ended the wait first. **Bounded on purpose**: callers proceed
    anyway (or skip this round), because the archive still has to advance when
    someone simply leaves the app open - a gate that can starve the archive is
    a bug, not caution.
    """
    deadline = time.monotonic() + max_wait_s
    while True:
        if stop is not None and stop.is_set():
            return False
        if not user_is_active(quiet_sec):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def reset_for_tests() -> None:
    """Clear the stamp so a test starts from "nobody is using the app"."""
    global _last_active
    with _lock:
        _last_active = 0.0
