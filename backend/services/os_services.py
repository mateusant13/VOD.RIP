"""Centralized platform-abstraction layer for VOD.RIP.

Consolidates scattered ``os.name`` / ``sys.platform`` checks into a single
module so cross-platform behaviour is consistent, auditable, and easy to fix.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


# ===================================================================
# BUG 1: Platform-aware path sanitization
# ===================================================================

# Characters Windows rejects in file paths — only stripped on Windows
_WINDOWS_FORBIDDEN_CHARS = re.compile(r'[<>:\"/\\|?*]')
# Control characters (0x00-0x1f) are rejected by *all* platforms
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
# Reserved device names on Windows — only checked on Windows
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename_component(value: str, fallback: str = "download") -> str:
    """Clean a single filename component for the current OS.

    On Windows, strips ``<>:"/\\|?*`` and rejects reserved device names.
    On macOS and Linux, only strips control characters (``\\x00-\\x1f``).
    Colons (``:``) are only stripped on Windows — they are valid on macOS
    (displayed as ``/`` in Finder) and on Linux.
    """
    if value is None:
        return fallback
    cleaned = _CONTROL_CHARS.sub("_", str(value)).strip(" .")
    if is_windows():
        cleaned = _WINDOWS_FORBIDDEN_CHARS.sub("_", cleaned)
        if not cleaned or cleaned.upper() in _WINDOWS_RESERVED_NAMES:
            return fallback
    if not cleaned:
        return fallback
    return cleaned


# ===================================================================
# BUG 2: macOS GPU detection via system_profiler
# ===================================================================

def _run_text(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("os_services command failed %s: %s", cmd[:2], e)
        return ""


def _gpu_names_nvidia_smi() -> List[str]:
    names: list[str] = []
    if not shutil.which("nvidia-smi"):
        return names
    out = _run_text(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    for line in out.splitlines():
        line = line.strip()
        if line and line.lower() != "name":
            names.append(line)
    return names


def _gpu_names_windows() -> List[str]:
    names: list[str] = []
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object -ExpandProperty Name"
    )
    out = _run_text(["powershell", "-NoProfile", "-Command", ps])
    for line in out.splitlines():
        line = line.strip()
        if line:
            names.append(line)
    if names:
        return names
    # wmic fallback (older Windows)
    out = _run_text(["wmic", "path", "win32_VideoController", "get", "name"])
    for line in out.splitlines():
        line = line.strip()
        if line and line.lower() != "name":
            names.append(line)
    return names


# ===================================================================
# BUG 3: Linux openers with fallback chain
# ===================================================================

# Ordered list of Linux open commands to try (first-found via shutil.which)
_LINUX_OPENERS = ["xdg-open", "gio", "exo-open", "gnome-open"]


def _find_linux_opener() -> Optional[str]:
    for cmd in _LINUX_OPENERS:
        if shutil.which(cmd):
            return cmd
    return None


def _wsl_windows_path(abspath: str) -> Optional[str]:
    """Map ``/mnt/c/Users/...`` to ``C:\\Users\\...`` for explorer.exe on WSL."""
    normalized = abspath.replace("\\", "/")
    if not normalized.startswith("/mnt/"):
        return None
    parts = [p for p in normalized.split("/") if p]
    if len(parts) < 2:
        return None
    drive = parts[1].upper()
    if len(drive) != 1 or not drive.isalpha():
        return None
    rest = parts[2:]
    if not rest:
        return f"{drive}:\\"
    return f"{drive}:\\" + "\\".join(rest)


def _wsl_explorer_exe() -> Optional[str]:
    for candidate in (
        shutil.which("explorer.exe"),
        "/mnt/c/Windows/explorer.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _open_via_wsl_explorer(abspath: str, *, reveal: bool, is_file: bool) -> bool:
    explorer = _wsl_explorer_exe()
    win_path = _wsl_windows_path(abspath)
    if not explorer or not win_path:
        return False
    try:
        if reveal or is_file:
            escaped = win_path.replace('"', '\\"')
            subprocess.Popen([explorer, f'/select,"{escaped}"'])
        else:
            subprocess.Popen([explorer, win_path])
        return True
    except OSError as exc:
        logger.debug("WSL explorer open failed: %s", exc)
        return False


def _run_opener(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
            logger.warning("Opener failed (%s): %s", " ".join(cmd[:2]), err[:200])
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Opener failed (%s): %s", cmd[0], exc)
        return False


def open_file_or_folder(
    path: str,
    *,
    select_file: Optional[str] = None,
    reveal: bool = False,
) -> None:
    """Open *path* (file or folder) in the OS file manager.

    On Windows:
      - If *reveal* is True (or the path is a file), opens the parent
        folder and selects/highlights the file.
      - Otherwise opens the folder directly.

    On macOS:
      - If *reveal* or ``select_file``, uses ``open -R`` to reveal in Finder.
      - Otherwise uses ``open``.

    On Linux:
      - Uses the first available opener from: ``xdg-open``, ``gio``,
        ``exo-open``, ``gnome-open`` (checked via ``shutil.which``).
    """
    abspath = os.path.abspath(path)
    is_file = os.path.isfile(abspath)
    p = Path(abspath)

    if is_windows():
        # Windows file/folder opening is handled by main.py directly
        return
    if is_macos():
        cmd: list[str]
        if reveal or select_file or is_file:
            cmd = ["open", "-R", abspath]
        else:
            cmd = ["open", abspath]
        if not _run_opener(cmd):
            logger.warning("Could not open path in Finder: %s", abspath)
    elif is_wsl():
        if not _open_via_wsl_explorer(abspath, reveal=reveal, is_file=is_file):
            opener = _find_linux_opener()
            if opener is None:
                logger.warning(
                    "No file opener on WSL (tried explorer.exe and %s)",
                    ", ".join(_LINUX_OPENERS),
                )
                return
            target = str(p.parent) if (is_file and not reveal) else abspath
            if not _run_opener([opener, target]):
                logger.warning("Could not open path on WSL: %s", abspath)
    else:
        opener = _find_linux_opener()
        if opener is None:
            logger.warning("No file opener found on Linux (tried: %s)", ", ".join(_LINUX_OPENERS))
            return
        target = str(p.parent) if (is_file and not reveal) else abspath
        if not _run_opener([opener, target]):
            logger.warning("Could not open path on Linux: %s", abspath)
# ===================================================================
# Platform detection helpers
# ===================================================================


def is_wsl() -> bool:
    """Detect Windows Subsystem for Linux (WSL1/WSL2).

    WSL runs Linux syscalls on top of a Windows kernel. ``os.name`` is
    ``"posix"`` and ``sys.platform`` is ``"linux"`` on WSL, but the
    filesystem is backed by NTFS and Windows executables (``explorer.exe``)
    are accessible via ``/mnt/c/``. Detecting WSL is important because:

    - ``xdg-open`` is often missing or broken (should use ``explorer.exe``)
    - File paths on ``/mnt/c/`` need Windows-level sanitization
    - ``lspci`` works on WSL2 but not WSL1

    Uses ``/proc/version`` ("Microsoft" string) and ``WSL_DISTRO_NAME``
    environment variable as detection signals.
    """
    if not is_linux():
        return False
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
        return "microsoft" in version.lower()
    except OSError:
        return False


def is_cygwin_or_msys() -> bool:
    """Detect Cygwin or MSYS2 environment.

    ``os.name`` is ``"posix"`` but ``sys.platform`` is ``"cygwin"`` or
    ``"msys"``. These environments provide POSIX APIs on Windows but
    lack native Linux tools like ``lspci`` or ``xdg-open``.
    """
    return sys.platform in ("cygwin", "msys")


def is_freebsd() -> bool:
    return sys.platform.startswith("freebsd")


def platform_label() -> str:
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    if is_wsl():
        return "WSL"
    if is_cygwin_or_msys():
        return sys.platform  # "cygwin" or "msys"
    if is_freebsd():
        return "FreeBSD"
    if is_linux():
        return "Linux"
    return sys.platform


# ===================================================================
# Centralised subprocess CREATE_NO_WINDOW flag (Windows only)
# ===================================================================

# Prevents a console window from popping up around subprocesses on Windows.
# Previously duplicated in 7 files — centralised here.
# ponytail: If Windows ever removes CREATE_NO_WINDOW, this becomes a no-op.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ===================================================================
# GPU detection
# ===================================================================

def _gpu_names_linux() -> List[str]:
    names: list[str] = []
    if shutil.which("lspci"):
        out = _run_text(["lspci"])
        for line in out.splitlines():
            if re.search(r"vga|3d|display", line, re.I):
                names.append(line.split(":", 2)[-1].strip())
    return names


def _gpu_names_macos() -> List[str]:
    """Fetch GPU names on macOS via ``system_profiler SPDisplaysDataType``."""
    names: list[str] = []
    out = _run_text(["system_profiler", "SPDisplaysDataType"])
    for line in out.splitlines():
        m = re.match(r"\s*Chipset Model:\s*(.+)", line)
        if m:
            names.append(m.group(1).strip())
        m = re.match(r"\s*Metal Family:\s*(.+)", line)
        if m:
            family = m.group(1).strip()
            if family and "Supported" not in family:
                names.append(f"Apple {family}")
    return names


def list_gpu_names() -> List[str]:
    if is_windows():
        names = _gpu_names_windows()
        for name in _gpu_names_nvidia_smi():
            if name not in names:
                names.append(name)
        return names
    if is_macos():
        return _gpu_names_macos()
    return _gpu_names_linux()


# ===================================================================
# BUG 5: Kill child processes by tracked PID (not broad taskkill/pkill)
# ===================================================================

# Track ffmpeg child PIDs so shutdown only kills *our* processes.
_CHILD_PIDS: set[int] = set()
_CHILD_PIDS_LOCK = threading.Lock()


def register_child_pid(pid: int) -> None:
    """Track a child process PID for targeted cleanup on shutdown."""
    with _CHILD_PIDS_LOCK:
        _CHILD_PIDS.add(pid)


def unregister_child_pid(pid: int) -> None:
    """Remove a PID from tracking (e.g. normal process exit)."""
    with _CHILD_PIDS_LOCK:
        _CHILD_PIDS.discard(pid)


def _pid_looks_like_ffmpeg(pid: int) -> bool:
    """Best-effort guard against PID reuse killing the wrong process."""
    if pid <= 0:
        return False
    if is_windows():
        out = _run_text(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        return "ffmpeg" in out.lower()
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        return "ffmpeg" in cmdline.lower()
    except OSError:
        return False


def _kill_pid(pid: int) -> None:
    """Kill a single process by PID (cross-platform)."""
    if not _pid_looks_like_ffmpeg(pid):
        logger.warning("Skipping kill pid=%d — process is not ffmpeg", pid)
        return
    try:
        if is_windows():
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            os.kill(pid, 9)
    except ProcessLookupError:
        pass  # Already exited
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("kill_pid(%d): %s", pid, exc)


def kill_child_processes() -> None:
    """Kill ffmpeg child processes that we explicitly registered.

    Historically this function also ran ``taskkill /IM ffmpeg.exe`` (image-name
    wildcard) as a fallback for "legacy" ffmpeg processes that hadn't been
    wired into PID tracking. The fallback was removed (F5 in ANTIVIRUS_AUDIT):
    image-name wildcards can match other users' ffmpeg processes, and
    ``taskkill /IM`` is a top-tier EDR heuristic. Every ffmpeg child the
    application launches is now tracked via ``register_child_pid`` at
    ``ytdlp_service._track_ffmpeg_proc`` (and equivalent call sites).
    If a process is somehow not tracked, it will be left running — the user's
    next download will not be harmed, and a phantom kill of another user's
    ffmpeg is the worst-case behaviour we are explicitly avoiding.
    """
    with _CHILD_PIDS_LOCK:
        pids = list(_CHILD_PIDS)
        _CHILD_PIDS.clear()
    for pid in pids:
        logger.info("Killing tracked ffmpeg child pid=%d", pid)
        _kill_pid(pid)


# ===================================================================
# BUG 4: Folder picker with cross-platform fallbacks
# ===================================================================


def pick_folder() -> tuple[Optional[str], Optional[str]]:
    """Show a native folder picker dialog.

    Strategy:
      1. tkinter (works on all platforms with Python Tcl/Tk).
      2. Windows fallback: PowerShell + System.Windows.Forms.
      3. macOS fallback: ``osascript`` (AppleScript).
      4. Linux fallback: ``zenity`` (GNOME) or ``kdialog`` (KDE).

    Returns ``(path, error_message)``.
    """
    err_msg: Optional[str] = None
    result_q: queue.Queue = queue.Queue(maxsize=1)

    def _tk_worker() -> None:
        try:
            path = _tk_pick_folder()
            result_q.put(("ok", path))
        # ponytail: survival guarantee for daemon thread worker — catch all to report on result queue
        except Exception as exc:
        # ponytail: best-effort — result_q.put(("ok", path))
            result_q.put(("err", str(exc)))

    t = threading.Thread(target=_tk_worker, daemon=True)
    t.start()
    t.join(timeout=125)
    if not result_q.empty():
        kind, value = result_q.get()
        if kind == "ok" and value:
            return value, None
        if kind == "err":
            err_msg = str(value)

    # Fallbacks per platform
    if err_msg is None:
        err_msg = "Folder picker cancelled or unavailable."

    # Windows fallback handled separately (tkinter on Windows works fine)
    if is_macos():
        path = _pick_folder_macos_fallback()
        if path:
            return path, None
    else:
        path = _pick_folder_linux_fallback()
        if path:
            return path, None

    return None, err_msg


def _tk_pick_folder() -> Optional[str]:
    """Native folder dialog via tkinter."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update_idletasks()
    try:
        path = filedialog.askdirectory(title="Choose download folder", parent=root)
        return path or None
    finally:
        try:
            root.destroy()
        except (tk.TclError, RuntimeError):
            pass

def _pick_folder_macos_fallback() -> Optional[str]:
    """Use AppleScript to show a native folder picker."""
    script = (
        'tell application "System Events"\n'
        '  activate\n'
        '  set folderPath to choose folder with prompt "Choose download folder"\n'
        '  return POSIX path of folderPath\n'
        'end tell'
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        path = (out.stdout or "").strip()
        return path if path else None
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        logger.debug("osascript folder picker failed: %s", exc)
        return None


def _pick_folder_linux_fallback() -> Optional[str]:
    """Try zenity (GNOME) or kdialog (KDE) for native folder picker."""
    if shutil.which("zenity"):
        try:
            out = subprocess.run(
                ["zenity", "--file-selection", "--directory",
                 "--title=Choose download folder"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            path = (out.stdout or "").strip()
            return path if path else None
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            logger.debug("zenity folder picker failed: %s", exc)
    if shutil.which("kdialog"):
        try:
            out = subprocess.run(
                ["kdialog", "--getexistingdirectory", "--title=Choose download folder"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            path = (out.stdout or "").strip()
            return path if path else None
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            logger.debug("kdialog folder picker failed: %s", exc)
    return None


assert _wsl_windows_path("/mnt/c/Users/test/file.mp4") == "C:\\Users\\test\\file.mp4"
