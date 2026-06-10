#!/usr/bin/env python3
"""Debug CLI — full parity with the web UI via the same HTTP API.

Every download is capped at MAX_VOD_SECONDS (20s) of content.

Examples:
    python debug_cli.py full --spawn-server
    python debug_cli.py full --spawn-server --headed
    python debug_cli.py channel --url titiltei
    python debug_cli.py info --url https://kick.com/titiltei/videos/...
    python debug_cli.py download --url <vod_url> --crop-end 20
    python debug_cli.py status --base http://localhost:7897
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Sibling imports (main, services) resolve from this directory.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

MAX_VOD_SECONDS = 20
DEFAULT_SPAWN_PORT = 17997
DEFAULT_BASE = f"http://127.0.0.1:{DEFAULT_SPAWN_PORT}"

# Line-buffer stdout so background / piped runs show progress immediately.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass


def _out(msg: str = "") -> None:
    print(msg, flush=True)

KICK_TEST_VOD = (
    "https://kick.com/titiltei/videos/ddaf9751-fc2e-4f5e-9d5d-94fe637ef234"
)
TWITCH_TEST_VOD = "https://www.twitch.tv/videos/2792650770"
TEST_CHANNEL = "titiltei"


# ---------------------------------------------------------------------------
# Playwright headed mode (optional)
# ---------------------------------------------------------------------------


def patch_headed_launch() -> None:
    from playwright.async_api import BrowserType

    orig = BrowserType.launch

    async def headed(self, *args, **kwargs):
        kwargs["headless"] = False
        if "args" in kwargs:
            kwargs["args"] = [
                a for a in kwargs["args"] if not a.startswith("--headless")
            ]
        _out("[debug] HEADED Chromium launch")
        return await orig(self, *args, **kwargs)

    BrowserType.launch = headed  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Embedded server
# ---------------------------------------------------------------------------


class EmbeddedServer:
    def __init__(self, port: int) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        import uvicorn

        def _run() -> None:
            uvicorn.run("main:app", host="127.0.0.1", port=self.port, log_level="warning")

        self._thread = threading.Thread(target=_run, name="debug-uvicorn", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base}/api/info", timeout=2):
                    _out(f"[debug] Server ready at {self.base}")
                    return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError(f"Server did not start on port {self.port}")


# ---------------------------------------------------------------------------
# HTTP client (mirrors every web-UI API call)
# ---------------------------------------------------------------------------


def clamp_crop(
    crop_start: Optional[float],
    crop_end: Optional[float],
    max_seconds: float = MAX_VOD_SECONDS,
) -> Tuple[float, float]:
    """Ensure at most `max_seconds` of VOD content is requested."""
    start = float(crop_start or 0)
    end = float(crop_end if crop_end is not None else max_seconds)
    if end <= start:
        end = start + max_seconds
    if end - start > max_seconds:
        end = start + max_seconds
    return start, end


class ApiClient:
    def __init__(self, base: str, verbose: bool = True) -> None:
        self.base = base.rstrip("/")
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            _out(msg)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        timeout: float = 300,
    ) -> Any:
        url = f"{self.base}{path}"
        data = None
        headers: Dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        self._log(f"  {method} {path}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError:
                detail = raw
            raise RuntimeError(f"HTTP {e.code} on {path}: {detail}") from e
        if not raw:
            return {}
        return json.loads(raw)

    def get(self, path: str, **kwargs) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, body: dict, **kwargs) -> Any:
        return self._request("POST", path, body=body, **kwargs)

    # --- routes matching App.tsx ---

    def server_info(self) -> dict:
        return self.get("/api/info")

    def ytdlp_status(self) -> dict:
        return self.get("/api/ytdlp/status")

    def index_html(self) -> str:
        url = f"{self.base}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def get_settings(self) -> dict:
        return self.get("/api/settings")

    def update_settings(self, **fields) -> dict:
        return self.post("/api/settings", {k: v for k, v in fields.items() if v is not None})

    def channel_videos(
        self,
        url: str,
        *,
        limit: int = 100,
        days: int = 14,
        platforms: str = "Kick,Twitch",
    ) -> dict:
        qs = urllib.parse.urlencode({
            "url": url,
            "limit": limit,
            "days": days,
            "platforms": platforms,
        })
        return self.get(f"/api/channel/videos?{qs}")

    def info_video(self, vod_url: str) -> dict:
        qs = urllib.parse.urlencode({"id": vod_url})
        return self.get(f"/api/info/video?{qs}")

    def info_clip(self, clip_url: str) -> dict:
        qs = urllib.parse.urlencode({"id": clip_url})
        return self.get(f"/api/info/clip?{qs}")

    def download_video(
        self,
        url: str,
        *,
        quality: Optional[str] = None,
        crop_start: Optional[float] = None,
        crop_end: Optional[float] = None,
        output_file: Optional[str] = None,
        oauth: Optional[str] = None,
    ) -> dict:
        start, end = clamp_crop(crop_start, crop_end)
        body: Dict[str, Any] = {
            "url": url,
            "crop_start": start,
            "crop_end": end,
        }
        if quality:
            body["quality"] = quality
        if output_file:
            body["output_file"] = output_file
        if oauth:
            body["oauth"] = oauth
        return self.post("/api/download/video", body)

    def download_clip(
        self,
        url: str,
        *,
        quality: Optional[str] = None,
        crop_start: Optional[float] = None,
        crop_end: Optional[float] = None,
        output_file: Optional[str] = None,
    ) -> dict:
        start, end = clamp_crop(crop_start, crop_end)
        body: Dict[str, Any] = {
            "url": url,
            "crop_start": start,
            "crop_end": end,
        }
        if quality:
            body["quality"] = quality
        if output_file:
            body["output_file"] = output_file
        return self.post("/api/download/clip", body)

    def list_downloads(self) -> List[dict]:
        data = self.get("/api/downloads")
        if isinstance(data, dict):
            return list(data.get("active") or []) + list(data.get("history") or [])
        return data

    def get_download(self, download_id: str) -> dict:
        return self.get(f"/api/download/{download_id}")

    def cancel_download(self, download_id: str) -> dict:
        return self.post(f"/api/download/{download_id}/cancel", {})

    def remove_download(self, download_id: str) -> dict:
        return self.post(f"/api/download/{download_id}/remove", {})

    def open_folder(self, path: str) -> dict:
        return self.post("/api/open-folder", {"path": path})

    def watch_sse(self, download_id: str, timeout: float = 600) -> List[dict]:
        """Consume SSE until complete/error (mirrors URL tab EventSource)."""
        state = self.get_download(download_id)
        if state.get("status") in ("Completed", "Failed", "Cancelled"):
            return [{"type": "status", "data": state.get("status")}]

        url = f"{self.base}/api/download/{download_id}/stream"
        events: List[dict] = []
        req = urllib.request.Request(url)
        deadline = time.monotonic() + timeout
        with urllib.request.urlopen(req, timeout=30) as resp:
            buf = b""
            while time.monotonic() < deadline:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                text = buf.decode("utf-8", errors="replace")
                while "\n\n" in text:
                    block, text = text.split("\n\n", 1)
                    for line in block.splitlines():
                        if line.startswith("data: "):
                            evt = json.loads(line[6:])
                            events.append(evt)
                            etype = evt.get("type")
                            self._log(f"    SSE {etype}: {evt.get('data')}")
                            if etype in ("complete", "error"):
                                return events
                buf = text.encode("utf-8", errors="replace")
        return events


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_status(client: ApiClient) -> int:
    info = client.server_info()
    ytdlp = client.ytdlp_status()
    print(json.dumps({"server": info, "ytdlp": ytdlp}, indent=2))
    return 0


def cmd_settings(client: ApiClient, sets: List[str]) -> int:
    if not sets:
        print(json.dumps(client.get_settings(), indent=2))
        return 0
    fields: Dict[str, Any] = {}
    for item in sets:
        if "=" not in item:
            print(f"Invalid setting (use key=value): {item}", file=sys.stderr)
            return 1
        key, val = item.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key in ("download_threads", "max_cache_mb", "throttle_kib"):
            fields[key] = int(val)
        else:
            fields[key] = val
    out = client.update_settings(**fields)
    print(json.dumps(out, indent=2))
    return 0


def cmd_info(client: ApiClient, url: str, clip: bool) -> int:
    data = client.info_clip(url) if clip else client.info_video(url)
    print(json.dumps(data, indent=2))
    return 0


def cmd_channel(
    client: ApiClient,
    url: str,
    limit: int,
    days: int,
    platforms: str,
) -> int:
    data = client.channel_videos(url, limit=limit, days=days, platforms=platforms)
    videos = data.get("videos") or []
    errs = data.get("per_platform_errors") or {}
    kick = sum(1 for v in videos if v.get("platform") == "Kick")
    tw = sum(1 for v in videos if v.get("platform") == "Twitch")
    print(f"channel={data.get('channel')} videos={len(videos)} (Kick={kick}, Twitch={tw})")
    if errs:
        print(f"per_platform_errors: {errs}")
    for v in videos[:5]:
        print(f"  [{v.get('platform')}] {v.get('title', '?')[:60]}")
    if len(videos) > 5:
        print(f"  ... and {len(videos) - 5} more")
    print(json.dumps(data, indent=2))
    return 1 if errs and not videos else 0


def _wait_download(client: ApiClient, download_id: str, use_sse: bool) -> dict:
    deadline = time.monotonic() + 600
    sse_thread: Optional[threading.Thread] = None
    if use_sse:
        def _sse() -> None:
            try:
                client.watch_sse(download_id, timeout=580)
            except Exception as exc:
                client._log(f"    SSE ended: {exc}")

        sse_thread = threading.Thread(target=_sse, daemon=True)
        sse_thread.start()

    while time.monotonic() < deadline:
        state = client.get_download(download_id)
        status = state.get("status", "")
        _out(f"  status: {status} ({state.get('progress', 0)}%)")
        if status in ("Completed", "Failed", "Cancelled"):
            return state
        time.sleep(1)
    raise RuntimeError(f"Download {download_id} timed out")


def cmd_download(
    client: ApiClient,
    url: str,
    quality: Optional[str],
    crop_start: Optional[float],
    crop_end: Optional[float],
    output: Optional[str],
    use_sse: bool,
    clip: bool,
) -> int:
    start, end = clamp_crop(crop_start, crop_end)
    print(f"Download clip: {end - start:.0f}s max (capped at {MAX_VOD_SECONDS}s)")
    if clip:
        resp = client.download_clip(
            url, quality=quality, crop_start=start, crop_end=end, output_file=output
        )
    else:
        resp = client.download_video(
            url, quality=quality, crop_start=start, crop_end=end, output_file=output
        )
    download_id = resp["download_id"]
    print(f"Started {download_id}")
    state = _wait_download(client, download_id, use_sse)
    print(json.dumps(state, indent=2))
    if state.get("status") != "Completed":
        return 1
    out = state.get("output_file")
    if out and os.path.isfile(out):
        size = os.path.getsize(out)
        print(f"OK — {out} ({size} bytes)")
        try:
            if Path(out).name.startswith("kd_"):
                os.remove(out)
                _out("  (temp file removed)")
        except OSError:
            pass
    else:
        print("FAIL — output file missing", file=sys.stderr)
        return 1
    return 0


def cmd_queue(client: ApiClient) -> int:
    print(json.dumps(client.list_downloads(), indent=2))
    return 0


def cmd_cancel(client: ApiClient, download_id: str) -> int:
    print(json.dumps(client.cancel_download(download_id), indent=2))
    return 0


def cmd_full(client: ApiClient, headed: bool) -> int:
    """End-to-end parity run: every web-UI capability, ≤20s downloads."""
    failures: List[str] = []

    def step(name: str, fn) -> None:
        print(f"\n=== {name} ===")
        try:
            rc = fn()
            if rc != 0:
                failures.append(name)
        except Exception as e:
            print(f"FAIL: {e}")
            failures.append(name)

    def s_status() -> int:
        info = client.server_info()
        ytdlp = client.ytdlp_status()
        print(f"  {info.get('name')} v{info.get('version')} | yt-dlp={ytdlp.get('version')}")
        if not ytdlp.get("available"):
            raise RuntimeError("yt-dlp not available")
        return 0

    def s_index() -> int:
        html = client.index_html()
        if "<" not in html:
            raise RuntimeError("index page empty")
        print(f"  SPA index OK ({len(html)} bytes)")
        return 0

    def s_settings() -> int:
        before = client.get_settings()
        print(f"  quality={before.get('quality')!r} threads={before.get('download_threads')}")
        after = client.update_settings(quality="720p")
        if after.get("quality") != "720p":
            raise RuntimeError("settings POST did not persist quality")
        print("  settings round-trip OK")
        return 0

    kick_vod_url = KICK_TEST_VOD
    twitch_vod_url = TWITCH_TEST_VOD

    def s_channel() -> int:
        nonlocal kick_vod_url
        data = client.channel_videos(TEST_CHANNEL, limit=100, days=14, platforms="Kick,Twitch")
        videos = data.get("videos") or []
        errs = data.get("per_platform_errors") or {}
        kick = [v for v in videos if v.get("platform") == "Kick"]
        print(f"  {len(videos)} VODs (Kick={len(kick)}, errs={errs or 'none'})")
        if errs.get("Kick") and not kick:
            raise RuntimeError(f"Kick failed: {errs['Kick']}")
        if kick and kick[0].get("url"):
            kick_vod_url = kick[0]["url"]
            print(f"  first Kick VOD: {kick_vod_url}")
        return 0 if videos else 1

    def s_info_kick() -> int:
        info = client.info_video(kick_vod_url)
        title = info.get("title") or "?"
        print(f"  Kick info: {title[:80]}")
        return 0

    def s_info_twitch() -> int:
        info = client.info_video(twitch_vod_url)
        title = info.get("title") or "?"
        print(f"  Twitch info: {title[:80]}")
        return 0

    def s_download_kick() -> int:
        tmp = tempfile.mktemp(suffix="_kick_debug.mp4", prefix="kd_")
        return cmd_download(
            client, kick_vod_url, "720p", 0, MAX_VOD_SECONDS, tmp, True, False
        )

    def s_download_twitch() -> int:
        tmp = tempfile.mktemp(suffix="_twitch_debug.mp4", prefix="kd_")
        return cmd_download(
            client, twitch_vod_url, "720p", 0, MAX_VOD_SECONDS, tmp, True, False
        )

    def s_queue() -> int:
        items = client.list_downloads()
        print(f"  {len(items)} entries in queue")
        return 0

    def s_channel_again() -> int:
        data = client.channel_videos(TEST_CHANNEL)
        errs = data.get("per_platform_errors") or {}
        if errs.get("Kick"):
            raise RuntimeError(f"Kick error on repeat browse: {errs['Kick']}")
        print(f"  repeat browse OK ({len(data.get('videos') or [])} VODs)")
        return 0

    if headed:
        patch_headed_launch()

    step("status", s_status)
    step("index", s_index)
    step("settings", s_settings)
    step("channel browse", s_channel)
    step("info kick", s_info_kick)
    step("info twitch", s_info_twitch)
    step("download kick (20s)", s_download_kick)
    step("download twitch (20s)", s_download_twitch)
    step("queue", s_queue)
    step("channel browse (repeat)", s_channel_again)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED steps: {', '.join(failures)}")
        return 1
    print("ALL STEPS PASSED")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"API base URL (default: {DEFAULT_BASE})",
    )
    p.add_argument(
        "--spawn-server",
        action="store_true",
        help="Start embedded uvicorn before running the command",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SPAWN_PORT,
        help=f"Port for --spawn-server (default: {DEFAULT_SPAWN_PORT})",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Show Playwright Chromium windows (Kick paths only)",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="Less HTTP logging")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    p = argparse.ArgumentParser(
        description="KickDownloader debug CLI (web-UI parity, ≤20s downloads)",
    )

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="GET /api/info + /api/ytdlp/status", parents=[common])

    sp = sub.add_parser("settings", help="GET or POST /api/settings", parents=[common])
    sp.add_argument("pairs", nargs="*", help="key=value fields to update")

    sp = sub.add_parser("info", help="GET /api/info/video", parents=[common])
    sp.add_argument("--url", required=True)

    sp = sub.add_parser("clip-info", help="GET /api/info/clip", parents=[common])
    sp.add_argument("--url", required=True)

    sp = sub.add_parser("channel", help="GET /api/channel/videos", parents=[common])
    sp.add_argument("--url", default=TEST_CHANNEL)
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--platforms", default="Kick,Twitch")

    for name, clip in (("download", False), ("clip-download", True)):
        sp = sub.add_parser(
            name,
            help=f"POST /api/download/{'clip' if clip else 'video'}",
            parents=[common],
        )
        sp.add_argument("--url", required=True)
        sp.add_argument("--quality", default="720p")
        sp.add_argument("--crop-start", type=float, default=0)
        sp.add_argument("--crop-end", type=float, default=MAX_VOD_SECONDS)
        sp.add_argument("--output")
        sp.add_argument("--sse", action="store_true", help="Watch SSE stream")

    sub.add_parser("queue", help="GET /api/downloads", parents=[common])

    sp = sub.add_parser("cancel", help="POST /api/download/{id}/cancel", parents=[common])
    sp.add_argument("--id", required=True)

    sub.add_parser("full", help="Run all web-UI flows end-to-end", parents=[common])

    sp = sub.add_parser("serve", help="Start embedded server and block", parents=[common])

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.base
    spawn = getattr(args, "spawn_server", False)
    headed = getattr(args, "headed", False)
    port = getattr(args, "port", DEFAULT_SPAWN_PORT)

    if args.command == "serve":
        port = args.port
        if args.headed:
            patch_headed_launch()
        EmbeddedServer(port).start()
        print(f"Serving at http://127.0.0.1:{port} — Ctrl+C to stop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    if spawn:
        if headed:
            patch_headed_launch()
        srv = EmbeddedServer(port)
        srv.start()
        base = srv.base

    client = ApiClient(base, verbose=not args.quiet)

    if args.command == "status":
        return cmd_status(client)
    if args.command == "settings":
        return cmd_settings(client, args.pairs)
    if args.command == "info":
        return cmd_info(client, args.url, clip=False)
    if args.command == "clip-info":
        return cmd_info(client, args.url, clip=True)
    if args.command == "channel":
        return cmd_channel(client, args.url, args.limit, args.days, args.platforms)
    if args.command == "download":
        return cmd_download(
            client, args.url, args.quality, args.crop_start, args.crop_end,
            args.output, args.sse, clip=False,
        )
    if args.command == "clip-download":
        return cmd_download(
            client, args.url, args.quality, args.crop_start, args.crop_end,
            args.output, args.sse, clip=True,
        )
    if args.command == "queue":
        return cmd_queue(client)
    if args.command == "cancel":
        return cmd_cancel(client, args.id)
    if args.command == "full":
        return cmd_full(client, headed=headed)

    return 1


if __name__ == "__main__":
    sys.exit(main())
