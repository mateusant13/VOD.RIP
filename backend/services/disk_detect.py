"""Drive detection + cache relocation — stdlib only (ctypes + shutil).

WS-8: large ephemeral on-disk caches (yt-dlp cache, temp files) default to
the fixed drive with the most free space instead of the system drive. AI
model weights are NOT part of this — they follow the AI-models pick
(disk_hygiene.best_model_cache_drive: free space AND speed). Drive
enumeration uses the Win32 API via ctypes because ``os.listdrives`` only
exists on Python 3.12+ and this app ships 3.11.

Relocation moves a cache tree across volumes with a verify step before the
source is removed; same-volume moves are skipped (a rename/copy within one
volume gains nothing and risks double disk usage during the copy).

ponytail: GetLogicalDriveStringsA is ANSI — fine for drive letters (always
ASCII). If a future build targets UNC-only volumes, switch to the W API.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
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
    or None when no fixed drive exists (non-Windows hosts, RAM-only boxes).

    The heavy-cache auto pick: ephemeral data (yt-dlp http cache, temp
    files) is written once and read once, so raw headroom beats speed — the
    biggest free space maximizes how much archive-scale churn the cache can
    absorb before the low-disk warning trips, and the files are re-creatable,
    so losing them costs nothing but re-download time. Distinct from the
    AI-models pick (best_model_cache_drive: free space AND speed — large
    weights, downloaded once) and the data pick (fastest_disk: DB + preview
    media need quick storage).
    """
    best: Optional[str] = None
    best_free = -1
    for drive in fixed_drives():
        free = free_space(drive)
        if free > best_free:
            best, best_free = drive, free
    return best


# A disk with less free space than this is not offered as the "fastest" pick:
# transcripts/chat data needs real headroom for the DB + WAL + vocab snapshots.
_FASTEST_MIN_FREE_BYTES = 2 * 1024**3


def fastest_disk() -> str:
    """Drive root (e.g. 'C:\\') of the fastest usable disk: best speed_rank
    among drives with >= _FASTEST_MIN_FREE_BYTES free; ties broken by most
    free space. '' when no usable drive exists (non-Windows host, probe
    failures). The heavy-cache counterpart is biggest_fixed_drive().
    """
    best = ""
    best_key: Optional[Tuple[int, int]] = None
    for item in disk_inventory():
        if item["free_bytes"] < _FASTEST_MIN_FREE_BYTES:
            continue
        key = (item["speed_rank"], -item["free_bytes"])
        if best_key is None or key < best_key:
            best, best_key = item["drive"], key
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


# --- per-drive inventory + media classification (disk tiering) -------------
# WS/disk-tiering: Settings > Storage picks a "heavy cache disk" (biggest
# free space — throwaway data), a "transcripts & chat data disk" (fastest —
# DB + preview media), and an "AI models folder" (best value: free space +
# speed credit — large weights, downloaded once). Space comes from
# ctypes/shutil; media/bus classification comes from one cached PowerShell
# call (Get-PhysicalDisk/Get-Partition). Every probe fails soft — an
# unreadable drive or missing PowerShell just yields Unknown ranking.

_LAYOUT_TTL_SEC = 60  # PowerShell classification cache TTL
_layout_cache: dict = {}  # {"ts": float, "data": dict}


def _drive_usage(drive: str) -> Optional[Tuple[int, int]]:
    """(total_bytes, free_bytes) for a drive root, or None on error (e.g. an
    empty CD-ROM has no filesystem to measure)."""
    try:
        u = shutil.disk_usage(drive)
        return int(u.total), int(u.free)
    except OSError:
        return None


def _volume_label(drive: str) -> str:
    """Volume label via GetVolumeInformationW ('' on error / non-Windows)."""
    if os.name != "nt":
        return ""
    try:
        buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong()
        max_comp = ctypes.c_ulong()
        flags = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive),
            buf,
            261,
            ctypes.byref(serial),
            ctypes.byref(max_comp),
            ctypes.byref(flags),
            fs_buf,
            261,
        )
        return buf.value if ok else ""
    except (AttributeError, OSError):
        return ""


def _run_powershell(script: str) -> Optional[dict]:
    """Run a PowerShell one-liner and parse stdout as JSON; None on any
    failure (missing powershell, non-zero exit, unparseable output). Fail-soft
    by design — callers fall back to Unknown classification."""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,  # Storage module cold start can take ~20s
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        parsed = json.loads(proc.stdout.strip() or "null")
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _storage_layout() -> dict:
    """Physical-disk classification for each drive letter, cached 60s.

    One PowerShell call returns the disk table (DeviceId -> FriendlyName /
    MediaType / BusType) and the partition table (DriveLetter ->
    DiskNumber); the letter-to-disk hop is what maps a drive letter to its
    physical media. Returns {"disks": {id: {...}}, "letters": {L: id}}; any
    probe failure returns an empty mapping (everything classifies Unknown).
    """
    now = time.time()
    cached = _layout_cache.get("ts")
    if cached is not None and now - cached < _LAYOUT_TTL_SEC:
        return _layout_cache["data"]
    data: dict = {"disks": {}, "letters": {}}
    raw = _run_powershell(
        # @(...) forces arrays so empty cmdlet output serializes as []
        # instead of a broken "disks = ;" statement.
        "@{ disks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | "
        "Select-Object DeviceId, FriendlyName, MediaType, BusType); "
        "partitions = @(Get-Partition -ErrorAction SilentlyContinue | "
        "Select-Object DriveLetter, DiskNumber) } | ConvertTo-Json -Depth 5"
    )
    if raw is not None:
        for d in raw.get("disks") or []:
            try:
                data["disks"][int(d["DeviceId"])] = {
                    "friendly_name": str(d.get("FriendlyName") or ""),
                    "media_type": str(d.get("MediaType") or "Unknown"),
                    "bus_type": str(d.get("BusType") or "Unknown"),
                }
            except (KeyError, TypeError, ValueError):
                continue
        for p in raw.get("partitions") or []:
            letter = p.get("DriveLetter")
            if letter:
                try:
                    data["letters"][str(letter).upper()] = int(p["DiskNumber"])
                except (KeyError, TypeError, ValueError):
                    continue
    _layout_cache["ts"] = now
    _layout_cache["data"] = data
    return data


def _speed_rank(media_type: str, bus_type: str) -> int:
    """1=NVMe, 2=SSD, 3=HDD, 4=Unknown.

    Classification is bus/media-table based, not benchmarked — a SATA SSD
    ranks below an NVMe drive even if it wins in practice.
    ponytail: upgrade path is a measured rank (e.g. CrystalDiskMark-style
    sequential read via a small local benchmark cached like _storage_layout);
    ship that only if bus classification ever misleads real users.
    """
    if "NVMe" in bus_type or media_type == "NVMe":
        return 1
    if media_type == "SSD" or bus_type == "SSD":
        return 2
    if media_type == "HDD" or bus_type == "HDD":
        return 3
    return 4


def disk_inventory() -> List[dict]:
    """Per-drive-letter inventory for the Storage pickers.

    Each entry: {drive, label, total_bytes, free_bytes, media_type,
    bus_type, speed_rank}. Drives without a usable filesystem are skipped;
    classification failures degrade to media_type/bus_type 'Unknown'
    (speed_rank 4). Never raises.
    """
    drives = _list_drives()
    if not drives:
        return []
    layout = _storage_layout()
    disks = layout["disks"]
    letters = layout["letters"]
    items: List[dict] = []
    for drive in sorted(drives):
        usage = _drive_usage(drive)
        if usage is None:
            continue
        total_bytes, free_bytes = usage
        letter = drive[0].upper()
        disk = disks.get(letters.get(letter)) or {}
        media_type = str(disk.get("media_type") or "Unknown")
        bus_type = str(disk.get("bus_type") or "Unknown")
        items.append(
            {
                "drive": drive,
                "label": _volume_label(drive),
                "total_bytes": total_bytes,
                "free_bytes": free_bytes,
                "media_type": media_type,
                "bus_type": bus_type,
                "speed_rank": _speed_rank(media_type, bus_type),
            }
        )
    return items


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
