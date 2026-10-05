"""Sharded decode -> the .srt the user downloads, END TO END.

``test_sharded_clip_offsets.py`` pins the arithmetic one layer up: it asserts
``start_sec``/``end_sec``/word times on the segments the decode seam returns.
That was the right layer to fix at, and it is NOT the layer that shipped the
bug twice, because nothing between that assertion and the artifact was
covered. A clip's absolute start can be correct on the segment and still be
wrong in the file if the SRT writer rebases, clamps, or re-reads it through a
store. The artifact is the deliverable: the SRT a user imports into an editor
and watches out of sync.

So this file drives the WHOLE chain, with the sharded pieces real:

    _ShardedAudio (real int16 shard files on disk, real absolute reads,
                   real end-of-audio clamping)
      -> _clips_to_audio          archive_transcribe.py:3221  (builds the
                   concatenated batch buffer AND clip_offsets - the two
                   descriptions of the same instant that got added together)
      -> _decode_batch            archive_transcribe.py:2719  (THE seam: Photon
                   over sherpa-onnx; the batched >1 branch that carried the
                   bug is archive_transcribe.py:2513)
      -> _commit_chunk_rows       archive_transcribe.py:3776  (seg_idx
                   allocation + the real archive_db.insert_transcript)
      -> archive_db              round-trips the rows, as a later download does
      -> transcript_sidecar      download_sidecars.py:275 -> format_transcript_txt
                   download_sidecars.py:128 -> _hms download_sidecars.py:61
      -> <stem>.srt              the bytes the user downloads

The production multi-clip sharded batch is ``_transcribe_audio_source``
(archive_transcribe.py:4591-4600, the per-call decode batch); the hybrid lane
at archive_transcribe.py:3886 feeds the same seam one clip at a time. The
arithmetic under test is the same either way, so this file starts at the seam's
input — the one boundary that is a real boundary in production and cheap to
drive without a model.

Two properties are pinned, and BOTH are needed:

  * the cue times, as exact ``HH:MM:SS,mmm`` strings, against KNOWN-CORRECT
    absolute values. This is the assertion that catches the bug: a clip's
    position inside the batch buffer is a slicing detail, and pre-fix it was
    ADDED to the clip's absolute start, so every clip after the first landed
    late in the file. Monotonicity cannot see that - the wrong times are
    perfectly ordered.
  * both engines against those same constants, not merely against each other.
    The Photon lane deliberately mirrored the bug so the two agreed; a test
    that pins engine A to engine B passes while both are wrong the same way.

No model load, no inference, no GPU, no network, no sidecar process: the
recognizer and the Photon handoff are injected, because neither is where a
timestamp is computed. Everything that CAN move a cue time is real code here.
"""

from __future__ import annotations

import math
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import archive_db  # noqa: E402
from services import archive_transcribe as at  # noqa: E402
from services import photon_asr  # noqa: E402
from services.download_sidecars import transcript_sidecar  # noqa: E402


_PLATFORM = "twitch"
_VIDEO_ID = "999000111"
_MEDIA = "vod.mp4"

# The .srt cue line: "<start> --> <end>" with a comma-decimal HH:MM:SS,mmm.
_CUE_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})$"
)


# --- isolated archive DB (env-swapped, so archive_db reconnects) ----------


@pytest.fixture()
def _scratch_db(tmp_path, monkeypatch):
    """Per-test archive DB. conftest already points VODRIP_ARCHIVE_DB at a
    session temp; this narrows it to one file so a test's rows cannot reach
    the next one through the sidecar's read-back."""
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


# --- a recognizer stand-in: deterministic, per-clip, no model ---------------


def _tokens_for(words: list[tuple[str, float, float]]) -> tuple[list[str], list[float]]:
    """Word list -> (tokens, timestamps) in the shape _parakeet_words wants.

    A word is emitted as two pieces (" first", "rest") at its own start and
    end: the leading space opens the word, the second piece has none so it
    extends it, and _parakeet_words takes the word's end from the LAST piece's
    timestamp. So the word times come back out exactly as given."""
    tokens: list[str] = []
    stamps: list[float] = []
    for word, start, end in words:
        tokens.append(" " + word[:1])
        tokens.append(word[1:])
        stamps.append(start)
        stamps.append(end)
    return tokens, stamps


class _FakeResult:
    def __init__(self, text: str, tokens, timestamps):
        self.text = text
        self.tokens = tokens
        self.timestamps = timestamps
        self.ys_log_probs = None


class _FakeStream:
    def __init__(self, text: str, tokens, timestamps):
        self.samples = None
        self._result = _FakeResult(text, tokens, timestamps)

    def accept_waveform(self, sample_rate, samples) -> None:  # noqa: ARG002
        self.samples = samples

    @property
    def result(self) -> _FakeResult:
        return self._result


class _FakeRec:
    """sherpa-onnx recognizer stand-in.

    One entry per clip, handed out in create_stream() call order (which is clip
    order). A clip's entry is ``(text, words)``; an EMPTY words list models an
    engine that returned text with no token timestamps, which is what drives
    the end-of-clip clamp instead of the last word's end."""

    def __init__(self, per_clip: list[tuple[str, list[tuple[str, float, float]]]]):
        self._per_clip = list(per_clip)
        self._next = 0
        self.decode_streams_calls = 0

    def create_stream(self) -> _FakeStream:
        text, words = self._per_clip[self._next]
        self._next += 1
        tokens, stamps = _tokens_for(words)
        return _FakeStream(text, tokens, stamps)

    def decode_streams(self, streams) -> None:  # noqa: ARG002
        self.decode_streams_calls += 1

    def decode_stream(self, stream) -> None:  # noqa: ARG002
        raise AssertionError(
            "batch_size must be > 1: the per-call decode batch is the only "
            "path where clips SHARE a buffer, which is the only place an "
            "in-buffer position exists to be double-counted"
        )


# --- real sharded audio, as int16 shard files on disk ---------------------


def _sharded(tmp_path: Path, total_sec: float, shard_sec: float = 30.0) -> at._ShardedAudio:
    """Real _ShardedAudio over real int16 PCM shard files."""
    n = max(1, int(math.ceil(total_sec / shard_sec)))
    files = []
    for i in range(n):
        lo, hi = at._shard_sample_bounds(i, shard_sec)
        n_s = max(0, min(hi, int(total_sec * at.SAMPLE_RATE)) - lo)
        p = tmp_path / f"shard-{i:03d}.pcm"
        p.write_bytes((np.arange(n_s, dtype=np.int16) % 251).tobytes())
        files.append(p)
    return at._ShardedAudio(files, shard_sec)


# --- the chain, driven end to end -----------------------------------------


def _decode_and_write_srt(
    tmp_path: Path,
    sharded: at._ShardedAudio,
    run: list[tuple[int, tuple[float, float]]],
    per_clip: list[tuple[str, list[tuple[str, float, float]]]],
    engine: str,
) -> str:
    """sharded audio -> decode seam -> archive DB -> <stem>.srt. Returns the SRT.

    Every step below is production code. The only injections are the
    recognizer (sherpa) and the Photon handoff, neither of which computes a
    timestamp: both return CLIP-RELATIVE word times, and turning those into
    absolute video time is the thing under test.
    """
    batch_audio, concat_clips, clip_offsets = at._clips_to_audio(sharded, run)

    if engine == "sherpa":
        rec = _FakeRec(per_clip)
        batch_out = at._decode_batch(
            rec, batch_audio, concat_clips, "pt",
            clip_offsets=clip_offsets, batch_size=len(concat_clips), use_cuda=False,
        )
        assert rec.decode_streams_calls == 1, "the whole batch is one decode_streams call"
    elif engine == "photon":
        batch_out = at._decode_batch_photon(
            batch_audio, concat_clips, "pt", clip_offsets,
        )
    else:  # pragma: no cover - a typo in a test parameter
        raise AssertionError(f"unknown engine {engine!r}")

    assert len(batch_out) == len(run)

    # The real commit path: seg_idx allocation, the twin guard, and the real
    # archive_db.insert_transcript through the batched flush.
    state = at._ChunkTranscribeState(0, set())
    manifest = tmp_path / "manifest.jsonl"
    for (ci, _), (chunk_segs, detected) in zip(run, batch_out):
        at._commit_chunk_rows(
            state,
            platform=_PLATFORM,
            video_id=_VIDEO_ID,
            manifest=manifest,
            ci=ci,
            chunk_segs=chunk_segs,
            language="pt",
            detected=detected,
            engine="parakeet",
            fix_on=False,
            fix_stats={},
            chunk_span=4.0,
        )
    at._flush_transcript_batch_locked(state, _PLATFORM, _VIDEO_ID)
    assert not state.twin_won

    media = tmp_path / _MEDIA
    media.write_bytes(b"video")
    res = transcript_sidecar(str(media), _PLATFORM, _VIDEO_ID, formats=("srt",))
    assert res["status"] == "written", res
    assert res["source"] == "archive", res
    return Path(res["path"]).read_text(encoding="utf-8")


def _cues(body: str) -> list[tuple[str, str, str]]:
    """(start, end, text) per cue, in file order — the parsed artifact."""
    out: list[tuple[str, str, str]] = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        assert len(lines) == 3, f"malformed SRT block: {block!r}"
        m = _CUE_RE.match(lines[1])
        assert m, f"malformed cue line: {lines[1]!r}"
        out.append((m.group(1), m.group(2), lines[2]))
    return out


def _assert_no_cue_past(body: str, video_sec: float, limit: str) -> None:
    """No cue may extend past the end of the video it describes.

    Pre-fix, a cue whose read was clamped at the end of the sharded audio was
    stamped past the end of the video — a 60 s VOD got a cue running to
    00:01:02,000. Editors clamp or drop such a cue, so the tail of every long
    VOD's subtitle track was silently wrong."""
    for start, end, _text in _cues(body):
        assert end <= limit, (
            f"cue {start} --> {end} runs past the end of the {video_sec:g}s "
            f"video ({limit})"
        )


# --- 1. three clips, second and third NOT at offset 0 ---------------------


def test_srt_carries_absolute_times_for_a_three_clip_sharded_batch(
    _scratch_db, tmp_path: Path
):
    """The batch case, asserted on the file the user downloads.

    Three clips in ONE batch: absolute 10 s, 29 s and 100 s, which land at
    0.0 s, 4.0 s and 8.0 s inside the concatenated batch buffer. The middle
    clip straddles the 30 s shard boundary (it begins in shard 0 and ends in
    shard 1) — the case a monotonicity check cannot see and the one most
    likely to be missed.

    A two-clip batch would MISS this: with one earlier clip the error is one
    constant, which reads like a plausible delay. The third clip is what makes
    the pattern unmissable — the error GROWS with the position in the batch
    (4 s, then 8 s), which is the signature of an in-buffer position being
    added to an absolute start.

    KNOWN-CORRECT absolute times, not engine-vs-engine."""
    sharded = _sharded(tmp_path, 120.0)
    run = [(0, (10.0, 14.0)), (1, (29.0, 33.0)), (2, (100.0, 104.0))]
    per_clip = [
        ("um", [("um", 0.0, 0.4)]),
        ("dois", [("dois", 0.0, 0.4)]),
        ("tres", [("tres", 0.0, 0.4)]),
    ]

    body = _decode_and_write_srt(tmp_path, sharded, run, per_clip, "sherpa")

    assert _cues(body) == [
        ("00:00:10,000", "00:00:10,700", "um"),
        ("00:00:29,000", "00:00:29,700", "dois"),
        ("00:01:40,000", "00:01:40,700", "tres"),
    ], (
        "each cue must carry ITS OWN absolute video time. The clip's position "
        "in the batch buffer (0.0/4.0/8.0 s) is a slicing detail and must "
        "never appear in a timestamp: adding it is what stamped cue 2 four "
        "seconds late and cue 3 eight seconds late, silently, in this file."
    )
    # the shard-straddling clip's own bound is honoured exactly
    assert _cues(body)[1][0] == "00:00:29,000"
    _assert_no_cue_past(body, 120.0, "00:02:00,000")


def test_photon_batch_writes_the_same_absolute_times_into_the_srt(
    _scratch_db, tmp_path: Path, monkeypatch
):
    """The accelerator is pinned to the ARTIFACT, against the same known-good
    constants as the guaranteed engine.

    The Photon lane deliberately mirrored the double-count so the two engines
    agreed, and the fix changed both. Pinning one engine to the other passes
    while both are wrong the same way; this pins each to the file's expected
    cue times."""
    sharded = _sharded(tmp_path, 120.0)
    run = [(0, (10.0, 14.0)), (1, (29.0, 33.0)), (2, (100.0, 104.0))]

    def _fake_transcribe_batch(span, clips, *, sample_rate):  # noqa: ARG001
        return [
            {
                "language": "pt",
                "segments": [{
                    "text": text,
                    "start": 0.0,
                    "end": 0.4,
                    "words": [
                        {"word": w, "start": s, "end": e, "probability": 0.9}
                        for w, s, e in words
                    ],
                }],
            }
            for text, words in [
                ("um", [("um", 0.0, 0.4)]),
                ("dois", [("dois", 0.0, 0.4)]),
                ("tres", [("tres", 0.0, 0.4)]),
            ]
        ][:len(clips)]

    monkeypatch.setattr(photon_asr, "transcribe_batch", _fake_transcribe_batch)

    photon_body = _decode_and_write_srt(tmp_path, sharded, run, [], "photon")

    assert _cues(photon_body) == [
        ("00:00:10,000", "00:00:10,700", "um"),
        ("00:00:29,000", "00:00:29,700", "dois"),
        ("00:01:40,000", "00:01:40,700", "tres"),
    ], "Photon must write the same KNOWN-CORRECT absolute cue times as sherpa-onnx"

    # ... and the two engines are byte-identical in the file they produce
    # (a full re-transcribe replaces the rows; seg_idx restarts at 0)
    archive_db.delete_transcripts(_PLATFORM, _VIDEO_ID)
    sherpa_body = _decode_and_write_srt(tmp_path, sharded, run, [
        ("um", [("um", 0.0, 0.4)]),
        ("dois", [("dois", 0.0, 0.4)]),
        ("tres", [("tres", 0.0, 0.4)]),
    ], "sherpa")
    assert photon_body == sherpa_body, (
        "a Photon batch must be indistinguishable downstream from a sherpa "
        "batch; a one-sided fix makes the two SRTs differ"
    )


# --- 2. the straddling cue: one of the two real failures -------------------


def test_srt_cue_straddling_the_end_of_the_audio_never_passes_the_video_end(
    _scratch_db, tmp_path: Path
):
    """A cue whose window runs past the end of the audio.

    The VOD is 60 s. The second clip is REQUESTED as 58-64 s, but the sharded
    read clamps at 60 s, so the engine only ever HEARD 58-60 s. The cue must
    therefore run 58 s -> 60 s: starting where it really starts, and ending
    where the audio it was decoded from actually ends.

    This is the direction the real bug took: pre-fix the cue was stamped
    00:01:00,000 --> 00:01:02,000 — it started at the very end of the video and
    ran two seconds PAST it. No word timestamps are supplied here, so the cue's
    end is the clip's own decoded end: that is what makes the clamp, rather
    than the last word, the thing being measured."""
    sharded = _sharded(tmp_path, 60.0)
    run = [(0, (10.0, 12.0)), (1, (58.0, 64.0))]
    per_clip = [("um", []), ("fim", [])]

    body = _decode_and_write_srt(tmp_path, sharded, run, per_clip, "sherpa")

    assert _cues(body) == [
        ("00:00:10,000", "00:00:12,000", "um"),
        ("00:00:58,000", "00:01:00,000", "fim"),
    ], (
        "the straddling cue must start at its true absolute start (58 s) and "
        "end where the audio actually decoded ends (60 s) — not at 60 s -> 62 s, "
        "which is the double-counted stamp this shipped"
    )
    _assert_no_cue_past(body, 60.0, "00:01:00,000")


def test_photon_straddling_cue_lands_on_the_same_known_times_in_the_srt(
    _scratch_db, tmp_path: Path, monkeypatch
):
    """The same straddling clamp on the accelerator, pinned to the same
    constants — the end-of-audio bound is arithmetic both engines share."""
    sharded = _sharded(tmp_path, 60.0)
    run = [(0, (10.0, 12.0)), (1, (58.0, 64.0))]

    def _fake_transcribe_batch(span, clips, *, sample_rate):  # noqa: ARG001
        # no `words` key -> Photon falls back to the clip's own end, which is
        # the same branch the sherpa stand-in takes with no token timestamps
        return [
            {"language": "pt", "segments": [{"text": t, "start": 0.0, "end": 0.0}]}
            for t in ("um", "fim")
        ][:len(clips)]

    monkeypatch.setattr(photon_asr, "transcribe_batch", _fake_transcribe_batch)

    body = _decode_and_write_srt(tmp_path, sharded, run, [], "photon")

    assert _cues(body) == [
        ("00:00:10,000", "00:00:12,000", "um"),
        ("00:00:58,000", "00:01:00,000", "fim"),
    ], "Photon must clamp the straddling cue to the audio it decoded, too"
    _assert_no_cue_past(body, 60.0, "00:01:00,000")
