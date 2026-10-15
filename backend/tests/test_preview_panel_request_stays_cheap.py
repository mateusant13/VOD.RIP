"""A panel poll must be cheap, because SIX of them run at once on a live tab.

Measured 2026-10-15 on the shipped v1.0.60 with six live previews open:

  playable = null (45 s timeout)
    +-10870ms init  + 42351ms dur  /api/preview/panel/twitch/...?limit=20000
    +      0ms init  + 33521ms dur  session
    +   2824ms init  + 32251ms dur  /api/preview/panel/...?limit=20000
    +   2829ms init  + 32741ms dur  /api/preview/panel/...?limit=20000
    +   2830ms init  + 34253ms dur  /api/preview/panel/...?limit=20000
    +   5341ms init  + 31756ms dur  /api/preview/panel/...?limit=20000
    +   7841ms init  + 29277ms dur  /api/preview/panel/...?limit=20000

Six requests of 20 000 rows each, 29-42 s apiece, all in flight together. They
held every browser connection slot, so the session request and the media
segments queued behind them and no preview ever reached playable.

The waste was absurd on its own terms: the live chat window keeps 300 rows, so
19 700 of every 20 000 were parsed, serialised, shipped and discarded - on every
poll, per preview. The client default and the backend default both have to stay
small for this to hold; either one regressing re-creates the stall.

RED ARM: set either constant back to 20_000 and the matching test fails.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLIENT = REPO / "src" / "components" / "PreviewChatPanel.tsx"
ROUTER = REPO / "backend" / "routers" / "preview.py"

# The live chat window keeps 300 rows (LiveChatPanel MAX_ROWS). A poll that asks
# for more than a few times that is paying for rows nothing can display.
MAX_SENSIBLE_POLL_ROWS = 5_000


def _client_limit() -> int:
    src = CLIENT.read_text(encoding="utf-8")
    code = "\n".join(
        l for l in src.splitlines() if not l.strip().startswith(("*", "//", "/*"))
    )
    m = re.search(r"const PANEL_LIMIT = ([\d_]+);", code)
    assert m, "PANEL_LIMIT not found in the client"
    return int(m.group(1).replace("_", ""))


def _backend_default() -> int:
    src = ROUTER.read_text(encoding="utf-8")
    code = "\n".join(
        l for l in src.splitlines() if not l.strip().startswith("#")
    )
    m = re.search(r"_PANEL_LIMIT_DEFAULT = ([\d_]+)", code)
    assert m, "_PANEL_LIMIT_DEFAULT not found in the router"
    return int(m.group(1).replace("_", ""))


def test_the_client_does_not_ask_for_more_rows_than_a_screen_could_show():
    """RED: restore 20_000 and this fails."""
    got = _client_limit()
    assert got <= MAX_SENSIBLE_POLL_ROWS, (
        f"the panel asks for {got} rows per timeline, per poll, per preview;"
        " six concurrent previews of that held every connection slot and none"
        " ever reached playable"
    )


def test_the_backend_default_is_cheap_too():
    """RED: restore the 20_000 default and this fails."""
    got = _backend_default()
    assert got <= MAX_SENSIBLE_POLL_ROWS, (
        f"a caller that names no limit pulls {got} rows per timeline"
    )


def test_the_backend_ceiling_still_allows_an_explicit_deep_read():
    """The ceiling is a separate decision from the default, and stays high."""
    code = "\n".join(
        l for l in ROUTER.read_text(encoding="utf-8").splitlines()
        if not l.strip().startswith("#")
    )
    m = re.search(r"_PANEL_LIMIT_MAX = ([\d_]+)", code)
    assert m
    assert int(m.group(1).replace("_", "")) >= 20_000, (
        "lowering the ceiling would break a caller that explicitly asks for a"
        " deep archive read; only the DEFAULT should be cheap"
    )