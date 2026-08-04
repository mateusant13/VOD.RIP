"""
Preview routes — preview sessions for HLS/MP4 playback.
"""

import asyncio
import logging
import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from models.schemas import LivePreviewRequest, LiveRotateRequest, PreviewQualityUpdateRequest, PreviewSeekRequest, PreviewSessionCreateRequest, PreviewSessionResponse, PreviewSessionStatusResponse, PreviewTimingRequest, PreviewWarmRequest, PreviewBatchWarmRequest

from deps import INFO_EXECUTOR, PREVIEW_EXECUTOR
from services.preview_service import (
    PreviewMuxPending,
    REPLAY_HLS_MARKER,
    StalePreviewUrls,
    WINDOW_HLS_MARKER,
    create_session,
    create_live_session,
    delete_session,
    open_progressive_proxy,
    open_replay_hls_proxy,
    open_segment_proxy,
    open_youtube_window_hls_proxy,
    preview_mux_ready,
    preview_playlist_ready,
    preview_segment_buffer_ready,
    preview_session_kind,
    preview_session_mux_status,
    proxy_master,
    proxy_playlist,
    proxy_segment,
    refresh_youtube_preview_session,
    resolve_upstream,
    UpstreamPreviewUnavailable,
    session_active_height,
    session_quality_labels,
    session_trim_timeline,
    session_variant_heights,
    set_session_prefer_height,
    get_session,
    schedule_youtube_window_hls_mux,
    youtube_window_hls_seek_remux,
    _is_playlist_url,
    _is_rangeable_cdn_media,
    _position_in_window_hls_mux,
    _window_hls_seg0_ready,
)

from services.youtube_diag import youtube_http_status, youtube_user_message
from services.preview_timing import log_preview_timing
from services import archive_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["preview"])


def _preview_user_message(exc: Exception) -> str:
    msg = str(exc)
    # Only wrap through YouTube diagnostics if it looks like a YouTube error
    if any(kw in msg.lower() for kw in ['youtu', 'innertube', 'player response', 'video id', 'sign in', 'age-gate']):
        return youtube_user_message(exc, preview=True)
    return msg


def _session_extract_source(session) -> str:
    if getattr(session, "platform", "") != "YouTube":
        return ""
    from services.youtube_innertube import extract_video_id
    from services.youtube_diag import last_extract_source

    vid = extract_video_id(session.vod_url) or ""
    return last_extract_source(vid)


def _preview_archive_capabilities(session) -> tuple[bool, bool]:
    """(has_transcript, has_chat) for the archived video a session maps to.

    Best-effort: an unarchived URL (or a live/master.m3u8 session) yields
    (False, False) without raising — the panel then shows empty states."""
    platform = str(getattr(session, "platform", "") or "").strip().lower()
    if platform not in archive_db.PLATFORMS:
        return False, False
    video_id = _preview_video_id(platform, getattr(session, "vod_url", "") or "")
    if not video_id:
        return False, False
    try:
        return (
            archive_db.has_transcript(platform, video_id),
            archive_db.has_chat(platform, video_id),
        )
    except Exception:
        # A DB hiccup must never fail the preview session response.
        return False, False
def _preview_channel_language(session) -> str:
    """Stored channel language of the previewed archived video ('' = none).

    WS-3: read from videos.channel_language so the preview badge shows the
    detected channel language; non-archived previews yield ''."""
    platform = str(getattr(session, "platform", "") or "").strip().lower()
    if platform not in archive_db.PLATFORMS:
        return ""
    video_id = _preview_video_id(platform, getattr(session, "vod_url", "") or "")
    if not video_id:
        return ""
    return archive_db.video_channel_language(platform, video_id) or ""


def _preview_session_response(session) -> PreviewSessionResponse:
    master = f"/api/preview/hls/{session.session_id}/master.m3u8"
    if session.kind == "progressive":
        playback = f"/api/preview/hls/{session.session_id}/stream.mp4"
    else:
        playback = master
    has_transcript, has_chat = _preview_archive_capabilities(session)
    return PreviewSessionResponse(
        session_id=session.session_id,
        master_url=master,
        playback_url=playback,
        kind=session.kind,
        variant_heights=session_variant_heights(session),
        quality_labels=session_quality_labels(session),
        active_height=session_active_height(session),
        extract_source=_session_extract_source(session),
        mux_ready=preview_mux_ready(session),
        playlist_ready=preview_playlist_ready(session),
        segment_buffer_ready=preview_segment_buffer_ready(session),
        trim_timeline=session_trim_timeline(session),
        duration_sec=float(getattr(session, "vod_duration", 0) or 0),
        window_hls_mux_start=float(getattr(session, "window_hls_mux_start", 0) or 0),
        window_hls_mux_end=float(getattr(session, "window_hls_mux_end", 0) or 0),
        cached_progressive=bool(
            getattr(session, "cached_progressive_path", None)
            and session.kind == "progressive"
        ),
        is_live=bool(getattr(session, "is_live", False)),
        growing_vod=bool(getattr(session, "growing_vod", False)),
        anonymous=bool(getattr(session, "anonymous", False)),
        archive_url=getattr(session, "archive_entry_url", None) or "",
        archive_duration=float(getattr(session, "archive_duration", 0) or 0),
        has_transcript=has_transcript,
        has_chat=has_chat,
        channel_language=_preview_channel_language(session),
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


# WS-2 preview chat panel payload. Reuses the archive_db transcript/message
# queries — no duplicated SQL — and is deliberately thin so the panel can
# fetch the whole timeline once and sync locally while seeking.
_PANEL_LIMIT_DEFAULT = 200_000
_PANEL_LIMIT_MAX = 500_000


@router.get("/api/preview/panel/{platform}/{video_id}")
async def preview_panel(
    platform: str,
    video_id: str,
    limit: int = Query(_PANEL_LIMIT_DEFAULT, ge=1, le=_PANEL_LIMIT_MAX),
):
    """Time-ordered transcript + chat rows for one archived video.

    Strict response shape:
      {transcript: [{offset_sec, text}], chat: [{offset_sec, text, username,
       spam_count}], has_transcript: bool, has_chat: bool}
    The flags mirror the preview-session capability flags so the UI can show
    empty states without loading the full payload first."""
    p = (platform or "").strip().lower()
    if p not in archive_db.PLATFORMS:
        raise HTTPException(status_code=400, detail="Unknown platform")
    return {
        "transcript": archive_db.transcript_offsets(p, video_id, limit),
        "chat": archive_db.chat_for(p, video_id, limit),
        "has_transcript": archive_db.has_transcript(p, video_id),
        "has_chat": archive_db.has_chat(p, video_id),
    }


@router.post("/api/preview/warm")
async def preview_warm(req: PreviewWarmRequest):
    """Fire-and-forget InnerTube/yt-dlp cache warm — safe on hover or URL paste.

    When ``full_mux=True`` the backend additionally schedules a background
    full-VOD mux so the first preview open is served from cache (instant).
    """
    url = (req.url or "").strip()
    if not url:
        return {"warmed": False, "reason": "empty"}
    from services.ytdlp_service import detect_platform
    if detect_platform(url) != "YouTube":
        return {"warmed": False, "reason": "not_youtube"}
    from deps import settings_mgr
    opts = settings_mgr.get()

    from services.preview_service import (
        kickoff_youtube_warm,
        kickoff_youtube_full_mux_warm,
    )

    # Plain hover warm resolves at the YouTube fast-start height (360) so the
    # warmed resolved-stream cache matches what create_session will read by
    # default for progressive previews. The full_mux warm below uses the client
    # height (typically 720) for its own mux/cache path.
    kickoff_youtube_warm(
        url,
        oauth=opts.oauth or None,
        cookies_file=opts.youtube_cookies_file or None,
        prefer_height=360,
    )
    if req.full_mux:
        kickoff_youtube_full_mux_warm(
            url,
            oauth=opts.oauth or None,
            cookies_file=opts.youtube_cookies_file or None,
            prefer_height=req.prefer_height or 720,
        )
    return {"warmed": True}


@router.post("/api/preview/warm/batch")
async def preview_warm_batch(req: PreviewBatchWarmRequest):
    """Fire-and-forget: warm all YouTube URLs in the batch."""
    urls = [u.strip() for u in (req.urls or []) if u.strip()]
    if not urls:
        return {"warmed": 0}
    logger.info("Batch warm received %d URLs", len(urls))
    from deps import INFO_EXECUTOR
    from services.preview_service import kickoff_youtube_batch_warm

    sem = asyncio.Semaphore(min(6, len(urls)))

    async def _warm_one(url: str) -> None:
        async with sem:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    INFO_EXECUTOR,
                    lambda u=url: kickoff_youtube_batch_warm(
                        u, prefer_height=req.prefer_height or 360,
                    ),
                )
            except Exception:
                pass

    tasks = [asyncio.create_task(_warm_one(u)) for u in urls]
    await asyncio.gather(*tasks, return_exceptions=True)
    log_total = len(urls)
    # Fire-and-forget: jobs are queued on WARM_EXECUTOR, resolves happen later.
    # Logging "warmed" here claimed success before any extract actually ran.
    logger.info("Batch warm queued: %d URLs (h=%d)", log_total, req.prefer_height or 360)
    return {"warmed": log_total}


@router.post("/api/preview/timing")
async def preview_timing_event(req: PreviewTimingRequest):
    """Client milestones — logged to dev console (npm run dev / uvicorn)."""
    log_preview_timing(
        platform=req.platform,
        surface=req.surface or "main",
        event=req.event or "unknown",
        open_ms=req.open_ms if req.open_ms > 0 else None,
        seek_ms=req.seek_ms if req.seek_ms > 0 else None,
        session_id=req.session_id,
        detail=req.detail,
    )
    return {"ok": True}


@router.post("/api/preview/invalidate")
async def preview_invalidate(req: PreviewWarmRequest):
    """Cold-reset every in-memory preview cache for one URL (test/debug hook).

    API binds localhost only; this just forces the next session create down
    the full cold path so timing measurements are comparable across runs.
    """
    from services.preview_service import invalidate_youtube_preview_caches

    invalidate_youtube_preview_caches(req.url or "")
    return {"ok": True}


# WS-1 preview-queue priority: previewing an archived video with no transcript
# yet enqueues (or bumps) its transcribe job to priority 1 so the worker picks
# it before any normal-queue job.
_PREVIEW_TRANSCRIBE_PRIORITY = 1


def _preview_video_id(platform: str, url: str) -> Optional[str]:
    """Archive video_id for a preview session's (platform, url), or None."""
    p = (platform or "").strip().lower()
    if p == "youtube":
        from services.youtube_innertube import extract_video_id

        return extract_video_id(url or "")
    if p == "twitch":
        m = re.search(r"twitch\.tv/videos/(\d+)", url or "", re.I)
        return m.group(1) if m else None
    if p == "kick":
        from services.kick_models import extract_vod_id

        return extract_vod_id(url or "")
    return None


def _priority_transcribe_for_preview(session) -> None:
    """Enqueue/bump a priority-1 transcribe job for a previewed archived video.

    Fires only when the archive DB has the video and transcription is enabled
    (archive_smart_enrich — the same toggle the search enrichment uses) and
    no transcript rows exist yet. An existing *queued* job is bumped to
    priority 1; running/failed/done jobs are never touched — the deterministic
    job id (PK) keeps the dedupe, so nothing double-enqueues."""
    platform = str(getattr(session, "platform", "") or "").strip().lower()
    if platform not in archive_db.PLATFORMS:
        return
    video_id = _preview_video_id(platform, getattr(session, "vod_url", "") or "")
    if not video_id:
        return
    if not archive_db.query(
        "SELECT 1 FROM videos WHERE platform = ? AND video_id = ? LIMIT 1",
        (platform, video_id),
    ):
        return  # not archived — nothing to transcribe
    try:
        from deps import settings_mgr  # lazy: same pattern as routers.archive

        enabled = bool(getattr(settings_mgr.get(), "archive_smart_enrich", True))
    except Exception:
        enabled = True
    if not enabled:
        return
    if archive_db.transcript_for(platform, video_id):
        return  # already transcribed
    job_id = f"transcribe-{platform}-{video_id}"
    latest = archive_db.latest_job(platform, video_id, kind="transcribe")
    if latest is None:
        try:
            archive_db.enqueue_job(
                job_id, "transcribe", platform, video_id,
                priority=_PREVIEW_TRANSCRIBE_PRIORITY,
            )
        except sqlite3.IntegrityError:
            pass  # raced with another enqueue — the queued row already exists
    elif latest["status"] == "queued":
        archive_db.execute(
            "UPDATE archive_jobs SET priority = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (_PREVIEW_TRANSCRIBE_PRIORITY, archive_db._now_iso(), latest["id"]),
        )


@router.post("/api/preview/session")
async def preview_create_session(req: PreviewSessionCreateRequest):
    # ponytail: crop_end=0 means "unknown" — let create_session fall back to
    # the extract's vod_duration so the click isn't blocked on /api/info/video
    # (which costs 30-60s on a cold YouTube URL). crop_end > 0 must still be
    # strictly greater than crop_start to avoid degenerate sessions.
    if req.crop_end <= req.crop_start and req.crop_end != 0:
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
    # ponytail: mark the URL the user is actively previewing in a context var so
    # stale warm jobs (channel-list scroll-over, hover) yield INFO_EXECUTOR
    # workers instead of stampeding the backend.
    from services.preview_service import set_active_youtube_preview
    set_active_youtube_preview(preview_url)
    try:
        import time as _time
        t0 = _time.monotonic()
        session = await asyncio.get_running_loop().run_in_executor(
            PREVIEW_EXECUTOR,
            lambda: create_session(
                preview_url,
                req.crop_start,
                req.crop_end,
                oauth=opts.oauth or None,
                prefer_height=req.prefer_height,
            ),
        )
        resolve_ms = (_time.monotonic() - t0) * 1000.0
        logger.info(
            "preview session created id=%s kind=%s url=%s",
            session.session_id[:8],
            session.kind,
            preview_url[:100],
        )
        from services.preview_timing import log_server_session_created
        log_server_session_created(session, resolve_ms=resolve_ms)
        # Preview-queue priority (WS-1): a previewed archived video jumps its
        # transcribe job to the front of the queue. Best-effort — a DB hiccup
        # must never fail the preview response.
        try:
            _priority_transcribe_for_preview(session)
        except Exception:
            logger.warning(
                "preview priority transcribe failed id=%s",
                session.session_id[:8],
                exc_info=True,
            )
        return _preview_session_response(session)
    except ValueError as e:
        logger.warning("preview session rejected url=%s: %s", preview_url[:100], e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        status = youtube_http_status(e)
        logger.warning("preview session rejected url=%s status=%d msg=%s", preview_url[:100], status, _preview_user_message(e))
        raise HTTPException(status_code=status, detail=_preview_user_message(e))


@router.post("/api/preview/live")
async def preview_live(req: LivePreviewRequest):
    """Open a preview session for a currently-live HLS stream.

    The frontend fetches the master playlist + headers via ``/api/live/{platform}``
    and posts them here — bypasses InnerTube/yt-dlp because live HLS lives on
    token-protected CDNs (usher.ttvnw.net, manifest.googlevideo.com, etc.) those
    extractors can't resolve. Trim + download work the same as any VOD session:
    the live master is registered under the standard preview proxy.
    """
    url = (req.url or "").strip()
    platform = (req.platform or "").strip() or "Unknown"
    if not url:
        raise HTTPException(status_code=400, detail="Live preview requires a master.m3u8 url")
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            PREVIEW_EXECUTOR,
            lambda: create_live_session(url, req.headers or {}, platform, req.vod_url or None),
        )
        logger.info(
            "live preview session created id=%s platform=%s url=%s",
            session.session_id[:8], platform, url[:100],
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("live preview session failed url=%s: %s", url[:100], e)
        raise HTTPException(status_code=500, detail=str(e))


# vaft-style midroll rotation — see services/live_capture.probe_twitch_live_master.
_USHER_CHANNEL_RE = re.compile(r"/api/channel/hls/([A-Za-z0-9_]+)\.m3u8")


def _rotate_live_twitch_session(session_id: str, player_type: Optional[str]) -> dict:
    """Swap a live Twitch session to the next vaft player type, in place.

    The proxied master URL (``/api/preview/hls/{sid}/master.m3u8``) is left
    unchanged — only the upstream ``session.master_url`` is replaced, so the
    frontend reloads the same proxy path and gets the new stream. Every player
    type is probed with a fresh GQL token; if none is ad-free the embed master
    is returned with ``ad_free=False`` and the frontend pLoader strips locally.
    """
    from services.live_capture import (
        _TWITCH_FALLBACK_PLAYER_TYPE,
        _TWITCH_PLAYER_TYPES,
        probe_twitch_live_master,
    )
    from services.preview.session import _hosts_for_url

    session = get_session(session_id)
    if not session:
        raise ValueError("Preview session not found or expired")
    if (session.platform or "").lower() != "twitch":
        raise ValueError("Rotation is only supported for Twitch live sessions")
    match = _USHER_CHANNEL_RE.search(session.master_url or "")
    if not match:
        raise ValueError("Session master is not a Twitch usher URL")
    login = match.group(1)

    if player_type:
        if player_type not in _TWITCH_PLAYER_TYPES:
            raise ValueError(f"Unknown player type: {player_type}")
        order = (player_type,) + tuple(p for p in _TWITCH_PLAYER_TYPES if p != player_type)
    else:
        current = getattr(session, "twitch_player_type", None) or _TWITCH_FALLBACK_PLAYER_TYPE
        try:
            idx = _TWITCH_PLAYER_TYPES.index(current)
        except ValueError:
            idx = 0
        nxt = _TWITCH_PLAYER_TYPES[(idx + 1) % len(_TWITCH_PLAYER_TYPES)]
        order = (nxt,) + tuple(p for p in _TWITCH_PLAYER_TYPES if p != nxt)

    # Fresh tokens per player type (skip_cache) — vaft rotates with a new token.
    probed = probe_twitch_live_master(login, player_types=order, skip_cache=True)
    if not probed:
        raise RuntimeError(f"No usher master reachable for {login}")

    session.master_url = probed["url"]
    session.entry_url = probed["url"]
    session.allowed_hosts = _hosts_for_url(probed["url"])
    session.twitch_player_type = probed["player_type"]
    # The rewritten-playlist cache is keyed by upstream URL, so old entries
    # become unreachable after the swap — no explicit purge needed.
    return {
        "ok": True,
        "player_type": probed["player_type"],
        "ad_free": probed["ad_free"],
        "url": probed["url"],
        "master_url": f"/api/preview/hls/{session_id}/master.m3u8",
        "headers": probed["headers"],
    }


@router.post("/api/preview/live/rotate/{session_id}")
async def preview_live_rotate(session_id: str, req: LiveRotateRequest):
    """Rotate a live Twitch session to the next vaft player type (midroll defense).

    Response: ``{ok, player_type, ad_free, url, master_url, headers}``. The
    proxied ``master_url`` already serves the rotated upstream — reload it in
    the player. Fails 404 when the session is not a Twitch live usher session
    (e.g. YouTube or the e2e synthetic master) — the frontend then keeps
    stripping, so playback never stalls.
    """
    try:
        return await asyncio.get_running_loop().run_in_executor(
            PREVIEW_EXECUTOR,
            lambda: _rotate_live_twitch_session(session_id, req.player_type),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.warning("live rotate failed session=%s: %s", session_id[:8], e)
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/preview/session/{session_id}/status")
async def preview_session_status(session_id: str):
    """Poll YouTube DASH mux readiness (background job started at session create)."""
    try:
        status = await asyncio.get_running_loop().run_in_executor(
            PREVIEW_EXECUTOR,
            lambda: preview_session_mux_status(session_id),
        )
        return PreviewSessionStatusResponse(**status)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/preview/session/{session_id}/seek")
async def preview_session_seek(session_id: str, req: PreviewSeekRequest):
    """Remux a window-HLS chunk around *position_sec* (large VODs mux in ≤90s slices)."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Preview session not found or expired")
    if not getattr(session, "dash_window_hls", False):
        return {"ok": True, "prewarmed": False, "remuxed": False}
    loop = asyncio.get_running_loop()
    position = float(req.position_sec)

    def _kick() -> bool:
        import time as _time
        session.timing_last_seek_mono = _time.monotonic()
        session.timing_last_seek_pos = position
        return youtube_window_hls_seek_remux(session_id, position)

    remuxed = await loop.run_in_executor(PREVIEW_EXECUTOR, _kick)
    if remuxed:
        pass
    elif not _position_in_window_hls_mux(session, position) or not _window_hls_seg0_ready(session):
        schedule_youtube_window_hls_mux(session_id)
    log_preview_timing(
        platform=getattr(session, "platform", "YouTube"),
        surface="server",
        event="seek_requested",
        session_id=session_id,
        detail=f"pos={position:.1f}s remuxed={remuxed}",
    )
    return {"ok": True, "prewarmed": True, "remuxed": remuxed}


@router.post("/api/preview/session/{session_id}/refresh")
async def preview_refresh_session(session_id: str, request: Request):
    """Re-resolve expired YouTube googlevideo URLs for an active preview session."""
    prefer_height = _parse_prefer_height_query(request) or 720
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            PREVIEW_EXECUTOR,
            lambda: refresh_youtube_preview_session(session_id, prefer_height=prefer_height),
        )
        return _preview_session_response(session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=_preview_user_message(e))


@router.post("/api/preview/session/{session_id}/quality")
async def preview_set_quality(session_id: str, req: PreviewQualityUpdateRequest):
    try:
        session = await asyncio.get_running_loop().run_in_executor(
            PREVIEW_EXECUTOR,
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
            PREVIEW_EXECUTOR,
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
    # ponytail: tier changes via POST /quality only — master?prefer_height raced POST /quality
    # and cleared ytseg cache while HLS.js fetched segments (black screen / 404).
    _ = prefer_height
    loop = asyncio.get_running_loop()
    if force_streaming:
        try:
            generate, ctype, extra_headers, status, cleanup = await loop.run_in_executor(
                PREVIEW_EXECUTOR,
                lambda: open_progressive_proxy(session_id, range_header),
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except PreviewMuxPending as e:
            raise HTTPException(status_code=503, detail=str(e))
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
            PREVIEW_EXECUTOR, proxy_master, session_id, range_header
        )
    except UpstreamPreviewUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
            headers={"Retry-After": "30"},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=_preview_user_message(e))
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
    kind = await loop.run_in_executor(PREVIEW_EXECUTOR, preview_session_kind, session_id)
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
            PREVIEW_EXECUTOR, lambda: resolve_upstream(session_id, id),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        if upstream.startswith(WINDOW_HLS_MARKER):
            # window-playlist → dynamic media playlist, window-seg-NNN → local .ts
            generate, ctype, extra_headers, status, cleanup = await loop.run_in_executor(
                PREVIEW_EXECUTOR,
                lambda: open_youtube_window_hls_proxy(session_id, id, range_header),
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
        if upstream.startswith(REPLAY_HLS_MARKER):
            # replay-playlist → ENDLIST snapshot of the archive media playlist
            generate, ctype, extra_headers, status, cleanup = await loop.run_in_executor(
                PREVIEW_EXECUTOR,
                lambda: open_replay_hls_proxy(session_id, id, range_header),
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
        if _is_playlist_url(upstream):
            data, ctype, extra_headers, status = await loop.run_in_executor(
                PREVIEW_EXECUTOR,
                lambda: proxy_playlist(session_id, upstream),
            )
            return Response(content=data, media_type=ctype, status_code=status, headers=extra_headers)
        if _is_rangeable_cdn_media(upstream):
            generate, ctype, extra_headers, status, cleanup = await loop.run_in_executor(
                PREVIEW_EXECUTOR,
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
            PREVIEW_EXECUTOR,
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
    except PreviewMuxPending as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
            headers={"Retry-After": "1"},
        )
    except RuntimeError as e:
        msg = str(e)
        if "exceeds size limit" in msg or "byte cap" in msg:
            raise HTTPException(status_code=413, detail=msg)
        if "googlevideo" in msg.lower() or "ffmpeg failed" in msg.lower():
            raise HTTPException(
                status_code=503,
                detail=msg,
                headers={"Retry-After": "1"},
            )
        raise HTTPException(status_code=502, detail=msg)
    except Exception as e:
    # ponytail: best-effort — network errors only
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/api/preview/session/{session_id}")
async def preview_delete_session(session_id: str):
    loop = asyncio.get_running_loop()
    # Capture the URL before deleting so we can clear the active-preview marker.
    from services.preview_service import get_session, set_active_youtube_preview
    sess = get_session(session_id)
    await loop.run_in_executor(PREVIEW_EXECUTOR, delete_session, session_id)
    if sess is not None:
        set_active_youtube_preview(None)
    return {"ok": True}
