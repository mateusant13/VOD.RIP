"""
VOD.RIP — FastAPI application factory.

Assembles the app, mounts static files, includes all routers, and provides
the dev ``__main__`` entry point.
"""

import logging
import os
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

app = FastAPI(title="Kick & Twitch Downloader", version="1.0.45")

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
