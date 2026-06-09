# KickDownloader -- Agent Notes (project memory)

This file captures the architecture decisions and project conventions
for the Kick & Twitch Downloader project.

---

## Architecture

### Stack
- **Backend:** FastAPI (Python) running on uvicorn
- **Frontend:** Vanilla HTML/CSS/JS (no framework)
- **Download Engine:** yt-dlp native Python API (no subprocess wrappers)
- **Video Processing:** ffmpeg / ffprobe

### Port & Launch
- Default port: **8080** (configurable via `PORT` env var)
- Entry point: `run.py` (auto-installs deps then launches uvicorn)
- Direct entry: `python main.py` (for development)
- Windows convenience: `launch.bat`

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

### test_downloads.py
End-to-end integrity test that:
1. Downloads 20s clips from Twitch and Kick via the running API server
2. Validates file size, duration (15-25s), video/audio streams via ffprobe
3. Extracts 10 random frames and checks brightness via ffmpeg signalstats
4. Checks audio volume via ffmpeg volumedetect (peak < -50 dB = silent)
5. Reports PASS/FAIL per test with a summary table

**Default test URLs:**
- Twitch: https://www.twitch.tv/videos/2786101426
- Kick: https://kick.com/titiltei/videos/c716c40f-c1ba-491b-83d8-b41b5321a0b5

**Prerequisites:** Server must be running on localhost:8080, ffmpeg+ffprobe on PATH.

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

### Why ffmpeg signalstats for black detection?
Using signalstats and parsing the YAVG (average luminance) value is fast and reliable
without pulling in OpenCV or other heavy dependencies. A frame is black if YAVG < 5.0.

### Why ffmpeg volumedetect for audio silence?
The volumedetect filter provides mean_volume and max_volume values in dB, which is a
standard and portable way to measure audio loudness. The threshold of -50 dB for silence
was chosen empirically -- most real audio content has peaks well above this (-2 to -20 dB).
A completely silent track hovers around -90 dB.

### Why 20-second clip for testing?
A 20-second download is long enough to verify real content exists but short enough to
complete quickly (2-5 seconds for most VODs). The crop is done server-side via yt-dlp's
crop_end parameter.

### Why random frames instead of evenly-spaced?
10 random frame positions across the clip reduce the chance of hitting a black section
that happens to fall at a fixed interval (e.g., a stinger transition), while still
providing good coverage. The first 0.5s is skipped to avoid initial black frames common
in live streams.

### Twitch URL change (Jun 2026)
The default Twitch test URL was changed from 2784018843 to 2786101426 because the
original VOD became unavailable. The Kick test URL has been stable.

---

## Files of Note

| File | Purpose |
|---|---|
| main.py | FastAPI application with all API routes |
| run.py | Launch script (auto-installs deps, starts uvicorn) |
| test_downloads.py | End-to-end integrity test suite |
| models/schemas.py | Pydantic models |
| services/ytdlp_service.py | yt-dlp wrapper |
| services/download_manager.py | Download queue management |
| services/settings.py | Settings persistence |
| static/index.html | Web UI |
| static/js/app.js | Frontend logic |
| static/css/app.css | Styles |

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| fastapi | >=0.100.0 | Web framework |
| uvicorn[standard] | >=0.23.0 | ASGI server |
| yt-dlp | >=2024.1.0 | Video download engine |
| pydantic | >=2.0 | Data validation |
| ffmpeg | system | Video processing (PATH) |
