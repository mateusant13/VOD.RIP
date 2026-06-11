"""Detect and optionally install Microsoft Edge WebView2 on Windows."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_WEBVIEW2_CLSID = "{F3017226-FE2A-4295-8BDF-00B3D09F7BF5}"
_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def webview2_installed() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg
    except ImportError:
        return True

    subkey = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLSID}"
    roots = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]

    for hive in roots:
        for access in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | access) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    if version and str(version) not in ("0.0.0.0", "0.0.0"):
                        return True
            except OSError:
                continue
    return False


def offer_webview2_install() -> bool:
    """Prompt to install WebView2. Returns True when runtime is available afterward."""
    if webview2_installed():
        return True
    if os.name != "nt":
        return False

    try:
        import ctypes

        MB_YESNO = 0x4
        IDYES = 6
        choice = ctypes.windll.user32.MessageBoxW(
            0,
            "VOD.RIP needs the Microsoft Edge WebView2 Runtime for the desktop window.\n\n"
            "Install it now? (~150 MB, requires internet)\n\n"
            "Choose No to continue in your browser instead.",
            "VOD.RIP — WebView2 required",
            MB_YESNO | 0x30,
        )
        if choice != IDYES:
            return False
    except Exception as exc:
        logger.debug("WebView2 prompt failed: %s", exc)
        return False

    return _run_webview2_bootstrapper()


def _run_webview2_bootstrapper() -> bool:
    dest = Path(tempfile.gettempdir()) / "VOD.RIP-WebView2-Setup.exe"
    try:
        import requests

        logger.info("Downloading WebView2 bootstrapper …")
        with requests.get(_BOOTSTRAPPER_URL, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
    except Exception as exc:
        logger.warning("WebView2 download failed: %s", exc)
        _show_install_failed()
        return False

    try:
        logger.info("Installing WebView2 …")
        proc = subprocess.run(
            [str(dest), "/silent", "/install"],
            capture_output=True,
            timeout=300,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode not in (0, 3010):  # 3010 = already installed
            logger.warning("WebView2 installer exit code %s", proc.returncode)
    except Exception as exc:
        logger.warning("WebView2 install failed: %s", exc)
        _show_install_failed()
        return False
    finally:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass

    for _ in range(12):
        if webview2_installed():
            return True
        time.sleep(2)

    _show_install_failed()
    return False


def _show_install_failed() -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "WebView2 could not be installed automatically.\n\n"
            "Download it manually from:\n"
            "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
            "VOD.RIP will open in your browser for now.",
            "VOD.RIP",
            0x30,
        )
    except Exception:
        pass
