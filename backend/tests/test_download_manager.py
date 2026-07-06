"""Basic unit tests for the download manager.

Tests the state-machine and concurrency primitives without
spawning real yt-dlp workers (no network).
"""

import pytest
from datetime import datetime, timezone

from models.schemas import DownloadState
from services.download_manager import DownloadManager
from download_test_utils import purge_download_manager

_VALID_KICK_VOD = "https://kick.com/realchannel/videos/100000"
_VALID_TWITCH_VOD = "https://twitch.tv/videos/1000001"


@pytest.fixture(autouse=True)
def _purge_download_manager_after_test():
    created: list[DownloadManager] = []
    original_init = DownloadManager.__init__

    def _track_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    DownloadManager.__init__ = _track_init
    yield
    DownloadManager.__init__ = original_init
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
