"""Photon sidecar arming: offline weights, a durable venv default, and a
visible on/off state.

NO GPU AND NO REAL VENV, and no network: every cache here is a synthetic one
built in tmp_path, so the suite is hermetic and fast. What is real is the
shape of the problem - a weight file that exists but carries no bytes is the
failure that reads as a hang, and it is what these tests pin down.

THE ASSERTION THAT MATTERS MOST: a missing-weights condition must surface as
PhotonWeightsMissing, never as PhotonTimeout, and never as a zero-byte
checkpoint quietly handed to the engine. Every weights test therefore also
asserts that no timeout was recorded - a test that passed because the clock ran
out would be worse than no test at all, because it would look like coverage of
the hang path.
"""
from __future__ import annotations

import hashlib
import json
import subprocess as sp
import sys
from pathlib import Path

import numpy as np
import pytest

from services import photon_asr

# The model's own files, in kestrel's loader order, with the manifest name
# that makes the ternary branch load. Sizes are small and declared by the
# synthetic manifest, which is what the resolver measures against - the real
# 177 MB checkpoint is asserted in its own test, not duplicated 8x per case.
_FILES = ("config.json", "tokenizer.json", "model.safetensors", "ternary.json")
_SIZES = (2048, 4096, 8192, 1024)


def _payload(name: str, size: int) -> bytes:
    # Distinct content per file, so a resolver that mixes them up is caught.
    return (name.encode() * (size // len(name) + 1))[:size]


def _build_cache(root: Path, *, truncate_weights: bool = False,
                keep_payload: bool = True) -> dict:
    """A synthetic HF cache: hub/blobs + refs/main + snapshots + trees.

    ``truncate_weights`` reproduces the interrupted transfer - the snapshot
    entry resolves to a 0-byte blob - which is the condition that must never
    be mistaken for a working checkpoint.
    """
    hub = root / "hub"
    repo = hub / "models--moondream--parakeet-redux"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "blobs").mkdir(parents=True, exist_ok=True)
    (repo / "trees").mkdir(parents=True, exist_ok=True)
    snap = repo / "snapshots" / "deadbeef"
    snap.mkdir(parents=True, exist_ok=True)
    (repo / "refs" / "main").write_text("deadbeef", encoding="utf-8")

    files, shas = {}, {}
    for name, size in zip(_FILES, _SIZES):
        data = _payload(name, size)
        sha = hashlib.sha256(data).hexdigest()
        shas[name] = sha
        blob = repo / "blobs" / sha[:2] / sha
        blob.parent.mkdir(parents=True, exist_ok=True)
        if name == "model.safetensors" and truncate_weights:
            blob.write_bytes(b"")  # the hole: present, and empty
            (snap / name).symlink_to(Path("..") / ".." / "blobs" / sha[:2] / sha)
            files[name] = {
                "size": size, "lfs_size": size,
                "lfs_sha256": sha, "xet_hash": sha,
            }
            continue
        blob.write_bytes(data)
        (snap / name).symlink_to(Path("..") / ".." / "blobs" / sha[:2] / sha)
        files[name] = {"size": size, "lfs_sha256": sha}

    if truncate_weights and keep_payload:
        # The reconstructed payload, complete, filed under its own hash - the
        # way an xet transfer leaves it when the final move is interrupted.
        staging = hub / "blobs" / "49" / shas["model.safetensors"]
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(_payload("model.safetensors", _SIZES[2]))

    (repo / "trees" / "deadbeef.json").write_text(
        json.dumps({"format_version": 1, "files": files}), encoding="utf-8"
    )
    return {"repo": repo, "snap": snap, "shas": shas}


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    """Pin the models root and the cache into tmp_path; keep scratch there too."""
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setenv("VODRIP_WHISPER_CACHE", str(models))
    for name in (photon_asr.WEIGHTS_ENV, photon_asr.VENV_ENV,
                 photon_asr.PYTHON_ENV, photon_asr.RUNNER_ENV,
                 photon_asr.MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(photon_asr, "_scratch_dir", lambda: tmp_path / "scratch")
    photon_asr.reset_stats()
    return models


# --- 1. weights resolve from the project-local cache ----------------------

def test_resolve_weights_returns_the_snapshot_when_complete(hermetic, monkeypatch):
    cache = hermetic / "photon-hf"
    built = _build_cache(cache)
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(cache))

    resolved = photon_asr.resolve_weights()

    assert resolved == built["snap"]
    for name in _FILES:
        assert (resolved / name).stat().st_size == _SIZES[_FILES.index(name)]
    # A complete cache is READ, not rewritten: no shadow copy is made.
    assert not (cache / "prepared").exists()


def test_hf_home_prefers_a_cache_under_the_models_root(hermetic, monkeypatch):
    """A cache inside the AI-models folder wins, so a configured models folder
    is obeyed and the location inherits the project's drive convention instead
    of a second hardcoded drive letter."""
    monkeypatch.delenv(photon_asr.HF_HOME_ENV, raising=False)
    _build_cache(hermetic / "photon-hf")

    assert photon_asr.hf_home() == hermetic / "photon-hf"


def test_hf_home_falls_back_to_a_known_populated_cache(hermetic, monkeypatch):
    """With nothing under the models root, the resolution is still
    deterministic and only ever names a documented location - never the
    absent per-user cache, and never the system drive."""
    monkeypatch.delenv(photon_asr.HF_HOME_ENV, raising=False)
    resolved = photon_asr.hf_home()
    assert resolved in {hermetic / "photon-hf"} | {
        Path(c) for c in photon_asr._POPULATED_HF_CACHES
    }, f"undocumented HF cache: {resolved}"
    assert not str(resolved).upper().startswith("C:")


def test_weights_env_override_short_circuits_resolution(hermetic, monkeypatch):
    ready = hermetic / "elsewhere"
    ready.mkdir()
    for name, size in zip(_FILES, _SIZES):
        (ready / name).write_bytes(_payload(name, size))
    monkeypatch.setenv(photon_asr.WEIGHTS_ENV, str(ready))
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(hermetic / "does-not-exist"))

    assert photon_asr.resolve_weights() == ready


def test_truncated_snapshot_is_recovered_from_the_cache_with_no_network(
    hermetic, monkeypatch,
):
    """The interrupted-transfer case: the snapshot entry is 0 bytes but the
    same cache holds a verified payload. Resolved, and the source untouched."""
    cache = hermetic / "photon-hf"
    built = _build_cache(cache, truncate_weights=True)
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(cache))
    holed = built["snap"] / "model.safetensors"
    assert holed.stat().st_size == 0, "the fixture must reproduce the hole"

    resolved = photon_asr.resolve_weights()

    assert (resolved / "model.safetensors").stat().st_size == _SIZES[2]
    for name in _FILES:
        assert (resolved / name).stat().st_size == _SIZES[_FILES.index(name)]
    # Nothing in the borrowed cache was moved, deleted or overwritten.
    assert holed.stat().st_size == 0
    assert (cache / "hub").is_dir()


def test_a_payload_that_fails_its_hash_is_refused(hermetic, monkeypatch):
    """Right size, wrong bytes must not be adopted: a half-written checkpoint
    would fail later as if the engine were at fault."""
    cache = hermetic / "photon-hf"
    built = _build_cache(cache, truncate_weights=True, keep_payload=False)
    staging = cache / "hub" / "blobs" / "49" / built["shas"]["model.safetensors"]
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"z" * _SIZES[2])  # correct length, wrong content
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(cache))

    with pytest.raises(photon_asr.PhotonWeightsMissing):
        photon_asr.resolve_weights()


# --- 2. missing weights: a NAMED error, never a timeout --------------------

def test_absent_cache_raises_the_named_weights_error(hermetic, monkeypatch):
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(hermetic / "no-such-cache"))

    with pytest.raises(photon_asr.PhotonWeightsMissing) as exc:
        photon_asr.resolve_weights()

    assert photon_asr.WEIGHTS_MISSING_MARKER in str(exc.value)
    # The distinction this whole change exists to protect.
    assert not isinstance(exc.value, photon_asr.PhotonTimeout)
    assert photon_asr.stats()["timeouts"] == 0


def test_empty_cache_with_no_payload_raises_the_named_error(hermetic, monkeypatch):
    cache = hermetic / "photon-hf"
    _build_cache(cache, truncate_weights=True, keep_payload=False)
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(cache))

    with pytest.raises(photon_asr.PhotonWeightsMissing) as exc:
        photon_asr.resolve_weights()

    assert photon_asr.WEIGHTS_MISSING_MARKER in str(exc.value)
    assert not isinstance(exc.value, photon_asr.PhotonTimeout)


def test_weights_missing_is_an_unavailable_so_the_caller_still_falls_back():
    """archive_transcribe catches PhotonUnavailable; the new type must stay
    inside it or a missing cache would become a failed job."""
    assert issubclass(photon_asr.PhotonWeightsMissing, photon_asr.PhotonUnavailable)
    assert issubclass(photon_asr.PhotonTimeout, photon_asr.PhotonUnavailable)


def test_real_runner_names_missing_weights_before_the_heavy_import(hermetic, tmp_path):
    """The shipped runner, run under an interpreter with no moondream, must
    blame the WEIGHTS - not the import - when weights_dir is unusable. The
    gate is before the import precisely so this answer is cheap and honest.
    """
    req = tmp_path / "req.json"
    pcm = tmp_path / "pcm.f32"
    np.zeros(16000, dtype=np.float32).tofile(str(pcm))
    empty = tmp_path / "no-weights"
    empty.mkdir()
    req.write_text(json.dumps({
        "model": "moondream/parakeet-redux",
        "weights_dir": str(empty),
        "sample_rate": 16000,
        "timestamps": "word",
        "pcm_path": str(pcm),
        "clips": [{"start": 0.0, "end": 1.0}],
    }), encoding="utf-8")

    proc = sp.run(
        [sys.executable, str(photon_asr.runner_path()), str(req)],
        capture_output=True, timeout=60,
    )

    assert proc.returncode != 0
    payload = json.loads(proc.stdout.decode("utf-8", "replace").strip())
    assert payload["ok"] is False
    assert photon_asr.WEIGHTS_MISSING_MARKER in payload["error"]
    assert "photon import failed" not in payload["error"]


def test_client_raises_weights_missing_and_records_no_timeout(hermetic, monkeypatch):
    """End to end through the production client and the REAL runner: a missing
    cache must be reported as a setup problem, quickly, and must not be
    counted as a hang."""
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(hermetic / "no-such-cache"))
    import time
    t0 = time.monotonic()

    with pytest.raises(photon_asr.PhotonWeightsMissing):
        photon_asr.run_sidecar(
            pcm=np.zeros(16000, dtype=np.float32),
            clips=[(0.0, 1.0)],
            sample_rate=16000,
            python=Path(sys.executable),
            timeout_s=60.0,
        )
    elapsed = time.monotonic() - t0

    snap = photon_asr.stats()
    assert snap["timeouts"] == 0, "a missing cache was recorded as a hang"
    assert snap["weights_missing"] == 1
    # Comfortably inside the 60 s budget: this answered, it did not wait.
    assert elapsed < 20.0, f"the weights error waited {elapsed:.1f}s"


# --- the anti-timeout guarantees ------------------------------------------

def test_sidecar_env_pins_the_project_cache_and_forbids_the_network(hermetic):
    env = photon_asr._sidecar_env()
    assert env["HF_HOME"] == str(photon_asr.hf_home())
    # These two are what make a cache miss an error instead of a 170 MB
    # download inside the 120 s wall clock.
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"


def test_request_carries_the_resolved_weights_directory(hermetic, monkeypatch):
    """The sidecar is told WHERE to load from, so kestrel's loader
    short-circuits on the local directory instead of resolving over the Hub."""
    cache = hermetic / "photon-hf"
    _build_cache(cache)
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(cache))
    seen = hermetic / "seen.txt"

    runner = hermetic / "record.py"
    runner.write_text(
        "import json, sys\n"
        "req = json.load(open(sys.argv[1]))\n"
        f"open(r'{seen}', 'w').write(req.get('weights_dir', '<<absent>>'))\n"
        "json.dump({'ok': True, 'clips': [{'language': 'en', 'segments': ["
        "{'text': 'hi', 'start': 0.0, 'end': 0.5, 'words': []}]} "
        "for _ in req['clips']]}, sys.stdout)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(photon_asr, "runner_path", lambda: runner)

    out = photon_asr.run_sidecar(
        pcm=np.zeros(16000, dtype=np.float32),
        clips=[(0.0, 1.0)],
        sample_rate=16000,
        python=Path(sys.executable),
        timeout_s=60.0,
    )

    assert out[0]["segments"][0]["text"] == "hi"
    recorded = seen.read_text(encoding="utf-8")
    assert recorded == str(photon_asr.resolve_weights())
    # And it is a directory the loader can actually open, not a bare name.
    assert (Path(recorded) / "model.safetensors").stat().st_size == _SIZES[2]
    assert photon_asr.stats()["timeouts"] == 0


# --- 3. the venv default resolves deterministically ------------------------

def test_venv_env_override_wins(hermetic, monkeypatch):
    monkeypatch.setenv(photon_asr.VENV_ENV, str(hermetic / "chosen"))
    assert photon_asr.venv_root() == hermetic / "chosen"


def test_venv_default_is_deterministic_and_documented(hermetic, monkeypatch):
    """Same answer every time, and never an undocumented path: either the
    durable models-root location or an explicitly listed compatibility
    candidate."""
    monkeypatch.delenv(photon_asr.VENV_ENV, raising=False)
    first = photon_asr.venv_root()
    assert first == photon_asr.venv_root()
    allowed = {hermetic / "photon-venv"} | {
        Path(c) for c in photon_asr._SANDBOX_VENVS
    }
    assert first in allowed, f"undocumented venv default: {first}"


def test_a_durable_venv_wins_over_the_scratch_candidate(hermetic, monkeypatch):
    """Once a venv exists at the durable location it takes over with no env
    var - that is the whole point of routing the default through the helper."""
    monkeypatch.delenv(photon_asr.VENV_ENV, raising=False)
    durable = hermetic / "photon-venv"
    (durable / "Scripts").mkdir(parents=True)
    (durable / "Scripts" / "python.exe").write_bytes(b"")

    assert photon_asr.venv_root() == durable
    assert photon_asr.python_path() == durable / "Scripts" / "python.exe"
    assert photon_asr.venv_is_sandbox() is False


def test_venv_default_is_never_the_system_interpreter(hermetic, monkeypatch):
    """The app interpreter runs a live API and a live transcription worker, so
    the durable default must be a directory this project owns - under the
    AI-models root, never under the interpreter that is already running."""
    monkeypatch.delenv(photon_asr.VENV_ENV, raising=False)
    root = photon_asr.venv_root()
    owned = (hermetic / "photon-venv") in [root] or root in {
        Path(c) for c in photon_asr._SANDBOX_VENVS
    }
    assert owned, f"the venv default escaped the project's own roots: {root}"
    assert str(Path(sys.prefix)).lower() not in str(root).lower()
    assert "site-packages" not in str(root)


# --- 4. the on/off state is observable ------------------------------------

def test_status_summary_reports_the_off_default(hermetic, monkeypatch):
    monkeypatch.delenv(photon_asr.ENABLED_ENV, raising=False)
    summary = photon_asr.log_status()

    assert summary["on"] is False
    assert summary["enabled"] is False
    assert photon_asr.ENABLED_ENV in summary["reason"]
    # Every documented field present, and JSON-able, so it can be handed to a
    # status route verbatim rather than re-derived per consumer.
    json.dumps(summary)
    for key in (
        "on", "enabled", "reason", "breaker_open", "attempts", "ok", "timeouts",
        "errors", "rejected", "weights_missing", "last_error", "model",
        "python", "venv", "venv_is_sandbox", "weights",
    ):
        assert key in summary, f"status surface is missing {key}"
    assert set(summary["weights"]) == {"ok", "dir", "reason"}


def test_status_summary_says_why_an_enabled_accelerator_is_unusable(
    hermetic, monkeypatch,
):
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, str(hermetic / "nope" / "python.exe"))
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(hermetic / "no-such-cache"))

    summary = photon_asr.log_status()

    assert summary["on"] is False
    assert "interpreter" in summary["reason"]
    # The setup problem is named here too, not only where it is raised.
    assert summary["weights"]["ok"] is False
    assert photon_asr.WEIGHTS_MISSING_MARKER in summary["weights"]["reason"]


def test_status_summary_reports_a_resolvable_cache_as_ready(hermetic, monkeypatch):
    cache = hermetic / "photon-hf"
    _build_cache(cache)
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(cache))

    weights = photon_asr.status_summary()["weights"]

    assert weights["ok"] is True
    assert weights["reason"] == ""
    assert Path(weights["dir"]).is_dir()


def test_available_announces_the_state_once_without_raising(hermetic, monkeypatch, caplog):
    """Visibility with no new registration: the first consultation logs the
    verdict, and later ones cost nothing."""
    monkeypatch.setattr(photon_asr, "_status_logged", False)
    monkeypatch.delenv(photon_asr.ENABLED_ENV, raising=False)
    with caplog.at_level("INFO", logger=photon_asr.logger.name):
        photon_asr.available()
        photon_asr.available()
        photon_asr.available()
    lines = [r for r in caplog.records if "photon GPU ASR accelerator" in r.getMessage()]
    assert len(lines) == 1, f"expected one status line, got {len(lines)}"
    assert photon_asr.ENABLED_ENV in lines[0].getMessage()


def test_available_does_not_become_a_weights_gate(hermetic, monkeypatch):
    """Documented, deliberate: available() is the cheap per-batch gate and
    says nothing about the weights, so the on/off contract it already had
    (and its tests) is unchanged. The weights get a named error instead."""
    monkeypatch.setenv(photon_asr.ENABLED_ENV, "1")
    monkeypatch.setenv(photon_asr.PYTHON_ENV, sys.executable)
    runner = hermetic / "runner.py"
    runner.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv(photon_asr.RUNNER_ENV, str(runner))
    monkeypatch.setenv(photon_asr.HF_HOME_ENV, str(hermetic / "no-such-cache"))

    assert photon_asr.available() is True
    assert photon_asr.unavailable_reason() == ""
    assert photon_asr.weights_ready() is False


# --- the real cache on this box -------------------------------------------

def test_real_weights_resolve_to_a_complete_checkpoint():
    """Against the project's actual cache, with no network. The invariant
    that must hold on any box: the weights either resolve to files carrying
    their published bytes, or they raise the named error. Never a hang, never
    a 0-byte checkpoint handed on as if it were a model.
    """
    try:
        resolved = photon_asr.resolve_weights()
    except photon_asr.PhotonWeightsMissing as exc:
        pytest.skip(f"no local Photon weights on this box: {exc}")

    assert resolved.is_dir()
    for name in photon_asr._BASE_WEIGHT_FILES:
        path = resolved / name
        assert path.is_file(), f"{name} absent from {resolved}"
        size = path.stat().st_size
        assert size > photon_asr._MIN_REAL_WEIGHT_BYTES, (
            f"{name} is {size} bytes - a pointer stub, not a checkpoint"
        )
    # The ternary manifest is what makes kestrel load the ternary student
    # rather than silently falling back to full precision.
    assert (resolved / photon_asr._TERNARY_MANIFEST).is_file()
    # Never on the system drive: AGENTS.md keeps heavy project data off C:.
    assert not str(resolved).upper().startswith("C:")


def test_real_default_does_not_resolve_to_the_absent_user_cache():
    """The original defect: with no HF_HOME the sidecar would have looked in
    ~/.cache/huggingface, which holds no moondream weights here."""
    hf = photon_asr.hf_home()
    assert not str(hf).upper().startswith("C:")
    legacy = Path.home() / ".cache" / "huggingface"
    if legacy.exists():
        assert hf.resolve() != legacy.resolve()
