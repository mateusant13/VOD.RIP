"""Saved-word / entity watching routes.

The watcher daemon scans transcriptions in the background; these routes give
the UI CRUD over entities, the hit log, acking, and a manual scan trigger.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from services import archive_db
from services.entity_watch import run_scan_once, sync_auto_entities

logger = logging.getLogger(__name__)
router = APIRouter(tags=["entities"])


@router.get("/api/entities")
def list_entities() -> dict:
    """Entities with hit counts, plus the watcher watermark and a manual
    sync/scan trigger for the UI."""
    try:
        synced = sync_auto_entities()
    except Exception:
        synced = 0
    return {
        "entities": archive_db.list_watched_entities(),
        "cursor": archive_db.entity_watch_cursor(),
        "auto_channels": synced,
    }


@router.post("/api/entities")
def add_entity(payload: dict = Body(...)) -> dict:
    """Add a manual entity: {text, aliases?: [..]}. Text must be non-empty."""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="entity text must not be empty")
    aliases = payload.get("aliases") or []
    if not isinstance(aliases, list):
        raise HTTPException(status_code=422, detail="aliases must be a list")
    eid = archive_db.upsert_watched_entity(
        text, kind="manual", aliases=[str(a) for a in aliases]
    )
    return {"id": eid, "entity": archive_db.get_watched_entity(eid)}


@router.put("/api/entities/{entity_id}")
def update_entity(entity_id: int, payload: dict = Body(...)) -> dict:
    ent = archive_db.get_watched_entity(entity_id)
    if ent is None:
        raise HTTPException(status_code=404, detail="entity not found")
    aliases = payload.get("aliases")
    enabled = payload.get("enabled")
    if aliases is not None and not isinstance(aliases, list):
        raise HTTPException(status_code=422, detail="aliases must be a list")
    archive_db.set_watched_entity(
        entity_id,
        aliases=[str(a) for a in aliases] if aliases is not None else None,
        enabled=bool(enabled) if enabled is not None else None,
    )
    return {"id": entity_id, "entity": archive_db.get_watched_entity(entity_id)}


@router.delete("/api/entities/{entity_id}")
def delete_entity(entity_id: int) -> dict:
    if archive_db.get_watched_entity(entity_id) is None:
        raise HTTPException(status_code=404, detail="entity not found")
    archive_db.delete_watched_entity(entity_id)
    return {"deleted": entity_id}


@router.get("/api/entities/hits")
def list_hits(
    entity_id: Optional[int] = Query(None),
    platform: Optional[str] = Query(None),
    video_id: Optional[str] = Query(None),
    unacked_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return {
        "hits": archive_db.list_entity_hits(
            entity_id=entity_id, platform=platform, video_id=video_id,
            acked_only=False if unacked_only else None,
            limit=limit,
        )
    }


@router.post("/api/entities/hits/{hit_id}/ack")
def ack_hit(hit_id: int) -> dict:
    archive_db.ack_entity_hit(hit_id)
    return {"acked": hit_id}


@router.post("/api/entities/scan")
def scan() -> dict:
    """Run one incremental scan pass synchronously (manual trigger)."""
    return run_scan_once()
