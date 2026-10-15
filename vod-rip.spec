# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller spec (Windows / macOS / Linux) — BASE app.

The heavy on-device ASR stack and optional AI runtimes (torch / torchaudio /
sherpa-onnx / panns-inference / silero-vad / ctranslate2 / onnxruntime /
tokenizers) are deliberately NOT bundled here. The base app falls back to raw
captions and lexical archive search until the separate
``VOD-RIP-ASR.exe`` runtime is installed on first use. The ASR stack ships in
``backend/asr_worker.py`` + ``vod-rip-asr.spec`` as that separate runtime.

Invocation (any CWD — paths are anchored to this spec's directory)::

    npm run build-dist
"""

import os
import sys
from pathlib import Path

# PyInstaller injects SPECPATH (the spec file's own directory) into the spec
# namespace; __file__ is NOT defined there. Keeping the import anchored to
# SPECPATH makes the build independent of the launch CWD.
_SPEC_DIR = Path(SPECPATH)
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

import spec_helpers as H  # noqa: E402

_IS_WIN = H.IS_WIN
_IS_MAC = H.IS_MAC
_ICON_ICO = H.ICON_ICO
_spec_version = H.spec_version()


def _hookspath():
    hooks = H.BUILD_DIR / "hooks"
    return [str(hooks)] if hooks.is_dir() else []


block_cipher = None

a = Analysis(
    [
        str(H.BACKEND_DIR / "__main_launcher__.py"),
        str(H.BACKEND_DIR / "main.py"),
    ],
    pathex=[str(H.BACKEND_DIR)],
    binaries=H.ffmpeg_binaries() + H.bundled_node_binaries(),
    datas=[
            # The WHOLE static dir, not just index.html. Measured 2026-10-15 on the
    # installed bundle: index.html asks for /assets/icon.ico and /assets/icon.png
    # and both 404ed on every launch, because build-copy puts them in
    # backend/static/assets and the spec shipped only the index. Vite serves
    # public/ at the root in dev, so nothing showed this until the frozen app
    # was opened.
    (str(H.STATIC_DIR), "static"),
        (str(H.ICON_ICO), "."),
    ] + ([(
        str(H.ICON_ICNS), ".",
    )] if _IS_MAC and H.ICON_ICNS.is_file() else [])
      + H.bundled_bgutil_datas()
      + [(str(H.BACKEND_DIR / "scripts" / "cookie_extension_auto_install.ps1"), "scripts")],
    hiddenimports=H.base_hidden_imports(),
    hookspath=_hookspath(),
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
        # numpy remains for live audio decoding; semantic search is optional
        # and degrades to lexical ranking when its external model is absent.
        # Heavy on-device ASR and optional AI stacks. They live in the
        # separately downloaded worker so the base installer stays small.
        "torch",
        "torchaudio",
        "sherpa_onnx",
        "panns_inference",
        "silero_vad",
        "ctranslate2",
        "onnxruntime",
        "tokenizers",
        # Measured 2026-10-15 (xref-VOD-RIP.html of the pre-fix build): the
        # module graph follows TRANSITIVE imports the base never makes:
        #   services.archive_transcribe -> huggingface_hub ->
        #   huggingface_hub._webhooks_server -> gradio -> gradio.pipelines ->
        #   transformers -> {torchvision, torchao, timm, datasets, accelerate,
        #   faiss, soynlp, babel, spacy, cv2, av, pandas, pyarrow, skimage}
        # torch's lib DLLs also ride in via binary-dependency analysis of
        # torchvision's _C.pyd, so excluding "torch" alone was NOT enough.
        #   services.archive_events -> librosa -> {sklearn, numba, llvmlite}
        # Every one of these is a function-level import in the backend (or an
        # unused transitive), so the base app boots and degrades without them;
        # vod-rip-asr.spec still ships them in the optional ASR runtime.
        "huggingface_hub._webhooks_server",
        "gradio",
        "transformers",
        "datasets",
        "accelerate",
        "torchvision",
        "torchao",
        "timm",
        "faiss",
        "librosa",
        "soynlp",
        "pandas",
        "pyarrow",
        "sklearn",
        "numba",
        "llvmlite",
        "spacy",
        "cv2",
        "av",
        "babel",
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
    _vr = H.win_version_resource(
        "VOD-RIP",
        "VOD.RIP — Kick & Twitch VOD downloader",
    )
    if _vr is not None:
        _exe_kwargs["version"] = _vr

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
    _bundle_icon = str(H.ICON_ICNS) if H.ICON_ICNS.is_file() else None
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
