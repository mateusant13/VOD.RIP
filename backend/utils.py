"""
Pure utility functions extracted from ``main.py`` — path building, shell helpers,
format helpers, and channel-browsing helpers.  No FastAPI dependency.
"""

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.os_services import (
    _NO_WINDOW,
    open_file_or_folder,
    pick_folder as os_pick_folder,
    sanitize_filename_component,
)
from services.ytdlp_service import detect_platform

logger = logging.getLogger(__name__)


# ==================== Error formatting ====================


def normalize_err(msg: str, limit: int = 200) -> str:
    """Shorten error messages for UI display."""
    if not msg:
        return ""
    msg = str(msg).strip()
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


def format_platform_error(exc: BaseException) -> str:
    """Human-readable per-platform error (Playwright may raise empty NotImplementedError)."""
    msg = str(exc).strip()
    if msg:
        return msg
    name = type(exc).__name__
    if name == "NotImplementedError":
        return (
            "Playwright subprocess failed (Windows event loop). "
            "Restart the backend; if using dev mode, ensure Kick runs in a worker thread."
        )
    return name


def explain_oserror(e: OSError) -> str:
    """Turn a raw OSError into something a human can act on."""
    msg = str(e) or e.__class__.__name__
    if e.filename:
        return f"{msg} (path: {e.filename!r})"
    return msg


# ==================== Filesystem helpers ====================


def safe_makedirs(path: Path) -> Path:
    """mkdir(parents=True, exist_ok=True) with fallback."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "KickDownloader"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def download_dir(opts) -> Path:
    """Resolve the configured download directory."""
    folder = (opts.download_folder or "").strip()
    if folder:
        return Path(folder)
    return Path.home() / "Downloads"


# ==================== URL / slug helpers ====================


def vod_id_from_url(url: str) -> str:
    platform = detect_platform(url)
    if platform == "Twitch":
        m = re.search(r"/videos/(\d+)", url)
        return m.group(1) if m else ""
    if platform == "Kick":
        m = re.search(
            r"/videos/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            url, re.I,
        )
        return m.group(1)[:8] if m else ""
    return ""


def channel_slug_from_url(url: str) -> str:
    lowered = (url or "").lower()
    m = re.search(r"kick\.com/([^/?#]+)", lowered, re.I)
    if m and m.group(1).lower() not in ("videos", "clips"):
        return m.group(1)
    m = re.search(r"twitch\.tv/([^/?#]+)", lowered)
    if m and m.group(1).lower() not in ("videos", "clip", "directory", "clips"):
        return m.group(1)
    return ""


# ==================== Output path builders ====================


def clip_duration_tag(seconds: Optional[float]) -> str:
    """Filesystem-safe clip length tag, e.g. clip_1m10s or clip_70s."""
    if seconds is None or seconds <= 0:
        return "clip"
    sec = max(1, int(round(seconds)))
    minutes, secs = divmod(sec, 60)
    if minutes > 0:
        return f"clip_{minutes}m{secs}s"
    return f"clip_{secs}s"


def trim_range_tag(crop_start: Optional[float], crop_end: Optional[float]) -> str:
    """Filesystem-safe trim tag like 00m12s-02m30s."""
    def _fmt(sec: float) -> str:
        sec = max(0, int(round(sec)))
        m, s = divmod(sec, 60)
        return f"{m:02d}m{s:02d}s"
    if crop_start is None and crop_end is None:
        return ""
    start = _fmt(crop_start or 0.0)
    end = _fmt(crop_end if crop_end is not None else (crop_start or 0.0) + 1)
    return f"{start}-{end}"


def resolve_output_file_override(req, opts, default_path: str) -> str:
    raw = (req.output_file or "").strip()
    if not raw:
        return default_path
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        return raw
    base = download_dir(opts)
    stem = sanitize_filename_component(Path(raw).stem, fallback="clip")
    return str(base / f"{stem}.mp4")


def build_output_path(req, opts, meta: dict) -> str:
    if req.output_file:
        return req.output_file
    base = download_dir(opts)
    title = meta.get("title") or detect_platform(req.url).lower()
    platform = detect_platform(req.url).lower()
    v_id = vod_id_from_url(req.url)
    duration = meta.get("duration")
    parts: list[str] = [sanitize_filename_component(str(title), fallback="video")]
    dur_tag = clip_duration_tag(duration) if duration else ""
    if dur_tag:
        parts.append(dur_tag)
    parts.append(platform)
    if v_id:
        parts.append(v_id)
    stem = " - ".join([parts[0]] + parts[1:])
    if req.crop_start is not None and req.crop_end is not None:
        tag = trim_range_tag(req.crop_start, req.crop_end)
        if tag:
            stem = f"{stem} [{tag}]"
    stem = sanitize_filename_component(stem, fallback="video")
    ext = "mp3" if getattr(req, "audio_only", False) else "mp4"
    return str(base / f"{stem}.{ext}")


def build_clip_output_path(req, opts, meta: dict) -> str:
    base = download_dir(opts)
    clipper = (
        meta.get("channel")
        or meta.get("uploader")
        or channel_slug_from_url(req.url)
        or "channel"
    )
    title = meta.get("title") or "clip"
    duration = meta.get("duration")
    parts: list[str] = [
        sanitize_filename_component(clipper, fallback="channel"),
        sanitize_filename_component(title, fallback="clip"),
        clip_duration_tag(duration) if duration else "clip",
    ]
    if req.crop_start is not None and req.crop_end is not None:
        tag = trim_range_tag(req.crop_start, req.crop_end)
        if tag:
            parts.append(f"[{tag}]")
    stem = " - ".join(parts)
    stem = sanitize_filename_component(stem, fallback="clip")
    default_path = str(base / f"{stem}.mp4")
    return resolve_output_file_override(req, opts, default_path)


# ==================== Shell / Explorer helpers ====================


def pick_folder_sync() -> tuple[Optional[str], Optional[str]]:
    """Show the native folder picker."""
    return os_pick_folder()


def allow_foreground() -> None:
    """Best-effort unlock so Explorer may take focus after a user click."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
    except Exception:
    # ponytail: ctypes/Win32 API errors only — best-effort foreground unlock
        pass


def _normalize_folder_path(path: str) -> str:
    """Canonical folder path for Explorer HWND matching (custom drives, long paths)."""
    p = (path or "").strip().strip('"')
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    p = os.path.expanduser(p)
    p = os.path.normpath(p)
    try:
        if os.path.exists(p):
            p = os.path.realpath(p)
    except OSError:
        pass
    p = p.rstrip("\\/")
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    return os.path.normcase(p)


def _folders_equivalent(shell_path: str, target: str) -> bool:
    a = _normalize_folder_path(shell_path)
    b = _normalize_folder_path(target)
    if a == b:
        return True
    # Explorer sometimes reports D: while we have D:\
    if a.rstrip("\\") == b.rstrip("\\"):
        return True
    return False


def _explorer_hwnds_for_folder(folder_path: str) -> list[int]:
    """Return HWNDs of Explorer windows showing folder_path (path match, not title)."""
    folder_norm = _normalize_folder_path(folder_path)
    hwnds: list[int] = []
    try:
        import win32com.client
        for window in win32com.client.Dispatch("Shell.Application").Windows():
            try:
                path = window.Document.Folder.Self.Path
                if path and _folders_equivalent(path, folder_norm):
                    hwnd = int(window.HWND)
                    if hwnd:
                        hwnds.append(hwnd)
            except Exception:
                continue
    except ImportError:
        hwnds = _explorer_hwnds_jscript(folder_norm)
    if not hwnds:
        hwnds = _explorer_hwnds_enum_cabinets(folder_norm)
    return hwnds


def _explorer_hwnds_enum_cabinets(folder_norm: str) -> list[int]:
    """Enum CabinetWClass top-level windows and match by Shell folder path."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        matched: list[int] = []

        def _path_for_hwnd(hwnd: int) -> Optional[str]:
            try:
                import win32com.client
                for window in win32com.client.Dispatch("Shell.Application").Windows():
                    if int(window.HWND) == hwnd:
                        return window.Document.Folder.Self.Path
            except Exception:
                return None
            return None

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(256)
            if not user32.GetClassNameW(hwnd, buf, 256):
                return True
            cls = buf.value
            if cls not in ("CabinetWClass", "ExploreWClass"):
                return True
            root = user32.GetAncestor(hwnd, 2) or hwnd
            path = _path_for_hwnd(int(root))
            if path and _folders_equivalent(path, folder_norm):
                matched.append(int(root))
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return matched
    except Exception:
        logger.debug("Cabinet HWND enum failed", exc_info=True)
        return []


def _explorer_hwnds_jscript(folder_norm: str) -> list[int]:
    """Shell.Application path lookup without pywin32 (cscript + JScript)."""
    import tempfile
    escaped = folder_norm.replace("\\", "\\\\").replace('"', '\\"')
    js = f"""
var shell = new ActiveXObject("Shell.Application");
var fso = new ActiveXObject("Scripting.FileSystemObject");
var target = "{escaped}".toLowerCase().replace(/[\\\\/]+$/, "");
function norm(p) {{
  try {{
    p = fso.GetAbsolutePathName(p);
  }} catch (ex) {{}}
  return p.toLowerCase().replace(/[\\\\/]+$/, "");
}}
var out = [];
var e = new Enumerator(shell.Windows());
for (; !e.atEnd(); e.moveNext()) {{
  try {{
    var p = e.item().Document.Folder.Self.Path;
    if (p && norm(p) === target) out.push(e.item().HWND);
  }} catch (ex) {{}}
}}
WScript.Echo(out.join("\\n"));
"""
    script_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
            script_path = f.name
        proc = subprocess.run(
            ["cscript", "//nologo", script_path],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0:
            return []
        return [int(line) for line in proc.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        logger.debug("JScript Explorer HWND lookup failed", exc_info=True)
        return []
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass


if os.name == "nt":
    assert os.path.normcase(os.path.abspath("C:\\foo")) == os.path.normcase("c:/foo")
    assert _normalize_folder_path("D:\\VODs\\") == _normalize_folder_path("D:/VODs")
    assert _folders_equivalent("D:\\VODs", "d:/vods/")


def _pick_topmost_hwnd(user32, ctypes, wintypes, hwnds: list[int]) -> int:
    """Among hwnds, return the one highest in desktop z-order."""
    if not hwnds:
        return 0
    want = set(hwnds)
    picked = 0
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        nonlocal picked
        if hwnd in want:
            picked = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return picked or hwnds[-1]


def _raise_hwnd_foreground(root: int, user32, kernel32, ctypes, wintypes) -> None:
    """Best-effort foreground activation for a top-level HWND."""
    SW_RESTORE = 9
    SW_SHOW = 5
    ASFW_ANY = 0xFFFFFFFF
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    try:
        user32.AllowSetForegroundWindow(ASFW_ANY)
        explorer_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(root, ctypes.byref(explorer_pid))
        if explorer_pid.value:
            user32.AllowSetForegroundWindow(explorer_pid.value)
    except Exception:
        pass  # ponytail: best-effort foreground unlock
    try:
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, 0)
        my_thread = kernel32.GetCurrentThreadId()
        attached = False
        if fg_thread and fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, True)
            attached = True
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.ShowWindow(root, SW_RESTORE)
            user32.ShowWindow(root, SW_SHOW)
            user32.BringWindowToTop(root)
            if hasattr(user32, "SwitchToThisWindow"):
                user32.SwitchToThisWindow(root, True)
            user32.SetForegroundWindow(root)
        finally:
            if attached:
                user32.AttachThreadInput(my_thread, fg_thread, False)
    except Exception:
        pass
    try:
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        user32.SetWindowPos(root, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(root, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    except Exception:
        pass


def focus_explorer_window(folder_path: str, item_name: Optional[str] = None) -> bool:
    """Bring the Explorer window showing folder_path to the foreground."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        GA_ROOT = 2
        hwnds = _explorer_hwnds_for_folder(folder_path)
        hwnd = _pick_topmost_hwnd(user32, ctypes, wintypes, hwnds)
        if not hwnd:
            return False
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        _raise_hwnd_foreground(root, user32, kernel32, ctypes, wintypes)
        return True
    except Exception:
        logger.debug("Could not focus Explorer window", exc_info=True)
        return False


def nudge_explorer_foreground(
    folder_path: str,
    item_name: Optional[str] = None,
    *,
    attempts: int = 1,
    delay: float = 0.0,
) -> None:
    """Raise Explorer after reveal.

    shell_reveal_via_pidl opens Explorer asynchronously, so the window
    may not exist yet.  We retry with a short delay to let the window
    appear before trying to focus it.
    """
    for i in range(max(1, attempts)):
        if focus_explorer_window(folder_path, item_name):
            return
        if i + 1 < attempts and delay > 0:
            time.sleep(delay)


def _schedule_explorer_foreground(folder_path: str, item_name: Optional[str] = None) -> None:
    """Focus the Explorer window once it exists (pywebview steals focus on click)."""

    def _work() -> None:
        for wait in (0.05, 0.15, 0.3, 0.5, 0.75, 1.0, 1.4, 1.9, 2.5, 3.2, 4.0, 5.0):
            time.sleep(wait)
            if focus_explorer_window(folder_path, item_name):
                return
        nudge_explorer_foreground(folder_path, item_name, attempts=20, delay=0.15)

    threading.Thread(target=_work, daemon=True, name="explorer-focus").start()


def ensure_shell_com() -> bool:
    """Initialize COM once per thread for shell reveal calls."""
    if os.name != "nt":
        return False
    from deps import _shell_com_local  # lazy import avoids circular deps
    if getattr(_shell_com_local, "ready", False):
        return True
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        if hr in (0, 1):
            _shell_com_local.ready = True
            return True
    except Exception:
    # ponytail: ctypes/COM errors only — best-effort COM init per thread
        logger.debug("CoInitializeEx failed", exc_info=True)
    return False


def shell_reveal_via_pidl(path: str) -> bool:
    """Reveal a path with SHOpenFolderAndSelectItems."""
    if os.name != "nt":
        return False
    if not ensure_shell_com():
        return False
    try:
        import ctypes
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
        if not getattr(shell32, "_vodrip_pidl_configured", False):
            shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            shell32.ILFree.argtypes = [ctypes.c_void_p]
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong,
            ]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            shell32._vodrip_pidl_configured = True
        allow_foreground()
        abspath = os.path.abspath(path)
        pidl = shell32.ILCreateFromPathW(abspath)
        if not pidl:
            return False
        try:
            result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            return result == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:
    # ponytail: ctypes/Win32 API errors only — best-effort shell reveal
        logger.debug("SHOpenFolderAndSelectItems failed", exc_info=True)
        return False


def explorer_select_arg(path: str) -> str:
    """Build explorer.exe /select,... argument with proper quoting."""
    escaped = path.replace('"', '\\"')
    return f'/select,"{escaped}"'


def reveal_path_windows(target: str) -> None:
    """Reveal a file in Explorer and bring the window forward."""
    allow_foreground()
    abspath = os.path.abspath(target)
    if os.path.isfile(abspath):
        folder = os.path.dirname(abspath)
        item = os.path.basename(abspath)
        reveal_target = abspath
    elif os.path.isdir(abspath):
        folder = abspath
        item = None
        reveal_target = abspath
    else:
        parent = os.path.dirname(abspath)
        if parent and os.path.isdir(parent):
            folder = parent
            item = None
            reveal_target = parent
        else:
            return
    if shell_reveal_via_pidl(reveal_target):
        _schedule_explorer_foreground(folder, item)
        return
    if item and os.path.isfile(abspath):
        subprocess.Popen(
            ["explorer.exe", explorer_select_arg(abspath)],
            creationflags=_NO_WINDOW,
        )
    else:
        subprocess.Popen(
            ["explorer.exe", folder],
            creationflags=_NO_WINDOW,
        )
    _schedule_explorer_foreground(folder, item)


def open_folder_sync(path: str) -> None:
    """Reveal a file in Explorer/Finder, or open its parent folder."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if not p.exists():
        for _ in range(2):
            time.sleep(0.05)
            if p.exists():
                break
    if p.exists():
        target = str(p.resolve())
        if os.name == "nt":
            reveal_path_windows(target)
        else:
            open_file_or_folder(target, reveal=p.is_file())
        return
    parent = p.parent.resolve()
    if not parent.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {parent}")
    folder = str(parent)
    if os.name == "nt":
        reveal_path_windows(folder)
    else:
        open_file_or_folder(folder)


def validate_open_folder_path(path: str, settings_mgr) -> str:
    """Return normalized path if the file or its parent folder exists."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if not p.is_absolute() and not (len(raw) > 1 and raw[1] == ":"):
        p = download_dir(settings_mgr.get()) / p
    if p.exists():
        return str(p.resolve())
    parent = p.parent.resolve()
    if parent.is_dir():
        return str(p.resolve())
    raise FileNotFoundError(f"Folder does not exist: {parent}")


_PLAYABLE_MEDIA_EXTS = frozenset({
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".m4a", ".mp3",
})
_MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}


def validate_local_media_path(path: str, settings_mgr) -> Path:
    """Resolved file under the configured download folder."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if not p.is_absolute() and not (len(raw) > 1 and raw[1] == ":"):
        p = download_dir(settings_mgr.get()) / p
    p = p.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    if p.suffix.lower() not in _PLAYABLE_MEDIA_EXTS:
        raise ValueError(f"Unsupported media type: {p.suffix}")
    dl_root = download_dir(settings_mgr.get()).resolve()
    if not p.is_relative_to(dl_root):
        raise PermissionError("Media must be inside the download folder")
    return p


def media_type_for_path(path: Path) -> str:
    return _MEDIA_MIME.get(path.suffix.lower(), "application/octet-stream")


# ==================== Channel-browsing helpers ====================


def looks_like_clip_entry(entry: dict) -> bool:
    """Clips on Kick/Twitch are short (<=60s) and use clip URLs, not VOD pages."""
    url = (entry.get("url") or "").lower()
    if "/videos/" in url and "/clips/" not in url and "/clip/" not in url:
        return False
    if "/clips/" in url or "clips.twitch.tv" in url:
        pass
    elif "/clip/" in url:
        pass
    elif entry.get("content_kind") != "clip":
        return False
    duration = entry.get("duration")
    if duration is not None:
        try:
            if float(duration) > 60:
                return False
        except (TypeError, ValueError):
            pass
    return True


def filter_clip_entries(entries: List[dict]) -> List[dict]:
    return [e for e in entries if looks_like_clip_entry(e)]


def resolve_channel_slug(raw: str) -> str:
    """Parse a channel login/slug from a bare name or Kick/Twitch URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Could not parse a channel name from the input.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    platform_hint = detect_platform(raw)
    channel: Optional[str] = None
    if platform_hint == "Twitch":
        m = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", raw)
        channel = m.group(1) if m else raw.strip().rstrip("/").split("/")[-1]
    elif platform_hint == "Kick":
        m = re.search(r"kick\.com/([a-zA-Z0-9_]+)", raw)
        channel = m.group(1) if m else raw.strip().rstrip("/").split("/")[-1]
    elif platform_hint == "YouTube":
        m = re.search(r"youtube\.com/@([^/?#]+)", raw, re.I)
        if m:
            channel = m.group(1)
        else:
            m = re.search(r"youtube\.com/channel/([^/?#]+)", raw, re.I)
            channel = m.group(1) if m else raw.strip().rstrip("/").split("/")[-1]
    else:
        channel = raw.strip().rstrip("/").split("/")[-1] or raw.strip()
        if channel.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            channel = urlparse(channel).path.strip("/").split("/")[0] or channel
    if not channel:
        raise ValueError("Could not parse a channel name from the input.")
    return channel


def parse_wanted_platforms(platforms: str) -> List[str]:
    if not platforms or not platforms.strip():
        return ["Twitch", "Kick"]
    return [p.strip() for p in platforms.split(",") if p.strip()]


def parse_video_date(value) -> Optional[datetime]:
    """Best-effort parse of a video's created_at into an aware datetime."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.match(r"^\d{8}$", s):
        try:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        s = s.replace(" ", "T") + "+00:00"
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ==================== Download helpers ====================


def require_hls_crop(req, platform: str) -> None:
    """Validate that crop_start/crop_end are provided for HLS downloads.

    Full-VOD downloads (both None) are allowed — the HLS path will
    download the entire stream.  Partial trim (one provided, one missing)
    is rejected.
    """
    from services.ytdlp_service import is_clip_url
    if is_clip_url(req.url):
        return
    if platform not in ("Twitch", "Kick"):
        return
    # Both None = full VOD download — allowed
    if req.crop_start is None and req.crop_end is None:
        return
    if req.crop_start is None or req.crop_end is None:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="crop_start and crop_end are required for trimmed downloads",
        )
    if req.crop_end <= req.crop_start:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="crop_end must be after crop_start")


def trim_estimated_bytes(meta: dict, crop_start: Optional[float], crop_end: Optional[float]) -> Optional[int]:
    """Scale full-VOD byte estimate to the requested trim window."""
    estimated = meta.get("estimated_bytes")
    if not estimated:
        return None
    duration = meta.get("duration")
    if crop_start is None or crop_end is None or not duration or duration <= 0:
        return int(estimated)
    clip_sec = float(crop_end) - float(crop_start)
    if clip_sec <= 0:
        return int(estimated)
    return int(int(estimated) * clip_sec / float(duration))


async def fetch_queue_meta(url: str, platform: str) -> dict:
    """Best-effort metadata fetch so the queue UI can show VOD info."""
    import asyncio
    from services.kick_api_service import get_clip_info_sync as kick_clip, get_video_info_sync as kick_video
    from services.twitch_gql_service import get_clip_info_sync as twitch_clip, get_video_info_sync as twitch_video
    from services.ytdlp_service import get_video_info, is_clip_url
    from deps import INFO_EXECUTOR
    try:
        loop = asyncio.get_running_loop()
        if is_clip_url(url):
            if platform == "Kick":
                info = await loop.run_in_executor(INFO_EXECUTOR, kick_clip, url)
            elif platform == "Twitch":
                info = await loop.run_in_executor(INFO_EXECUTOR, twitch_clip, url)
            else:
                info = await get_video_info(url)
        elif platform == "Kick":
            info = await loop.run_in_executor(INFO_EXECUTOR, kick_video, url)
        elif platform == "Twitch":
            info = await loop.run_in_executor(INFO_EXECUTOR, twitch_video, url)
        else:
            info = await get_video_info(url)
        if info is None:
            return {}
        if hasattr(info, "model_dump"):
            info = info.model_dump()
        elif not isinstance(info, dict):
            return {}
        return {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "duration_string": info.get("duration_string"),
        }
    except Exception:
    # ponytail: network/metadata errors only — returns empty dict on failure
        return {}


def download_func_for_entry(entry: dict):
    from services.ytdlp_service import detect_platform
    platform = detect_platform(entry["url"])
    dtype = entry.get("type", entry.get("download_type", "video"))
    if platform == "Kick" and dtype in ("video", "clip"):
        from services.kick_api_service import download_vod_sync as kick_download_vod
        return kick_download_vod
    return None


def remove_download_history(download_id: str, download_mgr):
    from fastapi import HTTPException
    if not download_mgr.discard_from_queue(download_id):
        raise HTTPException(status_code=404, detail="Download not found")
    return {"removed": True}
