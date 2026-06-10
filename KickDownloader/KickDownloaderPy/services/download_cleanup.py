"""Remove partial download artifacts after cancel or failure."""

from __future__ import annotations

import glob
import os
from pathlib import Path


def delete_partial_output(output_file: str, output_existed: bool) -> None:
    """Delete in-progress download files. Skips when output existed before we started."""
    if output_existed or not output_file:
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
