"""Real-network YouTube preview E2E — no mocks, playable body + post-warm speed."""
from __future__ import annotations

import re
import time

from starlette.testclient import TestClient
from app import app

URLS = [
    "https://www.youtube.com/shorts/IbkQI11-NZk",
    "https://www.youtube.com/shorts/t_Or3Oz5LX8",
    "https://www.youtube.com/watch?v=4kyvGbRpV7M",
    "https://www.youtube.com/watch?v=m7lRXNO1b4c",
]

POST_WARM_BUDGET_MS = 3000
STREAM_BUDGET_MS = 3000

_CODEC_RE = re.compile(r'CODECS="([^"]+)"', re.I)

def _hls_has_audio_codecs(master_text: str) -> bool:
    for m in _CODEC_RE.finditer(master_text):
        codecs = m.group(1).lower()
        if "mp4a" in codecs or "aac" in codecs or "opus" in codecs:
            return True
    for line in master_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return True
    return "#EXTM3U" in master_text


def _is_playable_preview_body(kind: str, content: bytes, ctype: str, text: str) -> bool:
    if "yt-ump" in (ctype or "").lower() or b"sabr." in content[:64]:
        return False
    if kind == "hls":
        return text.lstrip().startswith("#EXTM3U") and _hls_has_audio_codecs(text)
    return b"ftyp" in content[:32]


def test_youtube_preview_real_network():
    from services.preview_service import warm_youtube_preview_resolve
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE
    from services.preview_service import _RESOLVED_STREAM_CACHE

    _EXTRACT_INFO_CACHE.clear()
    _RESOLVED_STREAM_CACHE.clear()
    failures: list[str] = []

    with TestClient(app) as c:
        for url in URLS:
            warm_youtube_preview_resolve(url)
            t0 = time.monotonic()
            r = c.post(
                "/api/preview/session",
                json={"url": url, "crop_start": 0, "crop_end": 30, "prefer_height": 720},
            )
            session_ms = int((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                failures.append(f"CREATE {url}: {r.status_code} {r.text[:300]}")
                continue
            body = r.json()
            sid = body["session_id"]
            kind = body.get("kind") or "?"
            if kind == "hls":
                path = f"/api/preview/hls/{sid}/master.m3u8"
                t0 = time.monotonic()
                s = c.get(path)
            else:
                path = f"/api/preview/hls/{sid}/stream.mp4"
                t0 = time.monotonic()
                s = c.get(path, headers={"Range": "bytes=0-8191"})
            stream_ms = int((time.monotonic() - t0) * 1000)
            if s.status_code not in (200, 206) or not s.content:
                failures.append(
                    f"STREAM {url} kind={kind}: {s.status_code} {s.text[:200]}",
                )
                continue
            ctype = s.headers.get("content-type") or ""
            text = s.text if kind == "hls" else ""
            if not _is_playable_preview_body(kind, s.content, ctype, text):
                failures.append(
                    f"PLAYABLE {url} kind={kind} ctype={ctype} head={s.content[:40]!r}",
                )
                continue
            if session_ms > POST_WARM_BUDGET_MS:
                failures.append(
                    f"SESSION_BUDGET {url} session={session_ms}ms > {POST_WARM_BUDGET_MS}ms",
                )
            if stream_ms > STREAM_BUDGET_MS:
                failures.append(
                    f"STREAM_BUDGET {url} stream={stream_ms}ms > {STREAM_BUDGET_MS}ms",
                )

            if kind == "hls":
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("/api/preview/hls/"):
                        res = c.get(
                            line if line.startswith("http") else f"http://test{line}",
                            headers={"Range": "bytes=0-8191"},
                        )
                        if res.status_code not in (200, 206):
                            failures.append(f"RESOURCE {url}: {res.status_code}")
                        break

            ref = c.post(f"/api/preview/session/{sid}/refresh", json={})
            if ref.status_code != 200:
                failures.append(f"REFRESH {url}: {ref.status_code} {ref.text[:200]}")
            c.delete(f"/api/preview/session/{sid}")
    assert not failures, "\n".join(failures)