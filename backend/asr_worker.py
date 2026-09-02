"""asr_worker.py — entrypoint for the optional VOD-RIP-ASR.exe runtime.

The on-device ASR stack (torch / torchaudio / sherpa-onnx / ctranslate2 /
onnxruntime / panns-inference / silero-vad) is deliberately NOT bundled into
the base ``VOD-RIP.exe`` (see ``vod-rip.spec``): the base app boots without
importing any of it. This executable is the companion runtime, produced by
``vod-rip-asr.spec``, and is placed under a *versioned runtime directory*
next to the base app so the main app can discover and spawn it.

Modes (argv, first match wins):

  ``--health``
      Probe the ASR stack + CUDA and print one JSON line on stdout, then
      exit 0 when usable / 1 when a required engine is missing. The base
      app runs this before launching a transcription job to decide whether
      the runtime is present and healthy.

  ``--archive-worker``
      Run the existing supervised archive worker (``worker_server.main``) —
      the same queue-drain supervisor the dev tree runs, minus any GUI.
      One command for VOD.RIP's background worker; survives app close and
      crashes.

  ``--transcribe-once``
      Supervised-child dispatch. ``worker_server.main`` spawns this same
      frozen executable with ``--transcribe-once`` (the EXE cannot run
      ``python -m``), so the child drains the archive_jobs queue once and
      exits 0.

No mode: print usage and exit 2.
"""
from __future__ import annotations

import sys


def _health() -> int:
    """Probe the ASR stack and print one JSON status line.

    Imports are deliberately lazy and individually guarded so a missing
    engine (e.g. a CPU-only build without the CUDA wheel) degrades the
    report instead of crashing the probe. Output shape is stable for the
    base app to parse: ``{"status": "ok"|"degraded", "cuda": bool, "modules": {...}}``.
    """
    import importlib
    import json

    engine_modules = (
        "torch",
        "torchaudio",
        "sherpa_onnx",
        "ctranslate2",
        "onnxruntime",
        "panns_inference",
        "silero_vad",
    )
    modules: dict[str, object] = {}
    for name in engine_modules:
        try:
            mod = importlib.import_module(name)
            modules[name] = getattr(mod, "__version__", "present")
        except Exception as exc:  # noqa: BLE001 - probe must never crash
            modules[name] = f"missing: {type(exc).__name__}: {exc}"

    cuda = False
    try:
        import torch

        cuda = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        cuda = False

    ok = all(not isinstance(v, str) or not v.startswith("missing:") for v in modules.values())
    status = "ok" if ok else "degraded"
    print(
        json.dumps(
            {"status": status, "cuda": cuda, "modules": modules},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if ok else 1


def _archive_worker() -> int:
    """Run the existing supervised archive worker (no GUI)."""
    from worker_server import main as worker_main  # noqa: PLC0415 - local for spec modulegraph

    return worker_main()


def _transcribe_once() -> int:
    """Supervised-child dispatch: drain the archive_jobs queue once."""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from services import archive_db  # noqa: PLC0415

    if archive_db.worker_live(age_s=45):
        return 0
    from services.archive_transcribe import _set_worker_low_priority, run_worker  # noqa: PLC0415

    _set_worker_low_priority()  # background work — don't stutter the box
    run_worker(once=True, poll_interval=2.0)
    return 0


def main() -> int:
    if "--health" in sys.argv:
        return _health()
    if "--archive-worker" in sys.argv:
        return _archive_worker()
    if "--transcribe-once" in sys.argv:
        return _transcribe_once()
    print(
        "VOD-RIP-ASR: usage: --health | --archive-worker | --transcribe-once",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
