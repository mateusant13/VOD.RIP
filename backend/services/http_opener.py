"""One SSL context and one opener for the whole process.

Measured 2026-10-13 with py-spy against a SILENT API process. The MainThread was
idle in asyncio _poll - the event loop was fine - while a worker thread held the
GIL inside:

    _load_windows_store_certs (ssl.py:588)
    load_default_certs (ssl.py:596)
    create_default_context (ssl.py:775)
    __init__ (http/client.py:1437)
    urlopen (urllib/request.py:216)

urllib.request.urlopen() builds a NEW HTTPSHandler for every call, and that calls
ssl.create_default_context(), which on Windows enumerates the ENTIRE certificate
store. It is slow and it holds the GIL, so the event loop cannot run, /api/health
stops answering, and the supervisor restarts an API that was never hung - the
"o backend sempre cai" the owner reported, with a different cause each time.

Import `open_url` (or the OPENER) instead of calling urllib.request.urlopen
directly. The store is then loaded ONCE per process.
"""
from __future__ import annotations

import ssl
import urllib.request

__all__ = ["OPENER", "open_url"]

SSL_CONTEXT = ssl.create_default_context()
OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CONTEXT))


def open_url(req, timeout: float = 20.0):
    """urlopen through the shared opener - same signature, one SSL context."""
    return OPENER.open(req, timeout=timeout)
