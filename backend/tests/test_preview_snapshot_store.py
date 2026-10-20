"""Disk tier of the warm session-snapshot cache.

The point of these tests is the contract the click path depends on: a shell
stored by one process is usable by the next, types survive JSON, and a stale
or corrupt entry MISSES instead of serving an expired googlevideo URL.
"""
from __future__ import annotations

import json
import os

import pytest
from services.preview import snapshot_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_store, "_dir", lambda: tmp_path)
    return tmp_path


def _snapshot(**over):
    snap = {
        "session_id": "abc123",
        "cache_dir": "C:/tmp/sess",
        "master_url": "/api/preview/hls/abc123/master.m3u8",
        "entry_url": "https://cdn.example/v.m3u8",
        "platform": "YouTube",
        "http_headers": {"User-Agent": "x"},
        "allowed_hosts": {"cdn.example", "rr1.example"},
        "kind": "hls",
        "preview_audio_url": None,
        "variant_muxed": {"480": True},
        "variant_entries": [(480, "https://cdn.example/480.m3u8"), (1080, "https://cdn.example/1080.m3u8")],
        "custom_master": "#EXTM3U",
        "dash_window_hls": True,
        "vod_duration": 3600.0,
        "cached_progressive_path": None,
        "explore_yt_info": {"big": "blob"},
    }
    snap.update(over)
    return snap


def test_roundtrip_restores_set_and_tuple_types(store):
    snapshot_store.save("vid1", 480, _snapshot())

    loaded = snapshot_store.load("vid1", 480, ttl_sec=3600)

    assert loaded is not None
    assert loaded["allowed_hosts"] == {"cdn.example", "rr1.example"}
    assert loaded["variant_entries"] == [(480, "https://cdn.example/480.m3u8"),
                                         (1080, "https://cdn.example/1080.m3u8")]
    assert loaded["master_url"] == "/api/preview/hls/abc123/master.m3u8"
    assert loaded["mux_status"] == "pending"


def test_explore_yt_info_is_not_persisted(store):
    """It is the field most likely to be unserialisable and the reuse path
    never reads it."""
    snapshot_store.save("vid1", 480, _snapshot())

    raw = json.loads((store / "vid1_480.json").read_text(encoding="utf-8"))

    assert "explore_yt_info" not in raw["snapshot"]


def test_expired_entry_misses_and_is_deleted(store):
    snapshot_store.save("vid1", 480, _snapshot())
    path = store / "vid1_480.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["saved_at"] = 0  # ancient
    path.write_text(json.dumps(body), encoding="utf-8")

    assert snapshot_store.load("vid1", 480, ttl_sec=3600) is None
    assert not path.exists()


def test_closest_prefers_nearest_then_lower(store):
    snapshot_store.save("vid1", 360, _snapshot())
    snapshot_store.save("vid1", 1080, _snapshot())

    # wanting 720: both are 360 away -> the LOWER height wins (never upscale)
    assert snapshot_store.load_closest("vid1", 720, 3600)[0] == 360
    # wanting 1080 exact
    assert snapshot_store.load_closest("vid1", 1080, 3600)[0] == 1080
    # wanting 240 -> 360
    assert snapshot_store.load_closest("vid1", 240, 3600)[0] == 360


def test_corrupt_file_is_a_miss_not_a_crash(store):
    (store / "vid1_480.json").write_text("{not json", encoding="utf-8")

    assert snapshot_store.load("vid1", 480, 3600) is None
    assert snapshot_store.load_closest("vid1", 480, 3600) is None


def test_unsafe_video_id_cannot_escape_the_directory(store):
    snapshot_store.save("../../evil", 480, _snapshot())

    names = [p.name for p in store.iterdir()]
    safe = [n for n in names if n.endswith("_480.json")]
    assert safe and all("/" not in n and "\\" not in n and n.startswith("_") for n in safe)
    assert not (store.parent / "evil_480.json").exists()


def test_save_is_best_effort_when_the_directory_is_unwritable(store, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot_store.os, "replace", boom)
    snapshot_store.save("vid1", 480, _snapshot())  # must not raise
    assert snapshot_store.load("vid1", 480, 3600) is None


def test_delete_drops_one_height_or_all(store):
    snapshot_store.save("vid1", 480, _snapshot())
    snapshot_store.save("vid1", 1080, _snapshot())
    snapshot_store.save("vid2", 480, _snapshot())

    snapshot_store.delete("vid1", 480)
    assert snapshot_store.load("vid1", 480, 3600) is None
    assert snapshot_store.load("vid1", 1080, 3600) is not None

    snapshot_store.delete("vid1")
    assert snapshot_store.load("vid1", 1080, 3600) is None
    assert snapshot_store.load("vid2", 480, 3600) is not None


def test_prune_keeps_only_the_requested_videos(store):
    for vid in ("keep1", "keep2", "drop1", "drop2"):
        snapshot_store.save(vid, 480, _snapshot())

    removed = snapshot_store.prune({"keep1", "keep2"}, ttl_sec=3600)

    assert removed == 2
    assert snapshot_store.load("keep1", 480, 3600) is not None
    assert snapshot_store.load("drop1", 480, 3600) is None


def test_prune_drops_expired_files(store):
    snapshot_store.save("old", 480, _snapshot())
    path = store / "old_480.json"
    old = 0
    os.utime(path, (old, old))

    assert snapshot_store.prune(set(), ttl_sec=60) == 1
