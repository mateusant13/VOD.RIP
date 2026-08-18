"""Feature registry — single source of truth for opt-in features.

Mirrored in src/lib/featureManifest.ts (keep in sync). The JSON at
backend/services/feature_manifest.json is NOT needed — the two files
are the canonical source; a future codegen could emit one from the other.
"""
from __future__ import annotations

from typing import Dict, List

# Cost taxonomy: heavy = GPU/model/thread-pool heavy; light = cheap network/UI.
MANIFEST: List[Dict] = [
    {
        "id": "core-download",
        "cost": "light",
        "defaultEnabled": True,
        "description": "Core VOD / clip downloads (yt-dlp + ffmpeg)",
    },
    {
        "id": "transcribe-vod",
        "cost": "heavy",
        "defaultEnabled": False,
        "description": "VOD transcription (parakeet ASR, GPU/VRAM)",
    },
    {
        "id": "live-captions",
        "cost": "heavy",
        "defaultEnabled": False,
        "description": "Live captions — real-time ASR for live streams",
    },
    {
        "id": "live-preview",
        "cost": "heavy",
        "defaultEnabled": False,
        "description": "Live preview sessions & channel live-status warm",
    },
    {
        "id": "clipping",
        "cost": "light",
        "defaultEnabled": True,
        "description": "Clipping & trim tools (timeline, clip editor)",
    },
    {
        "id": "chat-live",
        "cost": "light",
        "defaultEnabled": True,
        "description": "Live chat capture & overlay for previews",
    },
]

# Fast lookup of defaults
_DEFAULTS: Dict[str, bool] = {f["id"]: bool(f["defaultEnabled"]) for f in MANIFEST}
_HEAVY_IDS = {f["id"] for f in MANIFEST if f["cost"] == "heavy"}
_ALL_IDS = set(_DEFAULTS)

# ponytail: future optimization could lazy-load manifest from a shared JSON;
# upgrade path: generate src/lib/featureManifest.ts from this file at build.


def _get_stored_features() -> Dict[str, bool]:
    try:
        from deps import settings_mgr  # lazy to avoid import cycle at module load

        s = settings_mgr.get()
        stored = getattr(s, "features", None)
        if isinstance(stored, dict):
            return {k: bool(v) for k, v in stored.items() if k in _ALL_IDS}
    except Exception:
        pass
    return {}


def get_manifest() -> List[Dict]:
    return [dict(f) for f in MANIFEST]


def get_defaults() -> Dict[str, bool]:
    return dict(_DEFAULTS)


def get_enabled_map() -> Dict[str, bool]:
    """Merged view: stored overrides + defaults for missing keys."""
    stored = _get_stored_features()
    out: Dict[str, bool] = {}
    for fid, d in _DEFAULTS.items():
        out[fid] = stored.get(fid, d) if fid in stored or fid not in stored else d
        # above expression simplifies to stored.get(fid, d)
        out[fid] = stored.get(fid, d)
    return out


def is_enabled(feature_id: str) -> bool:
    if feature_id not in _ALL_IDS:
        return False
    return get_enabled_map().get(feature_id, _DEFAULTS.get(feature_id, False))


def is_heavy(feature_id: str) -> bool:
    return feature_id in _HEAVY_IDS


def all_heavy_disabled() -> bool:
    m = get_enabled_map()
    return all(not m[fid] for fid in _HEAVY_IDS)


def set_feature(feature_id: str, enabled: bool) -> Dict[str, bool]:
    if feature_id not in _ALL_IDS:
        raise ValueError(f"unknown feature {feature_id}")
    from deps import settings_mgr

    s = settings_mgr.get()
    current = dict(getattr(s, "features", None) or {})
    current[feature_id] = bool(enabled)
    # persist via settings manager (atomic JSON write)
    updated = s.model_copy(update={"features": current})
    settings_mgr.save(updated)
    return get_enabled_map()


def set_features_bulk(updates: Dict[str, bool]) -> Dict[str, bool]:
    for fid in updates:
        if fid not in _ALL_IDS:
            raise ValueError(f"unknown feature {fid}")
    from deps import settings_mgr

    s = settings_mgr.get()
    current = dict(getattr(s, "features", None) or {})
    for fid, val in updates.items():
        current[fid] = bool(val)
    updated = s.model_copy(update={"features": current})
    settings_mgr.save(updated)
    return get_enabled_map()
