"""Keep the cookie store fed from the user's own browser, without a click.

The import path (:func:`services.browser_cookie_cdp.fetch_platform_cookies`
then the on-disk reader) already works, but it needed a button. A preview that
only gets an authenticated ladder after the user finds that button is a preview
that is 360p for everyone who never finds it - and a preview with no cookies
comes back as a single 360 rung, which leaves the engaged 1080 tier with
nothing to mux.

Policy, deliberately conservative:

  * GAP-FILL ONLY. A platform with zero stored cookies is filled. A platform
    that already has cookies is left ALONE - the extension is the refresh path
    (it pushes on cookies.onChanged), and letting a periodically-imported
    browser value overwrite a live one would be a downgrade dressed as a
    refresh.
  * The bridge kill switch wins. ``cookie_bridge_enabled`` false means no
    ingestion at all, which is what the Settings panel promises.
  * Background only. It runs on a daemon thread after the startup warm wave,
    so a browser that is slow to answer never delays a boot or a preview.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A missing platform is the only thing that triggers a pass; this floor just
# stops a restart loop on a box with nothing to import from hammering the
# DevTools ports every boot.
MIN_INTERVAL_SEC = 3600.0

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


def import_missing(force: bool = False) -> dict[str, Any]:
    """Fill platforms that have no cookies at all. Never overwrites."""
    if not _enabled():
        return {"ok": False, "reason": "cookie bridge disabled", "imported": {}}

    present = _platforms_present()
    # Every platform the app can use. Kick is included on purpose: it is the
    # one that usually has no auth_token (a logged-out browser), and a silent
    # skip there is what made "cookies work" look done when it was not.
    wanted = {"youtube", "twitch", "kick"} - present
    if not wanted and not force:
        return {"ok": True, "reason": "all platforms already stored", "imported": {}}

    state = _read_state()
    last = float(state.get("last_attempt") or 0)
    if not force and (time.time() - last) < MIN_INTERVAL_SEC:
        return {"ok": True, "reason": "checked recently", "imported": {}}

    cookies, reason, source = _collect()
    _write_state({"last_attempt": time.time(), "last_source": source, "last_reason": reason})
    if not cookies:
        return {"ok": False, "reason": reason or "no cookies found", "imported": {}}

    flat = [c for platform, group in cookies.items() if platform in wanted for c in group]
    if not flat:
        return {
            "ok": False,
            "reason": f"browser has no cookies for {sorted(wanted)}",
            "imported": {},
        }

    from services import cookie_store

    accepted, dropped = cookie_store.upsert_cookies(flat)
    imported = {
        platform: len(group)
        for platform, group in cookies.items()
        if platform in wanted and group
    }
    if accepted == 0:
        # Say WHY nothing landed. "kick: 0" with no explanation reads as a
        # broken importer; the truth is usually "that browser is logged out of
        # kick", and the keep-list name that is missing is the evidence.
        from services.cookie_store import KEEP_LISTS

        hints: list[str] = []
        for platform, group in cookies.items():
            if platform not in wanted or not group:
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
        "cookie auto-import (%s): accepted=%d dropped=%d platforms=%s",
        source,
        accepted,
        dropped,
        sorted(imported),
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
