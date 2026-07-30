"""Behavior trace — snapshots API response hashes for regression detection.

Usage:
    python scripts/behavior_trace.py                   # compare against baseline
    python scripts/behavior_trace.py --update           # update baseline snapshot

The trace exercises every endpoint through FastAPI TestClient with
deterministic inputs (invalid/missing data → stable error responses).
Happy paths involving real network I/O are excluded — those depend on
transient external state and would produce non-reproducible hashes.

Baseline stored at behavior-snapshot.json in repo root.
"""

import hashlib
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "behavior-snapshot.json"

# ── endpoint inventory ──────────────────────────────────────────────
# (method, path, body|None, status_expected, notes)
ENDPOINTS: list[tuple] = [
    ("GET", "/api/app/version", None),
    ("GET", "/api/channels", None),
    ("POST", "/api/preview/warm", {"url": "", "channel_id": "", "platform": "youtube"}),
    ("POST", "/api/preview/warm/batch", {"urls": []}),
    ("POST", "/api/preview/session", {"url": "invalid://bad-url"}),
    ("POST", "/api/preview/session", {"url": "", "session_id": ""}),
    ("DELETE", "/api/preview/session/nonexistent-session-id", None),
    ("GET", "/api/preview/session/nonexistent-session-id/status", None),
    ("GET", "/api/preview/session/nonexistent-session-id/master.m3u8", None),
    ("GET", "/api/preview/session/nonexistent-session-id/stream.mp4", None),
    ("POST", "/api/preview/session/nonexistent-session-id/quality", {"itag": -1}),
    ("POST", "/api/preview/invalidate", {"channel_id": ""}),
    ("POST", "/api/preview/live", {"platform": "twitch", "channel_id": ""}),
    ("GET", "/api/downloads", None),
    ("GET", "/api/download/nonexistent-download-id", None),
    ("DELETE", "/api/download/nonexistent-download-id", None),
    ("GET", "/api/channel/clips", None),
    # Note: /api/exit is deliberately excluded (it terminates the process)
]

# Endpoints that need context from previous responses:
ENDPOINTS_DYNAMIC: list[tuple] = [
    # These are tested by first creating a resource, then acting on it.
    # Added at runtime if creation succeeds.
]


def _hash(status: int, body: str) -> str:
    raw = f"{status}:{body}".encode(errors="replace")
    return hashlib.sha256(raw).hexdigest()


def collect_hashes(client: TestClient) -> dict[str, str]:
    """Hit every endpoint, return {label: sha256} dict."""
    hashes: dict[str, str] = {}

    for method, path, body in ENDPOINTS:
        label = f"{method} {path}"
        try:
            if body is not None:
                resp = client.request(method, path, json=body)
            else:
                resp = client.request(method, path)
            # Read content; handle streaming responses that have no .text
            try:
                body_text = resp.text
            except (RuntimeError, AttributeError):
                body_text = "<streaming>"
            hashes[label] = _hash(resp.status_code, body_text)
        except Exception as exc:
            hashes[label] = f"ERROR:{exc}"

    return hashes


def _make_app():
    """Lazy-import the app module to trigger router registration."""
    import sys
    import io
    import os
    # Ensure backend is importable
    backend_dir = str(REPO_ROOT / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    # Suppress yt-dlp noise during trace
    stderr = sys.stderr
    sys.stderr = io.StringIO()
    os.environ["VODRIP_HEADLESS"] = "1"
    try:
        from app import app as fastapi_app
    finally:
        sys.stderr = stderr
    return fastapi_app


def main() -> int:
    update = "--update" in sys.argv

    app = _make_app()
    client = TestClient(app)

    print("Collecting behavior hashes...")
    hashes = collect_hashes(client)
    print(f"  {len(hashes)} endpoints traced")

    if update:
        BASELINE_PATH.write_text(
            json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"  Baseline written to {BASELINE_PATH}")
        return 0

    if not BASELINE_PATH.exists():
        print("  No baseline snapshot found. Run with --update first.")
        return 1

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    all_ok = True
    for label in sorted(set(list(baseline.keys()) + list(hashes.keys()))):
        old = baseline.get(label)
        new = hashes.get(label)
        if old is None:
            print(f"  NEW: {label}  (not in baseline)")
            all_ok = False
        elif new is None:
            print(f"  MISSING: {label}  (removed from app)")
            all_ok = False
        elif old != new:
            print(f"  CHANGED: {label}")
            print(f"    baseline: {old}")
            print(f"    current:  {new}")
            all_ok = False

    if all_ok:
        print("  ALL HASHES MATCH — behavior unchanged.")
        return 0
    else:
        print(f"  {sum(1 for l in hashes if baseline.get(l) != hashes.get(l))} endpoints differ.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
