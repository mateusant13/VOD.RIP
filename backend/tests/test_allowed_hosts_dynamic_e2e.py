"""Self-check: verify two-tier trust model for allowed hosts.

Tier 1: session-scoped whitelist (hosts discovered in playlists).
Tier 2: fallback suffix allowlist (cold-fetch / no-session use).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from services.preview_service import is_host_allowed, _ALLOWED_HOST_SUFFIXES
from dataclasses import dataclass, field
from typing import Set


@dataclass
class _FakeSession:
    allowed_hosts: Set[str] = field(default_factory=set)


# -- Tier 1: session-scoped whitelist --
session = _FakeSession()

# Simulate "after a playlist-line rewrite discovered seg123.cdn.kick.com"
session.allowed_hosts.add("seg123.cdn.kick.com")

# Must be allowed (explicitly in session.allowed_hosts)
assert is_host_allowed("seg123.cdn.kick.com", session), \
    "seg123.cdn.kick.com should be allowed via Tier 1 (session whitelist)"
print("  OK is_host_allowed('seg123.cdn.kick.com', session) = True (Tier 1)")

# Must be denied (not in session.allowed_hosts, not in suffix list)
assert not is_host_allowed("evil.com", session), \
    "evil.com should be denied (not in Tier 1 or Tier 2)"
print("  OK is_host_allowed('evil.com', session) = False (correctly denied)")

# -- Tier 2: fallback suffix allowlist (no session / cold fetch) --
assert is_host_allowed("cdn.kick.com", None), \
    "cdn.kick.com should be allowed via Tier 2 (suffix fallback with None session)"
print("  OK is_host_allowed('cdn.kick.com', None) = True (Tier 2 fallback, None session)")

assert is_host_allowed("kick.com", None), \
    "kick.com should be allowed via Tier 2 (exact match in suffix list)"
print("  OK is_host_allowed('kick.com', None) = True (Tier 2 fallback, exact suffix)")

assert not is_host_allowed("evil.com", None), \
    "evil.com should be denied with None session"
print("  OK is_host_allowed('evil.com', None) = False (denied, no session)")

# -- Empty session.allowed_hosts falls through to Tier 2 --
empty = _FakeSession()
assert is_host_allowed("cdn.kick.com", empty), \
    "cdn.kick.com should fall through to Tier 2 when session.allowed_hosts is empty"
print("  OK is_host_allowed('cdn.kick.com', empty_session) = True (Tier 2 fallback)")

# -- Edge: empty host --
assert not is_host_allowed("", session), "empty host should always be denied"
assert not is_host_allowed("", None), "empty host should always be denied (no session)"
print("  OK is_host_allowed('', ...) = False (empty host denied)")

print(f"\n✅ All allowed-hosts dynamic checks pass (suffix count: {len(_ALLOWED_HOST_SUFFIXES)}")
