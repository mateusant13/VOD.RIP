"""Real-network YouTube DASH window HLS preview — seg0/1/2 must mux."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import app

# ponytail: candidates chosen for DASH-only or separate-audio tiers (not Shorts HLS)
CANDIDATES = [
    "https://www.youtube.com/watch?v=r6rh6Wh2WjI",
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "https://www.youtube.com/watch?v=4kyvGbRpV7M",
]


def _is_mpegts(body: bytes) -> bool:
    if len(body) < 376:
        return False
    hits = sum(1 for i in range(0, min(len(body), 188 * 5), 188) if body[i] == 0x47)
    return hits >= 2


def _fetch(c, path: str, retries: int = 180) -> tuple[int, bytes]:
    for _ in range(retries):
        resp = c.get(path)
        if resp.status_code == 503:
            time.sleep(1)
            continue
        return resp.status_code, resp.content
    return 503, b""


def _parse_extinf_durations(playlist_text: str) -> list[float]:
    out: list[float] = []
    for line in playlist_text.splitlines():
        if line.startswith("#EXTINF:"):
            out.append(float(line.split(":", 1)[1].split(",")[0]))
    return out


def _audit_segment_body(content: bytes, declared_sec: float) -> str | None:
    from services.ytdlp_ffmpeg import _resolve_ffprobe_exe, audit_segment_extinf

    if not _resolve_ffprobe_exe():
        return None
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        audit = audit_segment_extinf(tmp_path, declared_sec)
        if audit.ok:
            return None
        return (
            f"EXTINF audit declared={declared_sec:.3f} actual={audit.actual_duration:.3f} "
            f"delta={audit.delta:+.3f} pts={audit.first_pts}..{audit.last_pts}"
        )
    finally:
        tmp_path.unlink(missing_ok=True)



def test_youtube_window_hls_segments_zero_one_two():
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE

    _EXTRACT_INFO_CACHE.clear()
    failures: list[str] = []
    tested = False

    with TestClient(app) as c:
        for url in CANDIDATES:
            created = c.post(
                "/api/preview/session",
                json={"url": url, "crop_start": 0, "crop_end": 35, "prefer_height": 720},
            )
            if created.status_code != 200:
                failures.append(f"CREATE {url}: {created.status_code}")
                continue
            body = created.json()
            sid = body["session_id"]
            if not body.get("trim_timeline"):
                c.delete(f"/api/preview/session/{sid}")
                continue

            tested = True
            master = c.get(f"/api/preview/hls/{sid}/master.m3u8")
            if master.status_code != 200 or "window-playlist" not in master.text:
                failures.append(f"MASTER {url}: {master.status_code}")
                c.delete(f"/api/preview/session/{sid}")
                continue

            playlist_line = next(
                (
                    ln.strip()
                    for ln in master.text.splitlines()
                    if ln.strip().startswith("/api/") and "window-playlist" in ln
                ),
                "",
            )
            media_status, media = _fetch(c, playlist_line, retries=180)
            if media_status != 200:
                failures.append(f"MEDIA {url}: HTTP {media_status}")
                c.delete(f"/api/preview/session/{sid}")
                continue
            media_text = media.decode("utf-8", errors="replace")
            seg_paths = [
                ln.strip()
                for ln in media_text.splitlines()
                if ln.strip().startswith("/api/") and "window-seg-" in ln
            ]
            extinf = _parse_extinf_durations(media_text)
            if len(seg_paths) < 3:
                failures.append(f"PLAYLIST {url}: only {len(seg_paths)} segments")
                c.delete(f"/api/preview/session/{sid}")
                continue

            for idx, path in enumerate(seg_paths[:3]):
                status, content = _fetch(c, path)
                if status not in (200, 206):
                    failures.append(f"seg{idx} {url}: HTTP {status}")
                    continue
                if len(content) < 50_000:
                    failures.append(f"seg{idx} {url}: tiny body {len(content)}")
                    continue
                if not _is_mpegts(content):
                    failures.append(f"seg{idx} {url}: not MPEG-TS head={content[:20]!r}")
                    continue
                if idx < len(extinf):
                    audit_err = _audit_segment_body(content, extinf[idx])
                    if audit_err:
                        failures.append(f"seg{idx} {url}: {audit_err}")

            if len(seg_paths) >= 6:
                status, content = _fetch(c, seg_paths[5], retries=240)
                if status not in (200, 206) or not _is_mpegts(content):
                    failures.append(f"seg5 {url}: seek-mid failed status={status}")

            c.delete(f"/api/preview/session/{sid}")
            break

    if not tested:
        pytest.skip("no DASH window-HLS YouTube URL in candidates")
    assert not failures, "\n".join(failures)
