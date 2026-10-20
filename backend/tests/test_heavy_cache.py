"""Heavy per-channel cache lifecycle (48h grace + 1h boot countdown).

The contract these tests pin down, because every clause is an owner decision:

  * removing a channel does NOT delete its preview media immediately;
  * nothing is deleted before the 48h grace is over;
  * once 48h has elapsed, the app does not delete retroactively - it starts a
    ONE-HOUR countdown at that moment and deletes only when it expires;
  * re-adding the channel cancels the deletion, at any stage;
  * light cache (snapshots / rows / transcripts) is never involved.
"""
from __future__ import annotations

import json
import time

import pytest

from services import heavy_cache, instant_preview


@pytest.fixture()
def previews(tmp_path, monkeypatch):
    monkeypatch.setattr(instant_preview, "previews_dir", lambda: tmp_path)
    monkeypatch.setattr(heavy_cache, "_saved_channel_ids", lambda: set())
    return tmp_path


def _make_preview(root, cid):
    mp4 = root / f"{cid}.mp4"
    side = root / f"{cid}.json"
    mp4.write_bytes(b"\x00" * 64)
    side.write_text(json.dumps({"channel_id": cid}), encoding="utf-8")
    return mp4, side


def test_removal_keeps_media_and_starts_the_grace_clock(previews):
    mp4, side = _make_preview(previews, "ch1")
    now = 1_000_000.0

    heavy_cache.note_removed("ch1", now=now)

    assert mp4.is_file() and side.is_file()
    entry = heavy_cache._read()["ch1"]
    assert entry["orphaned_at"] == now
    assert entry["purge_at"] is None


def test_nothing_is_deleted_before_48h(previews):
    mp4, _ = _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)

    out = heavy_cache.sweep(now=1_000_000.0 + 47 * 3600)

    assert out["purged"] == [] and out["started"] == []
    assert out["waiting"] == ["ch1"]
    assert mp4.is_file()


def test_48h_elapsed_starts_a_one_hour_countdown_not_a_delete(previews):
    mp4, _ = _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)

    out = heavy_cache.sweep(now=1_000_000.0 + 48 * 3600)

    assert out["started"] == ["ch1"]
    assert out["purged"] == []
    assert mp4.is_file(), "the boot sweep must not delete on sight"
    assert heavy_cache._read()["ch1"]["purge_at"] == 1_000_000.0 + 48 * 3600 + 3600


def test_countdown_expiry_deletes_the_pair_and_clears_the_ledger(previews):
    mp4, side = _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)
    heavy_cache.sweep(now=1_000_000.0 + 48 * 3600)

    out = heavy_cache.sweep(now=1_000_000.0 + 48 * 3600 + 3601)

    assert out["purged"] == ["ch1"]
    assert out["files_removed"] == 2
    assert not mp4.exists() and not side.exists()
    assert heavy_cache._read() == {}


def test_readd_cancels_at_any_stage(previews):
    mp4, _ = _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)

    heavy_cache.note_added("ch1")
    assert heavy_cache._read() == {}
    assert mp4.is_file()

    # ...and after the countdown started too.
    heavy_cache.note_removed("ch1", now=1_000_000.0)
    heavy_cache.sweep(now=1_000_000.0 + 48 * 3600)
    assert heavy_cache._read()["ch1"]["purge_at"] is not None

    heavy_cache.note_added("ch1")
    out = heavy_cache.sweep(now=1_000_000.0 + 48 * 3600 + 7200)
    assert out["purged"] == []
    assert mp4.is_file()


def test_sweep_defends_itself_when_the_channel_is_saved_again(previews, monkeypatch):
    """A missed note_added (rolled-back settings, restored ledger) must not
    turn into a deleted channel."""
    mp4, _ = _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)
    heavy_cache.sweep(now=1_000_000.0 + 48 * 3600)
    monkeypatch.setattr(heavy_cache, "_saved_channel_ids", lambda: {"ch1"})

    out = heavy_cache.sweep(now=1_000_000.0 + 48 * 3600 + 7200)

    assert out["cancelled"] == ["ch1"]
    assert out["purged"] == []
    assert mp4.is_file()


def test_removing_twice_does_not_restart_the_clock(previews):
    heavy_cache.note_removed("ch1", now=1_000_000.0)

    heavy_cache.note_removed("ch1", now=1_100_000.0)

    assert heavy_cache._read()["ch1"]["orphaned_at"] == 1_000_000.0


def test_a_corrupt_ledger_is_an_empty_ledger_never_an_exception(previews):
    (previews / heavy_cache._LEDGER_NAME).write_text("{not json", encoding="utf-8")

    assert heavy_cache._read() == {}
    assert heavy_cache.pending_ids() == set()
    assert heavy_cache.sweep()["purged"] == []


def test_pending_ids_protects_the_preview_from_the_orphan_sweep(previews):
    """instant_preview deletes previews of non-saved channels; the grace window
    would be dead code without this."""
    _make_preview(previews, "kept")
    _make_preview(previews, "gone")
    heavy_cache.note_removed("kept", now=time.time())

    instant_preview._drop_orphan_previews({"other"})

    assert (previews / "kept.mp4").is_file()
    assert not (previews / "gone.mp4").exists()


def test_status_reports_the_countdown_without_touching_files(previews):
    mp4, _ = _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)

    waiting = heavy_cache.status(now=1_000_000.0 + 3600)[0]
    assert waiting["channel_id"] == "ch1"
    assert waiting["purge_at"] is None
    assert waiting["grace_remaining_sec"] == pytest.approx(47 * 3600)

    heavy_cache.sweep(now=1_000_000.0 + 48 * 3600)
    counting = heavy_cache.status(now=1_000_000.0 + 48 * 3600 + 600)[0]
    assert counting["delete_in_sec"] == pytest.approx(3000)
    assert mp4.is_file()


def test_light_cache_is_never_touched(previews):
    """The policy is media-only: a snapshot-style json that is not a preview
    sidecar must survive a full purge."""
    snapshot = previews / "some_video_480.json"
    snapshot.write_text("{}", encoding="utf-8")
    _make_preview(previews, "ch1")
    heavy_cache.note_removed("ch1", now=1_000_000.0)
    heavy_cache.sweep(now=1_000_000.0 + 48 * 3600)
    heavy_cache.sweep(now=1_000_000.0 + 48 * 3600 + 3601)

    assert snapshot.is_file()
