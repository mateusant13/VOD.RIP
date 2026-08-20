"""Intra-VOD hybrid chunk parallelism — mocked, no real GPU required."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from services import archive_transcribe as at


def _hybrid_plan(monkeypatch) -> None:
    """CUDA host with 1 GPU + 2 CPU lanes and ample VRAM."""
    monkeypatch.setattr(at._multi_tls, "pin", ("cuda", "int8"), raising=False)
    monkeypatch.setattr(
        at._multi_tls,
        "plan",
        (("cuda", "int8"), ("cpu", "int8"), ("cpu", "int8")),
        raising=False,
    )
    monkeypatch.setattr(at._multi_tls, "active", True, raising=False)
    monkeypatch.setattr(at, "_parakeet_cuda_ok", True)
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: 16 * 1024 ** 3)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * 1024 ** 3)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)
    monkeypatch.setattr(at, "caption_session_active", lambda: False)


def test_hybrid_chunk_slots_gpu_cpu(monkeypatch):
    _hybrid_plan(monkeypatch)
    slots = at._hybrid_chunk_slots()
    assert slots[0][0] == "cuda"
    assert sum(1 for d, _ in slots if d == "cpu") == 2


def test_hybrid_chunk_slots_caption_cpu_only(monkeypatch):
    _hybrid_plan(monkeypatch)
    monkeypatch.setattr(at, "caption_session_active", lambda: True)
    slots = at._hybrid_chunk_slots()
    assert slots == [("cpu", "int8"), ("cpu", "int8")]
    assert not any(d == "cuda" for d, _ in slots)


def test_hybrid_chunk_slots_requires_multi_mode(monkeypatch):
    _hybrid_plan(monkeypatch)
    monkeypatch.setattr(at._multi_tls, "active", False, raising=False)
    assert at._hybrid_chunk_slots() == []


def test_defer_gpu_gate_cuda_job_not_requeued(monkeypatch):
    """CUDA pool thread with hybrid slots must not requeue when the gate is busy."""
    _hybrid_plan(monkeypatch)
    monkeypatch.setattr(at, "_gpu_gate_try_acquire", lambda *_: False)
    monkeypatch.setattr(at, "_has_local_archive", lambda *_: True)
    monkeypatch.setattr(at, "transcribe_video", lambda *a, **k: {"segments": 1})
    monkeypatch.setattr(at, "_job_engine", lambda *_: "parakeet")

    job = {
        "id": 1,
        "platform": "twitch",
        "video_id": "v1",
        "kind": "transcribe",
    }
    result = at._process_job(job, multi=True)
    assert result.get("requeued") != "gpu-gate"
    assert result.get("segments") == 1


def test_cpu_thread_never_hits_gpu_gate_requeue(monkeypatch):
    """CPU-pinned pool thread must never be requeued for the GPU sequential gate."""
    monkeypatch.setattr(at._multi_tls, "pin", ("cpu", "int8"), raising=False)
    monkeypatch.setattr(
        at._multi_tls,
        "plan",
        (("cuda", "int8"), ("cpu", "int8")),
        raising=False,
    )
    monkeypatch.setattr(at._multi_tls, "active", True, raising=False)
    gate_calls: list[tuple[str, str]] = []

    def _track_gate(platform: str, video_id: str) -> bool:
        gate_calls.append((platform, video_id))
        return False

    monkeypatch.setattr(at, "_gpu_gate_try_acquire", _track_gate)
    monkeypatch.setattr(at, "_has_local_archive", lambda *_: True)
    monkeypatch.setattr(at, "transcribe_video", lambda *a, **k: {"segments": 2})
    monkeypatch.setattr(at, "_job_engine", lambda *_: "parakeet")

    job = {"id": 2, "platform": "kick", "video_id": "v2", "kind": "transcribe"}
    result = at._process_job(job, multi=True)
    assert gate_calls == []
    assert result.get("requeued") != "gpu-gate"


def test_transcribe_chunks_hybrid_fans_out(monkeypatch, tmp_path):
    """Missing chunks are distributed across GPU+CPU chunk workers."""
    _hybrid_plan(monkeypatch)
    slots = at._hybrid_chunk_slots()
    chunks = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)]
    missing = [0, 1, 2, 3]
    seen: dict[str, list[int]] = {"cuda": [], "cpu": []}
    lock = threading.Lock()

    def fake_batch(rec, audio, chunk_list, language, **kwargs):
        pin = at._thread_pin()
        device = pin[0] if pin else "cpu"
        cs, _ce = chunk_list[0]
        ci = int(cs // 10)
        with lock:
            seen[device].append(ci)
        seg = {
            "start_sec": float(ci * 10),
            "end_sec": float(ci * 10 + 5),
            "text": f"chunk{ci}",
            "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
        }
        return [([seg], "en")]

    monkeypatch.setattr(at, "_parakeet_model", lambda: object())
    monkeypatch.setattr(at, "_parakeet_batch_size", lambda: 1)
    monkeypatch.setattr(at, "_gpu_thermal_guard", lambda: None)
    monkeypatch.setattr(at, "_gpu_gate_try_acquire", lambda *_: True)
    monkeypatch.setattr(at, "_gpu_gate_release", lambda *_: None)
    monkeypatch.setattr(at, "_transcribe_batch_parakeet", fake_batch)
    monkeypatch.setattr(at.archive_db, "insert_transcript", MagicMock())
    monkeypatch.setattr(at, "_twin_transcribed_while_running", lambda *_: False)

    manifest = tmp_path / "m.jsonl"
    manifest.write_text('{"chunks": []}\n', encoding="utf-8")

    state = at._transcribe_chunks_hybrid(
        platform="twitch",
        video_id="hybrid-vod",
        chunks=chunks,
        missing=missing,
        audio=MagicMock(),
        sharded_audio=None,
        language="en",
        engine="parakeet",
        model=object(),
        manifest=manifest,
        existing=set(),
        seg_idx=0,
        progress_cb=None,
        speech_sec=40.0,
        n_chunks=4,
        fix_on=False,
        fix_stats={},
        slots=slots,
    )

    assert state.segments == 4
    assert set(seen["cuda"] + seen["cpu"]) == {0, 1, 2, 3}
    assert seen["cuda"] and seen["cpu"]


def test_commit_chunk_rows_thread_safe_seg_idx(monkeypatch):
    """Concurrent commits must allocate unique, monotonic seg_idx values."""
    monkeypatch.setattr(at.archive_db, "insert_transcript", MagicMock())
    monkeypatch.setattr(at, "_twin_transcribed_while_running", lambda *_: False)
    monkeypatch.setattr(at, "_append_manifest_entry", MagicMock())

    state = at._ChunkTranscribeState(0, set())
    manifest = MagicMock()
    seg = {
        "start_sec": 0.0,
        "end_sec": 1.0,
        "text": "x",
        "words": [],
    }

    def commit(ci: int) -> None:
        at._commit_chunk_rows(
            state,
            platform="twitch",
            video_id="v",
            manifest=manifest,
            ci=ci,
            chunk_segs=[{**seg, "text": f"t{ci}"}],
            language="en",
            detected="en",
            engine="parakeet",
            fix_on=False,
            fix_stats={},
            chunk_span=1.0,
        )

    threads = [threading.Thread(target=commit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.seg_idx == 8
    assert state.segments == 8


def test_caption_reserved_vram_unchanged(monkeypatch):
    """Live caption GPU reservation must stay on the caption_session_active path."""
    monkeypatch.setattr(at, "caption_session_active", lambda: True)
    assert (
        at.caption_reserved_vram_bytes()
        == at._PARAKEET_GPU_VRAM_EST + at._GPU_VRAM_HEADROOM
    )
    monkeypatch.setattr(at, "caption_session_active", lambda: False)
    assert at.caption_reserved_vram_bytes() == 0
