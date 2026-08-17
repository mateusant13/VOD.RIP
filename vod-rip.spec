# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller spec (Windows / macOS / Linux).

From project root::

    npm run build-dist
"""

import os
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(os.getcwd())
_BACKEND_DIR = _PROJECT_ROOT / "backend"
_STATIC_DIR = _BACKEND_DIR / "static"
_ASSETS_DIR = _PROJECT_ROOT / "assets"
_BUILD_DIR = _PROJECT_ROOT / "build"
_EXTERNAL_DIR = _BUILD_DIR / "external"
_ICON_ICO = _ASSETS_DIR / "icon.ico"
_ICON_ICNS = _ASSETS_DIR / "icon.icns"
_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

# Pull the canonical version out of services/_version.py — the same file the
# Python entry point reads — so the Windows version resource and the macOS
# CFBundleVersion stay in lock-step with what the user sees in the UI. Parse
# it with a tiny regex rather than `import` (PyInstaller's spec runs before
# sys.path is wired up).
_spec_version = "1.0.0"
_version_py = _BACKEND_DIR / "services" / "_version.py"
if _version_py.is_file():
    try:
        _m = re.search(
            r'__version__\s*=\s*["\']([^"\']+)["\']',
            _version_py.read_text(encoding="utf-8", errors="replace"),
        )
        if _m:
            _spec_version = _m.group(1)
    except Exception:
        _spec_version = "1.0.0"


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

    Ships under ``<exe-dir>/runtime/node.exe`` so the YouTube PO Token
    (bgutil) subprocess can spawn without requiring Node on PATH.
    Produced by ``scripts/download-node.ps1``. Skips silently when the
    artefact is absent — dev installs and CI without Node continue fine.
    """
    if sys.platform != "win32":
        return []
    node_exe = _EXTERNAL_DIR / "node.exe"
    if not node_exe.is_file():
        return []
    return [(str(node_exe), "runtime")]


def _bundled_bgutil_datas():
    """Bundle the bgutil-ytdlp-pot-provider server under ``runtime/bgutil-pot/``.

    Layout matches ``youtube_pot_service.frozen_runtime_paths``:

        <exe-dir>/runtime/bgutil-pot/server/build/main.js
        <exe-dir>/runtime/bgutil-pot/server/node_modules/...

    The server subtree is self-contained (``npm ci`` + ``npm run build``
    already executed by ``scripts/build-bgutil-bundle.ps1``). We bundle
    only the ``server/`` subtree — the repo root and ``docs/`` are not
    needed at runtime and just bloat the installer.
    """
    server_dir = _EXTERNAL_DIR / "bgutil-pot" / "server"
    if not server_dir.is_dir():
        return []
    return [(str(server_dir), "runtime/bgutil-pot/server")]


def _silero_vad_datas():
    """Bundle silero-vad's weights (silero_vad/data/*.onnx|*.jit).

    model.py resolves them via importlib.resources.files("silero_vad.data"),
    which modulegraph never sees — without this the frozen app dies with
    'No module named silero_vad.data' on every VAD call (archive + live)."""
    try:
        from PyInstaller.utils.hooks import collect_data_files
        return collect_data_files("silero_vad")
    except Exception:
        # ponytail: best-effort — a missing wheel just loses VAD weights
        return []


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
    elif _IS_MAC:
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
    ] + ([(
        str(_ICON_ICNS), ".",
    )] if _IS_MAC and _ICON_ICNS.is_file() else [])
      + _bundled_bgutil_datas() + _silero_vad_datas(),
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
    # F15 (ANTIVIRUS_AUDIT): noarchive=True embeds the entire archive into the
    # single EXE rather than emitting a side-by-side VOD-RIP.pkg + _internal/
    # directory. A single self-extracting EXE matches the well-known
    # "PyInstaller bootloader" pattern that AV vendors explicitly model, and
    # the directory layout was triggering a small number of YARA rules
    # (`PyInstaller/Trojanized`) and false-positive detections in the
    # "PUA:Win32/UncommonBinaryBundle" category.
    noarchive=True,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_exe_kwargs = dict(
    exclude_binaries=True,
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
    # Windows version resource, generated from _spec_version (single source
    # of truth) instead of a static version_info.py that drifts out of sync
    # on version bumps. AV heuristics treat a self-consistent FileVersion /
    # ProductName as a mark of a real product; a stale or absent resource is
    # a classic PyInstaller false-positive signal. Best-effort: a resource
    # problem must never fail the build.
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )

        _v = tuple(int(x) for x in _spec_version.split("."))
        _v = (_v + (0, 0, 0, 0))[:4]
        _ver_str = ".".join(map(str, _v))
        _exe_kwargs["version"] = VSVersionInfo(
            ffi=FixedFileInfo(
                filevers=_v,
                prodvers=_v,
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
                                StringStruct("FileDescription", "VOD.RIP — Kick & Twitch VOD downloader"),
                                StringStruct("FileVersion", _ver_str),
                                StringStruct("InternalName", "VOD.RIP"),
                                StringStruct("LegalCopyright", "Copyright (c) mateusant13"),
                                StringStruct("OriginalFilename", "VOD-RIP.EXE"),
                                StringStruct("ProductName", "VOD.RIP"),
                                StringStruct("ProductVersion", _ver_str),
                            ],
                        )
                    ]
                ),
                VarFileInfo([VarStruct("Translation", [1033, 1200])]),
            ],
        )
    except Exception:
        pass

exe = EXE(pyz, a.scripts, [], **_exe_kwargs)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VOD-RIP",
)
if _IS_MAC:
    _bundle_icon = str(_ICON_ICNS) if _ICON_ICNS.is_file() else None
    app = BUNDLE(
        coll,
        name="VOD.RIP.app",
        icon=_bundle_icon,
        bundle_identifier="com.vodrip.app",
        info_plist={
            "CFBundleDisplayName": "VOD.RIP",
            "CFBundleExecutable": "VOD-RIP",
            "CFBundleName": "VOD.RIP",
            "CFBundleVersion": _spec_version,
            "CFBundleShortVersionString": _spec_version,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
