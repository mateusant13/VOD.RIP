"""Background yt-dlp version check — keeps YouTube extractor current without user action."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SEC = 7 * 24 * 3600


def _stamp_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "/tmp"
    return Path(base) / "VOD.RIP" / "ytdlp_last_update.txt"


def _should_check() -> bool:
    path = _stamp_path()
    if not path.is_file():
        return True
    try:
        raw = path.read_text(encoding="utf-8").strip()
        last = float(raw)
    except (OSError, ValueError):
        return True
    return (time.time() - last) >= _CHECK_INTERVAL_SEC


def _write_stamp() -> None:
    path = _stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        logger.debug("yt-dlp update stamp write failed: %s", exc)


def maybe_check_ytdlp_update() -> None:
    """pip install -U yt-dlp when last check is older than 7 days.

    No-op under a frozen install: the bundled yt-dlp and bgutil plugin
    are version-pinned by ``backend/requirements.lock``, and the
    end-user ``%LOCALAPPDATA%\\VOD.RIP`` install tree is intentionally
    read-only for self-update. Letting a frozen install pip-install
    into its own tree would either silently fail with PermissionError
    or, worse, partially succeed and create a half-updated state the
    in-app update flow can't reason about. Updates for the frozen
    installer come through ``services.updater`` (full-exe download with
    Authenticode signature verification) instead.
    """
    if getattr(sys, "frozen", False):
        logger.debug("yt-dlp auto-update skipped (frozen install)")
        return
    if not _should_check():
        return
    _write_stamp()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "bgutil-ytdlp-pot-provider"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode == 0:
            logger.info("yt-dlp/bgutil dependency update check completed")
        else:
            logger.debug("yt-dlp update check exit %s: %s", proc.returncode, (proc.stderr or "")[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("yt-dlp update check failed: %s", exc)


def schedule_ytdlp_update_check() -> threading.Thread:
    """Daemon thread — never blocks API startup."""
    def _run() -> None:
        try:
            maybe_check_ytdlp_update()
        except Exception as exc:
            logger.debug("yt-dlp update thread failed: %s", exc)

    t = threading.Thread(target=_run, name="ytdlp-update", daemon=True)
    t.start()
    return t
