#!/usr/bin/env python3
"""DISK-01/DISK-04 + transcribe-selfcheck-gate exhaust fixes.

Mock-only tests:
  - conftest._wipe_vodrip_scratch now covers the non-vodrip leak families
    (ai-ask-tests-*, archive-chat-group-*, kd_test/, vodrip-search-lab/…)
    while still protecting fresh dirs and worker-owned vodrip-shards-*.
  - disk hygiene sweeps the worker's vodrip-transcribe-<platform>-<vid>-
    audio dirs (DISK-04 pairing: worker names + hygiene glob agree).
  - archive_transcribe's import-time selfcheck (nvidia-smi probe) is gated
    behind VODRIP_TRANSCRIBE_SELFCHECK=1.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib.util

import pytest

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")

# Load the ROOT conftest (backend/conftest.py) by file path: pytest imports
# backend/tests/conftest.py into sys.modules as plain 'conftest', so a bare
# `import conftest` would bind the wrong module.
_CT_PATH = os.path.join(os.path.dirname(__file__), "..", "conftest.py")
_ct_spec = importlib.util.spec_from_file_location("_root_conftest", _CT_PATH)
_ct = importlib.util.module_from_spec(_ct_spec)
_ct_spec.loader.exec_module(_ct)

from services.disk_hygiene import sweep_orphaned_temps  # noqa: E402


# --- DISK-01: wipe coverage ---------------------------------------------

def _make_dirs(root, names, age_sec):
    for name in names:
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "x.bin").write_bytes(b"x" * 16)
        old = time.time() - age_sec
        os.utime(p, (old, old))
    return root


def test_wipe_covers_non_vodrip_leak_families(monkeypatch, tmp_path):
    """Stale ai-ask/archive/kd_test/vodrip-search-lab scratch must be wiped."""
    _make_dirs(tmp_path, [
        "ai-ask-tests-abc", "archive-chat-group-xyz",
        "archive-enrich-v2-q", "archive-transcribe-download-z",
        "kd_test", "vodrip-search-lab", "vodrip-tests-scope-1",
        "yt-transcribe-abc", "twitch-transcribe-abc",
    ], age_sec=2 * 3600)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=0.0)
    left = sorted(p.name for p in tmp_path.iterdir())
    # tmp_path/VOD.RIP is the tests/conftest autouse app-data fixture.
    assert left == ["VOD.RIP"], f"all stale scratch must be wiped, left: {left}"


def test_wipe_keeps_fresh_dirs(monkeypatch, tmp_path):
    _make_dirs(tmp_path, ["ai-ask-tests-fresh", "vodrip-tests-fresh"], age_sec=0)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=3600.0)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["VOD.RIP", "ai-ask-tests-fresh", "vodrip-tests-fresh"]


def test_wipe_never_touches_worker_shards(monkeypatch, tmp_path):
    """vodrip-shards-* is worker-owned transient data — never wiped here."""
    _make_dirs(tmp_path, ["vodrip-shards-abc", "vodrip-tests-stale"], age_sec=2 * 3600)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=0.0)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["VOD.RIP", "vodrip-shards-abc"]


def test_wipe_ignores_unrelated_dirs(monkeypatch, tmp_path):
    _make_dirs(tmp_path, ["python", "node_modules", "my-app-data"], age_sec=2 * 3600)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=0.0)
    left = sorted(p.name for p in tmp_path.iterdir())
    # tmp_path/VOD.RIP is the tests/conftest autouse app-data fixture — the
    # wipe must leave it AND the unrelated dirs alone.
    assert left == ["VOD.RIP", "my-app-data", "node_modules", "python"]


# --- DISK-01b: file-shaped scratch (yt_anon_*) ---------------------------
# services/youtube_session.py:98 mkstemp(prefix="yt_anon_", suffix=".txt")
# puts a regular FILE in the system temp dir. rmtree is dir-only, so with
# ignore_errors=True the NotADirectoryError was swallowed and the cookie jar
# leaked forever — invisible, and it broke test_wipe_covers_* in a full run.

def test_wipe_removes_file_shaped_scratch(monkeypatch, tmp_path):
    """yt_anon_*.txt is a FILE: known prefix + a generic node deleter."""
    jar = tmp_path / "yt_anon_r1t0zag9.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    keep = tmp_path / "user-notes.txt"
    keep.write_text("mine", encoding="utf-8")
    old = time.time() - 2 * 3600
    os.utime(jar, (old, old))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=0.0)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert not jar.exists(), f"file-shaped scratch must be wiped, left: {left}"
    assert keep.exists(), "an unrelated file is never scratch"


def test_wipe_keeps_fresh_files(monkeypatch, tmp_path):
    """min_age_s still gates file-shaped scratch — a live jar survives."""
    jar = tmp_path / "yt_anon_fresh.txt"
    jar.write_text("x", encoding="utf-8")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=3600.0)
    assert jar.exists()


def test_wipe_surfaces_unremovable_scratch(monkeypatch, tmp_path):
    """A node that cannot be removed is retried, then SURFACED.

    The old `shutil.rmtree(..., ignore_errors=True)` swallowed every
    failure, so an unremovable leak was indistinguishable from a clean wipe.
    """
    jar = tmp_path / "yt_anon_locked.txt"
    jar.write_text("x", encoding="utf-8")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    real_unlink = Path.unlink
    attempts = []

    def flaky_unlink(self, *a, **k):
        attempts.append(self.name)
        raise PermissionError(5, "Access is denied", str(self))

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.warns(UserWarning, match="yt_anon_locked.txt"):
        _ct._wipe_vodrip_scratch(min_age_s=0.0)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    # retried, not given up on after one shot...
    assert len(attempts) >= 2, f"expected a retry, got {len(attempts)} attempt(s)"
    # ...and still present, because nothing could remove it.
    assert jar.exists()


def test_wipe_never_follows_a_link_out_of_temp(monkeypatch, tmp_path):
    """The real data root (%APPDATA%/VOD.RIP) is not under temp, and a
    LINK inside temp must never be recursed into — the wipe unlinks the
    link, leaving its target untouched."""
    outside = tmp_path / "outside" / "VOD.RIP"
    outside.mkdir(parents=True)
    keep = outside / "archive.db"
    keep.write_bytes(b"real user data")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    link = temp_root / "vodrip-tests-link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable (no developer mode/privilege)")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(temp_root))
    _ct._wipe_vodrip_scratch(min_age_s=0.0)
    assert keep.exists(), "a symlinked scratch entry must not delete its target"
    assert keep.read_bytes() == b"real user data"


def test_wipe_leaves_bare_vodrip_dir_alone(monkeypatch, tmp_path):
    """Bare `VOD.RIP` in the system temp dir is APP scratch, not test scratch.

    routers/disk.py:79, services/updater.py:169 and routers/live.py:1049 all
    create gettempdir()/"VOD.RIP"* for the RUNNING app (bgutil-pot, GPU-ASR
    stamp, live clips). Adding it to the wipe lists would delete live app
    data; in a test's own tmp_path it is the autouse _isolated_download_appdata
    dir every other assertion here requires to survive.
    """
    appdir = tmp_path / "VOD.RIP"
    # exist_ok: the autouse _isolated_download_appdata fixture (tests/conftest.py:99)
    # has ALREADY created this exact dir — the same one every other assertion
    # in this module requires to survive.
    appdir.mkdir(exist_ok=True)
    (appdir / "archive.db").write_bytes(b"real user data")
    old = time.time() - 48 * 3600
    os.utime(appdir, (old, old))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _ct._wipe_vodrip_scratch(min_age_s=0.0)
    assert (appdir / "archive.db").exists()


# --- DISK-04: hygiene pairs with worker prefix --------------------------

def test_hygiene_sweeps_worker_audio_dirs(tmp_path):
    """The worker's vodrip-transcribe-<platform>-<vid>- dirs are swept by
    the same glob that reaps the e2e scratch (24 h orphan guard)."""
    stale = tmp_path / "vodrip-transcribe-twitch-2833943352-"
    stale.mkdir()
    (stale / "audio.wav").write_bytes(b"x")
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))
    fresh = tmp_path / "vodrip-transcribe-youtube-aaaaaaaaaaa-"
    fresh.mkdir()
    stats = sweep_orphaned_temps(tmp_path, tmp_path / "appdata")
    assert stats["transcribe"] == 1
    assert not stale.exists() and fresh.exists()


def test_hygiene_sweeps_legacy_yt_transcribe_dirs(tmp_path):
    """Pre-fix yt-transcribe-* / twitch-transcribe-* leftovers must be swept."""
    stale = tmp_path / "yt-transcribe-dQw4w9WgXcQ-"
    stale.mkdir()
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))
    kick = tmp_path / "kick-transcribe-xyz-"
    kick.mkdir()
    os.utime(kick, (old, old))
    stats = sweep_orphaned_temps(tmp_path, tmp_path / "appdata")
    assert stats["transcribe"] == 2
    assert not stale.exists() and not kick.exists()


# --- fix-on-sight: transcribe selfcheck gate ----------------------------

def test_transcribe_selfcheck_gated_behind_env():
    """Importing archive_transcribe must NOT spawn the nvidia-smi probe by
    default; VODRIP_TRANSCRIBE_SELFCHECK=1 opts the import-time check in."""
    backend = os.path.join(os.path.dirname(__file__), "..")
    code = (
        "import subprocess, os, sys\n"
        "calls = []\n"
        "def fake(*a, **k):\n"
        "    calls.append(a)\n"
        "    raise FileNotFoundError\n"
        "subprocess.run = fake\n"
        "sys.path.insert(0, %r)\n"
        "os.environ['VODRIP_NO_DAEMONS'] = '1'\n"
        "import services.archive_transcribe\n"
        "print(len(calls))\n"
    ) % backend
    base_env = dict(os.environ)
    base_env.pop("VODRIP_TRANSCRIBE_SELFCHECK", None)
    base_env["VODRIP_NO_DAEMONS"] = "1"

    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=base_env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0", "no probe when the selfcheck is not opted in"

    env_on = dict(base_env, VODRIP_TRANSCRIBE_SELFCHECK="1")
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env_on,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1", "selfcheck opt-in must run the probe"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
