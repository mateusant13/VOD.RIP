# KickDownloader -- Agent Notes (project memory)

This file captures the architecture decisions and project conventions
for the Kick & Twitch Downloader project.

---

## Architecture

### Stack
- **Frontend:** React 19 + TypeScript + Tailwind v4 + Vite 7
- **Backend:** FastAPI (Python) running on uvicorn
- **Kick data:** `curl_cffi` → Kick public JSON API (`/api/v2/channels/.../videos`, `/api/v1/video/{uuid}`) — **no browser** for list/info/m3u8 (~0.5–2s)
- **Download Engine:** yt-dlp native Python API (Twitch); Kick HLS via ffmpeg after API m3u8 lookup
- **Video Processing:** ffmpeg / ffprobe
- **Playwright:** kept as legacy fallback code only; hot paths do not launch Chromium

### Port & Launch
- Default port: **7897** (configurable via `PORT` env var)
- Entry point: `run.py` (auto-installs deps then launches uvicorn)
- Direct entry: `python main.py` (for development)
- Windows convenience: `launch.bat`
- Frontend dev server: `http://localhost:5173` (API proxied to backend on :7897)
- **Dev workflow:** `npm run dev:all` starts both API + Vite. `npm run dev` alone only starts Vite — `/api` calls will fail until the Python backend is running.
- **Windows + uvicorn `--reload`:** reload mode sets `WindowsSelectorEventLoopPolicy`, which breaks Playwright subprocesses. `_run_async()` in `kick_playwright_service.py` must use an explicit `ProactorEventLoop` per call (fixed).

---

## Code Conventions

### Models
All Pydantic models live in `models/schemas.py`. Key models:
- `VideoInfo` -- video metadata (title, duration, qualities, platform)
- `DownloadRequest` -- POST body for starting a download
- `DownloadState` -- status of an active/completed download
- `AppSettings` -- persisted app configuration
- `SettingsUpdate` -- partial update schema (all fields Optional)

### Services
Business logic is split into three services:

| Service | File | Responsibility |
|---|---|---|
| ytdlp_service | services/ytdlp_service.py | yt-dlp wrapper: platform detection, metadata, downloads |
| download_manager | services/download_manager.py | Thread pool, queue, progress, SSE |
| settings | services/settings.py | JSON file persistence |

---

## Testing

### debug_cli.py
Full web-UI parity via the HTTP API. All downloads are capped at **20 seconds**
(`MAX_VOD_SECONDS`); `clamp_crop()` enforces this on every download subcommand.

```bash
cd KickDownloader/KickDownloaderPy
python debug_cli.py full --spawn-server          # end-to-end (all tabs)
python debug_cli.py full --spawn-server --headed # visible Playwright windows
python run.py --debug full --spawn-server        # same via run.py

# Individual subcommands (against running server on :7897):
python debug_cli.py --base http://localhost:7897 status
python debug_cli.py --base http://localhost:7897 channel --url titiltei
python debug_cli.py --base http://localhost:7897 info --url <vod_url>
python debug_cli.py --base http://localhost:7897 download --url <vod_url> --sse
python debug_cli.py --base http://localhost:7897 settings quality=720p
python debug_cli.py --base http://localhost:7897 queue
```

Subcommands: `status`, `settings`, `info`, `clip-info`, `channel`, `download`,
`clip-download`, `queue`, `cancel`, `full`, `serve`.

### test_downloads.py
End-to-end test that downloads 20-second clips from Twitch and Kick and validates file size (< 500 MB).

Supports three modes:
- **Direct** (default): uses yt-dlp directly, no server needed
- **`--server`**: starts download via the running API server
- **`--info`**: fetches metadata only (no download)

**Default test URLs:**
- Twitch: https://www.twitch.tv/videos/2792650770
- Kick: https://kick.com/titiltei/videos/ddaf9751-fc2e-4f5e-9d5d-94fe637ef234

**Prerequisites:** ffmpeg on PATH for HLS stream merging.

---

## Key Decisions

### Why FastAPI instead of Flask?
FastAPI provides async request handling, automatic OpenAPI docs (/docs), and Pydantic
validation out of the box -- all useful for a download server that needs to handle
concurrent SSE streams and long-running tasks.

### Why yt-dlp native API instead of subprocess?
The original TwitchDownloader used subprocess calls to yt-dlp. The migration to the
native Python API (yt_dlp.YoutubeDL) allows real-time progress callbacks, better error
handling, no string-parsing of CLI output, and thread-safe concurrent downloads.

### Why 20-second clip for testing?
A 20-second download is long enough to verify real content exists but short enough to
complete quickly (2-5 seconds for most VODs). The crop is done server-side via yt-dlp's
download_sections parameter.

### Why `vite-plugin-singlefile`?
Inlines all JS and CSS into a single `dist/index.html` so the Python backend can serve
the entire frontend as a single static file with zero additional dependencies.

---

## Files of Note

| File | Purpose |
|---|---|
| src/App.tsx | React frontend UI component |
| src/index.css | Tailwind v4 styles + custom animation system |
| main.py | FastAPI application with all API routes |
| run.py | Launch script (auto-installs deps, starts uvicorn) |
| test_downloads.py | End-to-end download test suite |
| models/schemas.py | Pydantic models |
| services/ytdlp_service.py | yt-dlp wrapper |
| services/download_manager.py | Download queue management |
| services/settings.py | Settings persistence |
| static/index.html | Built React UI (via npm run build-copy) |

### Browser lifecycle (kick_playwright_service)
- Each call gets its own headless Chromium, killed on context exit and force-killed after BROWSER_MAX_LIFETIME_SEC (30 s) by a watchdog task.
- The launched binary is pinned to chrome-headless-shell.exe via executable_path so the process is identifiable by image name.
- Cleanup helper `_kill_leftover_headless_shells()` targets HEADLESS_BROWSER_IMAGE only — **never** chrome.exe (the user's real browser). The agent must never run `taskkill /F /IM chrome.exe` or any command that names chrome.exe without the headless-shell suffix.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| react | ^19 | Frontend framework |
| vite | ^7 | Build tool |
| tailwindcss | ^4 | CSS framework |
| fastapi | >=0.100.0 | Web framework |
| uvicorn[standard] | >=0.23.0 | ASGI server |
| yt-dlp | >=2024.1.0 | Video download engine |
| pydantic | >=2.0 | Data validation |
| ffmpeg | system | Video processing (PATH) |
