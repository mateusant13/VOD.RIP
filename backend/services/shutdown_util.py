"""Shared shutdown logic — cancel downloads and kill child processes."""

import logging

from services._app_state import cancel_all_downloads

_logger = logging.getLogger(__name__)


def shutdown_downloads_and_children() -> None:
    """Cancel active downloads and kill child processes by PID (not name)."""
    _logger.info("Shutdown: cancelling downloads and killing ffmpeg children")
    cancel_all_downloads()
    from services.os_services import kill_child_processes
    kill_child_processes()
    _logger.info("Shutdown: download/ffmpeg cleanup done")
