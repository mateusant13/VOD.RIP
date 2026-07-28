"""Self-check: verify _host_allowed allows playback.live-video.net (Kick CDN) after fix."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from services.preview_service import _ALLOWED_HOST_SUFFIXES, _host_allowed

class _FakeSession:
    allowed_hosts = set()
    http_headers = {}

session = _FakeSession()

# -- Positive cases: hosts that MUST be allowed (the fix target) --
ALLOW_HOSTS = [
    'playback.live-video.net',
    'fa723fc1b171.us-west-2.playback.live-video.net',
    'seg123.playback.live-video.net',
    'cdn.kick.com',
    'kick.com',
]
for host in ALLOW_HOSTS:
    result = _host_allowed(host, session)
    assert result, f"FAIL: {host} should be allowed but got False"
    print(f"  OK _host_allowed('{host}') = {result}")

# -- Negative cases: must still be denied --
DENY_HOSTS = ['evil.example.com', 'localhost', 'playback.not-live-video.net']
for host in DENY_HOSTS:
    result = _host_allowed(host, session)
    if result:
        # callers raise PermissionError on False — verify the full path too
        raise PermissionError(f"{host} incorrectly allowed")
    assert result is False, f"FAIL: {host} should NOT be allowed but got {result}"
    print(f"  OK _host_allowed('{host}') = False (correctly denied)")

# -- The fix itself --
assert 'playback.live-video.net' in _ALLOWED_HOST_SUFFIXES, \
    "playback.live-video.net must be in the suffix list"

print("\n✅ All host-allowed checks pass: fix verified")
