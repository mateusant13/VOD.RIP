"""Stall watchdog unit tests — fake progress holder + fake clock, no network.

The stall watchdog (DownloadManager._stall_state / _stall_tick + the
per-download daemon loop in _spawn_worker) must cancel a download whose
downloaded_bytes has not moved for STALL_WATCHDOG_SEC — a true 0 B/s stall
emits no progress hooks, so neither the hook-driven deadline nor a user
cancel can ever fire, and the process-wide yt-dlp lock would be held
forever, blocking every other guarded yt-dlp op (preview extracts, other
downloads, enrich).
"""

import threading

from services.download_manager import (
    STALL_WATCHDOG_SEC,
    _stall_state,
    _stall_tick,
)


def _holder(**over):
    holder = {
        "armed": True,
        "last_bytes": 0.0,
        "last_move_wall": None,
        "error": None,
    }
    holder.update(over)
    return holder


def test_no_stall_while_bytes_keep_moving():
    holder = _holder(last_move_wall=100.0)
    # One second under the threshold, with the hook having just reset the
    # clock — not a stall.
    assert _stall_state(
        holder, now=100.0 + STALL_WATCHDOG_SEC - 1.0, active=True
    ) is None


def test_stall_detected_after_90s_of_no_byte_progress():
    holder = _holder(last_move_wall=100.0)
    msg = _stall_state(holder, now=100.0 + STALL_WATCHDOG_SEC, active=True)
    assert msg == "download stalled (0 B/s)"
    # Latched: subsequent checks keep returning the same message.
    assert (
        _stall_state(holder, now=100.0 + STALL_WATCHDOG_SEC + 5.0, active=True)
        == msg
    )
    assert holder["error"] == msg


def test_stall_clock_latches_on_first_armed_check():
    holder = _holder()  # last_move_wall None -> latch the check time
    assert _stall_state(holder, now=42.0, active=True) is None
    assert holder["last_move_wall"] == 42.0
    assert (
        _stall_state(holder, now=42.0 + STALL_WATCHDOG_SEC, active=True)
        == "download stalled (0 B/s)"
    )


def test_unarmed_download_never_stalls():
    # No hook event has ever fired (e.g. still in the startup extract, which
    # legitimately emits no bytes) — never a stall.
    holder = _holder(armed=False)
    assert _stall_state(holder, now=10**6, active=True) is None


def test_inactive_download_never_stalls():
    holder = _holder(last_move_wall=0.0)
    assert _stall_state(holder, now=10**6, active=False) is None
    # Sanity: the same holder *does* stall when active again.
    assert _stall_state(holder, now=10**6, active=True) is not None


def test_configurable_stall_sec():
    holder = _holder(last_move_wall=0.0)
    assert (
        _stall_state(holder, now=5.0, active=True, stall_sec=5.0)
        == "download stalled (0 B/s)"
    )


def test_existing_error_message_is_latched():
    holder = _holder(last_move_wall=0.0, error="custom stall reason")
    assert _stall_state(holder, now=STALL_WATCHDOG_SEC, active=True) == "custom stall reason"


# ---------------------------------------------------------------------------
# Cancel wiring — what the watchdog loop does when a stall is detected
# ---------------------------------------------------------------------------

def test_stall_tick_cancels_and_runs_abort_fns():
    """A detected stall must set the cancel event and fire every abort fn
    (killing ffmpeg children / unwinding the yt-dlp call) so the global
    yt-dlp lock frees with a clear error instead of holding forever."""
    cancel_event = threading.Event()
    fired: list[str] = []
    stall = _holder(last_move_wall=100.0)

    assert _stall_tick(
        stall, now=100.0 + STALL_WATCHDOG_SEC, active=True,
        cancel_event=cancel_event, abort_fns=[lambda: fired.append("a"), lambda: fired.append("b")],
    ) is True
    assert cancel_event.is_set()
    assert fired == ["a", "b"]
    assert stall["error"] == "download stalled (0 B/s)"


def test_stall_tick_noop_when_not_stalled():
    cancel_event = threading.Event()
    stall = _holder(last_move_wall=100.0)
    assert _stall_tick(
        stall, now=100.0 + STALL_WATCHDOG_SEC - 1.0, active=True,
        cancel_event=cancel_event, abort_fns=[],
    ) is False
    assert not cancel_event.is_set()


def test_stall_tick_abort_fns_cannot_break_the_watchdog():
    cancel_event = threading.Event()
    stall = _holder(last_move_wall=100.0)

    def exploding():
        raise RuntimeError("abort fn failed")

    # ponytail: survival guarantee — one bad abort fn must not stop the loop
    assert _stall_tick(
        stall, now=100.0 + STALL_WATCHDOG_SEC, active=True,
        cancel_event=cancel_event, abort_fns=[exploding],
    ) is True
    assert cancel_event.is_set()
