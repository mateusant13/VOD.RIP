import json
import sqlite3
from pathlib import Path

import pytest

from services import archive_db
from services.download_sidecars import (
    format_chat_txt,
    format_transcript_txt,
    resolve_remote_thumbnail,
    transcript_sidecar,
    write_chat_sidecar,
    write_download_sidecars,
    write_thumbnail_sidecar,
    write_transcript_sidecar,
)


def test_format_transcript_srt_like():
    """SRT shape AND its timestamps: cue index from 1, the comma-decimal
    arrow, blank-line separation, and — the contract that used to be
    untested — the REBASE that puts a trimmed download's first cue at 0:00."""
    body = format_transcript_txt([
        {"start_sec": 1.0, "end_sec": 3.5, "text": "hello"},
    ])
    assert "00:00:01,000 --> 00:00:03,500" in body
    assert "hello" in body
    # Cue index restarts at 1 and a single cue carries no trailing gap.
    assert body == "1\n00:00:01,000 --> 00:00:03,500\nhello\n"


def test_format_transcript_rebase_shifts_cues():
    """rebase_sec subtracts from BOTH ends, so a clip cut at 410s opens on
    00:00:00,000 instead of 00:06:50,000 — the defect the membership-only
    assertions could not see."""
    body = format_transcript_txt([
        {"start_sec": 410.0, "end_sec": 414.0, "text": "first"},
        {"start_sec": 420.0, "end_sec": 423.0, "text": "second"},
    ], rebase_sec=410.0)
    assert "1\n00:00:00,000 --> 00:00:04,000\nfirst" in body
    assert "2\n00:00:10,000 --> 00:00:13,000\nsecond" in body
    assert "00:06:50" not in body
    # Blocks are separated by a blank line (SRT cue delimiter).
    assert "\n\n2\n" in body


def test_format_transcript_rebase_clamps_at_zero():
    """A cue straddling the trim point must clamp to 0:00 — an SRT reader
    rejects a negative timestamp, and the audio IS present from 0:00."""
    body = format_transcript_txt([
        {"start_sec": 408.0, "end_sec": 412.0, "text": "straddles"},
    ], rebase_sec=410.0)
    assert "00:00:00,000 --> 00:00:02,000" in body
    # The start of a cue can never be negative (the arrow's own '-' aside).
    assert body.split("\n")[1].startswith("00:00:00,000 --> ")


def test_format_transcript_endless_caption_rows_get_bounds():
    """A YouTube timedtext cue carries a start and NO end — the writer borrows
    the next cue's start (clamped) instead of inventing a 2s tail, and the
    trailing cue gets the readable 2s default."""
    body = format_transcript_txt([
        {"offset_sec": 10.0, "text": "a"},
        {"offset_sec": 12.0, "text": "b"},
        {"offset_sec": 40.0, "text": "far away"},
    ])
    assert "00:00:10,000 --> 00:00:12,000" in body
    # A 28s gap is clamped to _CUE_MAX_S, not shipped as one huge cue.
    assert "00:00:12,000 --> 00:00:18,000" in body
    # Last cue has no successor: 2s, so the viewer can actually read it.
    assert "00:00:40,000 --> 00:00:42,000" in body


def test_format_transcript_keeps_a_long_asr_cue_verbatim():
    """A row that already has a real end is NOT clamped — a 30s ASR segment
    is the transcriber's verdict, not a caption gap to be tidied up."""
    body = format_transcript_txt([
        {"start_sec": 5.0, "end_sec": 35.0, "text": "long thought"},
    ])
    assert "00:00:05,000 --> 00:00:35,000" in body


def test_format_transcript_skips_empty_text_without_gaps_in_index():
    """A blank row must not burn a cue number (players number cues
    sequentially)."""
    body = format_transcript_txt([
        {"start_sec": 1.0, "end_sec": 2.0, "text": "one"},
        {"start_sec": 2.0, "end_sec": 3.0, "text": "   "},
        {"start_sec": 3.0, "end_sec": 4.0, "text": "two"},
    ])
    assert "\n2\n" in body
    assert "\n3\n" not in body


def test_format_chat_lines():
    """One `user: message` per line — no timestamps (plain text for editors)."""
    body = format_chat_txt([
        {"offset_sec": 75, "username": "bob", "text": "hi"},
        {"offset_sec": 90, "username": "alice", "text": "hello there"},
    ])
    assert body.strip() == "bob: hi\nalice: hello there"


def test_write_thumbnail_sidecar_prefers_ytdlp_jpg(tmp_path: Path):
    """A yt-dlp writethumbnail sidecar next to the output wins — no network."""
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    (tmp_path / "vod.jpg").write_bytes(b"thumb-bytes")
    assert write_thumbnail_sidecar("https://cdn.example/x.jpg", str(out)) == str(
        tmp_path / "vod.jpg"
    )


def test_write_thumbnail_sidecar_downloads_remote(tmp_path: Path):
    """Remote thumbnail URL (with Twitch placeholders) is fetched to <stem>.thumb.jpg."""
    src = tmp_path / "thumb48x36.jpg"
    src.write_bytes(b"jpeg-data")
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"video")
    url = src.as_uri().replace("thumb48x36", "thumb%{width}x%{height}")
    got = write_thumbnail_sidecar(url, str(out))
    assert got == str(tmp_path / "clip.mp4.thumb.jpg")
    assert Path(got).read_bytes() == b"jpeg-data"


def test_write_thumbnail_sidecar_no_source(tmp_path: Path):
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    assert write_thumbnail_sidecar(None, str(out)) is None
    assert write_thumbnail_sidecar("not-a-url", str(out)) is None


def test_resolve_remote_thumbnail_youtube_derived_locally():
    got = resolve_remote_thumbnail("https://www.youtube.com/watch?v=AbC123xYz9-", "YouTube")
    assert got == "https://i.ytimg.com/vi/AbC123xYz9-/mqdefault.jpg"


def test_resolve_remote_thumbnail_unknown_platform():
    assert resolve_remote_thumbnail("https://example.com/v", "Unknown") is None


# ── Trim-scoped transcript + trim-fallback chat (download-options contract) ──

_VOD = "2536167775"
_VOD_URL = "https://www.twitch.tv/videos/2536167775"


@pytest.fixture()
def _scratch_db(tmp_path, monkeypatch):
    """Isolated archive DB per test (env-swapped so archive_db reconnects)."""
    db = tmp_path / "archive.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(db))
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False
    archive_db.get_conn()
    yield db
    with archive_db._lock:
        archive_db._conn = None
        archive_db._schema_ready = False


def _seed_transcript() -> None:
    archive_db.execute(
        "DELETE FROM transcripts WHERE platform = 'twitch' AND video_id = ?", (_VOD,)
    )
    archive_db.insert_transcript("twitch", _VOD, [
        {"seg_idx": 0, "start_sec": 100.0, "end_sec": 104.0, "text": "before trim"},
        {"seg_idx": 1, "start_sec": 410.0, "end_sec": 414.0, "text": "in trim"},
        {"seg_idx": 2, "start_sec": 420.0, "end_sec": 423.0, "text": "still in trim"},
        {"seg_idx": 3, "start_sec": 600.0, "end_sec": 605.0, "text": "after trim"},
    ])


def _seed_chat() -> None:
    archive_db.execute(
        "DELETE FROM messages WHERE platform = 'twitch' AND video_id = ?", (_VOD,)
    )
    archive_db.insert_messages("twitch", _VOD, [
        {"offset_sec": 50.0, "username": "a", "text": "before trim"},
        {"offset_sec": 415.0, "username": "bob", "text": "in trim"},
        {"offset_sec": 422.0, "username": "alice", "text": "still in"},
        {"offset_sec": 700.0, "username": "a", "text": "after trim"},
    ])


def test_write_transcript_sidecar_trim_scoped(_scratch_db, tmp_path: Path):
    """Transcript sidecar covers exactly the trim window: rows outside the
    crop range are excluded, rows inside (or straddling it) are kept, and
    every cue is REBASED by crop_start so the timestamps line up with the
    trimmed media file (which starts at 0:00)."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    got = write_transcript_sidecar(
        str(out), "twitch", _VOD, crop_start=410.0, crop_end=423.0
    )
    # SRT by default: it is the only subtitle extension Premiere / Resolve /
    # Vegas / Final Cut / Shotcut / Kdenlive accept.
    assert got == str(tmp_path / "vod.srt")
    body = Path(got).read_text("utf-8")
    assert "in trim" in body
    assert "still in trim" in body
    assert "before trim" not in body
    assert "after trim" not in body
    # Rebase contract: the 410s cue must read ~0:00, NOT 00:06:50.
    assert "00:00:00,000" in body
    assert "00:06:50" not in body


def test_write_transcript_sidecar_no_trim_writes_whole(_scratch_db, tmp_path: Path):
    """Without a trim window the transcript sidecar keeps every row."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    got = write_transcript_sidecar(str(out), "twitch", _VOD)
    body = Path(got).read_text("utf-8")
    assert "before trim" in body
    assert "after trim" in body


def test_chat_sidecar_falls_back_to_trim(_scratch_db, tmp_path: Path):
    """include_chat with NO markers exports the trim window, not the whole
    VOD: messages outside crop_start/crop_end are dropped."""
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=False, include_chat=True,
        crop_start=410.0, crop_end=423.0,
        chat_start_sec=None, chat_end_sec=None,
        platform="Twitch",
    )
    body = Path(res["chat"]).read_text("utf-8")
    assert body.strip() == "bob: in trim\nalice: still in"


def test_chat_sidecar_markers_win_over_trim(_scratch_db, tmp_path: Path):
    """Explicit chat markers take precedence over the trim window."""
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=False, include_chat=True,
        crop_start=0.0, crop_end=100.0,
        chat_start_sec=415.0, chat_end_sec=422.0,
        platform="Twitch",
    )
    assert Path(res["chat"]).read_text("utf-8").strip() == "bob: in trim\nalice: still in"


def test_chat_sidecar_disabled_skipped(_scratch_db, tmp_path: Path):
    """include_chat=False writes no chat sidecar even when markers exist."""
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=False, include_chat=False,
        crop_start=410.0, crop_end=423.0,
        chat_start_sec=415.0, chat_end_sec=422.0,
        platform="Twitch",
    )
    assert "chat" not in res
    assert not (tmp_path / "vod.chat.txt").exists()


def test_download_sidecars_transcript_and_chat_share_trim(_scratch_db, tmp_path: Path):
    """One download, no chat markers: the transcript sidecar is bounded to
    the trim window AND the chat sidecar falls back to the same trim."""
    _seed_transcript()
    _seed_chat()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _VOD_URL,
        include_transcript=True, include_chat=True,
        crop_start=410.0, crop_end=423.0,
        chat_start_sec=None, chat_end_sec=None,
        platform="Twitch",
    )
    tbody = Path(res["transcript"]).read_text("utf-8")
    assert "in trim" in tbody and "still in trim" in tbody
    assert "before trim" not in tbody and "after trim" not in tbody
    # The full pipeline must also REPORT that it wrote one.
    assert res["transcript_status"] == "written"
    cbody = Path(res["chat"]).read_text("utf-8")
    assert cbody.strip() == "bob: in trim\nalice: still in"


# ── clip-slug fallback + non-silent failure (the "same for clips" contract) ──

_CLIP_SLUG = "FunnySlug1"
_CLIP_URL = f"https://clips.twitch.tv/{_CLIP_SLUG}"


@pytest.fixture()
def _isolated_data_dir(tmp_path, monkeypatch):
    """Point the clip-history lookup at a scratch data dir."""
    monkeypatch.setenv("VODRIP_DATA_DIR", str(tmp_path))
    return tmp_path


def test_clip_slug_resolves_source_vod_transcript(_scratch_db, _isolated_data_dir, tmp_path: Path):
    """A clip download carries the CLIP SLUG as its video id, which has no
    videos row — so transcript_source() returned None and clip transcripts
    almost never fired. The writer resolves the slug to its source VOD
    through the recorded clip history, exactly like the chat writer."""
    _seed_transcript()
    (_isolated_data_dir / "twitch_clips.json").write_text(
        json.dumps([{"id": _CLIP_SLUG, "url": _CLIP_URL, "vod_id": _VOD}]),
        encoding="utf-8",
    )
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"video")
    res = write_download_sidecars(
        str(out), _CLIP_URL,
        include_transcript=True, include_chat=False,
        crop_start=None, crop_end=None,
        chat_start_sec=None, chat_end_sec=None,
    )
    assert res["transcript"] == str(tmp_path / "clip.srt")
    assert res["transcript_status"] == "written"
    assert res["transcript_source"] == "archive-clip-vod"
    body = (tmp_path / "clip.srt").read_text("utf-8")
    assert "in trim" in body and "before trim" in body


def test_clip_slug_without_history_is_reported_not_silent(_scratch_db, _isolated_data_dir, tmp_path: Path):
    """No transcript and no clip history: the writer must REPORT it, not
    return None into a logger.debug and leave the user with a video and no
    explanation (defect 1)."""
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"video")
    res = transcript_sidecar(str(out), "twitch", _CLIP_SLUG)
    assert res["status"] == "unavailable"
    assert res["path"] is None
    assert "no transcript" in res["detail"]
    # Twitch/Kick have no caption track — the detail says what would fix it.
    assert "ASR" in res["detail"]
    assert not (tmp_path / "clip.srt").exists()


def test_transcript_rows_present_but_trimmed_away_is_reported(_scratch_db, tmp_path: Path):
    """A transcript that exists but has no cue inside the trim window is a
    distinct, reported case (not 'no transcript')."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = transcript_sidecar(str(out), "twitch", _VOD, crop_start=0.0, crop_end=5.0)
    assert res["status"] == "unavailable"
    assert "trimmed window" in res["detail"]


# ── output format, atomic write, and never touching a user's .txt ──

def test_formats_txt_opt_in_writes_both(_scratch_db, tmp_path: Path):
    """'srt+txt' keeps plain text available as a second file; the .srt stays
    the primary path (that is the editor-importable one)."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    res = transcript_sidecar(str(out), "twitch", _VOD, formats=("srt", "txt"))
    assert res["paths"] == {
        "srt": str(tmp_path / "vod.srt"),
        "txt": str(tmp_path / "vod.txt"),
    }
    assert res["path"] == str(tmp_path / "vod.srt")
    assert (tmp_path / "vod.srt").read_text("utf-8") == (tmp_path / "vod.txt").read_text("utf-8")


def test_default_never_touches_a_preexisting_txt(_scratch_db, tmp_path: Path):
    """The default writes the .srt and leaves a .txt the user already has on
    disk byte-for-byte alone — no silent rename, move, overwrite or delete."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    mine = tmp_path / "vod.txt"
    mine.write_text("my own notes, do not touch", encoding="utf-8")
    res = transcript_sidecar(str(out), "twitch", _VOD)
    assert res["path"] == str(tmp_path / "vod.srt")
    assert mine.read_text("utf-8") == "my own notes, do not touch"


def test_atomic_write_creates_parent_dirs(_scratch_db, tmp_path: Path):
    """Parent directories are created (the old writer only did this for the
    chat sidecar) and no temp file is left behind."""
    _seed_transcript()
    out = tmp_path / "nested" / "deeper" / "vod.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"video")
    res = transcript_sidecar(str(out), "twitch", _VOD)
    assert res["path"] == str(tmp_path / "nested" / "deeper" / "vod.srt")
    assert (tmp_path / "nested" / "deeper" / "vod.srt").is_file()
    assert list(out.parent.glob("*.tmp")) == []


def test_atomic_write_replaces_existing_srt_without_truncating_first(_scratch_db, tmp_path: Path):
    """Re-running a download replaces the .srt atomically: the old content is
    never a half-written file on disk."""
    _seed_transcript()
    out = tmp_path / "vod.mp4"
    out.write_bytes(b"video")
    target = tmp_path / "vod.srt"
    target.write_text("stale", encoding="utf-8")
    assert transcript_sidecar(str(out), "twitch", _VOD)["status"] == "written"
    assert "in trim" in target.read_text("utf-8")
    assert "stale" not in target.read_text("utf-8")

