"""Sharded batch clip timestamps — absolute video time, BOTH engines.

The multi-window sharded decode path concatenates a batch's clips into one
buffer so the engine can be fed a whole batch in a single call. A clip's
position in that buffer is NOT video time: it exists only so the caller can
slice audio back out of it. The clip's real start travels alongside, in
clip_offsets. The two describe the SAME instant, so they are alternatives and
not summands — adding them puts every clip after the first in a batch that
many seconds late, silently, in the SRT the user downloads.

Two properties are pinned here, deliberately, because either alone is weak:

  * ABSOLUTE timestamps, as exact numbers. A monotonicity/ordering assertion
    passes against a bug that adds a constant offset, which is exactly this
    bug; only the absolute value can catch it.
  * THE TWO ENGINES AGAINST EACH OTHER. The Photon accelerator mirrors the
    sherpa-onnx path so a Photon batch is indistinguishable downstream, so a
    regression test that pins one engine to a constant is worth less than one
    that pins them to each other.

No model load, no inference, no GPU, no network: the recognizer and the Photon
handoff are injected; the sharded audio is real (real int16 shard files, real
absolute reads, real clamping).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import archive_transcribe as at  # noqa: E402
from services import photon_asr  # noqa: E402


# --- a recognizer stand-in with deterministic word timestamps ---------------

# tokens/timestamps produce two words, clip-relative:
#   despous  0.0 -> 0.4      ola  0.6 -> 0.6
# (word end is the last token's start, per _parakeet_words)
_TOKENS = [" desp", "ous", " ola"]
_TIMESTAMPS = [0.0, 0.4, 0.6]
_TEXT = "despous ola"
_LAST_WORD_END = 0.6


class _FakeResult:
    def __init__(self, text: str, tokens, timestamps):
        self.text = text
        self.tokens = tokens
        self.timestamps = timestamps
        self.ys_log_probs = None


class _FakeStream:
    def __init__(self, text: str, tokens, timestamps):
        self.samples = None
        self._r = _FakeResult(text, tokens, timestamps)

    def accept_waveform(self, sample_rate, samples) -> None:  # noqa: ARG002
        self.samples = samples

    @property
    def result(self) -> _FakeResult:
        return self._r


class _FakeRec:
    """sherpa-onnx recognizer stand-in. ``with_words=False`` returns text with
    no token timestamps, which drives the end-of-clip clamp instead of the
    last word's end — the branch that pins the clip's decoded end."""

    def __init__(self, with_words: bool = True):
        self.tokens = list(_TOKENS) if with_words else []
        self.timestamps = list(_TIMESTAMPS) if with_words else []
        self.decode_stream_calls = 0
        self.decode_streams_calls = 0

    def create_stream(self):
        return _FakeStream(_TEXT, self.tokens, self.timestamps)

    def decode_streams(self, streams) -> None:  # noqa: ARG002
        self.decode_streams_calls += 1

    def decode_stream(self, stream) -> None:  # noqa: ARG002
        self.decode_stream_calls += 1


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


def _run_sherpa(sharded, run, *, batch_size=4, with_words=True):
    """The production sharded batch: _clips_to_audio -> the decode seam."""
    audio, clips, offsets = at._clips_to_audio(sharded, run)
    rec = _FakeRec(with_words=with_words)
    out = at._transcribe_batch_parakeet(
        rec, audio, clips, "pt", clip_offsets=offsets, batch_size=batch_size,
    )
    return out, rec


def _photon_stub(monkeypatch):
    """Photon answers every clip with the same two clip-relative words that
    the sherpa stand-in above produces, so the two engines are comparable
    word for word. Photon word times are relative to the clip it was given."""
    def _fake_transcribe_batch(span, clips, *, sample_rate):  # noqa: ARG001
        assert len(clips) == 1 or len(clips) >= 1
        return [
            {
                "language": "pt",
                "segments": [{
                    "text": _TEXT,
                    "start": 0.0,
                    "end": _LAST_WORD_END,
                    "words": [
                        {"word": "despous", "start": 0.0, "end": 0.4, "probability": 0.9},
                        {"word": "ola", "start": 0.6, "end": 0.6, "probability": 0.8},
                    ],
                }],
            }
            for _ in clips
        ]

    monkeypatch.setattr(photon_asr, "transcribe_batch", _fake_transcribe_batch)


def _times(out):
    """The timestamp contract of a decode result, engine-agnostic."""
    return [
        (seg["start_sec"], seg["end_sec"],
         [(w["word"], w["start"], w["end"]) for w in seg["words"]])
        for segs, _ in out for seg in segs
    ]


# --- 1. the core arithmetic: absolute start of every clip in a batch --------

def test_sharded_batch_clips_land_on_their_own_absolute_start(tmp_path):
    """Three clips in one batch. The middle one straddles the 30 s shard
    boundary (it begins in shard 0 and ends in shard 1) — the case a
    monotonicity assertion cannot see and the one most likely to be missed.

    Expected: each cue starts at ITS OWN absolute video time. The in-buffer
    position of a clip (0.0, 4.0, 8.0 here) is a slicing detail and must
    never appear in a timestamp."""
    sharded = _sharded(tmp_path, 120.0)
    run = [(0, (10.0, 14.0)), (1, (29.0, 33.0)), (2, (100.0, 104.0))]
    out, _ = _run_sherpa(sharded, run)

    assert [segs[0]["start_sec"] for segs, _ in out] == [10.0, 29.0, 100.0], (
        "each clip must start at its own absolute video position; the "
        "in-buffer position is not video time"
    )
    # end = clip start + last word end + 0.3, clamped to the clip's own end
    assert [segs[0]["end_sec"] for segs, _ in out] == [10.9, 29.9, 100.9]
    assert _times(out) == [
        (10.0, 10.9, [("despous", 10.0, 10.4), ("ola", 10.6, 10.6)]),
        (29.0, 29.9, [("despous", 29.0, 29.4), ("ola", 29.6, 29.6)]),
        (100.0, 100.9, [("despous", 100.0, 100.4), ("ola", 100.6, 100.6)]),
    ], "word timestamps are absolute video time too, not clip-local"


def test_sequential_cpu_path_shares_the_arithmetic(tmp_path):
    """batch_size 1 (the CPU slot) walks the same arithmetic through the other
    loop; a fix that only touched the batch>1 branch would leave the two
    disagreeing."""
    sharded = _sharded(tmp_path, 120.0)
    run = [(0, (10.0, 14.0)), (1, (29.0, 33.0)), (2, (100.0, 104.0))]
    out, rec = _run_sherpa(sharded, run, batch_size=1)

    assert rec.decode_stream_calls == 3 and not rec.decode_streams_calls
    assert [segs[0]["start_sec"] for segs, _ in out] == [10.0, 29.0, 100.0]


# --- 2. a cue that straddles the end of the decoded audio -------------------

def test_cue_straddling_the_end_of_the_audio_is_clamped_to_it(tmp_path):
    """The second clip is requested as 58-64 s but the sharded audio is only
    60 s long, so the read clamps and the engine only ever HEARD 58-60 s.

    A cue must never be stamped past the end of the audio it was decoded
    from — and it must still start where it really starts."""
    sharded = _sharded(tmp_path, 60.0)
    run = [(0, (10.0, 12.0)), (1, (58.0, 64.0))]
    # No token timestamps -> end_sec is the clip's own decoded end, so the
    # clamp is what this assertion actually measures.
    out, _ = _run_sherpa(sharded, run, with_words=False)

    starts = [segs[0]["start_sec"] for segs, _ in out]
    ends = [segs[0]["end_sec"] for segs, _ in out]
    assert starts == [10.0, 58.0], (
        "a clamped read does not move the cue's start off its true position"
    )
    assert ends == [12.0, 60.0], (
        "end_sec is bounded by the audio actually decoded (60 s), not by the "
        "requested window (64 s) and not by a double-counted offset"
    )
    assert all(e <= 60.0 for e in ends), "no cue may land past the end of the audio"


# --- 3. the two engines must not diverge ------------------------------------

def test_photon_and_sherpa_agree_on_a_sharded_batch(tmp_path, monkeypatch):
    """The accelerator mirrors the guaranteed engine. Same batch, same audio,
    same expected timestamps from both — asserted against each other AND
    against the absolute numbers, so neither engine alone is the reference."""
    sharded = _sharded(tmp_path, 120.0)
    run = [(0, (10.0, 14.0)), (1, (29.0, 33.0)), (2, (100.0, 104.0))]

    sherpa_out, _ = _run_sherpa(sharded, run)

    audio, clips, offsets = at._clips_to_audio(sharded, run)
    _photon_stub(monkeypatch)
    photon_out = at._decode_batch_photon(audio, clips, "pt", offsets)

    assert _times(photon_out) == _times(sherpa_out), (
        "Photon and sherpa-onnx must produce identical timestamps for the "
        "same batch; a one-sided fix makes them diverge"
    )
    assert _times(sherpa_out) == [
        (10.0, 10.9, [("despous", 10.0, 10.4), ("ola", 10.6, 10.6)]),
        (29.0, 29.9, [("despous", 29.0, 29.4), ("ola", 29.6, 29.6)]),
        (100.0, 100.9, [("despous", 100.0, 100.4), ("ola", 100.6, 100.6)]),
    ]
    assert [lang for _, lang in photon_out] == ["pt", "pt", "pt"]


def test_photon_straddling_cue_is_clamped_too(tmp_path, monkeypatch):
    """The straddle clamp is the same arithmetic in both engines, so it is
    asserted in both."""
    sharded = _sharded(tmp_path, 60.0)
    run = [(0, (10.0, 12.0)), (1, (58.0, 64.0))]
    audio, clips, offsets = at._clips_to_audio(sharded, run)
    _photon_stub(monkeypatch)
    out = at._decode_batch_photon(audio, clips, "pt", offsets)

    assert [(s["start_sec"], s["end_sec"]) for segs, _ in out for s in segs] == [
        (10.0, 10.9), (58.0, 58.9),
    ]


# --- 4. the output contract is unchanged ------------------------------------

def test_segment_shape_and_full_audio_path_unchanged(tmp_path):
    """Same JSON shape as before (start_sec/end_sec/text/words), and the
    full-audio path — where cs IS absolute and no offsets are supplied —
    keeps its byte-identical behaviour."""
    sharded = _sharded(tmp_path, 120.0)
    run = [(0, (10.0, 14.0)), (1, (100.0, 104.0))]
    out, _ = _run_sherpa(sharded, run)
    seg = out[0][0][0]
    assert set(seg) == {"start_sec", "end_sec", "text", "words"}
    assert seg["text"] == _TEXT
    assert [set(w) for w in seg["words"]] == [{"word", "start", "end"}] * 2

    # full-audio path: no clip_offsets, chunks already absolute
    audio = np.zeros(int(120 * at.SAMPLE_RATE), dtype=np.float32)
    full_out = at._transcribe_batch_parakeet(
        _FakeRec(), audio, [(10.0, 14.0), (100.0, 104.0)], "pt", batch_size=2,
    )
    assert [segs[0]["start_sec"] for segs, _ in full_out] == [10.0, 100.0]
    assert [segs[0]["end_sec"] for segs, _ in full_out] == [10.9, 100.9]
