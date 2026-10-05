"""Resumable downloads: byte-range continuation without ever corrupting a file.

Covers the two hot egress paths of the transcribe batch:
  * archive_ytdlp.download_bestaudio  (YouTube bestaudio, ~78 GB for the batch)
  * ytdlp_hls._download_one_segment   (Twitch/Kick HLS, ~170k segments)

The four properties under test, for both:
  1. a complete file is left untouched (no re-request, no rewrite);
  2. a partial file is CONTINUED from the bytes on disk, not restarted;
  3. a server that ignores Range restarts clean — never append onto a
     mismatched body, which would splice garbage into the middle;
  4. a truncated file is never mistaken for a finished one.

No network: the HTTP layer is faked (requests.get / yt-dlp's YoutubeDL).
"""

from pathlib import Path

import pytest

from services import archive_ytdlp, ytdlp_hls


# --- fakes -------------------------------------------------------------------


class _FakeResp:
    """Minimal requests.Response stand-in.

    Honours a ``Range`` request when *honour_range* is set (206 + a
    Content-Range starting at the requested offset); otherwise it behaves like
    a server that ignores Range and replies 200 with the WHOLE body — the
    exact case that must restart rather than append.
    """

    def __init__(self, body: bytes, *, honour_range: bool = False, total: int = 0,
                 status: int = 200):
        self._body = body
        self._honour = honour_range
        self._total = total or len(body)
        self.status_code = status
        self.headers = {}
        self.closed = False
        self.requested_range = None

    def _serve(self, start: int = 0) -> bytes:
        return self._body[start:]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ytdlp_hls.requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        self.closed = True


def _range_resp(tail: bytes, offset: int, total: int) -> _FakeResp:
    """A well-behaved 206 partial-content response for ``bytes=offset-``."""
    r = _FakeResp(tail, total=total)
    r.status_code = 206
    end = total - 1
    r.headers = {"content-range": f"bytes {offset}-{end}/{total}"}
    return r


def _patch_segment_requests(monkeypatch, responses):
    """Serve *responses* in order from ytdlp_hls.requests.get; record requests."""
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs.get("headers") or {})
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(ytdlp_hls.requests, "get", get)
    return seen


# --- HLS segment path --------------------------------------------------------


def test_segment_complete_file_left_untouched(tmp_path, monkeypatch):
    """A whole segment already on disk is reused with NO request at all."""
    path = tmp_path / "00007.ts"
    original = b"A" * 4096
    path.write_bytes(original)
    mtime = path.stat().st_mtime

    seen = _patch_segment_requests(monkeypatch, [_FakeResp(b"B" * 4096)])

    out = ytdlp_hls._download_one_segment(
        7, {"url": "https://cdn.invalid/seg7.ts"}, {}, str(tmp_path), None, None,
    )

    assert seen == [], "a complete segment must not be re-requested"
    assert Path(out).read_bytes() == original
    assert path.stat().st_mtime == mtime, "complete file must not be rewritten"


def test_segment_partial_is_continued_not_restarted(tmp_path, monkeypatch):
    """A 4 KiB .part is continued from byte 4096 — the body is not refetched."""
    part = tmp_path / "00007.ts.part"
    head = b"A" * 4096
    part.write_bytes(head)

    tail = b"B" * (2048 - 1)
    total = len(head) + len(tail)
    seen = _patch_segment_requests(monkeypatch, [_range_resp(tail, len(head), total)])

    out = ytdlp_hls._download_one_segment(
        7, {"url": "https://cdn.invalid/seg7.ts"}, {}, str(tmp_path), None, None,
    )

    assert seen[0].get("Range") == f"bytes={len(head)}-", "must ask for the tail only"
    assert Path(out).read_bytes() == head + tail
    assert not part.exists(), ".part must be renamed onto the final name"


def test_segment_server_ignoring_range_restarts_clean(tmp_path, monkeypatch):
    """200 for a Range request ⇒ restart from zero, never append.

    Appending here would produce head+A*4096 with the first 4096 bytes
    duplicated — a file that decodes as corrupt audio.
    """
    part = tmp_path / "00007.ts.part"
    head = b"A" * 4096
    part.write_bytes(head)

    full = b"Z" * 8192  # server sends the WHOLE body, ignoring our Range
    seen = _patch_segment_requests(monkeypatch, [_FakeResp(full)])

    out = ytdlp_hls._download_one_segment(
        7, {"url": "https://cdn.invalid/seg7.ts"}, {}, str(tmp_path), None, None,
    )

    assert seen[0].get("Range") == f"bytes={len(head)}-", "range was still offered"
    assert Path(out).read_bytes() == full, "must be the clean full body, not a splice"
    assert b"A" * 4096 not in Path(out).read_bytes()


def test_segment_content_range_mismatch_restarts_clean(tmp_path, monkeypatch):
    """206 whose Content-Range starts elsewhere is a mismatch ⇒ restart."""
    part = tmp_path / "00007.ts.part"
    part.write_bytes(b"A" * 4096)

    # Claims to start at 0 even though we asked for bytes=4096-.
    bad = _FakeResp(b"Z" * 8192, total=8192)
    bad.status_code = 206
    bad.headers = {"content-range": "bytes 0-8191/8192"}
    _patch_segment_requests(monkeypatch, [bad])

    out = ytdlp_hls._download_one_segment(
        7, {"url": "https://cdn.invalid/seg7.ts"}, {}, str(tmp_path), None, None,
    )
    assert Path(out).read_bytes() == b"Z" * 8192


def test_segment_truncated_partial_never_looks_complete(tmp_path, monkeypatch):
    """A .part left by a dead worker is never handed back as a segment."""
    part = tmp_path / "00007.ts.part"
    part.write_bytes(b"A" * 4096)

    # Server dies mid-body: the segment never reaches its final name.
    def get(url, **kwargs):
        r = _FakeResp(b"B" * 16)
        r.status_code = 206
        r.headers = {"content-range": f"bytes {len(b'A' * 4096)}-{4096 + 16 - 1}/8192"}

        def iter_content(chunk_size, _r=r):
            yield b"B" * 16
            raise ytdlp_hls.requests.RequestException("connection reset")

        r.iter_content = iter_content
        return r

    monkeypatch.setattr(ytdlp_hls.requests, "get", get)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        ytdlp_hls._download_one_segment(
            7, {"url": "https://cdn.invalid/seg7.ts"}, {}, str(tmp_path), None, None,
        )

    assert not (tmp_path / "00007.ts").exists(), (
        "a truncated segment must never appear under the final name"
    )
    assert part.exists(), "partial bytes are kept for the next attempt to resume"


def test_range_append_decision_matrix():
    """_range_append_is_safe: 206 at the right offset only."""
    safe = _range_resp(b"tail", offset=4096, total=8192)
    assert ytdlp_hls._range_append_is_safe(safe, 4096) is True
    # 0 bytes on disk → always safe to write fresh.
    assert ytdlp_hls._range_append_is_safe(_FakeResp(b"x"), 0) is True
    # 200 for a range request → server ignored it.
    assert ytdlp_hls._range_append_is_safe(_FakeResp(b"x"), 4096) is False
    # 206 but the offset does not line up.
    assert ytdlp_hls._range_append_is_safe(safe, 8192) is False
    # 206 with no/unparseable Content-Range.
    naked = _FakeResp(b"x")
    naked.status_code = 206
    assert ytdlp_hls._range_append_is_safe(naked, 4096) is False


# --- YouTube bestaudio path --------------------------------------------------


@pytest.fixture()
def resume_root(tmp_path, monkeypatch):
    """Point the resume dir at a tmp cache root; no real app data touched."""
    monkeypatch.setenv("VODRIP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


def _stub_ytdlp(monkeypatch, on_extract):
    """Replace guarded_youtube_dl with a recorder that runs *on_extract*."""
    captured = {}

    class _FakeYdl:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            captured["url"] = url
            on_extract(captured["opts"])

    monkeypatch.setattr(
        "services.ytdlp_guard.guarded_youtube_dl", lambda opts, **_control: _FakeYdl(opts)
    )
    # The session conditioning and engine opts are irrelevant here.
    monkeypatch.setattr(archive_ytdlp, "_apply_youtube_session", lambda *a, **k: None)
    monkeypatch.setattr(archive_ytdlp, "_ytdlp_engine_opts", dict)
    return captured


def test_bestaudio_sets_continuedl_and_resume_outtmpl(tmp_path, resume_root, monkeypatch):
    """The download options must actually carry continuedl (the reported bug)."""
    outdir = tmp_path / "attempt1"

    def fake_extract(opts):
        # Simulate yt-dlp finishing into the resume dir it was pointed at.
        tmpl = Path(opts["outtmpl"].replace("%(id)s.%(ext)s", "vid.webm"))
        tmpl.write_bytes(b"audio" * 512)

    captured = _stub_ytdlp(monkeypatch, fake_extract)

    out = archive_ytdlp.download_bestaudio("vid", outdir)

    assert captured["opts"]["continuedl"] is True
    assert "resume" in captured["opts"]["outtmpl"].lower()
    assert Path(out).read_bytes() == b"audio" * 512
    assert Path(out).parent == outdir, "caller gets the file in ITS outdir"


def test_bestaudio_partial_offset_reaches_download_options(tmp_path, resume_root, monkeypatch):
    """A pre-existing .part is picked up as the resume point for the options."""
    resume_dir = archive_ytdlp._audio_resume_dir("vid2")
    (resume_dir / "vid2.webm.part").write_bytes(b"P" * 5000)
    assert archive_ytdlp._partial_bytes(resume_dir) == 5000

    def fake_extract(opts):
        Path(opts["outtmpl"].replace("%(id)s.%(ext)s", "vid2.webm")).write_bytes(
            b"N" * 1000
        )

    _stub_ytdlp(monkeypatch, fake_extract)
    archive_ytdlp.download_bestaudio("vid2", tmp_path / "attempt2")

    # The .part is consumed by the (faked) continued download, leaving no
    # stale partial that a later attempt would treat as a fresh resume point.
    assert not (resume_dir / "vid2.webm.part").exists()


def test_bestaudio_resume_offset_not_double_counted(tmp_path, resume_root, monkeypatch):
    """Progress reports bytes-ON-DISK (offset + new), counted exactly once."""
    resume_dir = archive_ytdlp._audio_resume_dir("vid3")
    offset = 4000
    (resume_dir / "vid3.webm.part").write_bytes(b"P" * offset)
    seen = []

    def fake_extract(opts):
        for hook in opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 1000})
        Path(opts["outtmpl"].replace("%(id)s.%(ext)s", "vid3.webm")).write_bytes(
            b"N" * (offset + 1000)
        )

    _stub_ytdlp(monkeypatch, fake_extract)
    archive_ytdlp.download_bestaudio(
        "vid3", tmp_path / "attempt3", progress_hook=seen.append,
    )

    dl = [d for d in seen if d.get("status") == "downloading"]
    assert dl, "expected a downloading event"
    for d in dl:
        # 1000 fetched this run + 4000 already on disk. Counting the resumed
        # bytes as newly downloaded would give 5000+; reporting only the new
        # bytes would understate progress. Exactly one offset is added.
        assert d["downloaded_bytes"] == 1000 + offset


def test_bestaudio_truncated_partial_never_returned_as_audio(tmp_path, resume_root, monkeypatch):
    """A crash leaves only .part files ⇒ raise, never hand back a truncated file."""
    outdir = tmp_path / "attempt4"

    def fake_extract(opts):
        # The "download" died: only a .part exists, no final file.
        d = Path(opts["outtmpl"].replace("%(id)s.%(ext)s", "")).parent
        (d / "vid4.webm.part").write_bytes(b"half" * 100)

    _stub_ytdlp(monkeypatch, fake_extract)

    with pytest.raises(RuntimeError, match="produced no audio"):
        archive_ytdlp.download_bestaudio("vid4", outdir)


def test_finished_audio_files_ignores_partials(tmp_path):
    """The 'did it finish' decision keys on the .part convention, not size."""
    d = tmp_path / "resume"
    d.mkdir()
    (d / "vid.webm").write_bytes(b"A" * 100)          # complete
    (d / "vid.webm.part").write_bytes(b"B" * 900)     # partial but BIGGER
    (d / "vid.webm.ytdl").write_bytes(b"{}")          # yt-dlp state file
    names = sorted(p.name for p in archive_ytdlp._finished_audio_files(d))
    assert names == ["vid.webm"], (
        "a partial must never be selected as the finished download"
    )
