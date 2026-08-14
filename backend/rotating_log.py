"""File-like wrapper over logging.handlers.RotatingFileHandler (stdlib only).

DISK-06: the supervisor logs (worker.log, background.log, server-<port>.log)
used to append unbounded. This gives them the same 5 MB x 3 backup rotation
as __main_launcher__'s app.log while keeping the file-like API the server
tee loops use (_log writes lines, close flushes). Content is byte-identical
to the old raw writes — rotation only renames when a line crosses 5 MB.
"""
from __future__ import annotations

import logging.handlers
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


class RotatingLogFile:
    """Minimal ``.write/.flush/.close`` handle backed by a RotatingFileHandler.

    Rotation is checked on every write (stream size + next line length), so
    the file rolls at ~5 MB without ever routing through the logging
    formatter — the caller keeps full control of line content.
    """

    def __init__(self, path, max_bytes: int = MAX_BYTES, backup_count: int = BACKUP_COUNT):
        self.path = Path(path)
        self._handler = logging.handlers.RotatingFileHandler(
            str(self.path), maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8",
        )

    def write(self, msg: str) -> None:
        if not msg:
            return
        try:
            if self._handler.stream.tell() + len(msg.encode("utf-8", "replace")) >= self._handler.maxBytes:
                self._handler.doRollover()
            self._handler.stream.write(msg)
            self._handler.stream.flush()
        except OSError:
            pass

    def flush(self) -> None:
        try:
            self._handler.stream.flush()
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._handler.close()


def open_rotating(path, max_bytes: int = MAX_BYTES, backup_count: int = BACKUP_COUNT) -> RotatingLogFile:
    """Open ``path`` for append with rotation (5 MB x 3 by default)."""
    return RotatingLogFile(path, max_bytes=max_bytes, backup_count=backup_count)
