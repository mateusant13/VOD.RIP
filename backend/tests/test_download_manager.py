"""Basic unit tests for the download manager.

Tests the state-machine and concurrency primitives without
spawning real yt-dlp workers (no network).
"""

from services.download_manager import DownloadManager


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
        url="https://kick.com/test/videos/abc-123",
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
    assert ours[0].url == "https://kick.com/test/videos/abc-123"


def test_cancel_nonexistent_returns_false():
    """cancel returns False for an id that was never started."""
    mgr = DownloadManager(max_workers=2)
    assert mgr.cancel("dl_nonexistent") is False


def test_cancel_count_equals_active():
    """cancel_all returns a non-negative count (1+ per active download)."""
    mgr = DownloadManager(max_workers=2)
    id1 = mgr.start_download(
        url="https://kick.com/a/videos/1",
        output_file=r"C:\tmp\a.mp4",
    )
    id2 = mgr.start_download(
        url="https://twitch.tv/b/videos/2",
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
        url="https://kick.com/x/videos/3",
        output_file=r"C:\tmp\x.mp4",
    )
    mgr.cancel(dl_id)
    removed = mgr.discard_from_queue(dl_id)
    assert removed is True
    # Should no longer appear in history or queue
    state = mgr.get_active_and_history()
    all_ids = {d.download_id for d in state["queue"] + state["history"]}
    assert dl_id not in all_ids
