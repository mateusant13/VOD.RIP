"""Cookies straight from a running Chromium via the DevTools Protocol.

Why this exists next to :mod:`services.browser_cookie_extract`:

Reading the cookie SQLite file works only while the browser is closed (a
running Chrome denies even a read of ``<profile>/Network/Cookies``), and on a
modern profile the values come back *app-bound* encrypted (v20) anyway. App
bound keys live in the browser's own sandbox and cannot be unwrapped from
outside it - measured 2026-10-20: Edge's store decrypted fine, its YouTube
cookies were all v20 and refused.

The DevTools Protocol asks the BROWSER for the cookies. The browser hands back
plaintext - it does not need to decrypt anything for you - which is why this
path is immune to both problems. Same mechanism the sibling projects use
(``Network.getCookies`` over a CDP websocket).

Nothing here writes, navigates or evaluates: ``Storage.getCookies`` and
``Network.getAllCookies`` are both read-only.

The app never opens a debugging port. It only talks to a port something is
already listening on (a browser the user started with
``--remote-debugging-port``).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from services.browser_cookie_extract import _platform_for_host

logger = logging.getLogger(__name__)

# Ports the sibling tooling and Chrome itself use.
DEFAULT_PORTS = (9222, 9223, 9224, 9225, 9340)

_HTTP_TIMEOUT = 1.5
_WS_TIMEOUT = 6.0
_MAX_COOKIES = 4000


def _version_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/json/version"


def discover(ports: Optional[Iterable[int]] = None) -> list[dict[str, Any]]:
    """Return live CDP browser endpoints. A closed port is the normal case."""
    import httpx

    found: list[dict[str, Any]] = []
    for port in ports or DEFAULT_PORTS:
        try:
            r = httpx.get(_version_url(port), timeout=_HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            info = r.json()
        except Exception:  # noqa: BLE001
            continue
        ws = info.get("webSocketDebuggerUrl")
        if not ws:
            continue
        found.append(
            {
                "port": port,
                "browser": info.get("Browser") or "Chromium",
                "webSocketDebuggerUrl": ws,
            }
        )
    return found


class _CdpError(RuntimeError):
    pass


def _call(ws, method: str, params: Optional[dict] = None, *, msg_id: int = 1) -> dict:
    """One request/response round trip on an open DevTools websocket."""
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = ws.recv()
        if not raw:
            raise _CdpError(f"{method}: connection closed")
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("id") != msg_id:
            continue  # an event, not our reply
        if "error" in msg:
            raise _CdpError(f"{method}: {msg['error'].get('message') or msg['error']}")
        return msg.get("result") or {}


def _raw_cookies(url: str) -> list[dict]:
    """Read every cookie the browser holds, at browser scope (no page needed)."""
    import websocket  # websocket-client

    # No Origin header: Chrome 111+ rejects a mismatched one, and a non-browser
    # client legitimately has none.
    ws = websocket.create_connection(url, timeout=_WS_TIMEOUT, suppress_origin=True)
    try:
        try:
            result = _call(ws, "Storage.getCookies", msg_id=1)
        except _CdpError:
            # Older builds expose it only through a page target.
            result = _call(ws, "Network.getAllCookies", msg_id=2)
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return list(result.get("cookies") or [])


def _to_store_shape(raw: dict) -> Optional[dict]:
    """CDP cookie -> the dict :func:`cookie_store.upsert_cookies` expects."""
    name = str(raw.get("name") or "")
    domain = str(raw.get("domain") or "")
    if not name or not domain:
        return None
    value = raw.get("value")
    if not isinstance(value, str):
        return None
    expires = raw.get("expires")
    try:
        exp = float(expires) if expires is not None else -1.0
    except (TypeError, ValueError):
        exp = -1.0
    is_session = exp <= 0
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": raw.get("path") or "/",
        "is_session": is_session,
        "expires": None if is_session else int(exp),
        "secure": bool(raw.get("secure", True)),
        "httponly": bool(raw.get("httpOnly", False)),
    }


def fetch_platform_cookies(
    ports: Optional[Iterable[int]] = None,
) -> tuple[dict[str, list[dict]], str]:
    """Return ``(cookies_by_platform, reason)`` from a running browser.

    An empty dict plus a reason is the normal answer when no browser is
    exposing a DevTools port - callers fall back to the on-disk reader.
    Never raises; values are never logged.
    """
    endpoints = discover(ports)
    if not endpoints:
        return {}, "no browser DevTools port is open"

    reasons: list[str] = []
    best: dict[str, list[dict]] = {}
    for ep in endpoints:
        try:
            raw = _raw_cookies(ep["webSocketDebuggerUrl"])
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{ep['browser']} :{ep['port']}: {type(exc).__name__}")
            continue
        if not raw:
            continue
        by_platform: dict[str, list[dict]] = {}
        for item in raw[:_MAX_COOKIES]:
            cookie = _to_store_shape(item)
            if cookie is None:
                continue
            platform = _platform_for_host(cookie["domain"])
            if platform is None:
                continue
            by_platform.setdefault(platform, []).append(cookie)
        if by_platform and sum(len(v) for v in by_platform.values()) > sum(
            len(v) for v in best.values()
        ):
            best = by_platform
            logger.info(
                "cdp cookies: %s :%d -> %s",
                ep["browser"],
                ep["port"],
                {k: len(v) for k, v in sorted(by_platform.items())},
            )
    if best:
        return best, ""
    return {}, "; ".join(reasons) or "no matching cookies on any DevTools port"
