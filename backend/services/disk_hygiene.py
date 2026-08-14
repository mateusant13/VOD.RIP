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
DEFAULT_MODEL = "small"
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


# Once per process per model kind: a legacy (heavy-cache-dir) location is
# being used until the AI-models folder is populated — log it once, not on
# every resolution.
_LEGACY_MODEL_WARNED: set = set()


def _migrated_model_dir(primary: Path, legacy: Optional[Path], what: str) -> Path:
    """Resolve a model-weight home: prefer *primary* (the AI-models folder),
    but keep using a populated *legacy* location (the old heavy-cache-dir /
    drive-root layout) until the models folder actually holds something —
    re-downloading multi-GB weights during the migration would be worse than
    a temporary split home. One clear log line when legacy is in use.
    """
    if legacy is not None and legacy.is_dir() and any(legacy.iterdir()):
        has_model = primary.is_dir() and any(
            e for e in primary.iterdir() if e.is_dir() and e.name != ".locks"
        )
        if not has_model:
            if what not in _LEGACY_MODEL_WARNED:
                _LEGACY_MODEL_WARNED.add(what)
                logger.warning(
                    "AI models folder %s is empty — reusing legacy %s models "
                    "location %s until it is populated (move the files to %s "
                    "to consolidate)",
                    primary, what, legacy, primary,
                )
            return legacy
    return primary


def whisper_cache_dir() -> Path:
    """Resolve the whisper model cache dir — the root of the AI-models folder
    (all model weights resolve under it; see archive_embed/_parakeet_cache_dir/
    archive_events for the siblings).

    Precedence: VODRIP_WHISPER_CACHE env -> settings.whisper_model_cache ->
    best drive + VOD.RIP-models (auto: speed-first, see
    best_model_cache_drive) -> %APPDATA%/VOD.RIP/whisper-models. The auto
    branches fall back to the legacy heavy-cache location
    <cache root>/whisper-models while it still holds models (migration, see
    _migrated_model_dir). Pointing it at a shared HF hub dir (e.g. BrandOps'
    models--Systran--faster-whisper-* checkpoints) lets faster-whisper reuse
    already-downloaded models without any download.
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

    legacy_root = cache_root()
    legacy = legacy_root / "whisper-models" if legacy_root is not None else None
    drive = best_model_cache_drive()
    if drive:
        return _migrated_model_dir(
            Path(drive) / "VOD.RIP-models", legacy, "whisper"
        )
    return _migrated_model_dir(
        _get_appdata_dir() / "whisper-models", legacy, "whisper"
    )


# --- AI-models auto pick (Settings > Disk "AI Models Folder" Auto) ----------
# The models folder follows its own disk-choice rule, distinct from the heavy
# cache disk (biggest free space) and the data disk (fastest): the FASTEST
# tier with room wins. AI models (whisper/parakeet/embed/PANNs weights +
# tokenizers) are small (~0.1-0.7 GB each) but loaded at every worker start
# and warmed at live-caption open, so a slow HDD is the worst home — speed
# beats headroom; ties within a tier break by most free space.
_MODEL_CACHE_MIN_FREE_BYTES = 8 * 1024**3  # room for a model + growth
# Speed credit in GB of equivalent free space: NVMe +64 GB, SSD +32 GB. An
# SSD with "adequate" free space (e.g. 100 GB -> 164 GB score) beats an HDD
# with a bit more (120 GB -> 120 GB), while a large slow HDD with >X GB free
# (X = free_ssd + credit) beats a nearly-full SSD — exactly the intended
# tradeoff. ponytail: bus-classified credit (disk_detect._speed_rank) is a
# heuristic; upgrade path is a measured rank (CrystalDiskMark-style small
# benchmark, cached like _storage_layout) if bus classification ever misleads.
def best_model_cache_drive() -> Optional[str]:
    """Drive root (e.g. 'H:\\') of the best model-cache pick.

    Speed-first: the models are small (<= ~2 GB) but loaded at every worker
    start and warmed at live-caption open — the FASTEST tier with >= 8 GB
    free wins (NVMe over SSD over HDD), ties broken by most free space.
    A slow disk is only chosen when no faster drive has room. None when no
    usable drive exists (non-Windows host, probe failures)."""
    from services.disk_detect import disk_inventory  # lazy: keeps import light

    best: Optional[str] = None
    best_rank = 99  # 1 (NVMe) < 2 (SSD) < 3 (HDD) — lowest rank wins
    best_free = -1
    for item in disk_inventory():
        if item["free_bytes"] < _MODEL_CACHE_MIN_FREE_BYTES:
            continue
        rank, free = item["speed_rank"], item["free_bytes"]
        if rank < best_rank or (rank == best_rank and free > best_free):
            best, best_rank, best_free = item["drive"], rank, free
    return best


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
    models--Systran--faster-whisper-small). A dir is kept when the
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
