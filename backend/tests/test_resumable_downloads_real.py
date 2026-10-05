"""Real-network: the resumable download against an actual CDN.

``test_resumable_downloads.py`` proves the DECISION logic against fakes — a
fake HTTP layer and a fake yt-dlp. Fakes cannot see the failure mode that
actually bit in production, and merge b694d86 fixed two defects that only
exist against a real server and real bytes on disk:

  1. ``continuedl`` was INERT because the .part died with the attempt's
     throwaway ``mkdtemp`` — a fake server never notices that the file it is
     supposed to continue is not there.
  2. the completion check was ``max(iterdir, key=st_size)``, so a leftover
     .part could be handed to the transcriber as if it were finished audio.

This module drives the app's OWN ``download_bestaudio`` against a real CDN
that advertises ``Accept-Ranges``, interrupts a live transfer mid-flight, and
asserts that the SECOND transfer continues from the bytes already on disk —
verified on the wire, from the ``Range`` header the app's own downloader
emits, not from its own bookkeeping.

Opt-in (see backend/pytest.ini: network tests are excluded by default):

    pytest -m network backend/tests/test_resumable_downloads_real.py

Override the source with VODRIP_RESUME_TEST_URL. The default is MDN's
"flower.mp4" (CC0, 1.1 MB, served by GitHub Pages/Fastly) - small, freely
downloadable, and it advertises ``Accept-Ranges: bytes``. Any source works as
long as it serves byte ranges; the test skips (never silently passes) when it
does not.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.request
from pathlib import Path

import pytest
import yt_dlp

from services import archive_ytdlp

_DEFAULT_SOURCE = "https://mdn.github.io/shared-assets/videos/flower.mp4"

# A filesystem-safe resume-dir key; see the cdn_downloader fixture for why the
# URL itself cannot be the key.
_KEY = "cdnbest"

_TIMEOUT_S = 180.0
_HEAD_TIMEOUT_S = 30


def _head(url: str) -> tuple[int, str | None]:
    """(content_length, accept_ranges) for *url* — read-only, nothing written."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=_HEAD_TIMEOUT_S) as resp:
        return int(resp.headers.get("Content-Length") or 0), (
            resp.headers.get("Accept-Ranges") or ""
        )


def _range_start(value: str | None) -> int | None:
    """The first byte offset a ``Range: bytes=N-`` request asks for."""
    if not value:
        return None
    m = re.match(r"\s*bytes=(\d+)-", value)
    return int(m.group(1)) if m else None


def _content_range_start(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)", value)
    return int(m.group(1)) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class _RangeSpy:
    """Record what the app's downloader puts on the wire, then call through.

    Observation only: every request still goes to the real server through
    yt-dlp's own urlopen, so the transfer is the app's real transfer.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def install(self, monkeypatch) -> None:
        real = yt_dlp.YoutubeDL.urlopen
        log = self.requests  # bound here: `self` inside the spy is the YoutubeDL

        def _spy(_ydl, req, *a, **kw):
            rec: dict = {"range": None, "status": None,
                         "content_range": None, "content_length": None}
            try:
                headers = dict(getattr(req, "headers", {}) or {})
                rec["range"] = headers.get("Range") or headers.get("range")
            except Exception:  # noqa: BLE001 - a probe must never break a download
                pass
            resp = real(_ydl, req, *a, **kw)
            try:
                rec["status"] = getattr(resp, "status", None)
                rec["content_range"] = resp.headers.get("Content-Range")
                rec["content_length"] = resp.headers.get("Content-Length")
            except Exception:  # noqa: BLE001
                pass
            log.append(rec)
            return resp

        monkeypatch.setattr(yt_dlp.YoutubeDL, "urlopen", _spy)


@pytest.fixture(scope="module")
def cdn_source() -> tuple[str, int]:
    url = os.environ.get("VODRIP_RESUME_TEST_URL", _DEFAULT_SOURCE)
    try:
        length, accept_ranges = _head(url)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"source {url} unreachable ({exc.__class__.__name__})")
    # A source that will not serve a Range is a finding about the SOURCE, not a
    # pass and not a failure of the code under test — say so and do not pretend.
    if "bytes" not in accept_ranges.lower():
        pytest.skip(
            f"{url} does not advertise Accept-Ranges: bytes "
            f"(got {accept_ranges!r}) — resume cannot be exercised against it"
        )
    if length <= 0:
        pytest.skip(f"{url} reported no Content-Length")
    return url, length


@pytest.fixture(scope="module")
def reference_digest(cdn_source, tmp_path_factory) -> tuple[int, str]:
    """The source's true length and digest, fetched in one clean pass.

    This plain fetch is the ORACLE, not the subject: it is what the resumed
    artifact must equal. The code under test is always the app's own
    download_bestaudio.
    """
    url, length = cdn_source
    dest = tmp_path_factory.mktemp("reference") / "source.bin"
    with urllib.request.urlopen(url, timeout=_HEAD_TIMEOUT_S) as resp, \
            open(dest, "wb") as fh:
        while True:
            block = resp.read(1 << 16)
            if not block:
                break
            fh.write(block)
    got = dest.stat().st_size
    assert got == length, f"reference fetch {got} != Content-Length {length}"
    return length, _sha256(dest)


@pytest.fixture()
def cdn_downloader(monkeypatch, tmp_path, cdn_source):
    """download_bestaudio aimed at the real CDN, with a scratch resume dir.

    One seam only — ``_video_url``. download_bestaudio's own key->URL step is
    redirected at the CDN URL, because a URL contains ':' and Windows will not
    accept that in the per-video resume DIRECTORY name, so the test cannot use
    the URL itself as the key. Everything the test actually asserts on is the
    app's own untouched code: the stable resume dir, ``continuedl``, the
    outtmpl, governor admission, the guarded_youtube_dl wrapper,
    _finished_audio_files, and the move+drain tail.
    """
    url, _ = cdn_source
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(archive_ytdlp, "_video_url", lambda _id_or_url: url)
    return tmp_path


def _interrupt_hook(at_bytes: int):
    """Abort the transfer once *at_bytes* are on disk.

    Byte-for-byte the interruption the wall-clock cap performs: both raise
    out of download_bestaudio's own _hook, which yt-dlp propagates out of
    extract_info, leaving a live .part behind. Done on a byte count rather
    than a clock so the test does not depend on how fast the box is today.
    """
    def _hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        if int(d.get("downloaded_bytes") or 0) >= at_bytes:
            raise archive_ytdlp._YtDownloadTimedOut(
                f"test interrupt at {d.get('downloaded_bytes')} bytes")
    return _hook


def _parts(resume_dir: Path) -> list[Path]:
    return sorted(resume_dir.glob("*.part")) if resume_dir.is_dir() else []


@pytest.mark.network
def test_interrupted_download_resumes_from_the_bytes_on_disk(
    cdn_downloader, cdn_source, reference_digest, monkeypatch,
):
    """The decisive property: attempt 2 continues, it does not restart."""
    url, source_len = cdn_source
    ref_len, ref_sha = reference_digest

    resume_dir = archive_ytdlp._audio_resume_dir(_KEY)

    # --- attempt 1: a real transfer, killed mid-flight ---------------------
    first_out = cdn_downloader / "attempt1"
    with pytest.raises(archive_ytdlp._YtDownloadTimedOut):
        archive_ytdlp.download_bestaudio(
            _KEY, first_out,
            progress_hook=_interrupt_hook(64 * 1024),
            timeout_s=_TIMEOUT_S,
        )

    parts = _parts(resume_dir)
    assert parts, f"no .part survived the interrupt in {resume_dir}"
    on_disk = parts[0].stat().st_size
    assert 0 < on_disk < source_len, (
        f"partial is {on_disk} of {source_len} bytes — not a mid-transfer state")
    assert not list(first_out.glob("*")), (
        "an interrupted download handed a file to the caller")

    # Defect (2), on real state: a .part is not a finished download.
    assert archive_ytdlp._finished_audio_files(resume_dir) == [], (
        "a .part was classified as finished audio")

    # --- attempt 2: the resume --------------------------------------------
    spy = _RangeSpy()
    spy.install(monkeypatch)
    reported: list[int] = []

    def _progress(d: dict) -> None:
        if d.get("status") == "downloading":
            reported.append(int(d.get("downloaded_bytes") or 0))

    second_out = cdn_downloader / "attempt2"
    got = archive_ytdlp.download_bestaudio(
        _KEY, second_out, progress_hook=_progress, timeout_s=_TIMEOUT_S)

    # The transfer continued from the offset, it did not start over.
    sent = [r for r in spy.requests if _range_start(r["range"]) is not None]
    assert sent, (
        f"no Range request on the resume — attempt 2 re-fetched from byte 0 "
        f"(wire log: {spy.requests})")
    starts = [_range_start(r["range"]) for r in sent]
    assert on_disk in starts, (
        f"resumed at {starts} but {on_disk} bytes were on disk")
    assert all(
        _content_range_start(r["content_range"]) == _range_start(r["range"])
        for r in sent
    ), f"server did not honour the Range: {sent}"
    assert all(r["status"] == 206 for r in sent), (
        f"resume did not get a partial response: {sent}")

    # Progress is honest about the bytes already on disk.
    assert reported and reported[0] >= on_disk, (
        f"first reported progress {reported[:1]} ignores the {on_disk}-byte offset")

    # The artifact is the whole file, byte for byte.
    assert got.is_file()
    assert got.stat().st_size == source_len, (
        f"final file {got.stat().st_size} != source {source_len}")
    assert _sha256(got) == ref_sha, (
        "resumed artifact is not byte-identical to a single clean fetch")
    assert source_len == ref_len

    # The resume dir is drained: nothing for a later attempt to misread.
    assert not list(resume_dir.iterdir()), (
        f"resume dir still holds {list(resume_dir.iterdir())}")


@pytest.mark.network
def test_oversized_stale_part_is_never_returned_as_finished_audio(
    cdn_downloader, cdn_source, reference_digest,
):
    """Defect (2): a bigger .part must lose to the real file.

    The pre-merge check was ``max(iterdir, key=st_size)``, so a leftover
    .part LARGER than the real download was handed straight to the
    transcriber as finished audio. The decoy here is deliberately bigger than
    the source, which is what makes the old check fail loudly instead of by
    luck.
    """
    _, source_len = cdn_source
    _, ref_sha = reference_digest

    resume_dir = archive_ytdlp._audio_resume_dir(_KEY)
    decoy = resume_dir / "stale-decoy.m4a.part"
    decoy.write_bytes(b"\0" * (source_len + (1 << 20)))
    assert archive_ytdlp._finished_audio_files(resume_dir) == [], (
        "an oversized .part was classified as finished audio")

    out = cdn_downloader / "attempt-decoy"
    got = archive_ytdlp.download_bestaudio(_KEY, out, timeout_s=_TIMEOUT_S)

    assert got.stat().st_size == source_len
    assert _sha256(got) == ref_sha, "the .part was handed back as the audio"
    assert not decoy.exists(), "the stale .part was left behind"
    assert not list(resume_dir.iterdir())
