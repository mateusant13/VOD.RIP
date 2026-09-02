# -*- mode: python ; coding: utf-8 -*-
"""Shared PyInstaller spec helpers for VOD.RIP.

The base app specs (``vod-rip.spec`` onedir, ``vod-rip-onefile.spec``) and the
optional ASR runtime spec (``vod-rip-asr.spec``) import the helpers here so the
ffmpeg / Node / bgutil / silero-vad / ASR-GPU / hidden-import logic lives in one
place instead of being copy-pasted three times.

``vod-rip-asr.spec`` imports the *heavy* ASR helpers (``asr_gpu_binaries``,
``silero_vad_datas``, ``asr_hidden_imports``); the base specs deliberately omit
them so ``VOD-RIP.exe`` boots without ever importing torch / sherpa-onnx.

Each spec adds this module's directory to ``sys.path`` before importing (PyInstaller
does not guarantee the spec directory is importable).
"""

import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.getcwd())
BACKEND_DIR = PROJECT_ROOT / "backend"
STATIC_DIR = BACKEND_DIR / "static"
ASSETS_DIR = PROJECT_ROOT / "assets"
BUILD_DIR = PROJECT_ROOT / "build"
EXTERNAL_DIR = BUILD_DIR / "external"
VENDOR_DIR = PROJECT_ROOT / "vendor"
ICON_ICO = ASSETS_DIR / "icon.ico"
ICON_ICNS = ASSETS_DIR / "icon.icns"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def spec_version() -> str:
    """Canonical version from ``backend/services/_version.py``.

    Mirrors the regex the base spec already used: parse without importing
    (PyInstaller runs the spec before ``sys.path`` is wired up).
    """
    version = "1.0.0"
    version_py = BACKEND_DIR / "services" / "_version.py"
    if version_py.is_file():
        try:
            m = re.search(
                r'__version__\s*=\s*["\']([^"\']+)["\']',
                version_py.read_text(encoding="utf-8", errors="replace"),
            )
            if m:
                version = m.group(1)
        except Exception:  # noqa: BLE001 - best-effort, never fail the build
            version = "1.0.0"
    return version


def ffmpeg_binaries():
    if not EXTERNAL_DIR.is_dir():
        return []
    result = []
    for name in ("ffmpeg", "ffprobe"):
        for path in (
            EXTERNAL_DIR / f"{name}.exe",
            EXTERNAL_DIR / f"{name}.bin",
            EXTERNAL_DIR / name,
        ):
            if path.is_file():
                result.append((str(path), "."))
    return result


def bundled_node_binaries():
    """Bundle the private Node 20 runtime from build/external/node.exe.

    Ships under ``<exe-dir>/runtime/node.exe`` so the YouTube PO Token
    (bgutil) subprocess can spawn without requiring Node on PATH. Produced by
    ``scripts/download-node.ps1``. Skips silently when the artefact is absent.
    """
    if not IS_WIN:
        return []
    node_exe = EXTERNAL_DIR / "node.exe"
    if not node_exe.is_file():
        return []
    return [(str(node_exe), "runtime")]


def bundled_bgutil_datas():
    """Bundle the bgutil-ytdlp-pot-provider server under ``runtime/bgutil-pot/``.

    Layout matches ``youtube_pot_service.frozen_runtime_paths``:

        <exe-dir>/runtime/bgutil-pot/server/build/main.js
        <exe-dir>/runtime/bgutil-pot/server/node_modules/...

    The server subtree is self-contained (``npm ci`` + ``npm run build`` already
    executed by ``scripts/build-bgutil-bundle.ps1``).
    """
    server_dir = EXTERNAL_DIR / "bgutil-pot" / "server"
    if not server_dir.is_dir():
        return []
    return [(str(server_dir), "runtime/bgutil-pot/server")]


def silero_vad_datas():
    """Bundle silero-vad's weights (silero_vad/data/*.onnx|*.jit).

    model.py resolves them via importlib.resources.files("silero_vad.data"),
    which modulegraph never sees — without this the frozen app dies with
    'No module named silero_vad.data' on every VAD call (archive + live)."""
    try:
        from PyInstaller.utils.hooks import collect_data_files

        return collect_data_files("silero_vad")
    except Exception:  # noqa: BLE001
        # ponytail: best-effort — a missing wheel just loses VAD weights
        return []


def asr_gpu_binaries():
    """Bundle the on-device ASR runtime so packaged users transcribe with
    zero commands.

    Ships three things the modulegraph does not follow on its own:
      * sherpa-onnx's own onnxruntime + CUDA providers DLLs
        (``sherpa_onnx/lib/*.dll`` — the +cuda wheel's bundled ORT CUDA EP);
      * ctranslate2's native libs (faster-whisper's inference backend);
      * the ``nvidia/<pkg>/bin|lib`` CUDA runtime dirs (cublas, cufft,
        curand, cudnn, nvjitlink, cuda_runtime) so
        ``archive_transcribe._ensure_cuda_libs`` finds them in the frozen
        tree — it probes ``sys._MEIPASS`` (onefile) / ``sys.prefix``
        (onedir) roots, which is exactly where these land.

    CPU-only build hosts (CI without the GPU wheels) return [] — the runtime
    probe then degrades to the CPU EP, which is the designed fallback. The
    +cuda wheel itself is installed by the build (``backend/requirements-gpu.txt``).
    """
    result = []
    import site as _site  # noqa: PLC0415 - spec top-level import is fine too

    subdir = "bin" if os.name == "nt" else "lib"
    for root in _site.getsitepackages():
        root = Path(root)
        # sherpa-onnx native runtime (onnxruntime + CUDA providers)
        sherpa_lib = root / "sherpa_onnx" / "lib"
        if sherpa_lib.is_dir():
            result.append((str(sherpa_lib), "sherpa_onnx/lib"))
        # ctranslate2 native libs (faster-whisper backend)
        ct2 = root / "ctranslate2"
        if ct2.is_dir():
            for f in ct2.iterdir():
                if f.suffix.lower() in (".dll", ".so", ".pyd"):
                    result.append((str(f), "ctranslate2"))
        # nvidia-*-cu12 wheels: DLLs live in nvidia/<pkg>/bin (win) or
        # nvidia/<pkg>/lib (posix) — mirror _ensure_cuda_libs's probe.
        nvidia_root = root / "nvidia"
        if nvidia_root.is_dir():
            for pkg_dir in sorted(nvidia_root.iterdir()):
                if not pkg_dir.is_dir():
                    continue
                # Skip CUDA 13 wheels (nvidia-cudnn-cu13, nvidia-cublas 13.x,
                # nvidia-cuda-runtime 13.x, ...): the stack here is pinned to
                # cu12 (torch 2.7+cu128, sherpa-onnx +cuda12.cudnn9) and the
                # cu13 trees can slip in via an unrelated wheel's extra
                # (onnxruntime-gpu[extra] pulled nvidia-cudnn-cu13 once, +850MB).
                if pkg_dir.name == "cu13" or pkg_dir.name.endswith("-cu13"):
                    continue
                lib_dir = pkg_dir / subdir
                if lib_dir.is_dir():
                    result.append(
                        (str(lib_dir), f"nvidia/{pkg_dir.name}/{subdir}")
                    )
    # Dedupe by (source, dest) — getsitepackages can repeat the same root.
    return list(dict.fromkeys(result))


def base_hidden_imports() -> list[str]:
    """Hidden imports for the BASE app (no on-device ASR stack).

    ``services.archive_transcribe`` lives in the optional ASR runtime, not
    here — the base app boots without importing torch/sherpa-onnx.
    """
    imports = [
        "uvicorn",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.logging",
        "fastapi",
        "pydantic",
        "yt_dlp",
        "yt_dlp.extractor",
        "yt_dlp.downloader",
        "yt_dlp.postprocessor",
        "curl_cffi",
        "curl_cffi.requests",
        "main",
        "services.twitch_gql_service",
        "services.kick_models",
        "services.ytdlp_service",
        "services.gpu_detect",
        "services.size_estimate",
        "services.kick_api_service",
        "services.single_instance",
        "services.webview2_setup",
        "services.preview_service",
        "services.download_manager",
        "services.download_cleanup",
        "services.settings",
        "services.tray_service",
        "services.app_lifecycle",
        "services.server_lifecycle",
        "services.shutdown_util",
        "services.updater",
        "services.crash_handler",
        "services._version",
        "services.autostart",
        "services.youtube_pot_service",
        "models.schemas",
        # Semantic search is optional; its ONNX model and runtime are installed
        # separately only when that feature is used.
        "webview",
        "PIL",
        "PIL.Image",
        "pystray",
        "tkinter",
        "tkinter.filedialog",
    ]
    if IS_WIN:
        imports += [
            "webview.platforms.edgechromium",
            "pystray._win32",
        ]
    elif IS_MAC:
        imports += [
            "webview.platforms.cocoa",
            "pystray._darwin",
        ]
    else:
        imports += [
            "webview.platforms.gtk",
            "pystray._appindicator",
            "pystray._gtk",
        ]
    return imports


def asr_hidden_imports() -> list[str]:
    """Hidden imports for the ASR runtime (archive worker + on-device stack).

    The engine modules are imported lazily inside ``services.archive_transcribe``
    (and its stages), so modulegraph never sees them without this explicit list.
    ``services.archive_transcribe`` must be listed so the worker graph
    (archive_db / archive_events / archive_scheduler / archive_embed / ...) is
    pulled into the runtime bundle.
    """
    return [
        # worker supervisor entry graph
        "worker_server",
        "rotating_log",
        "services.archive_transcribe",
        "services.archive_db",
        "services.archive_scheduler",
        "services.archive_events",
        "services.archive_embed",
        "services.transcript_fix",
        # on-device ASR / embedding / event engines (lazy imports)
        "sherpa_onnx",
        "torch",
        "torchaudio",
        "ctranslate2",
        "onnxruntime",
        "panns_inference",
        "silero_vad",
    ]


def win_version_resource(exe_name: str, file_description: str) -> object | None:
    """Windows version resource for the given EXE.

    Self-consistent FileVersion / ProductName (generated from the single source
    of truth, ``spec_version()``) is an AV-heuristic trust marker. Best-effort:
    a resource problem must never fail the build. Returns ``None`` on non-Windows
    or on any error (caller falls back to no version resource).
    """
    if not IS_WIN:
        return None
    try:
        from PyInstaller.utils.win32.versioninfo import (  # noqa: PLC0415
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )

        v = tuple(int(x) for x in spec_version().split("."))
        v = (v + (0, 0, 0, 0))[:4]
        ver_str = ".".join(map(str, v))
        return VSVersionInfo(
            ffi=FixedFileInfo(
                filevers=v,
                prodvers=v,
                mask=0x3F,
                flags=0x0,
                OS=0x40004,
                fileType=0x1,
                subtype=0x0,
                date=(0, 0),
            ),
            kids=[
                StringFileInfo(
                    [
                        StringTable(
                            "040904B0",
                            [
                                StringStruct("CompanyName", "VOD.RIP"),
                                StringStruct("FileDescription", file_description),
                                StringStruct("FileVersion", ver_str),
                                StringStruct("InternalName", exe_name),
                                StringStruct("LegalCopyright", "Copyright (c) mateusant13"),
                                StringStruct("OriginalFilename", exe_name.upper() + ".EXE"),
                                StringStruct("ProductName", "VOD.RIP"),
                                StringStruct("ProductVersion", ver_str),
                            ],
                        )
                    ]
                ),
                VarFileInfo([VarStruct("Translation", [1033, 1200])]),
            ],
        )
    except Exception:  # noqa: BLE001 - resource must never fail the build
        return None
