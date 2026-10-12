"""A seek reuses the resolve; only real expiry forces a fresh one.

Measured 2026-10-12: _refresh_youtube_window_hls_urls invalidated the resolve
cache and called resolve_stream_info(force_fresh=True) on EVERY seek, so a
mid-preview seek paid a measured 2.2-2.8 s re-resolve for a format ladder it
already had. A seek does not change the ladder - the googlevideo URLs for a
VOD are the same whichever window we mux - and _RESOLVED_STREAM_CACHE already
treats a resolve as good for an hour everywhere else.

Expiry is NOT disarmed: the path that exists for it (_refresh_youtube_preview
_urls, called on the 403/500 that stale URLs produce) and the mux-failure
fallback both still force a fresh resolve. Both arms are asserted here, so a
later edit cannot quietly trade one for the other.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.preview import session as session_mod  # noqa: E402


def _body(name: str, *, with_docstring: bool = False) -> str:
    src = Path(session_mod.__file__).read_text(encoding="utf-8", errors="replace")
    start = src.index(f"def {name}(")
    nxt = src.find("\ndef ", start + 1)
    body = src[start:nxt if nxt != -1 else len(src)]
    if with_docstring:
        return body
    # The docstring legitimately NAMES force_fresh=True while explaining where
    # it belongs, so it must not count as a call. A law that cannot tell a
    # comment from a call fails on correct code.
    q = body.find('"""')
    if q != -1:
        end = body.find('"""', q + 3)
        if end != -1:
            body = body[:q] + body[end + 3 :]
    return body


def test_a_seek_reuses_the_resolve():
    """RED before this change: force_fresh=True was unconditional here."""
    body = _body("_refresh_youtube_window_hls_urls")
    assert "force_fresh=True" not in body, (
        "a seek forces a fresh resolve again - every mid-preview click pays for it"
    )
    assert "_invalidate_youtube_resolve_caches" not in body, (
        "a seek invalidates the resolve cache again, which guarantees the miss"
    )
    assert "force_fresh=False" in body


def test_the_expiry_path_still_forces_a_fresh_resolve():
    """The control: expiry is handled, just not on every seek."""
    body = _body("_refresh_youtube_preview_urls")
    assert "force_fresh=True" in body, (
        "the 403/500 expiry path no longer re-resolves - stale URLs would persist"
    )


def test_the_mux_failure_fallback_still_forces_a_fresh_resolve():
    """The safety net for genuinely dead URLs must survive."""
    body = _body("_try_fallback_from_window_hls")
    assert "force_fresh=True" in body, (
        "the mux-failure fallback no longer re-resolves - dead URLs would stick"
    )
