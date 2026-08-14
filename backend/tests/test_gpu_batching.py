"""GPU batching tests — real-GPU detection, VRAM-derived batch size,
decode_streams batching (window order preserved), and the sequential GPU
gate. All engines mocked: no model load, no inference, no downloads, no
GPU interaction (the probes are patched)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import archive_transcribe as at  # noqa: E402

GIB = 1024 ** 3


class _FakeResult:
    def __init__(self, text: str):
        self.text = text
        self.tokens = ()
        self.timestamps = ()
        self.ys_log_probs = None


class _FakeStream:
    """OfflineStream stand-in: stores the accepted waveform, returns a canned result."""

    def __init__(self, text: str):
        self.samples = None
        self._text = text

    def accept_waveform(self, sample_rate: int, samples) -> None:  # noqa: ARG002
        self.samples = samples

    @property
    def result(self) -> _FakeResult:
        return _FakeResult(self._text)


class _FakeRec:
    """OfflineRecognizer stand-in recording decode_stream / decode_streams calls."""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.decode_streams_calls: list[list] = []
        self.decode_stream_calls: list = []
        self._i = 0

    def create_stream(self) -> _FakeStream:
        stream = _FakeStream(self.texts[self._i % len(self.texts)])
        self._i += 1
        return stream

    def decode_streams(self, streams) -> None:
        self.decode_streams_calls.append(list(streams))

    def decode_stream(self, stream) -> None:
        self.decode_stream_calls.append(stream)


# --- part 1: real-GPU detection (rejects a fake adapter) -------------------

def test_real_gpu_detection_rejects_fake_adapter(monkeypatch):
    """A Virtual Display Driver (no nvidia-smi, no CUDA runtime device) must
    read as NO GPU — compute probes are the truth, never adapter names."""
    monkeypatch.setattr(at, "_nvidia_smi_vram", lambda: None)
    monkeypatch.setattr(at, "_cuda_runtime_vram", lambda: None)
    assert at._real_gpu_info() == (False, 0, 0), (
        "a fake adapter with no compute probe must not look like a GPU"
    )
    # Whisper device detection must also resolve to CPU on the fake adapter.
    at._detect_device.cache_clear()
    try:
        monkeypatch.setenv("VODRIP_WHISPER_DEVICE", "")
        assert at._detect_device() == ("cpu", "int8")
    finally:
        at._detect_device.cache_clear()  # never leak a patched-probe verdict


def test_real_gpu_detection_requires_vram_and_compute(monkeypatch):
    """A real probe (nvidia-smi) reports present; sub-1 GiB total is not a
    compute GPU; the CUDA-runtime fallback also confirms presence."""
    monkeypatch.setattr(at, "_nvidia_smi_vram", lambda: (8 * GIB, 4 * GIB))
    monkeypatch.setattr(at, "_cuda_runtime_vram", lambda: None)
    assert at._real_gpu_info() == (True, 8 * GIB, 4 * GIB)
    monkeypatch.setattr(at, "_nvidia_smi_vram", lambda: (512 * 1024 ** 2, 400 * 1024 ** 2))
    assert at._real_gpu_info() == (False, 0, 0), "sub-1 GiB total is not a compute GPU"
    monkeypatch.setattr(at, "_nvidia_smi_vram", lambda: None)
    monkeypatch.setattr(at, "_cuda_runtime_vram", lambda: (16 * GIB, 12 * GIB))
    assert at._real_gpu_info() == (True, 16 * GIB, 12 * GIB), (
        "the CUDA runtime probe is a valid presence source"
    )


def test_parakeet_cuda_wheel_requires_real_gpu(monkeypatch):
    """The +cuda wheel tag alone must NOT enable the parakeet GPU lane on a
    fake adapter — the real-GPU compute probe is ANDed in."""
    import sherpa_onnx

    monkeypatch.setattr(sherpa_onnx, "__version__", "1.13.4+cuda12.cudnn9")
    monkeypatch.setattr(at, "_parakeet_ok", True)
    monkeypatch.setattr(at, "_parakeet_cuda_ok", None)
    monkeypatch.setattr(at, "_nvidia_smi_vram", lambda: None)
    monkeypatch.setattr(at, "_cuda_runtime_vram", lambda: None)
    assert at._parakeet_cuda_available() is False, (
        "a CUDA wheel on a fake adapter must not enable the GPU lane"
    )


# --- part 2: VRAM-derived batch size ---------------------------------------

def _fresh_vram(free_bytes: int) -> None:
    at._vram_free_bytes = int(free_bytes)
    at._vram_free_at = time.monotonic()


def test_batch_size_respects_free_vram_minus_reservation(monkeypatch):
    """batch = (free - caption reservation - model - safety) // per-window,
    clamped [1, 32]; unknown free VRAM stays sequential."""
    monkeypatch.setattr(at, "_parakeet_provider", lambda: "cuda")
    monkeypatch.delenv(at.PARAKEET_BATCH_ENV, raising=False)
    saved = (at._vram_free_bytes, at._vram_free_at)
    try:
        _fresh_vram(8 * GIB)
        expected = (
            8 * GIB - at._PARAKEET_GPU_VRAM_EST - at._PARAAKEET_BATCH_VRAM_SAFETY
        ) // at._PARAAKEET_WINDOW_VRAM_EST
        assert at._parakeet_batch_size() == max(1, min(expected, at._PARAAKEET_BATCH_MAX))
        # caption reservation shrinks the budget (the seam reads their hook)
        monkeypatch.setattr(at, "caption_reserved_vram_bytes", lambda: 4 * GIB, raising=False)
        expected = (
            8 * GIB - 4 * GIB - at._PARAKEET_GPU_VRAM_EST - at._PARAAKEET_BATCH_VRAM_SAFETY
        ) // at._PARAAKEET_WINDOW_VRAM_EST
        assert at._parakeet_batch_size() == max(1, min(expected, at._PARAAKEET_BATCH_MAX))
        assert at._caption_reserved_vram_bytes() == 4 * GIB, (
            "the seam must surface the caption worker's reservation"
        )
        # a huge reservation floors the batch at 1 (never negative)
        monkeypatch.setattr(at, "caption_reserved_vram_bytes", lambda: 64 * GIB, raising=False)
        assert at._parakeet_batch_size() == 1
        # unknown free VRAM -> sequential (never gamble a batch we cannot size)
        monkeypatch.setattr(at, "caption_reserved_vram_bytes", lambda: 0)
        _fresh_vram(0)
        assert at._parakeet_batch_size() == 1
        # env cap wins over the VRAM estimate
        monkeypatch.setenv(at.PARAKEET_BATCH_ENV, "4")
        _fresh_vram(16 * GIB)
        assert at._parakeet_batch_size() == 4
    finally:
        at._vram_free_bytes, at._vram_free_at = saved


def test_batch_size_cpu_provider_sequential(monkeypatch):
    """The CPU provider keeps batch 1 — the A/B-measured sequential path."""
    monkeypatch.setattr(at, "_parakeet_provider", lambda: "cpu")
    _fresh_vram(16 * GIB)
    assert at._parakeet_batch_size() == 1


def test_caption_seam_defaults_to_zero(monkeypatch):
    """Absent caption hooks (not merged) read as 0 reservation / inactive."""
    monkeypatch.delattr(at, "caption_reserved_vram_bytes", raising=False)
    monkeypatch.delattr(at, "caption_session_active", raising=False)
    assert at._caption_reserved_vram_bytes() == 0
    assert at._caption_session_active() is False


# --- part 2: decode_streams batching (order preserved) ---------------------

def test_decode_streams_batched_order_preserved():
    """batch_size > 1 decodes via decode_streams in windows of the SAME
    video, results map back to the input window order (monotonic
    timestamps), and clip_offsets (sharded concat path) are applied."""
    rec = _FakeRec(["alpha", "beta", "gamma", "delta", "epsilon"])
    audio = [0.0] * int(50 * at.SAMPLE_RATE)
    chunks = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0), (40.0, 50.0)]
    out = at._transcribe_batch_parakeet(rec, audio, chunks, None, batch_size=2)
    assert [o[0][0]["text"] for o in out] == ["alpha", "beta", "gamma", "delta", "epsilon"], (
        "results must map 1:1 to the input window order"
    )
    assert [len(c) for c in rec.decode_streams_calls] == [2, 2, 1], (
        "windows must be buffered up to the batch size, then flushed"
    )
    # each stream carries its own window's samples (input order preserved)
    assert [len(s.samples) for c in rec.decode_streams_calls for s in c] == [
        int(10 * at.SAMPLE_RATE)] * 5
    # absolute timestamps stay monotonic and per-window
    assert [o[0][0]["start_sec"] for o in out] == [0.0, 10.0, 20.0, 30.0, 40.0]

    # sharded path: concat-relative clips + absolute offsets
    offsets = [100.0, 200.0, 300.0, 400.0, 500.0]
    out2 = at._transcribe_batch_parakeet(
        _FakeRec(["a", "b", "c"]), audio, chunks, "pt",
        clip_offsets=offsets, batch_size=3,
    )
    assert [o[0][0]["start_sec"] for o in out2] == [100.0, 210.0, 320.0, 430.0, 540.0], (
        "clip offsets must shift each window to its absolute video position"
    )
    assert all(o[1] == "pt" for o in out2)


def test_decode_streams_sequential_default():
    """batch_size 1 keeps the legacy per-stream decode loop (CPU path)."""
    rec = _FakeRec(["a", "b"])
    audio = [0.0] * int(30 * at.SAMPLE_RATE)
    out = at._transcribe_batch_parakeet(rec, audio, [(0.0, 10.0), (10.0, 30.0)], None)
    assert len(rec.decode_stream_calls) == 2 and not rec.decode_streams_calls
    assert [o[0][0]["text"] for o in out] == ["a", "b"]


# --- part 3: sequential GPU dispatch gate ----------------------------------

def test_gpu_gate_prevents_second_video():
    """One video at a time on the GPU; release frees the gate for the next.
    The gate is a pure claim — CPU lanes never consult it (their jobs are
    not transcribe-on-cuda, so _process_job never calls acquire for them)."""
    assert at._gpu_gate_held() is False
    assert at._gpu_gate_try_acquire("twitch", "v1") is True
    assert at._gpu_gate_held() is True
    assert at._gpu_gate_try_acquire("twitch", "v2") is False, (
        "a second video must not stack on the GPU"
    )
    assert at._gpu_gate_try_acquire("kick", "v3") is False, (
        "a different platform is blocked too"
    )
    assert at._gpu_gate_try_acquire("twitch", "v1") is True, (
        "the holder re-acquires its own video"
    )
    at._gpu_gate_release("twitch", "v1")
    assert at._gpu_gate_held() is False
    assert at._gpu_gate_try_acquire("kick", "v2") is True, (
        "release must free the gate for the next video"
    )
    at._gpu_gate_release("kick", "v2")


def test_gpu_gate_blocks_during_caption_session(monkeypatch):
    """A live-caption session (their hook) blocks new GPU dispatch."""
    monkeypatch.setattr(at, "caption_session_active", lambda: True, raising=False)
    assert at._gpu_gate_try_acquire("twitch", "v1") is False, (
        "no GPU dispatch while the caption session holds the GPU"
    )
    monkeypatch.setattr(at, "caption_session_active", lambda: False, raising=False)
    assert at._gpu_gate_try_acquire("twitch", "v1") is True
    at._gpu_gate_release("twitch", "v1")


def test_gpu_pinned_job_requeues_when_gate_held():
    """A GPU-pinned transcribe thread finding another video active releases
    its claim (requeue with backoff); a CPU-pinned thread runs past the
    gate (CPU lanes unaffected). Uses the scratch archive DB only."""
    from services import archive_db

    saved_pin = getattr(at._multi_tls, "pin", None)
    saved_video = at._gpu_gate_video
    try:
        archive_db.enqueue_job("gpu-gate-1", "transcribe", "twitch", "gg-v1")
        archive_db.enqueue_job("gpu-gate-2", "transcribe", "twitch", "gg-v2")
        job2 = at._claim_next_job()  # newest first — either row, both queued
        while job2 is not None and (job2["id"], job2["video_id"]) != ("gpu-gate-2", "gg-v2"):
            job2 = at._claim_next_job()
        assert job2 is not None, "gpu-gate-2 must be claimable"

        # GPU pin + gate held by v1 -> the v2 claim is released with backoff.
        at._multi_tls.pin = ("cuda", "float16")
        assert at._gpu_gate_try_acquire("twitch", "gg-v1") is True
        res = at._process_job(job2, multi=True)
        assert res.get("requeued") == "gpu-gate", res
        row = archive_db.query(
            "SELECT status, next_retry_at FROM archive_jobs WHERE id = ?",
            ("gpu-gate-2",),
        )[0]
        assert row["status"] == "queued" and row["next_retry_at"] is not None, (
            "the gated claim must requeue with a backoff deadline"
        )

        # CPU pin + gate still held -> the job runs past the gate (it fails
        # on the missing archive row, never gated).
        at._multi_tls.pin = ("cpu", "int8")
        res2 = at._process_job(job2, multi=True)
        assert res2.get("requeued") != "gpu-gate", (
            "CPU lanes must bypass the GPU sequential gate"
        )
        row2 = archive_db.query(
            "SELECT status, error FROM archive_jobs WHERE id = ?", ("gpu-gate-2",),
        )[0]
        assert row2["status"] == "failed" and "GPU sequential gate" not in (row2["error"] or ""), (
            "the CPU lane must have run the job, not gated it"
        )
    finally:
        at._multi_tls.pin = saved_pin
        at._gpu_gate_video = saved_video
