"""YouTube subtitles router — live-caption endpoint for URL-only previews.

yt-dlp is mocked (no network): the fake returns a fixture ``info`` dict from
``extract_info`` and serves caption payload bytes (or exceptions) per track
URL from ``urlopen`` — the same contract the archive caption-fetch tests
use, since the router now fetches tracks straight from their timedtext URLs
instead of ``_write_subtitles``. conftest.py already points the archive/
cookie DB env at a scratch dir, so importing services.archive_ytdlp (via
the router) never touches the real %APPDATA% DB.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from routers import subtitles as subtitles_router

VTT_PT = """WEBVTT
Kind: captions
Language: pt

00:00:03.000 --> 00:00:20.470
Não sei.

00:00:20.470 --> 00:00:22.000
Ih.
"""

VTT_EN = """WEBVTT
Kind: captions
Language: en

00:00:01.500 --> 00:00:04.000
Hello there.
"""

JSON3_PT = (
    '{"events": ['
    '{"tStartMs": 3000, "dDurationMs": 4000, "segs": [{"utf8": "Oi.", "tOffsetMs": 0}]}'
    ']}'
)


def _info(video_id: str = "abc123XYZ", subs=None, auto=None) -> dict:
    return {
        "id": video_id,
        "title": "Test video",
        "subtitles": subs or {},
        "automatic_captions": auto or {},
    }


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class _HTTP429(Exception):
    code = 429


class _FetchError(Exception):
    """Track URL not served by the fixture — non-429, so no retry."""

    def __init__(self, url: str) -> None:
        super().__init__(f"HTTP Error 404: {url}")
        self.code = 404


class _FakeYdl:
    """Stands in for yt_dlp.YoutubeDL: extract_info returns the fixture info
    dict; urlopen serves payload bytes (or raises) keyed by track URL and
    records every call."""

    def __init__(self, info: dict, payload_by_url: dict, extract_delay: float = 0.0) -> None:
        self._info = info
        self._payload_by_url = payload_by_url
        self._extract_delay = extract_delay
        self.extract_calls = 0
        self.urlopen_calls: list[str] = []
        # The sync router runs in a Starlette threadpool worker, not the
        # test's own thread; first urlopen identifies that worker so the
        # 429-retry test can count only sleeps from the request thread.
        self.handler_thread: int | None = None

    def extract_info(self, url, download=True):
        self.extract_calls += 1
        if self._extract_delay:
            time.sleep(self._extract_delay)
        return self._info

    def urlopen(self, url: str):
        if self.handler_thread is None:
            self.handler_thread = threading.get_ident()
        self.urlopen_calls.append(url)
        try:
            got = self._payload_by_url[url]
        except KeyError:
            raise _FetchError(url) from None
        if isinstance(got, Exception):
            raise got
        return _Resp(got)


def _patch_guard(monkeypatch, fake: _FakeYdl) -> None:
    @contextmanager
    def _guard(opts):
        yield fake

    monkeypatch.setattr(subtitles_router, "guarded_youtube_dl", _guard)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _clear_cache() -> None:
    with subtitles_router._subs_cache._lock:
        subtitles_router._subs_cache._data.clear()


@pytest.fixture(autouse=True)
def _clean_subs_cache():
    """Order-proof: every test shares the module-global _subs_cache and the
    same video URL; a test that fails before its trailing _clear_cache()
    would otherwise poison the next one ('pt' served where 'en' expected).
    Clearing up front makes each test deterministic regardless of order."""
    _clear_cache()
    yield


async def test_returns_timed_rows_for_best_lang(client, monkeypatch):
    fake = _FakeYdl(
        _info(auto={"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]}),
        {"http://x/pt.vtt": VTT_PT.encode()},
    )
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_subtitles"] is True
    assert data["lang"] == "pt"
    assert data["source"] == "auto"
    assert data["rows"] == [
        {"offset_sec": 3.0, "text": "Não sei."},
        {"offset_sec": 20.47, "text": "Ih."},
    ]
    assert fake.urlopen_calls == ["http://x/pt.vtt"]  # one track, one request
    _clear_cache()


async def test_prefers_manual_subtitles_over_auto_same_family(client, monkeypatch):
    # Manual "en" + auto "en-orig" for the same family: manual wins.
    fake = _FakeYdl(
        _info(subs={"en": [{"ext": "vtt", "url": "http://x/en.vtt"}]},
              auto={"en-orig": [{"ext": "vtt", "url": "http://x/en-orig.vtt"}]}),
        {"http://x/en.vtt": VTT_EN.encode(), "http://x/en-orig.vtt": VTT_EN.encode()},
    )
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://youtu.be/abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_subtitles"] is True
    assert data["lang"] == "en"
    assert data["source"] == "manual"
    assert data["rows"][0] == {"offset_sec": 1.5, "text": "Hello there."}
    assert fake.urlopen_calls == ["http://x/en.vtt"]
    _clear_cache()


async def test_no_captions_returns_explicit_empty(client, monkeypatch):
    fake = _FakeYdl(_info(), {})
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "url": "https://www.youtube.com/watch?v=abc123XYZ",
        "lang": None,
        "source": None,
        "has_subtitles": False,
        "rows": [],
    }
    assert fake.extract_calls == 1
    assert fake.urlopen_calls == []
    _clear_cache()


async def test_skips_merged_and_junk_codes(client, monkeypatch):
    # 'en-de-DE' is a merged/translated track, 'aa' is ASR junk — neither
    # family-matches the request, so only 'pt' is ever fetched.
    fake = _FakeYdl(
        _info(auto={
            "en-de-DE": [{"ext": "vtt", "url": "http://x/junk1.vtt"}],
            "aa": [{"ext": "vtt", "url": "http://x/junk2.vtt"}],
            "pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}],
        }),
        {"http://x/pt.vtt": VTT_PT.encode()},
    )
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 200
    assert resp.json()["lang"] == "pt"
    assert fake.urlopen_calls == ["http://x/pt.vtt"]
    _clear_cache()


async def test_vtt_429_retries_then_falls_back_to_json3(client, monkeypatch):
    # Only count sleeps from the request's own threadpool worker: the patch
    # is on the module-global `time`, so a background thread (e.g. a real
    # preview backfill from an earlier suite) sleeping mid-test would
    # inflate the count and make this order-dependent. The worker thread is
    # captured by the fake on its first urlopen.
    sleeps: list[float] = []
    monkeypatch.setattr(
        subtitles_router.time, "sleep",
        lambda sec: sleeps.append(sec)
        if threading.get_ident() == fake.handler_thread else None,
    )
    info = _info(auto={"pt": [
        {"ext": "vtt", "url": "http://x/pt.vtt"},
        {"ext": "json3", "url": "http://x/pt.json3"},
    ]})
    fake = _FakeYdl(
        info,
        {"http://x/pt.vtt": _HTTP429("Too Many Requests"), "http://x/pt.json3": JSON3_PT.encode()},
    )
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_subtitles"] is True
    assert data["lang"] == "pt"
    assert data["rows"] == [{"offset_sec": 3.0, "text": "Oi."}]
    # vtt: 2 attempts (429 -> 1s backoff -> 429 -> give up), then json3.
    assert fake.urlopen_calls.count("http://x/pt.vtt") == 2
    assert fake.urlopen_calls.count("http://x/pt.json3") == 1
    assert len(sleeps) == 1
    _clear_cache()


async def test_falls_through_to_next_candidate_when_best_fails(client, monkeypatch):
    # Best-ranked pt auto track serves nothing (404 on every format) ->
    # the next candidate, manual en, wins instead of a hard failure.
    fake = _FakeYdl(
        _info(
            subs={"en": [{"ext": "vtt", "url": "http://x/en.vtt"}]},
            auto={"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]},
        ),
        {"http://x/en.vtt": VTT_EN.encode()},
    )
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["lang"] == "en"
    assert data["source"] == "manual"
    assert data["rows"][0] == {"offset_sec": 1.5, "text": "Hello there."}
    assert "http://x/pt.vtt" in fake.urlopen_calls  # best track tried first
    _clear_cache()


async def test_non_youtube_url_rejected(client, monkeypatch):
    fake = _FakeYdl(_info(), {})
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://twitch.tv/videos/12345", "langs": "en,pt,es"},
    )
    assert resp.status_code == 400
    assert fake.extract_calls == 0  # rejected before any yt-dlp work
    _clear_cache()


async def test_youtube_url_without_video_id_rejected(client, monkeypatch):
    fake = _FakeYdl(_info(), {})
    _patch_guard(monkeypatch, fake)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/", "langs": "en,pt,es"},
    )
    assert resp.status_code == 400
    assert fake.extract_calls == 0
    _clear_cache()


async def test_positive_and_negative_results_are_cached(client, monkeypatch):
    fake = _FakeYdl(
        _info(auto={"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]}),
        {"http://x/pt.vtt": VTT_PT.encode()},
    )
    _patch_guard(monkeypatch, fake)

    for _ in range(2):
        resp = await client.get(
            "/api/subtitles",
            params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
        )
        assert resp.status_code == 200
        assert resp.json()["has_subtitles"] is True
    assert fake.extract_calls == 1  # second call served from cache

    # Negative results cache too: a caption-less video is not re-fetched.
    fake2 = _FakeYdl(_info(video_id="empty0001"), {})
    _patch_guard(monkeypatch, fake2)
    for _ in range(2):
        resp = await client.get(
            "/api/subtitles",
            params={"url": "https://www.youtube.com/watch?v=empty0001", "langs": "en,pt,es"},
        )
        assert resp.json()["has_subtitles"] is False
    assert fake2.extract_calls == 1
    _clear_cache()


async def test_concurrent_requests_share_one_fetch(client, monkeypatch):
    # Two simultaneous requests for the same video: single-flight joins the
    # second onto the first's extraction instead of duplicating it.
    fake = _FakeYdl(
        _info(auto={"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]}),
        {"http://x/pt.vtt": VTT_PT.encode()},
        extract_delay=0.3,
    )
    _patch_guard(monkeypatch, fake)

    resp_a, resp_b = await asyncio.gather(
        client.get(
            "/api/subtitles",
            params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
        ),
        client.get(
            "/api/subtitles",
            params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
        ),
    )
    assert resp_a.status_code == 200 and resp_b.status_code == 200
    assert resp_a.json() == resp_b.json()
    assert fake.extract_calls == 1
    _clear_cache()


async def test_extract_failure_is_502(client, monkeypatch):
    @contextmanager
    def _boom(opts):
        raise RuntimeError("network down")
        yield  # pragma: no cover

    monkeypatch.setattr(subtitles_router, "guarded_youtube_dl", _boom)

    resp = await client.get(
        "/api/subtitles",
        params={"url": "https://www.youtube.com/watch?v=abc123XYZ", "langs": "en,pt,es"},
    )
    assert resp.status_code == 502
    _clear_cache()
