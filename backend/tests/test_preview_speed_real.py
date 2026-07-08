"""Real-network preview SPEED+WORKING parity — YouTube / Kick / Twitch.

Real UX path: URL paste fires /api/preview/warm (async), user clicks preview;
create_session awaits in-flight warm then serves cached resolve.

Budgets (kick/twitch parity):
  - POST_WARM   : YouTube UX — post-warm session + first playable bytes <= 3000ms
  - TWITCH_KICK : Twitch/Kick CI — same path <= 4000ms (HLS/CDN variance ponytail)
  - PASTE_WARM  : cold YouTube extract on paste (background) <= 4000ms
  - SEEK        : post-seek first playable bytes <= 2000ms
"""
from __future__ import annotations

import statistics
import time
from typing import Optional

import pytest
from starlette.testclient import TestClient

from app import app

POST_WARM_BUDGET_MS = 3000
TWITCH_KICK_BUDGET_MS = 4000  # ponytail: cold HLS/progressive resolve variance on long VODs
PASTE_WARM_BUDGET_MS = 4000  # ponytail: cold YouTube extract variance on paste
SEEK_BUDGET_MS = 2000

PLATFORMS: list[tuple[str, str, float, bool]] = [
    # platform, url, seek_sec, needs_youtube_warm
    ("YouTube", "https://www.youtube.com/shorts/IbkQI11-NZk", 10.0, True),
    ("YouTube", "https://www.youtube.com/watch?v=4kyvGbRpV7M", 30.0, True),
    ("Twitch", "https://www.twitch.tv/videos/2811184893", 30.0, False),
    ("Kick", "https://kick.com/titiltei/videos/53ebeb1c-c691-47f9-9fd1-7139384a009a", 30.0, False),
]

_RESULTS: list[dict] = []


def _clear_caches() -> None:
    from services.preview_service import _RESOLVED_STREAM_CACHE
    from services.ytdlp_hls import _EXTRACT_INFO_CACHE
    from services.youtube_session import invalidate_anonymous_session

    try:
        invalidate_anonymous_session()
    except Exception:
        pass
    _EXTRACT_INFO_CACHE.clear()
    _RESOLVED_STREAM_CACHE.clear()


def _is_playable_body(kind: str, content: bytes, text: str) -> bool:
    if kind == "hls":
        if not text.lstrip().startswith("#EXTM3U"):
            return False
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("/api/preview/") or (ln and not ln.startswith("#")):
                return True
        return False
    return b"ftyp" in content[:32]


def _is_platform_blocked(resp) -> bool:
    if resp.status_code != 500:
        return False
    try:
        detail = (resp.json().get("detail") or "").lower()
    except Exception:
        return False
    return ("unavailable" in detail) or ("try again" in detail) or ("not found" in detail)


def _create_session(c: TestClient, url: str) -> tuple[Optional[str], Optional[str], object, int]:
    t0 = time.monotonic()
    r = c.post(
        "/api/preview/session",
        json={"url": url, "crop_start": 0, "crop_end": 30, "prefer_height": 720},
    )
    session_ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return None, None, r, session_ms
    body = r.json()
    return body.get("session_id", ""), body.get("kind", "hls"), r, session_ms


def _fetch_first_bytes(c: TestClient, sid: str, kind: str) -> tuple[int, int, int, bytes, str]:
    if kind == "progressive":
        path = f"/api/preview/hls/{sid}/stream.mp4"
        headers = {"Range": "bytes=0-8191"}
    else:
        path = f"/api/preview/hls/{sid}/master.m3u8"
        headers = None
    t0 = time.monotonic()
    s = c.get(path, headers=headers)
    stream_ms = int((time.monotonic() - t0) * 1000)
    text = s.text if kind == "hls" else ""
    return s.status_code, stream_ms, len(s.content), s.content, text


def _warm_youtube_paste_path(c: TestClient, url: str) -> int:
    """Simulate paste: POST /api/preview/warm then wait for in-flight warm."""
    from services.preview_service import await_youtube_warm_if_pending

    t0 = time.monotonic()
    c.post("/api/preview/warm", json={"url": url})
    await_youtube_warm_if_pending(url, timeout_sec=60.0)
    return int((time.monotonic() - t0) * 1000)


class _SessionFinalizer:
    @pytest.hookimpl
    def pytest_sessionfinish(self, session, exitstatus):  # noqa: ARG002
        if not _RESULTS:
            return
        by_platform: dict[str, list[dict]] = {}
        for r in _RESULTS:
            by_platform.setdefault(r["platform"], []).append(r)
        print("\n=== preview_speed_real timings (ms) ===")
        for platform, rows in by_platform.items():
            post = [r["post_warm_ms"] for r in rows if "post_warm_ms" in r]
            paste = [r["paste_ms"] for r in rows if "paste_ms" in r]
            seek = [r["post_seek_ms"] for r in rows if "post_seek_ms" in r]
            line = f"  {platform:<8}"
            if post:
                line += f"  post_warm_med={int(statistics.median(post)):>5}"
            if paste:
                line += f"  paste_med={int(statistics.median(paste)):>5}"
            if seek:
                line += f"  seek_med={int(statistics.median(seek)):>5}"
            print(line)
        print("=== /preview_speed_real ===\n")


@pytest.fixture(autouse=True, scope="session")
def _register_session_finalizer(request):
    request.config.pluginmanager.register(
        _SessionFinalizer(), name="preview_speed_real_finalizer"
    )


_PLATFORM_IDS = [f"{p[0]}:{p[1].split('/')[-1][:24]}" for p in PLATFORMS]


@pytest.mark.timeout(120)
@pytest.mark.parametrize("platform,url,position_sec,needs_warm", PLATFORMS, ids=_PLATFORM_IDS)
def test_preview_speed_post_warm_real(platform: str, url: str, position_sec: float, needs_warm: bool):
    """After paste warm completes, session + first bytes within platform budget (YouTube <=3s UX)."""
    _clear_caches()
    with TestClient(app) as c:
        if needs_warm:
            paste_ms = _warm_youtube_paste_path(c, url)
            _RESULTS.append({"platform": platform, "url": url, "paste_ms": paste_ms})

        sid, kind, resp, session_ms = _create_session(c, url)
        if sid is None:
            if _is_platform_blocked(resp):
                pytest.skip(f"{platform} extract blocked: {resp.text[:200]}")
            pytest.fail(f"CREATE {url}: {resp.status_code} {resp.text[:200]}")

        fstatus, stream_ms, nbytes, body, text = _fetch_first_bytes(c, sid, kind)
        post_warm_ms = session_ms + stream_ms
        _RESULTS.append(
            {
                "platform": platform,
                "url": url,
                "post_warm_ms": post_warm_ms,
                "session_ms": session_ms,
                "stream_ms": stream_ms,
            }
        )
        assert fstatus in (200, 206), f"{platform} stream={fstatus}"
        assert nbytes > 0
        assert _is_playable_body(kind, body, text)
        budget = POST_WARM_BUDGET_MS if platform == "YouTube" else TWITCH_KICK_BUDGET_MS
        assert post_warm_ms <= budget, (
            f"{platform} post-warm {post_warm_ms}ms > {budget}ms "
            f"(session={session_ms} stream={stream_ms})"
        )
        c.delete(f"/api/preview/session/{sid}")


@pytest.mark.timeout(120)
@pytest.mark.parametrize("platform,url,position_sec,needs_warm", PLATFORMS, ids=_PLATFORM_IDS)
def test_preview_seek_speed_real(platform: str, url: str, position_sec: float, needs_warm: bool):
    """Seek ack + post-seek playable body within 2s."""
    _clear_caches()
    with TestClient(app) as c:
        if needs_warm:
            _warm_youtube_paste_path(c, url)
        sid, kind, resp, _ = _create_session(c, url)
        if sid is None:
            if _is_platform_blocked(resp):
                pytest.skip(f"{platform} extract blocked: {resp.text[:200]}")
            pytest.fail(f"CREATE {url}: {resp.status_code} {resp.text[:200]}")
        _fetch_first_bytes(c, sid, kind)

        seek_resp = c.post(
            f"/api/preview/session/{sid}/seek",
            json={"position_sec": position_sec},
        )
        assert seek_resp.status_code == 200, seek_resp.text[:200]

        fstatus, post_seek_ms, nbytes, body, text = _fetch_first_bytes(c, sid, kind)
        _RESULTS.append({"platform": platform, "post_seek_ms": post_seek_ms})
        assert fstatus in (200, 206)
        assert nbytes > 0
        assert _is_playable_body(kind, body, text)
        assert post_seek_ms <= SEEK_BUDGET_MS, (
            f"post-seek {platform} {post_seek_ms}ms > {SEEK_BUDGET_MS}ms"
        )
        c.delete(f"/api/preview/session/{sid}")


@pytest.mark.timeout(120)
def test_youtube_paste_to_playable_budget():
    """Full paste path: async warm + awaited session + stream for a Short <= 3.5s."""
    from services.preview_service import await_youtube_warm_if_pending

    url = "https://www.youtube.com/shorts/IbkQI11-NZk"
    _clear_caches()
    with TestClient(app) as c:
        t0 = time.monotonic()
        c.post("/api/preview/warm", json={"url": url})
        await_youtube_warm_if_pending(url, timeout_sec=60.0)
        warm_ms = int((time.monotonic() - t0) * 1000)
        sid, kind, resp, session_ms = _create_session(c, url)
        if sid is None:
            if _is_platform_blocked(resp):
                pytest.skip(f"YouTube blocked: {resp.text[:200]}")
            pytest.fail(resp.text[:200])
        fstatus, stream_ms, nbytes, body, text = _fetch_first_bytes(c, sid, kind)
        post_ms = session_ms + stream_ms
        assert fstatus in (200, 206) and nbytes > 0
        assert _is_playable_body(kind, body, text)
        assert warm_ms <= PASTE_WARM_BUDGET_MS, f"warm {warm_ms}ms > {PASTE_WARM_BUDGET_MS}ms"
        assert post_ms <= POST_WARM_BUDGET_MS, (
            f"post-warm {post_ms}ms > {POST_WARM_BUDGET_MS}ms (session={session_ms} stream={stream_ms})"
        )
        c.delete(f"/api/preview/session/{sid}")


WARM_DEDUP_RACE_BUDGET_MS = 5000


@pytest.mark.timeout(120)
def test_youtube_warm_dedup_race_real():
    """Warm + immediate create_session must share one extract (no double work).

    Scenario: paste fires /api/preview/warm (background); user clicks preview
    moments later, triggering /api/preview/session for the same URL. With dedup
    the session awaits the in-flight warm and reuses the populated extract
    cache. A double extract (warm + session) would take ~6-7s end-to-end on
    a Short; with dedup it must finish in <= 5s.
    """
    url = "https://www.youtube.com/shorts/IbkQI11-NZk"
    _clear_caches()
    with TestClient(app) as c:
        t0 = time.monotonic()
        # Kick off warm — do NOT await; the session is the consumer.
        warm_resp = c.post("/api/preview/warm", json={"url": url})
        assert warm_resp.status_code == 200, warm_resp.text[:200]
        assert warm_resp.json().get("warmed") is True

        # Immediately create session for the same URL. create_session
        # internally calls await_youtube_warm_if_pending(url), so it should
        # join the in-flight warm rather than re-extract.
        sid, kind, resp, session_ms = _create_session(c, url)
        total_ms = int((time.monotonic() - t0) * 1000)

        if sid is None:
            if _is_platform_blocked(resp):
                pytest.skip(f"YouTube blocked: {resp.text[:200]}")
            pytest.fail(f"CREATE {url}: {resp.status_code} {resp.text[:200]}")

        fstatus, stream_ms, nbytes, body, text = _fetch_first_bytes(c, sid, kind)
        post_ms = session_ms + stream_ms

        _RESULTS.append(
            {
                "platform": "YouTube",
                "url": url,
                "post_warm_ms": post_ms,
                "session_ms": session_ms,
                "stream_ms": stream_ms,
            }
        )

        assert fstatus in (200, 206), f"stream={fstatus}"
        assert nbytes > 0
        assert _is_playable_body(kind, body, text)
        assert total_ms <= WARM_DEDUP_RACE_BUDGET_MS, (
            f"warm+session dedup race took {total_ms}ms "
            f"(session={session_ms} stream={stream_ms}); double extract suspected "
            f"(budget {WARM_DEDUP_RACE_BUDGET_MS}ms)"
        )
        c.delete(f"/api/preview/session/{sid}")
