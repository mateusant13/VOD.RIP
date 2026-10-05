"""GET /api/asr/runtime must make the opt-in Photon accelerator VISIBLE.

The accelerator was added for "legendas instantaneas" and armed, but nothing
in the app could ask whether it was on. photon_asr counted every attempt and
logged one status line; the only other way to learn its state was to read a
log. This suite pins the endpoint that answers it.

THE RULE EVERY TEST HERE EXISTS FOR: unmeasured is not zero. A counter that
has never incremented is null with a reason, not 0 - "0 timeouts" reads as a
clean run when it in fact means the accelerator was never consulted. Disabled
is a state of its own and must never be reported as broken, and a check that
could not be read says unknown instead of inventing a plausible default.

NO GPU, NO SIDECAR, NO NETWORK. The counters are written only by a real
sidecar run, which these tests must not do (the GPU on this box is shared and
only ~1.5-4.7 GB is free), so the failure/success bookkeeping is driven
through photon_asr's own counters instead. Every path is synthetic: the
weights dir, the venv interpreter and the parakeet model cache all live in
tmp_path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import system as system_router
from services import asr_runtime, photon_asr

# The accelerator's own weights, in the order _weights_ok checks them. Real
# sizes do not matter: the readiness floor is 1 KiB, and these are synthetic.
_WEIGHT_FILES = ("config.json", "tokenizer.json", "model.safetensors")
# The GUARANTEED engine's model files - a different set from the accelerator's
# above, which is the whole point: two models, two caches, two vocabularies.
_PARAKEET_FILES = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")


def _weights_dir(root: Path, *, complete: bool = True) -> Path:
    """A weights directory the resolver accepts (or an empty one it refuses)."""
    d = root / "photon-weights"
    d.mkdir(parents=True, exist_ok=True)
    for name in _WEIGHT_FILES[: 3 if complete else 2]:
        (d / name).write_bytes(b"x" * 2048)
    return d


def _parakeet_cache(root: Path, *, files=_PARAKEET_FILES, int8: bool = False) -> Path:
    """A sherpa model cache holding the Redux model (and optionally int8)."""
    cache = root / "parakeet-models" / "sherpa-onnx-nemo-parakeet-redux"
    cache.mkdir(parents=True, exist_ok=True)
    for name in files:
        (cache / name).write_bytes(b"y" * 16)
    if int8:
        (root / "parakeet-models"
         / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8").mkdir(parents=True)
    return root / "parakeet-models"


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    """Every Photon and parakeet path resolved into tmp_path; counters clean.

    The real resolution answers on this box (a populated G: cache, a 5 GB
    venv, models on H:), so nothing here may be left to chance: the HF cache
    is pointed at an empty synthetic root, which makes "weights absent" a
    fact of the fixture rather than of the machine.
    """
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setenv("VODRIP_WHISPER_CACHE", str(models))
    monkeypatch.setenv(asr_runtime.RUNTIME_DIR_ENV, str(tmp_path / "asr-runtime"))
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(tmp_path / "no-parakeet-yet"))
    empty_hf = tmp_path / "photon-hf-empty"
    (empty_hf / "hub").mkdir(parents=True)
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(empty_hf))
    for name in (photon_asr.ENABLED_ENV, photon_asr.WEIGHTS_ENV,
                 photon_asr.VENV_ENV, photon_asr.PYTHON_ENV,
                 photon_asr.RUNNER_ENV, photon_asr.MODEL_ENV,
                 photon_asr.MAX_FAILURES_ENV, photon_asr.BREAKER_COOLDOWN_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(photon_asr, "_scratch_dir", lambda: tmp_path / "scratch")
    photon_asr.reset_stats()
    return tmp_path


@pytest.fixture
def client():
    """A minimal app carrying only the system router - no lifespan, no daemons."""
    app = FastAPI()
    app.include_router(system_router.router)
    return TestClient(app)


def _get(client) -> dict:
    r = client.get("/api/asr/runtime")
    assert r.status_code == 200, r.text
    return r.json()


def _count(kind: str, message: str) -> None:
    """One recorded attempt of the given outcome, via the engine's own counters.

    Deliberately not transcribe_batch(): that spawns the sidecar, and the GPU
    is shared. The attempt counter and the outcome counter are exactly what a
    real run writes, so the endpoint sees the same state shape.
    """
    with photon_asr._lock:
        photon_asr._state["attempts"] += 1
    photon_asr._note_failure(kind, message)


# --- the pre-existing contract is not broken ------------------------------

def test_response_keeps_the_legacy_runtime_keys(hermetic, client, monkeypatch):
    """installed/version/executable/dir/env_override are the contract every
    existing consumer reads; the accelerator legs are additive."""
    legacy = {
        "installed": False, "version": None, "executable": None,
        "dir": str(hermetic / "asr-runtime"), "env_override": True,
    }
    monkeypatch.setattr(asr_runtime, "runtime_status", lambda: dict(legacy))

    body = _get(client)

    for key, value in legacy.items():
        assert body[key] == value, f"{key} changed: {body[key]!r} != {value!r}"


def test_response_is_json_serialisable_and_200_without_the_legacy_patch(
    hermetic, client,
):
    """The real runtime_status() path, so the endpoint cannot depend on a stub."""
    body = _get(client)

    assert body["engine"]["name"] == "parakeet"
    assert isinstance(body["photon"]["state"], str)


# --- disabled is a state, not a defect ------------------------------------

def test_disabled_accelerator_reads_as_disabled_not_broken(hermetic, client):
    """The default: VODRIP_PHOTON_ASR=0. Off by DECISION - never reported as
    a broken install, and its reason names the knob that turns it on."""
    body = _get(client)
    p = body["photon"]

    assert p["enabled"] is False
    assert p["on"] is False
    assert p["state"] == "disabled"
    assert p["switch_env"] in p["reason"]
    # Its state is still reported: disabled does not mean unchecked.
    assert set(p["weights"]) == {"ok", "dir", "reason"}
    assert p["weights"]["ok"] is False


def test_enabled_without_an_interpreter_is_misconfigured_not_disabled(
    hermetic, client, monkeypatch,
):
    """The switch is on and the thing still cannot run. That is a different
    problem from 'off', and collapsing the two is how a broken accelerator
    reads as a working default."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(
        photon_asr.PYTHON_ENV, str(hermetic / "absent" / "python.exe")
    )

    p = _get(client)["photon"]

    assert p["enabled"] is True
    assert p["on"] is False
    assert p["state"] == "misconfigured"
    assert p["state"] != "disabled"
    assert p["reason"]
    assert p["venv"]["interpreter_present"] is False


def test_open_breaker_reads_as_retired_and_says_for_how_long(
    hermetic, client, monkeypatch,
):
    """The breaker takes Photon out of the process after repeated failures.
    That is a THIRD state - not off, not broken-but-running - and the seconds
    left are the fact the user needs to decide whether to wait."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(photon_asr.runner_path()))
    monkeypatch.setenv(photon_asr.MAX_FAILURES_ENV, "1")
    monkeypatch.setenv(photon_asr.BREAKER_COOLDOWN_ENV, "900")
    _count("timeouts", "no result within 120s (pid 7, killed=True)")

    p = _get(client)["photon"]

    assert p["breaker"]["open"] is True
    assert p["state"] == "retired"
    assert 0 < p["breaker"]["seconds_remaining"] <= 900
    assert p["breaker"]["failure_threshold"] == 1
    assert p["breaker"]["cooldown_s"] == 900
    assert p["breaker"]["consecutive_failures"] == 1
    # The reason the breaker retired it is the failure that retired it.
    assert "no result within" in p["last_fallback_reason"]


# --- unmeasured is not zero -----------------------------------------------

def test_an_untried_accelerator_reports_unmeasured_not_zero(hermetic, client):
    """The counters of an accelerator that was never consulted are null, and
    say so. Reporting 0 would claim 'measured, nothing went wrong'."""
    p = _get(client)["photon"]
    c = p["counters"]

    assert c["measured"] is False
    for key in ("ok", "timeouts", "errors", "rejected",
                "weights_missing", "fallbacks"):
        assert c[key] is None, f"{key}={c[key]!r} claims a measurement"
    # The headline number follows the same rule.
    assert _get(client)["photon_fallbacks"] is None


def test_a_measured_zero_is_reported_as_zero(hermetic, client, monkeypatch):
    """The other half of the same distinction: two attempts that both timed
    out DID measure ok=0. Collapsing that into null would hide a real clean
    dimension and make a broken accelerator look untried."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(photon_asr.runner_path()))
    monkeypatch.setenv(photon_asr.MAX_FAILURES_ENV, "99")
    _count("timeouts", "hung (pid 1)")
    _count("timeouts", "hung (pid 2)")

    c = _get(client)["photon"]["counters"]

    assert c["measured"] is True
    assert c["attempts"] == 2
    assert c["ok"] == 0
    assert c["timeouts"] == 2
    assert c["errors"] is None or c["errors"] == 0


def test_kills_are_not_invented_as_a_zero(hermetic, client, monkeypatch):
    """The sidecar is killed inside the timeout path, so there is no separate
    kill counter. The endpoint must say that rather than print a 0 that
    reads as 'nothing was ever killed'."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.MAX_FAILURES_ENV, "99")
    _count("timeouts", "hung (pid 3)")

    c = _get(client)["photon"]["counters"]

    assert c["killed"] is None
    assert c["killed_reason"]
    assert c["timeouts"] == 1


def test_cuda_is_unverified_with_a_reason_and_never_false(hermetic, client):
    """Proving CUDA needs a real sidecar run. Reporting False would be a lie
    of the exact class this endpoint exists to stop; the reason must name
    why it is unknown."""
    cuda = _get(client)["photon"]["cuda"]

    assert cuda["verified"] is None
    assert cuda["reason"]


def test_unreadable_photon_state_is_unknown_and_the_rest_still_answers(
    hermetic, client, monkeypatch,
):
    """A check that cannot be read degrades ON ITS OWN: the accelerator block
    says unknown, the guaranteed path still reports, and the route is 200."""
    def _boom():
        raise RuntimeError("counters unreadable")

    monkeypatch.setattr(photon_asr, "stats", _boom)

    body = _get(client)

    assert body["photon"]["state"] == "unknown"
    assert body["photon"]["enabled"] is None
    assert "unreadable" in body["photon"]["reason"]
    assert body["photon_fallbacks"] is None
    # The guaranteed path is independent and must not go dark with it.
    assert body["engine"]["runtime"] == "sherpa-onnx"


# --- the fallback count, which is the whole point --------------------------

def test_fallback_count_is_surfaced_and_agrees_with_the_counters(
    hermetic, client, monkeypatch,
):
    """Every attempt that did not succeed is a batch re-run on sherpa-onnx.
    A silent CPU fallback under a "GPU" label is the lie this whole endpoint
    is for, so the number is hoisted to the top level - and the two copies
    are one value, so they cannot drift."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(photon_asr.runner_path()))
    monkeypatch.setenv(photon_asr.MAX_FAILURES_ENV, "99")
    _weights_dir(hermetic)
    monkeypatch.setenv(photon_asr.WEIGHTS_ENV, str(hermetic / "photon-weights"))
    _count("timeouts", "hung (pid 4)")
    _count("timeouts", "hung (pid 5)")
    _count("timeouts", "hung (pid 6)")

    body = _get(client)

    assert body["photon"]["counters"]["fallbacks"] == 3
    assert body["photon_fallbacks"] == 3
    assert body["photon_fallbacks"] == body["photon"]["counters"]["fallbacks"]


def test_fallbacks_make_a_usable_accelerator_read_as_degraded(
    hermetic, client, monkeypatch,
):
    """On, healthy, and still falling back - the state a plain
    enabled/available boolean cannot express."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(photon_asr.runner_path()))
    monkeypatch.setenv(photon_asr.MAX_FAILURES_ENV, "99")
    _weights_dir(hermetic)
    monkeypatch.setenv(photon_asr.WEIGHTS_ENV, str(hermetic / "photon-weights"))
    _count("errors", "sidecar exit 1: boom")

    p = _get(client)["photon"]

    assert p["on"] is True
    assert p["weights"]["ok"] is True
    assert p["state"] == "degraded"
    assert p["last_fallback_reason"] == "sidecar exit 1: boom"


def test_present_weights_are_reported_as_ok_with_the_directory(
    hermetic, client, monkeypatch,
):
    """The setup fact a user can act on: are the weights on disk, and where."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    ready = _weights_dir(hermetic)
    monkeypatch.setenv(photon_asr.WEIGHTS_ENV, str(ready))

    weights = _get(client)["photon"]["weights"]

    assert weights["ok"] is True
    assert weights["dir"] == str(ready)
    assert weights["reason"] == ""


# --- the guaranteed path stays legible ------------------------------------

def test_engine_names_sherpa_onnx_redux_and_an_unknown_device(
    hermetic, client, monkeypatch,
):
    """The accelerator made 'so what is transcribing my VODs?' urgent. The
    model and the engine are answerable; the DEVICE a job ran on is not, and
    is reported unknown rather than filled in with the plan slot."""
    _parakeet_cache(hermetic)
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(hermetic / "parakeet-models"))

    engine = _get(client)["engine"]

    assert engine["runtime"] == "sherpa-onnx"
    assert engine["guaranteed"] is True
    assert engine["model"] == "Codyfederer/sherpa-onnx-nemo-parakeet-redux"
    assert engine["model_present"] is True
    assert engine["model_dir"] == str(
        hermetic / "parakeet-models" / "sherpa-onnx-nemo-parakeet-redux"
    )
    assert engine["missing_files"] == []
    # The device is per-job state in the worker process, and it is NOT
    # readable from here. A plausible default here is what 4f6da7e removed.
    assert engine["device"]["known"] is False
    assert engine["device"]["value"] is None
    assert engine["device"]["reason"]


def test_model_selection_is_a_constant_not_a_knob(hermetic, client):
    """Which model is default is an owner's open decision. The endpoint
    reports that it is a code constant and does not pretend to offer a dial.
    """
    engine = _get(client)["engine"]

    assert "no runtime knob" in engine["model_selection"]


def test_int8_revert_on_disk_is_reported_without_claiming_it_will_run(
    hermetic, client, monkeypatch,
):
    """The int8 model is deliberately kept on disk. Its presence is a fact
    about the disk and NOT a statement about what will transcribe."""
    cache = _parakeet_cache(hermetic, int8=True)
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(cache))

    engine = _get(client)["engine"]

    assert engine["int8_fallback_on_disk"] is True
    assert engine["int8_fallback_dir"] == str(
        cache / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    )
    assert engine["model"] == "Codyfederer/sherpa-onnx-nemo-parakeet-redux"
    assert "not selectable at runtime" in engine["int8_fallback_note"]


def test_absent_int8_revert_is_a_real_false_not_unknown(
    hermetic, client, monkeypatch,
):
    """A checked-and-absent file is a measurement. Only an unreadable check
    is unknown."""
    cache = _parakeet_cache(hermetic, int8=False)
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(cache))

    engine = _get(client)["engine"]

    assert engine["int8_fallback_on_disk"] is False
    assert engine["int8_fallback_dir"] is None


def test_the_missing_model_file_is_named(hermetic, client, monkeypatch):
    """'The model is not downloaded' is not actionable; the absent filename
    is. The verifier resolves the same two candidate dirs the worker does,
    so this names the one the worker would look in first."""
    cache = _parakeet_cache(hermetic, files=("encoder.onnx", "decoder.onnx"))
    monkeypatch.setenv("VODRIP_SHERRPA_CACHE", str(cache))

    engine = _get(client)["engine"]

    assert engine["model_present"] is False
    assert engine["missing_files"] == ["joiner.onnx", "tokens.txt"]
    assert engine["model_dir"] is None


def test_absent_sherpa_is_reported_as_false_not_unknown(
    hermetic, client, monkeypatch,
):
    """A find_spec that finds nothing is a measurement: False, with a reason.
    This is the negative-result case that must not be confused with the
    unreadable-check case above."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    engine = _get(client)["engine"]

    assert engine["sherpa_onnx"] is False
    assert engine["sherpa_onnx_reason"]


# --- the probe must not touch the accelerator ------------------------------

def test_the_endpoint_never_launches_the_accelerator(hermetic, client, monkeypatch):
    """Everything above is a READ. Turning the accelerator on - or merely
    probing CUDA by running it - would take a GPU another workload is using,
    so the status path is pinned to never call the engine."""
    def _forbidden(*_a, **_k):
        raise AssertionError("the status endpoint must not run the accelerator")

    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setattr(photon_asr, "transcribe_batch", _forbidden)
    monkeypatch.setattr(photon_asr, "run_sidecar", _forbidden)
    monkeypatch.setattr(photon_asr, "available", _forbidden)
    _parakeet_cache(hermetic)

    body = _get(client)

    assert body["photon"]["enabled"] is True
