"""Consumer-side cookie bridge — read-only access to cookie_store for app consumers.

M3 wiring: yt-dlp (YouTube cookiefile), Kick API (auth_token/g_session),
Twitch GQL (auth-token/sp). Every entry point here is additive — with the
bridge flag off or the store empty, each returns None and the caller
behaves exactly as before (the regression bar).

The extension push side lives in routers/cookie_bridge.py; this module only
reads what the store already holds. Settings flag is owned by M2
(cookie_bridge_enabled, default True) — getattr keeps consumers working on
installs that predate the flag.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BRIDGE_SUBDIR = "bridge-cookies"
# Refresh the on-disk Netscape export when its last write is older than this
# even if the platform cookie count is unchanged (values rotate with the
# count stable — e.g. SID re-issued on login).
_BRIDGE_REFRESH_TTL_S = 10.0

# platform -> (cookie count at last write, monotonic time of last write)
_export_state: dict[str, tuple[int, float]] = {}


def bridge_enabled(settings=None) -> bool:
    """True when the cookie bridge consumer flag is on.

    ``getattr(..., True)`` keeps this working before M2's flag lands in
    AppSettings. Never raises: any settings-source failure means "enabled"
    and the empty-store checks below still short-circuit to None.
    """
    try:
        s = settings if settings is not None else _current_settings()
        return bool(getattr(s, "cookie_bridge_enabled", True))
    except Exception:
        return True


def _current_settings():
    from deps import settings_mgr

    return settings_mgr.get()


def _bridge_dir() -> Path:
    # Lazily import the module so tests can monkeypatch
    # services.settings._get_appdata_dir (see tests/conftest.py).
    from services import settings as _settings_mod

    return _settings_mod._get_appdata_dir() / _BRIDGE_SUBDIR


def resolve_cookiefile(platform: str = "youtube") -> Optional[str]:
    """Stable Netscape cookiefile for *platform*, refreshed from cookie_store.

    Returns None when the bridge is disabled or the store holds no cookies
    for the platform — callers then fall through to their existing chain.

    The export file is written at most once per ``_BRIDGE_REFRESH_TTL_S``
    per platform, and immediately when the stored cookie count changes, so
    the yt-dlp hot path never writes per request. Never raises.
    """
    if not bridge_enabled():
        return None
    try:
        from services import cookie_store

        count = cookie_store.counts().get(platform, 0)
    except Exception as exc:
        logger.debug("cookie bridge: store unavailable: %s", exc)
        return None
    if not count:
        return None
    path = _bridge_dir() / f"{platform}.txt"
    cached = _export_state.get(platform)
    try:
        fresh = (
            cached is not None
            and cached[0] == count
            and path.is_file()
            and time.monotonic() - cached[1] < _BRIDGE_REFRESH_TTL_S
        )
    except OSError:
        fresh = False
    if fresh:
        return str(path)
    try:
        text = cookie_store.pull_netscape(platform)
    except Exception as exc:
        logger.debug("cookie bridge: netscape pull failed: %s", exc)
        return None
    if not text or "\t" not in text:
        # header-only output — no cookie lines; treat as empty
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="bridge_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
        _export_state[platform] = (count, time.monotonic())
        return str(path)
    except OSError as exc:
        logger.debug("cookie bridge: export write failed: %s", exc)
        return None


def cookie_dict(platform: str) -> Optional[dict[str, str]]:
    """{name: value} of stored bridge cookies for *platform*, or None.

    Used by request builders that want a cookies jar (Kick). None means
    "send no cookies" — requests stay byte-identical without bridge data.
    """
    if not bridge_enabled():
        return None
    try:
        from services import cookie_store

        rows = cookie_store.list_cookies(platform)
    except Exception as exc:
        logger.debug("cookie bridge: list failed: %s", exc)
        return None
    if not rows:
        return None
    return {row["name"]: row["value"] for row in rows}


def cookie_header(platform: str) -> Optional[str]:
    """'name=value; …' Cookie header value, or None (no bridge cookies).

    Used by header-based request builders (Twitch GQL). Merges with the
    caller's existing headers — never replaces them.
    """
    d = cookie_dict(platform)
    if not d:
        return None
    return "; ".join(f"{name}={value}" for name, value in d.items())


# --- module self-check (pure gate logic — no I/O, no network) ---------------
class _FlaglessSettings:
    pass


assert bridge_enabled(_FlaglessSettings()) is True, "missing flag must default to enabled"
_off = _FlaglessSettings()
_off.cookie_bridge_enabled = False
assert bridge_enabled(_off) is False, "explicit disable must be honored"
_on = _FlaglessSettings()
_on.cookie_bridge_enabled = True
assert bridge_enabled(_on) is True
