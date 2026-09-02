# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller ONE-FILE spec (Windows) — BASE app.

Produces a single self-extracting ``VOD-RIP.exe``: the whole base app payload
(including ffmpeg, the Node runtime, the bgutil POT server, both browser
extensions and the UIA installer script) is embedded in the EXE and extracted
to a temp dir at launch. Built alongside the onedir ``vod-rip.spec`` — the zip
ships the onedir folder layout, this exe is the single download-and-run option.

The heavy on-device ASR stack and optional AI runtimes (torch / torchaudio /
sherpa-onnx / panns-inference / silero-vad / ctranslate2 / onnxruntime /
tokenizers) are deliberately NOT bundled here. The base app falls back to raw
captions and lexical archive search until the separate ``VOD-RIP-ASR.exe``
runtime is installed on first use. That worker ships in
``backend/asr_worker.py`` + ``vod-rip-asr.spec``.

    npm run build-dist          # onedir (unchanged)
    .venv/Scripts/python.exe -m PyInstaller vod-rip-onefile.spec --clean --noconfirm

The Analysis mirrors vod-rip.spec (same scripts / binaries / datas /
hiddenimports / hookspath / excludes) so both layouts stay in lock-step;
only the final EXE(...) call differs — onefile semantics, NO COLLECT.
"""

import os
import sys
from pathlib import Path

_SPEC_DIR = Path.cwd()
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

import spec_helpers as H  # noqa: E402

_IS_WIN = H.IS_WIN
_ICON_ICO = H.ICON_ICO


def _cookie_extension_datas():
    """Bundle the vendored VOD.RIP Cookie Bridge extension source.

    Onefile payloads are extracted to a temp dir, so the folder must be
    copied out next to the exe at first run — __main_launcher__.py
    ``_materialize_cookie_extension`` does that. Same fork guard as
    scripts/stage-cookie-extension.mjs: require the bridge module so we
    never ship upstream drift.
    """
    src = H.VENDOR_DIR / "cookie-extension" / "src"
    if not src.is_dir():
        return []
    if not (src / "manifest.json").is_file() or not (src / "modules" / "cookie_bridge.mjs").is_file():
        return []
    return [(str(src), "cookie-extension/src")]


def _kick_overlay_datas():
    """Bundle the unpacked Kick Overlay for the onefile installer."""
    src = H.VENDOR_DIR / "kick-overlay"
    if not (src / "manifest.json").is_file() or not (src / "content.js").is_file():
        return []
    return [(str(src), "kick-overlay")]


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
        (str(H.STATIC_DIR / "index.html"), "static"),
        (str(H.ICON_ICO), "."),
    ]
    + H.bundled_bgutil_datas()
    + _cookie_extension_datas()
    + _kick_overlay_datas()
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
    _version_file = H.ASSETS_DIR / "version_info.py"
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
