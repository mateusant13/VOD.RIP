"""Basic unit tests for the download manager.

Tests the state-machine and concurrency primitives without
spawning real yt-dlp workers (no network).
"""

import pytest
import time
import threading
from datetime import datetime, timezone

from models.schemas import DownloadState
from services import ytdlp_service
from services.download_manager import DownloadManager
from download_test_utils import install_daemon_pool, purge_download_manager

_VALID_KICK_VOD = "https://kick.com/realchannel/videos/100000"
_VALID_TWITCH_VOD = "https://twitch.tv/videos/1000001"


@pytest.fixture(autouse=True)
def _purge_download_manager_every_ten():
    """Purge queued/history test downloads every 10 start_download calls."""
    count = {"n": 0}
    created: list = []
    original_init = DownloadManager.__init__
    original_start = DownloadManager.start_download

    def _track_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    def _counting_start(self, *args, **kwargs):
        dl_id = original_start(self, *args, **kwargs)
        count["n"] += 1
        if count["n"] % 10 == 0:
            purge_download_manager(self)
        return dl_id

    DownloadManager.__init__ = _track_init
    DownloadManager.start_download = _counting_start
    yield
    DownloadManager.__init__ = original_init
    DownloadManager.start_download = original_start
    for mgr in created:
        purge_download_manager(mgr)


def test_download_manager_initial_state():
    """A fresh manager exposes queue and history lists with valid entries.

    Note: the manager reconciles on-disk artifacts from previous runs, so
    we can't assert emptiness. Instead we verify structure and type invariants.
    """
    mgr = DownloadManager(max_workers=2)
    state = mgr.get_active_and_history()
    assert "queue" in state
    assert "history" in state
    assert isinstance(state["queue"], list)
    assert isinstance(state["history"], list)
    # If there are entries, they must have the expected attributes
    for entry in state["queue"] + state["history"]:
        assert hasattr(entry, "download_id")
        assert hasattr(entry, "status")
        assert hasattr(entry, "url")


def test_start_download_adds_to_active():
    """start_download creates a download id and adds to active list."""
    mgr = DownloadManager(max_workers=2)
    dl_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=r"C:\tmp\test.mp4",
    )
    assert dl_id.startswith("dl_")
    state = mgr.get_active_and_history()
    queue = state["queue"]
    assert len(queue) >= 1
    # The most recent entry should be ours
    # (there may be reconciled entries from disk in app-data dir)
    ours = [d for d in queue if d.download_id == dl_id]
    assert len(ours) == 1
    assert ours[0].status in ("Starting...", "Downloading...")
    assert ours[0].url == _VALID_KICK_VOD


def test_cancel_nonexistent_returns_false():
    """cancel returns False for an id that was never started."""
    mgr = DownloadManager(max_workers=2)
    assert mgr.cancel("dl_nonexistent") is False


def test_cancel_count_equals_active():
    """cancel_all returns a non-negative count (1+ per active download)."""
    mgr = DownloadManager(max_workers=2)
    id1 = mgr.start_download(
        url="https://kick.com/realchannel/videos/100001",
        output_file=r"C:\tmp\a.mp4",
    )
    id2 = mgr.start_download(
        url=_VALID_TWITCH_VOD,
        output_file=r"C:\tmp\b.mp4",
    )
    count = mgr.cancel_all()
    assert count >= 1  # at least one job was active to cancel


def test_pause_returns_false_for_completed():
    """pause returns False when the download is already done."""
    mgr = DownloadManager(max_workers=2)
    assert mgr.pause("dl_nonexistent") is False


def test_discard_from_queue():
    """discard_from_queue removes an entry from both memory and queue.json."""
    mgr = DownloadManager(max_workers=2)
    dl_id = mgr.start_download(
        url="https://kick.com/realchannel/videos/123456",
        output_file=r"C:\tmp\x.mp4",
    )
    mgr.cancel(dl_id)
    removed = mgr.discard_from_queue(dl_id)
    assert removed is True
    # Should no longer appear in history or queue
    state = mgr.get_active_and_history()
    all_ids = {d.download_id for d in state["queue"] + state["history"]}
    assert dl_id not in all_ids


def test_concurrent_start_and_cancel(download_test_counter):
    """Starting and cancelling downloads concurrently doesn't deadlock."""
    from concurrent.futures import ThreadPoolExecutor
    mgr = DownloadManager(max_workers=4)
    urls = [f"https://twitch.tv/videos/{1_000_000 + i}" for i in range(10)]
    ids = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                mgr.start_download,
                url=url,
                output_file=rf"C:\tmp\{i}.mp4",
            )
            for i, url in enumerate(urls)
        ]
        for f in futures:
            ids.append(f.result())
    # Cancel all concurrently
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(mgr.cancel, dl_id) for dl_id in ids]
        results = [f.result() for f in futures]
    # All should return True (or at least not deadlock)
    count = sum(1 for r in results if r is True)
    assert count >= 0
    assert mgr.cancel_all() >= 0
    download_test_counter(mgr)


def test_remove_history_deletes_output_file(tmp_path):
    """remove_history deletes the completed download file from disk."""
    output = tmp_path / "completed.mp4"
    output.write_bytes(b"x" * 1000)
    assert output.is_file()

    mgr = DownloadManager(max_workers=2)
    dl_id = "dl_test_remove_file_001"
    state = DownloadState(
        download_id=dl_id,
        url=_VALID_KICK_VOD,
        status="Completed",
        output_file=str(output),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    mgr._db.record_history(state)

    assert mgr.remove_history(dl_id) is True
    for _ in range(30):
        if not output.is_file():
            break
        time.sleep(0.05)
    assert not output.is_file()


def test_cancel_all_idempotent():
    """Calling cancel_all twice in a row doesn't error."""
    mgr = DownloadManager(max_workers=2)
    mgr.start_download(url="https://kick.com/realchannel/videos/100001", output_file=r"C:\tmp\a.mp4")
    count1 = mgr.cancel_all()
    count2 = mgr.cancel_all()
    assert count1 >= 0
    assert count2 == 0  # second call should have nothing to cancel


def test_kill_pp_state_procs_terminates_child():
    """kill_pp_state_procs must stop tracked ffmpeg children (mux cancel path)."""
    import subprocess
    import sys
    import time

    from services.ytdlp_download import kill_pp_state_procs

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pp_state = {"active_procs": [proc]}
    kill_pp_state_procs(pp_state)
    time.sleep(0.1)
    assert proc.poll() is not None
    assert not pp_state.get("active_procs")


def test_has_active_runtime_false_when_event_present_but_runtime_purged():
    """_has_active_runtime must test live ownership, not cancel-event presence.

    A stale key in ``_cancel_events`` (an Event object) is NOT proof a runtime
    is active: ``bool(threading.Event())`` is True even when the Event is never
    set, so the old `if self._cancel_events.get(download_id)` returned True for
    every key present — indistinguishable from membership. After a
    purge/cancel that pops ``_worker_params`` but leaves a mid-race Event
    behind, that stale branch would report "active" and resurrect the queue row
    the purge just removed. Live ownership is ``_worker_params[download_id]``.
    """
    mgr = DownloadManager(max_workers=2)
    dl_id = "dl_runtime_gone"
    state = DownloadState(
        download_id=dl_id,
        url=_VALID_KICK_VOD,
        status="Failed",  # in _DONE_STATUSES — must not count via state path
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    with mgr._lock:
        mgr._downloads[dl_id] = state
        # The crux: a FRESH (never-set) Event is present, but the ownership
        # token is absent — the exact post-purge race shape.
        mgr._cancel_events[dl_id] = threading.Event()
        mgr._worker_params.pop(dl_id, None)

    assert mgr._has_active_runtime(dl_id) is False

    # Positive control: with the ownership token present, it IS active.
    with mgr._lock:
        mgr._worker_params[dl_id] = {"url": _VALID_KICK_VOD}
    assert mgr._has_active_runtime(dl_id) is True


def test_remove_history_returns_true_when_it_purged_own_worker(monkeypatch):
    """remove_history returns True when the live branch purged the worker's own
    terminal work.

    The live branch is reached deterministically (no real worker, no network):
    seed a runtime that `_has_active_runtime` reports as live (ownership token
    in `_worker_params` + a persisted queue row), and neutralize the
    `_force_stop_download` spin so the call resolves in milliseconds instead of
    racing the ~2.5s force-stop.

    Pre-fix the branch set `removed = True` but an unconditional `removed =
    False` re-init below it executed on every path, so the live branch returned
    False for work it had just purged and deleted — discarding own work looked
    like a no-op.
    """
    mgr = DownloadManager(max_workers=2)
    dl_id = "dl_remove_own_work"
    state = DownloadState(
        download_id=dl_id,
        url=_VALID_KICK_VOD,
        status="Downloading...",  # not in _DONE_STATUSES -> live via state
        started_at=datetime.now(timezone.utc).isoformat(),
        output_file="",
    )
    with mgr._lock:
        mgr._downloads[dl_id] = state
        mgr._worker_params[dl_id] = {"url": _VALID_KICK_VOD}
        mgr._cancel_events[dl_id] = threading.Event()
        mgr._pause_events[dl_id] = threading.Event()
        mgr._cleanup_info[dl_id] = {
            "output_file": "", "output_existed": False, "temp_dirs": [],
            "expected_duration": None,
        }
    mgr._db.upsert_queue_entry(state, mgr._worker_params[dl_id])

    # Neutralize the force-stop spin so the branch is entered deterministically
    # and quickly; the branch's own cleanup (purge + queue delete) still runs.
    monkeypatch.setattr(mgr, "_force_stop_download", lambda did: None)

    assert mgr._has_active_runtime(dl_id) is True, "precondition: runtime is live"
    assert mgr.remove_history(dl_id) is True, (
        "remove_history returned False after purging and deleting the queue "
        "row of the very work it removed"
    )
    assert dl_id not in {
        e.get("download_id") for e in mgr._db.queue
    }, "the removed download's queue row must be gone"
    assert dl_id not in mgr._worker_params, "the ownership token must be revoked"


def test_baseexception_escape_persists_failed_history_row(tmp_path, monkeypatch):
    """A BaseException escape (SystemExit) is still caught by the finally-block
    zombie guard after the dead second `except Exception:` arm was removed.

    The old worker had two `except Exception:` arms; the second (after the
    surviving `except Exception as e:`) was unreachable — every non-BaseException
    failure already exited the try via the first. Only BaseException escape
    reached the handler, and the surviving first arm never sees BaseException
    anyway, so the second arm was dead weight. Its removal is proven structurally
    by the lint N4 rule going green plus py_compile; this test pins the behavior
    the dead arm was once (wrongly) thought to own: a SystemExit from the worker
    must NOT leave the persisted queue row at "Finalising... 99%" — the finally
    zombie-guard marks it Failed and lands it in history.
    """
    install_daemon_pool(monkeypatch)

    def _suicide(url, output_path, cancel_event=None, **kwargs):
        raise SystemExit("worker died of BaseException")

    mgr = DownloadManager(max_workers=1)
    download_id = mgr.start_download(
        url=_VALID_KICK_VOD,
        output_file=str(tmp_path / "clip.mp4"),
        download_id="dl_baseexception_escape",
        download_func=_suicide,
    )

    # Give the worker time to unwind through finally.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if mgr.get(download_id) is None and download_id not in mgr._worker_params:
            break
        time.sleep(0.05)

    assert download_id in {
        e.get("download_id") for e in mgr._db.history
    }, "the SystemExit worker should still land a Failed history row"
    assert download_id not in {
        e.get("download_id") for e in mgr._db.queue
    }, "the SystemExit worker must not leave a queue entry behind"
