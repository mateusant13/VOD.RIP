"""Startup disk hygiene — bounded, best-effort sweeps of orphaned temp files.

Everything here is age-guarded: anything a live process is actively writing
has a fresh mtime, so a concurrent process's in-flight file/dir is never
deleted. Runs once at app startup (see ``run_startup_hygiene``); failures are
logged, never fatal.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Preview sessions expire after 30 min of inactivity and their dirs are
# touched on every write, so a kd_preview subdir older than 24 h is either a
# crash orphan or a stale cache. Same guard for the transcribe e2e temp dirs
# and cookie-store selfcheck DBs (kill leftovers).
_ORPHAN_AGE_SEC = 24 * 3600
# settings_*.tmp exists only for the few milliseconds of an atomic settings
# write; anything a week old is a crash leftover, never an in-flight write.
_SETTINGS_TMP_AGE_SEC = 7 * 24 * 3600


def _stale(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False


def sweep_orphaned_temps(temp_dir: Path, app_data: Path) -> dict:
    """Delete age-guarded orphaned files/dirs. Returns {sweep: deleted_count}.

    Pure function of the two directories — callable with scratch dirs in
    tests/self-checks, never touches anything outside them.
    """
    orphan_cutoff = time.time() - _ORPHAN_AGE_SEC
    stats: dict[str, int] = {}

    # 1) kd_preview subdirs (sessions, preflight, caches) left by crashed
    #    processes — an active session's dir is continuously written.
    preview_root = temp_dir / "kd_preview"
    if preview_root.is_dir():
        removed = 0
        for entry in preview_root.iterdir():
            if entry.is_dir() and _stale(entry, orphan_cutoff):
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    removed += 1
        stats["kd_preview"] = removed

    # 2) vodrip-transcribe-* e2e temp dirs (the worker itself never creates
    #    them — they come from the standalone transcribe e2e test).
    removed = 0
    for entry in temp_dir.glob("vodrip-transcribe-*"):
        if entry.is_dir() and _stale(entry, orphan_cutoff):
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed += 1
    stats["transcribe"] = removed

    # 3) vodrip_cookie_selfcheck_* .db leftovers from killed module
    #    self-checks (normal runs clean up after themselves).
    removed = 0
    for entry in temp_dir.glob("vodrip_cookie_selfcheck_*"):
        if _stale(entry, orphan_cutoff):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    stats["cookie_selfcheck"] = removed

    # 4) settings_*.tmp left by an interrupted atomic settings write.
    tmp_cutoff = time.time() - _SETTINGS_TMP_AGE_SEC
    removed = 0
    if app_data.is_dir():
        for entry in app_data.glob("settings_*.tmp"):
            if _stale(entry, tmp_cutoff):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
    stats["settings_tmp"] = removed

    return stats


def run_startup_hygiene() -> dict:
    """Sweep the real user dirs. Best-effort — never raises at startup."""
    try:
        stats = sweep_orphaned_temps(
            Path(tempfile.gettempdir()), _get_appdata_dir()
        )
        total = sum(stats.values())
        if total:
            logger.info("Disk hygiene: removed %d stale temp items %s", total, stats)
        return stats
    except Exception as exc:  # ponytail: startup must never die on housekeeping
        logger.debug("Disk hygiene sweep skipped: %s", exc)
        return {}


def _get_appdata_dir() -> Path:
    from services.settings import _get_appdata_dir as _real

    return _real()


# --- module self-check (env-guarded: creates scratch temp dirs at import) --
if os.environ.get("VODRIP_DISK_HYGIENE_SELFCHECK") == "1":
    _scratch = Path(tempfile.mkdtemp(prefix="vodrip-hygiene-selfcheck-"))
    try:
        _tmp = _scratch / "tmp"
        _app = _scratch / "app"
        _kd = _tmp / "kd_preview"
        _old = _kd / "old_session"
        _new = _kd / "active_session"
        _old.mkdir(parents=True)
        _new.mkdir()
        _transcribe = _tmp / "vodrip-transcribe-abc"
        _transcribe.mkdir()
        _selfcheck_db = _tmp / "vodrip_cookie_selfcheck_123.db"
        _selfcheck_db.write_bytes(b"x")
        _settings_tmp = _app / "settings_old.tmp"
        _app.mkdir()
        _settings_tmp.write_text("{}")
        _fresh = _app / "settings_fresh.tmp"
        _fresh.write_text("{}")
        _old_stat = _old.stat()
        _transcribe_stat = _transcribe.stat()
        _selfcheck_stat = _selfcheck_db.stat()
        _settings_stat = _settings_tmp.stat()
        # Age the stale items past their guards (24h orphans; settings tmp is
        # 7 days), keep the fresh ones current.
        _old_cutoff = time.time() - 2 * 86400
        _settings_cutoff = time.time() - 8 * 86400
        for _p in (_old, _transcribe, _selfcheck_db):
            os.utime(_p, (_old_cutoff, _old_cutoff))
        os.utime(_settings_tmp, (_settings_cutoff, _settings_cutoff))
        _stats = sweep_orphaned_temps(_tmp, _app)
        assert _stats["kd_preview"] == 1, f"stale session dir must be swept: {_stats}"
        assert _stats["transcribe"] == 1, f"stale transcribe dir must be swept: {_stats}"
        assert _stats["cookie_selfcheck"] == 1, f"stale selfcheck db must be swept: {_stats}"
        assert _stats["settings_tmp"] == 1, f"stale settings tmp must be swept: {_stats}"
        assert _new.exists(), "fresh session dir must never be swept"
        assert _fresh.exists(), "fresh settings tmp must never be swept"
    finally:
        shutil.rmtree(_scratch, ignore_errors=True)
