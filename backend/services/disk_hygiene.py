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

# Whisper model settings — same knobs as services.archive_transcribe. The
# env vars stay per-process overrides (tests/benchmarks pin env-first); the
# persisted settings fields are the user's choice whenever env is unset.
DEFAULT_MODEL = "large-v3-turbo"
MODEL_ENV = "VODRIP_WHISPER_MODEL"
CACHE_ENV = "VODRIP_WHISPER_CACHE"
# Transcripts/chat data root — env override for the settings.data_dir knob.
DATA_ENV = "VODRIP_DATA_DIR"

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
    #    processes — an active session's dir is continuously written. The root
    #    follows the data root (fastest disk); the legacy TEMP root and the
    #    legacy cache-root location (preview pre-dated the data-disk routing)
    #    are also swept so pre-move sessions age out instead of leaking.
    from services.preview._state import preview_root

    sweep_roots = {preview_root(), temp_dir / "kd_preview"}
    from services.settings import cache_root

    legacy = cache_root()
    if legacy is not None:
        sweep_roots.add(legacy / "kd_preview")
    removed = 0
    for root in sweep_roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
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
        # Whisper model cache — drop HF-style dirs that aren't the active
        # model (skipped unless the active model's dir exists, so a
        # not-yet-downloaded model never causes a wipe).
        pruned = prune_inactive_whisper_models(
            whisper_cache_dir(), active_whisper_model_id()
        )
        if pruned:
            stats["whisper_models"] = pruned
        total = sum(stats.values())
        if total:
            logger.info("Disk hygiene: removed %d stale items %s", total, stats)
        return stats
    except Exception as exc:  # ponytail: startup must never die on housekeeping
        logger.debug("Disk hygiene sweep skipped: %s", exc)
        return {}


def _get_appdata_dir() -> Path:
    from services.settings import _get_appdata_dir as _real

    return _real()


# Auto pick for the data root, resolved once per process: transcripts/chat
# are "fetched quickly" data, so the default is the FASTEST usable drive
# (matching the Settings > Storage "Auto (fastest: X:)" label). Resolved
# lazily on first use (which may probe drives for ~seconds) and cached —
# data_dir() runs on every DB open, so it must never re-probe. The env and
# settings branches above stay live; only the auto fallback is pinned, which
# matches the "takes effect after restart" contract for the data-disk pick.
_auto_data_dir: Optional[Path] = None


def data_dir() -> Path:
    """Resolve the transcripts/chat data root (archive DB + WAL/SHM).

    Precedence: VODRIP_DATA_DIR env (test/portable override) ->
    settings.data_dir (explicit path) -> fastest usable drive (auto, e.g.
    '<fastest>\\VOD.RIP-data') -> %APPDATA%/VOD.RIP when no usable drive
    exists. The Settings > Storage "data disk" pick writes data_dir to opt
    into a specific volume; the DB relocation plumbing (archive_db._db_path
    / _migrate_db_to_data_dir) moves an existing app-data DB once, so a
    clean install's DB lands on the fast drive unrequested — that IS the
    advertised auto behavior.
    """
    global _auto_data_dir
    env = os.environ.get(DATA_ENV, "").strip()
    if env:
        return Path(env)
    from deps import settings_mgr

    setting = (getattr(settings_mgr.get(), "data_dir", "") or "").strip()
    if setting:
        return Path(setting)
    if _auto_data_dir is None:
        from services.disk_detect import fastest_disk

        drive = fastest_disk()
        _auto_data_dir = (
            Path(drive) / "VOD.RIP-data" if drive else _get_appdata_dir()
        )
    return _auto_data_dir


# --- whisper model cache ---------------------------------------------------

def active_whisper_model_id() -> str:
    """Resolve the active faster-whisper model id.

    Precedence: VODRIP_WHISPER_MODEL env (per-process override, legacy knob
    pinned by test_disk_router) -> settings.whisper_model -> default.
    """
    env = os.environ.get(MODEL_ENV, "").strip()
    if env:
        return env
    from deps import settings_mgr

    return (
        getattr(settings_mgr.get(), "whisper_model", "") or ""
    ).strip() or DEFAULT_MODEL


def whisper_cache_dir() -> Path:
    """Resolve the whisper model cache dir.

    Precedence: VODRIP_WHISPER_CACHE env -> settings.whisper_model_cache ->
    cache_root()/whisper-models -> %APPDATA%/VOD.RIP/whisper-models. Pointing
    it at a shared HF hub dir (e.g. BrandOps' models--Systran--faster-whisper-*
    checkpoints) lets faster-whisper reuse already-downloaded models without
    any download.
    """
    env = os.environ.get(CACHE_ENV, "").strip()
    if env:
        return Path(env)
    from deps import settings_mgr

    setting = (
        getattr(settings_mgr.get(), "whisper_model_cache", "") or ""
    ).strip()
    if setting:
        return Path(setting)
    from services.settings import cache_root

    root = cache_root()
    if root is not None:
        return root / "whisper-models"
    return _get_appdata_dir() / "whisper-models"


def _dir_size(root: Path) -> int:
    """Recursive file-size sum; errors treated as 0 (mirrors routers.disk)."""
    total = 0
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _dir_size(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _delete_tree(path: Path) -> int:
    """Delete a dir tree; returns pre-delete byte size (0 if gone/failed)."""
    try:
        if path.is_dir() and not path.is_symlink():
            size = _dir_size(path)
            shutil.rmtree(path, ignore_errors=True)
            return size if not path.exists() else 0
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError:
        pass
    return 0


def prune_inactive_whisper_models(cache_dir: Path, active_id: str) -> int:
    """Delete HF-style model dirs that aren't the active model.

    faster-whisper cache dirs are named models--<org>--<model> (e.g.
    models--Systran--faster-whisper-large-v3-turbo). A dir is kept when the
    active model id (slashes -> '--') is contained in its name; non-HF-style
    dirs are left alone. Returns bytes freed.

    GUARD: if the active model's own dir is missing from the cache, delete
    NOTHING — pruning everything else would brick the next transcription.
    ponytail: an env/settings mismatch (active model not yet downloaded) is
    the realistic trigger; the manual "Whisper Models" cleanup is the
    explicit override for a deliberately empty cache.
    """
    active = (active_id or "").strip() or DEFAULT_MODEL
    needles = [active.replace("/", "--")]
    last_seg = active.rsplit("/", 1)[-1]
    if last_seg != active:
        needles.append(last_seg)
    if not cache_dir.is_dir():
        return 0
    active_dir = next(
        (e for e in cache_dir.iterdir() if e.is_dir() and any(n in e.name for n in needles)),
        None,
    )
    if active_dir is None:
        return 0  # guard: active model dir missing -> never prune
    freed = 0
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        if any(n in entry.name for n in needles):
            continue  # active model — never delete
        if "--" not in entry.name:
            continue  # not an HF-style model dir — leave unknown dirs alone
        freed += _delete_tree(entry)
    return freed


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
        # Pin the routed preview root (now the DATA root) and the legacy
        # cache root to the scratch tmp so the sweep is hermetic even when
        # real data/cache drives exist.
        _saved_data_dir = os.environ.get("VODRIP_DATA_DIR")
        _saved_cache_dir = os.environ.get("VODRIP_CACHE_DIR")
        os.environ["VODRIP_DATA_DIR"] = str(_tmp)
        os.environ["VODRIP_CACHE_DIR"] = str(_tmp)
        try:
            _stats = sweep_orphaned_temps(_tmp, _app)
        finally:
            for _name, _saved in (("VODRIP_DATA_DIR", _saved_data_dir), ("VODRIP_CACHE_DIR", _saved_cache_dir)):
                if _saved is None:
                    os.environ.pop(_name, None)
                else:
                    os.environ[_name] = _saved
        assert _stats["kd_preview"] == 1, f"stale session dir must be swept: {_stats}"
        assert _stats["transcribe"] == 1, f"stale transcribe dir must be swept: {_stats}"
        assert _stats["cookie_selfcheck"] == 1, f"stale selfcheck db must be swept: {_stats}"
        assert _stats["settings_tmp"] == 1, f"stale settings tmp must be swept: {_stats}"
        assert _new.exists(), "fresh session dir must never be swept"
        assert _fresh.exists(), "fresh settings tmp must never be swept"
    finally:
        shutil.rmtree(_scratch, ignore_errors=True)
