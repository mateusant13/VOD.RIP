"""Shared shutdown logic — cancel downloads and kill child processes."""

import logging

_logger = logging.getLogger(__name__)


def shutdown_downloads_and_children() -> None:
    """Cancel active downloads and kill child processes by PID (not name)."""
    try:
        from main import download_mgr
        download_mgr.cancel_all()
    except Exception as exc:
        _logger.warning("Error cancelling downloads: %s", exc)

    from services.os_services import kill_child_processes
    kill_child_processes()
