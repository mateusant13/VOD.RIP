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
    live,
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
            _lifespan_ready.set()
            return
        try:
            from deps import settings_mgr as _sm
            _saved = getattr(_sm.get(), "saved_channels", None) or []
            if _saved:
                logger.info(
                    "Daemon warm: sync-first then wave for %d saved channels",
                    len(_saved),
                )
                _warm_first_wave_sync(_saved)
                _startup_wave_warm(_saved)
        except Exception:
            logger.exception("Daemon preview warm crashed")
        finally:
            _lifespan_ready.set()

        # Background warm-ups (POT, session, yt-dlp)
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

        # Live-status warm — pre-populate the /api/channels/{id}/live cache so
        # the first user request after server start returns in O(1) instead of
        # paying the full 3-5s YouTube/Twitch extract on the critical path.
        # ponytail: a 4-worker pool inside the warm module bounds concurrency
        # so 20 saved channels don't slam YouTube at boot.
        try:
            from routers.live import warm_all_saved_channel_live_status
            warm_all_saved_channel_live_status()
        except Exception:
            logger.debug("Live status warm skipped", exc_info=True)

    # _warm_first_wave_sync is defined OUTSIDE _warm_youtube at lifespan
    # scope so both the daemon thread and the blocking lifespan warm can
    # call it.
    def _warm_first_wave_sync(saved_channels) -> None:
        """Sequential warm of first unique URLs (no thread pool, no double-hop)."""
        from services.preview_service import (
            _WARMED_URLS, _WARMED_URLS_LOCK,
            warm_youtube_resolve_only,
        )
        from services.youtube_innertube import extract_video_id
        import time as _tm

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

        # ponytail: warm the 4 most recent saved channels only
        sorted_channels = sorted_channels[:4]

        # ponytail: warm first-per-kind-per-channel so every tab's top row is
        # instant on click — not just the newest 2 of each channel. Matches the
        # frontend's KINDS = ['vods', 'clips', 'streams'] grouping; 'shorts'
        # live in clipVideos under YouTube-only filter (handled by clips kind).
        KINDS = ("vods", "clips", "streams")
        first_urls: list[str] = []
        seen_vids: set[str] = set()
        for ch_videos in sorted_channels:
            picked: set[str] = set()
            for kind in KINDS:
                for v in ch_videos:
                    url = v.get("url") or ""
                    if not url:
                        continue
                    ckind = v.get("content_kind") or ""
                    if kind == "vods" and ckind in ("stream", "clip"):
                        continue
                    if kind == "clips" and ckind != "clip":
                        continue
                    if kind == "streams" and ckind != "stream":
                        continue
                    # youtube.com / shorts / youtu.be already filtered above
                    vid = extract_video_id(url)
                    if vid and vid in seen_vids:
                        continue
                    if vid:
                        seen_vids.add(vid)
                    first_urls.append(url)
                    picked.add(url)
                    break

        logger.info(
            "STARTUP_SYNC_WARM: resolving %d URLs in parallel",
            len(first_urls),
        )

        from concurrent.futures import ThreadPoolExecutor

        def _warm_one(u: str) -> None:
            try:
                t0 = _tm.time()
                # warm_youtube_resolve_only does InnerTube fast pass + prog head
                # warm + session snapshot build. The snapshot is what makes the
                # click path skip the ~5s extract + variant-build + master work;
                # the prog head warm serves the first 12 MB from local disk so
                # the browser's canplay path doesn't hit googlevideo cold.
                warm_youtube_resolve_only(u, prefer_height=360)
                with _WARMED_URLS_LOCK:
                    _WARMED_URLS.add(u)
                logger.info(
                    "STARTUP_SYNC_WARM: %s done in %.1fs",
                    u[:50],
                    _tm.time() - t0,
                )
            except Exception as exc:
                logger.warning("STARTUP_SYNC_WARM: %s failed: %s", u[:50], exc)

        # ponytail: 2 workers to avoid tripping YouTube bot-gate on startup
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-sync-warm") as pool:
            list(pool.map(_warm_one, first_urls))

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
        """Wave-based warm sorted by recency, 8-per-channel per wave.

        The sync wave (first-of-kind per channel) already ran in parallel, so
        this queues the next ~40 per channel onto WARM_EXECUTOR the moment the
        server starts. ponytail: 40/channel covers the first screen + scroll
        depth; the long tail is handled by the frontend per-scroll warm.
        """
        from services.preview_service import (
            _WARMED_URLS,
            _WARMED_URLS_LOCK,
            kickoff_youtube_warm,
            kickoff_youtube_batch_warm,
        )

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

        # ponytail: warm the 4 most recent saved channels only
        sorted_channels = sorted_channels[:4]

        BATCH = 8
        MAX_WAVES = 5
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
                    # Non-blocking: kickoff self-submits to WARM_EXECUTOR (bulk
                    # warms never touch INFO/PREVIEW pools). 360 matches the
                    # frontend fast-start click so the resolve cache hits.
                    kickoff_youtube_batch_warm(u, prefer_height=360)
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

    _lifespan_ready = threading.Event()
    threading.Thread(target=_warm_youtube, daemon=True, name="yt-warm").start()

    # Mark startup ready immediately. The YouTube warm continues in the
    # daemon thread; first clicks in the first ~15s may pay the resolve
    # cost (3-5s) instead of hitting the warm cache. Strictly better
    # than blocking the server for 16s on every boot.
    _lifespan_ready.set()

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
app.include_router(live.router)
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
