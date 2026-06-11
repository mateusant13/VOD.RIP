# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller spec (Windows / macOS / Linux).

From project root::

    npm run build-dist
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
_ICON_ICO = _ASSETS_DIR / "icon.ico"
_ICON_ICNS = _ASSETS_DIR / "icon.icns"
_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"


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
        "services.windows_shortcuts",
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
    binaries=_ffmpeg_binaries(),
    datas=[
        (str(_STATIC_DIR / "index.html"), "static"),
        (str(_ICON_ICO), "."),
    ],
    hiddenimports=_hidden_imports(),
    hookspath=[str(_hooks)] if _hooks.is_dir() else [],
    runtime_hooks=[],
    excludes=[
        "tkinter.test",
        "tkinter.tix",
        "test",
        "unittest",
        "django",
        "flask",
        "tornado",
        "boto3",
        "botocore",
        "matplotlib",
        "scipy",
        "numpy",
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
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
    _version_file = _ASSETS_DIR / "version_info.py"
    if _version_file.is_file():
        _exe_kwargs["version"] = str(_version_file)

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
            "CFBundleVersion": "1.0.11",
            "CFBundleShortVersionString": "1.0.11",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
