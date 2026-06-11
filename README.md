<div align="center">

![VOD.RIP](screenshots/readme/hero.png)

# 🎬 VOD.RIP — Kick & Twitch Downloader

**Download, preview, and clip any VOD or clip from Kick & Twitch — right in your browser.**

<p>
  <a href="#features"><img src="https://img.shields.io/badge/platforms-Kick+Twitch-53fc18?style=flat-square"/></a>
  <a href="https://github.com/mateusant13/VOD.RIP/releases"><img src="https://img.shields.io/github/v/release/mateusant13/VOD.RIP?style=flat-square&color=53fc18"/></a>
  <a href="LICENSE.txt"><img src="https://img.shields.io/badge/license-MIT-53fc18?style=flat-square"/></a>
</p>

</div>

---

## 👀 What is VOD.RIP?

VOD.RIP is the **fastest way to save your favorite streams**. Paste a Kick or Twitch URL, preview the video right in the app, trim exactly the part you want, and download at up to 1080p60 — all without installing heavy desktop software or dealing with command-line tools.

Whether you're archiving a full 6-hour stream or clipping a 10-second funny moment, VOD.RIP handles it with a beautiful, dark-themed interface.

---

## ✨ Features That Matter

### 🎥 Preview Before You Download

See exactly what you're getting before you commit. The built-in **video preview player** lets you watch any VOD or clip directly in the app — no waiting for a full download just to check the content.

![Preview Player](screenshots/readme/preview.png)

### ✂️ Smart Crop & Trim

Don't download an entire 3-hour stream when you only need 5 minutes. Drag the trim handles to select the exact segment you want, or click the in/out points for frame-perfect precision. The **Needle Glance** popup shows you exactly where you are in the timeline.

### 🔍 Channel Browser

Browse and explore any streamer's recent VODs and clips without leaving the app. Save your favorite channels, toggle between Kick and Twitch feeds, and switch between VODs and clips — all organized in a clean, searchable list.

![Channel Browser](screenshots/readme/channel-open.png)

### 📥 Download Queue

Download multiple VODs at once with **real-time progress tracking**. The queue shows active downloads, speed, estimated completion, and keeps a history of everything you've ripped. Pause, resume, or cancel downloads at any time.

![Download Queue](screenshots/readme/queue.png)

### 🚀 One-Click Rips

| Feature | Kick | Twitch |
|---|---|---|
| **Full VOD Downloads** with custom start/end cropping | ✅ | ✅ |
| **Clip Downloads** — save short highlights instantly | ✅ | ✅ |
| **Quality Selection** — 360p to 1080p60 | ✅ | ✅ |
| **In-App Video Preview** | ✅ | ✅ |
| **Channel Browsing** — explore recent VODs & clips | ✅ | ✅ |
| **GPU-Accelerated Encoding** (NVIDIA, AMD, Intel) | ✅ | ✅ |

### ⚡ Powered by yt-dlp

VOD.RIP uses **yt-dlp** under the hood — the most powerful video extraction engine available. No subprocess wrappers, no fragile shell scripts. Direct Python API integration means faster downloads and better reliability.

### 🎨 Beautiful Dark UI

A pixel-perfect dark theme designed for extended use. Smooth animations, responsive layout, keyboard shortcuts for playback, and full-screen mode for the preview player. Every detail is crafted for the best experience.

---

## 🚀 Quick Start

### Prerequisites
- **Python 3.10+**
- **Node.js 18+**
- **ffmpeg** (for video merging — download from [ffmpeg.org](https://ffmpeg.org))

### Setup

```bash
# Install Python dependencies
cd backend
pip install -r requirements.txt

# Install frontend dependencies
cd ..
npm install
```

### Run (Development)

```bash
# Start everything with one command
npm run dev
```

Or run separately:
```bash
# Terminal 1 — Backend API (port 7897)
cd backend && python run.py

# Terminal 2 — Frontend UI (port 5173)
npm run dev:vite
```

Open **http://localhost:5173** — the UI proxies API calls to the backend automatically.

---

## 🐳 One-Click Desktop App

VOD.RIP ships as a **standalone desktop application** with an embedded WebView — no browser needed. Download the latest build for your platform from the [Releases page](https://github.com/mateusant13/VOD.RIP/releases).

| Platform | Download |
|---|---|
| **Windows** | `.exe` installer or portable `.zip` |
| **macOS** | `.app` bundle |
| **Linux** | Portable `.zip` |

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | React 19, TypeScript, Tailwind CSS 4, Vite 7 |
| **Backend** | Python, FastAPI, uvicorn |
| **Download Engine** | yt-dlp (native Python API) |
| **Video Processing** | FFmpeg / FFprobe |
| **Packaging** | PyInstaller, Inno Setup (Windows) |

---

## 📸 Screenshots

| | |
|---|---|
| ![Hero](screenshots/readme/hero.png) | ![Channels](screenshots/readme/channel-open.png) |
| **Channel List** — Browse saved streamers | **VOD Browser** — Explore recent content |
| ![Preview](screenshots/readme/preview.png) | ![Queue](screenshots/readme/queue.png) |
| **Video Preview** — Watch before downloading | **Download Queue** — Real-time progress |

---

## 🤝 Contributing

This is a personal project, but feel free to open issues or submit PRs for improvements.

1. Fork the repo
2. Create a feature branch: `git checkout -b cool-new-feature`
3. Commit your changes: `git commit -am 'Add cool feature'`
4. Push to the branch: `git push origin cool-new-feature`
5. Open a Pull Request

---

## 📄 License

[MIT](LICENSE.txt) — do what you want, just don't blame us if your hard drive fills up with streamer VODs.

---

<div align="center">
  <p>
    <strong>VOD.RIP</strong> — Because your favorite streams shouldn't disappear after 60 days.
  </p>
  <p>
    <a href="https://github.com/mateusant13/VOD.RIP">GitHub</a> ·
    <a href="https://github.com/mateusant13/VOD.RIP/releases">Releases</a> ·
    <a href="https://kick.com">Kick</a> ·
    <a href="https://twitch.tv">Twitch</a>
  </p>
</div>
