"""Detect a running VOD.RIP instance and ask it to take focus."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

APP_NAME_PREFIX = "VOD.RIP"


def is_vodrip_api_name(name: str) -> bool:
    return (name or "").startswith(APP_NAME_PREFIX)


def _api_base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_vodrip_running(port: int) -> bool:
    try:
        import requests

        resp = requests.get(f"{_api_base(port)}/api/info", timeout=1.5)
        if resp.status_code != 200:
            return False
        return is_vodrip_api_name(resp.json().get("name", ""))
    except Exception:
        return False


def try_activate_existing(port: int) -> bool:
    """If VOD.RIP is already running, focus its window and return True."""
    if not is_vodrip_running(port):
        return False
    try:
        import requests

        resp = requests.post(f"{_api_base(port)}/api/focus", timeout=2.5)
        if resp.status_code == 200:
            logger.info("Focused existing VOD.RIP instance on port %s", port)
            return True
    except Exception as exc:
        logger.debug("Focus existing instance failed: %s", exc)
    return False
