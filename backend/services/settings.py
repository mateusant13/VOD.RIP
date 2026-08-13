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
    """Effective root for the large on-disk caches (whisper models, yt-dlp
    cache, preview temp, embed models).

    Precedence: VODRIP_CACHE_DIR env (test/portable override) ->
    settings.cache_dir (explicit path) -> biggest fixed drive + VOD.RIP-cache
    (auto) -> None (each cache keeps its historical default — e.g. non-Windows
    hosts with no fixed drive to pick). Per-cache env knobs
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
        # Auto-create file with defaults if it doesn't exist
        if not self._settings_file.exists():
            self.save(self._settings)

    def _load(self) -> AppSettings:
        try:
            if self._settings_file.exists():
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                if "download_folder_confirmed" not in data:
                    data["download_folder_confirmed"] = bool(
                        (data.get("download_folder") or "").strip()
                    )
                if "video_encoder" not in data:
                    data["video_encoder"] = "auto"
                settings = AppSettings(**data)
                # The write-only key flag is derived from the actual key —
                # never trust a stale persisted copy.
                settings.ai_api_key_set = bool(settings.ai_api_key)
                return settings
        except Exception:
        # ponytail: best-effort — return AppSettings(**data)
            pass
        settings = AppSettings()
        return settings

    def _autofill_ffmpeg_if_needed(self) -> None:
        """Detect ffmpeg once under lock; persist via atomic save."""
        if (self._settings.ffmpeg_path or "").strip():
            return
        from services.ytdlp_ffmpeg import _find_ffmpeg

        found = _find_ffmpeg()
        if not found:
            return
        updated = self._settings.model_copy(update={"ffmpeg_path": found})
        self.save(updated)

    def get(self) -> AppSettings:
        with self._lock:
            self._autofill_ffmpeg_if_needed()
            return self._settings.model_copy()

    def save(self, settings: AppSettings):
        with self._lock:
            self._settings = settings
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
                    f.write(settings.model_dump_json(indent=2))
                os.replace(tmp_path, str(self._settings_file))
                tmp = tmp_path
            finally:
                if tmp is not None and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except Exception:
                    # ponytail: best-effort — I/O errors only
                        pass


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
