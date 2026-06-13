"""Start a Kick VOD download and print elapsed time until terminal status."""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "http://localhost:7897"
URL = "https://kick.com/titiltei/videos/53ebeb1c-c691-47f9-9fd1-7139384a009a"
CROP_END = 20371


def api_post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    t0 = time.time()
    started = datetime.now(timezone.utc).isoformat()
    resp = api_post(
        "/api/download/video",
        {"url": URL, "quality": "1080p", "crop_start": 0, "crop_end": CROP_END},
    )
    download_id = resp["download_id"]
    print(f"START {download_id} at {started}", flush=True)

    while True:
        state = api_get(f"/api/download/{download_id}")
        elapsed = time.time() - t0
        status = str(state.get("status", ""))
        progress = state.get("progress", 0)
        print(f"{elapsed:8.0f}s | {status[:70]} | {progress}%", flush=True)

        if status == "Completed":
            mins, secs = divmod(int(elapsed), 60)
            hours, mins = divmod(mins, 60)
            print(
                f"DONE {download_id} in {hours}h {mins}m {secs}s ({elapsed:.1f}s total)",
                flush=True,
            )
            print(f"OUTPUT {state.get('output_file')}", flush=True)
            return 0

        if status in ("Failed", "Cancelled"):
            print(f"FAIL after {elapsed:.1f}s: {state.get('error')}", flush=True)
            return 1

        time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
