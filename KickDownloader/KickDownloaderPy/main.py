"""Kick & Twitch Downloader — FastAPI server with yt-dlp backend."""

import asyncio
import json
import os
import platform
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from models.schemas import (
    AppSettings,
    DownloadRequest,
    DownloadState,
    SettingsUpdate,
    VideoInfo,
)
from services.download_manager import DownloadManager
from services.settings import SettingsManager
import yt_dlp
from services.ytdlp_service import detect_platform, get_video_info

app = FastAPI(title="Kick & Twitch Downloader", version="2.0.0")

settings_mgr = SettingsManager()
download_mgr = DownloadManager(max_workers=4)

# Mount static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ==================== ROUTES ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = Path(__file__).parent / "static" / "index.html"
    if index_file.exists():
        content = index_file.read_text(encoding="utf-8")
        return HTMLResponse(content, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    return HTMLResponse("<h1>Kick & Twitch Downloader</h1><p>Frontend not found. Place static/index.html in the static/ folder.</p>")


# --- Settings ---

@app.get("/api/settings", response_model=AppSettings)
async def get_settings():
    return settings_mgr.get()


@app.post("/api/settings", response_model=AppSettings)
async def update_settings(update: SettingsUpdate):
    current = settings_mgr.get()
    if update.download_threads is not None:
        current.download_threads = max(1, min(16, update.download_threads))
    if update.max_cache_mb is not None:
        current.max_cache_mb = max(50, min(2000, update.max_cache_mb))
    if update.throttle_kib is not None:
        current.throttle_kib = update.throttle_kib
    if update.ffmpeg_path is not None:
        current.ffmpeg_path = update.ffmpeg_path
    if update.temp_folder is not None:
        current.temp_folder = update.temp_folder
    if update.oauth is not None:
        current.oauth = update.oauth
    if update.quality is not None:
        current.quality = update.quality
    settings_mgr.save(current)
    return current


# --- Channel Videos ---

@app.get("/api/channel/videos")
async def channel_videos(url: str, limit: int = 20):
    url = unquote(url)
    try:
        # Auto-prepend protocol if missing (e.g. "twitch.tv/asmongold" -> "https://twitch.tv/asmongold")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        platform = detect_platform(url)

        if platform == "Unknown":
            raise ValueError(
                f"Could not detect platform. Enter a full channel URL like:\n"
                f"  • https://www.twitch.tv/channelname\n"
                f"  • https://kick.com/channelname"
            )

        # Build the channel videos URL
        if platform == "Twitch":
            m = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", url)
            channel = m.group(1) if m else url.strip().rstrip("/").split("/")[-1]
            videos_url = f"https://www.twitch.tv/{channel}/videos"
        elif platform == "Kick":
            m = re.search(r"kick\.com/([a-zA-Z0-9_]+)", url)
            channel = m.group(1) if m else url.strip().rstrip("/").split("/")[-1]
            videos_url = f"https://kick.com/{channel}/videos"
        else:
            raise ValueError(f"Could not parse channel from URL: {url}")

        # Use yt-dlp to extract playlist entries (flat=True for speed)
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "playlistend": limit,
            "noplaylist": False,
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(videos_url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _extract)

        if info is None:
            return {"videos": [], "channel": channel or url, "platform": platform}

        entries = info.get("entries", [])
        videos = []
        for entry in entries:
            if entry is None:
                continue
            videos.append({
                "id": entry.get("id", ""),
                "platform": platform,
                "title": entry.get("title") or entry.get("url", "Untitled"),
                "duration": entry.get("duration"),
                "created_at": entry.get("upload_date"),
                "views": entry.get("view_count"),
                "thumbnail_url": entry.get("thumbnail") or entry.get("url"),
            })

        return {"videos": videos, "channel": channel, "platform": platform}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        err_msg = str(e)
        if "Invalid argument" in err_msg or "Errno 22" in err_msg:
            err_msg = f"Could not fetch videos for this channel. The platform may block automated browsing ({platform if 'platform' in dir() else 'Unknown'})."
        raise HTTPException(status_code=400, detail=err_msg)


# --- Video Info ---

@app.get("/api/info/video")
async def info_video(id: str):
    try:
        info = await get_video_info(id)
        return info
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/info/clip")
async def info_clip(id: str):
    try:
        info = await get_video_info(id)
        return info
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Downloads ---

@app.post("/api/download/video")
async def download_video(req: DownloadRequest):
    opts = settings_mgr.get()
    output = req.output_file or str(
        Path.home() / "Downloads" / f"{detect_platform(req.url).lower()}_video.mp4"
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality or opts.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=req.crop_start,
        crop_end=req.crop_end,
    )
    return {"download_id": download_id, "status": "started"}


@app.post("/api/download/clip")
async def download_clip(req: DownloadRequest):
    opts = settings_mgr.get()
    output = req.output_file or str(
        Path.home() / "Downloads" / "clip.mp4"
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    download_id = download_mgr.start_download(
        url=req.url,
        output_file=output,
        quality=req.quality,
        oauth=req.oauth or opts.oauth,
        crop_start=req.crop_start,
        crop_end=req.crop_end,
    )
    return {"download_id": download_id, "status": "started"}


@app.get("/api/downloads")
async def list_downloads():
    return download_mgr.get_all()


@app.get("/api/download/{download_id}")
async def get_download(download_id: str):
    state = download_mgr.get(download_id)
    if not state:
        raise HTTPException(status_code=404, detail="Download not found")
    return state


@app.post("/api/download/{download_id}/cancel")
async def cancel_download(download_id: str):
    success = download_mgr.cancel(download_id)
    return {"cancelled": success}


@app.get("/api/download/{download_id}/stream")
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


# --- System ---

@app.get("/api/info")
async def server_info():
    return {
        "version": "2.0.0",
        "name": "Kick & Twitch Downloader",
        "engine": "yt-dlp (Python)",
        "description": "Lightweight web interface for downloading VODs and clips from Kick and Twitch",
        "python_version": platform.python_version(),
    }


@app.get("/api/ytdlp/status")
async def ytdlp_status():
    try:
        import yt_dlp
        return {"available": True, "version": yt_dlp.version.__version__}
    except ImportError:
        return {"available": False, "version": None}


# ==================== MAIN ====================

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 7897))
    print(f"================================================")
    print(f"  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  Open http://localhost:{port} in your browser")
    print(f"================================================")
    uvicorn.run(app, host="0.0.0.0", port=port)
