"""Remove partial download artifacts after cancel or failure."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional

from services.os_services import _NO_WINDOW


def _partial_output_candidates(output_file: str) -> List[str]:
    """Return the absolute paths of every partial file a cancelled/failed
    download may have left next to *output_file*.

    Pure function — no I/O beyond ``glob`` enumeration. Extracted from
    ``delete_partial_output`` to keep that function under a sane
    cyclomatic complexity (radon CC 15 -> 6 after the split).
    """
    output_file = os.path.abspath(output_file)
    parent = os.path.dirname(output_file) or "."
    base, ext = os.path.splitext(output_file)
    name = os.path.basename(output_file)
    ext_no_dot = ext.lstrip(".")

    candidates: set[str] = {
        output_file,
        output_file + ".part",
        output_file + ".ytdl",
        base + ".part",
        base + ".ytdl",
        base + ".f" + ext_no_dot + ".part",
        base + ".temp" + ext,
        base + f".{ext_no_dot}.ytdl" if ext_no_dot else base + ".ytdl",
        base + ".mp4.ytdl",
        base + ".mkv.ytdl",
        base + ".ts.ytdl",
    }

    for pattern in (
        output_file + ".*",
        base + ".*",
        name + ".*",
        name + "*.part*",
        base + "*.part*",
        base + "*Frag*.part*",
        name + "*Frag*.part*",
        base + "*.ytdl*",
        name + "*.ytdl*",
        base + "*.temp*",
        name + "*.temp*",
        os.path.join(parent, "*Frag*.part*"),
    ):
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                candidates.add(os.path.abspath(path))

    return sorted(candidates, key=len, reverse=True)


def _try_remove(path: str) -> None:
    """Best-effort ``os.remove`` wrapper. Silent on success and on
    already-missing. Callers don't need the bool; kept returning one
    would be dead return value. Avoids the isfile()+remove TOCTOU race
    by letting FileNotFoundError mean "nothing to do".
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        return


def delete_partial_output(
    output_file: str,
    output_existed: bool,
    expected_duration: Optional[float] = None,
) -> None:
    """Delete in-progress download files. Skips when output existed before we started.

    When ``expected_duration`` is provided the completion check also uses
    ffprobe to verify the file decodes to a length within tolerance, so a
    truncated-but-playable MP4 (ftyp + tiny mdat) doesn't get preserved.
    """
    if output_existed or not output_file:
        return
    # If the file looks complete, leave it alone — sometimes we get here
    # after a successful finish and we don't want to delete a real file.
    if is_video_complete(output_file, expected_duration=expected_duration):
        return

    for path in _partial_output_candidates(output_file):
        _try_remove(path)

    # yt-dlp may leave a zero-byte or tiny placeholder on the output itself.
    try:
        leftover = Path(output_file)
        if leftover.is_file() and leftover.stat().st_size < 1024:
            leftover.unlink(missing_ok=True)
    except OSError:
        pass


def remove_temp_dirs(paths: Optional[Iterable[str]] = None) -> int:
    """Remove an explicit list of temp directories created by THIS download.

    Returns the number of directories actually removed. Best-effort — never
    raises. We take explicit paths (not a prefix scan) so we never wipe a
    sibling download's temp dir when two jobs share an output folder.

    On Windows, open file handles (e.g. an ffmpeg process we just killed)
    can keep the rmtree from succeeding for a few hundred ms, so we retry
    briefly before giving up.
    """
    if not paths:
        return 0
    removed = 0
    for raw in paths:
        if not raw:
            continue
        path = os.path.abspath(raw)
        if not os.path.isdir(path):
            continue
        for _ in range(8):
            try:
                shutil.rmtree(path, ignore_errors=False)
                if not os.path.isdir(path):
                    removed += 1
                    break
            except OSError:
                time.sleep(0.15)
        else:
            # Final attempt: ignore_errors so we don't leak a stale dir if
            # the kernel is still holding handles. We still account for
            # the success so the return value isn't an under-report.
            try:
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.isdir(path):
                    removed += 1
            except OSError:
                pass
    return removed


def _probe_duration_seconds(output_file: str) -> Optional[float]:
    """Return the container duration in seconds via ffprobe, or None on failure.

    Used by ``is_video_complete`` so a partial-but-playable file (e.g. a
    truncated MP4 with moov+small mdat, ~70 KB) doesn't get treated as a
    successful download.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                output_file,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except Exception:
    # ponytail: best-effort — )
        return None
    if out.returncode != 0:
        return None
    try:
        return float(out.stdout.strip())
    except (TypeError, ValueError):
        return None


def is_video_complete(
    output_file: str,
    min_bytes: int = 100_000,
    expected_duration: Optional[float] = None,
    duration_tolerance: float = 1.5,
) -> bool:
    """True if *output_file* exists, has a sane size, and (when ffprobe is
    available) decodes to a duration within tolerance of the expected clip
    length.

    The 100 KB floor rules out the most common "ftyp + tiny mdat" truncations
    that pass a 50 KB check. The ffprobe cross-check is opt-in via
    ``expected_duration``; without it we fall back to size-only.
    """
    if not output_file:
        return False
    try:
        p = Path(output_file)
        if not (p.is_file() and p.stat().st_size >= min_bytes):
            return False
    except OSError:
        return False
    if expected_duration is not None and expected_duration > 0:
        actual = _probe_duration_seconds(output_file)
        if actual is None:
            # ffprobe not available or unreadable — be conservative and
            # treat the file as incomplete so a partial encode gets
            # cleaned up on cancel/failure.
            return False
        return abs(actual - expected_duration) <= duration_tolerance
    return True
