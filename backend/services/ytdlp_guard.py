"""Single gate for all yt-dlp — blocks getpot_wpc (Chrome); allows bgutil PO plugin."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from services import ytdlp_env  # noqa: F401

import yt_dlp  # noqa: E402

logger = logging.getLogger(__name__)

_YTDLP_LOCK = threading.Lock()
# Why no priority/bounded acquire here: a plain Lock has no priority, and a
# timeout would break legitimate long downloads (a 2-hour VOD legitimately
# holds this lock for minutes; a bounded acquire would fail preview extracts
# that merely wait behind it). Priority for live playback is instead handled
# structurally — live sessions never touch yt-dlp (pure CDN fetches) and run
# on their own LIVE_EXECUTOR — and the pathological holder (a 0 B/s stalled
# download) is reaped by DownloadManager's stall watchdog (STALL_WATCHDOG_SEC
# = 90s), which frees this lock with a clear error instead of holding it
# forever.
_YTDLP_CHANNEL_LOCK = threading.Lock()
_FORBIDDEN_PLUGIN_MARKERS = ("getpot_wpc", "getpot-wpc")
_BLOCKED_YOUTUBE_KEYS = frozenset()
_YTDLP_FORBIDDEN_PLUGIN_CACHED: bool | None = None


def _forbidden_plugin_present() -> bool:
    global _YTDLP_FORBIDDEN_PLUGIN_CACHED
    if _YTDLP_FORBIDDEN_PLUGIN_CACHED is not None:
        return _YTDLP_FORBIDDEN_PLUGIN_CACHED
    try:
        from yt_dlp.plugins import directories as plugin_dirs

        roots = plugin_dirs()
    except Exception:
        _YTDLP_FORBIDDEN_PLUGIN_CACHED = False
        return False
    for root in roots:
        try:
            base = Path(root)
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                name = entry.name.lower()
                if any(marker in name for marker in _FORBIDDEN_PLUGIN_MARKERS):
                    _YTDLP_FORBIDDEN_PLUGIN_CACHED = True
                    return True
        except OSError:
            continue
    _YTDLP_FORBIDDEN_PLUGIN_CACHED = False
    return False


def _pot_auto_enabled() -> bool:
    try:
        from services.youtube_pot_service import pot_service_ping

        return pot_service_ping()
    except Exception:
        return False


def assert_ytdlp_safe() -> None:
    """Fail fast if getpot_wpc PO plugin is installed (spawns headless Chrome)."""
    if _forbidden_plugin_present():
        raise RuntimeError(
            "yt-dlp getpot_wpc plugin must not be installed — it spawns headless Chrome",
        )


def sanitize_ytdlp_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Strip blocked keys; enable bgutil fetch_pot when the POT server is up."""
    out = dict(opts)
    ext = out.get("extractor_args")
    if not isinstance(ext, dict):
        ext = {}
    else:
        ext = dict(ext)
    yt = dict(ext.get("youtube") or {})
    for key in _BLOCKED_YOUTUBE_KEYS:
        yt.pop(key, None)
    if _pot_auto_enabled():
        yt["fetch_pot"] = ["auto"]
    else:
        yt["fetch_pot"] = ["never"]
    bgutil = dict(ext.get("youtubepot-bgutilhttp") or {})
    if _pot_auto_enabled():
        from services.youtube_pot_service import POT_DEFAULT_BASE

        bgutil.setdefault("base_url", [POT_DEFAULT_BASE])
    ext["youtube"] = yt
    if bgutil:
        ext["youtubepot-bgutilhttp"] = bgutil
    out["extractor_args"] = ext
    return out


_EXPECTED_YTDLP_MARKERS = (
    "not currently live",
    "this video is not available",
    "video unavailable",
    "sign in to confirm your age",
    "faça login para confirmar sua idade",
    "this live stream recording is not available",
    "começará em breve",
    "foi encerrado",
    "não está disponível",
)


class _YtdlpConsoleLogger:
    """yt-dlp logger that keeps REAL errors visible but drops expected
    extractor failures (offline channel, deleted video, age gate) from the
    console — those are normal conditions surfaced in the UI/job rows."""

    def debug(self, msg):
        pass

    def warning(self, msg):
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg):
        if any(m in str(msg).lower() for m in _EXPECTED_YTDLP_MARKERS):
            logger.debug("yt-dlp expected error: %s", msg)
        else:
            logger.error("yt-dlp: %s", msg)


def ytdlp_console_logger():
    """A yt-dlp-compatible logger (debug/info/warning/error) for the `logger=`
    option that filters expected extractor failures from the console."""
    return _YtdlpConsoleLogger()


@contextlib.contextmanager
def guarded_youtube_dl(opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
    """Only supported way to construct YoutubeDL — one instance at a time."""
    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    safe.setdefault("logger", ytdlp_console_logger())
    with _YTDLP_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


@contextlib.contextmanager
def guarded_youtube_dl_channel(opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
    """Flat channel playlists — separate lock so preview segment yt-dlp can't starve lists."""
    assert_ytdlp_safe()
    safe = sanitize_ytdlp_opts(opts)
    safe.setdefault("logger", ytdlp_console_logger())
    with _YTDLP_CHANNEL_LOCK:
        with yt_dlp.YoutubeDL(safe) as ydl:
            yield ydl


YTDLP_EXTRACT_LOCK = _YTDLP_LOCK
YTDLP_CHANNEL_LOCK = _YTDLP_CHANNEL_LOCK

assert_ytdlp_safe()
out_never = sanitize_ytdlp_opts({
    "extractor_args": {"youtube": {"fetch_pot": ["auto"], "player_client": ["ios"]}},
})
assert out_never["extractor_args"]["youtube"]["fetch_pot"] in (["auto"], ["never"])
