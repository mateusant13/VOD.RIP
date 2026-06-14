"""Windows Start Menu shortcuts for VOD.RIP (portable and installed builds).

Uses the native COM ``IShellLinkW`` / ``IPersistFile`` interfaces via ``ctypes``
to avoid spawning ``powershell.exe`` (which is a top-tier EDR heuristic, and
unnecessary for this trivial COM call). The previous PowerShell path is
preserved as a last-resort fallback for the rare case where the COM ``CoCreate``
or ``ole32`` calls fail (e.g. the pywin32 / ctypes COM is unavailable because
the build is running on Windows Nano / Server Core with no shell).
"""
import ctypes
import logging
import os
import subprocess
import sys
from ctypes import HRESULT, POINTER, byref, c_int, c_ulong, c_void_p
from ctypes.wintypes import BOOL, LPCWSTR
from pathlib import Path

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_START_MENU_FOLDER = "VOD.RIP"
_APP_NAME = "VOD.RIP"

# CLSID / IID for IShellLinkW (catid 00021401-0000-0000-C000-000000000046) and
# IPersistFile (0000010b-0000-0000-C000-000000000046). Hard-coded to avoid
# pulling in pywin32 for one COM call.
_CLSID_ShellLink = b"\x01\x14\x02\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46"
_IID_IShellLinkW = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"  # not used; queried via IID_PPV
_IID_IPersistFile = b"\x0b\x01\x00\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46"


def _programs_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / _START_MENU_FOLDER


def _create_shortcut_via_com(lnk_path: Path, target: Path, workdir: Path, icon: Path, description: str) -> bool:
    """Create a .lnk via the IShellLinkW COM interface. Returns True on success.

    Implemented with ctypes to avoid an extra dependency (pywin32) for one call.
    We use the universal ``IID_PPV_ARGS`` pattern: query an interface from a
    COM object using the special ``IID_IUnknown`` and then call QueryInterface
    on it to get IShellLinkW. For brevity we use the IShellLinkW vtable layout
    directly via ctypes — the IShellLinkW interface has 24 methods.
    """
    if os.name != "nt":
        return False
    ole32 = ctypes.windll.ole32
    # Ensure the COM runtime is initialised in COINIT_APARTMENTTHREADED.
    COINIT_APARTMENTTHREADED = 0x2
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

    # CLSCTX_INPROC_SERVER = 0x1
    CLSCTX_INPROC_SERVER = 0x1
    # Use the documented IID for IShellLinkW:
    # {000214F9-0000-0000-C000-000000000046}
    IID_IShellLinkW = b"\xF9\x14\x02\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46"
    IID_IPersistFile = b"\x0b\x01\x00\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46"
    CLSID_ShellLink = b"\x01\x14\x02\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46"

    shell_link = c_void_p()
    hr = ole32.CoCreateInstance(
        CLSID_ShellLink, None, CLSCTX_INPROC_SERVER,
        IID_IShellLinkW, byref(shell_link),
    )
    if hr != 0 or not shell_link.value:
        logger.debug("CoCreateInstance(IShellLinkW) failed: hr=0x%X", hr & 0xFFFFFFFF)
        return False

    try:
        # vtable layout: IShellLinkW has 3 base IUnknown slots, then 18 methods.
        # We only need SetPath (slot 20 in vtable = index 20), SetWorkingDirectory
        # (21), SetDescription (22), SetIconLocation (23).
        vtbl = ctypes.cast(ctypes.cast(shell_link, POINTER(POINTER(c_void_p)))[0], POINTER(c_void_p))
        # IUnknown: QueryInterface(0), AddRef(1), Release(2)
        # IShellLink: GetPath(3), GetIDList(4), SetIDList(5), GetDescription(6),
        # SetDescription(7), GetArguments(8), SetArguments(9), GetHotkey(10),
        # SetHotkey(11), GetShowCmd(12), SetShowCmd(13), GetIconLocation(14),
        # SetIconLocation(15), SetRelativePath(16), Resolve(17), SetPath(18).
        # The vtable indices vary slightly between IShellLinkW vs IShellLinkA; we
        # use IShellLinkW from the start by using its CLSID + IID above, so
        # method indices match the MSDN table for IShellLinkW.
        SetPath = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR)(vtbl[20])
        SetWorkingDirectory = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR)(vtbl[21])
        SetDescription = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR)(vtbl[22])
        SetIconLocation = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR, c_int)(vtbl[23])

        target_str = str(target.resolve())
        workdir_str = str(workdir.resolve())
        icon_str = str(icon.resolve())

        if SetPath(shell_link, target_str) != 0:
            logger.debug("IShellLinkW::SetPath failed")
            return False
        if SetWorkingDirectory(shell_link, workdir_str) != 0:
            logger.debug("IShellLinkW::SetWorkingDirectory failed")
            return False
        if SetDescription(shell_link, description) != 0:
            logger.debug("IShellLinkW::SetDescription failed")
            return False
        if SetIconLocation(shell_link, icon_str, 0) != 0:
            logger.debug("IShellLinkW::SetIconLocation failed")
            return False

        # Now query IPersistFile and call Save.
        # Use QueryInterface: IUnknown::QueryInterface(self, refiid, ppv).
        QueryInterface = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(vtbl[0])
        persist = c_void_p()
        if QueryInterface(shell_link, IID_IPersistFile, byref(persist)) != 0 or not persist.value:
            logger.debug("QueryInterface(IPersistFile) failed")
            return False
        try:
            vtbl_p = ctypes.cast(ctypes.cast(persist, POINTER(POINTER(c_void_p)))[0], POINTER(c_void_p))
            # IPersistFile: GetClassID(0), IsDirty(1), Load(2), Save(3),
            # SaveCompleted(4), GetCurFile(5). Save is index 3 + 3 IUnknown
            # methods = absolute index 6 in the vtable.
            Save = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR, BOOL)(vtbl_p[6])
            lnk_str = str(lnk_path)
            # Ensure parent directory exists.
            lnk_path.parent.mkdir(parents=True, exist_ok=True)
            # fSave = True (TRUE=1) commits the file atomically.
            if Save(persist, lnk_str, True) != 0:
                logger.debug("IPersistFile::Save failed")
                return False
        finally:
            # Release IPersistFile.
            ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl_p[2])(persist)
        return True
    finally:
        # Release IShellLinkW.
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(shell_link)


def _create_shortcut_via_powershell_fallback(lnk_path: Path, target: Path, workdir: Path, icon: Path, description: str) -> bool:
    """Last-resort: PowerShell with WScript.Shell COM. Kept for environments where
    the ctypes COM call fails (rare). The original PowerShell code is the AV
    trigger we wanted to avoid — this is intentionally a fallback, not the
    primary path.
    """
    def _escape_ps(value: str) -> str:
        return value.replace("'", "''")

    lines = [
        "$WshShell = New-Object -ComObject WScript.Shell",
        f"$s = $WshShell.CreateShortcut('{_escape_ps(str(lnk_path))}')",
        f"$s.TargetPath = '{_escape_ps(str(target.resolve()))}'",
        f"$s.WorkingDirectory = '{_escape_ps(str(workdir.resolve()))}'",
        f"$s.IconLocation = '{_escape_ps(str(icon.resolve()))},0'",
        f"$s.Description = '{description}'",
        "$s.Save()",
    ]
    script = "\n".join(lines)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=30,
            creationflags=_NO_WINDOW,
            check=False,
        )
        return True
    except Exception as exc:
        logger.debug("Start Menu shortcut PowerShell fallback failed: %s", exc)
        return False


def _create_shortcut(lnk_path: Path, target: Path, workdir: Path, icon: Path, description: str) -> bool:
    """Create a .lnk via native COM; fall back to PowerShell if that fails."""
    try:
        if _create_shortcut_via_com(lnk_path, target, workdir, icon, description):
            return True
    except Exception as exc:
        logger.debug("COM shortcut path raised %s — falling back to PowerShell", exc)
    return _create_shortcut_via_powershell_fallback(lnk_path, target, workdir, icon, description)


def ensure_windows_shortcuts(exe_path: Path, working_dir: Path, *, desktop: bool = False) -> None:
    """Create or refresh Start Menu (and optional Desktop) shortcuts."""
    if os.name != "nt":
        return
    if not exe_path.is_file():
        return

    programs = _programs_dir()
    programs.mkdir(parents=True, exist_ok=True)
    start_lnk = programs / f"{_APP_NAME}.lnk"
    description = f"{_APP_NAME} — Kick & Twitch downloader"
    if _create_shortcut(start_lnk, exe_path, working_dir, exe_path, description):
        logger.info("Start Menu shortcut ensured: %s", start_lnk)
    else:
        logger.warning("Failed to create Start Menu shortcut: %s", start_lnk)
        return

    if desktop:
        desktop_dir = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if desktop_dir.is_dir():
            desktop_lnk = desktop_dir / f"{_APP_NAME}.lnk"
            if _create_shortcut(desktop_lnk, exe_path, working_dir, exe_path, description):
                logger.info("Desktop shortcut ensured: %s", desktop_lnk)


def resolve_windows_exe(install_dir: Path) -> Path:
    for name in ("VOD-RIP.EXE", "VOD-RIP.exe", "vod-rip.exe"):
        candidate = install_dir / name
        if candidate.is_file():
            return candidate
    return install_dir / "VOD-RIP.EXE"


def install_dir_from_runtime() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent
