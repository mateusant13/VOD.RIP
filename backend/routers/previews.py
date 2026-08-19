"""Instant preview endpoints — status list + local MP4 with HTTP Range.

Contract (shared with the frontend):
- GET /api/previews/status -> {"previews":[{channel_id, platform, title,
  vod_url, vod_id, video_id, generated_at, media_url}]} — empty list allowed,
  never 500.
- GET /api/previews/<channel_id>/media -> the local 6s MP4; single-range
  Range requests return 206 with the correct byte slice (mirrors the app's
  local-file proxy in preview/session.py — video-element seeking).
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from services import instant_preview

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/previews", tags=["previews"])

_CHUNK_BYTES = 256 * 1024


@router.get("/status")
def previews_status() -> dict:
    return {"previews": instant_preview.list_previews()}


@router.get("/{channel_id}/media")
def preview_media(channel_id: str, request: Request):
    # ponytail: path traversal guard — channel_id is URL-decoded by FastAPI;
    # reject any value that could escape the previews dir.
    cid = (channel_id or "").strip()
    if not cid or ".." in cid or "/" in cid or "\\" in cid:
        raise HTTPException(status_code=400, detail="Invalid channel_id")
    mp4 = instant_preview.previews_dir() / f"{cid}.mp4"
    if not mp4.is_file():
        raise HTTPException(status_code=404, detail="Preview not found")
    size = mp4.stat().st_size
    start = 0
    end = size - 1
    status = 200
    range_header = request.headers.get("range")
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
        if m:
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            else:
                end = size - 1
            # Out-of-bounds ranges clamp to the whole file (app convention).
            if start >= size:
                start = 0
                end = size - 1
            status = 206
    length = max(0, end - start + 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-cache",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    def _generate():
        with open(mp4, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _generate(), media_type="video/mp4", status_code=status, headers=headers
    )
