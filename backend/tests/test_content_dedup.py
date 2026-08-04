"""Content-hash dedup tests — shared files + reference-counted deletion.

Covers the ingest path (register_archive_file / archive_kick._ingest_one),
the DB reference counting (delete_video, retention), and the API surface
(/api/archive/dedupe content_groups). The archive rows are the source of
truth for file references: a file is unlinked only when no row points at it.

Env var must be set before the first services.archive_db import anywhere in
the pytest session (same pattern as test_archive_retention.py). Each test
rebinds the module connection to a fresh tmp DB.

Run from backend/: python -m pytest tests/test_content_dedup.py
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="content-dedup-test-")) / "archive.db")

import pytest  # noqa: E402
from services import archive_content_dedup, archive_db  # noqa: E402
from services.archive_retention import enforce_archive_vod_retention  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _dedup_scratch_db():
    """Rebind the shared archive conn to THIS module's scratch DB at module
    start (collection-order independent), and restore after."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="content-dedup-test-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Fresh archive DB per test; module connection rebound to tmp path."""
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    archive_db._conn = None
    archive_db._schema_ready = False


BYTES = b"identical-video-bytes-for-content-dedup"


def _sha() -> str:
    return hashlib.sha256(BYTES).hexdigest()


def _register(archive_dir: Path, video_id: str, *, same: bool,
              started_at: str = "2026-08-01T12:00:00Z") -> str:
    """Simulate one completed ingest: write a file, register with dedup.

    Returns the archive_path stored on the row (dedup may re-link)."""
    path = archive_dir / f"{video_id}.mp4"
    path.write_bytes(BYTES if same else b"different-" + video_id.encode())
    reg = archive_content_dedup.register_archive_file(
        str(path), platform="kick", video_id=video_id
    )
    archive_db.upsert_video({
        "platform": "kick", "video_id": video_id, "channel": "ch",
        "title": f"vod {video_id}", "started_at": started_at,
        "duration_sec": 60.0, "archive_path": reg["archive_path"],
        "content_sha256": reg["content_sha256"], "canonical_key": f"k-{video_id}",
        "status": "ready",
    })
    return reg["archive_path"]


def test_duplicate_bytes_share_one_file(scratch_db, tmp_path):
    """Two distinct video_ids with identical bytes -> ONE file, both rows
    reference it, hash recorded on both, surfaced by content_duplicates."""
    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    p1 = _register(archive_dir, "v1", same=True)
    p2 = _register(archive_dir, "v2", same=True)

    assert p1 == p2, "second ingest must reuse the first row's archive_path"
    assert Path(p1).is_file(), "the shared file must exist"
    assert len(list(archive_dir.iterdir())) == 1, "second copy must not be stored"

    rows = archive_db.list_videos("kick")
    assert {r["video_id"] for r in rows} == {"v1", "v2"}
    assert all(r["content_sha256"] == _sha() for r in rows)
    assert all(r["archive_path"] == p1 for r in rows)

    dup = archive_db.content_duplicates()
    assert len(dup) == 1 and dup[0]["count"] == 2 and dup[0]["sha256"] == _sha()
    assert {m["video_id"] for m in dup[0]["videos"]} == {"v1", "v2"}


def test_unique_bytes_keep_own_files(scratch_db, tmp_path):
    """Different bytes -> separate files, no duplicate group."""
    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    p1 = _register(archive_dir, "v1", same=True)
    p3 = _register(archive_dir, "v3", same=False)

    assert p1 != p3
    assert len(list(archive_dir.iterdir())) == 2
    assert archive_db.content_duplicates() == []


def test_delete_one_row_keeps_shared_file(scratch_db, tmp_path):
    """Reference-counted deletion: the file survives while ANY row points
    at it; the last row's deletion removes it."""
    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    p = _register(archive_dir, "v1", same=True)
    _register(archive_dir, "v2", same=True)

    archive_db.delete_video("kick", "v1")
    assert Path(p).is_file(), "shared file must survive while v2 references it"
    assert len(list(archive_dir.iterdir())) == 1
    assert archive_db.content_duplicates() == []

    archive_db.delete_video("kick", "v2")
    assert not Path(p).exists(), "last reference gone -> file removed"
    assert len(list(archive_dir.iterdir())) == 0


def test_retention_is_reference_counted(scratch_db, tmp_path):
    """Evicting one of two rows sharing a file must NOT delete it; evicting
    the last one must."""
    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    p = _register(archive_dir, "v1", same=True, started_at="2026-08-01T10:00:00Z")
    _register(archive_dir, "v2", same=True, started_at="2026-08-01T11:00:00Z")
    p3 = _register(archive_dir, "v3", same=False, started_at="2026-08-01T12:00:00Z")

    stats = enforce_archive_vod_retention(keep_count=2)
    assert stats == {"deleted_files": 0, "cleared_rows": 1}, (
        "evicting v1 must not delete the file v2 still references"
    )
    assert Path(p).is_file()
    r1 = archive_db.query(
        "SELECT status, archive_path FROM videos WHERE video_id='v1'")[0]
    assert r1["status"] == "known" and r1["archive_path"] is None

    stats = enforce_archive_vod_retention(keep_count=1)
    assert stats == {"deleted_files": 1, "cleared_rows": 1}, (
        "evicting v2 (last reference) must delete the shared file"
    )
    assert not Path(p).exists()
    assert Path(p3).is_file(), "v3's own file must be untouched"
    r2 = archive_db.query(
        "SELECT status, archive_path FROM videos WHERE video_id='v2'")[0]
    assert r2["status"] == "known" and r2["archive_path"] is None


def test_ingest_path_dedupes_end_to_end(scratch_db, tmp_path, monkeypatch):
    """Full archive_kick._ingest_one flow with a mocked downloader: two
    video_ids producing identical bytes land on ONE file."""
    from services.archive_kick import _ingest_one
    from services.kick_models import KickVideo

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    written: list[str] = []

    def _fake_download(url: str, out_path: str, budget_sec: float, quality: object) -> dict:
        Path(out_path).write_bytes(BYTES)
        written.append(out_path)
        return {"ok": True}

    monkeypatch.setattr("services.archive_kick._download_with_budget", _fake_download)

    v1 = KickVideo(id="vid-1", title="Same Content", created_at="2026-08-01T10:00:00Z", duration=60.0)
    v2 = KickVideo(id="vid-2", title="Same Content", created_at="2026-08-01T11:00:00Z", duration=60.0)
    r1 = _ingest_one(v1, "ch", download=True, max_download_sec=30.0,
                     quality="720", archive_dir=str(archive_dir))
    r2 = _ingest_one(v2, "ch", download=True, max_download_sec=30.0,
                     quality="720", archive_dir=str(archive_dir))

    assert r1["action"] == "downloaded" and r2["action"] == "downloaded"
    assert len(written) == 2, "the downloader ran for both video_ids"
    assert r1["archive_path"] == r2["archive_path"], (
        "byte-identical re-download must reuse the first archive_path"
    )
    assert len(list(archive_dir.iterdir())) == 1, "exactly one file on disk"
    rows = {r["video_id"]: r for r in archive_db.list_videos("kick")}
    assert all(rows[vid]["content_sha256"] == _sha() for vid in ("vid-1", "vid-2"))
    assert rows["vid-1"]["archive_path"] == rows["vid-2"]["archive_path"]


def test_failed_download_cleanup_never_touches_shared_file(scratch_db, tmp_path, monkeypatch):
    """A failed ingest deletes only its own partial file; a shared file that
    other rows reference must survive."""
    from services.archive_kick import _ingest_one
    from services.kick_models import KickVideo

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    p = _register(archive_dir, "v1", same=True)

    def _failing_download(url: str, out_path: str, budget_sec: float, quality: object) -> dict:
        Path(out_path).write_bytes(b"partial-garbage")  # partial file, then failure
        return {"ok": False, "error": "simulated failure"}

    monkeypatch.setattr("services.archive_kick._download_with_budget", _failing_download)

    r = _ingest_one(KickVideo(id="vid-2", title="Other", created_at="2026-08-02T10:00:00Z",
                              duration=60.0), "ch", download=True, max_download_sec=30.0,
                    quality="720", archive_dir=str(archive_dir))

    assert r["action"] == "failed"
    assert Path(p).is_file(), "shared file must be untouched by failed-download cleanup"
    assert not (archive_dir / "vid-2.mp4").exists(), "partial file must be removed"
    assert archive_db.content_duplicates() == []


def test_dedupe_route_exposes_content_groups(scratch_db, tmp_path):
    """/api/archive/dedupe keeps the legacy 'groups' shape and adds
    'content_groups' (backward-compatible ADD)."""
    import asyncio

    from routers.archive import archive_dedupe

    archive_dir = tmp_path / "vods"
    archive_dir.mkdir()
    _register(archive_dir, "v1", same=True)
    _register(archive_dir, "v2", same=True)
    _register(archive_dir, "v3", same=False)

    resp = asyncio.run(archive_dedupe())
    assert "groups" in resp, "legacy canonical-key groups must stay"
    groups = resp["content_groups"]
    assert len(groups) == 1
    assert groups[0]["count"] == 2 and groups[0]["sha256"] == _sha()
    assert {m["video_id"] for m in groups[0]["videos"]} == {"v1", "v2"}
