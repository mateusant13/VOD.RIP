"""
VOD.RIP — FastAPI application factory.

Assembles the app, mounts static files, includes all routers, and provides
the dev ``__main__`` entry point.
"""

from services import ytdlp_env  # noqa: F401 — import order before yt-dlp
from services.ytdlp_guard import assert_ytdlp_safe

import logging
import os
import hashlib
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from deps import settings_mgr, download_mgr
from routers import (
    archive,
    channels,
    cookie_bridge,
    disk,
    downloads,
    entities,
    info,
    live,
    preview,
    settings,
    subtitles,
    system,
    twitch_clips,
)

logger = logging.getLogger(__name__)

# At-most-once-per-boot guard for the startup chat dedupe (see lifespan).
_chat_dedupe_done = False

# Heavy boot work (live warm, yt warm, embed backfill, ASR worker boot)
# waits this long after lifespan start so the API serves in ~2-3s and the
# first Vite/UI window stays uncontended.
_BOOT_WARM_GRACE_SEC = float(os.environ.get("VODRIP_BOOT_WARM_GRACE", "8"))
_archive_worker_started = False

try:
    from services._version import __version__
except ImportError:
    __version__ = "0.0.0"


def _spawn_detached_worker() -> Optional[int]:
    """Spawn the detached supervised archive worker (worker_server.py).

    The worker must survive even a hard kill of the whole app process tree
    (taskkill /T), so on Windows it is spawned through a short-lived
    launcher: the launcher Popen()s worker_server.py and exits immediately,
    orphaning it (its parent pid goes stale, so tree-walk kills never reach
    it). POSIX uses start_new_session() (setsid) for the same effect.
    Returns a child pid (the launcher's), or None when the spawn failed
    (the caller falls back to the in-process worker).
    """
    backend_dir = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        # Packaged app: sys.executable is VOD-RIP.EXE, which cannot run
        # `-c`/worker_server.py as a child — it would relaunch the whole
        # GUI (single-instance guard kills it) and the watchdog would wait
        # out its 120s grace before the in-process fallback. Skip straight
        # to the in-process worker; the wheel-bundled worker logic runs in
        # the same process and the ASR stack ships inside the bundle.
        logger.debug("detached worker spawn skipped (frozen install)")
        return None
    if os.name == "nt":
        launcher = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable] + sys.argv[1:], cwd=%r,"
            " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
            " stderr=subprocess.DEVNULL,"
            " creationflags=subprocess.CREATE_NEW_PROCESS_GROUP"
            " | subprocess.CREATE_NO_WINDOW, close_fds=True)\n"
        ) % str(backend_dir)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", launcher, str(backend_dir / "worker_server.py")],
                cwd=str(backend_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                ),
            )
        except Exception:
            logger.debug("detached worker spawn failed", exc_info=True)
            return None
        return proc.pid
    try:
        proc = subprocess.Popen(
            [sys.executable, str(backend_dir / "worker_server.py")],
            cwd=str(backend_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        logger.debug("detached worker spawn failed", exc_info=True)
        return None
    return proc.pid


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    # Boot maintenance — disk hygiene (incl. the whisper-model prune, which
    # recursively scans multi-GB model dirs), archive retention, chat
    # dedupe, FTS optimize, and the embed-model warm all run on a daemon
    # thread so the server binds in ~1-2s instead of paying 12-25s of
    # housekeeping before first byte (measured 2026-08-07). Every step is
    # best-effort and/or age-guarded, so racing the first requests is safe;
    # the scheduler and worker already run post-ready as daemon threads.
    def _boot_maintenance() -> None:
        # Startup disk hygiene — sweep orphaned temp/preview/selfcheck files
        # (best-effort, never fatal; age-guarded so live session dirs survive).
        try:
            from services.disk_hygiene import run_startup_hygiene

            run_startup_hygiene()
        except Exception:
            logger.debug("startup disk hygiene skipped", exc_info=True)

        # Archive VOD retention — evict video files beyond the newest N per
        # platform (DB rows/transcripts/chat stay forever). Best-effort,
        # never fatal; runs so the UI never lists dead files.
        try:
            from services.archive_retention import enforce_archive_vod_retention

            _retention_stats = enforce_archive_vod_retention()
            if _retention_stats["deleted_files"]:
                logger.info(
                    "startup archive retention: removed %d video file(s), cleared %d row(s)",
                    _retention_stats["deleted_files"],
                    _retention_stats["cleared_rows"],
                )
        except Exception:
            logger.debug("startup archive retention skipped", exc_info=True)

        # One-time boot cleanup: collapse exact-duplicate chat rows left behind
        # by pre-fix multi-writer capture (watchdog live sink vs GQL backfill vs
        # replay ingest writing the same message at the same offset, plus the
        # yt_live post-rename full re-send). Idempotent (second run deletes
        # nothing) and bounded; guarded so it runs at most once per boot.
        global _chat_dedupe_done
        if not _chat_dedupe_done:
            _chat_dedupe_done = True
            try:
                from services.archive_db import dedupe_messages

                _chat_dupes = dedupe_messages()
                if _chat_dupes:
                    logger.info(
                        "startup chat dedupe: removed %d duplicate message row(s)",
                        _chat_dupes,
                    )
            except Exception:
                logger.debug("startup chat dedupe skipped", exc_info=True)

        # Official-API hybrid: lift the Twitch helix token from the cookie
        # bridge (extension users — zero manual steps). Local I/O only,
        # never validates against the API (lazy validation on first ingest).
        try:
            from services.twitch_helix_service import auto_lift_token

            if auto_lift_token():
                logger.info("twitch helix token auto-lifted from cookie bridge")
        except Exception:
            logger.debug("helix token auto-lift skipped", exc_info=True)

        # Keep FTS5 search fast as the archive grows: PRAGMA optimize merges
        # fragmented FTS index b-tree pages and refreshes stats. Cheap no-op
        # when nothing is pending; no exclusive lock (safe with live readers).
        try:
            from services.archive_db import query as _adb_query

            _adb_query("PRAGMA optimize")
        except Exception:
            logger.debug("startup fts optimize skipped", exc_info=True)

        # Warm the semantic-search embedding model (only when the archive
        # already has vectors) so the first CTX search of a fresh boot skips
        # the ~2s ONNX session + tokenizer load. Racing the embed-backfill
        # thread's load is harmless: both hit the same module-level session
        # cache and the loser's session is garbage-collected.
        try:
            from services.archive_embed import warmup_if_indexed

            warmup_if_indexed()
        except Exception:
            logger.debug("startup embed warm skipped", exc_info=True)

    threading.Thread(target=_boot_maintenance, daemon=True, name="boot-maintenance").start()

    # Warm the YouTube chat display-name cache: resolve a bounded batch of
    # UC channel ids (the @handle-only rows) to the names viewers see, so
    # the USER search filter matches displayed names from the first search.
    # Fire-and-forget; bot-walled ids stay NULL and retry on later searches.
    def _warm_display_names() -> None:
        try:
            from services.archive_ytdlp import resolve_youtube_display_names

            n = resolve_youtube_display_names(20)
            if n:
                logger.info("resolved %d youtube chat display name(s) at boot", n)
        except Exception:
            logger.debug("startup display-name warm skipped", exc_info=True)

    threading.Thread(target=_warm_display_names, daemon=True, name="yt-display-warm").start()

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
        if _warm_shutdown.is_set():
            return
        try:
            from deps import settings_mgr as _sm
            _saved = getattr(_sm.get(), "saved_channels", None) or []
            if _saved and not _warm_shutdown.is_set():
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
        if _warm_shutdown.is_set():
            return
        from services.youtube_pot_service import schedule_pot_service_warm
        from services.youtube_ytdlp_update import schedule_ytdlp_update_check
        from services.archive_transcribe import schedule_gpu_sherpa_ensure

        schedule_pot_service_warm()
        schedule_ytdlp_update_check()
        schedule_gpu_sherpa_ensure()
        from services.youtube_session import warm_youtube_session

        warm_youtube_session()
        s = settings_mgr.get()
        manual = bool(
            (getattr(s, "youtube_cookies_file", "") or "").strip()
            or (getattr(s, "youtube_cookies_browser", "") or "").strip()
        )
        from services.youtube_auth import refresh_youtube_cookie_cache

        refresh_youtube_cookie_cache(
            auto_auth=not manual,
            cookies_from_browser=getattr(s, "youtube_cookies_browser", "") or "",
        )

        # Live-status warm runs FIRST on its own daemon thread (see lifespan
        # _warm_live_guarded) — never inside the yt-warm daemon, which waits
        # for the sync-first-wave YouTube warm before it gets here.
        if _warm_shutdown.is_set():
            return

    # _warm_first_wave_sync is defined OUTSIDE _warm_youtube at lifespan
    # scope so both the daemon thread and the blocking lifespan warm can
    # call it.
    def _warm_first_wave_sync(saved_channels) -> None:
        """Sequential warm of first unique URLs (no thread pool, no double-hop)."""
        s = settings_mgr.get()
        if getattr(s, 'skip_youtube_startup_warm', False):
            logger.info("STARTUP_SYNC_WARM: skipped (skip_youtube_startup_warm setting)")
            return

        from services.preview_service import (
            _WARMED_URLS, _WARMED_URLS_LOCK,
            warm_youtube_resolve_only,
        )
        from services.youtube_innertube import extract_video_id
        import time as _tm

        sorted_channels: list[tuple[str, list[dict]]] = []
        for ch in saved_channels or []:
            if not isinstance(ch, dict):
                continue
            videos: list[dict] = []
            for key in ("vodVideos", "clipVideos"):
                for v in ch.get(key) or []:
                    if not isinstance(v, dict):
                        continue
                    url = v.get("url") or ""
                    if "youtube.com" not in url and "youtu.be" not in url:
                        continue
                    ckind = v.get("content_kind") or ""
                    if ckind == "short" or "/shorts/" in url:
                        continue  # skip shorts, only warm VODs
                    videos.append(v)
            videos.sort(
                key=lambda v: (
                    v.get("created_at") or v.get("published_at") or v.get("upload_date") or ""
                ),
                reverse=True,
            )
            if videos:
                sorted_channels.append((ch.get("id") or "", videos))

        if not sorted_channels:
            return

        # ponytail: warm the 4 most recent saved channels only
        sorted_channels = sorted_channels[:4]

        # ponytail: warm first-per-kind-per-channel so every tab's top row is
        # instant on click — not just the newest 2 of each channel. Matches the
        # frontend's KINDS = ['vods', 'clips', 'streams'] grouping; 'shorts'
        # live in clipVideos under YouTube-only filter (handled by clips kind).
        KINDS = ("vods", "clips", "streams")
        first_urls: list[tuple[str, str]] = []
        seen_vids: set[str] = set()
        for ch_key, ch_videos in sorted_channels:
            picked: set[str] = set()
            for kind in KINDS:
                for v in ch_videos:
                    url = v.get("url") or ""
                    if not url:
                        continue
                    ckind = v.get("content_kind") or ""
                    if kind == "vods" and ckind in ("stream", "clip", "short"):
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
                    first_urls.append((url, ch_key))
                    picked.add(url)
                    break

        logger.info(
            "STARTUP_SYNC_WARM: resolving %d URLs in parallel",
            len(first_urls),
        )

        # Pre-warm the anonymous YouTube session BEFORE the extract pool: the
        # two workers would otherwise race each other on the single-flight
        # bootstrap and both stall waiting for it. This call is the leader, so
        # the extracts join its event and find the cache warm. Bounded: ~3x6s
        # worst case as leader, <=14s as follower — never blocks API readiness
        # (we run on the yt-warm daemon thread, after _lifespan_ready).
        from services.youtube_session import bootstrap_anonymous_session

        try:
            bootstrap_anonymous_session()
        except Exception:
            logger.debug("STARTUP_SYNC_WARM: anonymous session bootstrap skipped", exc_info=True)

        from concurrent.futures import ThreadPoolExecutor

        # Cap wall-clock per URL at ~15s: on a degraded network the light
        # extract alone can take 60s+, which pushed the startup wave to ~70s.
        # The abandoned thread keeps running in the background and still
        # lands its caches if it finishes.
        _SYNC_WARM_URL_CAP_SEC = 15.0

        def _warm_one(item: tuple[str, str]) -> None:
            u, ch_key = item
            done = threading.Event()

            def _run() -> None:
                try:
                    t0 = _tm.time()
                    # warm_youtube_resolve_only does InnerTube fast pass + prog head
                    # warm + session snapshot build. The snapshot is what makes the
                    # click path skip the ~5s extract + variant-build + master work;
                    # the prog head warm serves the first 2 MiB from local disk so
                    # the browser's canplay path doesn't hit googlevideo cold.
                    warm_youtube_resolve_only(u, prefer_height=360, channel_key=ch_key)
                    with _WARMED_URLS_LOCK:
                        _WARMED_URLS.add(u)
                    logger.info(
                        "STARTUP_SYNC_WARM: %s done in %.1fs",
                        u[:50],
                        _tm.time() - t0,
                    )
                except Exception as exc:
                    logger.warning("STARTUP_SYNC_WARM: %s failed: %s", u[:50], exc)
                finally:
                    done.set()

            threading.Thread(target=_run, daemon=True, name="yt-sync-warm-url").start()
            if not done.wait(_SYNC_WARM_URL_CAP_SEC):
                logger.warning(
                    "STARTUP_SYNC_WARM: %s still running after %.0fs — moving on",
                    u[:50],
                    _SYNC_WARM_URL_CAP_SEC,
                )

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
                        ckind = v.get("content_kind") or ""
                        if ckind == "short" or "/shorts/" in url:
                            continue
                        seen.add(url)
                        urls.append(url)
        return urls

    def _startup_wave_warm(saved_channels) -> None:
        """Wave-based warm sorted by recency, 5-per-channel per wave.

        The sync wave (first-of-kind per channel) already ran in parallel, so
        this queues the next 5 per channel onto WARM_EXECUTOR the moment the
        server starts. ponytail: 5/channel covers the first screen; the long
        tail is handled by the frontend per-scroll warm.
        """
        s = settings_mgr.get()
        if getattr(s, 'skip_youtube_startup_warm', False):
            logger.info("STARTUP_WAVE: skipped (skip_youtube_startup_warm setting)")
            return

        from services.preview_service import (
            _WARMED_URLS,
            _WARMED_URLS_LOCK,
            kickoff_youtube_warm,
            kickoff_youtube_batch_warm,
        )

        # Collect per-channel YouTube video lists, sorted newest-first by date.
        sorted_channels: list[tuple[str, list[dict]]] = []
        for ch in saved_channels or []:
            if not isinstance(ch, dict):
                continue
            videos: list[dict] = []
            for key in ("vodVideos", "clipVideos"):
                for v in ch.get(key) or []:
                    if not isinstance(v, dict):
                        continue
                    url = v.get("url") or ""
                    if "youtube.com" not in url and "youtu.be" not in url:
                        continue
                    ckind = v.get("content_kind") or ""
                    if ckind == "short" or "/shorts/" in url:
                        continue  # skip shorts, only warm VODs
                    videos.append(v)
            # Sort newest-first by date
            videos.sort(
                key=lambda v: (
                    v.get("created_at") or v.get("published_at") or v.get("upload_date") or ""
                ),
                reverse=True,
            )
            if videos:
                ch_id = ch.get("id") or ""
                sorted_channels.append((ch_id, videos))

        if not sorted_channels:
            return

        # ponytail: warm the 4 most recent saved channels only
        sorted_channels = sorted_channels[:4]

        # ponytail: 5/channel matches YOUTUBE_WARM_VOD_LIMIT; deeper scroll
        # triggers the frontend per-scroll batch warm.
        BATCH = 5
        MAX_WAVES = 1
        submitted = 0
        wave_count = 0

        for wave_idx in range(MAX_WAVES):
            wave_urls: list[tuple[str, str]] = []
            for ch_id, ch_videos in sorted_channels:
                start = wave_idx * BATCH
                for v in ch_videos[start : start + BATCH]:
                    wave_urls.append((v["url"], ch_id))
            if not wave_urls:
                break

            with _WARMED_URLS_LOCK:
                fresh = [(u, ck) for u, ck in wave_urls if u not in _WARMED_URLS]
                for u, _ in fresh:
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

            for u, ch_id in fresh:
                try:
                    # Non-blocking: kickoff self-submits to WARM_EXECUTOR (bulk
                    # warms never touch INFO/PREVIEW pools). 360 matches the
                    # frontend fast-start click so the resolve cache hits.
                    kickoff_youtube_batch_warm(u, prefer_height=360, channel_key=ch_id)
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
                    prefer_height=360,
                )
            except Exception:
                pass

    _lifespan_ready = threading.Event()
    _warm_shutdown = threading.Event()

    def _warm_live_guarded() -> None:
        """Live-status warm — pre-populates the /api/channels/{id}/live cache
        so the first Channels-tab poll after boot hits O(1) instead of paying
        the 3-5s Kick/Twitch/YouTube extract on the request path.

        Fires FIRST on its own daemon thread — before/parallel to the yt-warm
        sync wave — and never blocks API readiness: the router serves an
        empty + schedules a background refresh for any channel this warm
        hasn't reached yet.
        """
        try:
            if _warm_shutdown.wait(_BOOT_WARM_GRACE_SEC):
                return  # cold live cache is handled by the router's cold-miss path
            from routers.live import warm_all_saved_channel_live_status

            warm_all_saved_channel_live_status()
        except Exception:
            logger.debug("Live status warm skipped", exc_info=True)

    threading.Thread(target=_warm_live_guarded, daemon=True, name="live-warm").start()

    def _warm_youtube_guarded() -> None:
        """Wrap _warm_youtube so it checks shutdown before submitting to pools."""
        try:
            if _warm_shutdown.wait(_BOOT_WARM_GRACE_SEC):
                return  # cold caches are handled by each router's cold-miss path
            _warm_youtube()
        except Exception:
            logger.exception("warm crashed")

    threading.Thread(target=_warm_youtube_guarded, daemon=True, name="yt-warm").start()

    # Semantic-embedding backfill — embed transcript segments that lack a
    # vector, in the background, so the first SEMANTIC search doesn't pay
    # the inline backfill cost (up to 50k segments through torch on the
    # request path). Only meaningful archives trigger it (>=500 missing);
    # after a complete pass every boot finds 0 missing and exits at once.
    # VODRIP_EMBED_BACKFILL=0 opts out (tests/constrained boxes).
    try:
        if os.environ.get("VODRIP_EMBED_BACKFILL", "1").strip() not in ("0", "false", "no"):

            def _embed_backfill_guarded() -> None:
                try:
                    if _warm_shutdown.wait(_BOOT_WARM_GRACE_SEC):
                        return
                    from services.archive_embed import backfill_missing

                    done = backfill_missing(interrupt=_warm_shutdown, min_missing=500)
                    if done:
                        logger.info("semantic embed backfill: %d segments embedded", done)
                except Exception:
                    logger.debug("semantic embed backfill skipped", exc_info=True)

            threading.Thread(target=_embed_backfill_guarded, daemon=True, name="embed-backfill").start()
    except Exception:
        logger.debug("semantic embed backfill spawn skipped", exc_info=True)

    # Mark startup ready immediately. The YouTube warm continues in the
    # daemon thread; first clicks in the first ~15s may pay the resolve
    # cost (3-5s) instead of hitting the warm cache. Strictly better
    # than blocking the server for 16s on every boot.
    _lifespan_ready.set()

    # Archive chat watchdog — captures live chat into the local archive
    # while saved channels are live (polls the same live-status source as
    # the live router, starts/stops the platform chat sinks, writes rows
    # through archive_db).
    try:
        from services.archive_watchdog import start_archive_watchdog

        start_archive_watchdog()
        logger.info("Archive chat watchdog started")
    except Exception:
        logger.debug("Archive chat watchdog start skipped", exc_info=True)

    # Archive scheduler — proactively ingests VOD metadata for every saved
    # channel (Twitch/Kick), backfills Twitch VOD chat, fetches YouTube
    # captions/subtitles, and tops up the low-priority whisper queue. First
    # pass runs immediately at boot; later passes every ~3 min and right
    # after a channel is added (kick_scheduler_pass in routers/settings).
    try:
        from services.archive_scheduler import start_archive_scheduler

        start_archive_scheduler(delay=_BOOT_WARM_GRACE_SEC)
        logger.info("Archive scheduler started")
    except Exception:
        logger.debug("Archive scheduler start skipped", exc_info=True)

    # Archive transcribe worker. When the queue has pending work we spawn
    # the DETACHED supervised worker (worker_server.py): it drains
    # transcribe/events/chat jobs, survives app close + crashes, and skips
    # the in-process thread so the whisper model is never double-loaded.
    # Spawn failure (or an idle queue) falls back to the in-process worker.
    # Boots on a daemon thread after the warm grace: the ASR stack import
    # happens only in the fallback branch, so the parent process never pays
    # the ~38s torch/ASR import when the detached path succeeds.
    def _boot_archive_worker() -> None:
        if _warm_shutdown.wait(_BOOT_WARM_GRACE_SEC):
            return
        global _archive_worker_started
        try:
            from services import archive_db

            pending = archive_db.has_pending_jobs()
            spawned_pid = _spawn_detached_worker() if pending else None
            if spawned_pid is not None:
                logger.info(
                    "Archive worker: detached supervisor spawned (pid %s) — "
                    "in-process worker skipped", spawned_pid,
                )

                def _watch_detached_worker() -> None:
                    # The detached worker exits rc 0 once the queue is drained.
                    # When its heartbeat goes stale while the app still runs,
                    # the in-process worker takes over so jobs enqueued at
                    # runtime (search kicks, channel sync) keep a consumer.
                    #
                    # Boot race: the watchdog's first poll can run before the
                    # detached supervisor+child stamp their first heartbeat.
                    # worker_server boots ~1s, child import ~2-6s, and the
                    # child's claim-time GPU-lane measurement samples free VRAM
                    # over ~60s (median, machine-aware pool) BEFORE the first
                    # heartbeat — so the grace is 120s, never 75. Require either
                    # a previously-seen heartbeat OR the grace before concluding
                    # the detached worker is gone — a single early poll must
                    # never double-start a worker.
                    start = time.monotonic()
                    seen_alive = False
                    while not _warm_shutdown.is_set():
                        if archive_db.worker_live(age_s=45):
                            seen_alive = True
                        elif seen_alive or time.monotonic() - start > 120:
                            break
                        time.sleep(5)
                    if _warm_shutdown.is_set():
                        return
                    try:
                        from services.archive_transcribe import start_worker as _start_inprocess_worker

                        _start_inprocess_worker()
                        logger.info(
                            "Detached worker exited — in-process archive worker started"
                        )
                    except Exception:
                        logger.debug("in-process worker start failed", exc_info=True)

                threading.Thread(
                    target=_watch_detached_worker, daemon=True, name="worker-watchdog"
                ).start()
                _archive_worker_started = True
            else:
                from services.archive_transcribe import start_worker as _start_inprocess_worker

                _start_inprocess_worker()
                logger.info(
                    "Archive transcribe worker started in-process (%s)",
                    "no pending jobs — nothing to detach" if not pending else "detached spawn failed — fallback",
                )
                _archive_worker_started = True
        except Exception:
            logger.debug("archive worker boot skipped", exc_info=True)

    threading.Thread(target=_boot_archive_worker, daemon=True, name="archive-worker-boot").start()

    # Entity watcher — scans new transcription segments for saved words /
    # saved channels (auto mode), once at startup then every minute.
    try:
        from services.entity_watch import start_entity_watcher

        start_entity_watcher()
        logger.info("Entity watcher started")
    except Exception:
        logger.debug("Entity watcher start skipped", exc_info=True)

    yield
    _warm_shutdown.set()
    try:
        from services.entity_watch import stop_entity_watcher

        stop_entity_watcher(timeout=5.0)
    except Exception:
        logger.debug("Entity watcher stop failed", exc_info=True)
    try:
        from services.archive_watchdog import stop_archive_watchdog

        stop_archive_watchdog(timeout=6.0)
    except Exception:
        logger.debug("Archive chat watchdog stop failed", exc_info=True)
    try:
        if _archive_worker_started:
            from services.archive_transcribe import stop_worker

            stop_worker(timeout=6.0)
    except Exception:
        logger.debug("Archive transcribe worker stop failed", exc_info=True)
    try:
        from services.shutdown_util import shutdown_downloads_and_children

        logger.info("API shutdown — cancelling downloads and killing ffmpeg children")
        shutdown_downloads_and_children()
    except Exception:
        logger.exception("shutdown during API lifespan")


app = FastAPI(title="Kick & Twitch Downloader", version=__version__, lifespan=_app_lifespan)

assert_ytdlp_safe()

# Two-lane activity signal (user requirement): the app's interactive lane
# stamps an 'app-activity' heartbeat (throttled, fire-and-forget — never
# adds latency to a request) that the detached archive worker reads to back
# off its paced background YouTube work while the user is actively using the
# app. When the app is closed/idle the worker ramps back to heavy volume.
_ACTIVITY_STAMP_EVERY_S = 20.0
_activity_last_stamp = 0.0
_activity_stamp_lock = threading.Lock()


def _activity_stamp_due() -> bool:
    """Claim the next stamp slot (throttled); no I/O — safe on the event loop."""
    global _activity_last_stamp
    now = time.monotonic()
    with _activity_stamp_lock:
        if now - _activity_last_stamp < _ACTIVITY_STAMP_EVERY_S:
            return False
        _activity_last_stamp = now
        return True


def _stamp_app_activity() -> None:
    try:
        from services import archive_db

        archive_db.worker_heartbeat("app-activity")
    except Exception:
        logger.debug("app-activity stamp failed", exc_info=True)


@app.middleware("http")
async def _app_activity_middleware(request: Request, call_next):
    # Off the event loop: the SQLite write happens on a worker thread so a
    # transient DB lock can never stall the interactive lane.
    if _activity_stamp_due():
        threading.Thread(target=_stamp_app_activity, daemon=True).start()
    return await call_next(request)

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
app.include_router(archive.router)
app.include_router(entities.router)
app.include_router(cookie_bridge.router)
app.include_router(disk.router)
app.include_router(subtitles.router)
app.include_router(twitch_clips.router)


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
async def index(request: Request):
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
        # no-cache + ETag: browser keeps the 1MB single-file bundle, revalidates
        # with a cheap 304 instead of re-downloading it on every cold open.
        etag = '"%s"' % hashlib.sha1(content.encode("utf-8")).hexdigest()
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"Cache-Control": "no-cache", "ETag": etag})
        return HTMLResponse(content, headers={"Cache-Control": "no-cache", "ETag": etag})
    return HTMLResponse(
        "<h1>Kick & Twitch Downloader</h1>"
        "<p>Frontend not found. Run <code>npm run build-copy</code> then set <code>KICK_SERVE_UI=1</code>, "
        f"or open <a href=\"{ui_url}\">{ui_url}</a>.</p>"
    )


if __name__ == "__main__":
    import sys
    import uvicorn
    from services.server_lifecycle import guard_api_port

    port = int(os.environ.get("PORT", 7897))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    # Same first-wins guard as run.py — `python app.py` used to bypass it and
    # double-bind (Windows SO_REUSEADDR steal) when another launcher won the port.
    if guard_api_port(port):
        sys.exit(0)
    print("================================================")
    print("  Kick & Twitch Downloader v2.0 (Python)")
    print(f"  Open http://localhost:{port} in your browser")
    print("================================================")
    uvicorn.run(app, host="0.0.0.0", port=port)
