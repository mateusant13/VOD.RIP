# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller ONE-FILE spec (Windows).

Produces a single self-extracting ``VOD-RIP.exe``: the whole app payload
(including ffmpeg, the Node runtime, the bgutil POT server and the cookie
bridge extension) is embedded in the EXE and extracted to a temp dir at
launch. Built alongside the onedir ``vod-rip.spec`` — the zip ships the
onedir folder layout, this exe is the single download-and-run option.

    npm run build-dist          # onedir (unchanged)
    .venv/Scripts/python.exe -m PyInstaller vod-rip-onefile.spec --clean --noconfirm

The Analysis mirrors vod-rip.spec (same scripts / binaries / datas /
hiddenimports / hookspath / excludes) so both layouts stay in lock-step;
only the final EXE(...) call differs — onefile semantics, NO COLLECT.
"""

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(os.getcwd())
_BACKEND_DIR = _PROJECT_ROOT / "backend"
_STATIC_DIR = _BACKEND_DIR / "static"
_ASSETS_DIR = _PROJECT_ROOT / "assets"
_BUILD_DIR = _PROJECT_ROOT / "build"
_EXTERNAL_DIR = _BUILD_DIR / "external"
_VENDOR_DIR = _PROJECT_ROOT / "vendor"
_ICON_ICO = _ASSETS_DIR / "icon.ico"
_IS_WIN = sys.platform == "win32"


def _ffmpeg_binaries():
    if not _EXTERNAL_DIR.is_dir():
        return []
    result = []
    for name in ("ffmpeg", "ffprobe"):
        for path in (
            _EXTERNAL_DIR / f"{name}.exe",
            _EXTERNAL_DIR / f"{name}.bin",
            _EXTERNAL_DIR / name,
        ):
            if path.is_file():
                result.append((str(path), "."))
    return result


def _bundled_node_binaries():
    """Bundle the private Node 20 runtime from build/external/node.exe.

    Ships under ``<MEIPASS>/runtime/node.exe`` so the YouTube PO Token
    (bgutil) subprocess can spawn without requiring Node on PATH — see
    ``youtube_pot_service._frozen_runtime_paths`` (checks both the onedir
    exe dir and ``sys._MEIPASS``). Produced by ``scripts/download-node.ps1``.
    Skips silently when the artefact is absent.
    """
    if not _IS_WIN:
        return []
    node_exe = _EXTERNAL_DIR / "node.exe"
    if not node_exe.is_file():
        return []
    return [(str(node_exe), "runtime")]


def _bundled_bgutil_datas():
    """Bundle the bgutil-ytdlp-pot-provider server under ``runtime/bgutil-pot/``.

    Layout matches ``youtube_pot_service._frozen_runtime_paths``:

        <extract>/runtime/bgutil-pot/server/build/main.js
        <extract>/runtime/bgutil-pot/server/node_modules/...

    The server subtree is self-contained (``npm ci`` + ``npm run build``
    already executed by ``scripts/build-bgutil-bundle.ps1``).
    """
    server_dir = _EXTERNAL_DIR / "bgutil-pot" / "server"
    if not server_dir.is_dir():
        return []
    return [(str(server_dir), "runtime/bgutil-pot/server")]


def _asr_gpu_binaries():
    """Bundle the on-device ASR runtime so packaged users transcribe with
    zero commands (the answer to "users dont need to run any commands").

    Ships three things the modulegraph does not follow on its own:
      * sherpa-onnx's own onnxruntime + CUDA providers DLLs
        (``sherpa_onnx/lib/*.dll`` — the +cuda wheel's bundled ORT CUDA EP);
      * ctranslate2's native libs (faster-whisper's inference backend);
      * the ``nvidia/<pkg>/bin|lib`` CUDA runtime dirs (cublas, cufft,
        curand, cudnn, nvjitlink, cuda_runtime) so
        ``archive_transcribe._ensure_cuda_libs`` finds them in the frozen
        tree — it probes ``sys._MEIPASS`` (onefile) / ``sys.prefix``
        (onedir) roots, which is exactly where these land.

    CPU-only build hosts (CI without the GPU wheels) return [] — the
    runtime probe then degrades to the CPU EP, which is the designed
    fallback. The +cuda wheel itself is installed by the build (see
    ``backend/requirements-gpu.txt``).
    """
    result = []
    import site as _site

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
                lib_dir = pkg_dir / subdir
                if lib_dir.is_dir():
                    result.append(
                        (str(lib_dir), f"nvidia/{pkg_dir.name}/{subdir}")
                    )
    # Dedupe by (source, dest) — getsitepackages can repeat the same root.
    return list(dict.fromkeys(result))


def _cookie_extension_datas():
    """Bundle the vendored VOD.RIP Cookie Bridge extension source.

    Onefile payloads are extracted to a temp dir, so the folder must be
    copied out next to the exe at first run — __main_launcher__.py
    ``_materialize_cookie_extension`` does that. Same fork guard as
    scripts/stage-cookie-extension.mjs: require the bridge module so we
    never ship upstream drift.
    """
    src = _VENDOR_DIR / "cookie-extension" / "src"
    if not src.is_dir():
        return []
    if not (src / "manifest.json").is_file() or not (src / "modules" / "cookie_bridge.mjs").is_file():
        return []
    return [(str(src), "cookie-extension/src")]


def _hidden_imports():
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
        "services.archive_transcribe",
        "faster_whisper",
        "sherpa_onnx",
        "ctranslate2",
        "torch",
        "onnxruntime",
        "panns_inference",
        "silero_vad",
        "models.schemas",
        "webview",
        "PIL",
        "PIL.Image",
        "pystray",
        "tkinter",
        "tkinter.filedialog",
    ]
    if _IS_WIN:
        imports += [
            "webview.platforms.edgechromium",
            "pystray._win32",
        ]
    return imports


_hooks = _BUILD_DIR / "hooks"
block_cipher = None

a = Analysis(
    [
        str(_BACKEND_DIR / "__main_launcher__.py"),
        str(_BACKEND_DIR / "main.py"),
    ],
    pathex=[str(_BACKEND_DIR)],
    binaries=_ffmpeg_binaries() + _bundled_node_binaries() + _asr_gpu_binaries(),
    datas=[
        (str(_STATIC_DIR / "index.html"), "static"),
        (str(_ICON_ICO), "."),
    ]
    + _bundled_bgutil_datas()
    + _cookie_extension_datas(),
    hiddenimports=_hidden_imports(),
    hookspath=[str(_hooks)] if _hooks.is_dir() else [],
    runtime_hooks=[],
    excludes=[
        "tkinter.test",
        "tkinter.tix",
        "test",
        "django",
        "flask",
        "tornado",
        "boto3",
        "botocore",
        "matplotlib",
        "scipy",
        # numpy deliberately NOT excluded: torch, faster-whisper,
        # ctranslate2 and sherpa-onnx all import it at runtime, and the
        # packaged app must ship the on-device ASR stack (see
        # _asr_gpu_binaries + the ASR hiddenimports).
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    # F15 (ANTIVIRUS_AUDIT): the onedir spec sets noarchive=True for AV
    # posture (side-by-side layout was tripping YARA rules). The onefile
    # build deliberately keeps the default here — a single self-extracting
    # EXE is the well-known bootloader pattern AV vendors explicitly model,
    # and it is the entire point of the download-and-run artifact.
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_exe_kwargs = dict(
    name="VOD-RIP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if _IS_WIN and _ICON_ICO.is_file():
    _exe_kwargs["icon"] = str(_ICON_ICO)
    _version_file = _ASSETS_DIR / "version_info.py"
    if _version_file.is_file():
        _exe_kwargs["version"] = str(_version_file)

# Onefile semantics: pass a.binaries / a.zipfiles / a.datas straight into
# EXE (they are embedded in the PKG archive), no COLLECT, no
# exclude_binaries. Verified against PyInstaller 6.21.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    **_exe_kwargs,
)
