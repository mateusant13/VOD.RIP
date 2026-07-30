#!/usr/bin/env python3
"""Verify _validate_proxy_url is importable from session.py (no NameError).

Directly exercises the call path that was failing before the fix:
_open_upstream_stream → _validate_proxy_url.
"""

import sys, os, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from unittest.mock import patch
from services.preview.session import PreviewSession

session = PreviewSession(
    session_id=uuid.uuid4().hex,
    vod_url="",
    master_url="",
    entry_url="",
    platform="",
    http_headers={},
)

# Import via session module — this was NameError before the fix
from services.preview.session import _open_upstream_stream

# Also verify the function reference is the same as _state
from services.preview._state import _validate_proxy_url
from services.preview.session import _validate_proxy_url as sess_vpu
assert _validate_proxy_url is sess_vpu, "_validate_proxy_url from session is not from _state!"
print("PASS: _validate_proxy_url is importable from session (no NameError)")

# Exercise the function with an empty URL.
# _validate_proxy_url("") → urlparse("").hostname is None → returns False
# So PermissionError is raised BEFORE any network call.
try:
    _open_upstream_stream(session, "", range_header="bytes=0-1023")
except PermissionError as e:
    print(f"PASS: PermissionError raised (expected for empty URL): {e}")
except NameError as e:
    print(f"FAIL: NameError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"PASS: {type(e).__name__} (not NameError — bug is fixed): {e}")

# Test with a public URL to verify the full _validate_proxy_url → _open_upstream_stream path
with patch('services.preview.session._request_headers', return_value={}):
    try:
        result = _open_upstream_stream(session, "https://example.com/video.mp4", range_header="bytes=0-999")
        print(f"PASS: _open_upstream_stream via public URL returned {type(result).__name__}")
    except NameError as e:
        print(f"FAIL: NameError: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"PASS: {type(e).__name__} (not NameError — bug is fixed): {e}")

print("\nAll checks passed — no NameError!")
