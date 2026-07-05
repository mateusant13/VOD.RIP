"""
Preview routes — preview sessions for HLS/MP4 playback.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from models.schemas import PreviewQualityUpdateRequest, PreviewSessionCreateRequest, PreviewSessionResponse, PreviewWarmRequest

from deps import INFO_EXECUTOR
from services.preview_service import (
    StalePreviewUrls,
    create_session,
    delete_session,
    open_progressive_proxy,
    open_segment_proxy,
    preview_session_kind,
    proxy_master,
    proxy_playlist,
    proxy_segment,
    refresh_youtube_preview_session,
    resolve_upstream,
    session_active_height,
    session_quality_labels,
    session_variant_heights,
    set_session_prefer_height,
    _is_playlist_url,
    _is_rangeable_cdn_media,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["preview"])


def _preview_session_response(session) -> PreviewSessionResponse:
    master = f"/api/preview/hls/{session.session_id}/master.m3u8"
    if session.kind == "progressive":
        playback = f"/api/preview/hls/{session.session_id}/stream.mp4"
    else:
        playback = master
    return PreviewSessionResponse(
        session_id=session.session_id,
        master_url=master,
        playback_url=playback,
        kind=session.kind,
        variant_heights=session_variant_heights(session),
        quality_labels=session_quality_labels(session),
        active_height=session_active_height(session),
    )


def _parse_prefer_height_query(request: Request) -> Optional[int]:
    raw = request.query_params.get("prefer_height")
    if not raw:
        return None
    try:
        height = int(raw)
    except ValueError:
        return None
    return height if height > 0 else None


@router.post("/api/preview/warm")
async def preview_warm(req: PreviewWarmRequest):
    """Fire-and-forget InnerTube/yt-dlp cache warm — safe on hover or URL paste."""
    url = (req.url or "").strip()
    if not url:
        return {"warmed": False, "reason": "empty"}
    from services.ytdlp_service import detect_platform
    if detect_platform(url) != "YouTube":
        return {"warmed": False, "reason": "not_youtube"}
    from deps import settings_mgr
    opts = settings_mgr.get()

    def _run() -> None:
        from services.ytdlp_hls import warm_youtube_extract
        warm_youtube_extract(
            url,
            oauth=opts.oauth or None,
            cookies_file=opts.youtube_cookies_file or None,
        )

    asyncio.get_running_loop().run_in_executor(INFO_EXECUTOR, _run)
    return {"warmed": True}


@router.post("/api/preview/session")
async def preview_create_session(req: PreviewSessionCreateRequest):
    if req.crop_end <= req.crop_start:
        raise HTTPException(status_code=400, detail="End must be after start")
    from deps import settings_mgr
    opts = settings_mgr.get()
    preview_url = (req.url or "").strip()
    try:
        from services.kick_models import canonical_kick_clip_url, extract_clip_id
        if "kick.com" in preview_url.lower() and extract_clip_id(preview_url):
            preview_url = canonical_kick_clip_url(preview_url)
    except ValueError:
        pass
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: create_session(
                preview_url,
                req.crop_start,
                req.crop_end,
                oauth=opts.oauth or None,
                prefer_height=req.prefer_height,
            ),
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/preview/session/{session_id}/refresh")
async def preview_refresh_session(session_id: str, request: Request):
    """Re-resolve expired YouTube googlevideo URLs for an active preview session."""
    prefer_height = _parse_prefer_height_query(request) or 480
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: refresh_youtube_preview_session(session_id, prefer_height=prefer_height),
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/preview/session/{session_id}/quality")
async def preview_set_quality(session_id: str, req: PreviewQualityUpdateRequest):
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: set_session_prefer_height(session_id, req.prefer_height),
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=500, detail=str(e))


async def _preview_apply_prefer_height(session_id: str, prefer_height: Optional[int]) -> None:
    if not prefer_height or prefer_height <= 0:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            INFO_EXECUTOR,
            lambda: set_session_prefer_height(session_id, prefer_height),
        )
    except ValueError:
        pass


async def _preview_master_response(
    session_id: str,
    range_header: Optional[str],
    prefer_height: Optional[int] = None,
    *,
    force_streaming: bool = False,
) -> Response:
    if prefer_height:
        await _preview_apply_prefer_height(session_id, prefer_height)
    loop = asyncio.get_running_loop()
    if force_streaming:
        try:
            generate, ctype, extra_headers, status, cleanup = await loop.run_in_executor(
                INFO_EXECUTOR,
                lambda: open_progressive_proxy(session_id, range_header),
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        response_headers = dict(extra_headers or {})
        if ctype and ctype != "application/octet-stream":
            response_headers.setdefault("Content-Type", ctype)
        return StreamingResponse(
            generate(),
            media_type=ctype or "application/octet-stream",
            status_code=status,
            headers=response_headers,
            background=BackgroundTask(cleanup),
        )
    try:
        data, ctype, extra_headers, status = await loop.run_in_executor(
            INFO_EXECUTOR, proxy_master, session_id, range_header
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    body: any = data
    response_headers = dict(extra_headers or {})
    response_headers.setdefault("Cache-Control", "no-cache")
    if ctype and ctype != "application/octet-stream":
        response_headers.setdefault("Content-Type", ctype)
    return Response(
        content=body,
        media_type=ctype or "application/octet-stream",
        status_code=status,
        headers=response_headers,
    )


@router.get("/api/preview/hls/{session_id}/master.m3u8")
async def preview_hls_master(session_id: str, request: Request):
    loop = asyncio.get_running_loop()
    kind = await loop.run_in_executor(INFO_EXECUTOR, preview_session_kind, session_id)
    return await _preview_master_response(
        session_id,
        request.headers.get("range"),
        _parse_prefer_height_query(request),
        force_streaming=(kind == "progressive"),
    )


@router.get("/api/preview/hls/{session_id}/stream.mp4")
async def preview_stream_mp4(session_id: str, request: Request):
    """Progressive MP4 proxy — streams googlevideo/CDN with forwarded Range headers."""
    return await _preview_master_response(
        session_id,
        request.headers.get("range"),
        _parse_prefer_height_query(request),
        force_streaming=True,
    )


@router.get("/api/preview/hls/{session_id}/resource")
async def preview_hls_resource(session_id: str, request: Request, id: Optional[str] = None):
    range_header = request.headers.get("range")
    loop = asyncio.get_running_loop()
    try:
        upstream = await loop.run_in_executor(
            None, lambda: resolve_upstream(session_id, id),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        if _is_playlist_url(upstream):
            data, ctype, extra_headers, status = await loop.run_in_executor(
                INFO_EXECUTOR,
                lambda: proxy_playlist(session_id, upstream),
            )
            return Response(content=data, media_type=ctype, status_code=status, headers=extra_headers)
        if _is_rangeable_cdn_media(upstream):
            generate, ctype, extra_headers, status, cleanup = await loop.run_in_executor(
                INFO_EXECUTOR,
                lambda: open_segment_proxy(session_id, upstream, range_header),
            )
            response_headers = dict(extra_headers or {})
            if ctype and ctype != "application/octet-stream":
                response_headers.setdefault("Content-Type", ctype)
            return StreamingResponse(
                generate(),
                media_type=ctype or "application/octet-stream",
                status_code=status,
                headers=response_headers,
                background=BackgroundTask(cleanup),
            )
        data, ctype, extra_headers, status = await loop.run_in_executor(
            INFO_EXECUTOR,
            lambda: proxy_segment(session_id, upstream, range_header),
        )
        response_headers = dict(extra_headers or {})
        if ctype and ctype != "application/octet-stream":
            response_headers.setdefault("Content-Type", ctype)
        return Response(
            content=data,
            media_type=ctype or "application/octet-stream",
            status_code=status,
            headers=response_headers,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except StalePreviewUrls as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/api/preview/session/{session_id}")
async def preview_delete_session(session_id: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, delete_session, session_id)
    return {"ok": True}
