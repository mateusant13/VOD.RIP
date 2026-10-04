"""Photon (Moondream 2.6.1) GPU ASR accelerator — sidecar CLIENT.

An OPT-IN accelerator that runs beside the guaranteed engine, never instead
of it. sherpa-onnx Parakeet Redux stays the default and the guaranteed path;
this module is only ever asked for a GPU slot's batch, and every failure mode
returns ``None`` so the caller falls straight back to sherpa-onnx.

Why a subprocess and not an import (the measurements this is built on, taken
on this box's RTX 5080 by the probe lane):

  * 8:00 of audio in 3.51 s (RTF 137x) with 1.75 GB VRAM after load — worth
    having, but only on a GPU slot;
  * the engine HANGS, it does not raise: 2 of 3 launches hung during engine
    creation and 1 hung on its third call, with 0% CPU, a frozen resident set
    and no exception. A supervisor watching the worker process sees a live
    process that never returns. In-process this is a wedged app; out-of-process
    it is one killed child and one fallback;
  * there is NO CPU path (kestrel's native extension reports
    ``ternary_gemm_isa() == "scalar"`` and raises NotImplementedError), so this
    can never be the only engine;
  * the venv is 5.29 GB with its own torch — the app interpreter runs a live
    API and a live transcription worker against sherpa-onnx and must not be
    touched by that install.

A hang is therefore bounded twice over: a hard per-run wall-clock timeout
(kill + tree kill), and a circuit breaker that takes Photon out of the running
process after repeated failures, so one bad engine costs one batch instead of
the job.

WEIGHTS ARE OFFLINE, AND SAY SO. The sidecar used to resolve the model through
HF_HOME alone, which defaults to ~/.cache/huggingface on the system drive. This
machine has no moondream/parakeet-redux in any HF cache, so the first launch
would have pulled ~170 MB *inside* the hard timeout and reported a PhotonTimeout
— a false negative that reads exactly like "Photon hangs here", which is the
one thing this module exists to disprove. So the weights are now resolved from
the project's own populated cache on the models drive, the sidecar is launched
with HF_HUB_OFFLINE=1, and an absent (or interrupted) weight file raises
PhotonWeightsMissing — a SETUP problem with its own name — instead of a
download racing the wall clock. "Missing" is a thing the user fixes once;
"hung" is a property of the engine. The two must never be reported as each
other.

Env knobs (all optional; Photon is OFF unless VODRIP_PHOTON_ASR=1):

    VODRIP_PHOTON_ASR      "1" enables the accelerator. Default "0" — the
                           guaranteed path is the default on purpose.
    VODRIP_PHOTON_HF_HOME  HF cache root for the weights. Default: the
                           AI-models folder + photon-hf, so the cache
                           inherits the project's G: drive convention.
    VODRIP_PHOTON_WEIGHTS  A ready local weights directory, skipping cache
                           resolution entirely.
    VODRIP_PHOTON_VENV     Photon venv root. Default: resolved through the
                           AI-models folder (see venv_root).
    VODRIP_PHOTON_PYTHON   Full path to the sidecar interpreter, overriding
                           the venv layout (used by the tests).
    VODRIP_PHOTON_RUNNER   Full path to photon_runner.py (used by the tests).
    VODRIP_PHOTON_MODEL    Model id. Default "moondream/parakeet-redux".
    VODRIP_PHOTON_TIMEOUT  Hard per-run wall clock, seconds. Default 120.
    VODRIP_PHOTON_MIN_VRAM_BYTES
                           Free-VRAM floor before the accelerator is even
                           asked. Default 2.5 GiB (measured 1.75 GB resident
                           + 2.26 GiB peak reserved).
    VODRIP_PHOTON_MAX_FAILURES
                           Consecutive failures that open the circuit
                           breaker. Default 2.
    VODRIP_PHOTON_BREAKER_COOLDOWN
                           Seconds the breaker stays open. Default 900.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess as sp
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from services.os_services import _NO_WINDOW

logger = logging.getLogger(__name__)

# --- knobs ----------------------------------------------------------------

ENABLED_ENV = "VODRIP_PHOTON_ASR"
HF_HOME_ENV = "VODRIP_PHOTON_HF_HOME"
WEIGHTS_ENV = "VODRIP_PHOTON_WEIGHTS"
VENV_ENV = "VODRIP_PHOTON_VENV"
PYTHON_ENV = "VODRIP_PHOTON_PYTHON"
RUNNER_ENV = "VODRIP_PHOTON_RUNNER"
MODEL_ENV = "VODRIP_PHOTON_MODEL"
TIMEOUT_ENV = "VODRIP_PHOTON_TIMEOUT"
MIN_VRAM_ENV = "VODRIP_PHOTON_MIN_VRAM_BYTES"
MAX_FAILURES_ENV = "VODRIP_PHOTON_MAX_FAILURES"
BREAKER_COOLDOWN_ENV = "VODRIP_PHOTON_BREAKER_COOLDOWN"

# Sub-folder of the AI-models root that holds this accelerator's own state.
_PHOTON_HOME_NAME = "photon-hf"
# Where a Photon venv BELONGS (see venv_root for the resolution order).
_PHOTON_VENV_NAME = "photon-venv"
# The probe lane's install. Still a candidate — relocating a working 5.3 GB
# CUDA torch venv breaks its absolute paths — but never the preferred default,
# and warned about when it is what got picked: tooling on this box has reaped
# scratch directories under G:\Temp before.
_SANDBOX_VENVS = (r"G:\Temp\photonprobe\venv",)
# The one populated HF cache on this box, and the only place
# moondream/parakeet-redux exists. Kept in the list (rather than renamed into
# the durable path) so no existing model directory is moved. See hf_home().
_POPULATED_HF_CACHES = (r"G:\VOD.RIP-models\photonprobe-hf",)

# kestrel's loader contract for this model family: config + tokenizer +
# weights. ternary.json is required whenever the repo publishes it, because
# kestrel picks the ternary branch by that FILE existing — drop it and the same
# model id silently loads full precision instead.
_BASE_WEIGHT_FILES = ("config.json", "tokenizer.json", "model.safetensors")
_TERNARY_MANIFEST = "ternary.json"
# A checkpoint is never this small: 0 bytes is the signature of an interrupted
# transfer (an xet/hf half-download leaves a symlink to an empty blob).
_MIN_REAL_WEIGHT_BYTES = 1024
# Cap on the blob scan, so a pathological cache cannot turn resolution into a
# long directory walk on the batch path.
_BLOB_SCAN_CAP = 20000
# Written last, so a directory still being assembled is never "ready".
_READY_MARKER = ".ready"
DEFAULT_MODEL = "moondream/parakeet-redux"
# Load 10.9 s warm / 22.0 s cold + a 5x-slower first call (16.0 s, RTF 30)
# + 3.51 s per 8 min of audio. 120 s covers the cold path with room to spare
# and still caps a hang at two minutes.
DEFAULT_TIMEOUT_S = 120.0
# Measured: 1.75 GB resident after load, 2.26 GiB peak reserved.
DEFAULT_MIN_VRAM_BYTES = int(2.5 * 1024 ** 3)
DEFAULT_MAX_FAILURES = 2
DEFAULT_BREAKER_COOLDOWN_S = 900.0
# A batch shorter than this that returns no segments at all is a broken
# accelerator, not silence: the clips handed here are VAD speech regions.
_MIN_MEANINGFUL_AUDIO_SEC = 1.0
_KILL_GRACE_S = 5.0


class PhotonUnavailable(RuntimeError):
    """Photon cannot be used at all here (off, no venv, breaker open)."""


class PhotonTimeout(PhotonUnavailable):
    """The sidecar exceeded its hard wall clock and was killed."""

    def __init__(self, message: str, pid: Optional[int] = None) -> None:
        super().__init__(message)
        self.pid = pid


class PhotonWeightsMissing(PhotonUnavailable):
    """The model weights are not on disk. A SETUP problem, not a hang.

    Deliberately its own type, and a PhotonUnavailable so the caller's
    fallback still works. It exists so a missing cache can never be reported
    as a timeout: the user fixes this once, and no amount of engine debugging
    will change it.
    """


# The runner's own prefix for the same condition, so the specific error
# survives the process boundary in both directions.
WEIGHTS_MISSING_MARKER = "photon weights missing"


class PhotonProtocolError(PhotonUnavailable):
    """The sidecar answered with something we refuse to treat as a transcript."""


# --- module state ---------------------------------------------------------

_lock = threading.Lock()
_state: dict[str, Any] = {
    "attempts": 0,
    "ok": 0,
    "timeouts": 0,
    "errors": 0,
    "rejected": 0,
    "weights_missing": 0,
    "consecutive_failures": 0,
    "breaker_until": 0.0,
    "last_error": "",
    "last_ok_at": 0.0,
    "last_duration_s": 0.0,
    "audio_sec": 0.0,
}
# Set by log_status() so the on/off state is announced once per process.
_status_logged = False


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number - using %s", name, raw, default)
        return default
    return val if val > 0 else default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def enabled() -> bool:
    """The VODRIP_PHOTON_ASR kill switch. Default OFF."""
    return os.environ.get(ENABLED_ENV, "0").strip() == "1"


def min_vram_bytes() -> int:
    """Free-VRAM floor before the accelerator is asked for anything."""
    return int(_env_float(MIN_VRAM_ENV, DEFAULT_MIN_VRAM_BYTES))


def breaker_open() -> bool:
    """True while the circuit breaker is suppressing attempts."""
    with _lock:
        return time.monotonic() < _state["breaker_until"]


def stats() -> dict[str, Any]:
    """Counters for the log/status surface - a silent accelerator is the bug
    this module exists to avoid, so every attempt is counted.

    Also carries the on/off state and the weights verdict, so the one surface
    every consumer already reads answers "is it on, and could it even run?"
    without a second call site.
    """
    with _lock:
        snap = dict(_state)
    snap["enabled"] = enabled()
    snap["breaker_open"] = time.monotonic() < snap["breaker_until"]
    snap["python"] = str(python_path() or "")
    snap["model"] = model_name()
    snap["venv"] = str(venv_root())
    snap["weights"] = weights_status()
    return snap


def status_summary() -> dict:
    """Everything needed to answer "is Photon on, and why not?" in one call.

    Deliberately total: every field is present and JSON-able, and nothing here
    raises. That is what makes it safe to call from a health/status route
    where an exception would be worse than a missing field. Returns::

        {"on": bool, "enabled": bool, "reason": str, "breaker_open": bool,
         "attempts": int, "ok": int, "timeouts": int, "errors": int,
         "rejected": int, "weights_missing": int, "last_error": str,
         "model": str, "python": str, "venv": str, "venv_is_sandbox": bool,
         "weights": {"ok": bool, "dir": str, "reason": str}}

    ON BY DEFAULT IS FALSE AND THAT IS CORRECT until someone watches a real
    run; this function is how they check what it would do first.
    """
    snap = stats()
    reason = unavailable_reason()
    return {
        "on": bool(snap["enabled"] and reason == ""),
        "enabled": bool(snap["enabled"]),
        "reason": reason,
        "breaker_open": snap["breaker_open"],
        "attempts": snap["attempts"],
        "ok": snap["ok"],
        "timeouts": snap["timeouts"],
        "errors": snap["errors"],
        "rejected": snap["rejected"],
        "weights_missing": snap.get("weights_missing", 0),
        "last_error": snap["last_error"],
        "model": snap["model"],
        "python": snap["python"],
        "venv": snap["venv"],
        "venv_is_sandbox": venv_is_sandbox(),
        "weights": snap["weights"],
    }


def log_status() -> dict:
    """Log the on/off state and return the summary. Safe to call anywhere;
    costs a weights resolution, so prefer _announce_status_once() on a hot
    path.

    An accelerator that is quietly unusable is the failure this exists to
    prevent, so a missing weights cache is a WARNING here, not a silent
    False.
    """
    summary = status_summary()
    _log_summary(summary)
    return summary


def _log_summary(summary: dict) -> None:
    if summary["on"]:
        logger.info(
            "photon GPU ASR accelerator ON (opt-in %s=1): weights=%s venv=%s",
            ENABLED_ENV, summary["weights"]["dir"], summary["venv"],
        )
    elif summary["enabled"]:
        logger.warning(
            "photon GPU ASR accelerator ON but not usable: %s | weights: %s",
            summary["reason"] or "unknown",
            summary["weights"]["reason"] or summary["weights"]["dir"],
        )
    else:
        logger.info(
            "photon GPU ASR accelerator off (%s); sherpa-onnx is the engine%s",
            summary["reason"] or "disabled",
            (
                " | weights already ready at %s" % summary["weights"]["dir"]
                if summary["weights"]["ok"] else ""
            ),
        )
    if venv_is_sandbox():
        logger.warning(
            "photon venv resolved to the probe lane's scratch path %s - tooling "
            "on this box has reaped G:\\Temp before. Install a venv at %s (the "
            "durable default) and it takes over with no env var.",
            summary["venv"], models_root() / _PHOTON_VENV_NAME,
        )


def _announce_status_once() -> None:
    """Publish the on/off state the first time the accelerator is consulted.

    Hooked into available(), which every lane already calls, so the state is
    visible without a new registration and without a caller that might never
    run. Guarded FIRST and not after the work: available() runs per batch, and
    resolving the weights on every one of them would put a filesystem walk on
    the hot path.
    """
    global _status_logged
    with _lock:
        if _status_logged:
            return
        _status_logged = True
    try:
        _log_summary(status_summary())
    except Exception:
        logger.debug("photon status summary failed", exc_info=True)


def reset_stats() -> None:
    """Test hook: clear counters and close the breaker."""
    with _lock:
        _state.update({
            "attempts": 0, "ok": 0, "timeouts": 0, "errors": 0,
            "rejected": 0, "weights_missing": 0,
            "consecutive_failures": 0, "breaker_until": 0.0,
            "last_error": "", "last_ok_at": 0.0, "last_duration_s": 0.0,
            "audio_sec": 0.0,
        })


def _note_success(duration_s: float, audio_sec: float) -> None:
    with _lock:
        _state["ok"] += 1
        _state["consecutive_failures"] = 0
        _state["breaker_until"] = 0.0
        _state["last_ok_at"] = time.monotonic()
        _state["last_duration_s"] = duration_s
        _state["audio_sec"] += audio_sec
        _state["last_error"] = ""


def _note_failure(kind: str, message: str) -> None:
    """Count a failure and open the breaker once it repeats.

    The measured hang rate (2 of 3 launches) means a single failure is not
    evidence of a broken install - but repeated failures are, and continuing to
    pay a two-minute timeout per batch after the second one is worse than
    falling back for the rest of the process.
    """
    threshold = _env_int(MAX_FAILURES_ENV, DEFAULT_MAX_FAILURES)
    cooldown = _env_float(BREAKER_COOLDOWN_ENV, DEFAULT_BREAKER_COOLDOWN_S)
    with _lock:
        _state[kind] = _state.get(kind, 0) + 1
        _state["consecutive_failures"] += 1
        _state["last_error"] = message[:400]
        if _state["consecutive_failures"] >= threshold:
            _state["breaker_until"] = time.monotonic() + cooldown
            logger.warning(
                "photon accelerator disabled for %.0fs after %d consecutive "
                "failures (last: %s) - sherpa-onnx carries the GPU slot",
                cooldown, _state["consecutive_failures"], message[:200],
            )


# --- sidecar resolution ---------------------------------------------------

def models_root() -> Path:
    """The AI-models folder — the same root every other weight in this project
    resolves through (services.disk_hygiene.whisper_cache_dir, legacy name).

    Routing the Photon venv and HF cache through this helper rather than a
    second hardcoded drive letter is the point: the G: convention, the
    speed-first drive pick, and the user's "AI Models Folder" setting all
    apply here for free, and stay in one place to change.
    """
    try:
        from services.disk_hygiene import whisper_cache_dir

        return Path(whisper_cache_dir())
    except Exception:
        logger.debug("photon: whisper_cache_dir unavailable - trying the drive pick")
    try:
        from services.disk_hygiene import best_model_cache_drive

        drive = best_model_cache_drive()
        if drive:
            return Path(drive) / "VOD.RIP-models"
    except Exception:
        pass
    # Last resort: per-user and writable, per the split-runtime rule. Only
    # reached if the project helper cannot answer at all.
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VOD.RIP" / "models"


def _interpreter_exists(venv: Path) -> bool:
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe.is_file()


def venv_root() -> Path:
    """The venv that owns the Photon install.

    Resolution order, first hit wins:

      1. ``VODRIP_PHOTON_VENV`` — explicit, always honoured.
      2. ``<AI-models folder>/photon-venv`` — where a Photon venv belongs. Same
         helper, same G: convention, same user setting as every other model
         root in the project. This is the durable answer.
      3. the probe lane's ``G:\\Temp\\photonprobe\\venv``.

    (3) is a compatibility candidate, not a default: G:\\Temp is scratch space
    that tooling on this box has reaped before, and the venv holds a working
    5.3 GB CUDA torch whose absolute paths break if it is moved. So the
    working install is left exactly where it is, it is used when the durable
    one does not exist, and picking it logs a warning naming the durable
    path. When someone later installs the venv in (2), it takes over with no
    code change and no env var.

    When nothing exists the durable path is returned anyway, so
    ``unavailable_reason()`` can name the one place to install into.
    """
    override = os.environ.get(VENV_ENV, "").strip()
    if override:
        return Path(override)
    durable = models_root() / _PHOTON_VENV_NAME
    if _interpreter_exists(durable):
        return durable
    for candidate in _SANDBOX_VENVS:
        if _interpreter_exists(Path(candidate)):
            return Path(candidate)
    return durable


def venv_is_sandbox() -> bool:
    """True when the resolution landed on a scratch-path venv."""
    root = venv_root()
    return any(str(root) == c or str(root).startswith(c.rstrip("\\") + "\\")
               for c in _SANDBOX_VENVS)


def model_name() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def hf_home() -> Path:
    """The HF cache the weights are resolved from.

    ``VODRIP_PHOTON_HF_HOME`` wins. Otherwise the first of these that is
    actually populated, and failing all of them the durable default
    ``<AI-models folder>/photon-hf``:

      * ``<AI-models folder>/photon-hf`` — where a Photon cache BELONGS, via
        the same helper and the same onward-drive convention as every other
        weight in the project. First, so a configured models folder is
        always obeyed;
      * the probe lane's ``G:\\VOD.RIP-models\\photonprobe-hf`` — a STABLE
        model root on the drive AGENTS.md designates for model data, and the
        only place moondream/parakeet-redux is actually on disk. It stays in
        the list (renaming it would mean moving someone else's model
        directory) and is what the weights are read from today.

    Order matters and is deliberate: read from the cache that exists, write
    new state to the durable one. This is the whole of the first-run timeout
    fix — the sidecar is also launched with HF_HUB_OFFLINE=1, so even a miss
    fails immediately rather than downloading inside the wall clock.

    Note the project's models-root helper is speed-first and currently
    answers ``H:`` on this box, not the ``G:`` AGENTS.md documents, because H:
    is an NVMe with more free space. That is existing, correct project
    behaviour and not this module's to change; the compatibility candidate
    above is what keeps the existing weights in use meanwhile.
    """
    override = os.environ.get(HF_HOME_ENV, "").strip()
    if override:
        return Path(override)
    durable = models_root() / _PHOTON_HOME_NAME
    if (durable / "hub").is_dir():
        return durable
    for candidate in _POPULATED_HF_CACHES:
        root = Path(candidate)
        if (root / "hub").is_dir():
            return root
    return durable


# --- weights: resolved from disk, never fetched ---------------------------

def _repo_dirname(model: str) -> str:
    """huggingface_hub's on-disk name for a repo id."""
    return "models--" + model.strip("/").replace("/", "--")


def _tree_for(repo: Path, ref: str) -> dict:
    """The cache's own manifest for a revision: filename -> {size, lfs_sha256}.

    This is the authority on what a complete file looks like, and it is
    already on disk — so completeness is checked against what the hub
    published rather than against a guess.
    """
    try:
        data = json.loads((repo / "trees" / f"{ref}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def _snapshot_dir(model: str) -> tuple[Optional[Path], dict]:
    """The hub snapshot for *model* and its manifest, or (None, {})."""
    repo = hf_home() / "hub" / _repo_dirname(model)
    try:
        ref = (repo / "refs" / "main").read_text(encoding="utf-8").strip()
    except Exception:
        return None, {}
    if not ref:
        return None, {}
    snap = repo / "snapshots" / ref
    return (snap if snap.is_dir() else None), _tree_for(repo, ref)


def _required_files(tree: dict) -> tuple[str, ...]:
    names = list(_BASE_WEIGHT_FILES)
    if isinstance(tree, dict) and tree.get(_TERNARY_MANIFEST):
        names.append(_TERNARY_MANIFEST)
    return tuple(names)


def _expected_size(tree: dict, name: str) -> int:
    entry = tree.get(name) if isinstance(tree, dict) else None
    if not isinstance(entry, dict):
        return 0
    for key in ("lfs_size", "size"):
        val = entry.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return 0


def _file_is_real(path: Path, expected: int) -> bool:
    """True when *path* is a regular file carrying the expected real bytes.

    os.stat, not lstat: an HF snapshot entry is normally a SYMLINK into
    blobs/, and the broken half-download this guards against is exactly a
    symlink that resolves to a 0-byte blob. Existence alone is worthless
    here — a 0-byte model.safetensors passes an isfile() check and then
    fails deep inside safetensors as if the engine were broken.
    """
    try:
        if not path.is_file():
            return False
        size = os.stat(path).st_size
    except OSError:
        return False
    if expected > 0:
        return size == expected
    return size >= _MIN_REAL_WEIGHT_BYTES


def _weights_ok(root: Optional[Path], tree: dict) -> tuple[bool, str]:
    """(ready, why-not) for a candidate weights directory."""
    if root is None or not root.is_dir():
        return False, "no weights directory"
    bad = [
        name for name in _required_files(tree)
        if not _file_is_real(root / name, _expected_size(tree, name))
    ]
    if bad:
        return False, "missing or truncated: " + ", ".join(bad)
    return True, ""


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_blobs(hub: Path):
    """Regular files under the cache's blob store, bounded."""
    seen = 0
    root = hub / "hub" / "blobs"
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            seen += 1
            if seen > _BLOB_SCAN_CAP:
                logger.warning(
                    "photon: stopped scanning %s after %d blobs", root, _BLOB_SCAN_CAP
                )
                return
            yield Path(dirpath) / name


def _find_payload(model: str, name: str, expected: int, sha: str) -> Optional[Path]:
    """Locate the published bytes for *name* inside the same cache.

    An interrupted xet/hf transfer leaves the snapshot symlink pointing at an
    EMPTY repo blob while the reconstructed payload sits beside it under its
    own hash, complete. The bytes are therefore found by the size the hub
    manifest published and confirmed against its sha256 — the same cache, the
    same drive, no fetch. Anything that does not verify is not used: a
    half-written checkpoint is worse than a missing one, because it would
    fail as if the engine were at fault.
    """
    repo = hf_home() / "hub" / _repo_dirname(model)
    if sha:
        canonical = repo / "blobs" / sha[:2] / sha
        try:
            if canonical.is_file() and (
                expected <= 0 or canonical.stat().st_size == expected
            ):
                if not sha or _sha256(canonical) == sha:
                    return canonical
        except OSError:
            pass
    for blob in _iter_blobs(hf_home()):
        try:
            if expected > 0 and blob.stat().st_size != expected:
                continue
        except OSError:
            continue
        if not sha or _sha256(blob) == sha:
            return blob
    return None


def _link_or_copy(src: Path, dest: Path) -> None:
    """Materialize one weight file without moving or altering the source.

    A hardlink is the normal case — same drive, no data copied, and the
    probe lane's cache is left byte-for-byte untouched (nothing is moved,
    renamed or deleted). A copy is the fallback for a cache on another
    volume. Either way the file lands under a temp name and is renamed, so a
    reader never sees a half-written weight.

    The source is resolved to its REAL path first, and that is not cosmetic:
    an HF snapshot entry is a symlink with a path RELATIVE to its own
    directory, and a hardlink made to the link itself would carry that
    relative target to a new directory and dangle. os.link on Windows does
    not reliably dereference, so this resolves explicitly.
    """
    real = Path(os.path.realpath(src))
    staged = dest.with_name(dest.name + ".part")
    try:
        staged.unlink()
    except OSError:
        pass
    try:
        os.link(real, staged)
    except OSError:
        shutil.copy2(real, staged)
    os.replace(staged, dest)


def _prepare(dest: Path, sources: dict, tree: dict) -> None:
    """Assemble a ready-to-load weights directory from *sources*."""
    dest.mkdir(parents=True, exist_ok=True)
    for name, src in sources.items():
        target = dest / name
        if _file_is_real(target, _expected_size(tree, name)):
            continue
        _link_or_copy(src, target)
    # Last, so a directory still being assembled never passes _weights_ok().
    (dest / _READY_MARKER).write_text("ok\n", encoding="utf-8")


def resolve_weights(*, allow_prepare: bool = True) -> Path:
    """The local directory holding this model's weights, verified on disk.

    Order: ``VODRIP_PHOTON_WEIGHTS`` -> the cache snapshot -> a prepared copy
    assembled from whatever the cache already holds.

    Raises PhotonWeightsMissing when the weights are not on disk. Nothing here
    ever downloads, and nothing here waits: this is the distinction the
    accelerator exists to keep honest.
    """
    model = model_name()
    override = os.environ.get(WEIGHTS_ENV, "").strip()
    if override:
        root = Path(override)
        ok, why = _weights_ok(root, {})
        if not ok:
            raise PhotonWeightsMissing(f"{WEIGHTS_ENV}={root} - {why}")
        return root

    snap, tree = _snapshot_dir(model)
    if snap is not None:
        ok, _ = _weights_ok(snap, tree)
        if ok:
            return snap

    prepared = hf_home() / "prepared" / _repo_dirname(model)
    ok, _ = _weights_ok(prepared, tree)
    if ok:
        return prepared

    if snap is None:
        raise PhotonWeightsMissing(
            f"{WEIGHTS_MISSING_MARKER}: no {model} snapshot under "
            f"{hf_home() / 'hub'} (set {HF_HOME_ENV} to a populated cache). "
            f"A setup problem, not a hang - sherpa-onnx is unaffected."
        )
    if not allow_prepare:
        raise PhotonWeightsMissing(
            f"{WEIGHTS_MISSING_MARKER}: {snap} - {_weights_ok(snap, tree)[1]}"
        )

    sources: dict[str, Path] = {}
    for name in _required_files(tree):
        expected = _expected_size(tree, name)
        entry = snap / name
        if _file_is_real(entry, expected):
            sources[name] = entry
            continue
        entry_meta = tree.get(name) or {}
        sha = str(entry_meta.get("lfs_sha256") or "") if isinstance(entry_meta, dict) else ""
        payload = _find_payload(model, name, expected, sha)
        if payload is None:
            raise PhotonWeightsMissing(
                f"{WEIGHTS_MISSING_MARKER}: {name} in {snap} is "
                f"{_describe(entry, expected)} and {hf_home()} holds no "
                f"verified copy of the {expected}-byte payload. Restore the "
                f"cache or set {WEIGHTS_ENV}. A setup problem, not a hang."
            )
        sources[name] = payload
    _prepare(prepared, sources, tree)
    ok, why = _weights_ok(prepared, tree)
    if not ok:
        raise PhotonWeightsMissing(
            f"{WEIGHTS_MISSING_MARKER}: prepared {prepared} is still {why}"
        )
    logger.info(
        "photon weights resolved from %s -> %s (no network)", hf_home(), prepared
    )
    return prepared


def _describe(path: Path, expected: int) -> str:
    try:
        size = os.stat(path).st_size
    except OSError:
        return "absent"
    return f"{size} of {expected} bytes"


def weights_ready() -> bool:
    """Cheap: can the sidecar load the weights right now?"""
    try:
        resolve_weights()
    except PhotonWeightsMissing:
        return False
    except Exception:
        return False
    return True


def weights_status() -> dict:
    """The weights verdict for a status surface. Never raises."""
    out: dict[str, Any] = {"ok": False, "dir": "", "reason": ""}
    try:
        out["dir"] = str(resolve_weights())
        out["ok"] = True
    except PhotonWeightsMissing as exc:
        out["reason"] = str(exc)
    except Exception as exc:
        out["reason"] = f"{WEIGHTS_MISSING_MARKER}: {exc}"
    return out


def runner_path() -> Path:
    """The sidecar script. Ships next to this module in the app tree; the
    venv does not need the app on sys.path (the script is stdlib+numpy only)."""
    override = os.environ.get(RUNNER_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "photon_runner.py"


def python_path() -> Optional[Path]:
    """The interpreter that owns the Photon install, or None when absent."""
    override = os.environ.get(PYTHON_ENV, "").strip()
    if override:
        return Path(override) if Path(override).is_file() else None
    exe = venv_root() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.is_file() else None


def available() -> bool:
    """Cheap check: the switch is on, the breaker is closed, and the sidecar's
    interpreter + script are both on disk. Says nothing about CUDA working -
    only a real run can, and that is what the timeout is for.

    The weights are deliberately NOT part of this gate. It is consulted per
    batch, and a missing cache is a setup fact the user fixes once: folding
    it in here would turn every batch into a silent no-op with no error
    anywhere, which is the exact failure mode this module was written to
    avoid. The weights instead get their own named error
    (PhotonWeightsMissing) at the point of use, and the first consultation
    logs the verdict via log_status().
    """
    _announce_status_once()
    if not enabled() or breaker_open():
        return False
    return python_path() is not None and runner_path().is_file()


def unavailable_reason() -> str:
    """Why the accelerator is not being used, for logs and status."""
    if not enabled():
        return f"{ENABLED_ENV}=0 (default)"
    if breaker_open():
        with _lock:
            left = max(0.0, _state["breaker_until"] - time.monotonic())
        return f"circuit breaker open for another {left:.0f}s (last: {_state['last_error'][:160]})"
    if python_path() is None:
        return f"no sidecar interpreter (set {PYTHON_ENV} or {VENV_ENV})"
    if not runner_path().is_file():
        return f"sidecar script missing: {runner_path()}"
    return ""


# --- process control ------------------------------------------------------

def _process_gone(pid: int) -> bool:
    """True when the pid is no longer a live process.

    os.kill(pid, 0) is NOT usable on Windows - there it calls TerminateProcess
    with the signal as the exit code, so a liveness probe with signal 0 would
    KILL the very process we are checking.

    Two Windows traps this avoids, both found by asserting the pid was gone
    after a real kill (it was not):
      * ctypes' default restype is c_int, so a 64-bit HANDLE gets truncated -
        WaitForSingleObject on a truncated handle returns WAIT_FAILED for a
        perfectly live process. The signatures are declared explicitly.
      * OpenProcess still SUCCEEDS for a terminated process whose handle is
        still open (Popen holds one until it is reaped), so a successful open
        is NOT liveness. The exit code is: STILL_ACTIVE (259) only while the
        process is running.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
            ]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return True  # no such process
            code = wintypes.DWORD()
            try:
                ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            finally:
                kernel.CloseHandle(handle)
            if not ok:
                return True  # cannot query -> treat as gone; we cannot reach it
            return code.value != 259  # STILL_ACTIVE
        except Exception:
            return False  # probe unavailable: assume alive (fail-safe, never
                          # report a live orphan as dead)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def kill_tree(pid: int) -> bool:
    """Kill a sidecar and anything it started; True when the pid is gone.

    The Photon engine runs its CUDA worker in a THREAD of the sidecar process,
    so the child is the only process to kill - but a tree kill is the honest
    default for a child we do not control the shape of, and costs one call.
    """
    gone = _process_gone(pid)
    if gone:
        return True
    if os.name == "nt":
        try:
            sp.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=_KILL_GRACE_S,
                creationflags=_NO_WINDOW,
            )
        except Exception:
            pass
    try:
        # 9 == SIGKILL on POSIX; on Windows os.kill ignores the signal for
        # termination purposes and calls TerminateProcess(handle, 9). Not
        # signal.SIGKILL — that name does not exist on Windows.
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    # Bounded settle: the caller's budget is one wait, never an unbounded spin.
    deadline = time.monotonic() + _KILL_GRACE_S
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.05)
    return _process_gone(pid)


# --- scratch --------------------------------------------------------------

def _scratch_dir() -> "Any":
    """A writable scratch root for the PCM handoff.

    AGENTS.md: heavy project data lives on the stable model drive, never C: and
    never G:\\Temp (pytest reaps vodrip-* dirs at session end). The AI-models
    root is the G: stable root; if it is unusable, fall back to the platform
    temp dir rather than failing the batch.
    """
    try:
        from services.disk_hygiene import whisper_cache_dir

        root = Path(whisper_cache_dir()) / "photon-asr" / "scratch"
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return root
    except Exception:
        return Path(tempfile.mkdtemp(prefix="vodrip-photon-"))


# --- the run --------------------------------------------------------------

def _sidecar_env() -> dict:
    """The environment the sidecar runs in: pointed at the project cache, and
    forbidden from fetching anything.

    HF_HOME alone is the fix (the default is ~/.cache/huggingface on the
    system drive, which holds no moondream weights on this box). The two
    OFFLINE flags are the guarantee: if any code path inside the venv ever
    does reach for the Hub anyway, it raises immediately instead of
    downloading a 170 MB checkpoint inside the 120 s wall clock and reporting
    a timeout that looks exactly like a Photon hang.
    """
    env = os.environ.copy()
    env["HF_HOME"] = str(hf_home())
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def run_sidecar(
    *,
    pcm: Any,
    clips: list[tuple[float, float]],
    sample_rate: int,
    audio_sec: float = 0.0,
    python: Optional[Path] = None,
    runner: Optional[Path] = None,
    timeout_s: Optional[float] = None,
) -> list[dict]:
    """Run one sidecar process over one batch. Raises on every failure mode.

    Returns one validated entry per clip: ``{"language": str|None,
    "segments": [...]}`` with CLIP-RELATIVE timestamps, in input order.

    Raises PhotonUnavailable / PhotonTimeout / PhotonProtocolError /
    PhotonWeightsMissing. The caller treats any of those as "use sherpa-onnx".
    """
    exe = python if python is not None else python_path()
    script = runner if runner is not None else runner_path()
    if exe is None or not Path(exe).is_file():
        raise PhotonUnavailable(f"sidecar interpreter not found: {exe}")
    if not Path(script).is_file():
        raise PhotonUnavailable(f"sidecar script not found: {script}")
    if not clips:
        raise PhotonUnavailable("no clips to transcribe")

    # Resolved here, not inside the sidecar, and reported to it as a PATH: the
    # venv's kestrel loader short-circuits on an existing local directory, so
    # a local path is the difference between loading 170 MB off this drive in
    # a second and a 170 MB download inside the timeout. When it cannot be
    # resolved the sidecar is still launched, because the RUNNER is what owns
    # the condition - it is the process that would have to load these weights -
    # and it answers with the specific weights-missing envelope before it
    # imports 5 GB of torch. Either way nothing here waits on the network.
    try:
        weights = str(resolve_weights())
    except PhotonWeightsMissing:
        weights = ""

    budget = timeout_s if timeout_s is not None else _env_float(
        TIMEOUT_ENV, DEFAULT_TIMEOUT_S
    )
    workdir = Path(_scratch_dir()) / f"run-{os.getpid()}-{threading.get_ident()}"
    pcm_path = workdir / "pcm.f32"
    req_path = workdir / "req.json"
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        pcm.tofile(str(pcm_path))  # float32 mono, exactly kestrel's raw-PCM input
        req_path.write_text(
            json.dumps({
                "model": model_name(),
                "weights_dir": weights,
                "sample_rate": int(sample_rate),
                "timestamps": "word",
                "pcm_path": str(pcm_path),
                "pcm_dtype": "float32",
                "clips": [
                    {"start": float(cs), "end": float(ce)} for cs, ce in clips
                ],
            }),
            encoding="utf-8",
        )
    except Exception as exc:
        raise PhotonUnavailable(f"sidecar scratch write failed: {exc}") from exc

    flags = _NO_WINDOW
    if os.name == "nt":
        # Own process group: a console Ctrl+C aimed at the worker must not
        # race our timeout path, and the child never shares a console.
        flags |= getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)

    started = time.monotonic()
    try:
        proc = sp.Popen(
            [str(exe), str(script), str(req_path)],
            stdin=sp.DEVNULL,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            creationflags=flags,
            env=_sidecar_env(),
        )
    except Exception as exc:
        raise PhotonUnavailable(f"sidecar spawn failed: {exc}") from exc

    try:
        out, err = proc.communicate(timeout=budget)
    except sp.TimeoutExpired:
        # The hang path. Kill BEFORE reporting: an orphan holding ~2 GB of
        # VRAM is exactly the failure this module has to prevent.
        pid = proc.pid
        gone = kill_tree(pid)
        try:
            proc.communicate(timeout=_KILL_GRACE_S)
        except Exception:
            pass
        _note_failure(
            "timeouts",
            f"no result within {budget:.0f}s (pid {pid}, killed={gone})",
        )
        raise PhotonTimeout(
            f"photon sidecar hung: no result within {budget:.0f}s "
            f"(pid {pid}, terminated={gone})",
            pid=pid,
        ) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    duration = time.monotonic() - started
    stderr_tail = (err or b"").decode("utf-8", "replace").strip()[-600:]
    if audio_sec <= 0:
        # The empty-transcript guard below is only honest if it knows how much
        # audio it was given; derive it here rather than trusting every caller
        # to pass it (forgetting it would silently disable the guard).
        audio_sec = float(getattr(pcm, "size", 0)) / float(sample_rate or 1)
    stdout_text = (out or b"").decode("utf-8", "replace").strip()
    payload = None
    if stdout_text:
        try:
            payload = json.loads(stdout_text.splitlines()[-1])
        except Exception:
            payload = None
    # A failure ENVELOPE is more specific than the exit code, and it is read
    # first on purpose: the runner's _fail() always exits non-zero, so
    # checking returncode first would discard the one message that names the
    # actual problem — which is the whole reason a missing cache must not be
    # reported as a hang.
    if isinstance(payload, dict) and payload.get("ok") is not True:
        msg = str(payload.get("error"))
        if WEIGHTS_MISSING_MARKER in msg:
            _note_failure("weights_missing", msg)
            raise PhotonWeightsMissing(msg)
        _note_failure("errors", f"sidecar reported failure: {msg}")
        raise PhotonUnavailable(f"photon sidecar reported: {msg}")
    if proc.returncode != 0 or not stdout_text:
        _note_failure(
            "errors",
            f"sidecar exit {proc.returncode}: {stderr_tail or 'no stdout'}",
        )
        raise PhotonUnavailable(
            f"photon sidecar exit {proc.returncode}: {stderr_tail or 'no stdout'}"
        )
    if payload is None:
        _note_failure("errors", "unparseable response")
        raise PhotonProtocolError("photon response is not JSON")
    clips_out = payload.get("clips")
    if not isinstance(clips_out, list) or len(clips_out) != len(clips):
        _note_failure("rejected", "clip count mismatch in response")
        raise PhotonProtocolError(
            f"photon returned {len(clips_out) if isinstance(clips_out, list) else '?'} "
            f"clips for {len(clips)} requests"
        )
    if audio_sec > _MIN_MEANINGFUL_AUDIO_SEC and not any(
        (c.get("segments") if isinstance(c, dict) else None) for c in clips_out
    ):
        # A whole batch of VAD'd speech returning nothing is a broken
        # accelerator, not silence. Falling back costs a slower, correct pass.
        _note_failure("rejected", "empty transcript for a non-empty batch")
        raise PhotonProtocolError(
            f"photon returned no segments for {audio_sec:.1f}s of speech"
        )
    _note_success(duration, audio_sec)
    logger.info(
        "photon sidecar ok: %d clips, %.1fs audio in %.2fs (%.1fx realtime)",
        len(clips_out), audio_sec, duration,
        (audio_sec / duration) if duration > 0 else 0.0,
    )
    return clips_out


def transcribe_batch(
    audio: Any,
    clips: list[tuple[float, float]],
    *,
    sample_rate: int,
    python: Optional[Path] = None,
    runner: Optional[Path] = None,
    timeout_s: Optional[float] = None,
) -> list[dict]:
    """Public entry: one Photon batch, or an exception. Never returns None -
    the fallback decision belongs to the caller, where the sherpa-onnx call
    it guards is visible."""
    with _lock:
        _state["attempts"] += 1
    return run_sidecar(
        pcm=audio,
        clips=list(clips),
        sample_rate=sample_rate,
        audio_sec=float(getattr(audio, "size", 0)) / float(sample_rate or 1),
        python=python,
        runner=runner,
        timeout_s=timeout_s,
    )
