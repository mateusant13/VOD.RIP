# -*- mode: python ; coding: utf-8 -*-
"""
VOD.RIP — PyInstaller spec file.

Build command (run from PROJECT ROOT)::

    pyinstaller vod-rip.spec --clean

Paths are resolved relative to the project root (the working directory
when ``pyinstaller`` is invoked).
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Path anchors — assumes CWD is the project root (documented usage).
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.getcwd())
_BACKEND_DIR = _PROJECT_ROOT / "backend"
_STATIC_DIR = _BACKEND_DIR / "static"
_BUILD_DIR = _PROJECT_ROOT / "build"
_EXTERNAL_DIR = _BUILD_DIR / "external"


def _ffmpeg_binaries():
    """Return [(source, target)] tuples for ffmpeg/ffprobe if present."""
    if not _EXTERNAL_DIR.is_dir():
        return []
    result = []
    for name in ("ffmpeg", "ffprobe"):
        for path in (_EXTERNAL_DIR / f"{name}.exe", _EXTERNAL_DIR / name):
            if path.is_file():
                result.append((str(path), "."))
    return result


block_cipher = None

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    [
        str(_BACKEND_DIR / "__main_launcher__.py"),
        str(_BACKEND_DIR / "main.py"),
    ],
    pathex=[str(_BACKEND_DIR)],
    binaries=_ffmpeg_binaries(),
    datas=[
        (str(_STATIC_DIR / "index.html"), "static"),
        (str(_BUILD_DIR / "icon.ico"), "."),
    ],
    hiddenimports=[
        # --- uvicorn submodules ---
        'uvicorn',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.logging',
        # --- FastAPI ---
        'fastapi',
        'pydantic',
        # --- yt-dlp dynamic imports ---
        'yt_dlp',
        'yt_dlp.extractor',
        'yt_dlp.downloader',
        'yt_dlp.postprocessor',
        # --- curl_cffi ---
        'curl_cffi',
        'curl_cffi.requests',
        # --- Application entry + services ---
        'main',
        'services.kick_playwright_service',
        'services.twitch_gql_service',
        'services.kick_models',
        'services.ytdlp_service',
        'services.kick_api_service',
        'services.kick_download_worker',
        'services.ytdlp_service',
        'services.preview_service',
        'services.download_manager',
        'services.download_cleanup',
        'services.settings',
        'services.tray_service',
        'services.app_lifecycle',
        'services.shutdown_util',
        'services.updater',
        'services.crash_handler',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'services._version',
        'models.schemas',
        # --- PyWebView platform backends ---
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.cocoa',
        'webview.platforms.gtk',
        # --- tkinter (native folder picker) ---
        'tkinter',
        'tkinter.filedialog',
    ],
    hookspath=[str(_BUILD_DIR / "hooks")],
    runtime_hooks=[],
    excludes=[
        'tkinter.test', 'tkinter.tix',
        'test', 'unittest',
        'django', 'flask', 'tornado',
        'boto3', 'botocore',
        'matplotlib', 'scipy', 'numpy', 'pandas',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vod-rip',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_BUILD_DIR / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='vod-rip',
)

# ---------------------------------------------------------------------------
# macOS .app bundle
# ---------------------------------------------------------------------------

app = BUNDLE(
    coll,
    name='VOD.RIP.app',
    icon=str(_BUILD_DIR / "icon.icns"),
    bundle_identifier='com.vodrip.app',
    info_plist={
        'CFBundleDisplayName': 'VOD.RIP',
        'CFBundleExecutable': 'vod-rip',
        'CFBundleName': 'VOD.RIP',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
    },
)
