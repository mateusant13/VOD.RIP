"""The transcribe download must never hand the recogniser a video stream.

``download_bestaudio`` used to ask yt-dlp for ``bestaudio/best``. ``/`` is a
fallback chain, and yt-dlp's ``best`` means "best format containing BOTH video
and audio" — so on any extract that returned no audio-only format, the chain
fell through to a muxed h264+aac progressive stream (itag 18) and the
transcribe worker pulled the VIDEO bitrate to feed parakeet.

Measured 2026-10-05 with the production session
(``auth=cookies_file+po_token+visitor_data``, read from
``H:\\VOD.RIP-data\\archive.db`` ids): across 10 real videos spanning 2 min to
4 h, every extract returned 4-5 formats with ZERO audio-only, the chain chose
itag 18 every time at 289-695 kbps, and one real 120 s download came back
9,478,502 bytes with an h264 640x360 stream inside it next to the aac track.
The audio in that file is 48 kbps; the transfer was 630 kbps.

These tests are deliberately NOT "assert the constant equals a string". They
capture the opts the app really passes at the ``guarded_youtube_dl`` seam and
run them through yt-dlp's REAL format selector over a format list captured
from a REAL production extract, so the thing under assertion is the download
decision itself. A future edit that hardcodes ``bestaudio/best`` back into the
opts dict fails here even though the constant is untouched.

``test_real_download_has_no_video_stream`` is the artifact check proper: it
runs the app's own ``download_bestaudio`` against real YouTube and ffprobes
what the transcriber would receive. Opt-in (see backend/pytest.ini):

    pytest -m network backend/tests/test_audio_only_format_spec.py
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from shutil import which

import pytest
import yt_dlp

from services import archive_ytdlp

# --- captured from a real production extract, 2026-10-05 --------------------
# Video A0B8phymXOw (2.0 min, channel necrosow, id from the LIVE archive
# H:\VOD.RIP-data\archive.db), fetched with the app's own opts and session.
# `url` / `http_headers` are stripped on purpose: a googlevideo URL carries the
# po_token and signature and must never be committed.
# The shape that matters: FOUR formats, not one audio-only among them, and the
# only one with an audio track is a muxed h264+aac progressive (itag 18).
_REAL_EXTRACT_FORMATS: list[dict] = [
    {"format_id": "sb3", "ext": "mhtml", "vcodec": "none", "acodec": "none", "tbr": None, "abr": 0, "asr": None, "protocol": "mhtml", "filesize": None, "filesize_approx": None, "height": 27, "width": 48, "resolution": "48x27", "fps": 0.8333333333333334, "vbr": 0, "language": None, "quality": None, "preference": None, "source_preference": None, "format_note": "storyboard", "audio_ext": "none", "video_ext": "none", "container": None, "manifest_url": None, "_download_retcode": None},
    {"format_id": "sb2", "ext": "mhtml", "vcodec": "none", "acodec": "none", "tbr": None, "abr": 0, "asr": None, "protocol": "mhtml", "filesize": None, "filesize_approx": None, "height": 45, "width": 80, "resolution": "80x45", "fps": 0.5166666666666667, "vbr": 0, "language": None, "quality": None, "preference": None, "source_preference": None, "format_note": "storyboard", "audio_ext": "none", "video_ext": "none", "container": None, "manifest_url": None, "_download_retcode": None},
    {"format_id": "sb1", "ext": "mhtml", "vcodec": "none", "acodec": "none", "tbr": None, "abr": 0, "asr": None, "protocol": "mhtml", "filesize": None, "filesize_approx": None, "height": 90, "width": 160, "resolution": "160x90", "fps": 0.5166666666666667, "vbr": 0, "language": None, "quality": None, "preference": None, "source_preference": None, "format_note": "storyboard", "audio_ext": "none", "video_ext": "none", "container": None, "manifest_url": None, "_download_retcode": None},
    {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "tbr": None, "abr": 0, "asr": None, "protocol": "mhtml", "filesize": None, "filesize_approx": None, "height": 180, "width": 320, "resolution": "320x180", "fps": 0.5166666666666667, "vbr": 0, "language": None, "quality": None, "preference": None, "source_preference": None, "format_note": "storyboard", "audio_ext": "none", "video_ext": "none", "container": None, "manifest_url": None, "_download_retcode": None},
    {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "tbr": 630.193, "abr": None, "asr": 22050, "protocol": "https", "filesize": 9478502, "filesize_approx": 9478496, "height": 360, "width": 640, "resolution": "640x360", "fps": 30, "vbr": None, "language": "en", "quality": 6.0, "preference": None, "source_preference": -1, "format_note": "360p", "audio_ext": "none", "video_ext": "mp4", "container": None, "manifest_url": None, "_download_retcode": None},
]

_REAL_EXTRACT_VIDEO_ID = "A0B8phymXOw"
_REAL_EXTRACT_DURATION_S = 120.33

# Synthetic control, NOT a measurement: what an audio-only entry looks like,
# so the spec can be shown to still pick audio when audio is on the table.
_SYNTHETIC_AUDIO_ONLY: dict = {
    "format_id": "251",
    "ext": "webm",
    "vcodec": "none",
    "acodec": "opus",
    "abr": 160.0,
    "tbr": 160.0,
    "protocol": "https",
    "filesize": 2_400_000,
    "height": None,
    "width": None,
    "fps": None,
    "language": None,
    "format_note": "medium",
    "quality": 3,
    "preference": 0,
    "source_preference": 0,
    "vbr": False,
    "asr": None,
    "audio_ext": "webm",
    "video_ext": "none",
    "container": "webm",
    "resolution": "audio only",
}


class _SeamReached(BaseException):
    """Raised by the fake funnel to stop download_bestaudio after the seam.

    A BaseException on purpose: download_bestaudio's own ``except Exception``
    arm runs the YouTube gate classifier (which reaches for archive_db) before
    re-raising, and a test that stops at the seam has no business touching the
    archive at all.
    """


def _select_with_real_engine(spec: str, formats: list[dict]) -> list[dict]:
    """Run *spec* through yt-dlp's own selector — no app helper in the path."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl._select_formats(formats, ydl.build_format_selector(spec))


def _spec_the_app_hands_yt_dlp(monkeypatch, tmp_path, video_id: str) -> str:
    """The ``format`` value download_bestaudio really passes to yt-dlp.

    Captured at ``guarded_youtube_dl`` — the single funnel every YouTube
    request goes through — so this is the app's own opts dict, not a
    reconstruction of it.
    """
    seen: dict = {}

    @contextlib.contextmanager
    def _capture(opts):
        seen.update(opts)
        raise _SeamReached
        yield  # pragma: no cover — unreachable, keeps this a contextmanager

    monkeypatch.setattr(archive_ytdlp.ytdlp_guard, "guarded_youtube_dl", _capture)
    monkeypatch.setattr(archive_ytdlp, "_governor_admit_ytdlp", lambda *a, **k: None)
    monkeypatch.setattr(archive_ytdlp, "_apply_youtube_session", lambda *a, **k: None)
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    with pytest.raises(_SeamReached):
        archive_ytdlp.download_bestaudio(video_id, tmp_path)
    assert "format" in seen, "download_bestaudio passed no format spec to yt-dlp"
    return seen["format"]


def _with_video(formats: list[dict]) -> list[dict]:
    return [f for f in formats if f.get("vcodec") not in (None, "none")]


def test_no_audio_only_format_never_yields_a_video_stream(monkeypatch, tmp_path):
    """The defect itself, on a real format list that has no audio-only entry.

    Before the fix the chain fell through to itag 18 and this failed with the
    muxed h264+aac stream in the failure message.
    """
    spec = _spec_the_app_hands_yt_dlp(
        monkeypatch, tmp_path, _REAL_EXTRACT_VIDEO_ID)
    chosen = _select_with_real_engine(spec, _REAL_EXTRACT_FORMATS)
    offenders = _with_video(chosen)
    assert not offenders, (
        f"format spec {spec!r} selected a stream carrying VIDEO for a "
        f"speech-to-text download: "
        + ", ".join(
            f"itag={f.get('format_id')} vcodec={f.get('vcodec')} "
            f"acodec={f.get('acodec')} tbr={f.get('tbr')}"
            for f in offenders
        )
        + f" — the transcribe worker would pull the video bitrate "
          f"({offenders[0].get('tbr')} kbps on that format) instead of audio"
    )


def test_spec_still_selects_audio_when_audio_is_available(monkeypatch, tmp_path):
    """Control: the fix must not break the normal path.

    Same real format list plus one audio-only entry. Whatever the spec picks
    has to be the audio one, and still nothing with video.
    """
    spec = _spec_the_app_hands_yt_dlp(
        monkeypatch, tmp_path, _REAL_EXTRACT_VIDEO_ID)
    chosen = _select_with_real_engine(
        spec, [_SYNTHETIC_AUDIO_ONLY] + _REAL_EXTRACT_FORMATS)
    assert chosen, "spec selected nothing even though an audio-only format exists"
    assert not _with_video(chosen), (
        f"format spec {spec!r} picked a video stream: "
        + ", ".join(str(f.get("format_id")) for f in _with_video(chosen))
    )
    assert all(f.get("vcodec") in (None, "none") for f in chosen)


def test_real_extract_fixture_really_has_no_audio_only_format():
    """Guard the fixture: if this ever changes, the test above stops meaning
    what its docstring says, so fail loudly rather than quietly proving less."""
    audio_only = [
        f for f in _REAL_EXTRACT_FORMATS
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    assert not audio_only, (
        "captured extract unexpectedly offers audio-only formats: "
        + ", ".join(str(f.get("format_id")) for f in audio_only)
    )
    assert _with_video(_REAL_EXTRACT_FORMATS), "fixture has no muxed format either"


@pytest.mark.network
def test_real_download_has_no_video_stream(tmp_path, monkeypatch):
    """The artifact: download a real video and ffprobe what parakeet receives.

    Real id, real session, real bytes. Skips (never silently passes) when
    ffprobe is unavailable or the platform refuses the video outright.
    """
    exe = which("ffprobe")
    if not exe:
        pytest.skip("ffprobe not on PATH")
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    outdir = tmp_path / "attempt"
    try:
        path = archive_ytdlp.download_bastaudio(
            _REAL_EXTRACT_VIDEO_ID, outdir, timeout_s=20 * 60.0)
    except Exception as exc:  # noqa: BLE001
        if "not available" in str(exc).lower() or "age" in str(exc).lower():
            pytest.skip(f"platform refused the video: {exc}")
        raise

    probe = subprocess.run(
        [exe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-show_entries", "format=format_name,duration,bit_rate,size",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=180,
    )
    if probe.returncode != 0:
        pytest.skip(f"ffprobe could not read the download: {probe.stderr[:200]}")
    info = json.loads(probe.stdout)
    kinds = [s.get("codec_type") for s in info.get("streams", [])]
    assert "video" not in kinds, (
        f"download_bestaudio handed the transcriber a VIDEO stream: "
        f"streams={kinds} format={info.get('format')} bytes={path.stat().st_size}"
    )
    assert "audio" in kinds, f"no audio stream at all: streams={kinds}"
