"""Shared application state — breaks the ``shutdown_util → main`` circular import.

``main.py`` sets ``download_mgr`` during startup; ``shutdown_util.py`` reads
it during shutdown. Both import a third module instead of one importing the
other, which eliminates the import-cycle hazard entirely.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from services.download_manager import DownloadManager

_logger = logging.getLogger(__name__)

_download_mgr: Optional["DownloadManager"] = None


def set_download_manager(mgr: "DownloadManager") -> None:
    global _download_mgr
    _download_mgr = mgr


def get_download_manager() -> Optional["DownloadManager"]:
    return _download_mgr


def cancel_all_downloads() -> int:
    """Convenience — cancels every active download via the shared manager."""
    mgr = _download_mgr
    if mgr is None:
        _logger.warning("Download manager not initialised — nothing to cancel")
        return 0
    try:
        return mgr.cancel_all()
    except Exception as exc:
    # ponytail: best-effort — return mgr.cancel_all()
        _logger.warning("Error cancelling downloads: %s", exc)
        return 0
