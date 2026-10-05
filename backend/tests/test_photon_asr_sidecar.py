"""Photon GPU accelerator — sidecar protocol and every fallback path.

NO GPU AND NO REAL VENV: the sidecar interpreter and the runner script are both
injected, so these tests exercise the production client (photon_asr.run_sidecar
and archive_transcribe._decode_batch) against fake sidecars that speak the real
protocol. A GPU benchmark here would prove nothing the probe lane has not
already measured, and would contend for a shared card.

The hang tests are the point of the module: they assert the child process is
GONE after the timeout, not merely that a timeout was raised. An orphan left
burning ~2 GB of VRAM is the bug being fixed.
"""
from __future__ import annotations

import json
import os
import subprocess as sp
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from services import archive_transcribe as at
from services import photon_asr


# --- process liveness (no psutil dependency, no os.kill(pid, 0) on Windows) --

def _process_alive(pid: int) -> bool:
    """True while the pid names a live process."""
    return not photon_asr._process_gone(pid)


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_alive(pid)


# --- fake sidecars: the REAL protocol, without a GPU ----------------------

_OK_SEGMENTS = [
    {
        "text": "hello world",
        "start": 0.0,
        "end": 1.2,
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.5, "probability": 0.99},
            {"word": "world", "start": 0.5, "end": 1.2, "probability": 0.97},
        ],
    },
]

_FAKE_OK = """
import json, sys
req = json.load(open(sys.argv[1]))
json.dump({"ok": True, "clips": [
    {"language": "en", "segments": %(segs)s} for _ in req["clips"]
]}, sys.stdout)
sys.stdout.write("\\n")
"""

# Reads the request, then never returns — the measured Photon failure mode
# (0%% CPU, frozen resident set, no exception, no output).
_FAKE_HANG = """
import json, sys, time
json.load(open(sys.argv[1]))
time.sleep(3600)
"""

_FAKE_GARBAGE = """
import sys
sys.stdout.write("this is not json at all\\n")
"""

_FAKE_EMPTY = """
import json, sys
req = json.load(open(sys.argv[1]))
json.dump({"ok": True, "clips": [
    {"language": None, "segments": []} for _ in req["clips"]
]}, sys.stdout)
sys.stdout.write("\\n")
"""

_FAKE_CRASH = """
import sys
sys.stderr.write("CUDA error: out of memory\\n")
sys.exit(3)
"""

_FAKE_SHORT = """
import json, sys
req = json.load(open(sys.argv[1]))
json.dump({"ok": True, "clips": []}, sys.stdout)
sys.stdout.write("\\n")
"""

def _fake(tmp_path: Path, name: str, body: str, *, segs: str = None) -> Path:
    src = tmp_path / f"{name}.py"
    if segs is not None:
        body = body % {"segs": json.dumps(segs)}
    src.write_text(body, encoding="utf-8")
    return src


def _audio(sec: float = 2.0) -> np.ndarray:
    return np.zeros(int(sec * 16000), dtype=np.float32)


# --- availability ---------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    """The guaranteed path is the default: no env var means no accelerator."""
    monkeypatch.delenv(photon_asr.ENABLED_ENV, raising=False)
    assert photon_asr.enabled() is False
    assert photon_asr.available() is False
    assert "VODRIP_PHOTON_ASR=0" in photon_asr.unavailable_reason()


def test_enabled_but_missing_venv_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, str(tmp_path / "nope" / "python.exe"))
    assert photon_asr.python_path() is None
    assert photon_asr.available() is False
    assert "interpreter" in photon_asr.unavailable_reason()


def test_enabled_with_interpreter_and_runner_is_available(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    assert photon_asr.available() is True
    assert photon_asr.unavailable_reason() == ""


# --- the protocol ---------------------------------------------------------

def test_sidecar_ok_returns_one_entry_per_clip(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    out = photon_asr.run_sidecar(
        pcm=_audio(4.0),
        clips=[(0.0, 2.0), (2.0, 4.0)],
        sample_rate=16000,
        python=Path(sys.executable),
        runner=runner,
        timeout_s=60.0,
    )
    assert len(out) == 2
    assert out[0]["language"] == "en"
    assert out[0]["segments"][0]["text"] == "hello world"


def test_real_scratch_dir_is_writable():
    """The real scratch root resolves and accepts a write (AGENTS.md: heavy
    data goes on the stable model drive, not C: and not G:\\Temp)."""
    root = photon_asr._scratch_dir()
    assert root.is_dir(), f"scratch root is not a directory: {root}"
    probe = Path(root) / ".writable-probe"
    probe.write_text("ok", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "ok"
    probe.unlink()


def test_real_runner_reports_missing_photon_cleanly(tmp_path):
    """The shipped runner must answer with a failure ENVELOPE, not an import
    traceback on stderr, when the venv has no moondream. Run it under the test
    interpreter, which has no moondream - the same shape as a broken venv."""
    req = tmp_path / "req.json"
    pcm = tmp_path / "pcm.f32"
    _audio(1.0).tofile(str(pcm))
    req.write_text(json.dumps({
        "model": "moondream/parakeet-redux",
        "sample_rate": 16000,
        "timestamps": "word",
        "pcm_path": str(pcm),
        "clips": [{"start": 0.0, "end": 1.0}],
    }), encoding="utf-8")
    proc = sp.run(
        [sys.executable, str(at.Path(photon_asr.runner_path())), str(req)],
        capture_output=True, timeout=120, creationflags=at._NO_WINDOW,
    )
    assert proc.returncode != 0
    payload = json.loads(proc.stdout.decode("utf-8", "replace").strip())
    assert payload["ok"] is False
    assert "photon import failed" in payload["error"]


# --- failure paths --------------------------------------------------------

def test_missing_sidecar_raises_unavailable(tmp_path):
    with pytest.raises(photon_asr.PhotonUnavailable):
        photon_asr.run_sidecar(
            pcm=_audio(1.0), clips=[(0.0, 1.0)], sample_rate=16000,
            python=Path(sys.executable), runner=tmp_path / "does-not-exist.py",
            timeout_s=10.0,
        )


def test_crash_raises_unavailable(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "crash", _FAKE_CRASH)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    with pytest.raises(photon_asr.PhotonUnavailable):
        photon_asr.run_sidecar(
            pcm=_audio(1.0), clips=[(0.0, 1.0)], sample_rate=16000,
            python=Path(sys.executable), runner=runner, timeout_s=60.0,
        )


def test_garbage_response_raises_protocol_error(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "garbage", _FAKE_GARBAGE)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    with pytest.raises(photon_asr.PhotonProtocolError):
        photon_asr.run_sidecar(
            pcm=_audio(1.0), clips=[(0.0, 1.0)], sample_rate=16000,
            python=Path(sys.executable), runner=runner, timeout_s=60.0,
        )


def test_empty_transcript_for_speech_raises_protocol_error(monkeypatch, tmp_path):
    """Empty is a FAILURE here, not silence: the clips are VAD speech regions,
    so a whole batch with nothing in it means the accelerator is broken."""
    runner = _fake(tmp_path, "empty", _FAKE_EMPTY)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    with pytest.raises(photon_asr.PhotonProtocolError) as exc:
        photon_asr.run_sidecar(
            pcm=_audio(2.0), clips=[(0.0, 2.0)], sample_rate=16000,
            python=Path(sys.executable), runner=runner, timeout_s=60.0,
        )
    assert "no segments" in str(exc.value)


def test_clip_count_mismatch_raises_protocol_error(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "short", _FAKE_SHORT)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    with pytest.raises(photon_asr.PhotonProtocolError) as exc:
        photon_asr.run_sidecar(
            pcm=_audio(2.0), clips=[(0.0, 1.0), (1.0, 2.0)], sample_rate=16000,
            python=Path(sys.executable), runner=runner, timeout_s=60.0,
        )
    assert "clips for" in str(exc.value)


# --- THE hang: bounded in time AND actually killed ------------------------

def test_hang_times_out_and_the_process_is_gone(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "hang", _FAKE_HANG)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    t0 = time.monotonic()
    with pytest.raises(photon_asr.PhotonTimeout) as exc:
        photon_asr.run_sidecar(
            pcm=_audio(1.0), clips=[(0.0, 1.0)], sample_rate=16000,
            python=Path(sys.executable), runner=runner, timeout_s=2.0,
        )
    elapsed = time.monotonic() - t0
    pid = exc.value.pid
    assert pid, "the timeout must report the pid it killed"
    # Bounded in wall time...
    assert 1.5 <= elapsed < 25.0, f"timeout did not bound the hang: {elapsed:.1f}s"
    # ...and the process is ACTUALLY gone. This is the assertion that would
    # fail for a timeout that only raised while leaving an orphan on the GPU.
    assert _wait_gone(pid), f"sidecar pid {pid} survived the timeout"
    assert not _process_alive(pid)


def test_hang_does_not_leave_scratch_behind(monkeypatch, tmp_path):
    """The kill path must still clean up its PCM/request files."""
    runner = _fake(tmp_path, "hang", _FAKE_HANG)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    monkeypatch.setattr(photon_asr, "_scratch_dir", lambda: tmp_path / "scratch")
    with pytest.raises(photon_asr.PhotonTimeout):
        photon_asr.run_sidecar(
            pcm=_audio(1.0), clips=[(0.0, 1.0)], sample_rate=16000,
            python=Path(sys.executable), runner=runner, timeout_s=2.0,
        )
    left = list((tmp_path / "scratch").glob("run-*/")) if (tmp_path / "scratch").exists() else []
    assert not left, f"scratch survived the kill: {left}"


# --- the circuit breaker: a hang costs a batch, not the process lifetime --

def test_breaker_opens_after_repeated_failures(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "crash", _FAKE_CRASH)
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    monkeypatch.setenv(photon_asr.MAX_FAILURES_ENV, "2")
    photon_asr.reset_stats()
    for _ in range(2):
        with pytest.raises(photon_asr.PhotonUnavailable):
            photon_asr.run_sidecar(
                pcm=_audio(1.0), clips=[(0.0, 1.0)], sample_rate=16000,
                timeout_s=60.0,
            )
    assert photon_asr.breaker_open() is True
    assert photon_asr.available() is False
    assert "circuit breaker open" in photon_asr.unavailable_reason()
    # And no further process is spawned while it is open.
    assert photon_asr.stats()["attempts"] == 0


def test_success_closes_the_breaker(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    photon_asr.reset_stats()
    photon_asr.run_sidecar(
        pcm=_audio(2.0), clips=[(0.0, 2.0)], sample_rate=16000,
        python=Path(sys.executable), runner=runner, timeout_s=60.0,
    )
    assert photon_asr.breaker_open() is False
    assert photon_asr.stats()["ok"] == 1


# --- the seam: _decode_batch is the production decode path ----------------

class _FakeRec:
    """A sherpa-onnx recognizer stand-in; records that it was actually used."""

    def __init__(self) -> None:
        self.calls = 0

    def create_stream(self):
        rec = self

        class _S:
            def __init__(self) -> None:
                self.result = type("R", (), {"text": "sherpa said this", "tokens": [],
                                             "timestamps": []})()

            def accept_waveform(self, rate, samples) -> None:
                pass
        rec.calls += 1
        return _S()

    def decode_stream(self, stream) -> None:
        pass


def _photon_enabled(monkeypatch, tmp_path, runner: Path) -> None:
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    monkeypatch.setattr(at, "_photon_gpu_allowed", lambda: True, raising=False)
    # Keep the PCM handoff scratch inside tmp_path: the real _scratch_dir
    # resolves the AI-models root, and a test must not write there. The real
    # one has its own test below.
    monkeypatch.setattr(
        photon_asr, "_scratch_dir", lambda: tmp_path / "scratch", raising=False
    )
    photon_asr.reset_stats()


def test_decode_batch_uses_photon_on_a_gpu_slot(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    _photon_enabled(monkeypatch, tmp_path, runner)
    rec = _FakeRec()
    out = at._decode_batch(
        rec, _audio(4.0), [(0.0, 2.0), (2.0, 4.0)], "en",
        clip_offsets=[10.0, 12.0], use_cuda=True,
    )
    assert rec.calls == 0, "sherpa must not run when the accelerator answered"
    # Absolute video time via the SAME helper as the guaranteed path:
    # _absolute_clip_bounds. A clip's position inside the batch buffer is a
    # slicing detail; clip_offsets[i] is the clip's absolute start. Photon
    # mirrors the guaranteed path exactly, so a Photon batch is
    # indistinguishable from a sherpa-onnx batch downstream.
    assert out[0][0][0]["start_sec"] == 10.0
    assert out[0][0][0]["words"][0]["start"] == 10.0
    assert out[0][0][0]["words"][0]["conf"] == 0.99
    # 12.0 is this clip's OWN absolute start (clip_offsets[1]). It is not
    # 14.0: adding the in-buffer position (2.0) to the absolute offset counts
    # the same instant twice and lands every clip after the first late.
    assert out[1][0][0]["start_sec"] == 12.0  # clip_offsets[1], nothing added
    assert out[0][1] == "en"


def test_decode_batch_falls_back_to_sherpa_on_timeout(monkeypatch, tmp_path):
    """A hang must cost one batch, not the job — and the job continues."""
    runner = _fake(tmp_path, "hang", _FAKE_HANG)
    _photon_enabled(monkeypatch, tmp_path, runner)
    monkeypatch.setenv(photon_asr.TIMEOUT_ENV, "2")
    rec = _FakeRec()
    out = at._decode_batch(rec, _audio(2.0), [(0.0, 2.0)], "en", use_cuda=True)
    assert rec.calls == 1, "the job must continue on sherpa-onnx"
    assert out[0][0][0]["text"] == "sherpa said this"
    assert photon_asr.stats()["timeouts"] == 1


def test_decode_batch_falls_back_on_garbage(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "garbage", _FAKE_GARBAGE)
    _photon_enabled(monkeypatch, tmp_path, runner)
    rec = _FakeRec()
    out = at._decode_batch(rec, _audio(2.0), [(0.0, 2.0)], "en", use_cuda=True)
    assert rec.calls == 1
    assert out[0][0][0]["text"] == "sherpa said this"


def test_decode_batch_falls_back_on_empty(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "empty", _FAKE_EMPTY)
    _photon_enabled(monkeypatch, tmp_path, runner)
    rec = _FakeRec()
    at._decode_batch(rec, _audio(2.0), [(0.0, 2.0)], "en", use_cuda=True)
    assert rec.calls == 1
    assert photon_asr.stats()["rejected"] == 1


def test_decode_batch_never_touches_photon_on_a_cpu_slot(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    _photon_enabled(monkeypatch, tmp_path, runner)
    rec = _FakeRec()
    at._decode_batch(rec, _audio(2.0), [(0.0, 2.0)], "en", use_cuda=False)
    assert rec.calls == 1
    assert photon_asr.stats()["attempts"] == 0


def test_decode_batch_never_touches_photon_when_disallowed(monkeypatch, tmp_path):
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    _photon_enabled(monkeypatch, tmp_path, runner)
    monkeypatch.setattr(at, "_photon_gpu_allowed", lambda: False, raising=False)
    rec = _FakeRec()
    at._decode_batch(rec, _audio(2.0), [(0.0, 2.0)], "en", use_cuda=True)
    assert rec.calls == 1
    assert photon_asr.stats()["attempts"] == 0


def test_sherpa_works_with_photon_entirely_absent(monkeypatch, tmp_path):
    """The guaranteed path with the accelerator not installed at all."""
    monkeypatch.delenv(photon_asr.ENABLED_ENV, raising=False)
    monkeypatch.setenv(photon_asr.PYTHON_ENV, str(tmp_path / "absent" / "python.exe"))
    rec = _FakeRec()
    out = at._decode_batch(rec, _audio(2.0), [(0.0, 2.0)], "en", use_cuda=True)
    assert rec.calls == 1
    assert out[0][0][0]["text"] == "sherpa said this"
    assert out[0][0][0]["start_sec"] == 0.0
    assert photon_asr.stats()["attempts"] == 0


# --- THE PRODUCTION CALLER ------------------------------------------------
#
# The distinction that matters: the seam existing is not evidence the feature
# runs. These two tests drive the REAL _transcribe_audio_source - the function
# every transcribe job goes through - and assert the sidecar was actually
# spawned and its transcript is what landed in the database.

def _prod_harness(monkeypatch, tmp_path, runner: Path):
    """Wire _transcribe_audio_source around a fake sidecar; return a recorder."""
    from unittest.mock import patch

    from services import archive_db

    _photon_enabled(monkeypatch, tmp_path, runner)
    inserted: list = []
    audio = _audio(10.0)
    ctx = [
        patch.object(at, "decode_audio", return_value=audio),
        patch.object(at, "vad_speech_seconds", return_value=[(0.0, 10.0)]),
        patch.object(at, "_plan_chunks", return_value=[(0.0, 10.0)]),
        patch.object(at, "_job_engine", return_value="parakeet"),
        patch.object(at, "_parakeet_model", return_value=_FakeRec()),
        patch.object(at, "_parakeet_batch_size", return_value=1),
        # The sequential loop calls _gpu_thermal_guard() before every batch; on
        # a shared card sitting at 99% util that is a real 30 s wait, so the
        # test would measure this box's GPU temperature, not the wiring. The
        # guard has its own coverage.
        patch.object(at, "_gpu_thermal_guard", return_value=None),
        patch.object(at, "_transcribe_batch_parakeet", return_value=[
            ([{"start_sec": 0.0, "end_sec": 5.0, "text": "sherpa", "words": []}], "en"),
        ]),
        patch.object(at, "_read_manifest", return_value=({}, {})),
        patch.object(at, "_resume_plan", return_value=([0], 0)),
        patch.object(at, "_write_manifest_header", return_value=None),
        patch.object(at, "_append_manifest_entry", return_value=None),
        patch.object(at, "_manifest_path", return_value=tmp_path / "manifest.json"),
        patch.object(at, "_thread_pin", return_value=None),
        patch.object(at, "_effective_device", return_value=("cuda", "int8")),
        patch.object(at, "_asr_model_name", return_value=at.PARAKEET_MODEL),
        # The real DB is in play here; without these the twin guard aborts the
        # run before any insert and the test would assert on an empty list for
        # the wrong reason.
        patch.object(archive_db, "transcribed_on_higher_priority_platform",
                     return_value=False),
        patch.object(archive_db, "transcript_for", return_value=[]),
        patch.object(at, "_twin_transcribed_while_running", return_value=False),
        patch.object(archive_db, "insert_transcript",
                     # COPY the batch: the caller clears its own list right
                     # after the insert, so storing the reference would record
                     # an empty batch and make this test assert on nothing.
                     side_effect=lambda p, v, batch, **k: (
                         inserted.append((p, v, [dict(r) for r in batch])) or 0
                     )),
    ]
    for c in ctx:
        c.start()
    return ctx, inserted


def test_production_caller_actually_fires_the_accelerator(monkeypatch, tmp_path):
    """_transcribe_audio_source - the real job path - spawns the sidecar and
    stores ITS transcript. This is the test that would fail if the wiring were
    a registration nothing called."""
    runner = _fake(tmp_path, "ok", _FAKE_OK, segs=_OK_SEGMENTS)
    ctx, inserted = _prod_harness(monkeypatch, tmp_path, runner)
    try:
        stats = at._transcribe_audio_source(
            "kick", "k-photon", str(tmp_path / "a.mp4"), None, None, None, 0.0,
            sharded=False, shard_dir=None,
        )
    finally:
        for c in reversed(ctx):
            c.stop()
    assert photon_asr.stats()["attempts"] == 1, "the job never reached the sidecar"
    assert photon_asr.stats()["ok"] == 1
    rows = [r for call in inserted for r in call[2]]
    assert [r["text"] for r in rows] == ["hello world"], (
        f"the accelerator's transcript did not reach the DB: {rows} / {stats}"
    )
    # And the accelerator is visible in the job's own stats.
    assert stats["photon"]["ok"] == 1
    assert stats["photon"]["scope"] == "process"


def test_production_caller_falls_back_and_finishes_on_a_hang(monkeypatch, tmp_path):
    """The guaranteed path through the REAL job function: a hung sidecar is
    killed, sherpa-onnx decodes, the job still lands its rows."""
    runner = _fake(tmp_path, "hang", _FAKE_HANG)
    monkeypatch.setenv(photon_asr.TIMEOUT_ENV, "2")
    ctx, inserted = _prod_harness(monkeypatch, tmp_path, runner)
    try:
        stats = at._transcribe_audio_source(
            "kick", "k-hang", str(tmp_path / "b.mp4"), None, None, None, 0.0,
            sharded=False, shard_dir=None,
        )
    finally:
        for c in reversed(ctx):
            c.stop()
    assert photon_asr.stats()["timeouts"] == 1
    rows = [r for call in inserted for r in call[2]]
    assert [r["text"] for r in rows] == ["sherpa"], (
        f"the job did not finish on the guaranteed path: {rows} / {stats}"
    )
    assert stats["photon"]["timeouts"] == 1
    assert stats["segments"] == 1



# --- the GPU gate ---------------------------------------------------------

def _gate_host(monkeypatch, **over) -> None:
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(photon_asr.runner_path()))
    photon_asr.reset_stats()
    monkeypatch.setattr(at, "caption_session_active", lambda: False)
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)
    monkeypatch.setattr(at, "_thread_cpu_fallback", lambda: False)
    monkeypatch.setattr(at, "_gpu_vram_allowance", lambda: 16 * 1024 ** 3)
    monkeypatch.setattr(at, "_parakeet_gpu_allowed", lambda: True)
    for name, value in over.items():
        monkeypatch.setattr(at, name, value, raising=False)


def test_gate_allows_a_free_gpu(monkeypatch, tmp_path):
    _gate_host(monkeypatch)
    assert at._photon_gpu_allowed() is True


def test_gate_refuses_live_captions(monkeypatch):
    _gate_host(monkeypatch, caption_session_active=lambda: True)
    assert at._photon_gpu_allowed() is False


def test_gate_refuses_a_busy_gpu(monkeypatch):
    _gate_host(monkeypatch, _gpu_held_by_other=lambda: True)
    assert at._photon_gpu_allowed() is False


def test_gate_refuses_low_vram(monkeypatch):
    _gate_host(monkeypatch, _gpu_vram_allowance=lambda: 512 * 1024 ** 2)
    assert at._photon_gpu_allowed() is False


def test_gate_refuses_a_cpu_fallback_lane(monkeypatch):
    _gate_host(monkeypatch, _thread_cpu_fallback=lambda: True)
    assert at._photon_gpu_allowed() is False


def test_gate_refuses_when_disabled(monkeypatch):
    _gate_host(monkeypatch)
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "0")
    assert at._photon_gpu_allowed() is False


def test_gate_refuses_in_restrictive_governor_mood(monkeypatch):
    class _G:
        class state:
            cpu_raw = 0.99
        ram_pause = staticmethod(lambda: False)
    monkeypatch.setattr(at, "_GOVERNOR_AVAILABLE", True, raising=False)
    _gate_host(monkeypatch)
    monkeypatch.setattr(at, "get_governor", lambda: _G(), raising=False)
    assert at._photon_gpu_allowed() is False


# --- the production callers ----------------------------------------------

def test_sequential_path_calls_the_seam():
    """The decode loop must go through _decode_batch, not around it - a seam
    nothing calls is the bug this feature has to not ship. Four production
    call sites: the hybrid lane's sharded + full-audio branches, and the
    sequential loop's sharded + full-audio branches."""
    src = Path(at.__file__).read_text(encoding="utf-8")
    assert src.count("batch_out = _decode_batch(") == 4, (
        "expected the seam at all four production decode sites"
    )
    # And no DECODE site may bypass the seam. The two remaining direct calls
    # are deliberate: prewarm_parakeet's silence warmup (loads the guaranteed
    # model, not a decode) and _decode_batch's own fallback.
    direct = [
        ln for ln in src.splitlines()
        if "_transcribe_batch_parakeet(" in ln
        and "def _transcribe_batch_parakeet" not in ln
    ]
    assert len(direct) == 2, f"unexpected direct calls: {direct}"
    assert "_parakeet_global, silence" in direct[0], "prewarm keeps its warmup"
    assert direct[1].strip().startswith("return _transcribe_batch_parakeet("), (
        f"a decode call site bypasses the seam: {direct}"
    )
    assert "rec, audio, chunks" in src, "the seam forwards the full signature"
