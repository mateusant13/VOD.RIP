"""Parakeet CPU lane unit tests — routing, fallbacks, word assembly.

No sherpa-onnx import and no model load: the availability probe is pinned
via the module's cached ``_parakeet_ok`` flag (same technique the module's
own self-check uses), so these run on machines without sherpa-onnx too.
"""
from __future__ import annotations

import builtins
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import archive_transcribe as at  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("VODRIP_PARAAKEET", raising=False)
    monkeypatch.delenv("VODRIP_SHERRPA_CACHE", raising=False)
    monkeypatch.setattr(at, "_parakeet_ok", None)
    monkeypatch.setattr(at, "_parakeet_cuda_ok", None)
    yield
    monkeypatch.setattr(at, "_parakeet_ok", None)
    monkeypatch.setattr(at, "_parakeet_cuda_ok", None)


def _routed(language, *, device="cpu", parakeet_ok=True, cuda_ok=None, vram_free=None):
    at._parakeet_ok = parakeet_ok
    at._parakeet_cuda_ok = cuda_ok
    at._device_override = (device, "int8" if device == "cpu" else "float16")
    saved_vram_free, saved_vram_at = at._vram_free_bytes, at._vram_free_at
    if vram_free is not None:
        at._vram_free_bytes = vram_free
        at._vram_free_at = time.monotonic()  # fresh cache -> the gate reads it
    try:
        return at._job_engine(language)
    finally:
        at._device_override = None
        at._vram_free_bytes, at._vram_free_at = saved_vram_free, saved_vram_at


def test_cpu_lane_routes_supported_languages_to_parakeet():
    for lang in ("pt", "en", "es", "fr", "de", "it", "ru", "uk", "lt", "el"):
        assert _routed(lang) == "parakeet", lang


def test_known_other_and_unknown_stay_whisper():
    for lang in ("ja", "ko", "zh", "ar", "hi"):
        assert _routed(lang) == "whisper", lang
    assert _routed(None) == "whisper"
    assert _routed("") == "whisper"


GIB = 1024 ** 3


def test_gpu_lane_routes_parakeet_with_cuda_sherpa():
    """GPU slot + CUDA-enabled sherpa + supported lang + ample VRAM -> parakeet."""
    assert _routed("pt", device="cuda", cuda_ok=True, vram_free=16 * GIB) == "parakeet"
    assert _routed("en", device="cuda", cuda_ok=True, vram_free=16 * GIB) == "parakeet"
    assert _routed("es", device="cuda", cuda_ok=True, vram_free=16 * GIB) == "parakeet"


def test_gpu_lane_whisper_without_cuda_sherpa():
    """GPU slot + CPU-only sherpa -> whisper (graceful degradation, pre-parakeet path).

    The unprobed cache (cuda_ok=None) probes the INSTALLED wheel at runtime,
    so its outcome is env-dependent — pinned flags keep this hermetic; the
    None path is covered by the module self-check and the real GPU smoke."""
    assert _routed("pt", device="cuda", cuda_ok=False) == "whisper"


def test_gpu_lane_vram_tight_falls_back():
    """GPU slot + CUDA sherpa but tight measured free VRAM -> whisper."""
    assert _routed("pt", device="cuda", cuda_ok=True, vram_free=1 * GIB) == "whisper"


def test_gpu_lane_keeps_whisper_for_other_languages():
    """Known-other (ja) and unknown languages stay whisper on GPU slots."""
    assert _routed("ja", device="cuda", cuda_ok=True, vram_free=16 * GIB) == "whisper"
    assert _routed(None, device="cuda", cuda_ok=True, vram_free=16 * GIB) == "whisper"
    assert _routed("", device="cuda", cuda_ok=True, vram_free=16 * GIB) == "whisper"


def test_cpu_lane_parakeet_unaffected_by_cuda_probe():
    """CPU routing ignores the CUDA probe entirely (int8 recognizer as before)."""
    assert _routed("pt", device="cpu", cuda_ok=False) == "parakeet"
    assert _routed("pt", device="cpu", cuda_ok=True) == "parakeet"


def test_import_fail_falls_back_to_whisper():
    assert _routed("pt", parakeet_ok=False) == "whisper"
    assert at._slot_engine("cpu") == "whisper"
    assert at._slot_engine("cuda") == "whisper"  # import fail gates the CUDA probe too


def test_availability_probe_caches_failed_import(monkeypatch):
    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError("no sherpa-onnx on this machine")
        return real_import(name, *args, **kwargs)

    at._parakeet_ok = None
    monkeypatch.setattr(builtins, "__import__", _boom)
    assert at._parakeet_available() is False
    assert at._parakeet_available() is False, "probe result must be cached"
    assert at._parakeet_ok is False


def test_kill_switch_disables_lane(monkeypatch):
    monkeypatch.setenv("VODRIP_PARAAKEET", "0")
    at._parakeet_ok = True  # even with the import healthy, the switch wins
    assert at._parakeet_available() is False
    assert _routed("pt") == "whisper"
    assert at._slot_engine("cpu") == "whisper"


def test_slot_engine():
    at._parakeet_ok = True
    at._parakeet_cuda_ok = True
    assert at._slot_engine("cpu") == "parakeet"
    assert at._slot_engine("cuda") == "parakeet"
    at._parakeet_cuda_ok = False
    assert at._slot_engine("cuda") == "whisper"
    at._parakeet_ok = False
    assert at._slot_engine("cpu") == "whisper"


def test_langs_intersect_with_model_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(tmp_path))
    at._parakeet_ok = True
    # pre-download: the candidate set is authoritative
    assert at._parakeet_langs() == at.PARAKEET_LANG_CANDIDATES
    # model dir present -> routing narrows to the model's actual lang tokens
    d = tmp_path / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    d.mkdir()
    for f in at._PARAKEET_FILES:
        (d / f).write_text("x", encoding="utf-8")
    (d / "tokens.txt").write_text("<|pt|> 0\n<|ja|> 1\n<|zh|> 2\n", encoding="utf-8")
    assert at._parakeet_langs() == {"pt"}


def test_langs_empty_when_lane_unavailable():
    at._parakeet_ok = False
    assert at._parakeet_langs() == frozenset()


def test_cache_dir_override_and_default_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(tmp_path / "s"))
    assert at._parakeet_cache_dir() == tmp_path / "s"
    monkeypatch.delenv("VODRIP_SHERRPA_CACHE")
    monkeypatch.setattr(at, "_cache_dir", lambda: tmp_path / "whisper-models")
    assert at._parakeet_cache_dir() == tmp_path / "parakeet-models"


def test_word_assembly():
    toks = [" N", "eg", "an", " de", " ", "1", "0", " minut", "os", ",", " né", "?"]
    ts = [0.32, 0.48, 0.56, 0.8, 1.04, 1.12, 1.12, 1.2, 1.28, 1.36, 2.08, 2.24]
    words = at._parakeet_words(toks, ts)
    assert [w["word"] for w in words] == ["Negan", "de 10", "minutos,", "né?"]
    assert words[0] == {"word": "Negan", "start": 0.32, "end": 0.56}
    assert words[-1] == {"word": "né?", "start": 2.08, "end": 2.24}
    assert at._parakeet_words([], []) == []


def test_thread_budget_bounded():
    n = at._parakeet_threads()
    assert 1 <= n <= at._PARAAKEET_MAX_THREADS


def test_cpu_auto_workers_ladder(monkeypatch):
    """The default CPU-lane count scales with the box: 2 below 16 threads,
    3 at 16-31, 4 at 32+ — and the env override always wins."""
    for threads, expected in ((4, 2), (8, 2), (15, 2), (16, 3), (20, 3), (31, 3), (32, 4), (64, 4)):
        monkeypatch.setattr("os.cpu_count", lambda: threads)
        assert at._cpu_auto_workers() == expected, threads
        assert at._cpu_worker_ceiling() == expected, f"no-env ceiling @ {threads}"
    # explicit env wins over the ladder
    monkeypatch.setattr("os.cpu_count", lambda: 64)
    monkeypatch.setenv(at.WORKERS_ENV, "1")
    assert at._cpu_worker_ceiling() == 1, "env override must beat the ladder"
    monkeypatch.setenv(at.WORKERS_ENV, "0")
    assert at._cpu_worker_ceiling() == 0, "0 keeps the exclusive-GPU meaning"
    monkeypatch.delenv(at.WORKERS_ENV, raising=False)


def test_ensure_cuda_libs_exposes_nvidia_dirs(tmp_path, monkeypatch):
    """The CUDA wheels' DLL dirs must land on PATH (nt: <pkg>/bin) / on
    LD_LIBRARY_PATH (POSIX: <pkg>/lib) — a CUDA-13-era driver alone has no
    CUDA 12 toolkit, so onnxruntime's CUDA EP finds these DLLs via the env.
    Both layouts are simulated; the real wheels only ship the matching one.
    """
    import site as _site

    sp = tmp_path / "site-packages"
    for pkg in ("cublas", "cuda_runtime", "cufft", "curand", "cudnn", "nvjitlink"):
        (sp / "nvidia" / pkg / "bin").mkdir(parents=True)
        (sp / "nvidia" / pkg / "lib").mkdir(parents=True)
    monkeypatch.setattr(_site, "getsitepackages", lambda: [str(sp)])
    monkeypatch.delenv("VODRIP_NO_CUDA_LIBS", raising=False)

    env_name = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    subdir = "bin" if os.name == "nt" else "lib"
    monkeypatch.setenv(env_name, "")
    at._ensure_cuda_libs()
    for pkg in ("cublas", "cuda_runtime", "cufft", "curand", "cudnn", "nvjitlink"):
        expected = str(sp / "nvidia" / pkg / subdir)
        assert expected in os.environ[env_name], (pkg, expected)

    # the OTHER layout must never leak in (bin dirs are meaningless on POSIX
    # and vice versa) — keep the env var strictly the platform's own layout
    leaked = str(sp / "nvidia" / "cudnn" / ("lib" if os.name == "nt" else "bin"))
    assert leaked not in os.environ[env_name]


def test_ensure_cuda_libs_skip_flag(tmp_path, monkeypatch):
    import site as _site

    sp = tmp_path / "site-packages"
    (sp / "nvidia" / "cudnn" / ("bin" if os.name == "nt" else "lib")).mkdir(parents=True)
    monkeypatch.setattr(_site, "getsitepackages", lambda: [str(sp)])
    monkeypatch.setenv("VODRIP_NO_CUDA_LIBS", "1")
    env_name = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    monkeypatch.setenv(env_name, "")
    at._ensure_cuda_libs()
    assert os.environ[env_name] == ""


def test_manifest_engine_change_invalidates_resume():
    header = {"chunks": [(0.0, 5.0)], "model": at.model_name()}
    entries = {0: {"ci": 0, "first": 0, "count": 1}}
    # pre-parakeet manifests carry no 'engine' key -> read as whisper
    missing, _ = at._resume_plan(header["chunks"], header, entries, {0}, engine="whisper")
    assert missing == [], "old-format manifest must resume a whisper run"
    # a parakeet run must NOT trust a whisper manifest (model + engine differ)
    missing, _ = at._resume_plan(header["chunks"], header, entries, {0}, engine="parakeet")
    assert missing == [0], "engine switch must invalidate manifest entries"
    # and a whisper run must not trust a parakeet manifest
    pheader = {"chunks": [(0.0, 5.0)], "model": at.PARAKEET_MODEL, "engine": "parakeet"}
    missing, _ = at._resume_plan(pheader["chunks"], pheader, entries, {0}, engine="whisper")
    assert missing == [0]


def test_gpu_autoinstall_needed_gates(monkeypatch):
    """The boot-time GPU swap must fire ONLY on NVIDIA hosts with a CPU
    sherpa-onnx: never on AMD/Intel, never when the lane is off, never when
    the CUDA wheel is already installed, never behind the kill switch."""
    monkeypatch.delenv(at.GPU_AUTOINSTALL_ENV, raising=False)
    monkeypatch.setattr(at, "detect_gpu_vendor", lambda: "nvidia")
    monkeypatch.delenv(at.PARAKEET_ENV, raising=False)

    class _FakeCuda:
        __version__ = "1.13.4+cuda12.cudnn9"

    class _FakeCpu:
        __version__ = "1.13.4"

    import builtins

    _orig_import = builtins.__import__

    def _import(name, *a, **k):
        if name == "sherpa_onnx":
            return _FakeCpu
        return _orig_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _import)
    assert at._gpu_autoinstall_needed() is True

    # CUDA wheel already present -> no-op
    monkeypatch.setattr(builtins, "__import__", lambda n, *a, **k: (
        _FakeCuda if n == "sherpa_onnx" else _orig_import(n, *a, **k)
    ))
    assert at._gpu_autoinstall_needed() is False

    # non-NVIDIA GPU -> no-op
    monkeypatch.setattr(at, "detect_gpu_vendor", lambda: "amd")
    assert at._gpu_autoinstall_needed() is False

    # kill switch (VODRIP_PARAAKEET=0) -> no-op even on NVIDIA
    monkeypatch.setattr(at, "detect_gpu_vendor", lambda: "nvidia")
    monkeypatch.setenv(at.PARAKEET_ENV, "0")
    assert at._gpu_autoinstall_needed() is False

    # explicit escape hatch wins over everything
    monkeypatch.delenv(at.PARAKEET_ENV)
    monkeypatch.setenv(at.GPU_AUTOINSTALL_ENV, "1")
    assert at._gpu_autoinstall_needed() is False


def test_gpu_autoinstall_skips_frozen(monkeypatch):
    """Frozen installs never self-modify (read-only tree; upgrades ship via
    the signed updater) — same posture as the yt-dlp check."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls = []

    def _fake_install():
        calls.append(1)

    monkeypatch.setattr(at, "_gpu_autoinstall_needed", lambda: True)
    monkeypatch.setattr(at, "_gpu_autoinstall_due", lambda: True)
    monkeypatch.setattr(at, "sp", type("_sp", (), {"run": staticmethod(lambda *a, **k: calls.append(2))}))
    at.maybe_ensure_gpu_sherpa()
    assert calls == [], "frozen install must not attempt pip"


def test_gpu_autoinstall_respects_stamp_and_installs(tmp_path, monkeypatch):
    """Due + NVIDIA + CPU wheel -> exactly one pip run; stamp updated. Not
    due -> zero pip runs."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(at, "_gpu_autoinstall_stamp_path", lambda: tmp_path / "stamp.txt")
    monkeypatch.setattr(at, "_gpu_autoinstall_needed", lambda: True)
    monkeypatch.setattr(at, "detect_gpu_vendor", lambda: "nvidia")
    runs = []

    class _Proc:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(at, "sp", type("_sp", (), {
        "run": staticmethod(lambda *a, **k: (runs.append(k), _Proc())[1]),
        "TimeoutExpired": TimeoutError,
    }))
    at.maybe_ensure_gpu_sherpa()
    assert len(runs) == 1
    assert (tmp_path / "stamp.txt").is_file()
    # first call after the stamp write is no longer due -> no second pip
    monkeypatch.setattr(at, "_gpu_autoinstall_due", lambda: False)
    at.maybe_ensure_gpu_sherpa()
    assert len(runs) == 1
