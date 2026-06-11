# VOD.RIP

Save Twitch and Kick VODs, clips, and highlights in a few clicks.

<p>
  <a href="https://github.com/mateusant13/VOD.RIP/releases"><img src="https://img.shields.io/badge/download-Windows%20%E2%80%A2%20macOS%20%E2%80%A2%20Linux-53fc18?style=flat-square"/></a>
  <a href="https://github.com/mateusant13/VOD.RIP/releases"><img src="https://img.shields.io/github/v/release/mateusant13/VOD.RIP?style=flat-square&color=53fc18"/></a>
  <a href="LICENSE.txt"><img src="https://img.shields.io/badge/license-MIT-53fc18?style=flat-square"/></a>
</p>

[Download the latest release](https://github.com/mateusant13/VOD.RIP/releases) — no setup required.

- **Download Twitch and Kick VODs** — full streams or just the parts you want
- **Download clips** — save short highlights instantly
- **Preview before downloading** — watch any VOD or clip inside the app
- **Trim only what you need** — don't download an entire stream for a 5-minute segment
- **Queue multiple downloads** — start several downloads at once and track progress
- **Save favorite channels** — browse recent VODs and clips without leaving the app

![Hero screenshot showing VOD info and download controls](screenshots/readme/hero.png)

## Preview Before Downloading

See what you're getting before you commit. Paste a Kick or Twitch URL, extract the video info, and preview the content directly in the app. No need to wait for a full download just to check what's in it.

![Preview player and video information](screenshots/readme/preview.png)

## Download Only What You Need

Pick a start and end point — download just the segment you care about. Drag the trim handles or click the in/out markers for precise control. A 3-hour stream becomes a 5-minute highlight in one step.

![Trim controls with start and end markers](screenshots/readme/trim.png)

## Explore Channels

Browse any streamer's recent VODs and clips side by side. Switch between Kick and Twitch feeds, toggle between VODs and clips, and save channels for quick access later.

![Channel browser with saved channels and VOD listings](screenshots/readme/channel-open.png)

## Manage Multiple Downloads

The queue shows progress, speed, and estimated completion for each download. Start, pause, resume, or cancel at any time. Finished downloads stay in the history so you can find them again.

![Download queue with active and completed items](screenshots/readme/queue.png)

## Download

VOD.RIP runs as a standalone desktop app — no browser or command-line knowledge needed.

| Platform | Download |
|---|---|
| **Windows** | `.exe` installer or portable `.zip` |
| **macOS** | `.app` bundle |
| **Linux** | Portable `.zip` |

Grab the latest build from the [Releases page](https://github.com/mateusant13/VOD.RIP/releases).

## Run from Source

```bash
npm install        # install frontend dependencies
cd backend
pip install -r requirements.txt   # install Python dependencies
cd ..
npm run dev        # start both frontend and backend
```

Then open `http://localhost:5173`.

## For Developers

VOD.RIP is built with:

- **Frontend:** React, TypeScript, Vite
- **Backend:** Python, FastAPI
- **Download engine:** yt-dlp
- **Desktop window:** PyWebView
- **Video processing:** FFmpeg

The backend runs a FastAPI server that wraps yt-dlp's Python API. The frontend is a single-page React app that communicates with the backend over HTTP. The desktop version bundles everything into a single executable using PyInstaller.

## License

[MIT](LICENSE.txt)
