#!/usr/bin/env python3
"""GPU-01 exhaust fix: PANNs SED device routing + idle release.

Mock-only tests for services.archive_events:
  - _effective_device() honors the transcribe-lane pin (CPU lane -> cpu,
    GPU lane -> cuda); off-pool it follows the compute-probe default.
  - _sed_model() reloads per lane (a CPU-lane job never runs inference on
    the CUDA copy a GPU lane left behind).
  - _release_sed_on_idle() drops the module singleton on CPU lanes.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

os.environ.setdefault("VODRIP_NO_DAEMONS", "1")
os.environ.pop("VODRIP_EVENTS_DEVICE", None)

from services import archive_events as _ev  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_sed(monkeypatch):
    monkeypatch.delenv("VODRIP_EVENTS_DEVICE", raising=False)
    _ev._sed = None
    _ev._sed_device = None
    yield
    _ev._sed = None
    _ev._sed_device = None


def _patch_pin(monkeypatch, pin):
    monkeypatch.setattr(
        "services.archive_transcribe._thread_pin",
        (lambda: pin) if pin is not None else (lambda: None),
    )


def _patch_detect(monkeypatch, device):
    monkeypatch.setattr(
        "services.archive_transcribe._detect_device",
        lambda: (device, "int8"),
    )


# --- device routing -----------------------------------------------------

def test_effective_device_follows_cpu_lane_pin(monkeypatch):
    """A CPU-pinned events job must never resolve to cuda (RTX is parakeet's)."""
    _patch_pin(monkeypatch, ("cpu", "int8"))
    _patch_detect(monkeypatch, "cuda")
    assert _ev._effective_device() == "cpu"


def test_effective_device_follows_gpu_lane_pin(monkeypatch):
    _patch_pin(monkeypatch, ("cuda", "int8"))
    _patch_detect(monkeypatch, "cpu")
    assert _ev._effective_device() == "cuda"


def test_effective_device_off_pool_uses_compute_probe(monkeypatch):
    """Off-pool (direct API/test call) -> the compute-probe default, which
    ignores Virtual Display Driver adapters."""
    _patch_pin(monkeypatch, None)
    _patch_detect(monkeypatch, "cpu")
    assert _ev._effective_device() == "cpu"
    _patch_detect(monkeypatch, "cuda")
    assert _ev._effective_device() == "cuda"


def test_effective_device_env_override_wins(monkeypatch):
    monkeypatch.setenv("VODRIP_EVENTS_DEVICE", "cuda")
    _patch_pin(monkeypatch, ("cpu", "int8"))
    assert _ev._effective_device() == "cuda"
    monkeypatch.setenv("VODRIP_EVENTS_DEVICE", "cpu")
    _patch_pin(monkeypatch, ("cuda", "int8"))
    assert _ev._effective_device() == "cpu"


# --- per-lane model reload ----------------------------------------------

class _FakeSED:
    instances = []

    def __init__(self, checkpoint_path, device):
        self.checkpoint_path = checkpoint_path
        self.device = device
        self.labels = ["Laughter", "Speech", "Music"]
        _FakeSED.instances.append(self)


@pytest.fixture
def _fake_panns(monkeypatch, tmp_path):
    _FakeSED.instances = []
    mod = types.ModuleType("panns_inference")
    mod.SoundEventDetection = _FakeSED
    monkeypatch.setitem(sys.modules, "panns_inference", mod)
    monkeypatch.setattr(_ev, "_ensure_checkpoint", lambda: str(tmp_path / "ckpt.pth"))
    return _FakeSED


def test_sed_model_reloads_per_lane(_fake_panns, monkeypatch):
    """A CPU lane that finds a CUDA copy reloads on CPU (and vice versa)."""
    _patch_detect(monkeypatch, "cuda")
    _patch_pin(monkeypatch, ("cuda", "int8"))
    m1 = _ev._sed_model()
    assert m1.device == "cuda"
    assert _FakeSED.instances == [m1]

    _patch_pin(monkeypatch, ("cpu", "int8"))
    m2 = _ev._sed_model()
    assert m2 is not m1 and m2.device == "cpu"
    assert len(_FakeSED.instances) == 2

    _patch_pin(monkeypatch, ("cuda", "int8"))
    m3 = _ev._sed_model()
    assert m3 is not m2 and m3.device == "cuda"
    assert len(_FakeSED.instances) == 3


def test_sed_model_reuses_same_lane_copy(_fake_panns, monkeypatch):
    _patch_pin(monkeypatch, ("cpu", "int8"))
    a = _ev._sed_model()
    b = _ev._sed_model()
    assert a is b and len(_FakeSED.instances) == 1


def test_sed_model_off_pool_reuses_compute_probe_copy(_fake_panns, monkeypatch):
    _patch_pin(monkeypatch, None)
    _patch_detect(monkeypatch, "cpu")
    a = _ev._sed_model()
    b = _ev._sed_model()
    assert a is b and a.device == "cpu"


# --- idle release -------------------------------------------------------

def test_release_sed_on_idle_cpu_lane(_fake_panns, monkeypatch):
    _patch_pin(monkeypatch, ("cuda", "int8"))
    _ev._sed_model()
    assert _ev._sed is not None
    _patch_pin(monkeypatch, ("cpu", "int8"))
    _ev._release_sed_on_idle()
    assert _ev._sed is None and _ev._sed_device is None


def test_release_sed_keeps_gpu_lane_copy(_fake_panns, monkeypatch):
    _patch_pin(monkeypatch, ("cuda", "int8"))
    _ev._sed_model()
    _ev._release_sed_on_idle()
    assert _ev._sed is not None, "the GPU lane owns the card — keep the copy"


def test_release_sed_off_pool_keeps_copy(_fake_panns, monkeypatch):
    _patch_pin(monkeypatch, None)
    _ev._sed_model()
    _ev._release_sed_on_idle()
    assert _ev._sed is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
