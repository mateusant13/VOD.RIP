"""Self-check: YouTube bot-gate 503 response shape — no mocks, real URL."""
from __future__ import annotations

from fastapi.testclient import TestClient
from app import app

# titiltei YouTube video from the startup warm list
YT_URL = "https://www.youtube.com/watch?v=1tap3CLaqr8"
EXPECTED_HINT = "YouTube preview is temporarily restricted. Try again in a few minutes."


def test_youtube_preview_botgate_message():
    """Post a real YouTube URL; if it 503s (bot-gate), verify the message is
    the new user-facing string, not a generic 503 or a 500 crash."""
    with TestClient(app) as c:
        resp = c.post("/api/preview/session", json={"url": YT_URL})
        # 200 = no bot-gate (test still passes), 503 = bot-gate that should
        # have the right message; anything else is a regression.
        if resp.status_code == 503:
            detail = resp.json().get("detail", "")
            assert (
                EXPECTED_HINT in detail
            ), f"Expected bot-gate hint in 503 detail, got: {detail}"
        elif resp.status_code != 200:
            # Unexpected error (e.g. 500) — fail
            body = resp.text[:256]
            assert False, (
                f"Unexpected status {resp.status_code} for YT URL {YT_URL}: {body}"
            )
