"""Central server/application error logging with latest-500 retention.

Mirrors the live_captions error-ring precedent (services/live_captions.py)
but for the whole app: a process-wide ring plus a JSONL file under the
runtime data-root (``<appdata>/logs/errors.jsonl``) that retains the latest
500 error records. Secrets (cookies/tokens) are stripped before anything is
recorded; the ring never holds full request bodies.

Wire-in (each is independent and idempotent):
  * ``install_error_handler()`` — attach as a root logging.Handler so any
    ``logging.error``/``logger.exception`` (including Starlette's "Exception
    in ASI application" for uncaught 500s) lands in the ring + file.
  * ``record_error(kind, message)`` — call directly from a FastAPI 500
    exception handler (see app.py) to capture the request path/method.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ERROR_RING_MAX = 500
_ERROR_RING: "collections.deque[dict]" = collections.deque(maxlen=_ERROR_RING_MAX)
_ERROR_RING_LOCK = threading.Lock()

# ponytail: a fixed pattern set chosen deliberately small. Upgrade path if a
# new secret field appears: add its key here. Never log full request bodies.
_SECRET_KEYS = (
    "cookie",
    "po_token",
    "poToken",
    "visitor_data",
    "visitorData",
    "authorization",
    "x-api-key",
    "x-goog-visitor-id",
    "x-youtube-identity-token",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
)
_QUERY_PARAM_RE = re.compile(r"([?&](?:[^=&\s]*))=(?P<val>[^&\s]*)")
_URL_RE = re.compile(r"(https?://\S+)")


def _error_log_path() -> Path:
    try:
        from services.settings import _get_appdata_dir

        return _get_appdata_dir() / "logs" / "errors.jsonl"
    except Exception:
        return Path("logs") / "errors.jsonl"


def _sanitize_message(message: str) -> str:
    """Redact secret-bearing values (query params / cookie header values)."""
    text = str(message)

    # Keep common non-secret query params readable; redact the rest so a
    # leaked URL never carries an embedded token (e.g. YouTube/po tokens).
    _INNOCENT_KEYS = {
        "v", "channel", "q", "list", "index", "t", "start", "end",
        "start_time", "end_time", "limit", "offset", "page", "sort",
        "lang", "region", "hl", "gl",
    }

    def _qfix(m: "re.Match[str]") -> str:
        key = m.group(1).lstrip("?&").lower()
        if key in _INNOCENT_KEYS:
            return m.group(0)
        return f"{m.group(1)}=[REDACTED]"

    def _redact_url(match: "re.Match[str]") -> str:
        return _QUERY_PARAM_RE.sub(_qfix, match.group(0))

    text = _URL_RE.sub(_redact_url, text)
    # Strip cookie/authorization header style "key: value" and "key=value",
    # including "Bearer <token>" values (the bearer word + trailing token).
    for key in _SECRET_KEYS:
        text = re.sub(
            rf"(?i)({re.escape(key)}(\s*[:=]\s*))((?:bearer\s+)?[^\s,;\"']+)",
            rf"\1[REDACTED]",
            text,
        )
    return text[:500]


def record_error(kind: str, message: str) -> None:
    """Append an error record (thread-safe) to the ring and the JSONL file."""
    entry = {
        "ts": time.time(),
        "kind": kind,
        "message": _sanitize_message(message),
    }
    with _ERROR_RING_LOCK:
        _ERROR_RING.append(entry)
    try:
        pp = _error_log_path()
        pp.parent.mkdir(parents=True, exist_ok=True)
        with _ERROR_RING_LOCK:
            ts = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).isoformat()
            line = json.dumps({"ts": ts, "kind": kind, "message": entry["message"]}, ensure_ascii=False)
            # Retain exactly the latest _ERROR_RING_MAX records: read existing
            # lines, keep the tail, append the new one, rewrite atomically via
            # temp + replace so a crash never leaves a truncated JSONL.
            lines: list[str] = []
            try:
                if pp.exists():
                    lines = pp.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            lines.append(line)
            lines = lines[-_ERROR_RING_MAX:]
            tmp = pp.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(pp)
    except Exception:
        # The logging path must never itself raise into the request.
        pass


def get_error_ring(limit: int = 50) -> list[dict]:
    """Return the latest up to ``limit`` (max 500) error records."""
    with _ERROR_RING_LOCK:
        return list(_ERROR_RING)[-max(1, min(limit, _ERROR_RING_MAX)):]


def clear_error_ring_for_tests() -> None:
    with _ERROR_RING_LOCK:
        _ERROR_RING.clear()


class _ErrorFileHandler(logging.Handler):
    """Root handler forwarding ERROR+ log records into the ring/file."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "vodrip_error_recorded", False):
                return
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message = f"{message}: {record.exc_info[1]}"
            record_error(record.levelname.lower(), message)
        except Exception:
            pass


_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def install_error_handler(loglevel: int = logging.ERROR) -> "Optional[logging.Handler]":
    """Attach a root ERROR handler once (idempotent). Returns the handler.

    Called from run.py ``_install_logging`` and __main_launcher__
    ``_setup_logging``; the root logger's existing handlers continue to write
    the normal app.log / console.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return None
        handler = _ErrorFileHandler(level=loglevel)
        root = logging.getLogger()
        root.addHandler(handler)
        _INSTALLED = True
        return handler