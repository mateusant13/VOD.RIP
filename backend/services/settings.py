"""Settings manager — persists settings to a JSON file."""

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from models.schemas import AppSettings


def _get_appdata_dir() -> Path:
    """Return the platform-appropriate user data directory for VOD.RIP.

    VODRIP_APP_DATA overrides the base dir (tests isolate all JSON/DB
    stores from real %APPDATA% before any import-time singleton binds it;
    the archive/cookie DBs use their own VODRIP_*_DB overrides)."""
    if os.environ.get("VODRIP_APP_DATA", "").strip():
        return Path(os.environ["VODRIP_APP_DATA"].strip())
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "VOD.RIP"


def cache_root() -> Optional[Path]:
    """Effective root for the large ephemeral on-disk caches (yt-dlp cache,
    transcript-fix cache, temp files). AI model weights do NOT live here —
    they resolve under the AI-models folder (whisper_model_cache).

    Precedence: VODRIP_CACHE_DIR env (test/portable override) ->
    settings.cache_dir (explicit path) -> biggest fixed drive + VOD.RIP-cache
    (auto: most free space — throwaway data grows with the archive, so
    headroom beats speed) -> None (each cache keeps its historical default —
    e.g. non-Windows hosts with no fixed drive to pick). Per-cache env knobs
    (VODRIP_WHISPER_CACHE, VODRIP_EMBED_CACHE) are checked by each cache's own
    resolver BEFORE this — env always wins over the setting.
    """
    env = os.environ.get("VODRIP_CACHE_DIR", "").strip()
    if env:
        return Path(env)
    from deps import settings_mgr

    setting = (getattr(settings_mgr.get(), "cache_dir", "") or "").strip()
    if setting:
        return Path(setting)
    from services.disk_detect import biggest_fixed_drive

    drive = biggest_fixed_drive()
    if drive:
        return Path(drive) / "VOD.RIP-cache"
    return None


class SettingsManager:
    def __init__(self):
        self._settings_dir = _get_appdata_dir()
        self._settings_file = self._settings_dir / "settings.json"
        self._lock = threading.RLock()  # get() → _autofill_ffmpeg_if_needed → save() re-enters
        self._settings = self._load()
        # ponytail: negative-cache for the ffmpeg probe — get() runs on every
        # request; when _find_ffmpeg() fails (no ffmpeg installed), re-probing
        # per call is a syscall ladder for nothing. Reset by save() so a
        # settings change re-arms one probe (upgrade path: settings field).
        self._ffmpeg_probe_failed = False
        # Auto-create file with defaults if it doesn't exist
        if not self._settings_file.exists():
            self.save(self._settings)

    def _normalize_loaded(self, data: dict) -> AppSettings:
        """Build an AppSettings from a raw settings.json payload.

        Shared by `_load()` (first read at import) and `_read_disk_settings()`
        (the merge read inside save()) so both agree on what "the file says" —
        same legacy defaults, same derived key flag, no phantom diffs.
        """
        if "download_folder_confirmed" not in data:
            data["download_folder_confirmed"] = bool(
                (data.get("download_folder") or "").strip()
            )
        if "video_encoder" not in data:
            data["video_encoder"] = "auto"
        settings = AppSettings(**data)
        # The write-only key flag is derived from the actual key — never trust
        # a stale persisted copy.
        settings.ai_api_key_set = bool(settings.ai_api_key)
        return settings

    def _load(self) -> AppSettings:
        settings = None
        try:
            if self._settings_file.exists():
                settings = self._normalize_loaded(
                    json.loads(self._settings_file.read_text(encoding="utf-8"))
                )
        except Exception:
        # ponytail: best-effort — fall back to defaults rather than crash boot
            settings = None
        if settings is None:
            settings = AppSettings()
        # Give the freshly-read state its own baseline: `get()` copies this
        # object and `model_copy()` carries private attrs forward, so the
        # first save after boot is already a three-way merge instead of a
        # blind wholesale write. That matters for the second SettingsManager
        # instance (`services/app_lifecycle.py`) — its `_load()` snapshot is
        # the only thing telling its save which keys another writer committed
        # since. Stamped here, not in `_normalize_loaded`, because the merge
        # read inside save() also uses that helper and needs no baseline.
        settings._vodrip_base = settings.model_copy(deep=True)
        return settings

    def _read_disk_settings(self) -> Optional[AppSettings]:
        """Strict read of settings.json for the merge pass in save().

        Unlike `_load()` this NEVER swallows a parse/validation error into a
        default object: a defaults-shaped object would look like "another
        writer reset everything" and the merge would happily revert real user
        settings. Returns None when the file is absent or unreadable, which
        tells save() to write the caller's payload wholesale (there is no disk
        state to preserve).

        Plain file IO only — no sqlite, no archive_db lock, no network. That
        is what makes it safe to call this under `self._lock` (see save()).
        """
        try:
            if not self._settings_file.exists():
                return None
            return self._normalize_loaded(
                json.loads(self._settings_file.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    @staticmethod
    def _three_way_merge(
        settings: AppSettings, disk: AppSettings
    ) -> AppSettings:
        """Layer on-disk commits the payload never touched back onto it.

        Pure given (payload, disk, payload._vodrip_base); does no IO and takes
        no lock, so it is directly testable. Returns `settings` unchanged (no
        copy) when there is nothing to restore.
        """
        base = settings._vodrip_base
        if base is None:
            # No provenance → nothing can be attributed to a third writer →
            # today's wholesale write.
            return settings
        restored = {
            key: getattr(disk, key)
            for key in type(settings).model_fields
            if getattr(settings, key) == getattr(base, key)
            and getattr(disk, key) != getattr(base, key)
        }
        if not restored:
            return settings
        return settings.model_copy(update=restored)

    def _autofill_ffmpeg_if_needed(self) -> None:
        """Detect ffmpeg once under lock; persist via atomic save."""
        if self._ffmpeg_probe_failed:
            return
        from services.ytdlp_ffmpeg import _find_ffmpeg

        found = _find_ffmpeg()
        if not found:
            self._ffmpeg_probe_failed = True
            return
        if found == self._settings.ffmpeg_path:
            # A prior save already persisted this path. Do not rewrite the
            # settings file on every request.
            self._ffmpeg_probe_failed = True
            return
        updated = self._settings.model_copy(update={"ffmpeg_path": found})
        self.save(updated)
        # save() re-arms the probe for explicit callers; this get() already
        # completed the probe and must not immediately repeat it.
        self._ffmpeg_probe_failed = True
    def get(self) -> AppSettings:
        with self._lock:
            self._autofill_ffmpeg_if_needed()
            return self._settings.model_copy()

    def save(self, settings: AppSettings) -> AppSettings:
        """Persist `settings` with last-writer-per-KEY semantics (CAS merge).

        The old shape of this method was a wholesale replace of the file with
        whatever object the caller held, and every caller builds that object by
        read-modify-write (`get()` → set one field → `save()`). Two such
        writers on different threads therefore lose one of them: `POST
        /api/settings` changes field X, the cookie-bridge toggle changes field
        Y, and whichever lands second reverts the first — for whole-object
        fields like `features` or `saved_channels` the reverted entry never
        comes back (P2 from the 23c600f9 review).

        So this is a three-way merge against the state the caller read from:
          * caller changed the key (payload != base)       → payload wins,
            i.e. a genuine edit is still last-writer-wins;
          * someone else committed the key (disk != base,
            payload == base)                               → disk wins, so a
            write never clobbers a field it never touched;
          * nobody touched it                              → unchanged.

        `base` (see `AppSettings._vodrip_base`) is the snapshot this payload
        was derived from, and it rides on the object, NOT on the manager: a
        writer's read and its save are separated by arbitrary work —
        `routers/settings.py::_apply_settings_update` does sqlite writes with a
        10 s busy_timeout between `get()` and `save()` — so any manager-level
        "last known state" would be re-attributed to whichever writer happened
        to commit in between. A payload with no provenance (hand-built object,
        `__init__` seeding the file) keeps the old wholesale behaviour and
        restores nothing.

        Locking: `self._lock` is an RLock (`get()` →
        `_autofill_ffmpeg_if_needed()` → `save()` re-enters) and is held across
        the read + merge + atomic write below. That is deliberately the only IO
        it spans — plain JSON file ops on a small file. NEVER widen it to
        sqlite or to `archive_db._lock`: holding any lock across a
        `busy_timeout` spin re-creates the event-loop wedge that 23c600f9 just
        removed, and the CAS must stay file-level for exactly that reason.

        Returns the object actually written (the merged result), so a caller
        that cares can read back what stuck.
        """
        with self._lock:
            prev = self._settings
            disk = self._read_disk_settings()
            merged = settings
            if disk is not None:
                merged = self._three_way_merge(settings, disk)
                # ai_api_key_set is derived from the key, never authored —
                # recompute so a restored key can't desync its own flag.
                merged.ai_api_key_set = bool(merged.ai_api_key)
            # The auto data-dir pick is pinned in disk_hygiene._auto_data_dir
            # (resolved once per process), and the DB path memo keys on it.
            # Only an ACTUAL EFFECTIVE CHANGE — a merged value that differs
            # from the previously persisted state captured in `prev` — may
            # un-pin it: saves fire constantly (window geometry, toggles),
            # and dropping the pin on every one would re-run the drive
            # inventory on the next DB touch for no reason.
            data_dir_changed = merged.data_dir != prev.data_dir
            # Baseline for this writer's NEXT save. It has to be a DEEP copy:
            # it is what the merge diffs against, and a caller may mutate a
            # nested container in place (`s = get(); s.features["x"] = True;
            # save(s)`). Shallow-aliased, that edit would read as "unchanged"
            # (payload == base) and another writer's value would revert it.
            # The previous generation is detached FIRST because deep copy
            # recurses through `_vodrip_base`: without the reset every save
            # would chain one level deeper (window-geometry saves are frequent),
            # leaking memory and making each copy slower.
            merged._vodrip_base = None
            snapshot = merged.model_copy(deep=True)
            merged._vodrip_base = snapshot
            self._settings = merged
            self._settings_dir.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file, then replace to avoid corruption
            tmp = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self._settings_dir),
                    prefix="settings_",
                    suffix=".tmp",
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(merged.model_dump_json(indent=2))
                os.replace(tmp_path, str(self._settings_file))
                tmp = tmp_path
            finally:
                if tmp is not None and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except Exception:
                    # ponytail: best-effort — I/O errors only
                        pass
            # Feature-gate consumers (routers, captioner, etc.) read the
            # memoized map on every request; any save re-arms it. Lazy import
            # — module-level would be a cycle (feature_registry -> deps ->
            # SettingsManager).
            try:
                from services.feature_registry import invalidate_enabled_cache

                invalidate_enabled_cache()
            except Exception:
            # ponytail: best-effort — invalidation must never break a save
                pass
            # Gap 3a: the archive DB path memo reads settings.data_dir — any
            # save may change the data-disk pick, so drop the memo too. Lazy
            # import, same pattern as feature_registry above.
            try:
                from services.archive_db import invalidate_db_path_cache

                invalidate_db_path_cache()
            except Exception:
                pass  # best-effort — invalidation must never break a save
            # When this save actually edited data_dir, also drop the auto
            # pick (the fallback the explicit value replaces): both caches
            # must move together, or the next resolution re-answers from the
            # stale pin (see disk_hygiene.invalidate_auto_data_dir_cache).
            # Separate try block: a failure here must neither skip the reset
            # nor undo the memo drop.
            try:
                if data_dir_changed:
                    from services.disk_hygiene import invalidate_auto_data_dir_cache

                    invalidate_auto_data_dir_cache()
            except Exception:
                pass  # best-effort — invalidation must never break a save
            # One fresh ffmpeg probe re-armed per explicit save.
            self._ffmpeg_probe_failed = False
            return merged


# --- recommended resource defaults (Settings > Recommended) -----------------
# Machine-aware suggestions for download_threads / max_cache_mb, served by
# GET /api/settings/recommended and filled via the Settings UI "Recommended"
# button. Formulas are pure given the host facts (tests inject them); the
# route probes the real host.

# Each parallel download is a yt-dlp python process + ffmpeg child; the work
# is network/disk-bound, so half the logical cores keeps the other half for
# the UI, preview muxing, transcription and the OS.
_THREADS_CORES_RATIO = 0.5
# Rough RSS per concurrent downloader (yt-dlp + ffmpeg): ~2 GB is a safe cap
# for low-RAM boxes (an 8 GB machine gets at most 4 threads from this guard).
_RAM_BYTES_PER_DOWNLOADER = 2 * 1024**3
# Clamps mirror the /api/settings validation (1-16 / 50-2000).
_THREADS_MIN, _THREADS_MAX = 2, 16
_CACHE_MB_MIN, _CACHE_MB_MAX = 50, 2000
# Max cache = 2000 MB when the drive is 100% free; scale linearly with the
# free share so a nearly-full volume is never filled further (this machine's
# disks are all >90% full, so the honest suggestion is a small cache).
_CACHE_MB_PER_FREE_PCT = 20


def _probe_cpu_count() -> int:
    return os.cpu_count() or 4


def _probe_ram_bytes() -> int:
    """Total physical RAM in bytes. Windows: GlobalMemoryStatusEx (stdlib
    ctypes, no psutil dep); POSIX: sysconf pages. Falls back to 8 GiB."""
    if sys.platform == "win32":
        try:
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
        except (AttributeError, OSError):
            pass
    elif sys.platform == "darwin":
        try:
            return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (ValueError, OSError):
            pass
    else:
        try:
            return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (ValueError, OSError):
            pass
    return 8 * 1024**3


def _recommended_threads(cpu_count: int, ram_bytes: int) -> int:
    """clamp(round(0.5 * logical cores), 2, 16), then RAM-guarded:
    at least 2 GB per downloader so an 8 GB box never suggests 16."""
    threads = max(_THREADS_MIN, min(_THREADS_MAX, round(cpu_count * _THREADS_CORES_RATIO)))
    ram_guard = max(_THREADS_MIN, int(ram_bytes // _RAM_BYTES_PER_DOWNLOADER))
    return max(_THREADS_MIN, min(threads, ram_guard))


def _recommended_cache_mb(drive_total: int, drive_free: int) -> int:
    """Free-share of the cache drive -> MB, clamped 50-2000.

    drive_total/drive_free come from the drive the heavy caches auto-land on
    (biggest fixed drive). A disk that is 100% free suggests the 2000 MB cap;
    a disk with 2.5% free hits the 50 MB floor."""
    pct_free = (drive_free / drive_total * 100.0) if drive_total > 0 else 100.0
    return max(_CACHE_MB_MIN, min(_CACHE_MB_MAX, round(pct_free * _CACHE_MB_PER_FREE_PCT)))


def recommended_resource_defaults(
    cpu_count: Optional[int] = None,
    ram_bytes: Optional[int] = None,
    drive_total: Optional[int] = None,
    drive_free: Optional[int] = None,
) -> dict:
    """download_threads + max_cache_mb suggested for this machine.

    Pure when all four facts are passed (tests); probes the host otherwise.
    The cache drive defaults to the biggest fixed drive — the same auto pick
    cache_dir uses — so the cache-size suggestion matches where the cache
    actually lands."""
    if cpu_count is None:
        cpu_count = _probe_cpu_count()
    if ram_bytes is None:
        ram_bytes = _probe_ram_bytes()
    if drive_total is None or drive_free is None:
        total = free = 0
        from services.disk_detect import biggest_fixed_drive, free_space

        drive = biggest_fixed_drive()
        if drive:
            free = free_space(drive)
            try:
                total = int(shutil.disk_usage(drive).total)
            except OSError:
                total = 0
        drive_total, drive_free = total, free
    return {
        "download_threads": _recommended_threads(cpu_count, ram_bytes),
        "max_cache_mb": _recommended_cache_mb(drive_total, drive_free),
    }
