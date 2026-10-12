"""A stale playability record must never brand a healthy video as dead.

Measured 2026-10-12, cold preview click on jNQXAC9IVRw (on YouTube since 2005):
POST /api/preview/session -> 404 in 27,499 ms with
{"detail": "This video is unavailable."}, while the backend log's last word was
`extract fail video=jNQXAC9IVRw reason=preview_race_exhausted`.

The chain was not reporting a dead video. It consulted
innertube_last_playability, which has NO age limit and NO kind validation, so a
record written by ONE transient bot-gated probe -- "a bot-gated WEB probe
reports 'Video unavailable' while ANDROID plays the same video"
(youtube_innertube.py:668-670) -- branded the video for the life of the process,
because _record_playability never downgrades a stored fatal.

Its sibling innertube_playability_verdict exists for exactly this and says so:
"A caller that turns this into a user-facing status code must not be handed a
guess." The preview ladder and the fatal-cache writer now use the safe reader.

The wrong word here is not cosmetic: the client treats "unavailable" as
permanent (_PREVIEW_CREATE_FATAL_RE, src/previewPlayerUtils.ts:38), so it turns
a retryable timeout into a dead end the user cannot escape by clicking again.
"""
import ast
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import services.youtube_innertube as yt
from services.ytdlp_hls import _youtube_extract_preview_with_retries

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VID = "dQw4w9WgXcQ"
# ytdlp_hls.py's generic transient message -- the honest one for a timed-out
# extract (maps to 503 + "try again", which the client DOES retry).
TRANSIENT = "YouTube preview unavailable for this video"
# The ambiguous string the classifier warns about: a soft bot gate answers with
# it too, so it must never be reached through an unvalidated record.
AMBIGUOUS = "This video is unavailable"


@contextmanager
def _offline_ladder():
    """Drive the preview ladder with no network and no slow fallback."""
    with patch("services.ytdlp_hls._youtube_extract_pass", return_value=None), \
         patch("services.ytdlp_hls.preview_fast_only_mode", return_value=True), \
         patch("services.ytdlp_hls._youtube_manual_auth_configured", return_value=False), \
         patch("services.ytdlp_hls._youtube_has_user_auth", return_value=True):
        yield


@contextmanager
def _seeded(vid, kind, status, reason, *, age_s=0.0):
    """Write a playability record aged `age_s` seconds into the real store."""
    prev = yt._LAST_PLAYABILITY.get(vid), yt._PLAYABILITY_AT.get(vid)
    yt._LAST_PLAYABILITY[vid] = (status, reason, kind)
    yt._PLAYABILITY_AT[vid] = time.monotonic() - age_s
    try:
        yield
    finally:
        if prev[0] is None:
            yt._LAST_PLAYABILITY.pop(vid, None)
        else:
            yt._LAST_PLAYABILITY[vid] = prev[0]
        if prev[1] is None:
            yt._PLAYABILITY_AT.pop(vid, None)
        else:
            yt._PLAYABILITY_AT[vid] = prev[1]


def _ladder_message():
    with pytest.raises(RuntimeError) as ei:
        _youtube_extract_preview_with_retries(YOUTUBE_URL, {})
    return str(ei.value)


def test_a_stale_fatal_record_does_not_abort_the_ladder():
    """THE regression. A one-hour-old fatal record must not become a 404.

    Red arm: with the call site on innertube_last_playability this returns
    "This video is unavailable" (404, permanent to the client) instead of the
    transient message, and the user's retry never happens.
    """
    with _seeded(VID, "fatal", "ERROR", AMBIGUOUS, age_s=3600.0), _offline_ladder():
        assert _ladder_message() == TRANSIENT


def test_a_fresh_fatal_record_still_aborts_with_the_real_reason():
    """The fix must not over-correct: a FRESH definitive verdict is still a
    dead video and still short-circuits with YouTube's own reason."""
    with _seeded(VID, "fatal", "ERROR", "Video removed by the uploader", age_s=1.0), \
         _offline_ladder():
        assert _ladder_message() == "Video removed by the uploader"


def test_a_fresh_soft_record_falls_through_to_the_ladder():
    """A bot/consent gate is never definitive, fresh or stale."""
    with _seeded(VID, "retry", "LOGIN_REQUIRED", "Video unavailable", age_s=1.0), \
         _offline_ladder():
        assert _ladder_message() == TRANSIENT


def test_the_two_readers_disagree_about_a_stale_record():
    """This disagreement IS the defect, pinned so it cannot be forgotten:
    the unsafe reader answers forever; the safe one refuses."""
    with _seeded(VID, "fatal", "ERROR", AMBIGUOUS, age_s=3600.0):
        assert yt.innertube_last_playability(VID) == ("ERROR", AMBIGUOUS, "fatal")
        assert yt.innertube_playability_verdict(VID) is None


def test_no_production_module_reads_the_unsafe_reader():
    """A source law, because the defect is a CALL SITE, not a function.

    innertube_last_playability stays exported for its own self-checks, but no
    production module may consult it: it is the reader that turns a guess into
    a user-facing verdict. Reintroducing one call site fails here.
    """
    backend = Path(__file__).resolve().parents[1]
    offenders = []
    for pattern in ("*.py", "services/*.py", "services/preview/*.py", "routers/*.py"):
        for path in sorted(backend.glob(pattern)):
            if path.name == "youtube_innertube.py" or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "innertube_last_playability":
                    offenders.append(f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Attribute) and node.attr == "innertube_last_playability":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "production code must use innertube_playability_verdict (fresh, kind-validated); "
        f"the unsafe reader has no age limit: {offenders}"
    )
