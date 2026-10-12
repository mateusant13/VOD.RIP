"""A key is not a predicate.

Measured 2026-10-12: the preview miss log printed pending_warm=True on every
click, including clicks that had no warm at all, because it read
_youtube_warm_inflight_key(url) as a boolean. That function returns
"extract_video_id(raw) or raw" - a KEY, non-empty for any non-empty URL - so
the answer was always True and the log could not distinguish "a warm is queued"
from "nothing is running". Those two have different fixes, which is the whole
reason the line exists.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.preview.warm import (  # noqa: E402
    _YOUTUBE_WARM_INFLIGHT,
    _YOUTUBE_WARM_LOCK,
    _youtube_warm_inflight_key,
    youtube_warm_is_pending,
)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_the_key_is_not_a_predicate():
    """The red arm: the old expression is True even with nothing running."""
    assert _youtube_warm_inflight_key(URL), "it returns a key"
    assert bool(_youtube_warm_inflight_key(URL)) is True, (
        "this is the trap: the key is truthy whether or not a warm exists"
    )
    assert youtube_warm_is_pending(URL) is False, (
        "and this is the answer the log actually needed"
    )


def test_pending_is_true_only_while_the_event_is_unset():
    key = _youtube_warm_inflight_key(URL)
    ev = threading.Event()
    with _YOUTUBE_WARM_LOCK:
        _YOUTUBE_WARM_INFLIGHT[key] = ev
    try:
        assert youtube_warm_is_pending(URL) is True, "running warm -> pending"
        ev.set()
        assert youtube_warm_is_pending(URL) is False, (
            "a finished warm is not pending, even though its event is still "
            "registered - the registry entry outlives the run"
        )
    finally:
        with _YOUTUBE_WARM_LOCK:
            _YOUTUBE_WARM_INFLIGHT.pop(key, None)
    assert youtube_warm_is_pending(URL) is False, "and not pending once removed"


def test_an_empty_url_is_never_pending():
    assert youtube_warm_is_pending("") is False
    assert youtube_warm_is_pending(None) is False
