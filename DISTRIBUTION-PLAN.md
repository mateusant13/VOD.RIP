# VOD.RIP — Distribution & Packaging Plan

**Last updated**: June 2026
**Target audience**: Non-technical users
**Primary platform**: Windows (with macOS and Linux support)

---

## Table of Contents

1. [Final Architecture](#1-final-architecture)
2. [Installation Directory Layout](#2-installation-directory-layout)
3. [Technology Stack](#3-technology-stack)
4. [Packaging Strategy](#4-packaging-strategy)
   - [PyInstaller Configuration](#41-pyinstaller-configuration)
   - [PyWebView Desktop Shell](#42-pywebview-desktop-shell)
   - [Cross-Platform File Layout](#43-cross-platform-file-layout)
   - [FFmpeg Distribution](#44-ffmpeg-distribution)
   - [Playwright Browser Management](#45-playwright-browser-management)
5. [Entry Point](#5-entry-point)
6. [Application Lifecycle](#6-application-lifecycle)
   - [Startup Sequence](#61-startup-sequence)
   - [Shutdown Sequence](#62-shutdown-sequence)
7. [Installer Technology](#7-installer-technology)
   - [Windows: Inno Setup](#71-windows-inno-setup)
   - [macOS: create-dmg](#72-macos-create-dmg)
   - [Linux: Flatpak / AppImage](#73-linux-flatpak--appimage)
8. [CI/CD Pipeline](#8-cicd-pipeline)
   - [GitHub Actions Matrix Build](#81-github-actions-matrix-build)
   - [GitHub Releases Strategy](#82-github-releases-strategy)
9. [Auto-Update System](#9-auto-update-system)
10. [Logging & Crash Reporting](#10-logging--crash-reporting)
11. [Antivirus & SmartScreen Mitigation](#11-antivirus--smartscreen-mitigation)
12. [Files to Create](#12-files-to-create)
13. [Files to Modify](#13-files-to-modify)
14. [Estimated Sizes](#14-estimated-sizes)

---

## 1. Final Architecture

```
                    ┌──────────────────────────┐
                    │    PyWebView Window       │
                    │  (native desktop shell)   │
                    │  1280×800, resizable      │
                    │  own taskbar entry        │
                    │  own Alt+Tab entry        │
                    └───────────┬──────────────┘
                                │ loads
                    ┌───────────▼──────────────┐
                    │   React SPA (Vite)       │
                    │  inlined index.html      │
                    │  API calls via localhost  │
                    └───────────┬──────────────┘
                                │
                    ┌───────────▼──────────────┐
                    │  FastAPI (uvicorn)        │
                    │  127.0.0.1:7897           │
                    └───────────┬──────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                  ▼
    ┌─────────────────┐ ┌──────────────┐ ┌──────────────┐
    │ yt-dlp          │ │ Playwright   │ │ FFmpeg       │
    │ download engine │ │ (Chromium    │ │ (merge,      │
    │                 │ │  headless)   │ │  trim)       │
    └─────────────────┘ └──────────────┘ └──────────────┘
```

**Key principle**: The React frontend and FastAPI backend are **completely unchanged**. PyWebView is a ~30-line wrapper that opens a native window pointing at `http://127.0.0.1:7897`. Nothing in the application logic knows or cares that it's running in a desktop shell vs. a browser tab.

**Graceful degradation**: If PyWebView is unavailable (e.g., missing WebView2 on Windows 10), the app falls back to launching the default browser + a system tray icon.

---

## 2. Installation Directory Layout

### Windows

```
%LOCALAPPDATA%\VOD.RIP\
├── vod-rip.exe                          ← PyInstaller entry point
├── _internal\                           ← PyInstaller bundle (Python + .pyd + .pyc)
├── ffmpeg.exe                           ← BtbN FFmpeg build
├── ffprobe.exe
├── browsers\ms-playwright\
│   └── chromium_headless_shell-1150\
│       └── chrome-headless-shell.exe
├── static\index.html                    ← Inlined React build (npm run build-copy)
└── unins000.exe                         ← Inno Setup uninstaller
```

```
%APPDATA%\VOD.RIP\                       ← User data (never deleted on update)
├── settings.json
├── logs\
│   └── app-2026-06-10.log
└── crash_reports\
    └── crash_2026-06-10T12-34-56.txt
```

### macOS

```
VOD.RIP.app/
└── Contents/
    ├── MacOS/
    │   └── vod-rip                      ← PyInstaller binary
    ├── Resources/
    │   ├── ffmpeg
    │   ├── ffprobe
    │   ├── browsers/ms-playwright/...
    │   └── static/index.html
    ├── Info.plist
    └── icon.icns
```

```
~/Library/Application Support/VOD.RIP/  ← User data
├── settings.json
├── logs/
└── crash_reports/
```

### Linux (AppImage internal structure)

```
VOD.RIP-x86_64.AppImage/
└── (squashfs)
    ├── AppRun                           ← Entry point shell script
    ├── usr/
    │   ├── bin/
    │   │   └── vod-rip                  ← PyInstaller binary
    │   ├── lib/
    │   │   ├── ffmpeg
    │   │   ├── ffprobe
    │   │   └── browsers/ms-playwright/...
    │   ├── share/
    │   │   ├── vod-rip/static/index.html
    │   │   ├── applications/vod-rip.desktop
    │   │   └── icons/...
    └── .DirIcon
```

```
~/.local/share/VOD.RIP/                 ← User data (XDG_DATA_HOME)
├── settings.json
├── logs/
└── crash_reports/
```

---

## 3. Technology Stack

| Layer | 2026 Choice | Rationale |
|---|---|---|
| **Desktop shell** | PyWebView v6 | Native window, cross-platform (WebView2/WKWebView/WebKitGTK), zero frontend changes |
| **Backend** | FastAPI + uvicorn | Unchanged from current |
| **Frontend** | React 19 + Vite 7 + Tailwind 4 | Unchanged from current |
| **Frontend build** | vite-plugin-singlefile | Inlines all JS/CSS into one HTML file — no static file serving |
| **Python bundler** | PyInstaller (--onedir) | Most mature Python packaging, handles yt-dlp + curl_cffi well |
| **Windows installer** | Inno Setup | Battle-tested, Pascal scripting, silent upgrade support |
| **macOS packaging** | create-dmg | Standard .dmg creation from .app bundles |
| **Linux packaging** | Flatpak (preferred) / AppImage | GNOME runtime includes WebKitGTK; Flatpak sandboxing |
| **Auto-update** | Custom Python (GitHub Releases API) | Simple, no extra dependencies, Inno Setup /VERYSILENT |
| **Graceful fallback** | pystray + webbrowser | If PyWebView unavailable, tray icon + browser launch |
| **Update distribution** | GitHub Releases | Already using GitHub, no extra infrastructure |

---

## 4. Packaging Strategy

### 4.1 PyInstaller Configuration

**Mode**: `--onedir` (directory mode, NOT `--onefile`)
**Console**: `--noconsole` (`console=False` in spec file)
**UPX**: Enabled (compresses .pyd/.dll ~30%)

#### Spec file (`build/vod-rip.spec`)

```python
# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['backend/__main_launcher__.py'],
    pathex=['backend'],
    binaries=[
        # curl_cffi shared library
        ('curl_cffi*', '.'),
    ],
    datas=[
        ('backend/static/index.html', 'static'),
        ('build/icon.ico', '.'),
    ],
    hiddenimports=[
        # uvicorn submodules
        'uvicorn',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.logging',
        # FastAPI
        'fastapi',
        'pydantic',
        # yt-dlp dynamic imports
        'yt_dlp',
        'yt_dlp.extractor',
        'yt_dlp.downloader',
        'yt_dlp.postprocessor',
        # curl_cffi
        'curl_cffi',
        'curl_cffi.requests',
        # App services
        'services.kick_playwright_service',
        'services.kick_api_service',
        'services.kick_download_worker',
        'services.ytdlp_service',
        'services.preview_service',
        'services.download_manager',
        'services.download_cleanup',
        'services.settings',
        'models.schemas',
        'services.tray_service',
        'services.updater',
        # PyWebView platform backends
        'webview',
        'webview.platforms.edgechromium',   # Windows
        'webview.platforms.cocoa',           # macOS
        'webview.platforms.gtk',             # Linux
        # tkinter (native folder picker)
        'tkinter',
        'tkinter.filedialog',
    ],
    hookspath=['build/hooks'],
    excludes=[
        'tkinter.test', 'tkinter.tix',
        'test', 'unittest',
        'django', 'flask', 'tornado',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='vod-rip',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # --noconsole (GUI app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='build/icon.ico',
)
```

#### Custom hook: `build/hooks/hook-curl_cffi.py`

```python
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules
hiddenimports = collect_submodules('curl_cffi')
binaries = collect_dynamic_libs('curl_cffi')
```

### 4.2 PyWebView Desktop Shell

The desktop shell is a minimal wrapper — ~30 lines of new Python. The entire React/FastAPI stack is untouched.

```python
import threading
import webview
import uvicorn
from main import app

def start_server():
    uvicorn.run(app, host="127.0.0.1", port=7897, log_config=None)

threading.Thread(target=start_server, daemon=True).start()
webview.create_window("VOD.RIP", "http://127.0.0.1:7897",
                       width=1280, height=800,
                       min_size=(800, 600),
                       confirm_close=True)
webview.start()
```

**Graceful fallback** (when WebView2 is missing on Windows 10):

```python
try:
    import webview
    webview.create_window(...)
    webview.start()
except Exception:
    # PyWebView failed — fall back to browser + tray
    import webbrowser
    webbrowser.open("http://127.0.0.1:7897")
    from services.tray_service import TrayService
    tray = TrayService(...)
    tray.run()
```

### 4.3 Cross-Platform File Layout

| Item | Windows | macOS | Linux |
|---|---|---|---|
| **Install location** | `%LOCALAPPDATA%\VOD.RIP` | `/Applications/VOD.RIP.app` | `/opt/VOD.RIP` or AppImage |
| **User data** | `%APPDATA%\VOD.RIP` | `~/Library/Application Support/VOD.RIP` | `~/.local/share/VOD.RIP` |
| **Browser engine** | WebView2 (Chromium) | WKWebView (Safari) | WebKitGTK |
| **Preinstalled** | Yes (Win11), Mostly (Win10) | Yes | No (needs Flatpak runtime) |
| **Extra installer dep** | 2 MB WebView2 bootstrapper | None | ~100 MB GNOME runtime (Flatpak) |
| **Entry point** | `vod-rip.exe` | `VOD.RIP.app/Contents/MacOS/vod-rip` | `AppRun → vod-rip` |
| **Icon format** | `.ico` | `.icns` | `.png` |

### 4.4 FFmpeg Distribution

**Source**: [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) — prebuilt binaries for each platform.

- **Windows**: `ffmpeg-master-latest-win64-gpl.zip` → extract `ffmpeg.exe`, `ffprobe.exe`
- **macOS**: `ffmpeg-master-latest-macos-arm64-gpl.zip` (Apple Silicon) or `...-x86_64-...` (Intel)
- **Linux**: `ffmpeg-master-latest-linux64-gpl.zip`

**Placement**: Alongside the PyInstaller executable in the install directory.

**Runtime discovery** (unchanged from current code, but with frozen-aware pathing):

```python
def _resolve_ffmpeg_exe():
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
        # On macOS .app bundles, Contents/MacOS is sibling to Contents/Resources
        if sys.platform == 'darwin':
            resources = base.parent / 'Resources'
            if (resources / 'ffmpeg').is_file():
                return str(resources / 'ffmpeg')
    else:
        base = Path(__file__).parent
    # Check alongside exe first, then PATH
    for exe in ['ffmpeg.exe', 'ffmpeg']:
        candidate = base / exe
        if candidate.is_file():
            return str(candidate)
    return 'ffmpeg'  # fall back to PATH
```

**License**: FFmpeg is GPL-licensed. VOD.RIP itself remains MIT because it calls ffmpeg via subprocess (no linking). Include `THIRD-PARTY-LICENSES.txt` with ffmpeg's license notice.

### 4.5 Playwright Browser Management

Playwright's Chromium headless binary (~170 MB compressed) is **not** bundled inside the PyInstaller output. Instead:

1. **CI installs** the browser to a known directory:
   ```bash
   export PLAYWRIGHT_BROWSERS_PATH=$RUNNER_TEMP/browsers
   python -m playwright install chromium
   ```

2. **Installer copies** the `browsers/` directory alongside the executable.

3. **Launcher sets** the environment variable before any Playwright code runs:
   ```python
   os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
       Path(sys.executable).parent / "browsers"
   )
   os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
   ```

4. **macOS**: Browsers are inside the `.app` bundle:
   ```python
   os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
       Path(sys.executable).parent.parent / "Resources" / "browsers"
   )
   ```

5. **Linux (AppImage)**: Browsers are inside the AppImage at `usr/lib/browsers/`. The `AppRun` wrapper script sets the path.

**Version pinning**: The Playwright Python package and browser binary must be from the same release. Pin the version in `requirements.txt`:

```
playwright==1.52.0
```

---

## 5. Entry Point

The single entry point for all platforms: `backend/__main_launcher__.py`

```python
"""
VOD.RIP — Production launch entry point (all platforms).

Responsibilities:
1. Set up logging to %APPDATA%/VOD.RIP/logs/
2. Configure Playwright browser path
3. Start FastAPI/uvicorn on 127.0.0.1:PORT
4. Wait for server readiness
5. Launch PyWebView native window (fallback: browser + tray icon)
6. On window close / Quit: graceful shutdown of all downloads and ffmpeg processes
"""

import multiprocessing
import os
import sys
import threading
import time
import logging
from pathlib import Path


# ── Version ──────────────────────────────────────────────────────────────

__version__ = "1.0.0"


# ── Path helpers ─────────────────────────────────────────────────────────

def _get_appdata_dir() -> Path:
    """Cross-platform user data directory."""
    if sys.platform == "win32":
        base = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:  # Linux / XDG
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "VOD.RIP"


def _get_install_dir() -> Path:
    """Return the directory containing the executable (or dev source dir)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _get_resources_dir() -> Path:
    """On macOS, Resources/ is sibling to MacOS/ inside the .app bundle."""
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        candidate = _get_install_dir().parent / "Resources"
        if candidate.is_dir():
            return candidate
    return _get_install_dir()


# ── Logging ──────────────────────────────────────────────────────────────

def _setup_logging() -> Path:
    app_data = _get_appdata_dir()
    log_dir = app_data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_path), encoding="utf-8"),
            logging.StreamHandler(sys.stderr),  # inert with --noconsole
        ],
    )

    # Suppress noisy loggers from dependencies
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)

    logging.getLogger("VOD.RIP").info("=== VOD.RIP %s starting ===", __version__)
    return log_path


# ── Environment setup ────────────────────────────────────────────────────

def _setup_environment():
    """Set environment variables before any service code is imported."""
    resources = _get_resources_dir()
    browsers_path = str(resources / "browsers")
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", browsers_path)
    os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
    os.environ["KICK_SERVE_UI"] = "1"


# ── Server lifecycle ─────────────────────────────────────────────────────

def _start_server(port: int):
    from main import app
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_config=None,       # Use our logging config
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


def _wait_for_server(port: int, timeout_sec: int = 15) -> bool:
    """Poll the API until it responds or timeout expires."""
    import requests
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/info", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ── UI launch ────────────────────────────────────────────────────────────

def _launch_pywebview(port: int) -> bool:
    """Attempt to open a PyWebView native window. Returns False on failure."""
    try:
        import webview

        # Enable DevTools (F12) when VODRIP_DEBUG is set
        debug = os.environ.get("VODRIP_DEBUG", "") == "1"

        webview.create_window(
            "VOD.RIP",
            f"http://127.0.0.1:{port}",
            width=1280,
            height=800,
            min_size=(800, 600),
            resizable=True,
            fullscreen=False,
            confirm_close=True,       # Warn if downloads are active
            text_select=True,
            easy_x=debug,             # DevTools in debug mode
        )
        webview.start(debug=debug)
        return True
    except Exception as exc:
        logging.getLogger("VOD.RIP").warning(
            "PyWebView unavailable (%s), falling back to browser + tray", exc
        )
        return False


def _launch_browser_and_tray(port: int):
    """Fallback: open the default browser and start a tray icon."""
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}")

    from services.tray_service import TrayService

    def on_quit():
        raise SystemExit(0)

    tray = TrayService(port=port, shutdown_callback=on_quit)
    tray.run()


# ── Shutdown ─────────────────────────────────────────────────────────────

def _shutdown(server_thread: threading.Thread):
    """Cancel downloads, kill ffmpeg/Playwright, then exit."""
    from services.download_manager import download_mgr

    logger = logging.getLogger("VOD.RIP")

    logger.info("Shutting down...")

    # 1. Cancel all active downloads
    download_mgr.cancel_all()

    # 2. Kill ffmpeg processes we spawned
    import subprocess
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe", "/T"],
                       capture_output=True, timeout=5)
    else:
        subprocess.run(["pkill", "-9", "-f", "ffmpeg"],
                       capture_output=True, timeout=5)

    # 3. Kill Playwright headless shells
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "chrome-headless-shell.exe"],
                       capture_output=True, timeout=5)
    else:
        subprocess.run(["pkill", "-9", "-f", "chrome-headless-shell"],
                       capture_output=True, timeout=5)

    logger.info("Shutdown complete")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    multiprocessing.freeze_support()
    log_path = _setup_logging()
    _setup_environment()

    port = int(os.environ.get("PORT", 7897))
    logger = logging.getLogger("VOD.RIP")

    logger.info("Starting FastAPI on 127.0.0.1:%d", port)

    # Start server in a daemon thread
    server_thread = threading.Thread(
        target=_start_server, args=(port,), daemon=True
    )
    server_thread.start()

    # Wait for server to be ready
    if not _wait_for_server(port):
        logger.error("Server did not start within timeout")
        sys.exit(1)

    logger.info("Server ready — launching UI")

    # Try PyWebView; fall back to browser + tray
    if not _launch_pywebview(port):
        _launch_browser_and_tray(port)

    # Clean shutdown
    _shutdown(server_thread)
    os._exit(0)


if __name__ == "__main__":
    main()
```

---

## 6. Application Lifecycle

### 6.1 Startup Sequence

| # | Action | Time (approx.) |
|---|---|---|
| 1 | User double-clicks `vod-rip.exe` / `.app` / AppImage | T+0s |
| 2 | `multiprocessing.freeze_support()` — prevents subprocess loop on Windows | T+0.1s |
| 3 | `_setup_logging()` — creates `%APPDATA%/VOD.RIP/logs/` | T+0.2s |
| 4 | `_setup_environment()` — sets `PLAYWRIGHT_BROWSERS_PATH`, `KICK_SERVE_UI=1` | T+0.3s |
| 5 | Import all services (download manager, settings, preview, etc.) | T+0.5s |
| 6 | `_start_server()` — launches uvicorn on `127.0.0.1:7897` | T+0.8s |
| 7 | `_wait_for_server()` — polls `/api/info` until 200 OK | T+1–3s |
| 8 | `_launch_pywebview()` — opens native window | T+1.5–3.5s |
| 9 | Frontend loads from localhost, calls `/api/settings`, `/api/info` | T+2–4s |
| 10 | **Application is ready** | **T+3–6s total** |

**Fallback path** (no WebView2 on old Windows 10):
- Step 8 fails gracefully → opens default browser + tray icon
- Tray icon manages lifecycle instead of window close

### 6.2 Shutdown Sequence

| Trigger | User closes PyWebView window | User logs off Windows |
|---|---|---|
| 1 | PyWebView's window close event fires | OS sends `WM_ENDSESSION` |
| 2 | `webview.start()` returns → `main()` continues | Python process killed |
| 3 | `_shutdown()` called | `atexit` handler fires (if registered) |
| 4 | `download_mgr.cancel_all()` — sets cancel events on all active downloads | Same |
| 5 | FFmpeg processes killed (`taskkill /IM ffmpeg.exe`) | Same |
| 6 | Playwright headless shells killed | Same |
| 7 | `os._exit(0)` — terminates the process | OS terminates |

**If user closes the window with active downloads**: PyWebView's `confirm_close=True` shows a native dialog: "You have active downloads. Are you sure you want to quit?" Only if the user confirms does shutdown proceed.

---

## 7. Installer Technology

### 7.1 Windows: Inno Setup

`build/installer.iss`:

```pascal
#define MyAppName "VOD.RIP"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "VOD.RIP"
#define MyAppURL "https://github.com/your-username/vod-rip"
#define MyAppExeName "vod-rip.exe"

[Setup]
AppId={{B8F4A3D2-5E6F-4A7B-9C8D-1E2F3A4B5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\VOD.RIP
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=..\Output
OutputBaseFilename=VOD.RIP-Setup
SetupIconFile=icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
CloseApplications=yes
RestartApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableStartupPrompt=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
; Main application
Source: "installer_staging\vod-rip.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_staging\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; FFmpeg
Source: "installer_staging\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_staging\ffprobe.exe"; DestDir: "{app}"; Flags: ignoreversion
; Playwright browsers
Source: "installer_staging\browsers\*"; DestDir: "{app}\browsers"; Flags: ignoreversion recursesubdirs createallsubdirs
; Static UI
Source: "installer_staging\static\index.html"; DestDir: "{app}\static"; Flags: ignoreversion
; WebView2 bootstrapper (optional — only if not preinstalled)
Source: "build\webview2\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not IsWebView2Installed

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch VOD.RIP"; Flags: nowait postinstall skipifsilent

[Code]
function IsWebView2Installed: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  if not Result then
    Result := RegKeyExists(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not IsWebView2Installed then
  begin
    Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
         '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
```

### 7.2 macOS: create-dmg

```bash
#!/bin/bash
# build-macos-dmg.sh

APP_NAME="VOD.RIP"
VERSION="1.0.0"
SIGN_IDENTITY=${1:-""}  # Optional: "Developer ID Application: Your Name (TEAMID)"

# 1. PyInstaller builds .app bundle
pyinstaller --onedir --windowed \
  --icon=build/icon.icns \
  --osx-bundle-identifier=com.vodrip.app \
  --name="$APP_NAME" \
  backend/__main_launcher__.py

# 2. Copy resources into the .app bundle
RESOURCES="dist/$APP_NAME.app/Contents/Resources"
cp ffmpeg "$RESOURCES/"
cp ffprobe "$RESOURCES/"
cp -r browsers/ms-playwright "$RESOURCES/browsers/"
cp -r backend/static/index.html "$RESOURCES/static/"
cp build/icon.icns "$RESOURCES/"

# 3. (Optional) Code sign
if [ -n "$SIGN_IDENTITY" ]; then
  codesign --deep --force --verify --verbose \
    --options runtime \
    --sign "$SIGN_IDENTITY" \
    "dist/$APP_NAME.app"
fi

# 4. Create DMG
brew install create-dmg 2>/dev/null || true
create-dmg \
  --volname "$APP_NAME $VERSION" \
  --volicon "build/icon.icns" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 100 \
  --icon "$APP_NAME.app" 150 200 \
  --hide-extension "$APP_NAME.app" \
  --app-drop-link 450 200 \
  --no-internet-enable \
  "Output/$APP_NAME-$VERSION.dmg" \
  "dist/$APP_NAME.app"
```

### 7.3 Linux: Flatpak / AppImage

#### Flatpak (recommended)

`build/com.vodrip.app.yml`:

```yaml
id: com.vodrip.app
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: vod-rip

finish-args:
  - --socket=wayland
  - --socket=x11
  - --share=network
  - --socket=pulseaudio

modules:
  - name: python3-packages
    buildsystem: simple
    build-commands:
      - pip3 install --prefix=/app -r backend/requirements.txt
      - pip3 install --prefix=/app pyinstaller pywebview

  - name: vod-rip
    buildsystem: simple
    depends: [python3-packages]
    build-commands:
      - pyinstaller --onedir build/vod-rip.spec --distpath=/app/bin
      - export PLAYWRIGHT_BROWSERS_PATH=/app/bin/browsers
      - python3 -m playwright install chromium
      - cp -r $PLAYWRIGHT_BROWSERS_PATH /app/bin/browsers
      - cp ffmpeg /app/bin/
      - cp ffprobe /app/bin/
    sources:
      - type: dir
        path: .
```

#### AppImage (simpler alternative)

```bash
#!/bin/bash
# build-linux-appimage.sh

VERSION="1.0.0"
ARCH="x86_64"

# 1. PyInstaller build
pyinstaller --onedir build/vod-rip.spec

# 2. Create AppDir structure
APPDIR="dist/VOD.RIP.AppDir"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp -r dist/vod-rip/* "$APPDIR/usr/bin/"
cp ffmpeg ffprobe "$APPDIR/usr/lib/"
cp -r browsers/ms-playwright "$APPDIR/usr/lib/browsers/"
cp backend/static/index.html "$APPDIR/usr/share/vod-rip/static/"

# 3. AppRun entry point
cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${HERE}/usr/lib:${PATH}"
export PLAYWRIGHT_BROWSERS_PATH="${HERE}/usr/lib/browsers"
export PLAYWRIGHT_SKIP_BROWSER_GC=1
export KICK_SERVE_UI=1
exec "${HERE}/usr/bin/vod-rip"
EOF
chmod +x "$APPDIR/AppRun"

# 4. Desktop entry
cat > "$APPDIR/usr/share/applications/vod-rip.desktop" << 'EOF'
[Desktop Entry]
Name=VOD.RIP
Comment=Download and trim VODs from Kick and Twitch
Exec=vod-rip
Icon=vod-rip
Type=Application
Categories=AudioVideo;Network;
Terminal=false
EOF

# 5. Icon
cp build/icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/vod-rip.png"
cp build/icon.png "$APPDIR/.DirIcon"

# 6. Build AppImage
wget -q "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-$ARCH.AppImage" \
     -O appimagetool
chmod +x appimagetool
ARCH="$ARCH" ./appimagetool "$APPDIR" "Output/VOD.RIP-$VERSION-$ARCH.AppImage"
```

---

## 8. CI/CD Pipeline

### 8.1 GitHub Actions Matrix Build

`.github/workflows/release.yml`:

```yaml
name: Build and Release

on:
  push:
    tags:
      - "v*"

jobs:
  build:
    strategy:
      matrix:
        include:
          - os: windows-latest
            artifact-name: windows
            installer-cmd: iscc build/installer.iss
            output: Output/VOD.RIP-Setup.exe
            upload: VOD.RIP-${{ github.ref_name }}-Setup.exe

          - os: macos-latest
            artifact-name: macos
            installer-cmd: bash build-macos-dmg.sh
            output: Output/VOD.RIP-*.dmg
            upload: VOD.RIP-${{ github.ref_name }}.dmg

          - os: ubuntu-22.04
            artifact-name: linux
            installer-cmd: bash build-linux-appimage.sh
            output: Output/VOD.RIP-*.AppImage
            upload: VOD.RIP-${{ github.ref_name }}-x86_64.AppImage

    runs-on: ${{ matrix.os }}
    defaults:
      run:
        shell: bash

    steps:
      - uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: "20"

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install frontend deps
        run: npm ci

      - name: Build frontend
        run: npm run build-copy

      - name: Install Python deps
        run: |
          pip install -r backend/requirements.txt
          pip install pyinstaller pywebview pystray Pillow

      - name: Install Playwright browsers
        run: |
          export PLAYWRIGHT_BROWSERS_PATH=$RUNNER_TEMP/browsers
          python -m playwright install chromium
          echo "BROWSERS_PATH=$RUNNER_TEMP/browsers" >> $GITHUB_ENV

      - name: Download ffmpeg
        run: bash build/download-ffmpeg.sh
        # This script downloads the platform-appropriate ffmpeg build
        # and places ffmpeg + ffprobe in build/external/

      - name: Build with PyInstaller
        run: pyinstaller build/vod-rip.spec --clean

      - name: Stage installer files
        run: |
          mkdir -p installer_staging
          cp -r dist/vod-rip/* installer_staging/
          cp -r $BROWSERS_PATH/* installer_staging/browsers/
          cp build/external/ffmpeg* installer_staging/
          cp backend/static/index.html installer_staging/static/

      - name: Package installer
        run: ${{ matrix.installer-cmd }}

      - name: Upload release artifact
        uses: softprops/action-gh-release@v2
        with:
          files: ${{ matrix.output }}
          generate_release_notes: true
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### 8.2 GitHub Releases Strategy

| Field | Convention |
|---|---|
| **Tag** | `v1.0.0` (semver) |
| **Title** | `VOD.RIP v1.0.0` |
| **Body** | Auto-generated from conventional commits |
| **Assets** | 3 files (Windows .exe, macOS .dmg, Linux .AppImage) |
| **Asset naming** | `VOD.RIP-v1.0.0-Setup.exe`, `VOD.RIP-v1.0.0.dmg`, `VOD.RIP-v1.0.0-x86_64.AppImage` |
| **Draft** | Yes (QA before publishing) |
| **Prerelease** | Yes for beta builds |

**Branch strategy**:
- `main` — stable. Tagged releases only.
- `develop` — integration branch.
- Feature branches off `develop`.

**Release workflow**:
1. Merge `develop` → `main`
2. Push tag `v1.1.0`
3. CI builds, creates draft release with 3 platform assets
4. Manual QA on draft artifacts
5. Publish release

**Release notes template**:

```
## VOD.RIP v1.0.0

### Downloads

| Platform | Download | Size |
|---|---|---|
| Windows | [VOD.RIP-v1.0.0-Setup.exe](...) | 72 MB |
| macOS   | [VOD.RIP-v1.0.0.dmg](...) | 85 MB |
| Linux   | [VOD.RIP-v1.0.0-x86_64.AppImage](...) | 120 MB |

SHA256: `abc123...`

### System Requirements
- Windows 10/11 (64-bit), macOS 12+, Linux (x86_64, glibc 2.28+)
- 4 GB RAM recommended
- 200 MB disk space (before downloads)

### What's New
- Initial public release
- Download Twitch VODs with trimming
- Download Kick VODs with trimming
- Download Twitch and Kick clips
- Browse channel VODs and clips
- Preview before downloading

### First Time?
1. Download the installer for your platform
2. Windows: SmartScreen may show a warning — click "More info" → "Run anyway"
3. The VOD.RIP desktop window will open
```

---

## 9. Auto-Update System

Using a custom Python update checker + silent installer execution.

### Update Checker (`backend/services/updater.py`)

```python
"""Check for updates against GitHub Releases API."""

import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

GITHUB_REPO = "your-username/vod-rip"
CHECK_INTERVAL_SEC = 24 * 3600  # Once per day
CACHE_FILE = "update_cache.json"

logger = logging.getLogger(__name__)


class UpdateChecker:
    def __init__(self, current_version: str, app_data_dir: Path):
        self.current_version = current_version.lstrip("v")
        self.cache_path = app_data_dir / CACHE_FILE

    def _should_check(self) -> bool:
        if not self.cache_path.is_file():
            return True
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return (time.time() - data.get("last_check", 0)) > CHECK_INTERVAL_SEC
        except Exception:
            return True

    def _save_cache(self, data: dict):
        try:
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def check(self) -> Optional[dict]:
        """Check for updates. Returns release info dict if a newer version exists."""
        if not self._should_check():
            return None

        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                self._save_cache({"last_check": time.time(), "error": resp.status_code})
                return None

            data = resp.json()
            latest_tag = data.get("tag_name", "").lstrip("v")

            if latest_tag == self.current_version:
                self._save_cache({"last_check": time.time(), "latest": latest_tag})
                return None

            # Find platform-appropriate asset
            platform_key = self._platform_key()
            asset = self._find_asset(data.get("assets", []), platform_key)

            if not asset:
                logger.warning("No matching asset for %s in release %s", platform_key, latest_tag)
                return None

            result = {
                "version": latest_tag,
                "download_url": asset["browser_download_url"],
                "release_url": data["html_url"],
                "release_notes": data.get("body", ""),
            }
            self._save_cache({"last_check": time.time(), "latest": latest_tag})
            return result

        except requests.RequestException as e:
            logger.warning("Update check failed: %s", e)
            return None

    def _platform_key(self) -> str:
        if sys.platform == "win32":
            return "Setup.exe"
        elif sys.platform == "darwin":
            return ".dmg"
        else:
            return ".AppImage"

    def _find_asset(self, assets: list, key: str) -> Optional[dict]:
        for asset in assets:
            name = asset.get("name", "")
            if key in name:
                return asset
        return None

    def download_and_install(self, release_info: dict) -> bool:
        """Download the new installer and launch it."""
        download_url = release_info["download_url"]
        tmp_dir = Path(tempfile.gettempdir()) / "VOD.RIP-Updates"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        ext = ".exe" if sys.platform == "win32" else ".dmg" if sys.platform == "darwin" else ".AppImage"
        installer_path = tmp_dir / f"VOD.RIP-{release_info['version']}{ext}"

        logger.info("Downloading update %s ...", release_info["version"])
        resp = requests.get(download_url, stream=True, timeout=300)
        resp.raise_for_status()

        with open(installer_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info("Download complete. Launching installer...")

        # Platform-specific install
        if sys.platform == "win32":
            subprocess.Popen(
                [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                 "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                close_fds=True,
            )
        elif sys.platform == "darwin":
            # Mount DMG and copy to /Applications
            subprocess.Popen(["open", str(installer_path)])
        else:  # Linux
            os.chmod(installer_path, 0o755)
            subprocess.Popen([str(installer_path)])

        logger.info("Exiting for update...")
        os._exit(0)
```

### Tray Integration

When an update is available, the UI or tray icon shows a notification:

```python
# In tray_service.py — add "Update Available" menu item
def _on_check_updates(self, icon, item):
    result = self.update_checker.check()
    if result:
        if self._ask_user_to_update(result["version"], result["release_notes"]):
            self.update_checker.download_and_install(result)
    else:
        self._show_notification("No updates available")
```

---

## 10. Logging & Crash Reporting

### Logging

| File | Location | Content |
|---|---|---|
| `app.log` | `%APPDATA%/VOD.RIP/logs/` | Application logs (info, warning, error) |

**Rotation**: 5 MB per file, keep 3 files (via `RotatingFileHandler`).

**What gets logged**:
- Application startup/shutdown
- Download start, progress, completion, failure
- Settings changes
- PyWebView fallback events
- Auto-update check results
- Unhandled exceptions

### Crash Reporting

`backend/services/crash_handler.py`:

```python
"""Global exception hook — writes crash dumps to disk."""

import faulthandler
import os
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path


def install_crash_handler(app_data_dir: Path, version: str):
    crash_dir = app_data_dir / "crash_reports"
    crash_dir.mkdir(parents=True, exist_ok=True)

    # Enable faulthandler for native/C extension crashes
    faulthandler.enable(all_threads=True)

    # Python-level crash handler
    def _handle_uncaught(exc_type, exc_value, exc_tb):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_file = crash_dir / f"crash_{timestamp}.txt"
        try:
            with open(crash_file, "w", encoding="utf-8") as f:
                f.write(f"VOD.RIP Crash Report\n")
                f.write(f"Version: {version}\n")
                f.write(f"Time: {datetime.now().isoformat()}\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"Platform: {platform.platform()}\n")
                f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
                f.write("=" * 60 + "\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                f.write("\nAll threads:\n")
                for tid, frame in sys._current_frames().items():
                    f.write(f"\n--- Thread 0x{tid:x} ---\n")
                    traceback.print_stack(frame, file=f)
        except Exception:
            pass

    sys.excepthook = _handle_uncaught
```

---

## 11. Antivirus & SmartScreen Mitigation

### Risk Assessment

| Threat | Likelihood | Impact |
|---|---|---|
| PyInstaller binary flagged by Windows Defender | High (common false positive) | Medium (user sees scary warning) |
| SmartScreen "Unrecognized app" warning | High (new publisher) | Medium (user clicks "Run anyway") |
| curl_cffi DLL flagged as suspicious | Medium | Medium |
| VirusTotal false positives | High for new PyInstaller builds | Low (informed users check) |

### Mitigations (in priority order)

**Tier 1 — Free (do immediately)**:
- Submit your installer to [Microsoft Defender Security Intelligence](https://www.microsoft.com/en-us/wdsi/filesubmission) for false positive review
- Check with VirusTotal before each release
- Add a note to the README: "Your antivirus may flag the installer because it bundles Python. This is a known false positive. Verify the SHA256 checksum."

**Tier 2 — Low cost (after hitting ~500 users)**:
- Purchase an **OV code signing certificate** (~$200-400/year). This associates your publisher name with the binary. SmartScreen becomes less aggressive over time as reputation builds.
- Sign BOTH the inner `vod-rip.exe` AND the outer `VOD.RIP-Setup.exe`

**Tier 3 — Full solution (after validating demand)**:
- Purchase an **EV code signing certificate** (~$300-500/year)
- Store it in Azure Key Vault for CI signing
- Old: `AzureSignTool sign -kvu ...`
- Newer: Azure Trusted Signing (Microsoft's cloud signing service, pay-per-signature ~$0.20)

### Without code signing (v1)

Users will see:
```
Windows protected your PC
Microsoft Defender SmartScreen prevented an unrecognized app from starting.
Running this app might put your PC at risk.
  [More info] → [Run anyway]
```

This is survivable. Most users of tools like this are comfortable with this flow. Monitor support requests about it; if it becomes a frequent complaint, buy a cert.

---

## 12. Files to Create

| File | Purpose |
|---|---|
| `backend/__main_launcher__.py` | Production entry point with PyWebView + fallback |
| `backend/services/tray_service.py` | System tray icon (fallback when PyWebView unavailable) |
| `backend/services/updater.py` | GitHub Releases update checker |
| `backend/services/crash_handler.py` | Global exception hook |
| `backend/services/_version.py` | `__version__ = "1.0.0"` (single source of truth) |
| `build/vod-rip.spec` | PyInstaller spec file |
| `build/hooks/hook-curl_cffi.py` | PyInstaller hook for curl_cffi |
| `build/installer.iss` | Inno Setup script (Windows) |
| `build/build-macos-dmg.sh` | macOS DMG build script |
| `build/build-linux-appimage.sh` | Linux AppImage build script |
| `build/com.vodrip.app.yml` | Flatpak manifest (Linux) |
| `build/download-ffmpeg.sh` | Cross-platform ffmpeg download script |
| `build/icon.ico` | Application icon (Windows) |
| `build/icon.icns` | Application icon (macOS) |
| `build/icon.png` | Application icon (Linux, 256x256) |
| `.github/workflows/release.yml` | CI/CD pipeline |
| `DISTRIBUTION-PLAN.md` | This document |

---

## 13. Files to Modify

| File | Change |
|---|---|
| `backend/services/settings.py` | Change settings path from `~/.config/KickDownloader/` to `%APPDATA%/VOD.RIP/settings.json` (with cross-platform fallback) |
| `backend/requirements.txt` | Add `pywebview`, `pystray`, `Pillow` |
| `backend/main.py` | Minor: update ffmpeg path resolution for PyInstaller frozen mode |
| `package.json` | Add build script that also copies to installer staging |
| `README.md` | Update installation instructions for packaged versions |

---

## 14. Estimated Sizes

| Component | Windows | macOS | Linux (AppImage) | Linux (Flatpak) |
|---|---|---|---|---|
| Python + stdlib | 15 MB | 18 MB | 15 MB | (runtime shared) |
| FastAPI + uvicorn + deps | 8 MB | 10 MB | 8 MB | (shared) |
| yt-dlp | 8 MB | 8 MB | 8 MB | (shared) |
| curl_cffi | 4 MB | 5 MB | 4 MB | (shared) |
| Playwright Chromium headless | 170 MB | 200 MB | 200 MB | 200 MB |
| ffmpeg + ffprobe | 60 MB | 40 MB | 50 MB | 50 MB |
| PyWebView / WebKit | 0 MB (system) | 0 MB (system) | 100 MB (WebKit libs) | (GNOME runtime) |
| React UI (inlined) | 0.2 MB | 0.2 MB | 0.2 MB | 0.2 MB |
| PyInstaller overhead | 3 MB | 4 MB | 3 MB | (shared) |
| UPX compression savings | -40 MB | — | — | — |
| **Total installed** | **~225 MB** | **~285 MB** | **~390 MB** | **~250 MB** |
| **Installer (compressed)** | **~75 MB** | **~90 MB** | **~150 MB** | — (Flatpak downloads runtime) |

**Note**: The Playwright Chromium binary dominates. If this becomes a distribution problem:
1. On first launch, download the browser binary (requires internet, adds friction)
2. Offer a "lite" version without Kick support (Playwright-less)
3. Use Playwright's `chrome-headless-shell` only (already the case — not full Chromium)

---

*End of distribution plan. This document should be updated as decisions change.*
