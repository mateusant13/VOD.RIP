"""The yt-dlp outcome vocabulary, the learned per-channel skip, and the
error-ring budget.

THE DEFECT, MEASURED (not inferred). The runtime error log
(``%APPDATA%\\VOD.RIP\\logs\\errors.jsonl``) is a 500-record ring buffer. Reading
the live file (96,755 bytes, 504 lines, window 2026-09-15 -> 2026-10-05) and
normalising the yt-dlp records:

    106  "Faça login para confirmar que você não é um bot"  (bot wall)
     52  "This channel does not have a streams tab"          (@srdoglol)
     51  "Unable to download API page: HTTP Error 404"       (@seeelbr)
     47  "This channel does not have a streams tab"          (@JBSniperPRIME)
     45  "This channel does not have a shorts tab"           (@NecrosOW)
     39  "Seja membro do canal para ter acesso ..."         (members-only)
     32  "Este vídeo foi removido pelo usuário ..."          (removed)
     25  "This live event will begin in a few moments."      (live upcoming)
     22  "Offline."                                          (live offline)
     21  "Join this channel to get access to members-only ..."
     15  "Ele foi bloqueado devido ao conteúdo reivindicado por SME."
      3  subscriber-only / cookie-file lines
      2  "@seeelbr/shorts ... HTTP Error 404"

411 of the 500 retained records were ``yt-dlp: ERROR:`` lines, and EVERY one is
an expected condition. Two consequences, both real:

  1. A genuine defect is evicted within hours — a ring that is ~100% expected
     outcomes has almost no headroom for a real error.
  2. A channel that has no /streams tab (144 records here) burns a governor
     token, a yt-dlp spawn and ~8s EVERY cycle, and is indistinguishable in
     the log from a channel that is merely transiently offline.

WHAT IS PINNED HERE:

  1. every vocabulary code, one test each, on the REAL production message
     strings (not paraphrases);
  2. an unrecognised line classifies as `unknown` — the classifier is
     conservative, and a line is never guessed into an expected bucket;
  3. the learn-and-skip path, for an absent tab and for a 404 channel;
  4. that a skip draws NO governor token, while a GOVERNOR REFUSAL still
     propagates (that behaviour is load-bearing and is owned by
     test_youtube_channel_walk_governor.py — this file only proves the skip
     does not swallow it);
  5. that the release path works AND survives a restart — a park that cannot be
     released would be worse than the bug, because a channel can gain a
     streams tab later;
  6. the UTF-8 round-trip of a Portuguese string into the JSONL sink;
  7. that a classified-expected outcome does NOT append to the error ring
     while `unknown` DOES.

No network: the yt-dlp context manager is stubbed at the module seam
(`services.ytdlp_guard`), the same seam test_youtube_channel_walk_governor.py
documents — `list_channel_videos_sync` imports it from INSIDE the function, so
patching the module attribute is the only interception point.

Run from backend/: python -m pytest tests/test_ytdlp_outcomes.py
"""
from __future__ import annotations

import contextlib
import json
import logging
import types

import pytest

from services import archive_db, rate_budget, ytdlp_guard, ytdlp_outcomes, youtube_service
from services.archive_ytdlp import YtGovernorExhausted

# --- the REAL production message strings ------------------------------------
# Verbatim from the live error ring, accents and all. Paraphrases would let a
# marker silently stop matching the thing it was written for.
BOT_WALL = (
    "ERROR: [youtube] g1rlQ_jOCI4: Faça login para confirmar que você não é "
    "um bot. Isso ajuda a proteger nossa comunidade. Saiba mais"
)
TAB_ABSENT_STREAMS = "ERROR: [youtube:tab] @srdoglol: This channel does not have a streams tab"
TAB_ABSENT_SHORTS = "ERROR: [youtube:tab] @NecrosOW: This channel does not have a shorts tab"
CHANNEL_404 = (
    "ERROR: [youtube:tab] @seeelbr/streams: Unable to download API page: "
    "HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)"
)
CHANNEL_404_SHORTS = (
    "ERROR: [youtube:tab] @seeelbr/shorts: Unable to download API page: "
    "HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)"
)
LIVE_UPCOMING = "ERROR: [youtube] VzuPKrGl0z8: This live event will begin in a few moments."
LIVE_OFFLINE = "ERROR: [youtube] VzuPKrGl0z8: Offline."
MEMBERS_ONLY_PT = (
    "ERROR: [youtube] VzuPKrGl0z8: Seja membro do canal para ter acesso a "
    "conteúdo exclusivo, como este vídeo, e a outros benefícios especiais."
)
REMOVED_PT = "ERROR: [youtube] VzuPKrGl0z8: Este vídeo foi removido pelo usuário que fez o envio"
SME_CLAIM_PT = (
    "ERROR: [youtube] VzuPKrGl0z8: Ele foi bloqueado devido ao conteúdo "
    "reivindicado por SME."
)

# A real defect: nothing in the vocabulary describes it, so it must stay an
# error. This is the whole point of the conservative default.
REAL_DEFECT = "ERROR: [youtube] VzuPKrGl0z8: Unable to write client (deadline exceeded)"


# --- 1. the vocabulary, one test per code -----------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        (TAB_ABSENT_STREAMS, ytdlp_outcomes.TAB_ABSENT),
        (TAB_ABSENT_SHORTS, ytdlp_outcomes.TAB_ABSENT),
        (CHANNEL_404, ytdlp_outcomes.CHANNEL_GONE),
        (CHANNEL_404_SHORTS, ytdlp_outcomes.CHANNEL_GONE),
        (BOT_WALL, ytdlp_outcomes.BOT_WALL_UNAUTHENTICATED),
        (LIVE_UPCOMING, ytdlp_outcomes.LIVE_UPCOMING),
        (LIVE_OFFLINE, ytdlp_outcomes.LIVE_OFFLINE),
        (MEMBERS_ONLY_PT, ytdlp_outcomes.VIDEO_RESTRICTED),
        (REMOVED_PT, ytdlp_outcomes.VIDEO_RESTRICTED),
        (SME_CLAIM_PT, ytdlp_outcomes.VIDEO_RESTRICTED),
    ],
    ids=[
        "tab_absent-streams", "tab_absent-shorts",
        "channel_gone-streams", "channel_gone-shorts",
        "bot_wall-pt-br", "live_upcoming", "live_offline",
        "video_restricted-members-pt", "video_restricted-removed-pt",
        "video_restricted-sme-pt",
    ],
)
def test_each_vocabulary_code_classifies(message, expected):
    assert ytdlp_outcomes.classify(message) == expected
    assert ytdlp_outcomes.is_expected(expected)


def test_every_expected_code_is_reachable_from_a_real_message():
    """A code nobody can produce is dead vocabulary. Each EXPECTED_CODES entry
    must be classified from at least one of the real production strings."""
    real = [TAB_ABSENT_STREAMS, TAB_ABSENT_SHORTS, CHANNEL_404, CHANNEL_404_SHORTS,
            BOT_WALL, LIVE_UPCOMING, LIVE_OFFLINE, MEMBERS_ONLY_PT, REMOVED_PT, SME_CLAIM_PT]
    produced = {ytdlp_outcomes.classify(m) for m in real}
    assert produced == set(ytdlp_outcomes.EXPECTED_CODES), (
        f"unreachable codes: {set(ytdlp_outcomes.EXPECTED_CODES) - produced}"
    )


def test_unknown_is_not_in_the_expected_vocabulary():
    """`unknown` is the ABSENCE of a match, not an outcome we expect. If it
    were in EXPECTED_CODES, an unrecognised line would read as expected."""
    assert ytdlp_outcomes.UNKNOWN not in ytdlp_outcomes.EXPECTED_CODES
    assert not ytdlp_outcomes.is_expected(ytdlp_outcomes.UNKNOWN)


# --- 2. the conservative default --------------------------------------------


def test_real_defect_is_unknown_not_guessed():
    assert ytdlp_outcomes.classify(REAL_DEFECT) == ytdlp_outcomes.UNKNOWN
    assert not ytdlp_outcomes.is_expected_text(REAL_DEFECT)


@pytest.mark.parametrize(
    "message",
    [
        "",
        "   ",
        None,
        "ERROR: something nobody has seen before",
        "HTTP Error 500: Internal Server Error",   # a 5xx is NOT channel_gone
        "Unable to download webpage: timed out",    # nor is a timeout a 404
        "This video is not available in your country",  # near-miss, not in vocab
    ],
    ids=["empty", "blank", "none", "novel", "http-500", "timeout", "near-miss"],
)
def test_unrecognised_never_becomes_expected(message):
    assert ytdlp_outcomes.classify(message) == ytdlp_outcomes.UNKNOWN


def test_a_500_is_not_read_as_a_permanent_channel_verdict():
    """The 404 marker is deliberately scoped: a transient 5xx on the same code
    path must stay a real error, or the walk would park a healthy channel."""
    text = "ERROR: [youtube:tab] @somechan/streams: Unable to download API page: HTTP Error 503"
    assert ytdlp_outcomes.classify(text) == ytdlp_outcomes.UNKNOWN


def test_only_two_codes_are_learnable_and_skippable():
    """The permanence split is the crux: a bot wall is IP state and a live
    'Offline' resolves on its own. Parking either would hide a channel that
    recovers, which is the bug the release path exists to prevent."""
    assert ytdlp_outcomes.PERMANENT_CODES == frozenset(
        {ytdlp_outcomes.TAB_ABSENT, ytdlp_outcomes.CHANNEL_GONE}
    )
    for transient in (ytdlp_outcomes.BOT_WALL_UNAUTHENTICATED, ytdlp_outcomes.LIVE_OFFLINE,
                      ytdlp_outcomes.LIVE_UPCOMING, ytdlp_outcomes.VIDEO_RESTRICTED):
        assert not ytdlp_outcomes.is_permanent(transient), (
            f"{transient} must never be a permanent per-channel verdict"
        )


def test_outcome_text_is_derived_for_every_code_and_falls_back():
    for code in ytdlp_outcomes.EXPECTED_CODES:
        assert ytdlp_outcomes.outcome_text(code)
    assert "Unrecognised" in ytdlp_outcomes.outcome_text("not_a_code")


def test_channel_and_tab_are_parsed_from_a_channel_tab_message():
    assert ytdlp_outcomes.channel_from_message(CHANNEL_404) == "seeelbr"
    assert ytdlp_outcomes.tab_from_message(CHANNEL_404) == "streams"
    # A per-video line names no channel: attributing one to a channel would be
    # a verdict nobody made.
    assert ytdlp_outcomes.channel_from_message(BOT_WALL) is None
    assert ytdlp_outcomes.channel_from_message(REAL_DEFECT) is None


# --- 3/4/5. learn, skip, release, restart -----------------------------------


class _FakeYdl:
    """Minimal stand-in for a YoutubeDL handle (flat playlist extract)."""

    def __init__(self, exc: BaseException | None = None):
        self._exc = exc
        self.extracted: list[str] = []

    def extract_info(self, url, download=False):
        self.extracted.append(url)
        if self._exc is not None:
            raise self._exc
        return {
            "id": "VzuPKrGl0z8", "title": "some channel", "channel": "some channel",
            "uploader": "some channel", "channel_id": "UCaaa", "uploader_id": "UCaaa",
            "entries": [{"id": "VzuPKrGl0z8", "title": "a stream", "duration": 60}],
        }


@pytest.fixture()
def ydl_seam(monkeypatch):
    """Bind a _FakeYdl at the ONE seam, via the module (the function-local
    import means there is no module attribute to patch)."""

    def _install(exc: BaseException | None = None) -> _FakeYdl:
        ydl = _FakeYdl(exc)

        @contextlib.contextmanager
        def _cm(opts):
            yield ydl

        monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts, **_control: _cm(opts))
        return ydl

    return _install


@pytest.fixture()
def acquires(monkeypatch):
    """Record every governor acquire(); scripted decision, last one repeats."""
    ctl = types.SimpleNamespace(calls=[], script=[])

    def _acquire(platform, source="auto", *, kind=None):
        ctl.calls.append((platform, source, kind))
        if ctl.script:
            d = ctl.script.pop(0) if len(ctl.script) > 1 else ctl.script[0]
        else:
            d = rate_budget.Decision(
                platform=platform, source=source, allowed=True, wait_s=0.0,
                ceiling_rpm=60.0, tokens=5.0, reason="ok",
            )
        return d

    monkeypatch.setattr(rate_budget, "acquire", _acquire)
    return ctl


def _decision(allowed, wait_s=0.0, source="auto"):
    return rate_budget.Decision(
        platform="youtube", source=source, allowed=allowed, wait_s=wait_s,
        ceiling_rpm=2.0, tokens=-1.0, reason="ok" if allowed else "auto_exhausted",
    )


@pytest.fixture()
def clean_outcomes():
    """Every test starts with NO learned state and no memory."""
    archive_db.execute("DELETE FROM youtube_channel_outcomes")
    yield
    archive_db.execute("DELETE FROM youtube_channel_outcomes")


def test_absent_tab_is_learned_then_skipped(ydl_seam, acquires, clean_outcomes):
    """The 52-cycle loop, in two walks.

    Walk 1 reaches yt-dlp, fails with a permanent per-channel condition, and
    the walk LEARNS it. Walk 2 must not reach yt-dlp at all — and must not
    draw a governor token, because it makes no request."""
    ydl = ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))

    rows = youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                                    source="auto", enrich=False)
    assert rows == [], "a failed tab lists nothing"
    assert len(ydl.extracted) == 1, "the first walk must actually ask"
    assert acquires.calls == [("youtube", "auto", "yt_channel_list")]

    parked = archive_db.channel_outcome_parked("srdoglol", "streams")
    assert parked is not None, "a permanent per-channel condition must be learned"
    assert parked["outcome_code"] == ytdlp_outcomes.TAB_ABSENT
    assert parked["skipped"] == 0, "a fresh park has skipped nothing yet (NULL != 0)"

    # Walk 2: learned, so no request and no token.
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1, "the learned condition must prevent the second request"
    assert acquires.calls == [("youtube", "auto", "yt_channel_list")], (
        "a remembered skip draws NO governor token: it makes no request, and "
        "charging it would make the token count lie about real egress"
    )
    assert archive_db.note_channel_outcome_skipped("srdoglol", "streams") == 1


def test_404_channel_is_learned_then_skipped(ydl_seam, acquires, clean_outcomes):
    """The @seeelbr case: 51 ring records, retried every ~12 minutes since
    2026-09-04 with no memory that it ever failed."""
    ydl = ydl_seam(RuntimeError(CHANNEL_404))

    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1
    parked = archive_db.channel_outcome_parked("seeelbr", "streams")
    assert parked is not None
    assert parked["outcome_code"] == ytdlp_outcomes.CHANNEL_GONE

    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1, "a 404 channel must not be re-probed"
    assert len(acquires.calls) == 1


def test_the_skip_is_keyed_per_tab_not_per_channel(ydl_seam, acquires, clean_outcomes):
    """"No /streams tab" says NOTHING about /videos. A channel that never
    livestreams still has an ordinary videos tab, and parking the whole
    channel on its streams verdict would be a false — and, without a release,
    irreversible — claim."""
    ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is not None
    assert archive_db.channel_outcome_parked("srdoglol", "videos") is None

    ydl = ydl_seam()  # the videos tab answers normally
    rows = youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="videos",
                                                    source="auto", enrich=False)
    assert rows, "the videos tab must still be walked after the streams park"
    assert len(ydl.extracted) == 1


def test_a_released_park_is_not_re_applied(ydl_seam, acquires, clean_outcomes):
    """release_channel_outcome must make the condition inert, not delete the
    history: a row with released_at set is never skipped again."""
    ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is not None

    assert youtube_service.release_learned_channel_outcome("srdoglol", "streams") == 1
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is None

    ydl = ydl_seam()
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1, "a released park must let the walk ask again"


def test_a_park_rearms_when_the_channel_answers_the_same_way_again(ydl_seam, acquires, clean_outcomes):
    """Re-seeing the condition makes it CURRENT again, not historical: the
    channel is answering that way right now."""
    ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    youtube_service.release_learned_channel_outcome("srdoglol", "streams")
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is None

    ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is not None


def test_transient_outcomes_are_never_learned(ydl_seam, acquires, clean_outcomes):
    """A bot wall and a live 'Offline' must not become a per-channel verdict.
    Parking them would hide a channel that recovers — and the skip would be
    permanent while the condition is not."""
    for message in (BOT_WALL, LIVE_OFFLINE, LIVE_UPCOMING):
        ydl_seam(RuntimeError(message))
        youtube_service.list_channel_videos_sync("@somechan", 5, playlist="streams",
                                                source="auto", enrich=False)
        assert archive_db.channel_outcome_parked("somechan", "streams") is None, (
            f"a transient condition ({message[:40]}) must not be learned"
        )


def test_a_real_defect_is_not_learned_either(ydl_seam, acquires, clean_outcomes):
    """Only PERMANENT_CODES are learned. An unknown failure must not park a
    channel on a verdict nobody established."""
    ydl_seam(RuntimeError(REAL_DEFECT))
    youtube_service.list_channel_videos_sync("@somechan", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert archive_db.channel_outcome_parked("somechan", "streams") is None


def test_a_skipped_walk_reports_uncovered_never_verified_empty(ydl_seam, acquires, clean_outcomes):
    """The skip returns an empty listing, but coverage is UNKNOWN — the walk
    made no request. Reporting it as a verified-empty channel is how an empty
    listing gets cached and the sweep concludes a live channel has no videos."""
    ydl_seam(RuntimeError(CHANNEL_404))
    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False,
                                            return_has_more=True, return_crawl_saturation=True)
    rows, has_more, saturated = youtube_service.list_channel_videos_sync(
        "@seeelbr", 5, playlist="streams", source="auto", enrich=False,
        return_has_more=True, return_crawl_saturation=True,
    )
    assert rows == [] and has_more is False
    assert saturated is True, "an un-requested walk cannot claim complete coverage"


def test_a_governor_refusal_still_propagates_and_is_not_learned(ydl_seam, acquires, clean_outcomes):
    """A dry pool says 'not now', not 'this channel is unprobeable'. The
    refusal must reach the caller AND must not be recorded as a per-channel
    condition — that is what keeps the skip from caching a rate-limit refusal
    as a permanent verdict."""
    acquires.script = [_decision(False, wait_s=1.0, source="auto")]
    ydl_seam(RuntimeError(CHANNEL_404))  # never reached

    with pytest.raises(YtGovernorExhausted):
        youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                                source="auto", enrich=False)
    assert archive_db.channel_outcome_parked("seeelbr", "streams") is None


def test_a_learned_broken_table_degrades_to_no_memory(ydl_seam, acquires, clean_outcomes, monkeypatch):
    """A learning table that raises must not become a new failure mode for the
    walk. It degrades to exactly today's behaviour: try the channel."""
    def _boom(*a, **kw):
        raise RuntimeError("learning table unavailable")

    monkeypatch.setattr(archive_db, "channel_outcome_parked", _boom)
    ydl = ydl_seam()
    rows = youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                                    source="auto", enrich=False)
    assert rows, "a broken learning table must not break the listing"
    assert len(ydl.extracted) == 1


# --- 5b. durability across a restart ----------------------------------------


def _simulate_restart() -> None:
    """Drop the per-process schema cache exactly as a fresh process would.

    The DB is left alone — it is what survives a restart, and that is the
    whole point. A build that kept the memory in a module dict would pass
    without this, so the dict is cleared if one exists (pre-fix builds)."""
    for name in ("_deep_jobs_tables_ok",):
        cache = getattr(archive_db, name, None)
        if isinstance(cache, set):
            cache.clear()
    for mod in (youtube_service, ytdlp_guard):
        legacy = getattr(mod, "_channel_outcome_parked_cache", None)
        if isinstance(legacy, dict):
            legacy.clear()


def test_park_survives_a_restart(ydl_seam, acquires, clean_outcomes):
    """The walk must not re-prove, after a restart, a condition it already
    learned — the 30-day retry loop this whole change exists to stop."""
    ydl = ydl_seam(RuntimeError(CHANNEL_404))
    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1

    _simulate_restart()

    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1, (
        "the learned condition must survive a restart; an in-process cache "
        "would be re-proved on every boot and the loop would continue"
    )


def test_release_survives_a_restart(ydl_seam, acquires, clean_outcomes):
    """A park that survives a restart but whose RELEASE does not would be
    worse than the bug: the channel is skipped forever and the user is given
    no way back. Both halves must be durable."""
    ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    youtube_service.release_learned_channel_outcome("srdoglol", "streams")

    _simulate_restart()

    ydl = ydl_seam()
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1, "a release must survive a restart too"
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is None


def test_the_skip_counter_survives_a_restart(ydl_seam, acquires, clean_outcomes):
    """A counter that resets on restart reports a saving that did not happen.
    The count is what makes the skip observable instead of a silent zero."""
    ydl_seam(RuntimeError(CHANNEL_404))
    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False)
    for _ in range(3):
        youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                                source="auto", enrich=False)
    assert archive_db.channel_outcome_parked("seeelbr", "streams")["skipped"] == 0, (
        "the walk counts nothing on its own; only an explicit note does"
    )
    _simulate_restart()
    assert archive_db.note_channel_outcome_skipped("seeelbr", "streams") == 1
    assert archive_db.channel_outcome_parked("seeelbr", "streams")["skipped"] == 1


def test_handles_are_normalised_so_one_channel_parked_twice_is_one_channel():
    """'@SeeelBR' and 'seeelbr' are one channel. If the key were not
    normalised, the walk would park it twice and skip neither consistently."""
    archive_db.learn_channel_outcome("@SeeelBR", "streams", ytdlp_outcomes.CHANNEL_GONE)
    archive_db.learn_channel_outcome("seeelbr", "streams", ytdlp_outcomes.CHANNEL_GONE)
    rows = archive_db.channel_outcome_snapshot()
    assert len(rows) == 1, f"one channel must be one row, got {rows}"
    assert archive_db.channel_outcome_parked("@SEEELBR", "streams") is not None


def test_release_without_a_tab_releases_every_tab(ydl_seam, acquires, clean_outcomes):
    """The operator escape hatch: one call clears the channel."""
    ydl_seam(RuntimeError(CHANNEL_404))
    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                            source="auto", enrich=False)
    ydl_seam(RuntimeError(CHANNEL_404_SHORTS))
    youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="shorts",
                                            source="auto", enrich=False)
    assert len(archive_db.channel_outcome_snapshot()) == 2
    assert youtube_service.release_learned_channel_outcome("seeelbr") == 2
    assert archive_db.channel_outcome_snapshot() == []


# --- 6. the UTF-8 round-trip ------------------------------------------------
#
# The briefing reported the bot-wall line stored as "Fa?a login", i.e. yt-dlp's
# UTF-8 decoded with the wrong codec. MEASURED AGAINST THE LIVE FILE, that is
# NOT true: errors.jsonl is 96,755 bytes, decodes under a STRICT UTF-8 decoder
# with ZERO U+FFFD characters, and the sequence at the "Fa?a" position is
# `46 61 c3 a7 61` = "Faça" — correct UTF-8. The rendering was a console
# artifact, not a stored defect.
#
# The sink is therefore ALREADY correct, and this test is the PIN that keeps it
# that way — a regression guard, not a fix for an observed break. It is
# deliberately an exact round-trip assertion: a "fix" that stripped non-ASCII
# (replacing ç with ?) would pass a looser test and silently destroy every
# Portuguese yt-dlp message from then on.


def test_a_portuguese_string_round_trips_through_the_error_log_exactly(tmp_path, monkeypatch):
    from services import error_log

    monkeypatch.setenv("VODRIP_APP_DATA", str(tmp_path / "appdata"))
    error_log.clear_error_ring_for_tests()

    # The exact production bot-wall text, accents included.
    message = f"yt-dlp: ERROR: [youtube] g1rlQ_jOCI4: {BOT_WALL}"
    error_log.record_error("error", message)

    # On disk, as bytes: the accents must be the UTF-8 encoding of ç/ã/é —
    # not '?' and not a mojibake double-encoding.
    raw = error_log._error_log_path().read_bytes()
    assert " Faça login ".encode("utf-8") in raw
    assert b"Fa?a" not in raw
    assert "não".encode("utf-8") in raw

    # Strict decode: an ill-formed byte sequence would raise here.
    text = raw.decode("utf-8")
    assert "�" not in text
    row = json.loads(text.strip().splitlines()[-1])
    assert row["message"] == message, "the message must survive byte-exact"


# --- 7. the error-ring budget -----------------------------------------------


@pytest.fixture()
def error_ring(tmp_path, monkeypatch):
    """A REAL, installed error-log sink.

    Without this the two ring-budget tests pass for the wrong reason: the
    root handler is what forwards a `logger.error` into the ring, so with it
    absent NOTHING reaches the ring and "expected outcomes stay out" would be
    true for a trivial reason. install_error_handler is process-global and
    one-shot, so the handler is removed and the flag restored afterwards —
    otherwise this fixture would leak the sink into every later test."""
    from services import error_log

    monkeypatch.setenv("VODRIP_APP_DATA", str(tmp_path / "appdata"))
    error_log.clear_error_ring_for_tests()
    # install_error_handler is one-shot and process-global: the FIRST caller
    # gets the handler, every later caller gets None while _INSTALLED stays
    # True. So look for an already-attached sink FIRST and only call install
    # when there is none -- otherwise the second test in this file would find
    # neither a returned handler nor a live one and error out in setup.
    root = logging.getLogger()
    handler = next(
        (h for h in root.handlers if isinstance(h, error_log._ErrorFileHandler)), None
    )
    if handler is None:
        handler = error_log.install_error_handler()
    assert handler is not None, "the error sink must be installed for a ring assertion"
    yield error_log
    if handler in root.handlers:
        root.removeHandler(handler)
    # install_error_handler is one-shot, so removing the handler without
    # clearing the flag would make the NEXT test unable to attach its own and
    # fail in setup. Reset it so each test re-installs cleanly.
    error_log._INSTALLED = False
    error_log.clear_error_ring_for_tests()


def test_an_expected_outcome_does_not_enter_the_error_ring(ydl_seam, acquires, clean_outcomes,
                                                           error_ring):
    """THE consequence: the ring buffer's budget belongs to real defects.

    411 of 500 live records were yt-dlp errors, every one expected. With them
    in the ring, a genuine defect is evicted within hours."""
    error_log = error_ring
    log = ytdlp_guard.ytdlp_console_logger()
    for message in (TAB_ABSENT_STREAMS, CHANNEL_404, BOT_WALL, LIVE_OFFLINE, MEMBERS_ONLY_PT):
        log.error(f"yt-dlp: {message}")

    assert error_log.get_error_ring(50) == [], (
        "expected outcomes must not consume the error ring"
    )


def test_an_unknown_outcome_does_enter_the_error_ring(error_ring):
    """The other half, and the one that must never regress: a line the
    vocabulary does not recognise is a real error and keeps its record."""
    error_log = error_ring
    log = ytdlp_guard.ytdlp_console_logger()
    log.error(f"yt-dlp: {REAL_DEFECT}")

    rows = error_log.get_error_ring(50)
    assert len(rows) == 1, "an unrecognised yt-dlp failure must stay in the ring"
    assert "Unable to write client" in rows[0]["message"]


def test_the_ring_sink_is_live_so_the_two_tests_above_are_not_vacuous(error_ring):
    """Belt and braces on the fixture itself: a real defect DOES land in the
    ring in the same test session. Without this, both budget tests could pass
    against a sink that is simply not wired, which is how a 'no expected
    errors' assertion becomes a tautology."""
    error_log = error_ring
    logging.getLogger("probe.sink.live").error("a genuine defect")
    assert len(error_log.get_error_ring(10)) == 1, (
        "if this fails the two budget tests above prove nothing"
    )


def test_the_console_filter_keeps_exactly_the_expected_counts():
    """A bare drop makes 'the filter works' and 'yt-dlp went quiet' look
    identical. The per-code counter is the honesty discipline 3f4ceb9 applied
    to /api/asr/runtime: a code absent from the map was never OBSERVED, which
    is not a count of zero."""
    log = ytdlp_guard._YtdlpConsoleLogger()
    before = ytdlp_guard._YtdlpConsoleLogger.expected_counts()

    log.error(f"yt-dlp: {TAB_ABSENT_STREAMS}")
    log.error(f"yt-dlp: {CHANNEL_404}")
    log.error(f"yt-dlp: {BOT_WALL}")
    log.error(f"yt-dlp: {REAL_DEFECT}")  # not counted: it is not an outcome

    after = ytdlp_guard._YtdlpConsoleLogger.expected_counts()
    assert after[ytdlp_outcomes.TAB_ABSENT] == before[ytdlp_outcomes.TAB_ABSENT] + 1
    assert after[ytdlp_outcomes.CHANNEL_GONE] == before[ytdlp_outcomes.CHANNEL_GONE] + 1
    assert after[ytdlp_outcomes.BOT_WALL_UNAUTHENTICATED] == (
        before[ytdlp_outcomes.BOT_WALL_UNAUTHENTICATED] + 1
    )
    assert ytdlp_outcomes.UNKNOWN not in after, (
        "unknown is not an outcome we expect, so it is never counted as one"
    )
