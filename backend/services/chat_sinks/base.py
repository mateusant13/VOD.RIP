"""Shared chat-sink plumbing: buffered rows flushed to the archive.

One ``ChatSink`` thread per live stream. Subclasses implement ``_run``
(a blocking connect/read loop) and push rows with ``handle_row``. Rows are
buffered in memory and flushed by a per-sink timer thread (``flush_interval``)
or as soon as the buffer reaches ``flush_max`` rows — the archive contract's
"flush every 5s or 100 rows" rule. Default flush target is
``archive_db.insert_messages``; tests inject a counting callback.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_INTERVAL_SEC = 5.0
DEFAULT_FLUSH_MAX_ROWS = 100


class ChatSink(threading.Thread):
    """Daemon thread capturing chat for one live stream."""

    platform: str = ""  # 'twitch' | 'kick' | 'youtube'
    kind: str = "chat"

    def __init__(self, *, video_id: str, channel: str, title: str = "",
                 stream_start_ts: Optional[float] = None,
                 flush_interval: float = DEFAULT_FLUSH_INTERVAL_SEC,
                 flush_max: int = DEFAULT_FLUSH_MAX_ROWS,
                 flush_cb: Optional[Callable[[List[dict]], int]] = None,
                 log: Optional[logging.Logger] = None):
        super().__init__(daemon=True, name=f"{self.kind}-{self.platform}-{video_id[:12]}")
        self.video_id = video_id
        self.channel = channel
        self.title = title
        # Epoch ms of stream start (absolute). None → anchor offsets on the
        # first seen message (sink-side fallback).
        self.stream_start_ts = stream_start_ts
        self._rows: List[dict] = []
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()  # NB: not _stop — that shadows Thread._stop()
        self._flush_cb = flush_cb
        self._flush_interval = float(flush_interval)
        self._flush_max = int(flush_max)
        self.disconnect_reason: Optional[str] = None
        self.rows_flushed = 0
        self._anchor_ms: Optional[float] = None
        self._log = log or logger

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._flusher, daemon=True,
                         name=self.name + "-flush").start()
        super().start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal stop, interrupt the read loop, join, and flush leftovers."""
        self._stop_evt.set()
        self._interrupt()
        self.join(timeout=timeout)
        self.flush()

    def stop_requested(self) -> bool:
        return self._stop_evt.is_set()

    def _interrupt(self) -> None:
        """Subclass hook: close the socket / kill the process to unblock _run."""

    # -- row handling ------------------------------------------------------

    def handle_row(self, row: dict) -> None:
        """Anchor the offset when the parser could not (unknown stream start),
        then buffer the row."""
        ts_ms = _iso_to_epoch_ms(row.get("ts"))
        if row.get("offset_sec") is None and ts_ms is not None:
            if self._anchor_ms is None:
                self._anchor_ms = ts_ms
            row["offset_sec"] = max((ts_ms - self._anchor_ms) / 1000.0, 0.0)
        if row.get("offset_sec") is None:
            # No timestamp at all — receipt-relative; the ts column keeps truth.
            row["offset_sec"] = 0.0
        self.add_row(row)

    def add_row(self, row: dict) -> None:
        with self._lock:
            self._rows.append(row)
            n = len(self._rows)
        if n >= self._flush_max:
            self.flush()

    def flush(self) -> int:
        """Drain buffered rows through the flush callback. Returns count."""
        with self._lock:
            if not self._rows:
                return 0
            rows, self._rows = self._rows, []
        try:
            n = self._flush_cb(rows) if self._flush_cb else self._default_flush(rows)
            self.rows_flushed += n
            return n
        except Exception as exc:
            # ponytail: drop-with-log instead of retrying forever; live chat is
            # lossy-tolerant and a wedged flush must not stall the sink.
            self._log.warning("chat sink %s flush failed (%d rows dropped): %s",
                              self.name, len(rows), exc)
            return 0

    def _default_flush(self, rows: List[dict]) -> int:
        from services import archive_db

        return archive_db.insert_messages(self.platform, self.video_id, rows)

    # -- thread body -------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self.disconnect_reason = f"{type(exc).__name__}: {exc}"
            self._log.warning("chat sink %s ended: %s", self.name, self.disconnect_reason)
        else:
            self.disconnect_reason = self.disconnect_reason or "ended"

    def _run(self) -> None:  # subclass hook
        raise NotImplementedError

    def _flusher(self) -> None:
        while not self._stop_evt.wait(self._flush_interval):
            self.flush()
        # final drain happens in stop()


def _iso_to_epoch_ms(value: Optional[str]) -> Optional[float]:
    """Parse ISO-8601 (Z / ±HH:MM / naive-UTC) to epoch ms."""
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=time.timezone.utc)
    return dt.timestamp() * 1000.0


# Module self-check: buffering + anchor math without starting any thread.
def _selfcheck() -> None:
    flushed: List[List[dict]] = []
    sink = ChatSink(video_id="__selfcheck__", channel="x",
                    flush_interval=999.0, flush_max=3,
                    flush_cb=lambda rows: (flushed.append(rows), len(rows))[1])
    sink.handle_row({"username": "a", "text": "1", "ts": "2026-08-01T22:30:01Z"})
    assert sink._anchor_ms is not None, "missing offset + ts must set anchor"
    assert abs(sink._rows[0]["offset_sec"] - 0.0) < 1e-9
    sink2 = ChatSink(video_id="__selfcheck__", channel="x", flush_interval=999.0,
                     flush_max=10, flush_cb=lambda rows: (flushed.append(rows), len(rows))[1])
    sink2.handle_row({"offset_sec": 1.0, "username": "b", "text": "2"})
    sink2.handle_row({"offset_sec": 5.0, "username": "b", "text": "3"})
    assert sink2._anchor_ms is None, "known offsets must not touch the anchor"
    assert abs(sink2._rows[0]["offset_sec"] - 1.0) < 1e-9
    assert abs(sink2._rows[1]["offset_sec"] - 5.0) < 1e-9, sink2._rows
    sink3 = ChatSink(video_id="__selfcheck__", channel="x", flush_interval=999.0,
                     flush_max=2, flush_cb=lambda rows: (flushed.append(rows), len(rows))[1])
    sink3.add_row({"offset_sec": 1.0, "username": "c", "text": "4"})
    assert len(flushed) == 0, "no flush below flush_max"
    sink3.add_row({"offset_sec": 2.0, "username": "c", "text": "5"})
    assert len(flushed) == 1 and flushed[-1][0]["text"] == "4" \
        and flushed[-1][1]["text"] == "5", "flush_max must auto-flush"
    assert sink3.flush() == 0
    assert _iso_to_epoch_ms("2026-08-01T22:30:00Z") is not None
    assert _iso_to_epoch_ms(None) is None


_selfcheck()
