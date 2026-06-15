"""Constants and pure helper functions for download management.

Extracted from services.download_manager — used by both the DownloadManager
class and the DownloadPersistence layer.
"""

# flake8: noqa: F841 (constants referenced via star-import elsewhere)

DOWNLOAD_PROGRESS_CAP = 90
POSTPROCESS_PROGRESS_FLOOR = DOWNLOAD_PROGRESS_CAP
POSTPROCESS_PROGRESS_SPAN = 9.0  # 90 -> 99 during mux/encode
_DONE_STATUSES = frozenset({"Completed", "Failed", "Cancelled"})
_UNFINISHED_STATUSES = frozenset({"Failed", "Cancelled", "Interrupted"})
_RESUMABLE_STATUSES = frozenset({"Paused", "Failed", "Cancelled", "Interrupted"})
_HISTORY_MAX_ENTRIES = 200  # Bound on-disk growth; UI also caps at 50.
_QUEUE_PERSIST_INTERVAL = 15.0
_LARGE_DOWNLOAD_MIN_BYTES = 12 * 1024 * 1024 * 1024  # ~12 GB
_LARGE_DOWNLOAD_TIMEOUT_SEC = 15 * 60  # remux + segment download for ~15 GB class
_TRANSCODE_TIMEOUT_SEC = 10 * 60  # fail fast once GPU transcode is required


def _download_timeout_seconds(estimated_bytes: int) -> float | None:
    """Wall-clock budget for large VOD jobs (~12 GB+) on the remux/download path."""
    if estimated_bytes is not None and estimated_bytes >= _LARGE_DOWNLOAD_MIN_BYTES:
        return float(_LARGE_DOWNLOAD_TIMEOUT_SEC)
    return None


def _hook_progress_percent(d: dict) -> int | None:
    """Extract a 0-100 integer percent from a yt-dlp progress dict."""
    status = d.get("status")
    if status not in ("downloading", "postprocessing"):
        return None

    raw: float | None = None
    if d.get("percent") is not None:
        raw = float(d["percent"])
    elif d.get("_percent") is not None:
        raw = float(d["_percent"])
    elif d.get("_percent_str"):
        try:
            raw = float(str(d["_percent_str"]).replace("%", "").strip())
        except ValueError:
            raw = None
    elif status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        if total and total > 0:
            raw = downloaded / float(total) * 100.0

    if raw is None:
        return None

    raw = max(0.0, min(100.0, raw))
    # FFmpeg mux/encode/finalise phases emit 90-99.9; pass through
    # without rescaling to the download cap (90%).
    if status == "postprocessing" or raw >= POSTPROCESS_PROGRESS_FLOOR:
        return min(99, round(raw))
    pct = round(raw * DOWNLOAD_PROGRESS_CAP / 100.0)
    if pct == 0 and raw > 0:
        pct = 1
    return min(DOWNLOAD_PROGRESS_CAP, pct)
