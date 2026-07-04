"""
Video/clip info routes — identify URLs and fetch metadata.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException

from deps import INFO_EXECUTOR
from services.kick_api_service import (
    get_clip_info_sync as kick_get_clip_info_sync,
    get_video_info_sync as kick_get_video_info_sync,
)
from services.twitch_gql_service import (
    get_clip_info_sync as twitch_get_clip_info_sync,
    get_video_info_sync as twitch_get_video_info_sync,
)
from services.ytdlp_service import get_video_info, is_clip_url
from utils import explain_oserror

logger = logging.getLogger(__name__)
router = APIRouter(tags=["info"])


@router.get("/api/info/video")
async def info_video(id: str):
    try:
        lowered = id.lower()
        if is_clip_url(id):
            return await info_clip(id)
        loop = asyncio.get_running_loop()
        if "kick.com" in lowered:
            return await loop.run_in_executor(INFO_EXECUTOR, kick_get_video_info_sync, id)
        if "youtube.com" in lowered or "youtu.be" in lowered:
            info = await get_video_info(id)
            return info
        if "twitch.tv" in lowered or re.search(r"^\d+$", id.strip()):
            return await loop.run_in_executor(INFO_EXECUTOR, twitch_get_video_info_sync, id)
        info = await get_video_info(id)
        return info
    except OSError as e:
        raise HTTPException(status_code=400, detail=explain_oserror(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/info/clip")
async def info_clip(id: str):
    try:
        loop = asyncio.get_running_loop()
        lowered = id.lower()
        if "kick.com" in lowered and is_clip_url(id):
            return await loop.run_in_executor(INFO_EXECUTOR, kick_get_clip_info_sync, id)
        if is_clip_url(id) or "clips.twitch.tv" in lowered or "/clip/" in lowered:
            return await loop.run_in_executor(INFO_EXECUTOR, twitch_get_clip_info_sync, id)
        info = await get_video_info(id)
        return info
    except Exception as e:
    # ponytail: best-effort — return info
        raise HTTPException(status_code=404, detail=str(e))
