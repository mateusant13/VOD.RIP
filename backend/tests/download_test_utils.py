"""Helpers for backend tests (not named conftest — avoids stdlib 'tests' package clash)."""

from __future__ import annotations
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Thread pool whose workers cannot outlive the interpreter.

    ``concurrent.futures`` registers every spawned worker in
    ``_threads_queues``, and its ``atexit`` hook joins each of those threads
    without a timeout. A test that leaves a download worker wedged (which is
    exactly what a red deadlock regression does) therefore hangs the whole
    pytest process AFTER the summary line has printed: the run reports
    ``1 failed`` and then never exits, hiding the reportable failure behind a
    timeout.

    Spawning the worker as a daemon thread is not enough on its own — the
    atexit join is unconditional — so the thread is also dropped from the
    registry the join iterates. Dropping it is safe here because teardown
    already calls ``shutdown(wait=False)`` via ``purge_download_manager``, and
    nothing in these tests relies on a pool thread finishing during
    interpreter shutdown.
    """

    def _adjust_thread_count(self) -> None:  # pragma: no cover - CPython mirror
        from concurrent.futures import thread as _cft

        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (
                self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_cft._worker,
                args=(weakref.ref(self, weakref_cb),
                      self._work_queue,
                      self._initializer,
                      self._initargs),
            )
            t.daemon = True
            t.start()
            self._threads.add(t)
            # Unregistered from _threads_queues => invisible to the
            # unconditional atexit join in _python_exit.
            _cft._threads_queues.pop(t, None)


def install_daemon_pool(monkeypatch) -> None:
    """Route every pool a DownloadManager builds through daemon threads.

    Patching the module-level name covers both construction sites
    (``__init__`` and ``set_max_workers``).
    """
    from services import download_manager as _dm

    monkeypatch.setattr(_dm, "ThreadPoolExecutor", DaemonThreadPoolExecutor)


def purge_download_manager(mgr) -> None:
    mgr.cancel_all()
    time.sleep(0.05)
    state = mgr.get_active_and_history()
    for d in state["queue"]:
        mgr.discard_from_queue(d.download_id)
    for d in state["history"] + state["recent"]:
        mgr.remove_history(d.download_id)



class LockHoldWitness:
    """Records whether the manager lock was held at the moment of a write.

    Wrapping a persistence call and probing ``acquire(timeout=0)`` from *inside*
    it is exact rather than sampled: ``_lock`` is a plain non-reentrant
    ``threading.Lock``, so a thread already holding it cannot re-acquire it, and
    a held uncontended lock cannot be stolen by another thread. A successful
    non-blocking acquire therefore means the write ran with nobody holding the
    lock — the check-then-write gap the ownership gate is supposed to close.
    """

    def __init__(self, lock: threading.Lock, download_id: str):
        self._lock = lock
        self._download_id = download_id
        self.observed = 0
        self.unheld = 0
        self.writers: list[str] = []
        self.unheld_writers: list[str] = []

    def wrap(self, callback):
        """Return ``callback`` instrumented for the window of one call."""
        def _wrapped(*args, **kwargs):
            target = args[0] if args else kwargs.get("state")
            download_id = getattr(target, "download_id", None)
            if download_id == self._download_id:
                writer = threading.current_thread().name
                acquired = self._lock.acquire(timeout=0)
                self.observed += 1
                self.writers.append(writer)
                if acquired:
                    self.unheld += 1
                    self.unheld_writers.append(writer)
                    self._lock.release()
            return callback(*args, **kwargs)
        return _wrapped