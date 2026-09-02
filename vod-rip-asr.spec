# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller spec for the optional ASR RUNTIME (``VOD-RIP-ASR.exe``).

Builds the on-device archive-transcription runtime as a SEPARATE onedir bundle
so the base ``VOD-RIP.exe`` never imports torch / sherpa-onnx. The spec keeps
the same helpers as the base spec (``spec_helpers``) but ADDS the heavy ASR
stack: ``asr_gpu_binaries`` (sherpa-onnx ORT + CUDA providers, ctranslate2,
nvidia cu12 runtime libs), ``silero_vad_datas`` (VAD weights) and
``asr_hidden_imports`` (torch/sherpa/ctranslate2/onnxruntime/panns/silero plus
the archive-worker graph).

Entry point: ``backend/asr_worker.py`` — a minimal launcher exposing
``--health``, ``--archive-worker`` and ``--transcribe-once`` (the supervised
child dispatch). The main app finds this exe under a versioned runtime
directory and spawns it; see ``scripts/deploy-asr-runtime.mjs``.

Windows-only in practice (the worker is spawned by the Windows GUI app), but the
spec is platform-agnostic like ``vod-rip.spec``.

    .venv/Scripts/python.exe -m PyInstaller vod-rip-asr.spec --clean --noconfirm
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
_ICON_ICO = H.ICON_ICO
_spec_version = H.spec_version()


def _hookspath():
    hooks = H.BUILD_DIR / "hooks"
    return [str(hooks)] if hooks.is_dir() else []


block_cipher = None

a = Analysis(
    [
        str(H.BACKEND_DIR / "asr_worker.py"),
    ],
    pathex=[str(H.BACKEND_DIR)],
    # ffmpeg/ffprobe are bundled so the worker can decode audio without a
    # system install; the heavy ASR stack (sherpa-onnx ORT + CUDA providers,
    # ctranslate2 native libs, nvidia cu12 runtime DLLs) is what makes this
    # runtime "heavy" relative to the base app.
    binaries=H.ffmpeg_binaries() + H.asr_gpu_binaries(),
    datas=H.silero_vad_datas(),
    hiddenimports=H.asr_hidden_imports(),
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
        # numpy is REQUIRED here (torch / sherpa / onnxruntime / silero), so it
        # is deliberately NOT excluded.
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    # Onedir + noarchive keeps the runtime layout simple to stage into a
    # versioned directory and to ZIP up as a standalone artifact.
    noarchive=True,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_exe_kwargs = dict(
    exclude_binaries=True,
    name="VOD-RIP-ASR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=True is required: the base app captures stdout when it runs
    # `VOD-RIP-ASR.exe --health` to parse the JSON status, and the worker
    # supervisor + child tee their logs to stdout.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if _IS_WIN and _ICON_ICO.is_file():
    _exe_kwargs["icon"] = str(_ICON_ICO)
    # Windows version resource, generated from the single source of truth so
    # FileVersion / ProductName stay consistent with the base app.
    _vr = H.win_version_resource(
        "VOD-RIP-ASR",
        "VOD.RIP — ASR transcription runtime",
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
    name="VOD-RIP-ASR",
)
