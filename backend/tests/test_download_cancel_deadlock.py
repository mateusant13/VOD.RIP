"""07-C5 sibling: force-stopping a live download must not wedge the pool.

The worker's ``CancelledError`` handler took ``self._lock`` and called
``_notify_sse()`` INSIDE the holder — and ``_notify_sse`` acquires the same
non-reentrant lock at the top of its own body. The worker blocked there forever
holding the lock, exactly like the pause unwind in
``test_download_pause_deadlock.py``. ``cancel()`` masks the bug by pre-writing
``"Cancelled"`` before force-stopping, so the transition guard skips the notify,
but ``discard_from_queue``/``remove_history`` force-stop through
``_force_stop_download`` WITHOUT setting that status, so the branch is live.

Two further properties are pinned here:
* a force-stop that publishes no terminal status gets exactly ONE Cancelled SSE
  event, from the worker that observed the cancellation, and
* a caller that already published ``"Cancelled"`` is not second-announced.

A red run of this file must stay reportable, so the download pool is built from
daemon threads that skip ``concurrent.futures``' unconditional atexit join
(``install_daemon_pool``); a worker left wedged by the bug under test otherwise
hangs the interpreter AFTER pytest has printed its summary.
"""

import concurrent.futures
import json
import queue
import threading
from pathlib import Path

import pytest

from services import ytdlp_service
from services.download_manager import DownloadManager
from download_test_utils import install_daemon_pool, purge_download_manager

_VALID_KICK_VOD = "https://kick.com/realchannel/videos/100000"
_DOWNLOAD_ID = "dl_cancel_deadlock"


@pytest.fixture(autouse=True)
def _bounded_teardown(monkeypatch):
    """Purge created managers, but never through a lock a worker still owns."""
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

    The pool has a single slot, so a probe submitted after the force-stop only
    runs when the worker that owns that slot has unwound.
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


@pytest.mark.parametrize("entry_point", ["discard_from_queue", "remove_history"])
def test_force_stop_frees_the_download_worker(entry_point, tmp_path):
    """A cancelled worker unwinds instead of self-deadlocking on ``_lock``."""
    in_worker = threading.Event()
    saw_cancel = threading.Event()

    def fake_download(url, output_path, cancel_event):
        in_worker.set()
        assert cancel_event.wait(10), "force-stop never set cancel_event"
        saw_cancel.set()
        raise ytdlp_service.CancelledError("cancelled by test")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id=_DOWNLOAD_ID,
        download_func=fake_download,
    )

    assert in_worker.wait(10), "worker never reached the download func"

    # Neither entry point publishes a terminal status, so this transition is the
    # worker's own and must be announced. register_sse buffers a status/progress
    # snapshot for a live row; that is drained before the action so only the
    # action's events are counted.
    sink: queue.Queue = queue.Queue()
    assert mgr.register_sse(download_id, sink) is True
    assert mgr._sse_queues.get(download_id) == [sink]
    _drain(sink)

    # The force-stop callers take _lock themselves while winding down, so a
    # wedged worker strands them too — run the API call off the main thread
    # to keep the (intended) red failure reportable instead of hanging pytest.
    api_result: dict = {}
    api_thread = threading.Thread(
        target=lambda: api_result.update(
            value=getattr(mgr, entry_point)(download_id)
        ),
        daemon=True,
        name=f"force-stop-{entry_point}",
    )
    api_thread.start()
    assert saw_cancel.wait(10)

    assert _worker_slot_freed(mgr), (
        f"{entry_point}() wedged the download worker: the CancelledError "
        "handler called _notify_sse() while already holding the non-reentrant "
        "_lock and self-deadlocked in the worker thread"
    )

    acquired = mgr._lock.acquire(timeout=2.0)
    if acquired:
        mgr._lock.release()
    assert acquired, "the manager lock stayed held after the cancel unwind"

    # Exactly one: the notify is emitted after the lock is released, while the
    # subscriber is still attached.
    assert _status_events(sink, "Cancelled") == [
        {"type": "status", "data": "Cancelled"}
    ]

    api_thread.join(10)
    assert not api_thread.is_alive(), f"{entry_point}() never returned"
    assert api_result.get("value") is True

    # Only now is it safe to touch any _lock-taking manager method.
    assert download_id not in {
        e.get("download_id") for e in mgr._db.queue
    }
    # The claim also wins the history side. `_VALID_KICK_VOD` passes
    # `is_sensible_vod_url`, so an unwinding worker's terminal
    # `record_history` really does persist the row here: it must not come
    # back as a resumable "Recent" entry for a download the user just
    # discarded. `remove_history` drops history itself, so only
    # `discard_from_queue` relies on the worker not writing it.
    assert download_id not in {
        e.get("download_id") for e in mgr._db.history
    }, "the discarded download came back as a phantom history row"
    on_disk = json.loads(
        Path(mgr._db._history_file).read_text(encoding="utf-8")
    )
    assert download_id not in {e.get("download_id") for e in on_disk}, (
        "the phantom history row was flushed to disk and will survive a "
        "restart"
    )


def test_worker_does_not_reannounce_a_cancel_the_caller_published(tmp_path):
    """A caller that already said "Cancelled" must not be seconded.

    Reproduced without calling ``cancel()`` itself, because ``cancel()`` also
    purges the runtime — and purging pops ``_sse_queues``, so a later worker
    notify would go nowhere and the count would read zero for the wrong reason.
    Here the status is pre-written exactly as ``cancel()`` does while the
    subscriber stays attached, so the only thing that can suppress the second
    event is the transition guard in the CancelledError handler.
    """
    in_worker = threading.Event()

    def fake_download(url, output_path, cancel_event):
        in_worker.set()
        assert cancel_event.wait(10), "cancel_event never set"
        # What cancel() does before force-stopping: publish the terminal
        # status, then let the worker unwind.
        with mgr._lock:
            mgr._downloads[download_id].status = "Cancelled"
        raise ytdlp_service.CancelledError("cancelled by test")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id="dl_cancel_preannounced",
        download_func=fake_download,
    )
    assert in_worker.wait(10), "worker never reached the download func"

    sink: queue.Queue = queue.Queue()
    assert mgr.register_sse(download_id, sink) is True
    # Attached, so anything the worker emits from here IS observable.
    assert mgr._sse_queues.get(download_id) == [sink]
    _drain(sink)

    # Release the worker the way _force_stop_download does, minus the purge.
    mgr._cancel_events[download_id].set()

    assert _worker_slot_freed(mgr), "the cancelled worker never unwound"
    assert mgr._lock.acquire(timeout=2.0) is True
    mgr._lock.release()

    assert _status_events(sink, "Cancelled") == []


def test_late_terminal_write_after_a_claim_is_dropped(tmp_path):
    """A worker that unwinds AFTER its download was claimed persists nothing.

    In the test above the claim lands ~2.5 s early — `_force_stop_download`
    always burns its whole deadline loop — so the worker's terminal write happens
    first and the claim's own cleanup deletes the row it left. The ordering that
    actually resurrects a row is the reverse: a slow unwind (blocking temp-dir
    cleanup, a postprocess poller taking its full join) that reaches the terminal
    write after the purge has already deleted queue row and history entry.

    The worker here deliberately ignores `cancel_event` and unwinds only once the
    claim has returned, pinning that ordering without a sleep.
    """
    in_worker = threading.Event()
    claimed = threading.Event()

    def fake_download(url, output_path, cancel_event):
        in_worker.set()
        assert claimed.wait(10), "the claim never completed"
        raise ytdlp_service.CancelledError("cancelled by test")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id="dl_cancel_late_write",
        download_func=fake_download,
    )

    assert in_worker.wait(10), "worker never reached the download func"

    result: dict = {}
    claim = threading.Thread(
        target=lambda: result.update(value=mgr.discard_from_queue(download_id)),
        daemon=True,
        name="late-claim",
    )
    claim.start()
    claim.join(20)
    assert not claim.is_alive(), "discard_from_queue() never returned"
    assert result.get("value") is True
    claimed.set()

    assert _worker_slot_freed(mgr), "the unwinding worker never returned"
    assert mgr._lock.acquire(timeout=2.0) is True
    mgr._lock.release()

    persisted = {e.get("download_id") for e in mgr._db.queue} | {
        e.get("download_id") for e in mgr._db.history
    }
    assert download_id not in persisted, (
        "a worker still unwinding re-persisted a download whose runtime had "
        "already been purged by discard_from_queue"
    )
    assert download_id not in mgr._worker_params
