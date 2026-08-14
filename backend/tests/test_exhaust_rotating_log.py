#!/usr/bin/env python3
"""DISK-06 exhaust fix: supervisor logs rotate instead of appending forever.

Mock-only test for backend/rotating_log.py — the file-like wrapper used by
worker_server.py and dev_server.py (background_server.py uses the same
RotatingFileHandler pattern as __main_launcher__.py).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rotating_log import open_rotating  # noqa: E402


def test_rotating_logfile_rolls_at_limit(tmp_path):
    """worker.log must stay bounded: 50 x ~75 B lines with a 200 B cap ->
    the live file stays small, at most 3 .N backups exist, and the surviving
    lines are a contiguous suffix (old content is pruned by design — the
    point is bounded disk, not archival)."""
    log = tmp_path / "worker.log"
    f = open_rotating(log, max_bytes=200, backup_count=3)
    for i in range(50):
        f.write(f"line {i:03d}: " + "x" * 60 + "\n")
    f.close()

    live = (tmp_path / "worker.log").stat().st_size
    assert live <= 200 + 80, f"live log must stay near the cap, got {live} B"
    backups = sorted(p.name for p in tmp_path.glob("worker.log.*"))
    assert len(backups) <= 3, f"backupCount=3, got {backups}"

    text = ""
    for p in sorted(tmp_path.glob("worker.log*"), reverse=True):
        text += p.read_text(encoding="utf-8")
    nums = [int(t.split(":")[0].split()[1]) for t in text.splitlines() if t.startswith("line ")]
    assert nums == sorted(nums), "surviving lines must be in write order"
    assert nums == list(range(nums[0], nums[-1] + 1)), "no gaps in the surviving suffix"
    assert len(nums) <= 4 * 2, f"live+3 backups at ~2 lines each, got {len(nums)}"
    assert nums[-1] == 49, "the newest line must survive"


def test_rotating_logfile_appends_when_small(tmp_path):
    log = tmp_path / "server-7897.log"
    f = open_rotating(log)
    try:
        f.write("[2026-08-14 10:00:00] hello\n")
        f.write("[2026-08-14 10:00:01] world\n")
    finally:
        f.close()
    assert log.read_text(encoding="utf-8").splitlines() == [
        "[2026-08-14 10:00:00] hello",
        "[2026-08-14 10:00:01] world",
    ]
    assert not list(tmp_path.glob("server-7897.log.*"))


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
