"""YouTube subtitles router — live-caption endpoint for URL-only previews.

yt-dlp is mocked (no network): the fake writes fixture .vtt files exactly
where the real ``_write_subtitles`` would land them, and the router parses
those files. conftest.py already points the archive/cookie DB env at a
scratch dir, so importing services.archive_ytdlp (via the router) never
touches the real %APPDATA% DB.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

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


def _info(video_id: str = "abc123XYZ", subs=None, auto=None) -> dict:
    return {
        "id": video_id,
        "title": "Test video",
        "subtitles": subs or {},
        "automatic_captions": auto or {},
    }


class _FakeYdl:
    """Stands in for yt_dlp.YoutubeDL: writes fixture vtts into the outdir
    the opts point at, exactly like the real ``_write_subtitles``."""

    def __init__(self, info: dict, vtt_by_lang: dict[str, str]) -> None:
        self._info = info
        self._vtt_by_lang = vtt_by_lang
        self.extract_calls = 0
        self.outdir: Path | None = None

    def extract_info(self, url, download=True):
        self.extract_calls += 1
        return self._info

    def prepare_filename(self, info, kind=None):
        return "ignored"

    def _write_subtitles(self, info, base):
        assert self.outdir is not None
        written = []
        for lang, content in self._vtt_by_lang.items():
            p = self.outdir / f"{info['id']}.{lang}.vtt"
            p.write_text(content, encoding="utf-8")
            written.append((p, p))
        return written


def _patch_guard(monkeypatch, fake: _FakeYdl) -> None:
    @contextmanager
    def _guard(opts):
        fake.outdir = Path(opts["outtmpl"]).parent
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


async def test_returns_timed_rows_for_best_lang(client, monkeypatch):
    fake = _FakeYdl(_info(auto={"pt": [{"ext": "vtt", "url": "http://x"}]}), {"pt": VTT_PT})
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
    _clear_cache()


async def test_prefers_manual_subtitles_over_auto_same_family(client, monkeypatch):
    # Manual "en" + auto "en-orig" for the same family: manual wins.
    fake = _FakeYdl(
        _info(subs={"en": [{"ext": "vtt", "url": "http://x"}]},
              auto={"en-orig": [{"ext": "vtt", "url": "http://x"}]}),
        {"en": VTT_EN, "en-orig": VTT_EN},
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
    fake = _FakeYdl(_info(auto={"pt": [{"ext": "vtt", "url": "http://x"}]}), {"pt": VTT_PT})
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
