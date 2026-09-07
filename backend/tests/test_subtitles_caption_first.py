"""Caption-first fast path for /api/subtitles (GAP 3).

The router must resolve the caption tracklist with ONE InnerTube ANDROID
player call and serve the track from its timedtext URL — never paying the
full yt-dlp video-info resolve first. These tests fake the two network seams
(``youtube_innertube._player_request`` and the module's ``_caption_http_get``)
and prove:

* fast-path hit: ``guarded_youtube_dl`` is NEVER entered (patched to raise);
* the InnerTube captionTracks convert exactly like yt-dlp's process_language
  (fmt/xosf query, vssId lang code, ``kind == 'asr'`` -> auto split), so the
  shared ranking serves manual over auto;
* no captionTracks / fast-path exception / env guard off -> the yt-dlp
  fallback runs unchanged (same payload, no 502).

No network: every upstream call is monkeypatched.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from routers import subtitles as subtitles_router
from services import youtube_innertube

VTT_PT = """WEBVTT
Kind: captions
Language: pt

00:00:03.000 --> 00:00:20.470
Não sei.

00:00:20.470 --> 00:00:22.000
Ih.
"""

URL = "https://www.youtube.com/watch?v=abc123XYZ"


def _player_data(tracks: list[dict]) -> dict:
    return {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": tracks}}}


def _track(base_url: str, lang: str = "pt", vss_id: str = ".pt", asr: bool = False) -> dict:
    track = {"baseUrl": base_url, "languageCode": lang, "vssId": vss_id}
    if asr:
        track["kind"] = "asr"
    return track


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class _FetchError(Exception):
    """Track URL not served by the fixture — non-429, so no retry."""

    def __init__(self, url: str) -> None:
        super().__init__(f"HTTP Error 404: {url}")
        self.code = 404


class _FakeYdl:
    """yt-dlp fallback double: same contract as test_subtitles_router's fake."""

    def __init__(self, subs: dict, auto: dict, payload_by_url: dict) -> None:
        self._info = {"id": "abc123XYZ", "subtitles": subs, "automatic_captions": auto}
        self._payload_by_url = payload_by_url
        self.extract_calls = 0
        self.urlopen_calls: list[str] = []

    def extract_info(self, url, download=True):
        self.extract_calls += 1
        return self._info

    def urlopen(self, url: str):
        self.urlopen_calls.append(url)
        try:
            got = self._payload_by_url[url]
        except KeyError:
            raise _FetchError(url) from None
        if isinstance(got, Exception):
            raise got
        return _Resp(got)


def _patch_player(monkeypatch, data, kind: str = "ok", status: int = 200) -> dict:
    """Fake the InnerTube player call; the returned dict records the call."""
    calls: dict = {}

    def fake(video_id, profile, read_timeout, session=None, http=None):
        calls["video_id"] = video_id
        calls["profile"] = profile
        calls["timeout"] = read_timeout
        return data, status, kind

    monkeypatch.setattr(youtube_innertube, "_player_request", fake)
    return calls


def _patch_http(monkeypatch, payload_by_url: dict) -> list[str]:
    """Fake the timedtext body fetch; returns the list of requested URLs."""
    seen: list[str] = []

    def fake_get(url: str) -> _Resp:
        seen.append(url)
        try:
            got = payload_by_url[url]
        except KeyError:
            raise subtitles_router._CaptionHttpError(404, url) from None
        if isinstance(got, Exception):
            raise got
        return _Resp(got)

    monkeypatch.setattr(subtitles_router, "_caption_http_get", fake_get)
    return seen


def _patch_guard_boom(monkeypatch) -> None:
    """Prove the yt-dlp fallback is never entered on a fast-path hit."""

    @contextmanager
    def _never(opts):
        raise AssertionError("yt-dlp fallback must not run on a caption-first hit")
        yield None  # pragma: no cover

    monkeypatch.setattr(subtitles_router, "guarded_youtube_dl", _never)


def _patch_guard(monkeypatch, fake: _FakeYdl) -> None:
    @contextmanager
    def _guard(opts):
        yield fake

    monkeypatch.setattr(subtitles_router, "guarded_youtube_dl", _guard)


@pytest.fixture(autouse=True)
def _clean_subs_cache():
    with subtitles_router._subs_cache._lock:
        subtitles_router._subs_cache._data.clear()
    yield
    with subtitles_router._subs_cache._lock:
        subtitles_router._subs_cache._data.clear()


def test_fast_path_hit_never_enters_yt_dlp(monkeypatch):
    calls = _patch_player(
        monkeypatch,
        _player_data([_track("//www.youtube.com/api/timedtext?lang=pt&v=abc123XYZ")]),
    )
    served = _patch_http(
        monkeypatch,
        {"https://www.youtube.com/api/timedtext?lang=pt&v=abc123XYZ&fmt=vtt&xosf=": VTT_PT.encode()},
    )
    _patch_guard_boom(monkeypatch)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert calls["video_id"] == "abc123XYZ"
    assert calls["profile"].name == "ANDROID"
    assert payload == {
        "url": URL,
        "lang": "pt",
        "source": "manual",  # no kind == 'asr' -> manual container
        "has_subtitles": True,
        "rows": [
            {"offset_sec": 3.0, "text": "Não sei."},
            {"offset_sec": 20.47, "text": "Ih."},
        ],
    }
    # protocol-relative baseUrl upgraded; yt-dlp's fmt + empty xosf appended.
    assert served == ["https://www.youtube.com/api/timedtext?lang=pt&v=abc123XYZ&fmt=vtt&xosf="]


def test_manual_beats_auto_and_family_ranking_survives(monkeypatch):
    # The conversion must land tracks in the same containers yt-dlp uses
    # (kind != 'asr' -> subtitles), so the shared ranking — family pref,
    # then manual over auto — picks the manual 'en' over the auto 'es'.
    _patch_player(
        monkeypatch,
        _player_data(
            [
                _track("http://x/auto-es", lang="es", vss_id=".es", asr=True),
                _track("http://x/manual-en", lang="en", vss_id=".en"),
            ]
        ),
    )
    served = _patch_http(monkeypatch, {"http://x/manual-en?fmt=vtt&xosf=": VTT_PT.encode()})
    _patch_guard_boom(monkeypatch)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert (payload["lang"], payload["source"]) == ("en", "manual")
    assert served == ["http://x/manual-en?fmt=vtt&xosf="]


def test_asr_only_track_serves_auto_source(monkeypatch):
    _patch_player(
        monkeypatch,
        _player_data([_track("http://x/a", vss_id="a-pt", asr=True)]),
    )
    _patch_http(monkeypatch, {"http://x/a?fmt=vtt&xosf=": VTT_PT.encode()})
    _patch_guard_boom(monkeypatch)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert payload["lang"] == "pt"  # 'a-' prefix stripped, like yt-dlp
    assert payload["source"] == "auto"


def test_playability_failure_with_tracks_still_serves(monkeypatch):
    # ANDROID can gate formats (LOGIN_REQUIRED) while captionTracks survive —
    # the fast path reads the tracks regardless of the FailureKind.
    _patch_player(
        monkeypatch, _player_data([_track("http://x/t?lang=pt")]), kind="permanent"
    )
    _patch_http(monkeypatch, {"http://x/t?lang=pt&fmt=vtt&xosf=": VTT_PT.encode()})
    _patch_guard_boom(monkeypatch)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert payload["has_subtitles"] is True


def test_no_caption_tracks_falls_back_to_yt_dlp(monkeypatch):
    _patch_player(monkeypatch, {"captions": None})
    fake = _FakeYdl({}, {"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]}, {"http://x/pt.vtt": VTT_PT.encode()})
    _patch_guard(monkeypatch, fake)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert fake.extract_calls == 1
    assert (payload["lang"], payload["source"]) == ("pt", "auto")
    assert payload["has_subtitles"] is True


def test_fast_path_exception_falls_back_no_502(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("innertube down")

    monkeypatch.setattr(youtube_innertube, "_player_request", boom)
    fake = _FakeYdl({}, {}, {})
    _patch_guard(monkeypatch, fake)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert fake.extract_calls == 1
    assert payload == {
        "url": URL,
        "lang": None,
        "source": None,
        "has_subtitles": False,
        "rows": [],
    }


def test_env_guard_off_skips_fast_path(monkeypatch):
    monkeypatch.setenv("VODRIP_CAPTION_FIRST", "0")

    def boom(*args, **kwargs):
        raise AssertionError("fast path must not run when disabled")

    monkeypatch.setattr(youtube_innertube, "_player_request", boom)
    fake = _FakeYdl({"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]}, {}, {"http://x/pt.vtt": VTT_PT.encode()})
    _patch_guard(monkeypatch, fake)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert fake.extract_calls == 1
    assert payload["source"] == "manual"


def test_track_body_failure_falls_back(monkeypatch):
    # Tracklist served, but no body fetches on the fast path -> the yt-dlp
    # fallback gets its chance instead of a false "no subtitles" result.
    _patch_player(monkeypatch, _player_data([_track("http://x/t")]))
    _patch_http(monkeypatch, {})  # every URL -> HTTP 404
    fake = _FakeYdl({}, {"pt": [{"ext": "vtt", "url": "http://x/pt.vtt"}]}, {"http://x/pt.vtt": VTT_PT.encode()})
    _patch_guard(monkeypatch, fake)

    payload = subtitles_router.get_subtitles(url=URL, langs="en,pt,es")

    assert fake.extract_calls == 1
    assert payload["has_subtitles"] is True
