"""asr_worker.py — entrypoint for the optional VOD-RIP-ASR.exe runtime.

The on-device ASR stack (torch / torchaudio / sherpa-onnx / ctranslate2 /
onnxruntime / panns-inference / silero-vad) is deliberately NOT bundled into
the base ``VOD-RIP.exe`` (see ``vod-rip.spec``): the base app boots without
importing any of it. This executable is the companion runtime, produced by
``vod-rip-asr.spec``. The base app installs it on demand (see
``services.asr_runtime``) under the AI-models folder — the same root that
hosts model weights — resolving the payload from a versioned runtime
archive (``scripts/deploy-asr-runtime.mjs`` stages it under
``dist/runtimes/asr/<version>/`` for the release workflow).

Modes (argv, first match wins):

  ``--health``
      Probe the ASR stack + CUDA and print one JSON line on stdout, then
      exit 0 when usable / 1 when a required engine is missing. This is a
      diagnostic mode for support and release smoke checks.

  ``--serve --port <port>``
      Run a small loopback HTTP service for live-caption windows. The base
      app starts this only after the optional runtime has been installed.

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
    """Probe the ASR stack and print one JSON status line."""
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
    print(
        json.dumps(
            {"status": "ok" if ok else "degraded", "cuda": cuda, "modules": modules},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if ok else 1


def _transcribe_window_server(port: int) -> int:
    """Serve float32 16 kHz windows over loopback for live captions."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self.send_error(404)
                return
            self._json({"ok": True})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/transcribe":
                self.send_error(404)
                return
            try:
                import numpy as np
                from services import archive_transcribe as at

                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 64 * 1024 * 1024:
                    raise ValueError("invalid audio payload size")
                audio = np.frombuffer(self.rfile.read(length), dtype=np.float32)
                speech = at.vad_speech_seconds(audio)
                if not speech:
                    self._json({"text": "", "lang": None})
                    return
                results = at._transcribe_batch_parakeet(
                    at._parakeet_model(), audio, speech, None
                )
                texts: list[str] = []
                detected_lang = None
                for items, lang in results:
                    if detected_lang is None and lang:
                        detected_lang = lang
                    for item in items:
                        text = (item.get("text") or "").strip()
                        if text:
                            texts.append(text)
                self._json({"text": " ".join(texts), "lang": detected_lang})
            except Exception as exc:  # worker errors must not kill the server
                self._json({"error": str(exc)[:500]}, status=503)

        def _json(self, body: dict, status: int = 200) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(json.dumps({"port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


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
    if "--serve" in sys.argv:
        try:
            index = sys.argv.index("--port")
            port = int(sys.argv[index + 1])
        except (ValueError, IndexError):
            print("--serve requires --port", file=sys.stderr)
            return 2
        if not 0 < port < 65536:
            print("--port must be between 1 and 65535", file=sys.stderr)
            return 2
        return _transcribe_window_server(port)
    if "--archive-worker" in sys.argv:
        return _archive_worker()
    if "--transcribe-once" in sys.argv:
        return _transcribe_once()
    print("usage: VOD-RIP-ASR.exe --health | --serve --port PORT | --archive-worker | --transcribe-once")
    return 2


if __name__ == "__main__":
    sys.exit(main())
