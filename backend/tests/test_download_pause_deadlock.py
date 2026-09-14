"""Conformance 07-C5: pausing a live download must not wedge the pool.

The worker's ``finally`` block takes ``self._lock`` and its ``Paused`` branch
re-acquired that same non-reentrant lock to snapshot ``_worker_params``. The
worker blocked there forever while still holding the lock, so every other
``_lock`` caller — ``pause``, ``get``, ``get_all``, ``get_active_and_history``,
``cancel``, the stall watchdog and SSE notification — deadlocked behind it, and
the download slot never came back.

Two further properties are pinned here:
* the Paused SSE event reaches subscribers exactly ONCE per user pause, and
* a Paused queue row keeps its persisted ``_params`` and is never resurrected
  by a worker that unwinds after the download was already discarded.

A red run of this file must stay reportable, so the download pool is built from
daemon threads that skip ``concurrent.futures``' unconditional atexit join
(``install_daemon_pool``); a worker left wedged by the bug under test otherwise
hangs the interpreter AFTER pytest has printed its summary.
"""

import concurrent.futures
import queue
import threading

import pytest

from services import ytdlp_service
from services.download_manager import DownloadManager
from download_test_utils import LockHoldWitness, install_daemon_pool, purge_download_manager

_VALID_KICK_VOD = "https://kick.com/realchannel/videos/100000"
_DOWNLOAD_ID = "dl_pause_deadlock"


@pytest.fixture(autouse=True)
def _bounded_teardown(monkeypatch):
    """Purge created managers, but never through a lock a worker still owns.

    Pre-fix the paused worker holds ``_lock`` forever and
    ``purge_download_manager`` goes through ``cancel_all``, so a wedged manager
    is skipped rather than hanging the session.
    """
    install_daemon_pool(monkeypatch)
    created: list = []
    original_init = DownloadManager.__init__

    def _track_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(DownloadManager, "__init__", _track_init)
    yield
    for mgr in created:
        if not mgr._lock.acquire(timeout=0.5):
            continue
        mgr._lock.release()
        purge_download_manager(mgr)


def _worker_slot_freed(mgr: DownloadManager) -> bool:
    """Return True once the download worker has fully returned.

    The pool has a single slot, so a probe submitted after the pause only runs
    when the worker that owns that slot has unwound. A timed lock acquire is not
    enough on its own: the pausing thread can win ``_lock`` before the worker
    ever reaches the wedged unwind.
    """
    future = mgr._executor.submit(lambda: None)
    try:
        future.result(timeout=5)
    except concurrent.futures.TimeoutError:
        return False
    return True


def _drain(sink: queue.Queue) -> list:
    """Everything currently buffered, leaving the sink empty."""
    events = []
    while True:
        try:
            events.append(sink.get_nowait())
        except queue.Empty:
            return events


def _status_events(sink: queue.Queue, data: str) -> list:
    return [e for e in _drain(sink) if e.get("data") == data]


def test_pause_frees_the_download_worker(tmp_path):
    """A paused worker unwinds instead of self-deadlocking on ``_lock``."""
    in_worker = threading.Event()

    def fake_download(url, output_path, pause_event):
        in_worker.set()
        assert pause_event.wait(10), "pause() never set pause_event"
        raise ytdlp_service.PausedError("paused by test")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id=_DOWNLOAD_ID,
        download_func=fake_download,
    )

    assert in_worker.wait(10), "worker never reached the download func"

    # Subscribe while the download is still live: register_sse refuses a
    # terminal row and buffers a status/progress snapshot, which is drained
    # before the action so only the action's own events are counted.
    sink: queue.Queue = queue.Queue()
    assert mgr.register_sse(download_id, sink) is True
    _drain(sink)

    assert mgr.pause(download_id) is True

    assert _worker_slot_freed(mgr), (
        "the paused download worker never returned: it self-deadlocked "
        "re-acquiring the non-reentrant _lock in its Paused unwind while still "
        "holding the outer acquire of its finally block"
    )

    acquired = mgr._lock.acquire(timeout=2.0)
    if acquired:
        mgr._lock.release()
    assert acquired, "the manager lock stayed held after the pause unwind"

    # pause() already announced the transition, so the worker's unwind must not
    # emit a second one: one user action, one Paused event.
    assert _status_events(sink, "Paused") == [
        {"type": "status", "data": "Paused"}
    ]

    # Only now is it safe to touch any _lock-taking manager method.
    state = mgr.get(download_id)
    assert state is not None
    assert state.status == "Paused"
    # Already paused -> pause() is a no-op, not a second queue row.
    assert mgr.pause(download_id) is False

    # The row must still be resumable: resume-after-restart needs the persisted
    # params, so the snapshot must survive the removed nested acquire.
    resumable = mgr.get_resumable_entry(download_id)
    assert resumable is not None
    assert resumable["_params"]["url"] == _VALID_KICK_VOD
    entries = [
        e for e in mgr._db.queue if e.get("download_id") == download_id
    ]
    assert len(entries) == 1
    assert entries[0]["status"] == "Paused"
    # The RESUMED download is rebuilt from the persisted queue row, not from
    # in-memory state, so the params have to be on the row itself.
    # ``get_resumable_entry`` answers from ``_worker_params`` while the
    # download is live, which is why the assertion above alone cannot prove it.
    assert entries[0]["_params"]["url"] == _VALID_KICK_VOD


def test_discard_during_pause_unwind_does_not_resurrect_the_row(tmp_path):
    """Discarding a paused download must survive the worker's unwinding write.

    ``discard_from_queue`` force-stops, purges the runtime and deletes the
    queue row — while the worker that is still unwinding its ``Paused`` path
    writes that same row back. ``_worker_params`` is the runtime key the purge
    pops, so its absence is the worker's own signal that it no longer owns the
    download and must not persist.
    """
    in_worker = threading.Event()

    def fake_download(url, output_path, pause_event):
        in_worker.set()
        assert pause_event.wait(10), "pause() never set pause_event"
        # The worst point of the race: the discard completes in full before
        # this worker reaches its Paused handler and finally block. Driven from
        # the worker thread instead of a second one to make the interleaving
        # deterministic; the API call itself is unchanged.
        assert mgr.discard_from_queue(download_id) is True
        raise ytdlp_service.PausedError("paused by test")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id="dl_pause_discarded",
        download_func=fake_download,
    )

    assert in_worker.wait(10), "worker never reached the download func"
    assert mgr.pause(download_id) is True

    assert _worker_slot_freed(mgr), "the paused worker never unwound"
    assert mgr._lock.acquire(timeout=2.0) is True
    mgr._lock.release()

    assert download_id not in {
        e.get("download_id") for e in mgr._db.queue
    }, "the discarded queue row was resurrected by the unwinding worker"
    assert download_id not in mgr._abort_fns, (
        "the worker stashed abort hooks for a download whose runtime had "
        "already been purged, leaving runtime residue nothing owns"
    )
    assert mgr.get(download_id) is None



def test_paused_write_happens_inside_the_ownership_check_lock_hold(tmp_path):
    """The ownership check and its write must share one ``_lock`` hold.

    The resurrection gate reads ``_worker_params`` to decide whether this worker
    still owns the download. If the queue write then happens after the lock is
    released, ``_purge_download_runtime`` can revoke the token in that gap and
    the write lands anyway — the row comes back even though the check was
    correct when it ran. No behavioural test can see that window (a claim either
    lands before the check, which the other tests cover, or after the write), so
    this pins the atomicity directly: at the instant of the write, ``_lock`` must
    be held by the writing thread.
    """
    in_worker = threading.Event()

    def fake_download(url, output_path, pause_event):
        in_worker.set()
        assert pause_event.wait(10), "pause() never set pause_event"
        raise ytdlp_service.PausedError("paused by test")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id="dl_pause_atomic",
        download_func=fake_download,
    )
    assert in_worker.wait(10), "worker never reached the download func"

    witness = LockHoldWitness(mgr._lock, download_id)
    original_upsert = mgr._db.upsert_queue_entry
    mgr._db.upsert_queue_entry = witness.wrap(original_upsert)

    assert mgr.pause(download_id) is True
    assert _worker_slot_freed(mgr), "the paused worker never unwound"
    # pause() writes the row from the caller thread; this test is about the
    # worker's own terminal write, which is the one gated on the token.
    worker_writes = [w for w in witness.writers if w != "MainThread"]
    assert len(worker_writes) == 1, (
        "expected exactly one Paused queue write from the worker unwind, got "
        f"{len(worker_writes)}"
    )
    assert not [w for w in witness.unheld_writers if w != "MainThread"], (
        "the worker's Paused queue write ran with _lock released: the ownership "
        "check it followed is a TOCTOU read, so a purge landing in the gap "
        "resurrects the row the claim already deleted"
    )