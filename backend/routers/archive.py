"""
Archive routes — read/write the local SQLite store (chat, transcripts, video
index, dedupe, job queue). Consumers: ingestion adapters (YouTube/Twitch/Kick)
and the search UI.
"""

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from services import archive_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["archive"])


def _require_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in archive_db.PLATFORMS:
        raise HTTPException(status_code=400, detail=f"platform must be one of {archive_db.PLATFORMS}")
    return p


@router.get("/api/archive/videos")
async def archive_videos(platform: str | None = None, channel: str | None = None):
    return {"videos": archive_db.list_videos(platform, channel)}


@router.post("/api/archive/videos")
async def archive_videos_upsert(video: dict):
    try:
        archive_db.upsert_video(video)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"missing required field: {exc}") from exc
    return {"ok": True}


@router.post("/api/archive/messages")
async def archive_messages(platform: str, video_id: str, body: Any = Body(...)):
    _require_platform(platform)
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id required")
    # Accept both raw array (legacy) and {messages: [...]} (documented contract).
    messages = body.get("messages") if isinstance(body, dict) and "messages" in body else body
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="body must be a list or {messages: [...]}")
    count = archive_db.insert_messages(platform, video_id, messages)
    return {"ok": True, "inserted": count}


@router.post("/api/archive/transcripts")
async def archive_transcripts(platform: str, video_id: str, body: Any = Body(...)):
    _require_platform(platform)
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id required")
    segments = body.get("segments") if isinstance(body, dict) and "segments" in body else body
    if not isinstance(segments, list):
        raise HTTPException(status_code=400, detail="body must be a list or {segments: [...]}")
    count = archive_db.insert_transcript(platform, video_id, segments)
    return {"ok": True, "inserted": count}


@router.get("/api/archive/search")
async def archive_search(
    q: str = Query(..., min_length=1),
    platform: str | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    return {"hits": archive_db.search(q, platform=platform, limit=limit)}


@router.get("/api/archive/videos/{platform}/{video_id}/chat")
async def archive_chat_window(platform: str, video_id: str, offset: float = 0.0, half: float = 30.0):
    _require_platform(platform)
    return {"messages": archive_db.chat_window(platform, video_id, offset, half)}


@router.get("/api/archive/videos/{platform}/{video_id}/transcript")
async def archive_transcript(platform: str, video_id: str):
    _require_platform(platform)
    return {"segments": archive_db.transcript_for(platform, video_id)}


@router.get("/api/archive/dedupe")
async def archive_dedupe():
    return {"groups": archive_db.dedupe_view()}


@router.post("/api/archive/aliases")
async def archive_aliases(platform: str, video_id: str, canonical_key: str, note: str = ""):
    _require_platform(platform)
    if not canonical_key.strip():
        raise HTTPException(status_code=400, detail="canonical_key required")
    archive_db.set_alias(platform, video_id, canonical_key.strip(), note)
    return {"ok": True}


@router.get("/api/archive/jobs")
async def archive_jobs(limit: int = Query(50, ge=1, le=500)):
    return {"jobs": archive_db.list_jobs(limit)}


@router.post("/api/archive/jobs")
async def archive_jobs_enqueue(job: dict):
    job_id = str(job.get("id") or "").strip()
    kind = str(job.get("kind") or "").strip()
    platform = str(job.get("platform") or "").strip()
    video_id = str(job.get("video_id") or "").strip()
    if not (job_id and kind and video_id):
        raise HTTPException(status_code=400, detail="id, kind and video_id required")
    _require_platform(platform)
    try:
        archive_db.enqueue_job(job_id, kind, platform, video_id)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"job {job_id} already exists") from None
    return {"ok": True, "id": job_id}
