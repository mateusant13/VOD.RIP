"""Instant-preview cache ceiling + orphan reclamation — measured, not narrated.

The previews directory had NO bound and NO reclamation of deleted channels'
payloads. Both knobs below are pinned to scratch: VODRIP_DATA_DIR is re-read at
call time by disk_hygiene.data_dir(), so no test touches the live previews dir.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="preview-ceiling-")) / "archive.db")
os.environ["VODRIP_APP_DATA"] = str(Path(tempfile.mkdtemp(prefix="preview-ceiling-app-")))
os.environ["VODRIP_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="preview-ceiling-data-")))
os.environ["VODRIP_CACHE_DIR"] = str(Path(tempfile.mkdtemp(prefix="preview-ceiling-cache-")))

import pytest  # noqa: E402

from services import instant_preview  # noqa: E402
from services import probe_gate, rate_budget  # noqa: E402

_CHUNK = 1024 * 1024
# Fixture budget: 12 MiB ceiling, 16 MiB keep-free would leave nothing to do, so
# keep-free is 2 MiB and the eviction target is 10 MiB.
_CAP = 12 * _CHUNK
_KEEP_FREE = 2 * _CHUNK
_TARGET = _CAP - _KEEP_FREE


@pytest.fixture(autouse=True)
def _reset_gates():
    probe_gate.reset()
    rate_budget.reset()
    yield
    probe_gate.reset()
    rate_budget.reset()


@pytest.fixture()
def _previews(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(instant_preview, "PREVIEW_CACHE_MAX_BYTES", _CAP)
    monkeypatch.setattr(instant_preview, "PREVIEW_CACHE_KEEP_FREE_BYTES", _KEEP_FREE)
    d = tmp_path / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _channel(cid):
    return {"id": cid, "twitchSlug": cid, "kickSlug": "", "youtubeSlug": ""}


def _write_pair(d: Path, cid: str, *, size_mb: int = 5, age_sec: int = 0) -> None:
    """A preview pair with a real payload, back-dated for LRU ordering."""
    mp4 = d / (cid + ".mp4")
    js = d / (cid + ".json")
    mp4.write_bytes(b"\x00" * (size_mb * _CHUNK))
    js.write_text(json.dumps({"platform": "twitch", "vod_url": "https://x/" + cid}),
                  encoding="utf-8")
    if age_sec:
        old = os.stat(mp4).st_mtime - age_sec
        os.utime(mp4, (old, old))
        os.utime(js, (old, old))


# --- the ceiling ----------------------------------------------------------

def test_previews_dir_is_bounded_by_a_total_size_ceiling(_previews):
    """RED without the cap: three 5 MiB previews sit at 15 MiB with nothing to
    bring the directory back under 10 MiB. With the cap the pass evicts pairs
    until it is at or below the target."""
    d = _previews
    _write_pair(d, "oldest", age_sec=900)
    _write_pair(d, "older", age_sec=300)
    _write_pair(d, "newest", age_sec=0)
    assert sum(p.stat().st_size for p in d.glob("*.mp4")) == 15 * _CHUNK

    freed = instant_preview._enforce_preview_cache_ceiling()

    total = sum(p.stat().st_size for p in d.iterdir())
    assert total <= _TARGET
    assert freed > 0
    assert (d / "newest.mp4").is_file()  # the survivor is the NEWEST pair


def test_ceiling_evicts_least_recently_used_first(_previews):
    """LRU order, not id order: the untouched pair goes before the fresh one."""
    d = _previews
    _write_pair(d, "old", age_sec=900)
    _write_pair(d, "new", age_sec=0)
    instant_preview._enforce_preview_cache_ceiling()
    assert not (d / "old.mp4").exists()
    assert not (d / "old.json").exists()
    assert (d / "new.mp4").is_file()
    assert (d / "new.json").is_file()


def test_ceiling_keeps_the_pair_just_written(_previews):
    """A refresh that lands above the ceiling still serves ITS channel — the
    eviction may not delete the pair the caller just produced."""
    d = _previews
    _write_pair(d, "stale", age_sec=600, size_mb=7)
    fresh = d / "c_live.mp4"
    fresh.write_bytes(b"\x00" * (5 * _CHUNK))
    (d / "c_live.json").write_text("{}", encoding="utf-8")
    instant_preview._enforce_preview_cache_ceiling(keep=fresh)
    assert fresh.is_file()
    assert not (d / "stale.mp4").exists()


def test_ceiling_is_a_noop_below_the_limit(_previews):
    d = _previews
    _write_pair(d, "only")
    assert instant_preview._enforce_preview_cache_ceiling() == 0
    assert (d / "only.mp4").is_file()


def test_ceiling_never_raises_on_a_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path / "nowhere"))
    assert instant_preview._enforce_preview_cache_ceiling() == 0


# --- what LRU cannot do: a removed channel payload ------------------------

def test_run_pass_drops_previews_for_unsaved_channels(_previews, monkeypatch):
    """RED without the sweep: a channel removed outside the settings route
    leaves its payload behind forever, and LRU alone would eat a quiet live
    channel first (its mtime is older than a recently deleted one's)."""
    d = _previews
    _write_pair(d, "ghost", age_sec=1000)
    live = _channel("alive")
    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", lambda ch, p: {
        "platform": "twitch", "title": "t", "vod_url": "https://x/alive",
        "vod_id": "1", "video_id": None, "duration_sec": 60})
    monkeypatch.setattr(instant_preview, "_download_range",
                        lambda url, output_path, s, e: output_path.write_bytes(b"\x00" * _CHUNK))
    instant_preview.run_pass([live])
    assert not (d / "ghost.mp4").exists()
    assert not (d / "ghost.json").exists()
    assert (d / "alive.mp4").is_file()
    assert (d / "alive.json").is_file()


def test_run_pass_keeps_a_quiet_live_channel_below_the_ceiling(_previews, monkeypatch):
    """The quiet live preview (old mtime, still saved) survives a pass that is
    over the ceiling — LRU must not eat a live channel to make room."""
    d = _previews
    _write_pair(d, "quiet", age_sec=1000)
    ghost = d / "ghost.mp4"
    ghost.write_bytes(b"\x00" * (8 * _CHUNK))
    (d / "ghost.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(instant_preview, "_fetch_latest_vod", lambda ch, p: None)
    instant_preview.run_pass([_channel("quiet")])
    assert (d / "quiet.mp4").is_file()  # still saved -> not touched


def test_remove_channel_previews_still_works(_previews):
    """The explicit per-channel delete keeps its contract (settings route)."""
    d = _previews
    _write_pair(d, "gone")
    instant_preview.remove_channel_previews("gone")
    assert not (d / "gone.mp4").exists()
    assert not (d / "gone.json").exists()


# --- list_previews must never advertise a 404 ----------------------------

def test_list_previews_skips_mp4_less_sidecars(_previews):
    d = _previews
    (d / "no_mp4.json").write_text(json.dumps({"platform": "twitch"}), encoding="utf-8")
    _write_pair(d, "ok")
    rows = instant_preview.list_previews()
    assert [r["channel_id"] for r in rows] == ["ok"]


def test_list_previews_is_empty_on_a_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path / "nowhere"))
    assert instant_preview.list_previews() == []
