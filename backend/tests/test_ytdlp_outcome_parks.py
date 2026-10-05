"""The USER-VISIBLE half of the learned per-channel yt-dlp park.

WHAT IS PINNED HERE, and why it needed pinning at all. Merge b789bba taught the
channel walk to learn a permanent, per-(channel, tab) condition and skip it
(backend/services/ytdlp_outcomes.py, archive_db.youtube_channel_outcomes,
youtube_service._channel_outcome_parked). That made the walk cheap, and it made
the park INVISIBLE: the owner could not see which channels were parked, could
not see why, and could not un-park one. A park the owner cannot see and cannot
reverse is nearly as bad as the bug it replaced - a channel that later gains a
/streams tab would stay skipped forever with no way back.

So the gap this file covers is the surface, and the three properties that make
it honest rather than decorative:

  1. THE CODES ARE REAL. The endpoint must serve the vocabulary codes the walk
     actually learns - not English prose, and not a re-derived guess. A client
     branches on the code and renders its own localised phrase, so a payload
     carrying prose would both break localisation and let a wording edit break a
     stored row.
  2. ABSENT IS NOT HEALTHY. A channel with no learned outcome, and a code this
     build does not recognise, both read `learned: null` - WITH a machine
     readable status saying which. Neither may come back as a fabricated
     success, because "we have no memory of this channel" is not "this channel
     works" and conflating them is how an empty listing gets cached as a
     verified-empty channel.
  3. THE RELEASE IS DURABLE AND NO-OP-SAFE. It must survive a genuine
     connection reopen (archive_db.close_connections, not just a cache clear),
     and a second press must report 0 released rather than the historical
     total - the difference between "released your park" and a number that
     looks like success while nothing changed.

No network: the yt-dlp context manager is stubbed at the module seam
(`services.ytdlp_guard`), the same seam test_ytdlp_outcomes.py documents.
The archive DB is the conftest scratch DB (VODRIP_ARCHIVE_DB), never the live
archive on H:.

Run from backend/: python -m pytest tests/test_ytdlp_outcome_parks.py
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi import HTTPException

from routers import channels
from services import archive_db, rate_budget, ytdlp_guard, ytdlp_outcomes, youtube_service

# The release request model is imported INSIDE the helper, not at module top
# level, on purpose. At top level a build without the route aborts collection
# with one ImportError, which reports "a name is missing" and hides which
# behaviour is absent. Imported lazily, the same build fails 18 named tests
# that each say what the surface could not do - which is the evidence that
# actually distinguishes "the park is invisible" from "a typo".
def _release(channel: str, tab: str | None = None, platform: str = "youtube") -> dict:
    from models.schemas import ChannelOutcomeReleaseRequest

    return asyncio.run(
        channels.release_channel_outcome_park(
            ChannelOutcomeReleaseRequest(channel=channel, tab=tab, platform=platform)
        )
    )

# Verbatim from the live error ring (same constants test_ytdlp_outcomes.py uses):
# a paraphrase would let a marker stop matching the message it was written for.
TAB_ABSENT_STREAMS = "ERROR: [youtube:tab] @srdoglol: This channel does not have a streams tab"
CHANNEL_404 = (
    "ERROR: [youtube:tab] @seeelbr/streams: Unable to download API page: "
    "HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)"
)


# --- helpers ----------------------------------------------------------------


def _learn(channel: str, tab: str, code: str) -> None:
    archive_db.learn_channel_outcome(channel, tab, code)


def _snapshot(platform: str = "youtube") -> dict:
    return asyncio.run(channels.channel_outcome_parks(platform=platform))


def _read(channel: str, tab: str = "", platform: str = "youtube") -> dict:
    return asyncio.run(channels.channel_outcome_park(channel=channel, tab=tab, platform=platform))


def _reopen() -> None:
    """Close EVERY connection archive_db holds, so the next read re-opens.

    This is a genuine reopen, not the cache clear test_ytdlp_outcomes.py uses:
    a durability claim that only survives a cache clear is a claim about a
    process-lifetime dict, which is the bug this table replaced.
    """
    archive_db.close_connections()


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
            "entries": [],
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
    """Record every governor acquire(); always allow."""
    ctl = type("Ctl", (), {"calls": []})()
    monkeypatch.setattr(
        rate_budget,
        "acquire",
        lambda platform, source="auto", *, kind=None: ctl.calls.append(
            (platform, source, kind)
        ) or rate_budget.Decision(
            platform="youtube", source=source, allowed=True, wait_s=0.0,
            ceiling_rpm=60.0, tokens=5.0, reason="ok",
        ),
    )
    return ctl


@pytest.fixture()
def clean_outcomes():
    """Every test starts with NO learned state and no memory."""
    archive_db.execute("DELETE FROM youtube_channel_outcomes")
    yield
    archive_db.execute("DELETE FROM youtube_channel_outcomes")


# --- 1. the endpoint serves the REAL codes -----------------------------------


def test_the_snapshot_serves_what_the_walk_actually_learned(ydl_seam, acquires, clean_outcomes):
    """End to end through the real walk: the code the walk learned is the code
    the surface serves. Anything else (a re-derived guess, prose) would let the
    UI describe a park the walk did not make."""
    ydl = ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1

    body = _snapshot()
    assert body["count"] == 1, body
    row = body["parked"][0]
    assert row["channel"] == "srdoglol", "the row must be keyed by the NORMALISED handle"
    assert row["tab"] == "streams"
    assert row["outcome_code"] == ytdlp_outcomes.TAB_ABSENT
    assert row["known"] is True
    assert row["permanent"] is True
    assert row["first_seen"], "a park with no learning time cannot be aged"
    assert row["last_seen"] == row["first_seen"]


def test_the_snapshot_serves_every_parkable_code_as_a_code(clean_outcomes):
    """Both PERMANENT_CODES, keyed per (channel, tab) - and the codes are
    served, not the English sentence. A client localises from the code; prose in
    the payload would be an English literal rendered to a pt-BR user."""
    assert ytdlp_outcomes.PERMANENT_CODES == frozenset(
        {ytdlp_outcomes.TAB_ABSENT, ytdlp_outcomes.CHANNEL_GONE}
    ), "the parkable set is exactly the two permanent per-channel conditions"
    _learn("chanA", "streams", ytdlp_outcomes.TAB_ABSENT)
    _learn("chanA", "shorts", ytdlp_outcomes.CHANNEL_GONE)
    _learn("chanB", "streams", ytdlp_outcomes.CHANNEL_GONE)

    body = _snapshot()
    served = {(r["channel"], r["tab"]): r["outcome_code"] for r in body["parked"]}
    assert served == {
        # The stored handle is NORMALISED (lower-cased), which is what makes
        # '@SeeelBR' and 'seeelbr' one park instead of two.
        ("chana", "shorts"): ytdlp_outcomes.CHANNEL_GONE,
        ("chana", "streams"): ytdlp_outcomes.TAB_ABSENT,
        ("chanb", "streams"): ytdlp_outcomes.CHANNEL_GONE,
    }, "the park is per (channel, tab): one tab's verdict says nothing about another"
    assert body["count"] == 3

    for row in body["parked"]:
        assert row["outcome_code"] in ytdlp_outcomes.EXPECTED_CODES
        # No field may carry the module's English phrasing: that is the client's
        # to derive, localised.
        blob = repr(row)
        for code, prose in ytdlp_outcomes.OUTCOME_TEXT.items():
            assert prose not in blob, f"English prose for {code} leaked into the payload"


def test_the_skip_counter_is_served_verbatim_and_never_inflated(
    ydl_seam, acquires, clean_outcomes
):
    """The `skipped` counter is served exactly as stored, and a walk that skips
    from memory does NOT move it.

    Pinned because the walk deliberately counts nothing on its own (the merge's
    own suite asserts `skipped == 0` after four walks, and only an explicit
    `note_channel_outcome_skipped` moves it), and nothing in the shipped code
    calls that. So 0 is what the surface reports in production, and the UI is
    built to omit a zero rather than print it as a saving. An endpoint that
    invented a non-zero here would be reporting a saving that did not happen.
    """
    ydl_seam(RuntimeError(CHANNEL_404))
    for _ in range(4):
        youtube_service.list_channel_videos_sync("@seeelbr", 5, playlist="streams",
                                                source="auto", enrich=False)
    assert _snapshot()["parked"][0]["skipped"] == 0, (
        "the walk does not count its own skips; the endpoint must not invent one"
    )

    # An explicitly noted skip IS served - the counter is wired, not dropped.
    assert archive_db.note_channel_outcome_skipped("seeelbr", "streams") == 1
    _reopen()
    assert _snapshot()["parked"][0]["skipped"] == 1, (
        "a counted skip must be served, and must survive a reopen"
    )


def test_a_missing_counter_is_served_as_zero_not_as_a_claim(clean_outcomes):
    """A row the backend sent without a counter must not become a fabricated
    number: the endpoint serves 0 (the column's NOT NULL default), and the UI
    is what must decide a zero is not worth printing."""
    _learn("srdoglol", "streams", ytdlp_outcomes.TAB_ABSENT)
    row = _snapshot()["parked"][0]
    assert row["skipped"] == 0
    assert isinstance(row["skipped"], int), "a counter is never None, so NULL != 0 is not faked"


# --- 2. absent is NOT healthy -----------------------------------------------


def test_a_channel_with_no_learned_outcome_is_null_with_a_reason(clean_outcomes):
    """The load-bearing distinction. `learned` is null and `status` says it was
    never measured. A payload that answered `parked: false` or `ok: true` here
    would be a fabricated success: nobody asked this channel anything."""
    body = _read("neverasked", "streams")
    assert body["learned"] is None
    assert body["status"] == channels.PARK_STATUS_NOT_LEARNED
    assert body["status"] != channels.PARK_STATUS_PARKED
    # Nothing anywhere in the payload may claim the channel is fine.
    assert "ok" not in body
    assert "healthy" not in body
    assert "parked" not in body, "there is no field that could be read as 'not parked, all good'"


def test_a_released_park_reads_as_not_learned_not_as_parked(clean_outcomes):
    """After a release the memory is gone, so the honest status is the same
    unmeasured one - never a lingering 'parked' for a park that no longer skips."""
    _learn("srdoglol", "streams", ytdlp_outcomes.TAB_ABSENT)
    assert _read("srdoglol", "streams")["status"] == channels.PARK_STATUS_PARKED
    _release("srdoglol", "streams")
    body = _read("srdoglol", "streams")
    assert body["learned"] is None
    assert body["status"] == channels.PARK_STATUS_NOT_LEARNED


def test_an_unrecognised_code_is_not_presented_as_a_park(clean_outcomes):
    """A row whose code this build cannot name must come back null + a reason,
    and must NOT be dressed up as a park whose reason we understand. It is
    still LISTED in the snapshot (marked known:false) so the owner can release
    it - dropping it would hide state the walk is acting on."""
    _learn("mystery", "streams", ytdlp_outcomes.UNKNOWN)

    body = _read("mystery", "streams")
    assert body["learned"] is None, "an unknown code is not a park reason"
    assert body["status"] == channels.PARK_STATUS_UNRECOGNISED

    snap = _snapshot()
    assert snap["count"] == 1, "the row must still be visible so it can be released"
    row = snap["parked"][0]
    assert row["known"] is False
    assert row["permanent"] is False
    assert row["outcome_code"] == ytdlp_outcomes.UNKNOWN, "the raw code is served verbatim"


def test_a_transient_code_is_not_served_as_a_permanent_park(clean_outcomes):
    """Only PERMANENT_CODES may be presented as a park. A bot wall is IP state
    and 'Offline' resolves on its own; parking either would hide a channel that
    recovers, which is the bug the release path exists to avoid.

    Note `known` stays TRUE: this build CAN describe 'live_offline'. The two
    bits answer different questions - known (can we name it?) vs permanent (may
    it be parked?) - and collapsing them would either hide a description the app
    has or vouch for a park that must not exist."""
    _learn("livechan", "streams", ytdlp_outcomes.LIVE_OFFLINE)
    body = _read("livechan", "streams")
    assert body["learned"] is None
    assert body["status"] == channels.PARK_STATUS_UNRECOGNISED
    row = _snapshot()["parked"][0]
    assert row["permanent"] is False
    assert row["known"] is True, "live_offline IS in EXPECTED_CODES - we can name it"


def test_a_read_that_failed_is_never_reported_as_an_empty_table(clean_outcomes, monkeypatch):
    """The load-bearing honesty test for this surface.

    A read that blows up says NOTHING about what is parked. Returning
    `parked: []` would make the client render its explicit EMPTY state - telling
    the owner no channel is parked at the exact moment the app has no idea - and
    answering `not_learned` for one channel would claim it was unmeasured. Both
    are the fabricated claim this endpoint exists to avoid, so the failure is
    reported as a failure in both shapes.
    """
    def _boom(_plat):
        raise RuntimeError("the learning table is on fire")

    monkeypatch.setattr(channels, "channel_outcome_snapshot", _boom)

    snap = _snapshot()
    assert "parked" not in snap, (
        "a failed read must NOT carry an empty `parked` array - the client reads "
        "its EMPTY state from that key and would report 'nothing is parked'"
    )
    assert snap.get("error"), "the failure must be reported as a failure"

    body = _read("srdoglol", "streams")
    assert body["status"] == channels.PARK_STATUS_READ_FAILED
    assert body["learned"] is None
    assert body["status"] != channels.PARK_STATUS_NOT_LEARNED, (
        "a read that could not run has not established that this is unmeasured"
    )


def test_a_corrupt_skip_counter_does_not_break_the_whole_snapshot(clean_outcomes):
    """A counter that will not convert costs the owner one number, not the
    panel. Previously a non-numeric `skipped` raised out of the row mapper and
    turned the entire read into an error - so one bad cell could hide every
    genuinely parked channel."""
    _learn("srdoglol", "streams", ytdlp_outcomes.TAB_ABSENT)
    _learn("other", "streams", ytdlp_outcomes.CHANNEL_GONE)
    archive_db.execute(
        "UPDATE youtube_channel_outcomes SET skipped='not a number' "
        "WHERE channel_norm='other'"
    )
    rows = {r["channel"]: r for r in _snapshot()["parked"]}
    assert set(rows) == {"srdoglol", "other"}, "one corrupt cell must not hide the rest"
    assert rows["other"]["skipped"] == 0
    assert rows["srdoglol"]["skipped"] == 0


def test_an_empty_snapshot_is_a_real_answer(clean_outcomes):
    """Nothing parked, read successfully: an explicit empty list, not null and
    not an error. The client renders its own explicit empty state from this."""
    body = _snapshot()
    assert body == {"platform": "youtube", "count": 0, "parked": []}


def test_the_channel_handle_is_matched_case_and_at_insensitively(clean_outcomes):
    """The stored key is normalised (no '@', lowercase), so a request spelled
    the way a user would type it must still find the row - otherwise a park
    exists that the owner cannot even see, let alone release."""
    _learn("@SeeelBR", "streams", ytdlp_outcomes.CHANNEL_GONE)
    assert _read("@SeeelBR", "streams")["status"] == channels.PARK_STATUS_PARKED
    assert _read("seeelbr", "streams")["status"] == channels.PARK_STATUS_PARKED
    assert _read("  SEEELBR ", "STREAMS")["status"] == channels.PARK_STATUS_PARKED


def test_reading_without_a_tab_covers_every_tab(clean_outcomes):
    _learn("multi", "streams", ytdlp_outcomes.TAB_ABSENT)
    _learn("multi", "shorts", ytdlp_outcomes.CHANNEL_GONE)
    body = _read("multi")
    assert body["status"] == channels.PARK_STATUS_PARKED
    assert body["learned"]["tab"] == "shorts", "rows are ordered by (channel, tab)"


# --- 3. the release is durable and no-op-safe -------------------------------


def test_release_clears_the_park_durably_across_a_reopen(clean_outcomes):
    """A park whose release dies with the connection would hide the channel
    again on the next boot. Reopen for real - close_connections(), not a cache
    clear - so an in-process dict cannot make this pass."""
    _learn("srdoglol", "streams", ytdlp_outcomes.TAB_ABSENT)
    res = _release("srdoglol", "streams")
    assert res["released"] == 1, res
    assert res["status"] == "released"

    _reopen()

    assert _snapshot()["parked"] == [], "the park must be gone after a reopen"
    assert archive_db.channel_outcome_parked("srdoglol", "streams") is None, (
        "the walk must not skip a channel whose release did not survive"
    )


def test_a_second_release_is_a_no_op_and_reports_zero(clean_outcomes):
    """Releasing twice must be safe AND honest. The archived total for the
    channel is 1 after the first press, so returning that again would report a
    successful release of a park that was not there - the fabricated-success
    hole this endpoint exists not to have."""
    _learn("srdoglol", "streams", ytdlp_outcomes.TAB_ABSENT)

    first = _release("srdoglol", "streams")
    assert (first["released"], first["status"]) == (1, "released")

    second = _release("srdoglol", "streams")
    assert second["released"] == 0, second
    assert second["status"] == "nothing_to_release"
    assert second["status"] != "released"

    third = _release("srdoglol", "streams")
    assert (third["released"], third["status"]) == (0, "nothing_to_release")
    assert _snapshot()["parked"] == []


def test_releasing_a_channel_that_was_never_parked_is_a_safe_no_op(clean_outcomes):
    res = _release("neverexisted", "streams")
    assert res["released"] == 0
    assert res["status"] == "nothing_to_release"


def test_a_release_is_scoped_to_the_tab_unless_tab_is_omitted(clean_outcomes):
    """The park is keyed per (channel, tab), so the release must be too: a
    /shorts verdict says nothing about /streams, and blanking the whole channel
    on one tab's verdict would be a false, irreversible claim."""
    _learn("multi", "streams", ytdlp_outcomes.TAB_ABSENT)
    _learn("multi", "shorts", ytdlp_outcomes.CHANNEL_GONE)

    res = _release("multi", "streams")
    assert res["released"] == 1
    assert [r["tab"] for r in _snapshot()["parked"]] == ["shorts"], (
        "releasing one tab must leave the other tab parked"
    )

    all_tabs = _release("multi")  # tab omitted -> every tab of the channel
    assert all_tabs["released"] == 1, all_tabs
    assert _snapshot()["parked"] == []


def test_a_released_park_is_re_learned_if_the_condition_still_holds(
    ydl_seam, acquires, clean_outcomes
):
    """What the release PROMISES, pinned: ask again - not "this channel works".

    The walk is stubbed to keep failing, so this is the same channel answering
    the same way. A release must therefore be followed by a RE-LEARN, and the
    park must come back. A build that treated a release as a permanent fix
    (a delete, or a skip that ignores the re-learn) fails here, and that is
    exactly the copy the UI must not tell the owner.
    """
    ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert _snapshot()["count"] == 1

    assert _release("srdoglol", "streams")["released"] == 1
    assert _snapshot()["parked"] == []

    # The next cycle asks again (one real request) and learns the same condition.
    ydl = ydl_seam(RuntimeError(TAB_ABSENT_STREAMS))
    youtube_service.list_channel_videos_sync("@srdoglol", 5, playlist="streams",
                                            source="auto", enrich=False)
    assert len(ydl.extracted) == 1, "a release must let the walk ask the channel again"

    body = _snapshot()
    assert body["count"] == 1, (
        "the same condition must be learned again - a release clears the MEMORY, "
        "it does not repair the channel"
    )
    assert body["parked"][0]["outcome_code"] == ytdlp_outcomes.TAB_ABSENT


# --- 4. input validation ----------------------------------------------------


def test_a_read_or_release_without_a_channel_is_rejected(clean_outcomes):
    """Not a silent 200 with an empty list: an empty channel names no row, and
    returning 'nothing parked' for it would be the fabricated empty state."""
    for call in (
        lambda: _read("", "streams"),
        lambda: _read("   ", "streams"),
    ):
        with pytest.raises(HTTPException) as ei:
            call()
        assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        _release("")
    assert ei.value.status_code == 400


def test_the_routes_are_registered_on_the_channels_router():
    """The panel fetches these paths by name; a rename that leaves the frontend
    pointing at a 404 is the 'parked channel is invisible' bug all over again."""
    paths = {(r.path, tuple(sorted(r.methods))) for r in channels.router.routes}
    assert ("/api/channel/outcome-parks", ("GET",)) in paths
    assert ("/api/channel/outcome-park", ("GET",)) in paths
    assert ("/api/channel/outcome-park/release", ("POST",)) in paths
