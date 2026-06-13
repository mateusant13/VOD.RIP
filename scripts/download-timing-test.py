"""Start a VOD download and log phase/timing until terminal status."""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "http://127.0.0.1:7897"
POLL_SEC = 8


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: download-timing-test.py <label> <url> <crop_end_sec> [crop_start]", file=sys.stderr)
        return 2
    label, url = sys.argv[1], sys.argv[2]
    crop_end = float(sys.argv[3])
    crop_start = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0

    t0 = time.time()
    started = datetime.now(timezone.utc).isoformat()
    resp = api_post(
        "/api/download/video",
        {
            "url": url,
            "quality": "1080p",
            "crop_start": crop_start,
            "crop_end": crop_end,
        },
    )
    download_id = resp["download_id"]
    print(f"=== {label} ===", flush=True)
    print(f"START {download_id} at {started}", flush=True)
    print(f"URL {url}", flush=True)
    print(f"CROP {crop_start:.0f}s -> {crop_end:.0f}s ({(crop_end-crop_start)/3600:.2f}h)", flush=True)

    last_status = ""
    phase_times: dict[str, float] = {}
    current_phase = ""

    while True:
        try:
            state = api_get(f"/api/download/{download_id}")
        except urllib.error.URLError as exc:
            print(f"{time.time()-t0:8.0f}s | poll error: {exc}", flush=True)
            time.sleep(POLL_SEC)
            continue
        elapsed = time.time() - t0
        status = str(state.get("status", ""))
        progress = state.get("progress", 0)

        phase = status.split()[0] if status else ""
        if phase and phase != current_phase:
            if current_phase:
                phase_times[current_phase] = elapsed
            current_phase = phase
            print(f"{elapsed:8.0f}s | >>> phase: {phase}", flush=True)

        if status != last_status:
            print(f"{elapsed:8.0f}s | {status[:90]} | {progress}%", flush=True)
            last_status = status

        if status == "Completed":
            mins, secs = divmod(int(elapsed), 60)
            hours, mins = divmod(mins, 60)
            print(f"DONE in {hours}h {mins}m {secs}s ({elapsed:.1f}s)", flush=True)
            print(f"OUTPUT {state.get('output_file')}", flush=True)
            print(f"PHASES {json.dumps(phase_times)}", flush=True)
            return 0

        if status in ("Failed", "Cancelled"):
            print(f"FAIL after {elapsed:.1f}s: {state.get('error', '')[:400]}", flush=True)
            print(f"PHASES {json.dumps(phase_times)}", flush=True)
            return 1

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
