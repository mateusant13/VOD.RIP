"""Detect WebView2 on Windows and guide the user to install it from Microsoft.

We intentionally do NOT download or execute installers from inside VOD.RIP —
that pattern is flagged as trojan/dropper behavior by antivirus software.

The detection logic was wrong in two ways that mattered to the user
experience (ANTIVIRUS_AUDIT follow-up, 2026-06):

1.  The CLSID used to look up the WebView2 Runtime in EdgeUpdate\\Clients
    was the Edge browser's GUID ``{F3017226-FE2A-4295-8BDF-00B3D09F7BF5}``.
    The correct WebView2 Runtime GUID is
    ``{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}``. With the wrong CLSID, the
    registry check always returned False and the binary-on-disk check was
    the only thing keeping WebView2 detection working at all.

2.  Even when the registry check succeeded, ``pv`` is sometimes set to a
    non-empty value by EdgeUpdate for a runtime that has been removed
    (e.g. after Disk Cleanup or a manual uninstall). The runtime is
    *registered* but the binary is *missing*. The new detector never
    trusts ``pv`` alone — it always verifies that ``msedgewebview2.exe``
    exists at the registry-reported path, and falls back to scanning the
    well-known install folders.

This file deliberately has zero third-party dependencies. ``winreg`` and
``pathlib`` are stdlib; the only ctypes call is ``ExpandEnvironmentStringsW``
which is used to expand ``%ProgramFiles%`` in a HKLM-registered custom path.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
import webbrowser
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Correct CLSID for the WebView2 Runtime in EdgeUpdate\\Clients.
# Microsoft publishes this — do not change. The earlier code in this file
# used the Edge browser's CLSID, which always returned False from the
# registry check. The browser's GUID ends in ``7BF5``; the WebView2
# Runtime's GUID ends in ``E4C5``. ``7BF5 != E4C5``.
_WEBVIEW2_RUNTIME_CLSID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_INSTALL_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_INVALID_VERSIONS = frozenset(("", "0.0.0.0", "0.0.0"))

# EdgeUpdate registry keys (in the order Windows searches them).
# Microsoft stores the CLSID *with* curly braces in the key name.
_REG_HIVES_AND_KEYS: Tuple[Tuple[int, str], ...] = (
    (0x80000002, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_RUNTIME_CLSID}"),  # HKLM 32-bit
    (0x80000002, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_RUNTIME_CLSID}"),            # HKLM 64-bit
    (0x80000001, rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_RUNTIME_CLSID}"),            # HKCU
)

# Well-known install locations for msedgewebview2.exe, in priority order.
# Each entry is (set_of_env_var_names, relpath_under_env_var_value).
#
# Why multiple env vars per entry:
#   * ``ProgramFiles(x86)`` = the 32-bit program files on 64-bit Windows
#     (or the only one on 32-bit Windows).
#   * ``ProgramFiles`` = the 32-bit program files on 32-bit Windows, or
#     the redirected 32-bit view on WoW64 Python.
#   * ``ProgramW64386`` = the *native* 64-bit ``C:\\Program Files`` on
#     64-bit Windows when the current process is 32-bit (WoW64). Without
#     this env var, a 32-bit Python on a 64-bit box would never look at
#     the native 64-bit install dir.
#   * ``LOCALAPPDATA`` = per-user installs.
_KNOWN_INSTALL_ROOTS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    # Evergreen per-machine (the default Win10/11 install + bootstrapper).
    (("ProgramFiles(x86)", "ProgramFiles", "ProgramW64386"),
     r"Microsoft\EdgeWebView\Application"),
    # Evergreen per-user (manual bootstrap with /silent-install /silent).
    (("LOCALAPPDATA",),
     r"Microsoft\EdgeWebView\Application"),
    # Edge (Chromium) shares its runtime with WebView2 on Win11+ — the
    # binary lives inside the Edge install tree.
    (("ProgramFiles(x86)", "ProgramFiles", "ProgramW64386"),
     r"Microsoft\Edge\Application"),
)


def _msedgewebview2_exe_name() -> str:
    return "msedgewebview2.exe"


def _expand_env(value: str) -> str:
    """Expand %FOO% in *value* via kernel32 on Windows. Returns *value*
    unchanged on other platforms."""
    if os.name != "nt" or "%" not in value:
        return value
    try:
        kernel32 = ctypes.windll.kernel32
        out = ctypes.create_unicode_buffer(2048)
        n = kernel32.ExpandEnvironmentStringsW(value, out, 2048)
        return out.value if n else value
    except Exception:
        return value


def _binary_exists(root: Path) -> Optional[Path]:
    """If *root* contains a versioned subfolder holding msedgewebview2.exe,
    return the full path. Otherwise None. Safe on any platform (returns
    None for non-Windows)."""
    if os.name != "nt" or not root.is_dir():
        return None
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            exe = child / _msedgewebview2_exe_name()
            if exe.is_file():
                return exe
    except (PermissionError, OSError) as exc:
        logger.debug("webview2 scan %s: %s", root, exc)
    return None


def _webview2_on_disk() -> Optional[Path]:
    """Return the path of msedgewebview2.exe if it exists in any well-known
    install location. Used as the **primary** detection signal.

    Dedup is required because on WoW64 Python (``os.environ.get('ProgramFiles')``
    returns the 32-bit redirected view) several env vars can resolve to the
    same physical directory.
    """
    seen_roots: set = set()
    for env_keys, relpath in _KNOWN_INSTALL_ROOTS:
        for env_key in env_keys:
            env_value = os.environ.get(env_key)
            if not env_value:
                continue
            root = Path(env_value) / relpath
            # Normalise to a canonical form for dedup.
            try:
                canonical = str(root.resolve())
            except OSError:
                canonical = str(root)
            if canonical in seen_roots:
                continue
            seen_roots.add(canonical)
            found = _binary_exists(root)
            if found is not None:
                logger.debug("WebView2 found on disk: %s", found)
                return found
    return None


def _registry_reported_path(hive: int, subkey: str) -> Optional[Path]:
    """Read the ``location`` value from the EdgeUpdate client key. Returns
    the **path to msedgewebview2.exe** if the registry points at a real
    install, or None otherwise.

    Microsoft writes the install root to ``location`` (not ``path`` —
    that was an older custom-install field). This function is strict:
    it returns the path of the binary only if the file actually exists.
    A registry entry whose binary has been removed returns None.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            # ``location`` is the canonical Microsoft key. ``path`` is an
            # older custom-install key we still read as a fallback.
            location: Optional[str] = None
            pv: Optional[str] = None
            for value_name in ("location", "path"):
                try:
                    value, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if isinstance(value, str) and value and not location:
                    location = value
            try:
                pv_value, _ = winreg.QueryValueEx(key, "pv")
                if isinstance(pv_value, str) and pv_value and pv_value not in _INVALID_VERSIONS:
                    pv = pv_value
            except OSError:
                pass
    except OSError:
        return None
    if not location:
        return None
    root = Path(_expand_env(location))
    candidates: list = []
    if pv:
        candidates.append(root / pv / _msedgewebview2_exe_name())
    candidates.append(root / _msedgewebview2_exe_name())
    # The "Application" dir holds versioned subfolders; if ``pv`` did
    # not give us a hit, walk one level down to find any version subdir
    # with the binary. This handles EdgeUpdate's behaviour of leaving
    # ``location`` pointing at the parent directory.
    if not any(c.is_file() for c in candidates):
        for child in root.iterdir():
            if not child.is_dir():
                continue
            cand = child / _msedgewebview2_exe_name()
            if cand.is_file():
                candidates.append(cand)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None

def _registry_installed() -> bool:
    """True iff EdgeUpdate knows about a WebView2 Runtime *and* the
    binary is actually present at the reported path.

    ``_registry_reported_path`` returns the path of msedgewebview2.exe
    (verified with ``is_file()``) or None. A non-None return is proof
    of install.
    """
    for hive, subkey in _REG_HIVES_AND_KEYS:
        if _registry_reported_path(hive, subkey) is not None:
            return True
    return False


def webview2_installed() -> bool:
    """Return True iff a usable WebView2 runtime is present on this machine.

    Detection priority:
        1.  Binary exists in a well-known install folder (most reliable,
            survives EdgeUpdate registry staleness).
        2.  EdgeUpdate registry key points at a real msedgewebview2.exe
            (fast path on systems where the on-disk scan is slow).

    Returns True on non-Windows (Linux/macOS use WebKit / Cocoa, not WV2).
    """
    if os.name != "nt":
        return True

    on_disk = _webview2_on_disk()
    if on_disk is not None:
        return True

    # Fall back to registry; only trust it if the binary path it reports
    # actually resolves to an existing msedgewebview2.exe. This is the case
    # for *custom* Evergreen installs that the on-disk scan above missed
    # (rare, but possible when the user has run the bootstrapper with a
    # custom /install-location).
    if _registry_installed():
        return True

    logger.debug("WebView2 not detected: no binary in known locations and registry empty")
    return False


def webview2_version() -> Optional[str]:
    """Return the WebView2 Runtime version string, or None if not installed.

    Useful for diagnostics and for the bootstrapper to decide whether a
    newer download is needed.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive, subkey in _REG_HIVES_AND_KEYS:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, "pv")
                except OSError:
                    continue
                if _INVALID_VERSIONS and str(value) in _INVALID_VERSIONS:
                    continue
                if value:
                    return str(value)
        except OSError:
            continue
    return None


def ensure_webview2() -> bool:
    """Return True when WebView2 is available. Otherwise show a setup dialog."""
    if webview2_installed():
        return True
    if os.name != "nt":
        return False

    # Registry can lag a few seconds right after the Setup installer finishes WebView2.
    for _ in range(8):
        time.sleep(0.5)
        if webview2_installed():
            return True

    return _show_setup_dialog()


def open_webview2_download_page() -> None:
    webbrowser.open(_WEBVIEW2_INSTALL_URL)


def _show_setup_dialog() -> bool:
    """Guide the user to install WebView2 from Microsoft (no bundled/silent install)."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        logger.warning("WebView2 missing and tkinter unavailable")
        return False

    root = tk.Tk()
    root.title("VOD.RIP — WebView2 required")
    root.geometry("500x250")
    root.resizable(False, False)
    root.configure(bg="#0A0A0A")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    result = {"ok": False}

    frame = tk.Frame(root, bg="#0A0A0A", padx=24, pady=20)
    frame.pack(expand=True, fill="both")

    tk.Label(
        frame,
        text="Desktop window needs WebView2",
        fg="#ffffff",
        bg="#0A0A0A",
        font=("Segoe UI", 12, "bold"),
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        frame,
        text=(
            "VOD.RIP uses Microsoft's free WebView2 runtime for the native app window.\n\n"
            "If you just ran the VOD.RIP installer, click “Check again” — registration can\n"
            "take a few seconds after setup finishes.\n\n"
            "Otherwise open Microsoft's installer below, complete it, then click “Check again”.\n"
            "You can also use browser mode to run VOD.RIP without the native window."
        ),
        fg="#a1a1aa",
        bg="#0A0A0A",
        font=("Segoe UI", 9),
        justify="left",
        anchor="w",
    ).pack(fill="x", pady=(10, 16))

    status = tk.StringVar(value="")
    status_label = tk.Label(
        frame, textvariable=status, fg="#fbbf24", bg="#0A0A0A", font=("Segoe UI", 9), anchor="w",
    )
    status_label.pack(fill="x", pady=(0, 10))

    btn_row = tk.Frame(frame, bg="#0A0A0A")
    btn_row.pack(fill="x")

    def on_download() -> None:
        open_webview2_download_page()
        status.set("Install WebView2 from the page that opened, then click “Check again”.")

    def on_retry() -> None:
        if webview2_installed():
            result["ok"] = True
            root.destroy()
        else:
            status.set("WebView2 not detected yet. Finish Microsoft's installer, then retry.")

    def on_browser() -> None:
        result["ok"] = False
        root.destroy()

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    ttk.Button(btn_row, text="Open Microsoft installer", command=on_download).pack(side="left", padx=(0, 8))
    ttk.Button(btn_row, text="Check again", command=on_retry).pack(side="left", padx=(0, 8))
    ttk.Button(btn_row, text="Use browser mode", command=on_browser).pack(side="right")

    root.mainloop()
    return result["ok"] or webview2_installed()
