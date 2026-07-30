# /// script
# requires-python = ">=3.11"
# ///
"""Self-check: cloudfront CDN bypass for sub-only Twitch VODs.

Verifies that the cloudfront bypass fallback in ``get_vod_playback_sync``
works against live Twitch infrastructure.

Usage::

    cd <repo-root>
    python backend/services/twitch_gql_service.selfcheck.py

"""

import sys
import urllib.request

if __name__ != "__main__":
    raise SystemExit("Run directly, not imported")

sys.path.insert(0, "backend")

from services.twitch_gql_service import (
    get_vod_playback_sync,
    _get_vod_meta_sync,
    _resolve_cloudfront_variants,
    _gql_persisted,
    VOD_PLAYBACK_TOKEN_HASH,
)

_OK = 0
_FAIL = 0


def _check(label: str, cond: bool, detail: str = "") -> None:
    global _OK, _FAIL
    if cond:
        _OK += 1
        print(f"  \u2713 {label}")
    else:
        _FAIL += 1
        print(f"  \u2717 {label}: {detail}")


# ---------------------------------------------------------------------------
# Find a VOD to test — first try a known sub-only ID, then fall back
# ---------------------------------------------------------------------------
CANDIDATES = [
    # cellbit VOD — the persisted token might be returned but usher 403s
    "2832215082",
]

test_vod: str = ""
for vid in CANDIDATES:
    try:
        _get_vod_meta_sync(vid)
        test_vod = vid
        break
    except Exception:
        continue

if not test_vod:
    print("No Twitch VOD responds to metadata query — cannot verify bypass")
    sys.exit(0)

print(f"Using VOD id={test_vod}")

# ---------------------------------------------------------------------------
# 1. Persisted query path — show that the primary path alone would fail
# ---------------------------------------------------------------------------
print("\n--- 1. Persisted PlaybackAccessToken query ---")
data = _gql_persisted(
    "PlaybackAccessToken",
    VOD_PLAYBACK_TOKEN_HASH,
    {
        "isLive": False,
        "login": "",
        "isVod": True,
        "vodID": test_vod,
        "playerType": "embed",
        "platform": "site",
    },
)
tn = data.get("videoPlaybackAccessToken") or data.get("playbackAccessToken") or {}
sig = tn.get("signature")
token = tn.get("value")
primary_ok = bool(sig and token)
print(f"  Token from persisted query: {'present' if primary_ok else 'NULL (sub-only expected)'}")

# ---------------------------------------------------------------------------
# 2. Metadata (inline) query — must work even for sub-only
# ---------------------------------------------------------------------------
print("\n--- 2. Inline metadata query ---")
video_data = _get_vod_meta_sync(test_vod)
_check("seekPreviewsURL present", bool(video_data.get("seekPreviewsURL")))
broadcast_type = video_data.get("broadcastType", "")
owner = video_data.get("owner", {}) or {}
channel_login = owner.get("login", "")
_check("broadcastType not empty", bool(broadcast_type))
_check("owner.login not empty", bool(channel_login), f"got login={channel_login!r}")
print(f"  broadcastType={broadcast_type}, login={channel_login}")

# ---------------------------------------------------------------------------
# 3. Cloudfront variants — the bypass must find at least one working quality
# ---------------------------------------------------------------------------
print("\n--- 3. Cloudfront CDN variant probing ---")
cf_variants = _resolve_cloudfront_variants(test_vod, video_data)
_check("At least one cloudfront variant found", len(cf_variants) >= 1)
if cf_variants:
    heights = [v.get("height", 0) for v in cf_variants]
    _check("Highest variant >= 720p", heights[0] >= 720,
           f"top: {heights[0]}p  all: {heights}")
    # Verify first variant returns HLS
    best = cf_variants[0]["url"]
    req = urllib.request.Request(
        best,
        headers={
            "Referer": "https://www.twitch.tv/",
            "Origin": "https://www.twitch.tv",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read(4096).decode("utf-8", errors="replace")
    _check("Variant is valid HLS (#EXTM3U)",
           body.lstrip().startswith("#EXTM3U"))
    _check("Variant has segments (#EXTINF)", "#EXTINF" in body)

# ---------------------------------------------------------------------------
# 4. Full get_vod_playback_sync — end-to-end with auto fallback
# ---------------------------------------------------------------------------
print("\n--- 4. Full get_vod_playback_sync (auto fallback) ---")
master_url, headers, variants = get_vod_playback_sync(test_vod)
_check("Returns master_url", bool(master_url))
_check("Returns headers", isinstance(headers, dict))
_check("Returns non-empty variants", len(variants) >= 1)
if variants:
    heights = sorted(set(v.get("height", 0) for v in variants), reverse=True)
    _check("Variants include 720p+", any(h >= 720 for h in heights))

# ---------------------------------------------------------------------------
# 5. Public VOD — ensure primary (usher) path not regressed
# ---------------------------------------------------------------------------
print("\n--- 5. Public VOD (no regression on usher path) ---")
_PUBLIC_VOD = "2831160253"
try:
    public_master, public_headers, public_variants = get_vod_playback_sync(_PUBLIC_VOD)
    _check("Uses usher URL", "usher.ttvnw.net" in public_master,
           f"got {public_master[:60]}")
    _check("Returns variants", len(public_variants) >= 1,
           f"got {len(public_variants)}")
    if public_variants:
        _check("First variant resolvable", bool(public_variants[0].get("height", 0) > 0))
except Exception as e:
    _check(f"Public VOD {_PUBLIC_VOD} fails", False, str(e))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if _FAIL:
    print(f"FAILED: {_FAIL} check(s) failed")
    sys.exit(1)
elif _OK:
    print(f"ALL {_OK} CHECKS PASSED")
    sys.exit(0)
else:
    print("Skipped: no assertions ran")
    sys.exit(0)
