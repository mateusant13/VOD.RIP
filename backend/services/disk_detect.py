"""Drive detection + cache relocation — stdlib only (ctypes + shutil).

WS-8: large on-disk caches (whisper models, yt-dlp cache, preview temp,
embed models) default to the drive with the most free space instead of the
system drive. Drive enumeration uses the Win32 API via ctypes because
``os.listdrives`` only exists on Python 3.12+ and this app ships 3.11.

Relocation moves a cache tree across volumes with a verify step before the
source is removed; same-volume moves are skipped (a rename/copy within one
volume gains nothing and risks double disk usage during the copy).

ponytail: GetLogicalDriveStringsA is ANSI — fine for drive letters (always
ASCII). If a future build targets UNC-only volumes, switch to the W API.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

_MAX_DRIVE_STRING_BUFFER = 512


def _list_drives() -> List[str]:
    """Return drive roots like ['C:\\', 'D:\\'] via GetLogicalDriveStringsA.

    Empty on non-Windows platforms (no drives to enumerate)."""
    if os.name != "nt":
        return []
    try:
        buf = ctypes.create_string_buffer(_MAX_DRIVE_STRING_BUFFER)
        if not ctypes.windll.kernel32.GetLogicalDriveStringsA(_MAX_DRIVE_STRING_BUFFER, buf):
            return []
        return [d.decode("ascii", errors="replace") for d in buf.raw.split(b"\0") if d]
    except (AttributeError, OSError):
        return []


def _drive_type(drive: str) -> int:
    """Win32 drive type constant (DRIVE_FIXED=3, DRIVE_REMOVABLE=2, ...)."""
    if os.name != "nt":
        return DRIVE_UNKNOWN
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive))
    except (AttributeError, OSError):
        return DRIVE_UNKNOWN


def _drive_rank(drive_type: int) -> int:
    """Fixed drives first, removable second, everything else last."""
    if drive_type == DRIVE_FIXED:
        return 3
    if drive_type == DRIVE_REMOVABLE:
        return 2
    return 1


def free_space(path) -> int:
    """Free bytes on the volume containing *path* (0 on error)."""
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def _ranked(drives: List[Tuple[str, int, int]]) -> List[Tuple[str, int]]:
    """Sort [(letter, drive_type, free_bytes)] by type rank desc then free
    desc -> [(letter, free_bytes)]. Pure — unit-testable without real disks."""
    return [
        (letter, free)
        for letter, _type, free in sorted(
            drives, key=lambda t: (-_drive_rank(t[1]), -t[2])
        )
    ]


def ranked_drives() -> List[Tuple[str, int]]:
    """All drive letters ordered by (type rank, free bytes) — for UI/debug."""
    return _ranked([(d, _drive_type(d), free_space(d)) for d in _list_drives()])


def fixed_drives() -> List[str]:
    return [d for d in _list_drives() if _drive_type(d) == DRIVE_FIXED]


def biggest_fixed_drive() -> Optional[str]:
    """Drive root (e.g. 'D:\\') of the fixed drive with the most free space,
    or None when no fixed drive exists (non-Windows hosts, RAM-only boxes)."""
    best: Optional[str] = None
    best_free = -1
    for drive in fixed_drives():
        free = free_space(drive)
        if free > best_free:
            best, best_free = drive, free
    return best


# --- relocation -----------------------------------------------------------

def _same_volume(a: Path, b: Path) -> bool:
    """True when both paths live on the same volume (conservative on error:
    a failed probe means 'don't move')."""
    try:
        return os.path.splitdrive(os.path.abspath(a))[0].lower() == os.path.splitdrive(
            os.path.abspath(b)
        )[0].lower()
    except OSError:
        return True


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_tree_verified(src: Path, dst: Path) -> None:
    """Copy *src* into *dst*, verify (file count + one sha256), then remove
    *src*. Raises OSError on any mismatch — the caller keeps *src* intact."""
    if dst.exists():
        raise OSError(f"destination exists: {dst}")
    shutil.copytree(src, dst)
    src_files = sorted(p for p in src.rglob("*") if p.is_file())
    dst_files = sorted(p for p in dst.rglob("*") if p.is_file())
    if len(src_files) != len(dst_files):
        raise OSError(
            f"file count mismatch after copy: src={len(src_files)} dst={len(dst_files)}"
        )
    if src_files and _file_sha256(src_files[0]) != _file_sha256(dst_files[0]):
        raise OSError(f"checksum mismatch after copy: {src_files[0]}")
    shutil.rmtree(src)


def relocate_cache(src_dir, dst_dir) -> dict:
    """Move a cache tree to another volume; same-volume or failure -> no-op.

    Returns {"moved": bool, "reason": str}. The source is removed only AFTER
    the destination is verified (file count + one sha256); any failure leaves
    the source intact and the caller can retry or surface the reason.
    """
    src = Path(src_dir)
    dst = Path(dst_dir)
    if not src.is_dir():
        return {"moved": False, "reason": "source-missing"}
    if _same_volume(src, dst):
        return {"moved": False, "reason": "same-volume-skip"}
    try:
        _copy_tree_verified(src, dst)
    except OSError as exc:
        return {"moved": False, "reason": f"copy-failed: {exc}"}
    return {"moved": True, "reason": "copied-and-verified"}


# --- module self-check (env-guarded: creates scratch temp dirs at import) --
if os.environ.get("VODRIP_DISK_DETECT_SELFCHECK") == "1":
    _scratch = Path(tempfile.mkdtemp(prefix="vodrip-disk-detect-selfcheck-"))
    try:
        _ranked_list = ranked_drives()
        _biggest = biggest_fixed_drive()
        _expected = next(
            (d for d, _ in _ranked_list if _drive_type(d) == DRIVE_FIXED), None
        )
        assert _biggest == _expected, (
            f"biggest_fixed_drive()={_biggest!r} must be the top-ranked fixed "
            f"drive={_expected!r} (ranked={_ranked_list})"
        )
        assert all(isinstance(f, int) and f >= 0 for _, f in _ranked_list)
        print(f"disk_detect self-check: ranked={_ranked_list} biggest_fixed_drive={_biggest!r}")

        # Relocation: same-volume skip, then forced cross-volume copy.
        _src = _scratch / "src"
        _dst = _scratch / "dst"
        _src.mkdir()
        (_src / "sub").mkdir()
        (_src / "a.bin").write_bytes(b"x" * 100)
        (_src / "sub" / "b.bin").write_bytes(b"hello world")
        _r = relocate_cache(_src, _dst)
        assert _r == {"moved": False, "reason": "same-volume-skip"}, _r
        assert _src.is_dir(), "same-volume skip must not touch the source"
        _orig_same_volume = _same_volume
        _same_volume = lambda a, b: False  # type: ignore[assignment]  # force cross-volume
        try:
            _r = relocate_cache(_src, _dst)
        finally:
            _same_volume = _orig_same_volume
        assert _r["moved"] is True, _r
        assert not _src.exists(), "verified copy must remove the source"
        assert (_dst / "sub" / "b.bin").read_bytes() == b"hello world"
        print("disk_detect self-check: relocation copy+verify+remove OK")
    finally:
        shutil.rmtree(_scratch, ignore_errors=True)
