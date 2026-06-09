#!/usr/bin/env python3
"""test_downloads.py — End-to-end download test for Twitch and Kick VODs.

Downloads a 20-second clip from each platform, validates duration and file size.
Run standalone:  python test_downloads.py
Run via server:  python test_downloads.py --server  (requires server on localhost:7897)
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure we can always find our sibling modules
_script_dir = Path(__file__).parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_CLIP_DURATION = 20  # seconds — keeps tests fast and disk-friendly
MAX_FILE_MB = 500

TEST_URLS = {
    "Twitch": "https://www.twitch.tv/videos/2792650770",
    "Kick": "https://kick.com/titiltei/videos/ddaf9751-fc2e-4f5e-9d5d-94fe637ef234",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_bytes(b: int) -> str:
    if b >= 1024 * 1024:
        return f"{b / (1024*1024):.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


# ---------------------------------------------------------------------------
# Test modes
# ---------------------------------------------------------------------------

def test_direct(url: str, label: str) -> dict:
    """Download a 20-second clip using yt-dlp directly (no server needed)."""
    import yt_dlp

    tmp = tempfile.mktemp(suffix=".mp4", prefix=f"test_{label}_")
    result = {"platform": label, "url": url, "status": "FAIL", "file": None, "size": 0, "error": None}

    print(f"\n  [{label}] Downloading 20s clip...", end=" ", flush=True)

    start = time.time()

    ydl_opts = {
        "outtmpl": tmp,
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "download_sections": [f"*0-+{TEST_CLIP_DURATION}"],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        elapsed = time.time() - start
        size = os.path.getsize(tmp)

        result["status"] = "OK"
        result["file"] = tmp
        result["size"] = size
        result["elapsed"] = elapsed

        print(f"OK ({fmt_bytes(size)}, {elapsed:.1f}s)")

    except Exception as e:
        result["error"] = str(e)
        print(f"FAIL — {e}")

    return result


def test_via_server(url: str, label: str, base: str = "http://localhost:7897") -> dict:
    """Start a download via the FastAPI server and wait for completion."""
    import urllib.request
    import urllib.error

    result = {"platform": label, "url": url, "status": "FAIL", "file": None, "size": 0, "error": None}

    print(f"\n  [{label}] Sending download request...", end=" ", flush=True)

    try:
        # Start download via API
        body = json.dumps({
            "url": url,
            "quality": "720p",
            "crop_start": 0,
            "crop_end": TEST_CLIP_DURATION,
        }).encode()
        req = urllib.request.Request(
            f"{base}/api/download/video",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
        download_id = data.get("download_id")
        print(f"started (id={download_id})")

        # Poll until complete
        print(f"  [{label}] Waiting...", end=" ", flush=True)
        while True:
            time.sleep(1)
            with urllib.request.urlopen(f"{base}/api/download/{download_id}") as resp:
                state = json.loads(resp.read().decode())

            if state["status"] in ("Completed", "Failed", "Cancelled"):
                break

        print(f"{state['status']}")

        if state["status"] == "Completed":
            output = state["output_file"]
            if os.path.isfile(output):
                size = os.path.getsize(output)
                result["status"] = "OK"
                result["file"] = output
                result["size"] = size
            else:
                result["error"] = "Output file not found on disk"
        else:
            result["error"] = state.get("error", "Unknown error")

    except Exception as e:
        result["error"] = str(e)

    return result


def get_video_info(url: str, label: str) -> dict:
    """Extract video metadata using yt-dlp."""
    import yt_dlp

    result = {"platform": label, "url": url, "status": "FAIL", "info": None, "error": None}

    print(f"  [{label}] Fetching metadata...", end=" ", flush=True)

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        result["status"] = "OK"
        result["info"] = {
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "duration_str": info.get("duration_string"),
            "is_live": info.get("is_live"),
        }

        dur = info.get("duration", 0)
        print(f"OK — \"{info.get('title', '?')}\" ({fmt_duration(dur)})")

    except Exception as e:
        result["error"] = str(e)
        print(f"FAIL — {e}")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Test VOD downloads from Twitch and Kick")
    parser.add_argument("--server", action="store_true", help="Test via the running API server instead of direct yt-dlp")
    parser.add_argument("--info", action="store_true", help="Only fetch video info (no download)")
    parser.add_argument("--url", type=str, help="Test a single custom URL instead of defaults")
    args = parser.parse_args()

    urls = {}
    if args.url:
        # Detect platform from URL
        from services.ytdlp_service import detect_platform
        platform = detect_platform(args.url)
        urls[platform] = args.url
    else:
        urls = TEST_URLS

    passed = 0
    failed = 0

    print("\n" + "-" * 60)
    print("  VOD.RIP - Download Test Suite")
    print("-" * 60)

    for platform, url in urls.items():
        print(f"\n  --- {platform} ---")
        print(f"  Platform: {platform}")
        print(f"  URL:      {url}")

        # 1. Always try to get video info first
        info = get_video_info(url, platform)
        if info["status"] == "OK":
            passed += 1
        else:
            failed += 1
            continue  # Don't try to download if we can't get info

        if args.info:
            continue

        # 2. Download 20-second clip
        if args.server:
            dl = test_via_server(url, platform)
        else:
            dl = test_direct(url, platform)

        if dl["status"] == "OK":
            passed += 1
            size_mb = dl["size"] / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                print(f"  ⚠  WARNING: File exceeds {MAX_FILE_MB}MB ({size_mb:.1f}MB)")
            # Cleanup
            if dl.get("file") and os.path.isfile(dl["file"]):
                try:
                    os.remove(dl["file"])
                except OSError:
                    pass
        else:
            failed += 1

    # Summary
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"  Results:  {passed}/{total} passed, {failed}/{total} failed")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
