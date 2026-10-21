"""Keep the cookie store fed from the user's own browser, without a click.

The import path (:func:`services.browser_cookie_cdp.fetch_platform_cookies`
then the on-disk reader) already works, but it needed a button. A preview that
only gets an authenticated ladder after the user finds that button is a preview
that is 360p for everyone who never finds it - and a preview with no cookies
comes back as a single 360 rung, which leaves the engaged 1080 tier with
nothing to mux.

Policy, deliberately conservative:

  * GAP-FILL, plus a narrow REFRESH. A platform with zero stored cookies is
    filled. A platform that already has cookies is normally left ALONE - the
    extension is the push path (cookies.onChanged), and letting a periodically
    imported browser value overwrite a live one is a downgrade dressed as a
    refresh.
    Two facts overturn "never touch an existing set", both measured 2026-10-20:
    the stored YouTube set had been grabbed 10 h earlier and yt-dlp answered
    "Sign in to confirm you're not a bot" while the CDP reader was holding a
    FRESH set in the same second ("cdp cookies: Chrome/155 -> {... youtube ...}"
    immediately followed by "accepted=0 dropped=18"), and the app then kept
    serving the stale file until a human clicked something. So a platform is
    also refreshed when its stored set is older than REFRESH_AFTER_SEC, or when
    that platform's gate is currently FROZEN - the platforms telling us the
    stored set is being rejected. Overwriting stays safe: ``upsert_cookies``
    applies KEEP_LISTS, so a browser that is logged out contributes nothing to
    overwrite with.
  * The bridge kill switch wins. ``cookie_bridge_enabled`` false means no
    ingestion at all, which is what the Settings panel promises.
  * Background only. It runs on a daemon thread after the startup warm wave,
    so a browser that is slow to answer never delays a boot or a preview.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A missing platform is the only thing that triggers a pass; this floor just
# stops a restart loop on a box with nothing to import from hammering the
# DevTools ports every boot.
MIN_INTERVAL_SEC = 3600.0
# How old a stored set may get before a browser read replaces it. 6 h: short
# enough that a day of use never runs an entire afternoon on yesterday's
# YouTube session, long enough that a quiet box is not re-reading the browser
# (and re-encrypting a store) every hour for nothing.
REFRESH_AFTER_SEC = float(
    os.environ.get("VODRIP_COOKIE_REFRESH_AFTER_SEC", "21600")
)

_LOCK = threading.Lock()
_STARTED = False


def _state_path() -> Path:
    from services.cookie_store import _db_path

    return _db_path().parent / "cookie_import.json"


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent/corrupt state is just "never ran"
        return {}


def _write_state(state: dict[str, Any]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("cookie import state not written", exc_info=True)


def _enabled() -> bool:
    try:
        from deps import settings_mgr

        return bool(getattr(settings_mgr.get(), "cookie_bridge_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def _platforms_present() -> set[str]:
    try:
        from services import cookie_store

        return {p for p, n in cookie_store.counts().items() if n > 0}
    except Exception:  # noqa: BLE001
        return set()


def _collect() -> tuple[dict[str, list[dict]], str, str]:
    """``(cookies_by_platform, reason, source)`` - CDP first, files second."""
    from services import browser_cookie_cdp, browser_cookie_extract

    cookies, reason = browser_cookie_cdp.fetch_platform_cookies()
    if cookies:
        return cookies, "", "cdp"
    file_cookies, file_reason = browser_cookie_extract.extract_platform_cookies(None)
    if file_cookies:
        return file_cookies, "", "profile"
    return {}, (reason or file_reason), "none"


def _stale_or_gated(present: set[str]) -> tuple[set[str], set[str]]:
    """``(platforms_to_refresh, platforms_currently_gated)``."""
    from services import cookie_store

    try:
        status = cookie_store.status()
    except Exception:  # noqa: BLE001 - a status read must never block a pass
        return set(), set()
    out: set[str] = set()
    gated_platforms: set[str] = set()
    now = time.time()
    for platform in present:
        gated = False
        try:
            if platform == "youtube":
                from services.yt_gate import youtube_gate_active

                gated = bool(youtube_gate_active())
            elif platform == "kick":
                from services.kick_gate import gate_remaining_sec

                gated = gate_remaining_sec() > 0
        except Exception:  # noqa: BLE001
            gated = False
        if gated:
            # The platform is already refusing this set - refresh it now
            # instead of waiting out the staleness window.
            out.add(platform)
            gated_platforms.add(platform)
            continue
        raw = (status.get(platform) or {}).get("lastGrabAt")
        if not raw:
            continue
        try:
            age = now - datetime.fromisoformat(str(raw)).timestamp()
        except (TypeError, ValueError):
            continue
        if age >= REFRESH_AFTER_SEC:
            out.add(platform)
    return out, gated_platforms


def import_missing(force: bool = False) -> dict[str, Any]:
    """Fill platforms that have no cookies; refresh stale or gate-refused ones."""
    if not _enabled():
        return {"ok": False, "reason": "cookie bridge disabled", "imported": {}}

    present = _platforms_present()
    # Every platform the app can use. Kick is included on purpose: it is the
    # one that usually has no auth_token (a logged-out browser), and a silent
    # skip there is what made "cookies work" look done when it was not.
    wanted = {"youtube", "twitch", "kick"} - present
    refresh, gated_now = _stale_or_gated(present)
    target = wanted | refresh
    if not target and not force:
        return {"ok": True, "reason": "all platforms already stored", "imported": {}}

    state = _read_state()
    last = float(state.get("last_attempt") or 0)
    # A gate-refused platform is refreshed on the next pass even if the
    # interval has not elapsed: that is the app being told, by the platform
    # itself, that what it holds does not work.
    bypass_interval = bool(refresh & gated_now)
    if not force and not bypass_interval and (time.time() - last) < MIN_INTERVAL_SEC:
        return {"ok": True, "reason": "checked recently", "imported": {}}

    cookies, reason, source = _collect()
    _write_state({"last_attempt": time.time(), "last_source": source, "last_reason": reason})
    if not cookies:
        return {"ok": False, "reason": reason or "no cookies found", "imported": {}}

    flat = [c for platform, group in cookies.items() if platform in target for c in group]
    if not flat:
        return {
            "ok": False,
            "reason": f"browser has no cookies for {sorted(target)}",
            "imported": {},
        }

    from services import cookie_store

    accepted, dropped = cookie_store.upsert_cookies(flat)
    imported = {
        platform: len(group)
        for platform, group in cookies.items()
        if platform in target and group
    }
    if accepted == 0:
        # Say WHY nothing landed. "kick: 0" with no explanation reads as a
        # broken importer; the truth is usually "that browser is logged out of
        # kick", and the keep-list name that is missing is the evidence.
        from services.cookie_store import KEEP_LISTS

        hints: list[str] = []
        for platform, group in cookies.items():
            if platform not in target or not group:
                continue
            have = {str(c.get("name") or "") for c in group}
            need = KEEP_LISTS.get(platform) or frozenset()
            missing = need - have
            if need and len(missing) == len(need):
                hints.append(
                    f"{platform}: logged out in the browser "
                    f"(no {' / '.join(sorted(need))})"
                )
            elif missing:
                hints.append(f"{platform}: missing {' / '.join(sorted(missing))}")
        reason = "; ".join(hints) or "no keep-listed cookies found"
    logger.info(
        "cookie auto-import (%s): accepted=%d dropped=%d platforms=%s refreshed=%s",
        source,
        accepted,
        dropped,
        sorted(imported),
        sorted(refresh & set(imported)),
    )
    return {
        "ok": accepted > 0,
        "source": source,
        "accepted": accepted,
        "dropped": dropped,
        "imported": imported,
        "reason": "" if accepted else reason,
    }


def start_importer(delay_sec: float = 20.0) -> bool:
    """Start the one-shot background pass. Idempotent; False if already running."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True

    def _run() -> None:
        try:
            if delay_sec > 0:
                time.sleep(delay_sec)
            result = import_missing()
            if result.get("ok"):
                logger.info("cookie auto-import: %s", result)
            else:
                logger.debug("cookie auto-import skipped: %s", result)
        except Exception:  # noqa: BLE001 - never take the boot down
            logger.debug("cookie auto-import failed", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="cookie-auto-import").start()
    return True
