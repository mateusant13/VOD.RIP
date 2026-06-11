"""Detect and silently install Microsoft Edge WebView2 on Windows."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
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
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for access in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | access) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    if version and str(version) not in ("0.0.0.0", "0.0.0"):
                        return True
            except OSError:
                continue
    return False


def ensure_webview2_silent() -> bool:
    """Install WebView2 if missing. Shows a one-time setup window while installing."""
    if webview2_installed():
        return True
    if os.name != "nt":
        return False

    state = {"done": False, "ok": False}

    def worker() -> None:
        try:
            state["ok"] = _run_webview2_bootstrapper()
        finally:
            state["done"] = True

    threading.Thread(target=worker, daemon=True, name="webview2-install").start()
    _show_installing_window(state)
    return state["ok"] or webview2_installed()


def _show_installing_window(state: dict) -> None:
    try:
        import tkinter as tk
    except ImportError:
        while not state["done"]:
            time.sleep(0.2)
        return

    root = tk.Tk()
    root.title("VOD.RIP")
    root.geometry("440x150")
    root.resizable(False, False)
    root.configure(bg="#0A0A0A")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    frame = tk.Frame(root, bg="#0A0A0A")
    frame.pack(expand=True, fill="both", padx=24, pady=22)

    tk.Label(
        frame,
        text="Setting up VOD.RIP",
        fg="#ffffff",
        bg="#0A0A0A",
        font=("Segoe UI", 12, "bold"),
        anchor="w",
    ).pack(fill="x")

    status = tk.Label(
        frame,
        text="Installing Microsoft WebView2 Runtime…\n"
        "One-time setup (~150 MB). Please wait.",
        fg="#a1a1aa",
        bg="#0A0A0A",
        font=("Consolas", 9),
        justify="left",
        anchor="w",
    )
    status.pack(fill="x", pady=(10, 0))

    def pump() -> None:
        if state["done"]:
            if not (state["ok"] or webview2_installed()):
                status.config(
                    text="WebView2 install did not finish.\n"
                    "VOD.RIP will try opening in your browser.",
                    fg="#fbbf24",
                )
                root.after(1800, root.destroy)
            else:
                root.destroy()
            return
        root.update_idletasks()
        root.after(80, pump)

    root.after(80, pump)
    root.mainloop()


def _run_webview2_bootstrapper() -> bool:
    dest = Path(tempfile.gettempdir()) / "VOD.RIP-WebView2-Setup.exe"
    try:
        import requests

        logger.info("Downloading WebView2 bootstrapper …")
        with requests.get(_BOOTSTRAPPER_URL, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
    except Exception as exc:
        logger.warning("WebView2 download failed: %s", exc)
        return False

    try:
        logger.info("Installing WebView2 …")
        proc = subprocess.run(
            [str(dest), "/silent", "/install"],
            capture_output=True,
            timeout=300,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode not in (0, 3010):
            logger.warning("WebView2 installer exit code %s", proc.returncode)
            return False
    except Exception as exc:
        logger.warning("WebView2 install failed: %s", exc)
        return False
    finally:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass

    for _ in range(15):
        if webview2_installed():
            return True
        time.sleep(2)

    return webview2_installed()
