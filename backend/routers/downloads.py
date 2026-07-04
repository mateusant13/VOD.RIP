"""
Download routes — start, cancel, pause, resume, SSE streaming.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import DownloadRequest

from deps import download_mgr, settings_mgr, INFO_EXECUTOR
from services.ytdlp_service import detect_platform
from utils import (
    build_clip_output_path,
    build_output_path,
    download_func_for_entry,
    download_dir,
    fetch_queue_meta,
    remove_download_history,
    require_hls_crop,
    safe_makedirs,
    trim_estimated_bytes,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["downloads"])

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024


# Ponytail: validate URL is a supported Kick/Twitch URL before starting download
# This prevents "not-a-url" entries from polluting the queue/history
def _cap_estimated_bytes(
    estimated_bytes: int | None,
    duration: float | None,
    crop_start: float | None,
    crop_end: float | None,
) -> tuple[int | None, float | None, float | None, str]:
    """Enforce a 1 GB cap on downloads by auto-adjusting the trim window.

    Returns (capped_bytes, adjusted_crop_start, adjusted_crop_end, warning_msg).
    """
    if estimated_bytes is None or estimated_bytes <= 0:
        return estimated_bytes, crop_start, crop_end, ""
    if estimated_bytes <= MAX_DOWNLOAD_BYTES:
        return estimated_bytes, crop_start, crop_end, ""
    if not duration or duration <= 0 or crop_start is None or crop_end is None:
        return None, crop_start, crop_end, ""
    clip_sec = crop_end - crop_start
    if clip_sec <= 0:
        return None, crop_start, crop_end, ""
    ratio = MAX_DOWNLOAD_BYTES / float(estimated_bytes)
    new_clip_sec = clip_sec * ratio
    new_crop_end = crop_start + new_clip_sec
    if new_crop_end <= crop_start:
        return None, crop_start, crop_end, ""
    capped = int(estimated_bytes * ratio)
    gb = estimated_bytes / (1024 * 1024 * 1024)
    return (
        capped, crop_start, new_crop_end,
        f"Download capped at 1 GB (was {gb:.1f} GB). Trim adjusted to {new_clip_sec:.0f}s.",
    )


def _validate_download_url(url: str) -> str:
    """Validate URL is a supported Kick/Twitch URL. Returns platform."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="URL must be http or https")
        host = parsed.netloc.lower()
        # Kick
        if host in ("kick.com", "www.kick.com") or host.endswith(".kick.com"):
            return "Kick"
        # Twitch
        if host in ("twitch.tv", "www.twitch.tv", "clips.twitch.tv") or host.endswith(".twitch.tv"):
            return "Twitch"
        # UUID (Kick VOD ID) or numeric (Twitch VOD ID)
        path = parsed.path.strip("/")
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", path):
            return "Kick"
        if re.match(r"^\d+$", path):
            return "Twitch"
    except HTTPException:
        raise
    except Exception:
        pass
    raise HTTPException(status_code=400, detail="Unsupported URL — only Kick and Twitch URLs are accepted")


@router.post("/api/download/video")
async def download_video(req: DownloadRequest):
    opts = settings_mgr.get()
    platform = _validate_download_url(req.url)
    require_hls_crop(req, platform)
    meta = await fetch_queue_meta(req.url, platform)
    output = build_output_path(req, opts, meta)
    safe_makedirs(Path(output).parent)
    download_func = None
    if platform == "Kick":
        from services.kick_api_service import download_vod_sync as kick_download_vod
        download_func = kick_download_vod
    # 1GB download cap: auto-adjust trim if estimated size exceeds limit
    raw_estimated = trim_estimated_bytes(meta, req.crop_start, req.crop_end)
    capped_bytes, adj_start, adj_end, cap_warning = _cap_estimated_bytes(
        raw_estimated, meta.get("duration"), req.crop_start, req.crop_end,
    )
    if cap_warning:
        req.crop_start = adj_start
        req.crop_end = adj_end
        logger.info("Download capped: %s", cap_warning)
    est = capped_bytes if cap_warning else raw_estimated
    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality or opts.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=req.crop_start,
        crop_end=req.crop_end,
        download_func=download_func,
        settings_mgr=settings_mgr,
        title=meta.get("title"),
        channel=meta.get("channel"),
        thumbnail=meta.get("thumbnail"),
        duration=meta.get("duration"),
        duration_string=meta.get("duration_string"),
        estimated_bytes=est,
    )
    return {"download_id": download_id, "status": "started", "cap_warning": cap_warning or None}


@router.post("/api/download/clip")
async def download_clip(req: DownloadRequest):
    opts = settings_mgr.get()
    platform = _validate_download_url(req.url)
    meta = await fetch_queue_meta(req.url, platform)
    output = build_clip_output_path(req, opts, meta)
    safe_makedirs(Path(output).parent)
    crop_start = req.crop_start
    crop_end = req.crop_end
    if crop_start is not None and crop_end is not None and crop_end <= crop_start:
        raise HTTPException(status_code=400, detail="crop_end must be after crop_start")
    # 1GB download cap for clips
    raw_estimated = trim_estimated_bytes(meta, crop_start, crop_end)
    capped_bytes, adj_start, adj_end, cap_warning = _cap_estimated_bytes(
        raw_estimated, meta.get("duration"), crop_start, crop_end,
    )
    if cap_warning:
        crop_start = adj_start
        crop_end = adj_end
        logger.info("Clip download capped: %s", cap_warning)
    est = capped_bytes if cap_warning else raw_estimated
    # Kick clips are HLS streams — route through kick_download_vod
    # (same proven path as Kick VODs) instead of yt-dlp.
    download_func = None
    if platform == "Kick":
        from services.kick_api_service import download_vod_sync as kick_download_vod
        download_func = kick_download_vod
    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality or opts.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=crop_start,
        crop_end=crop_end,
        download_func=download_func,
        download_type="clip",
        settings_mgr=settings_mgr,
        title=meta.get("title"),
        channel=meta.get("channel"),
        thumbnail=meta.get("thumbnail"),
        duration=meta.get("duration"),
        duration_string=meta.get("duration_string"),
        estimated_bytes=est,
    )
    return {"download_id": download_id, "status": "started", "cap_warning": cap_warning or None}


@router.get("/api/downloads")
async def list_downloads():
    return download_mgr.get_active_and_history()


@router.post("/api/download/{download_id}/retry")
async def retry_download(download_id: str):
    """Re-queue a failed, cancelled, or interrupted download (alias for resume)."""
    return await resume_download(download_id)


@router.post("/api/download/{download_id}/resume")
async def resume_download(download_id: str):
    opts = settings_mgr.get()
    entry = download_mgr.get_resumable_entry(download_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Download not found or not resumable")
    new_id = download_mgr.resume(
        download_id,
        oauth=opts.oauth,
        download_func=download_func_for_entry(entry),
        settings_mgr=settings_mgr,
    )
    if not new_id:
        raise HTTPException(status_code=404, detail="Download not found or not resumable")
    return {"download_id": new_id, "resumed": True}


@router.get("/api/download/{download_id}")
async def get_download(download_id: str):
    state = download_mgr.get(download_id)
    if not state:
        raise HTTPException(status_code=404, detail="Download not found")
    return state


@router.post("/api/download/{download_id}/cancel")
async def cancel_download(download_id: str):
    success = download_mgr.cancel(download_id)
    return {"cancelled": success}


@router.post("/api/download/{download_id}/pause")
async def pause_download(download_id: str):
    success = download_mgr.pause(download_id)
    if not success:
        raise HTTPException(status_code=404, detail="Download not found or not pausable")
    return {"paused": True}


@router.post("/api/download/{download_id}/remove")
async def remove_download_history(download_id: str):
    """Remove a finished download from history."""
    from utils import remove_download_history as remove_dl_history
    return remove_dl_history(download_id, download_mgr)


@router.delete("/api/download/{download_id}")
async def delete_download_history(download_id: str):
    """Remove a finished download from history."""
    from utils import remove_download_history as remove_dl_history
    return remove_dl_history(download_id, download_mgr)


@router.get("/api/download/{download_id}/stream")
async def download_stream(download_id: str, request: Request):
    queue: asyncio.Queue = asyncio.Queue()
    download_mgr.register_sse(download_id, queue)

    async def stream():
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("complete", "error"):
                    break
            except asyncio.TimeoutError:
                yield f": keepalive\n\n"

    async def stream_wrapper():
        try:
            async for chunk in stream():
                yield chunk
        finally:
            download_mgr.unregister_sse(download_id, queue)

    return StreamingResponse(
        stream_wrapper(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )