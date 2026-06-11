"""Shared shutdown logic — cancel downloads and kill child processes."""

import logging
import os
import subprocess

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_logger = logging.getLogger(__name__)


def shutdown_downloads_and_children() -> None:
    """Cancel active downloads and kill ffmpeg child processes."""
    try:
        from main import download_mgr
        download_mgr.cancel_all()
    except Exception as exc:
        _logger.warning("Error cancelling downloads: %s", exc)

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/IM", "ffmpeg.exe"],
                capture_output=True,
                timeout=5,
                creationflags=_NO_WINDOW,
            )
        else:
            subprocess.run(
                ["pkill", "-9", "-f", "ffmpeg"],
                capture_output=True,
                timeout=5,
            )
    except Exception as exc:
        _logger.debug("Killing ffmpeg: %s", exc)
