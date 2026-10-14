"""Structured playability -> HTTP status and message, for localized failures.

Measured 2026-10-11: YouTube answered a dead video with its pt-BR reason
"Este vídeo não está disponível". No English substring table in
services/youtube_diag matched it, so the preview route answered HTTP 500 with
"Preview failed — try again." for a video that can never play. The innertube
probe that ran inside the same request had recorded (status, reason, kind) for
that id, and "kind" is language-independent, so youtube_http_status and
youtube_user_message consult that record when they are handed the url.

These tests pin the contract around that record:
  fatal  -> 404 + "This video is unavailable."
  retry  -> 503 + the transient-gate text (never a hard 404)
  absent / stale / malformed / unknown kind -> the old English-table behaviour
  one lookup feeds both outputs, so the pair cannot disagree

  python tests/test_youtube_playability_structured.py
  pytest tests/test_youtube_playability_structured.py
"""

import time

from services import youtube_innertube as it
from services.youtube_diag import (
    PLAYABILITY_TRUST_S,
    structured_playability,
    youtube_error_response,
    youtube_http_status,
    youtube_user_message,
)

VIDEO_ID = "aaaaaaaaaaa"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"

# The real measured pt-BR failure text: no English marker matches it, and it is
# the reason the structured record is needed at all.
DEAD = RuntimeError(
    "[youtube] youtube-liv: Este vídeo não está disponível"
)
# A localized bot gate: also matches no English marker.
GATE = RuntimeError(
    "[youtube] youtube-liv: Faça login para confirmar que você não é um robô"
)
RETRY_PREVIEW = "YouTube preview is temporarily restricted. Try again in a few minutes."
GENERIC_PREVIEW = "Preview failed — try again."


def _record(monkeypatch, kind, *, reason="motivo localizado", status="ERROR", age_s=0.0):
    """Store a playability record for VIDEO_ID as of age_s seconds ago."""
    monkeypatch.setitem(it._LAST_PLAYABILITY, VIDEO_ID, (status, reason, kind))
    monkeypatch.setitem(it._PLAYABILITY_AT, VIDEO_ID, time.monotonic() - age_s)


def test_extract_video_id_accepts_the_fixture_url():
    # The record is keyed by the id the url resolves to. If this drifts, every
    # test below silently exercises the "no record" path instead.
    assert it.extract_video_id(URL) == VIDEO_ID


def test_a_fatal_record_makes_a_localized_dead_video_a_404(monkeypatch):
    _record(monkeypatch, "fatal")
    assert structured_playability(URL) == ("unavailable", "motivo localizado")
    assert youtube_http_status(DEAD, url=URL) == 404
    assert youtube_user_message(DEAD, preview=True, url=URL) == "This video is unavailable."


def test_without_the_record_a_localized_dead_video_is_still_a_500():
    # The control for the test above: the record is the fix, not the text.
    assert structured_playability(URL) is None
    assert youtube_http_status(DEAD, url=URL) == 500
    assert youtube_user_message(DEAD, preview=True, url=URL) == GENERIC_PREVIEW


def test_a_retry_record_keeps_a_localized_gate_retryable(monkeypatch):
    _record(monkeypatch, "retry", status="LOGIN_REQUIRED")
    assert structured_playability(URL) == ("gate", "motivo localizado")
    assert youtube_http_status(GATE, url=URL) == 503
    assert youtube_user_message(GATE, preview=True, url=URL) == RETRY_PREVIEW


def test_english_soft_text_still_wins_over_a_fatal_record(monkeypatch):
    # A recognized bot gate must never become a 404 because of a fatal record
    # for the same video: the tables are consulted first, on purpose.
    _record(monkeypatch, "fatal")
    exc = RuntimeError("Sign in to confirm you're not a bot")
    assert youtube_http_status(exc, url=URL) == 503
    assert youtube_user_message(exc, preview=True, url=URL) == RETRY_PREVIEW


def test_a_malformed_record_is_no_record_and_never_raises(monkeypatch):
    # Regression: the unpack used to sit outside the guard, so a 2-tuple raised
    # ValueError while another exception was already being handled.
    monkeypatch.setitem(it._LAST_PLAYABILITY, VIDEO_ID, ("ERROR", "boom"))
    monkeypatch.setitem(it._PLAYABILITY_AT, VIDEO_ID, time.monotonic())
    assert structured_playability(URL) is None
    assert youtube_http_status(DEAD, url=URL) == 500
    assert youtube_user_message(DEAD, preview=True, url=URL) == GENERIC_PREVIEW


def test_an_unknown_kind_is_no_record(monkeypatch):
    _record(monkeypatch, "maybe")
    assert structured_playability(URL) is None
    assert youtube_http_status(DEAD, url=URL) == 500


def test_a_stale_record_is_ignored(monkeypatch):
    _record(monkeypatch, "fatal", age_s=PLAYABILITY_TRUST_S + 1.0)
    assert structured_playability(URL) is None
    assert youtube_http_status(DEAD, url=URL) == 500


def test_one_lookup_feeds_both_the_status_and_the_message(monkeypatch):
    _record(monkeypatch, "fatal")
    assert youtube_error_response(DEAD, preview=True, url=URL) == (
        404,
        "This video is unavailable.",
    )
    # A precomputed hint is used verbatim, so the pair cannot disagree.
    assert youtube_http_status(DEAD, hint=("unavailable", "x")) == 404
    assert youtube_user_message(DEAD, preview=True, hint=None) == GENERIC_PREVIEW
    assert youtube_user_message(DEAD, preview=True, hint=("gate", "x")) == RETRY_PREVIEW


def test_no_url_means_no_structured_answer():
    assert structured_playability(None) is None
    assert structured_playability("") is None
    assert youtube_http_status(DEAD) == 500
    assert youtube_user_message(DEAD, preview=True) == GENERIC_PREVIEW


def test_an_error_text_with_both_signals_agrees_with_itself():
    # yt-dlp prints one ERROR line per player client it tried, so a single blob
    # can carry both a definitive phrase and a bot-gate phrase. Measured defect:
    # the status walked its tables unavailable-first (404) while the message
    # walked them transient-first ("try again"). One precedence answers both now.
    exc = RuntimeError(
        "[youtube] aaaaaaaaaaa: Video unavailable. "
        "[youtube] aaaaaaaaaaa: Sign in to confirm you're not a bot"
    )
    assert youtube_error_response(exc, preview=True) == (503, RETRY_PREVIEW)
    # preview=False keeps the download wording, but it is the same transient class
    assert youtube_error_response(exc) == (
        503,
        "Could not load this YouTube video — try again in a moment.",
    )


def test_the_precedence_table_is_the_same_for_both_outputs():
    dead = RuntimeError("[youtube] x: Video unavailable")
    members = RuntimeError("[youtube] x: This video is members-only content")
    private = RuntimeError("[youtube] x: This is a private video")
    assert youtube_error_response(dead) == (404, "This video is unavailable.")
    assert youtube_error_response(members) == (
        403,
        "Members-only video — requires channel membership.",
    )
    assert youtube_error_response(private) == (403, "This video is private.")
