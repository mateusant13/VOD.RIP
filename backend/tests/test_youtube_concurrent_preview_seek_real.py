"""Real-network: concurrent YouTube previews + seek to 75% VOD position.

Simulates user opening multiple previews at once (channel explore / paste race)
then seeking deep into long VODs. Hits the running API on :7897 when available.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import pytest

# 2+ full VOD watch URLs + one Short for concurrent mix
YOUTUBE_URLS = [
    ("vod_a", "https://www.youtube.com/watch?v=4kyvGbRpV7M"),
    ("vod_b", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("short", "https://www.youtube.com/shorts/IbkQI11-NZk"),
]

API_BASE = os.environ.get("VODRIP_API_BASE", "http://localhost:7897")
CREATE_BUDGET_MS = 65000  # ponytail: concurrent cold resolve on long VODs
SEEK_STREAM_BUDGET_MS = 5000


def _api_reachable() -> bool:
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{API_BASE}/api/info/video", params={"id": "https://youtu.be/dQw4w9WgXcQ"})
            return r.status_code in (200, 404)
    except Exception:
        return False


def _playable(kind: str, content: bytes, text: str) -> bool:
    if kind == "hls":
        if not text.lstrip().startswith("#EXTM3U"):
            return False
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("/api/preview/") or (ln and not ln.startswith("#")):
                return True
        return False
    return b"ftyp" in content[:32]


def _fetch_playable(
    c: httpx.Client, sid: str, kind: str,
) -> tuple[int, bytes, str]:
    if kind == "progressive":
        path = f"/api/preview/hls/{sid}/stream.mp4"
        headers = {"Range": "bytes=0-16383"}
    else:
        path = f"/api/preview/hls/{sid}/master.m3u8"
        headers = None
    r = c.get(f"{API_BASE}{path}", headers=headers)
    text = r.text if kind == "hls" else ""
    return r.status_code, r.content, text


def _vod_duration_sec(c: httpx.Client, url: str) -> float:
    """Real /api/info/video — same path the UI uses before trim."""
    ir = c.get(f"{API_BASE}/api/info/video", params={"id": url})
    if ir.status_code == 200:
        dur = float(ir.json().get("duration") or 0)
        if dur > 0:
            return dur
    return 600.0  # ponytail: fallback window when info is slow/blocked


def _run_one_preview(label: str, url: str) -> dict:
    """Full user path: warm → concurrent-style create → play → seek 75% → play."""
    out: dict = {"label": label, "url": url, "ok": False}
    t0 = time.monotonic()
    with httpx.Client(timeout=180.0) as c:
        c.post(f"{API_BASE}/api/preview/warm", json={"url": url})
        dur = _vod_duration_sec(c, url)
        crop_end = max(60.0, dur)
        out["info_duration_sec"] = dur

        cr = c.post(
            f"{API_BASE}/api/preview/session",
            json={
                "url": url,
                "crop_start": 0,
                "crop_end": crop_end,
                "prefer_height": 720,
            },
        )
        out["create_ms"] = int((time.monotonic() - t0) * 1000)
        if cr.status_code != 200:
            out["error"] = f"create {cr.status_code}: {cr.text[:200]}"
            return out
        body = cr.json()
        sid = body["session_id"]
        kind = body.get("kind") or "hls"
        dur = float(body.get("duration_sec") or dur or 0)
        out["kind"] = kind
        out["duration_sec"] = dur

        st, content, text = _fetch_playable(c, sid, kind)
        out["initial_stream_ms"] = int((time.monotonic() - t0) * 1000)
        if st not in (200, 206) or not _playable(kind, content, text):
            out["error"] = f"initial stream {st} playable={_playable(kind, content, text)}"
            c.delete(f"{API_BASE}/api/preview/session/{sid}")
            return out

        if dur <= 0:
            out["error"] = "missing duration_sec on session"
            c.delete(f"{API_BASE}/api/preview/session/{sid}")
            return out

        seek_pos = round(dur * 0.75, 2)
        out["seek_pos"] = seek_pos
        t_seek = time.monotonic()
        sr = c.post(
            f"{API_BASE}/api/preview/session/{sid}/seek",
            json={"position_sec": seek_pos},
        )
        if sr.status_code != 200:
            out["error"] = f"seek {sr.status_code}: {sr.text[:200]}"
            c.delete(f"{API_BASE}/api/preview/session/{sid}")
            return out

        st2, content2, text2 = _fetch_playable(c, sid, kind)
        out["post_seek_ms"] = int((time.monotonic() - t_seek) * 1000)
        out["total_ms"] = int((time.monotonic() - t0) * 1000)
        if st2 not in (200, 206) or not _playable(kind, content2, text2):
            out["error"] = (
                f"post-seek stream {st2} playable={_playable(kind, content2, text2)}"
            )
            c.delete(f"{API_BASE}/api/preview/session/{sid}")
            return out

        # HLS: fetch first media segment after seek playlist
        if kind == "hls":
            seg_url = None
            for ln in text2.splitlines():
                ln = ln.strip()
                if ln.startswith("/api/preview/hls/"):
                    seg_url = ln
                    break
            if seg_url:
                seg = c.get(f"{API_BASE}{seg_url}", headers={"Range": "bytes=0-8191"})
                if seg.status_code not in (200, 206) or not seg.content:
                    out["error"] = f"segment after seek {seg.status_code}"
                    c.delete(f"{API_BASE}/api/preview/session/{sid}")
                    return out

        out["ok"] = True
        c.delete(f"{API_BASE}/api/preview/session/{sid}")
        return out


@pytest.mark.timeout(300)
def test_youtube_concurrent_previews_seek_75pct_real():
    """3 YouTube URLs in parallel; each seeks to 75% of reported VOD duration."""
    if not _api_reachable():
        pytest.skip(f"API not reachable at {API_BASE} — start dev server first")
    results: list[dict] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(YOUTUBE_URLS)) as pool:
        futures = {
            pool.submit(_run_one_preview, label, url): label
            for label, url in YOUTUBE_URLS
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    wall_ms = int((time.monotonic() - t0) * 1000)
    vods = [r for r in results if r["label"].startswith("vod")]
    failures: list[str] = []

    if len(vods) < 2:
        failures.append("expected at least 2 VOD rows in test matrix")

    for r in sorted(results, key=lambda x: x["label"]):
        line = (
            f"{r['label']}: ok={r.get('ok')} kind={r.get('kind')} "
            f"dur={r.get('duration_sec')} seek={r.get('seek_pos')} "
            f"create={r.get('create_ms')}ms post_seek={r.get('post_seek_ms')}ms"
        )
        print(line)
        if r.get("error"):
            print(f"  err: {r['error']}")
        if not r.get("ok"):
            if r["label"].startswith("vod"):
                failures.append(f"{r['label']} {r.get('url')}: {r.get('error', 'unknown')}")
            else:
                print(f"  note: non-VOD {r['label']} failed (not required)")
        elif r.get("create_ms", 0) > CREATE_BUDGET_MS:
            failures.append(
                f"{r['label']} create {r['create_ms']}ms > {CREATE_BUDGET_MS}ms",
            )
        elif r.get("post_seek_ms", 0) > SEEK_STREAM_BUDGET_MS:
            failures.append(
                f"{r['label']} post-seek {r['post_seek_ms']}ms > {SEEK_STREAM_BUDGET_MS}ms",
            )

    vod_ok = sum(1 for r in vods if r.get("ok"))
    print(f"=== concurrent wall={wall_ms}ms vod_ok={vod_ok}/{len(vods)} ===")
    assert vod_ok >= 2, (
        "At least 2 YouTube VODs must preview+seek successfully:\n"
        + "\n".join(failures)
    )
    assert not failures, "\n".join(failures)
