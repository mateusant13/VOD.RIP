"""Live-caption MAX-priority reservation — archive_transcribe planner + CUDA gate.

The real-time captioner (services.live_captions) owns the GPU while a
livestream is watched: the archive pool's plan (_gpu_copies / _worker_plan)
must yield — 0 GPU copies and a single quiet CPU lane — and the captioner's
own CUDA eligibility is gated on a REAL NVIDIA GPU (torch CUDA up + vendor
nvidia), never a Virtual Display Driver (the access-violation repro).
Pure-plan tests: no model load, no GPU — every probe is patched.

Run from the backend directory with:
    python -m pytest tests/test_caption_priority.py -q
"""

from __future__ import annotations

from services import archive_transcribe as at
from transcribe_plan_isolation import isolate_worker_plan, reset_cpu_load_cache

GIB = 1024 ** 3


def _force_cuda(monkeypatch) -> None:
    # raising=False: pin is a per-thread attribute of threading.local and
    # never exists on the pytest thread — default raising=True AttributeErrors.
    monkeypatch.setattr(at._multi_tls, "pin", ("cuda", "int8"), raising=False)
    # Fixed thread count: the CPU cap (budget = 0.4 x threads) must be
    # deterministic on any runner — 20 threads -> budget 8, auto lanes 3.
    monkeypatch.setattr("os.cpu_count", lambda: 20)


def _force_cpu(monkeypatch) -> None:
    monkeypatch.setattr(at._multi_tls, "pin", ("cpu", "int8"), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 20)


def _idle_planner(monkeypatch, vram_gib: float) -> None:
    """Unheld, idle, RAM-ample planner: only the tested gate can reduce slots."""
    isolate_worker_plan(monkeypatch)
    monkeypatch.setattr(at, "_gpu_free_vram_bytes", lambda: int(vram_gib * GIB))
    monkeypatch.setattr(at, "_gpu_held_by_other", lambda: False)
    monkeypatch.setattr(at, "_gpu_util", lambda: 0.1)
    monkeypatch.setattr(at, "_cpu_load_high", lambda: False)
    monkeypatch.setattr(at, "_free_system_ram_bytes", lambda: 64 * GIB)


# --- archive GPU/CPU yield while a caption session is live -------------------

def test_caption_session_pauses_archive_gpu_lane(monkeypatch):
    """A live caption session forces the archive GPU lane off — the real-time
    captioner owns the card; releasing the session restores the full plan."""
    _force_cuda(monkeypatch)
    _idle_planner(monkeypatch, 64.0)
    monkeypatch.setenv(at.GPU_COPIES_ENV, "8")
    monkeypatch.setenv(at.WORKERS_ENV, "4")
    assert at.caption_session_active() is False
    full = at._worker_plan()
    assert any(d == "cuda" for d, _ in full), full
    try:
        at.set_caption_session_active(True)
        assert at._gpu_copies() == 0  # never stack on the captioner
        plan = at._worker_plan()
        assert plan == [("cpu", "int8")], plan  # one quiet lane, no GPU
    finally:
        at.set_caption_session_active(False)
    assert at._worker_plan() == full  # the yield is reversible


def test_caption_session_caps_archive_cpu_lanes(monkeypatch):
    """CPU-only host: a caption session caps the pool to one quiet lane so the
    captioner's parakeet decode threads get the box."""
    _force_cpu(monkeypatch)
    _idle_planner(monkeypatch, 0.0)
    reset_cpu_load_cache()
    monkeypatch.setenv(at.WORKERS_ENV, "4")
    assert at._worker_plan() == [("cpu", "int8")] * 4
    try:
        at.set_caption_session_active(True)
        assert at._worker_plan() == [("cpu", "int8")]
    finally:
        at.set_caption_session_active(False)
    assert at._worker_plan() == [("cpu", "int8")] * 4


def test_caption_reserved_vram_bytes():
    """While active, the captioner's CUDA footprint is reserved from the
    archive's free-VRAM budget (parakeet model + EP arena + tenant headroom);
    inactive sessions reserve nothing."""
    try:
        at.set_caption_session_active(True)
        assert at.caption_reserved_vram_bytes() == (
            at._PARAKEET_GPU_VRAM_EST + at._GPU_VRAM_HEADROOM
        )
    finally:
        at.set_caption_session_active(False)
    assert at.caption_reserved_vram_bytes() == 0


# --- captioner CUDA gate: real GPU only --------------------------------------

def test_offpool_cuda_probe_requires_real_gpu(monkeypatch):
    """The captioner's CUDA gate is a REAL NVIDIA GPU — a Virtual Display
    Driver / broken-CUDA box stays CPU (the access-violation repro), the
    RTX-5080 class goes CUDA, VODRIP_CAPTION_CUDA=0 is a hard kill, and a
    missing +cuda wheel keeps CPU even on a real card."""
    monkeypatch.setattr(at, "_offpool_cuda_ok", None)
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: True)
    monkeypatch.setattr(at, "_real_cuda_works", lambda: False)  # VDD: no torch CUDA
    monkeypatch.setattr("services.gpu_detect.detect_gpu_vendor", lambda: "none")
    assert at._offpool_cuda_available() is False

    monkeypatch.setattr(at, "_offpool_cuda_ok", None)
    monkeypatch.setattr(at, "_real_cuda_works", lambda: True)  # real card
    monkeypatch.setattr("services.gpu_detect.detect_gpu_vendor", lambda: "nvidia")
    assert at._offpool_cuda_available() is True

    monkeypatch.setattr(at, "_offpool_cuda_ok", None)
    monkeypatch.setenv("VODRIP_CAPTION_CUDA", "0")
    monkeypatch.setattr(at, "_real_cuda_works", lambda: True)
    monkeypatch.setattr("services.gpu_detect.detect_gpu_vendor", lambda: "nvidia")
    assert at._offpool_cuda_available() is False  # explicit kill switch

    monkeypatch.setattr(at, "_offpool_cuda_ok", None)
    monkeypatch.setenv("VODRIP_CAPTION_CUDA", "1")
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: False)
    monkeypatch.setattr(at, "_real_cuda_works", lambda: True)
    monkeypatch.setattr("services.gpu_detect.detect_gpu_vendor", lambda: "nvidia")
    assert at._offpool_cuda_available() is False  # CPU wheel -> CPU


def test_parakeet_provider_offpool_cuda_on_real_gpu(monkeypatch):
    """_parakeet_provider: off-pool callers (the live captioner) get 'cuda'
    only when the real-GPU probe passes; pool threads keep their pinned slot
    device regardless of the off-pool probe."""
    monkeypatch.setattr(at, "_thread_pin", lambda: None)
    monkeypatch.setattr(at, "_offpool_cuda_available", lambda: True)
    assert at._parakeet_provider() == "cuda"
    monkeypatch.setattr(at, "_offpool_cuda_available", lambda: False)
    assert at._parakeet_provider() == "cpu"
    # pool pins are untouched by the off-pool probe
    monkeypatch.setattr(at, "_thread_pin", lambda: ("cuda", "int8"))
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: True)
    assert at._parakeet_provider() == "cuda"
    monkeypatch.setattr(at, "_thread_pin", lambda: ("cpu", "int8"))
    assert at._parakeet_provider() == "cpu"
    monkeypatch.setattr(at, "_thread_pin", lambda: None)
    assert at._parakeet_provider() == "cpu"
