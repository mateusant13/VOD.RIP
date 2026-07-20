"""
VOD.RIP — FastAPI application factory.

Assembles the app, mounts static files, includes all routers, and provides
the dev ``__main__`` entry point.
"""

from services import ytdlp_env  # noqa: F401 — import order before yt-dlp
from services.ytdlp_guard import assert_ytdlp_safe

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from deps import settings_mgr, download_mgr
from routers import (
    channels,
    downloads,
    info,
    preview,
    settings,
    system,
)

logger = logging.getLogger(__name__)

try:
    from services._version import __version__
except ImportError:
    __version__ = "0.0.0"


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    # Clamp dangerous settings from older builds (WPC spawns headless Chrome).
    try:
        s = settings_mgr.get()
        if getattr(s, "youtube_wpc_pot", False):
            s.youtube_wpc_pot = False
            settings_mgr.save(s)
            logger.warning("youtube_wpc_pot forced off at startup (headless Chrome disabled)")
    except Exception:
        logger.debug("settings wpc clamp skipped", exc_info=True)

    def _warm_youtube() -> None:
        from services.ytdlp_hls import preview_fast_only_mode

        if preview_fast_only_mode():
            logger.info("YouTube warm-up skipped (VODRIP_PREVIEW_FAST_ONLY)")
            return
        try:
            from services.youtube_pot_service import schedule_pot_service_warm
            from services.youtube_ytdlp_update import schedule_ytdlp_update_check

            schedule_pot_service_warm()
            schedule_ytdlp_update_check()
            from services.youtube_session import warm_youtube_session

            warm_youtube_session()
            s = settings_mgr.get()
            manual = bool(
                (getattr(s, "youtube_cookies_file", "") or "").strip()
                or (getattr(s, "youtube_cookies_browser", "") or "").strip()
            )
            if manual:
                from services.youtube_auth import refresh_youtube_cookie_cache

                refresh_youtube_cookie_cache(
                    auto_auth=False,
                    cookies_from_browser=getattr(s, "youtube_cookies_browser", "") or "",
                )

            # Start-up preview warm from saved_channels — runs BEFORE the user
            # opens the page so the first preview click is instant.
            # Wave 0 (newest from each channel) gets full warm (resolve +
            # preflight mux). Subsequent waves are resolve-only and run in
            # background.
            try:
                saved = getattr(s, "saved_channels", None) or []
                if saved:
                    logger.info(
                        "Startup preview warm: scheduling for %d saved channels",
                        len(saved),
                    )
                    _startup_wave_warm(saved)
            except Exception:
                logger.exception("Startup preview warm crashed")  # was debug — crashes invisible!
        except Exception:
            logger.debug("YouTube warm-up skipped", exc_info=True)

    def _collect_saved_youtube_urls(saved_channels) -> list:
        """Pull YouTube URLs out of the saved channel list (any field that
        looks like a YouTube link is a candidate)."""
        import re

        urls = []
        seen = set()
        yt_re = re.compile(r"youtube\.com|youtu\.be")
        for ch in saved_channels or []:
            if not isinstance(ch, dict):
                continue
            for key in ("vodVideos", "clipVideos", "videos"):
                for v in ch.get(key) or []:
                    if not isinstance(v, dict):
                        continue
                    url = v.get("url") or ""
                    if url and yt_re.search(url) and url not in seen:
                        seen.add(url)
                        urls.append(url)
        return urls

    def _startup_wave_warm(saved_channels) -> None:
        """Wave-based warm sorted by recency, 2-per-channel per wave.

        Wave 0 (newest from each channel) fires immediately via full warm
        (resolve + preflight mux) so the first click is instant.
        Subsequent waves are resolve-only (lighter, faster) and run in
        background. The INFO_EXECUTOR (24 workers) handles the fan-out.
        """
        from services.preview_service import (
            _WARMED_URLS,
            _WARMED_URLS_LOCK,
            kickoff_youtube_warm,
            kickoff_youtube_batch_warm,
        )
        from deps import INFO_EXECUTOR

        # Collect per-channel YouTube video lists, sorted newest-first by date.
        sorted_channels: list[list[dict]] = []
        for ch in saved_channels or []:
            if not isinstance(ch, dict):
                continue
            videos: list[dict] = []
            for key in ("vodVideos", "clipVideos"):
                for v in ch.get(key) or []:
                    if not isinstance(v, dict):
                        continue
                    url = v.get("url") or ""
                    if "youtube.com" in url or "youtu.be" in url:
                        videos.append(v)
            # Sort newest-first by date
            videos.sort(
                key=lambda v: (
                    v.get("created_at") or v.get("published_at") or v.get("upload_date") or ""
                ),
                reverse=True,
            )
            if videos:
                sorted_channels.append(videos)

        if not sorted_channels:
            return

        BATCH = 2
        MAX_WAVES = 100
        submitted = 0
        wave_count = 0

        for wave_idx in range(MAX_WAVES):
            wave_urls: list[str] = []
            for ch_videos in sorted_channels:
                start = wave_idx * BATCH
                wave_urls.extend(v["url"] for v in ch_videos[start : start + BATCH])
            if not wave_urls:
                break

            with _WARMED_URLS_LOCK:
                fresh = [u for u in wave_urls if u not in _WARMED_URLS]
                for u in fresh:
                    _WARMED_URLS.add(u)

            if not fresh:
                continue

            wave_count += 1

            if wave_count <= 3 or wave_count % 15 == 0:
                logger.info(
                    "STARTUP_WAVE: wave %d firing %d URLs",
                    wave_count,
                    len(fresh),
                )

            for u in fresh:
                try:
                    INFO_EXECUTOR.submit(
                        kickoff_youtube_batch_warm,
                        u,
                        prefer_height=720,
                    )
                    submitted += 1
                except Exception as exc:
                    logger.warning("STARTUP_WAVE: submit failed for %s: %s", u[:60], exc)

        logger.info(
            "STARTUP_WAVE: done — %d URLs queued in %d waves",
            submitted,
            wave_count,
        )

    def _startup_batch_warm(urls: list) -> None:
        """Legacy helper retained for backward compat — unused by new wave path."""
        from services.preview_service import kickoff_youtube_batch_warm
        from deps import INFO_EXECUTOR, CHANNEL_EXECUTOR

        for u in urls:
            try:
                CHANNEL_EXECUTOR.submit(
                    kickoff_youtube_batch_warm,
                    u,
                    prefer_height=720,
                )
            except Exception:
                pass

    threading.Thread(target=_warm_youtube, daemon=True, name="yt-warm").start()
    yield
    try:
        from services.shutdown_util import shutdown_downloads_and_children

        logger.info("API shutdown — cancelling downloads and killing ffmpeg children")
        shutdown_downloads_and_children()
    except Exception:
        logger.exception("shutdown during API lifespan")


app = FastAPI(title="Kick & Twitch Downloader", version=__version__, lifespan=_app_lifespan)

assert_ytdlp_safe()

# Mount static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include routers
app.include_router(settings.router)
app.include_router(preview.router)
app.include_router(channels.router)
app.include_router(info.router)
app.include_router(downloads.router)
app.include_router(system.router)


def _warm_youtube_session() -> None:
    from services.ytdlp_hls import preview_fast_only_mode

    if preview_fast_only_mode():
        logger.info("YouTube session pre-warm skipped (VODRIP_PREVIEW_FAST_ONLY)")
        return
    try:
        from services.youtube_session import warm_youtube_session

        warm_youtube_session()
        logger.info("YouTube anonymous session pre-warmed")
    except Exception:
        logger.debug("YouTube session pre-warm failed", exc_info=True)


threading.Thread(
    target=_warm_youtube_session,
    daemon=True,
    name="youtube-warm",
).start()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve bundled UI when KICK_SERVE_UI=1; otherwise redirect to Vite (dev)."""
    serve_ui = os.environ.get("KICK_SERVE_UI", "").strip() == "1"
    ui_url = os.environ.get("KICK_UI_URL", "http://localhost:5173").strip()
    if not serve_ui:
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={ui_url}">
<title>VOD.RIP 🪦</title></head>
<body style="font-family:system-ui;background:#09090b;color:#fafafa;padding:2rem">
<p>Redirecting to the UI at <a href="{ui_url}" style="color:#53fc18">{ui_url}</a>…</p>
<p style="color:#a1a1aa;font-size:0.875rem">API is on this port ({os.environ.get("PORT", "7897")}).
Run <code>npm run dev</code> for API + UI, or set <code>KICK_SERVE_UI=1</code> after <code>npm run build-copy</code>.</p>
</body></html>""",
            headers={"Cache-Control": "no-store"},
        )
    index_file = static_dir / "index.html"
    if index_file.exists():
        content = index_file.read_text(encoding="utf-8")
        return HTMLResponse(content, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    return HTMLResponse(
        "<h1>Kick & Twitch Downloader</h1>"
        "<p>Frontend not found. Run <code>npm run build-copy</code> then set <code>KICK_SERVE_UI=1</code>, "
        f"or open <a href=\"{ui_url}\">{ui_url}</a>.</p>"
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7897))
    print("================================================")
    print("  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  Open http://localhost:{port} in your browser")
    print("================================================")
    uvicorn.run(app, host="0.0.0.0", port=port)
