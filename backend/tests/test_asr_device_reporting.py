"""Device reporting when a CUDA slot silently degrades to CPU (Q2).

`VODRIP_TRANSCRIBE_GPU_COPIES` is dead code (multi-copy was removed), and the
docs must stop advertising it. Separately, a cuda-pinned pool slot whose CUDA
session fails to build (the bundled onnxruntime CUDA EP ships no kernels for
some compute capabilities — OrtSessionOptionsAppendExecutionProvider_Cuda,
error 1114) degrades to CPU inside `_load_parakeet`, which flips
`_parakeet_cuda_ok` for the whole process. The plan pin still reads
("cuda", ...), so reporting the pin told the user a GPU run happened when the
VOD was decoded on the CPU. `_ran_device` is the single choke point the job
stats now use.
"""

from __future__ import annotations

import inspect

from services import archive_transcribe as at


def _pin(monkeypatch, value):
    monkeypatch.setattr(at._multi_tls, "pin", value, raising=False)


# --- _ran_device: report what actually ran, not the plan slot --------------

def test_cuda_slot_reports_cpu_after_cuda_became_unavailable(monkeypatch):
    """The real defect: session creation failed, the work ran on the CPU, and
    the job row must not say 'cuda'."""
    _pin(monkeypatch, ("cuda", "int8"))
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: False)
    monkeypatch.setattr(at, "_effective_device", lambda: ("cuda", "int8"))
    assert at._ran_device() == ("cpu", "int8")


def test_cuda_slot_still_reports_cuda_when_cuda_works(monkeypatch):
    """A healthy cuda slot must NOT be rewritten to cpu — the fix maps only a
    slot whose provider actually degraded."""
    _pin(monkeypatch, ("cuda", "int8"))
    monkeypatch.setattr(at, "_parakeet_cuda_available", lambda: True)
    monkeypatch.setattr(at, "_effective_device", lambda: ("cuda", "int8"))
    assert at._ran_device() == ("cuda", "int8")


def test_cpu_slot_and_offpool_are_unchanged(monkeypatch):
    _pin(monkeypatch, ("cpu", "int8"))
    monkeypatch.setattr(at, "_effective_device", lambda: ("cpu", "int8"))
    assert at._ran_device() == ("cpu", "int8")

    # Off-pool caller (live captions, direct calls): no pin, so the global
    # default stands.
    _pin(monkeypatch, None)
    monkeypatch.setattr(at, "_effective_device", lambda: ("cpu", "int8"))
    assert at._ran_device() == ("cpu", "int8")


def test_all_job_stats_device_sites_use_the_choke_point():
    """Every place the transcribe stats report a device must go through
    _ran_device, so no site can keep reporting the raw slot pin."""
    src = inspect.getsource(at._transcribe_audio_source)
    assert '_ran = _thread_pin() or _effective_device()' not in src, (
        "a stats site regressed to reporting the raw plan pin: a cuda slot "
        "whose CUDA session failed to build would be reported as a GPU run"
    )
    assert src.count("_ran = _ran_device()") == 3, (
        "expected the no-speech, twin-won and final stats sites to all use "
        "_ran_device()"
    )


# --- the dead VODRIP_TRANSCRIBE_GPU_COPIES knob ---------------------------

def test_docs_do_not_advertise_gpu_copies_as_a_working_knob():
    """The knob is never read. No docstring may present it as a dial without
    marking it dead — the three docstrings used to disagree (default 2 /
    default 1 / returns 1)."""
    marked = ("REMOVED", "IGNORED", "ignored", "dead knob", "not read")
    docs = {
        "module": at.__doc__ or "",
        "_gpu_copies": at._gpu_copies.__doc__ or "",
        "_worker_plan": at._worker_plan.__doc__ or "",
    }
    for where, doc in docs.items():
        if at.GPU_COPIES_ENV not in doc:
            continue
        # Scoped to the whole docstring, not one line: the "this knob is dead"
        # note may wrap onto the next line.
        assert any(m in doc for m in marked), (
            f"{where} advertises {at.GPU_COPIES_ENV} without marking it dead: "
            f"{doc.strip()[:160]!r}"
        )


def test_gpu_copies_knob_is_genuinely_ignored():
    """The knob's value must not change the lane count (the pre-existing
    shared-model contract)."""
    before = None
    for value in ("1", "2", "8"):
        import os
        os.environ[at.GPU_COPIES_ENV] = value
        try:
            at._parakeet_cuda_ok = False
            no_gpu = at._gpu_copies()
            at._parakeet_cuda_ok = None
            before = before if before is not None else no_gpu
            assert no_gpu == before, f"{at.GPU_COPIES_ENV}={value} changed the result"
        finally:
            os.environ.pop(at.GPU_COPIES_ENV, None)
