# VOD.RIP — Kick & Twitch Downloader

A lightweight web application for ripping VODs and clips from **[Kick.com](https://kick.com)** and **[Twitch.tv](https://twitch.tv)**. Built with a **React + Vite** frontend and a **Python FastAPI + yt-dlp** backend.

---

## Features

| Feature | Kick | Twitch |
|---|---|---|
| VOD Download with start/end cropping and quality selection | ✓ | ✓ |
| Clip Download | ✓ | ✓ |
| Download Queue with real-time SSE progress | ✓ | ✓ |
| Channel Browsing | ✓ | ✓ |
| Web UI (React + Tailwind) | ✓ | ✓ |
| OAuth Support | ✓ | ✓ |
| Native yt-dlp (no subprocess wrappers) | ✓ | ✓ |

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **Node.js 18+**
- **ffmpeg** (must be on PATH for video merging)

### Setup

```bash
# 1. Install Python dependencies
cd KickDownloader/KickDownloaderPy
pip install -r requirements.txt

# 2. Install frontend dependencies (from project root)
cd ../..
npm install
```

### Running (Development)

Run **both** the backend and frontend simultaneously:

**Terminal 1 — Python Backend:**
```bash
cd KickDownloader/KickDownloaderPy
python run.py
```
Server starts at `http://localhost:7897`

**Terminal 2 — React Frontend (dev server with API proxy):**
```bash
npm run dev
```
UI opens at `http://localhost:5173` — API calls are proxied to the backend.

### Running (Production)

```bash
# Build the React frontend (inlines into a single HTML file)
npm run build

# This copies the built frontend to the Python backend's static folder
# Then just run the Python server:
cd KickDownloader/KickDownloaderPy
python run.py
```

Open `http://localhost:7897` — the React UI is served directly by the Python backend.

---

## Project Structure

```
.
├── src/                          # React frontend (Vite + Tailwind)
│   ├── App.tsx                   # Main UI component
│   ├── index.css                 # Styles
│   ├── main.tsx                  # Entry point
├── index.html                    # React entry HTML
├── package.json                  # Frontend dependencies
├── vite.config.ts                # Vite config with API proxy
├── tsconfig.json                 # TypeScript config
│
├── KickDownloader/
│   ├── KickDownloaderPy/         # Python backend (FastAPI)
│   │   ├── main.py               # FastAPI server + API routes
│   │   ├── run.py                # Launch script (auto-installs deps)
│   │   ├── requirements.txt      # Python dependencies
│   │   ├── models/
│   │   │   └── schemas.py        # Pydantic data models
│   │   ├── services/
│   │   │   ├── ytdlp_service.py  # yt-dlp wrapper
│   │   │   ├── download_manager.py # Download queue + SSE
│   │   │   └── settings.py       # JSON settings persistence
│   │   └── static/
│   │       └── index.html        # Built React UI (via npm run build-copy)
│   ├── launch.bat                # Windows launcher
│   ├── AGENTS.md                 # AI agent project memory
│   ├── LICENSE.txt
│   └── THIRD-PARTY-LICENSES.txt
└── .gitignore
```

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/info` | GET | Server info and Python version |
| `/api/info/video?id=` | GET | Get VOD metadata |
| `/api/info/clip?id=` | GET | Get clip metadata |
| `/api/channel/videos?url=&platforms=Kick,Twitch&limit=` | GET | List channel videos (filterable by platform) |
| `/api/download/video` | POST | Start VOD download |
| `/api/download/clip` | POST | Start clip download |
| `/api/downloads` | GET | List active downloads |
| `/api/download/{id}` | GET | Get download state |
| `/api/download/{id}/cancel` | POST | Cancel a download |
| `/api/download/{id}/stream` | GET | SSE progress stream |
| `/api/settings` | GET/POST | App settings |
| `/api/ytdlp/status` | GET | yt-dlp version check |

---

## Testing

```bash
# Start the server
cd KickDownloader/KickDownloaderPy
python run.py

# In another terminal, run the end-to-end tests
python test_downloads.py
```

The test suite downloads 20-second clips from both Twitch and Kick, then validates:
- File size and duration via ffprobe
- Frame brightness (no black frames) via ffmpeg signalstats
- Audio volume (no silence) via ffmpeg volumedetect

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 19, TypeScript, Tailwind 4, Vite 7 |
| Backend | Python, FastAPI, uvicorn |
| Download Engine | yt-dlp (native Python API) |
| Video Processing | ffmpeg / ffprobe |

---

## License

[MIT](KickDownloader/LICENSE.txt)
