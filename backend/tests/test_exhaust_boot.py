#!/usr/bin/env python3
"""BOOT-02/03 exhaust fixes: idle transcribe enqueue + sherpa stamp-on-success.

Mock-only: scratch archive DB, no pip, no GPU, no daemons.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")

from services import archive_db  # noqa: E402
from services import archive_scheduler  # noqa: E402
from services import archive_transcribe as at  # noqa: E402


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    monkeypatch.delenv("VODRIP_BACKGROUND", raising=False)
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if archive_db._conn is not None:
        try:
            archive_db._conn.close()
        except Exception:
            pass
        archive_db._conn = None
    archive_db._schema_ready = False


def _vod(video_id: str, platform: str = "twitch") -> None:
    archive_db.upsert_channel_video({
        "platform": platform,
        "video_id": video_id,
        "channel": "c",
        "title": "t",
        "kind": "vod",
        "duration_sec": 60,
        "status": "ready",
    })


def test_enqueue_skips_pass2_when_queue_idle(scratch_db):
    """No user/search transcribe work in flight -> scheduler must not invent it."""
    _vod("111")
    archive_scheduler._enqueue_transcriptions()
    rows = list(archive_db.query(
        "SELECT id FROM archive_jobs WHERE kind='transcribe'"
    ))
    assert rows == [], f"idle pass-2 must not enqueue, got {rows}"


def test_enqueue_pass2_when_transcribe_already_inflight(scratch_db):
    """A queued transcribe job is the user-action signal — pass-2 may top up."""
    _vod("111")
    _vod("222")
    archive_db.enqueue_job("transcribe-twitch-222", "transcribe", "twitch", "222")
    archive_scheduler._enqueue_transcriptions()
    vids = {r["video_id"] for r in archive_db.query(
        "SELECT video_id FROM archive_jobs WHERE kind='transcribe'"
    )}
    assert "222" in vids
    assert "111" in vids, "pass-2 must top up other candidates once work is in flight"


def test_sherpa_skips_stamp_and_pip_when_worker_live(monkeypatch, tmp_path):
    stamp = tmp_path / "gpu_sherpa_last_check.txt"
    monkeypatch.setattr(at, "_gpu_autoinstall_due", lambda: True)
    monkeypatch.setattr(at, "_gpu_autoinstall_needed", lambda: True)
    monkeypatch.setattr(at, "_gpu_autoinstall_stamp_path", lambda: stamp)
    monkeypatch.setattr(at.archive_db, "worker_live", lambda age_s=45: True)
    ran = []
    monkeypatch.setattr(
        at.sp, "run",
        lambda *a, **k: ran.append(1) or types.SimpleNamespace(returncode=0, stderr=""),
    )
    at.maybe_ensure_gpu_sherpa()
    assert not stamp.exists(), "must not lock out 24h while a worker holds the DLLs"
    assert ran == [], "must not pip-install under a live worker"


def test_sherpa_stamps_only_on_successful_install(monkeypatch, tmp_path):
    stamp = tmp_path / "gpu_sherpa_last_check.txt"
    monkeypatch.setattr(at, "_gpu_autoinstall_due", lambda: True)
    monkeypatch.setattr(at, "_gpu_autoinstall_needed", lambda: True)
    monkeypatch.setattr(at, "_gpu_autoinstall_stamp_path", lambda: stamp)
    monkeypatch.setattr(at.archive_db, "worker_live", lambda age_s=45: False)

    monkeypatch.setattr(
        at.sp, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr="pip boom"),
    )
    at.maybe_ensure_gpu_sherpa()
    assert not stamp.exists(), "failed install must not stamp a 24h lockout"

    monkeypatch.setattr(
        at.sp, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stderr=""),
    )
    at.maybe_ensure_gpu_sherpa()
    assert stamp.exists(), "successful install stamps so we do not retry all day"


def test_sherpa_stamps_when_install_not_needed(monkeypatch, tmp_path):
    stamp = tmp_path / "gpu_sherpa_last_check.txt"
    monkeypatch.setattr(at, "_gpu_autoinstall_due", lambda: True)
    monkeypatch.setattr(at, "_gpu_autoinstall_needed", lambda: False)
    monkeypatch.setattr(at, "_gpu_autoinstall_stamp_path", lambda: stamp)
    monkeypatch.setattr(at.archive_db, "worker_live", lambda age_s=45: False)
    ran = []
    monkeypatch.setattr(at.sp, "run", lambda *a, **k: ran.append(1))
    at.maybe_ensure_gpu_sherpa()
    assert stamp.exists()
    assert ran == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
