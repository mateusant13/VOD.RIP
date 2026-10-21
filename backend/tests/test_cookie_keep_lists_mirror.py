"""The extension's cookie keep-list and the backend's must be the SAME list.

`vendor/cookie-extension/src/modules/cookie_bridge.mjs` carries a comment
saying "MUST mirror backend/services/cookie_store.py KEEP_LISTS", and nothing
enforced it. The two lists had in fact drifted in the direction that matters:
the browser holds the rotation cookies Google added to keep a session alive
(`__Secure-1PSIDTS`, `__Secure-3PSIDTS`, `__Secure-1PSIDCC`,
`__Secure-3PSIDCC`, `SIDCC`, `LOGIN_INFO` - names measured over CDP on
2026-10-21, values never read), and BOTH sides dropped them. An exported jar
without PSIDTS is the documented shape of a session that looks logged out,
which this app answers with a bot gate and a 360p-only ladder.

Two facts are pinned here:
1. the lists are identical (a name added on one side only is silently
   half-applied: the extension sends it and the backend drops it, or the
   backend would accept something the extension never sends);
2. the rotation cookies are actually in the youtube list, so removing them
   takes a deliberate edit and a failing test, not a quiet regression.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.cookie_store import KEEP_LISTS  # noqa: E402

_EXT_MODULE = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "vendor", "cookie-extension", "src", "modules", "cookie_bridge.mjs",
)

# Cookies YouTube rotates to keep a signed-in session alive.
ROTATION_COOKIES = frozenset({
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
    "SIDCC",
    "LOGIN_INFO",
})


def _ext_keep_lists() -> dict[str, set[str]]:
    with open(_EXT_MODULE, encoding="utf-8") as fh:
        source = fh.read()
    out: dict[str, set[str]] = {}
    for platform in ("kick", "youtube", "twitch"):
        match = re.search(
            rf"{platform}:\s*new Set\(\[(.*?)\]\)", source, re.S
        )
        assert match, f"{platform}: new Set([...]) not found in {_EXT_MODULE}"
        # Strip // comments first: a commented-out name must not count.
        body = re.sub(r"//[^\n]*", "", match.group(1))
        out[platform] = set(re.findall(r"'([^']+)'", body))
    return out


def test_extension_and_backend_keep_lists_are_identical():
    ext = _ext_keep_lists()
    for platform in ("kick", "youtube", "twitch"):
        backend = set(KEEP_LISTS[platform])
        assert ext[platform] == backend, (
            f"{platform} keep-lists drifted.\n"
            f"  only in extension: {sorted(ext[platform] - backend)}\n"
            f"  only in backend:   {sorted(backend - ext[platform])}"
        )


def test_youtube_rotation_cookies_are_kept():
    backend = set(KEEP_LISTS["youtube"])
    missing = ROTATION_COOKIES - backend
    assert not missing, (
        f"youtube keep-list dropped rotation cookies {sorted(missing)}; "
        "an exported session without them is what makes a logged-in profile "
        "look logged out and trips the bot gate"
    )


def test_keep_lists_do_not_grow_into_a_full_dump():
    """The lists are a deliberate whitelist, not 'send everything'.

    A browser carries dozens of youtube.com cookies (26 measured 2026-10-21);
    exporting all of them would move tracking and preference cookies for no
    auth benefit. A generous ceiling keeps that intent visible.
    """
    for platform, names in KEEP_LISTS.items():
        assert len(names) <= 20, f"{platform} keep-list looks like a full dump: {len(names)}"
        for name in names:
            assert name.strip() == name and name, f"{platform}: blank/spacey cookie name"
