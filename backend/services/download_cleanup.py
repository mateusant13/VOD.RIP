"""Remove partial download artifacts after cancel or failure."""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path


def delete_partial_output(output_file: str, output_existed: bool) -> None:
    """Delete in-progress download files. Skips when output existed before we started."""
    if output_existed or not output_file:
        return
    # If the file looks complete, leave it alone — sometimes we get here
    # after a successful finish and we don't want to delete a real file.
    if is_video_complete(output_file):
        return

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

    patterns = [
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
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                candidates.add(os.path.abspath(path))

    for path in sorted(candidates, key=len, reverse=True):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    # yt-dlp may leave a zero-byte or tiny placeholder
    try:
        leftover = Path(output_file)
        if leftover.is_file() and leftover.stat().st_size < 1024:
            leftover.unlink(missing_ok=True)
    except OSError:
        pass


def remove_temp_dirs(
    output_file: str,
    prefixes: tuple[str, ...] = ("hls_clip_",),
) -> int:
    """Remove leftover HLS / yt-dlp temp directories near the output file.

    Returns the number of directories removed. Best-effort — never raises.
    """
    if not output_file:
        return 0
    output_file = os.path.abspath(output_file)
    parent = os.path.dirname(output_file) or "."
    removed = 0
    for entry in list(os.scandir(parent)):
        if not entry.is_dir() or not entry.name.startswith(prefixes):
            continue
        try:
            shutil.rmtree(entry.path, ignore_errors=True)
            if not os.path.isdir(entry.path):
                removed += 1
        except OSError:
            pass
    return removed


def is_video_complete(output_file: str, min_bytes: int = 50_000) -> bool:  # noqa: ARG001
    """True if *output_file* exists and looks like a real video (>50 KB by default)."""
    if not output_file:
        return False
    try:
        p = Path(output_file)
        return p.is_file() and p.stat().st_size >= min_bytes
    except OSError:
        return False
