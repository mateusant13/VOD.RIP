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
    with pytest.warns(UserWarning, match="younger than"):
        report = _ct._wipe_vodrip_scratch(min_age_s=3600.0)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["VOD.RIP", "ai-ask-tests-fresh", "vodrip-tests-fresh"]
    # kept ON PURPOSE, and the report says which nodes and why
    assert report.kept_young == ("ai-ask-tests-fresh", "vodrip-tests-fresh"), report
    assert report.removed == (), report


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
    with pytest.warns(UserWarning, match="younger than"):
        report = _ct._wipe_vodrip_scratch(min_age_s=3600.0)
    assert jar.exists()
    assert report.kept_young == ("yt_anon_fresh.txt",), report


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


# --- reaper honesty: a skip is a REPORT, not a silence --------------------
# The reaper used to gate on `now - mtime >= min_age_s` and `continue` when
# that was false, with nothing said. Two things were lost:
#
#  1. "declined on purpose" and "could not do its job" looked identical —
#     a reaper that quietly kept something is indistinguishable from one
#     that cleaned it, and the reader has no way to tell which happened;
#  2. a node whose NTFS mtime is a hair AHEAD of time.time() has a
#     NEGATIVE age, fails `>= 0.0`, and was therefore skipped SILENTLY.
#     That is the flake in test_wipe_surfaces_unremovable_scratch above: it
#     writes a jar and immediately expects the reaper to try (and fail) to
#     remove it; a sub-tick future mtime made the reaper skip instead, no
#     warning was raised, and pytest.warns failed with DID NOT WARN.
#     Real, intermittent, and impossible to reproduce on demand.
#
# Every test below drives mtime EXPLICITLY with os.utime. Nothing here sleeps
# and hopes: the same age is presented on every run, so these tests are red or
# green deterministically, which is the whole point — a flake cannot be pinned
# by a test that would flake with it.


def test_reaper_reaps_a_node_stamped_a_hair_in_the_future(monkeypatch, tmp_path):
    """Sub-tick clock skew is NOT "too young" — it is a node just written.

    A negative age means the FILE timestamp is ahead of our clock read, not
    that the node is fresh: Windows stamps file times from a system clock that
    is coarse (and NTP slews it), so a node written microseconds ago can carry
    an mtime slightly in the future. Treating that as "too young to touch" made
    the reaper skip a node in the caller's own root, silently.

    Skew here is 0.5 s — orders of magnitude above the in-process wall-clock
    drift between the os.utime below and the reaper's own time.time() read
    (microseconds), so the age is negative on EVERY run, not usually.
    """
    jar = tmp_path / "yt_anon_skewed.txt"
    jar.write_text("x", encoding="utf-8")
    skewed = time.time() + 0.5
    os.utime(jar, (skewed, skewed))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    report = _ct._wipe_vodrip_scratch(min_age_s=0.0)

    assert not jar.exists(), (
        "a sub-tick-future mtime is clock skew, not a reason to decline; "
        "the node was written in this same run and min_age_s=0.0 asks for it"
    )
    assert "yt_anon_skewed.txt" in report.removed, (
        f"and the reaper must say it removed it, got {report!r}"
    )


def test_reaper_reports_a_node_younger_than_the_floor(monkeypatch, tmp_path):
    """A node below the age floor is KEPT — deliberately, and said out loud."""
    fresh = tmp_path / "vodrip-tests-young"
    fresh.mkdir()
    old = time.time() - 30.0  # 30 s old, floor is an hour
    os.utime(fresh, (old, old))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    with pytest.warns(UserWarning, match="vodrip-tests-young"):
        report = _ct._wipe_vodrip_scratch(min_age_s=3600.0)

    assert fresh.exists(), "a node below the floor must survive"
    assert "vodrip-tests-young" in report.kept_young, (
        f"the decline must be in the report, got {report!r}"
    )
    assert "vodrip-tests-young" not in report.removed


def test_reaper_keeps_and_warns_about_a_bogus_future_stamp(monkeypatch, tmp_path):
    """A stamp far in the future is an ANOMALY, not freshness.

    Such a node can never age past the floor — its timestamp has to arrive
    first — so every future run skips it too, forever. The old code skipped it
    silently, which is a permanent leak wearing the costume of a policy skip.
    It is kept (we do not delete a node whose timestamp we do not understand)
    but it is reported.
    """
    far = tmp_path / "vodrip-tests-far-future"
    far.mkdir()
    stamped = time.time() + 3600.0  # an hour ahead: far beyond clock skew
    os.utime(far, (stamped, stamped))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    with pytest.warns(UserWarning, match="vodrip-tests-far-future"):
        report = _ct._wipe_vodrip_scratch(min_age_s=0.0)

    assert far.exists(), "a bogus future stamp is kept, not deleted"
    assert "vodrip-tests-far-future" in report.kept_future, (
        f"it must be reported as un-reapable, got {report!r}"
    )
    assert report.kept_young == (), "it is NOT a too-young skip; keep the reasons apart"


def test_reaper_reports_an_unremovable_node_by_name(monkeypatch, tmp_path):
    """Failure to clean stays visible, and the report names the node."""
    jar = tmp_path / "yt_anon_stuck.txt"
    jar.write_text("x", encoding="utf-8")
    old = time.time() - 2 * 3600
    os.utime(jar, (old, old))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    real_unlink = Path.unlink
    attempts = []

    def flaky_unlink(self, *a, **k):
        attempts.append(self.name)
        raise PermissionError(5, "Access is denied", str(self))

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    try:
        with pytest.warns(UserWarning, match="yt_anon_stuck.txt"):
            report = _ct._wipe_vodrip_scratch(min_age_s=0.0)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)

    assert len(attempts) >= 2, f"expected a retry, got {len(attempts)} attempt(s)"
    assert jar.exists(), "nothing could remove it"
    assert "yt_anon_stuck.txt" in report.stuck, (
        f"an uncleaned node must be in the report, got {report!r}"
    )
    assert report.kept_young == () and report.kept_future == (), (
        "a retry failure is 'stuck', not 'too young' — the reasons must not blur"
    )


def test_reaper_report_partitions_every_outcome(monkeypatch, tmp_path):
    """One sweep, five nodes, FOUR different answers — none of them silence.

    This is the property the old signature could not express: a caller could
    only learn the reaper's outcome from the filesystem afterwards, and a node
    it had no intention of removing was indistinguishable from one it cleaned.

    ``skewed`` is the interesting one. Its mtime is AHEAD of the clock, and
    with an hour's floor it must land in kept_young — clamped to an age of
    zero, judged like any node written this second — and NOT in kept_future.
    A regression that read "negative age" as "wrong timestamp" would put it
    with the node that is genuinely future-dated, and this fails.
    """
    stale = tmp_path / "vodrip-tests-stale"
    young = tmp_path / "vodrip-tests-young"
    skewed = tmp_path / "vodrip-tests-skewed"
    future = tmp_path / "vodrip-tests-future"
    stuck = tmp_path / "yt_anon_stuck.txt"
    for d in (stale, young, skewed, future):
        d.mkdir()
    stuck.write_text("x", encoding="utf-8")
    t = time.time()
    os.utime(stale, (t - 2 * 3600, t - 2 * 3600))  # older than the floor
    os.utime(stuck, (t - 2 * 3600, t - 2 * 3600))  # older than the floor...
    os.utime(young, (t - 5, t - 5))                # ...but newer
    os.utime(skewed, (t + 0.5, t + 0.5))           # clock skew, this run
    os.utime(future, (t + 7200, t + 7200))         # bogus stamp
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    real_unlink = Path.unlink

    def locked_unlink(self, *a, **k):
        raise PermissionError(5, "Access is denied", str(self))

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    try:
        with pytest.warns(UserWarning):
            report = _ct._wipe_vodrip_scratch(min_age_s=3600.0)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)

    assert report.removed == ("vodrip-tests-stale",), report
    assert report.kept_young == ("vodrip-tests-skewed", "vodrip-tests-young"), report
    assert report.kept_future == ("vodrip-tests-future",), report
    assert report.stuck == ("yt_anon_stuck.txt",), report
    assert not stale.exists()
    assert young.exists() and skewed.exists() and future.exists()
    assert stuck.exists(), "the locked node is a leak the reaper must report"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
