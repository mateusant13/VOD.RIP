"""Archive VOD retention tests — scratch DB + real files, keep-N eviction.

Env var must be set before the first services.archive_db import anywhere in
the pytest session (same pattern as test_archive_watchdog.py). Each test
rebinds the module connection to a fresh tmp DB so keep-N arithmetic is
exact and no rows leak between tests.

Run from backend/: python -m pytest tests/test_archive_retention.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="retention-test-")) / "archive.db")

import pytest  # noqa: E402
from services import archive_db  # noqa: E402
from services.archive_retention import enforce_archive_vod_retention  # noqa: E402


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


def _video(platform: str, video_id: str, day: int, path: Path) -> dict:
    return {
        "platform": platform,
        "video_id": video_id,
        "channel": "ch",
        "title": f"vod {video_id}",
        "started_at": f"2026-07-{day:02d}T12:00:00Z",
        "duration_sec": 100.0,
        "archive_path": str(path),
        "canonical_key": f"vod-{video_id}",
        "status": "ready",
    }


def _seed_archived(keep_dir: Path, platform: str, n: int, prefix: str = "") -> list[dict]:
    """Insert n status='ready' videos with REAL files, oldest day first."""
    rows = []
    for i in range(n):
        path = keep_dir / f"{prefix}v{i}.mp4"
        path.write_bytes(b"fake-video-bytes")
        row = _video(platform, f"{prefix}v{i}", 1 + i, path)
        archive_db.upsert_video(row)
        rows.append(row)
    return rows


def _seed_chat_transcript(platform: str, video_id: str) -> None:
    archive_db.insert_messages(platform, video_id, [
        {"offset_sec": 0.5, "user_id": "u1", "username": "alice", "text": "hello chat",
         "badges": [], "emotes": [], "ts": None},
        {"offset_sec": 1.5, "user_id": "u2", "username": "bob", "text": "hi alice",
         "badges": [], "emotes": [], "ts": None},
    ])
    archive_db.insert_transcript(platform, video_id, [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "first words"},
        {"seg_idx": 1, "start_sec": 1.0, "end_sec": 2.0, "text": "second words"},
    ])


def _count(table: str, platform: str, video_id: str) -> int:
    return archive_db.query(
        f"SELECT COUNT(*) AS c FROM {table} WHERE platform=? AND video_id=?",
        (platform, video_id),
    )[0]["c"]


def _row(platform: str, video_id: str) -> dict:
    return archive_db.query(
        "SELECT * FROM videos WHERE platform=? AND video_id=?", (platform, video_id)
    )[0]


def test_keep_5_evicts_oldest_two_files(scratch_db, tmp_path):
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    rows = _seed_archived(keep_dir, "kick", 7)  # days 1..7, newest = v6
    # transcripts + chat on both evicted videos must survive
    _seed_chat_transcript("kick", "v0")
    _seed_chat_transcript("kick", "v1")

    stats = enforce_archive_vod_retention(keep_count=5)

    assert stats == {"deleted_files": 2, "cleared_rows": 2}
    # newest 5 untouched: files exist, rows still ready with paths
    for i in range(2, 7):
        r = _row("kick", f"v{i}")
        assert r["status"] == "ready" and r["archive_path"] == str(keep_dir / f"v{i}.mp4")
        assert (keep_dir / f"v{i}.mp4").is_file()
    # oldest 2 evicted: files gone, rows kept but cleared + known
    for i in range(2):
        r = _row("kick", f"v{i}")
        assert r["status"] == "known" and r["archive_path"] is None
        assert not (keep_dir / f"v{i}.mp4").exists()
        assert r["title"] == f"vod v{i}" and r["canonical_key"] == f"vod-v{i}"  # metadata kept
        assert _count("messages", "kick", f"v{i}") == 2
        assert _count("transcripts", "kick", f"v{i}") == 2


def test_keep_1_evicts_six(scratch_db, tmp_path):
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    rows = _seed_archived(keep_dir, "kick", 7)

    stats = enforce_archive_vod_retention(keep_count=1)

    assert stats == {"deleted_files": 6, "cleared_rows": 6}
    assert _row("kick", "v6")["archive_path"] == str(keep_dir / "v6.mp4")
    assert (keep_dir / "v6.mp4").is_file()
    for i in range(6):
        assert _row("kick", f"v{i}")["status"] == "known"
        assert _row("kick", f"v{i}")["archive_path"] is None
        assert not (keep_dir / f"v{i}.mp4").exists()


def test_keep_50_deletes_nothing(scratch_db, tmp_path):
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    rows = _seed_archived(keep_dir, "kick", 7)

    stats = enforce_archive_vod_retention(keep_count=50)

    assert stats == {"deleted_files": 0, "cleared_rows": 0}
    for i in range(7):
        assert _row("kick", f"v{i}")["status"] == "ready"
        assert (keep_dir / f"v{i}.mp4").is_file()


def test_per_platform_isolation(scratch_db, tmp_path):
    """Each platform keeps its own newest N — a crowded platform never evicts another's."""
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    _seed_archived(keep_dir, "kick", 3, prefix="k")
    _seed_archived(keep_dir, "twitch", 3, prefix="t")

    stats = enforce_archive_vod_retention(keep_count=2)

    assert stats == {"deleted_files": 2, "cleared_rows": 2}
    assert not (keep_dir / "kv0.mp4").exists() and not (keep_dir / "tv0.mp4").exists()
    for p, i in (("kick", 1), ("kick", 2), ("twitch", 1), ("twitch", 2)):
        assert _row(p, f"{p[0]}v{i}")["status"] == "ready"
        assert (keep_dir / f"{p[0]}v{i}.mp4").is_file()


def test_missing_file_row_still_cleared_when_beyond_count(scratch_db, tmp_path):
    """A beyond-count row whose file is already gone gets its path cleared too
    (the row would lie otherwise); within-count rows are never touched."""
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    rows = _seed_archived(keep_dir, "kick", 3)
    (keep_dir / "v0.mp4").unlink()  # oldest beyond count (keep=2), file already missing
    assert _row("kick", "v0")["archive_path"] is not None  # row still lies pre-run

    stats = enforce_archive_vod_retention(keep_count=2)

    assert stats == {"deleted_files": 0, "cleared_rows": 1}
    assert _row("kick", "v0")["archive_path"] is None
    assert _row("kick", "v0")["status"] == "known"
    # within-count rows keep their paths even though one file is missing
    for i in (1, 2):
        assert _row("kick", f"v{i}")["archive_path"] == str(keep_dir / f"v{i}.mp4")
        assert _row("kick", f"v{i}")["status"] == "ready"


def test_non_archived_rows_never_evicted(scratch_db, tmp_path):
    """Rows with archive_path NULL (metadata-only, failed, known) are skipped."""
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    _seed_archived(keep_dir, "kick", 2)
    archive_db.upsert_video({**_video("kick", "meta-only", 9, keep_dir / "nope.mp4"),
                             "archive_path": None, "status": "known"})
    archive_db.upsert_video({**_video("kick", "failed", 10, keep_dir / "nope2.mp4"),
                             "archive_path": None, "status": "failed"})

    stats = enforce_archive_vod_retention(keep_count=1)

    assert stats == {"deleted_files": 1, "cleared_rows": 1}
    assert _row("kick", "meta-only")["status"] == "known"
    assert _row("kick", "failed")["status"] == "failed"
    assert not (keep_dir / "nope.mp4").exists()


def test_idempotent_second_run_deletes_nothing(scratch_db, tmp_path):
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    _seed_archived(keep_dir, "kick", 7)

    first = enforce_archive_vod_retention(keep_count=5)
    second = enforce_archive_vod_retention(keep_count=5)

    assert first == {"deleted_files": 2, "cleared_rows": 2}
    assert second == {"deleted_files": 0, "cleared_rows": 0}


def test_settings_driven_keep_count(scratch_db, tmp_path):
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    _seed_archived(keep_dir, "kick", 3)

    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace(archive_vod_keep_count=2)
        stats = enforce_archive_vod_retention()

    assert stats == {"deleted_files": 1, "cleared_rows": 1}
    assert not (keep_dir / "v0.mp4").exists()


def test_settings_without_flag_defaults_to_5(scratch_db, tmp_path):
    """Pre-settings builds / tests without the flag fall back to keep=5."""
    keep_dir = tmp_path / "vods"
    keep_dir.mkdir()
    _seed_archived(keep_dir, "kick", 7)

    with patch("deps.settings_mgr") as mgr:
        mgr.get.return_value = SimpleNamespace()
        stats = enforce_archive_vod_retention()

    assert stats == {"deleted_files": 2, "cleared_rows": 2}
