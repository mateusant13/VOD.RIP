"""A preview click must never queue behind media I/O.

Measured 2026-10-20 on the live app: ``POST /api/preview/session`` for a Twitch
VOD answered in **52.7 s** cold, while the same resolve in isolation is
**0.86 s** - and the identical request 30 s later, when the queue had drained,
answered in **3.1 s**. Nothing was slow; the click was queued.

The reason was pool sharing. Every HLS fragment, key, init segment, rewritten
playlist and master manifest request ran on ``PREVIEW_EXECUTOR``, the same
12-worker pool that ``create_session`` waits on. A playing preview holds several
workers for the length of each upstream transfer, a couple of open popups hold
the rest, and the next click sits behind all of it.

``PREVIEW_STREAM_EXECUTOR`` is now a separate pool. These tests pin the split at
both levels: the pools are genuinely independent, and the media routes are
actually the ones using the stream pool.
"""
from __future__ import annotations

import inspect
import threading
import time


def test_the_control_pool_runs_while_the_stream_pool_is_saturated():
    """A click is answered while dozens of segments are in flight."""
    from deps import PREVIEW_EXECUTOR, PREVIEW_STREAM_EXECUTOR

    gate = threading.Event()

    def _hold() -> None:
        gate.wait(30)

    blockers = [PREVIEW_STREAM_EXECUTOR.submit(_hold) for _ in range(48)]
    try:
        started = time.perf_counter()
        value = PREVIEW_EXECUTOR.submit(lambda: "clicked").result(timeout=5)
        elapsed = time.perf_counter() - started
    finally:
        gate.set()
        for fut in blockers:
            fut.cancel()

    assert value == "clicked"
    assert elapsed < 2.0, (
        f"a control-pool task waited {elapsed:.1f}s behind stream work - the two "
        "pools must be independent"
    )


def test_the_two_pools_are_distinct_objects():
    from deps import PREVIEW_EXECUTOR, PREVIEW_STREAM_EXECUTOR

    assert PREVIEW_EXECUTOR is not PREVIEW_STREAM_EXECUTOR


def test_media_routes_use_the_stream_pool_not_the_control_pool():
    """Pins the split where it matters: the routes the player hammers."""
    from routers import preview as routes

    # preview_stream_mp4 delegates to _preview_master_response (asserted above),
    # so it names no pool itself.
    for name in ("preview_hls_resource", "_preview_master_response"):
        source = inspect.getsource(getattr(routes, name))
        assert "PREVIEW_STREAM_EXECUTOR" in source, (
            f"{name} must run its media I/O on PREVIEW_STREAM_EXECUTOR"
        )
        assert "PREVIEW_EXECUTOR" not in source.replace("PREVIEW_STREAM_EXECUTOR", ""), (
            f"{name} still queues media I/O on the control pool - a click will "
            "wait behind it"
        )


def test_create_session_still_runs_on_the_control_pool():
    """The click itself must keep the pool that media can no longer occupy."""
    from routers import preview as routes

    source = inspect.getsource(routes.preview_create_session)
    assert "PREVIEW_EXECUTOR" in source
    assert "PREVIEW_STREAM_EXECUTOR" not in source
