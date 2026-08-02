"""Cookie bridge routes — pairing + ingest from the local browser extension.

POST /api/session/cookies  {token, cookies:[...]}  — pairing happens on the
    first successful POST (any token becomes the paired token); later calls
    must present the same token or get 403.
GET  /api/session/cookies/pull?platform=  — Netscape cookies.txt (text/plain)
    with only the keep-listed cookie names for that platform.
GET  /api/session/cookies/status — {paired, platforms:{platform:count}}
GET  /api/session/cookies/token  — the paired token (Settings diagnostics).
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from deps import settings_mgr
from services import cookie_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cookie-bridge"])


def _require_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in cookie_store.PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"platform must be one of {cookie_store.PLATFORMS}",
        )
    return p


def _paired_token() -> str:
    return (settings_mgr.get().cookie_bridge_token or "").strip()


@router.post("/api/session/cookies")
async def session_cookies_ingest(body: dict):
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="token required")
    paired = _paired_token()
    if not paired:
        # Pairing = first successful POST: any token becomes the paired one.
        s = settings_mgr.get()
        s.cookie_bridge_token = token
        settings_mgr.save(s)
        logger.info("cookie bridge paired (token set)")
    elif token != paired:
        raise HTTPException(status_code=403, detail="invalid cookie bridge token")
    cookies = body.get("cookies")
    if not isinstance(cookies, list):
        raise HTTPException(status_code=422, detail="cookies must be a list")
    accepted, dropped = cookie_store.upsert_cookies(cookies)
    return {"ok": True, "accepted": accepted, "dropped": dropped}


@router.get("/api/session/cookies/pull")
async def session_cookies_pull(platform: str):
    p = _require_platform(platform)
    return PlainTextResponse(
        cookie_store.pull_netscape(p),
        media_type="text/plain",
    )


@router.get("/api/session/cookies/status")
async def session_cookies_status():
    return {
        "paired": bool(_paired_token()),
        "platforms": cookie_store.counts(),
    }


@router.get("/api/session/cookies/token")
async def session_cookies_token():
    return {"token": _paired_token()}
