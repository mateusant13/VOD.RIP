"""Regression: concurrent settings.json saves must not lose a whole field.

The P2 from the 23c600f9 review gate: every caller persists settings by
read-modify-write (`get()` → set one field → `save()`), and `save()` used to
replace the file with whatever object it was handed. Two writers on different
threads therefore interleaved as A.read, B.read, A.write, B.write — and B's
write reverted A's field. For whole-object fields (`features`,
`saved_channels`, `window_geometry`) the reverted entry never came back:
`POST /api/settings` (download threads) racing the cookie-bridge toggle lost
one of them silently.

`SettingsManager.save()` is now a three-way merge (see its docstring), so both
survive. These tests pin the merge's contract, including the two directions
that must NOT change: a genuine edit of the same key is still last-writer-wins,
and a payload with no provenance (a startup clamp that forces a field back to
its default) still writes wholesale.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from deps import settings_mgr
from models.schemas import AppSettings
from services.settings import SettingsManager


@pytest.fixture(autouse=True)
def _isolated_settings_file():
    """Point the singleton at a scratch file and neutralize the ffmpeg probe.

    `get()` probes for ffmpeg and, on success, re-enters save() to persist the
    path — a real subprocess/syscall ladder that would add unpinned writes to
    every case here. Forcing the negative cache keeps the file's writer count
    exactly equal to the test's own.
    """
    original_file = settings_mgr._settings_file
    temp_file = original_file.parent / f"settings_interleave_{os.getpid()}.json"
    temp_file.unlink(missing_ok=True)
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    settings_mgr._ffmpeg_probe_failed = True
    # Seed the file and give the in-memory state a committed baseline, so both
    # "writers" below start from one known-good generation.
    settings_mgr.save(settings_mgr.get())
    yield
    settings_mgr._settings_file = original_file
    settings_mgr._ffmpeg_probe_failed = False
    temp_file.unlink(missing_ok=True)


def _on_disk() -> dict:
    return json.loads(settings_mgr._settings_file.read_text(encoding="utf-8"))


def test_concurrent_saves_keep_both_fields() -> None:
    """The lost update: two overlapping read-modify-writes must both land.

    The barrier forces the interleaving deterministically — both threads read
    BEFORE either writes — which is exactly the window the old wholesale
    replace lost a field in.
    """
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(mutate) -> None:
        try:
            settings = settings_mgr.get()
            mutate(settings)
            barrier.wait(timeout=10)
            settings_mgr.save(settings)
        except BaseException as exc:  # noqa: BLE001 - re-asserted on main
            errors.append(exc)
            barrier.abort()

    def set_quality(s: AppSettings) -> None:
        s.quality = "480p"

    def toggle_feature(s: AppSettings) -> None:
        # Whole-object field: under the old save this dict is what vanished.
        s.features = {**(s.features or {}), "transcribe-vod": True}

    threads = [threading.Thread(target=writer, args=(fn,)) for fn in
               (set_quality, toggle_feature)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"writer threads raised: {errors!r}"
    disk = _on_disk()
    assert disk["quality"] == "480p", "the settings POST's field was reverted"
    assert disk["features"].get("transcribe-vod") is True, (
        "the concurrent whole-object write was reverted"
    )


def test_same_key_edit_is_still_last_writer_wins() -> None:
    """The merge must not turn into a refusal to overwrite."""
    first = settings_mgr.get()
    second = settings_mgr.get()
    first.quality = "480p"
    second.quality = "2160p"
    settings_mgr.save(first)
    settings_mgr.save(second)
    assert _on_disk()["quality"] == "2160p"


def test_in_place_nested_edit_is_attributed_to_its_author() -> None:
    """A caller that mutates the dict IN PLACE still wins that key.

    No `{**...}` re-alias here on purpose: `get()` shallow-copies, so this
    edit mutates the very dict the writer's baseline references. Only a DEEP
    per-writer baseline (services/settings.py, save()) can tell "author
    changed this nested key" apart from "baseline and payload are the same
    object". Shallow-aliased, the diff reads "caller changed nothing" and the
    other writer's committed value silently reverts this edit.
    """
    # Commit a non-default features dict FIRST, so the baseline snapshot has
    # a features object at all (fresh AppSettings has features=None) and the
    # in-place edit below is a real change (True -> False), not item-assign
    # onto None.
    seed = settings_mgr.get()
    seed.features = {"live-captions": True}
    settings_mgr.save(seed)

    editor = settings_mgr.get()
    other = settings_mgr.get()
    editor.features["live-captions"] = False      # truly in-place
    other.quality = "720p"
    settings_mgr.save(other)

    returned = settings_mgr.save(editor)
    # save()'s return contract (routers/settings.py GET + POST feed it into
    # the response): the merged generation, incl. the key restored from disk.
    assert returned.quality == "720p"
    disk = _on_disk()
    assert disk["features"]["live-captions"] is False
    assert disk["quality"] == "720p"


def test_payload_without_provenance_writes_wholesale() -> None:
    """app.py's startup clamps build a fresh object to FORCE a default.

    Such a payload carries no baseline, so save() must not restore anything
    from disk — otherwise the merge would revert the clamp it exists to apply.
    """
    committed = settings_mgr.get()
    committed.quality = "480p"
    settings_mgr.save(committed)
    settings_mgr.save(AppSettings(quality="1080p"))
    assert _on_disk()["quality"] == "1080p"


def test_unreadable_disk_falls_back_to_wholesale_write() -> None:
    """A corrupt file must not brick persistence, and must not be "merged"."""
    settings_mgr._settings_file.write_text("{ not json", encoding="utf-8")
    settings = settings_mgr.get()
    settings.quality = "2160p"
    settings_mgr.save(settings)
    assert _on_disk()["quality"] == "2160p"


def test_second_manager_instance_does_not_revert_the_first() -> None:
    """`services/app_lifecycle.py:143` builds its own SettingsManager.

    Both instances write the SAME file. A sibling that read the file before
    the singleton committed must still merge on its own save — provenance has
    to survive a cold `_load()`, not just the singleton's in-memory state.
    Every assertion below compares against a NON-default value, so a revert is
    observable rather than hidden behind an equal-looking default.
    """
    seed = settings_mgr.get()
    seed.cookie_bridge_enabled = False          # non-default (field default True)
    settings_mgr.save(seed)

    sibling = SettingsManager()
    sibling._settings_file = settings_mgr._settings_file
    sibling._ffmpeg_probe_failed = True
    sibling._settings = sibling._load()         # the sibling's cold read, at T0

    # T1: the singleton commits a different key.
    newer = settings_mgr.get()
    newer.quality = "480p"                      # non-default (field default 1080p)
    settings_mgr.save(newer)

    # T2: the sibling writes its own edit, still holding the T0 snapshot.
    stale = sibling.get()
    stale.cookie_bridge_enabled = True
    sibling.save(stale)

    disk = _on_disk()
    # Pre-fix this is "1080p": the sibling's wholesale write reverted the
    # singleton's commit out from under it.
    assert disk["quality"] == "480p"
    assert disk["cookie_bridge_enabled"] is True
