"""The APP's YouTube channel walk under the adaptive rate governor.

The defect this pins: ``services/youtube_service.py`` had THREE
``guarded_youtube_dl_channel`` egresses and ZERO ``rate_budget`` references.
That module IS the app's channel walk — the enumerator the caption sweep uses
to decide which videos exist at all (``routers.archive._deep_enumerate`` ->
``list_channel_videos_sync``, driven every pass by ``archive_scheduler``) — so
the single most scrape-shaped request the app can make was the one path with no
learned ceiling behind it.

What is pinned here:
  1. each chokepoint acquires exactly ONE token per logical operation, on the
     SAME accounting as the other YouTube egress (archive_ytdlp), not a second
     scheme;
  2. NO DOUBLE CHARGE across the youtube_service -> archive_ytdlp boundary.
     The regression this file exists for: ``archive_ytdlp.list_channel_videos``
     already had a gate, and naively gating ``youtube_service`` too looks like
     charging one walk twice. It is not — they are two independent
     implementations with disjoint call graphs. The test proves it structurally
     and by counting tokens end to end;
  3. the wait is BOUNDED for every pathological ``wait_s`` (1e12 / inf / nan /
     negative / zero) and a USER caller never sleeps at all;
  4. a batch loop BREAKS on a dry pool instead of paying the wait once per
     remaining item (the RSS-probe union and the sweep's 3-tab walk);
  5. an ``acquire`` that RAISES is caught and never becomes a hang;
  6. a refusal is never swallowed into a plausible-looking empty listing.

No network. The seam that matters: ``list_channel_videos_sync`` and
``_make_rss_probe`` import ``guarded_youtube_dl_channel`` from INSIDE the
function, so the module attribute does not exist to patch — the patch must land
on ``services.ytdlp_guard`` or the REAL yt-dlp runs and the test hits the live
network. The governor is driven by patching ``services.rate_budget.acquire``,
which is what the delegated seam itself imports, so the whole chain including
the bounded-wait arithmetic is exercised.

Run from backend/: python -m pytest tests/test_youtube_channel_walk_governor.py
"""
from __future__ import annotations

import contextlib
import types
from pathlib import Path

import pytest

from services import archive_ytdlp, rate_budget, ytdlp_guard, youtube_service
from services.archive_ytdlp import YtGovernorExhausted

BOUND = archive_ytdlp._YTDLP_GOVERNOR_MAX_WAIT_S


class _FakeYdl:
    """Minimal stand-in for a YoutubeDL handle (flat playlist extract)."""

    def __init__(self, entries=None):
        self.entries = entries if entries is not None else [
            {"id": "VzuPKrGl0z8", "title": "a stream", "duration": 60,
             "channel_id": "UCaaa", "uploader_id": "UCaaa"},
        ]
        self.extracted: list[tuple[str, bool]] = []

    def extract_info(self, url, download=False):
        self.extracted.append((url, download))
        return {
            "id": "VzuPKrGl0z8",
            "title": "some channel",
            "channel": "some channel",
            "uploader": "some channel",
            "channel_id": "UCaaa",
            "uploader_id": "UCaaa",
            "entries": self.entries,
        }


def _ctx(ydl):
    @contextlib.contextmanager
    def _cm():
        yield ydl

    return _cm()


@pytest.fixture()
def acquires(monkeypatch):
    """Record every acquire() and return a scripted decision sequence.

    `.calls` is the list of (platform, source, kind) triples; `.script` is a
    list of Decisions consumed in order (the last one repeats once exhausted).
    Patching the MODULE the delegated seam imports is what makes the whole
    chain testable: archive_ytdlp._governor_admit_ytdlp does a function-local
    `from services.rate_budget import acquire`, so patching the module
    attribute is the only seam that reaches it.
    """
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


def _calls_named(fn, name: str) -> bool:
    """True when fn's body really CALLS `name` or imports it.

    Deliberately AST-based, not a substring search: the code under test
    documents the double-charge rule in comments that legitimately contain the
    words 'acquire' and 'archive_ytdlp.list_channel_videos', and a text scan
    would fail on its own explanation. Only real calls and real imports count.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name).split(".")[-1] == name:
                    return True
    return False


@pytest.fixture()
def no_ytdlp(monkeypatch):
    """Stub the yt-dlp context manager AT THE ONE SEAM IT IS BOUND TO.

    The trap that bit this lane once already: the guard used to be imported
    INSIDE `list_channel_videos_sync` (function-local) in youtube_service but
    at MODULE level in archive_ytdlp, so there were two bindings and a patch
    at either one left the other reaching the REAL extractor. That is a live
    network call, not a test.

    Both consumers now resolve the name through the guard MODULE, so
    `services.ytdlp_guard` is the single seam and this one patch intercepts
    every channel egress in the process. The assertions below fail loudly if
    a module re-binds the name, so a future rebind shows up as a failing test
    instead of a silent request.
    """
    ydl = _FakeYdl()
    stub = lambda opts: _ctx(ydl)  # noqa: E731
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", stub)
    # Fail loudly if a consumer re-binds the name instead of going through the
    # module: that is the exact shape of the escape this fixture exists for.
    assert ytdlp_guard.guarded_youtube_dl_channel is stub
    for mod in (archive_ytdlp, youtube_service):
        assert not hasattr(mod, "guarded_youtube_dl_channel"), (
            f"{mod.__name__} re-binds the guard name; patch "
            f"services.ytdlp_guard instead or the stub is bypassed"
        )
    return ydl


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The RSS-probe cache is module-wide; a leaked entry would hide a gate."""
    youtube_service._RSS_SHORT_PROBE_CACHE.clear()
    yield
    youtube_service._RSS_SHORT_PROBE_CACHE.clear()


# --- one token per logical operation ----------------------------------------

def test_channel_walk_takes_exactly_one_token(acquires, no_ytdlp):
    """A channel tab walk is ONE flat listing request, not one per entry."""
    rows = youtube_service.list_channel_videos_sync("@chan", 5)
    assert rows, "the stub must still be reached"
    assert acquires.calls == [("youtube", "user", "yt_channel_list")]


def test_channel_walk_defaults_to_user_scope(acquires, no_ytdlp):
    """The app-facing entry point is interactive by default: a caller that never
    heard of the governor still cannot put a user to sleep behind the pool."""
    youtube_service.list_channel_videos_sync("@chan", 5)
    assert [c[1] for c in acquires.calls] == ["user"]


def test_background_sweep_opts_into_auto(acquires, no_ytdlp):
    """Background work that CAN wait declares so explicitly."""
    youtube_service.list_channel_videos_sync("@chan", 5, source="auto")
    assert acquires.calls == [("youtube", "auto", "yt_channel_list")]


def test_channel_search_takes_exactly_one_token(acquires, no_ytdlp):
    youtube_service.search_channel_videos_sync("chan", "vale da estranheza", 5)
    assert acquires.calls == [("youtube", "user", "yt_channel_search")]


def test_rss_probe_takes_its_own_token(acquires, no_ytdlp, monkeypatch):
    """A full single-video extract is a heavier egress than the flat tab walk,
    so it is its own unit — but it is never free, and never per-entry."""
    probe = youtube_service._make_rss_probe()
    probe("VzuPKrGl0z8")
    assert [c[2] for c in acquires.calls] == ["yt_channel_rss_probe"]


def test_rss_probe_is_user_scope_by_construction(acquires, no_ytdlp):
    """The probe takes no source parameter: it only runs for an
    enrich=True /shorts listing, and the one background enumerator passes
    enrich=False, so no background caller can ever reach it. Pinning this keeps
    the no-argument signature (which several test doubles stub) honest."""
    import inspect

    sig = inspect.signature(youtube_service._make_rss_probe)
    assert list(sig.parameters) == [], "the probe must not grow a source parameter"
    youtube_service._make_rss_probe()("VzuPKrGl0z8")
    assert [c[1] for c in acquires.calls] == ["user"]


# --- THE regression: no double charge across the youtube_service boundary ----

def test_no_double_charge_across_the_youtube_service_archive_ytdlp_boundary(
    acquires, no_ytdlp, monkeypatch
):
    """The whole point of this lane.

    ``archive_ytdlp.list_channel_videos`` already had a governor gate. A
    reviewer cannot tell from the two call sites alone whether gating
    ``youtube_service`` too charges ONE walk twice. This pins the structural
    fact that makes it safe: they are two INDEPENDENT implementations, and the
    app path never reaches the already-gated one.
    """
    import inspect

    # 1. Structurally: the app module neither imports nor reaches the gated
    #    operator-script walk. AST, not text — the module's comments name it.
    assert not _calls_named(youtube_service, "list_channel_videos"), (
        "if youtube_service ever calls the already-gated archive_ytdlp walk, "
        "one logical operation would be charged twice"
    )
    assert "archive_ytdlp" not in inspect.getsource(youtube_service.list_channel_videos_sync)

    # 2. By counting: a full app-facing walk draws exactly ONE token, and it is
    #    this module's own kind — never the archive_ytdlp kind, which would
    #    mean the same egress went through both gates.
    youtube_service.list_channel_videos_sync("@chan", 5, source="auto")
    assert len(acquires.calls) == 1
    platform, _source, kind = acquires.calls[0]
    assert (platform, kind) == ("youtube", "yt_channel_list")

    # 3. The two chokepoints are genuinely separate egresses, each drawing one
    #    token — two tokens for two real requests is correct accounting, not a
    #    double charge of one.
    acquires.calls.clear()
    archive_ytdlp.list_channel_videos("https://youtube.com/@chan", limit=3)
    after_archive_walk = list(acquires.calls)
    assert after_archive_walk == [("youtube", "auto", "yt_dlp_channel_list")], (
        "the pre-existing operator-script gate is untouched by this lane"
    )


def test_app_walk_never_reaches_the_operator_script_gate(acquires, no_ytdlp, monkeypatch):
    """Belt and braces: even with the OTHER walk present, the app path must not
    trigger it — one app walk == one token, from one gate."""
    def _explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("the app channel walk reached archive_ytdlp's gated walk")

    monkeypatch.setattr(archive_ytdlp, "list_channel_videos", _explode)
    youtube_service.list_channel_videos_sync("@chan", 5, source="auto")
    assert len(acquires.calls) == 1


# --- bounded wait, guaranteed ------------------------------------------------

def test_exhausted_pool_waits_bounded_then_refuses(acquires, no_ytdlp, monkeypatch):
    """Bounded wait, then a clear failure. Never a hang."""
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    acquires.script = [_decision(False, wait_s=1_000_000.0)]

    with pytest.raises(YtGovernorExhausted) as exc:
        youtube_service.list_channel_videos_sync("@chan", 5, source="auto")

    assert slept and slept[0] <= BOUND
    assert slept[0] <= rate_budget.MAX_AUTO_WAIT_S
    msg = str(exc.value)
    assert "yt_channel_list" in msg, "must name the operation"
    assert "rate limit" in msg.lower(), "must say it is a rate/budget refusal"
    assert "ceiling=" in msg, "must carry the learned ceiling so it is attributable"


@pytest.mark.parametrize("pathological", [1e12, float("inf"), float("nan"), -5.0, 0.0])
def test_pathological_wait_always_collapses_to_the_bound(acquires, no_ytdlp, monkeypatch, pathological):
    """THE anti-hang guarantee, on the app's own chokepoint.

    Whatever the governor reports, the worst case is at most ONE bounded sleep
    and then a refusal. A 30-channel batch must not become 30 x 30s.
    """
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    acquires.calls.clear()
    acquires.script = [_decision(False, wait_s=pathological)]

    with pytest.raises(YtGovernorExhausted):
        youtube_service.list_channel_videos_sync("@chan", 5, source="auto")

    assert len(slept) <= 1, f"wait_s={pathological} produced {len(slept)} sleeps"
    for s in slept:
        assert 0.0 <= s <= BOUND, f"wait_s={pathological} slept {s}s > {BOUND}s"
    # The refused operation never reached yt-dlp: the gate is the entry point.
    assert len(acquires.calls) == 1


def test_user_scope_never_sleeps(acquires, no_ytdlp, monkeypatch):
    """On-demand work is never queued behind background work — and here it is
    doubly important: every app call site already has a 25-30s HTTP timeout, so
    a 30s pacing sleep would surface as a useless 'timed out'."""
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    # The decision echoes the coerced source, as the real acquire() does — a
    # USER pool is exhausted the moment it is spent, so there is nothing to wait
    # for even without the interactive flag.
    acquires.script = [_decision(False, wait_s=600.0, source="user")]

    with pytest.raises(YtGovernorExhausted):
        youtube_service.list_channel_videos_sync("@chan", 5)  # default scope
    assert slept == [], "a USER caller must fail fast, not sleep"


def test_interactive_flag_short_circuits_the_wait(acquires, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    acquires.script = [_decision(False, wait_s=600.0, source="auto")]
    with pytest.raises(YtGovernorExhausted):
        youtube_service._governor_admit_channel_walk("auto", "yt_channel_list", interactive=True)
    assert slept == []


def test_acquire_raising_is_caught_and_never_hangs(acquires, no_ytdlp, monkeypatch):
    """A broken governor is advisory, never a new failure mode — and never an
    unbounded wait."""
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))

    def _boom(*a, **kw):
        raise RuntimeError("governor unavailable")

    monkeypatch.setattr(rate_budget, "acquire", _boom)
    # Must return normally (ungated) rather than propagate or hang.
    rows = youtube_service.list_channel_videos_sync("@chan", 5, source="auto")
    assert rows, "a broken governor must not break the listing"
    assert slept == []


def test_missing_governor_module_is_not_a_new_failure_mode(monkeypatch, no_ytdlp):
    """An unimportable governor module must not become a new failure path."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name.startswith("services.archive_ytdlp") or name == "services.rate_budget":
            raise ImportError("no governor")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    rows = youtube_service.list_channel_videos_sync("@chan", 5, source="auto")
    assert rows


# --- the batch-break guarantee ----------------------------------------------

def test_rss_probe_union_breaks_instead_of_paying_per_candidate(acquires, no_ytdlp, monkeypatch):
    """THE 20x-stall guard.

    _union_rss_shorts probes up to _RSS_SHORT_PROBE_BUDGET candidates in a
    loop. The pool is platform-wide, so once it is dry every remaining
    candidate refuses too; paying the bounded wait per candidate turns a
    4-deep budget into a 4x stall inside one listing.
    """
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        youtube_service, "_fetch_youtube_rss_rows",
        lambda cid, content_kind="vod": [
            {"id": f"vid{i:011d}", "title": f"rss {i}", "views": "5"}
            for i in range(8)
        ],
    )
    acquires.calls.clear()
    # USER pool, as the probe always is: refuses immediately, no sleep. The
    # point of this test is the ONE admission attempt, not the wait.
    acquires.script = [_decision(False, wait_s=30.0, source="user")]

    kept = youtube_service._union_rss_shorts(
        [{"id": "existing0000", "platform": "YouTube", "title": "tab row"}],
        "UCaaa",
        youtube_service._make_rss_probe(),
    )

    # ONE admission attempt, not eight: the loop bailed on the first refusal.
    assert len(acquires.calls) == 1, "the batch must not retry per remaining candidate"
    # The tab rows already in hand are still returned — freshness is additive.
    assert [r["id"] for r in kept] == ["existing0000"]
    assert len(slept) <= 1, f"a dry pool must cost at most one bounded sleep, got {len(slept)}"


def test_probe_refusal_is_never_cached(acquires, no_ytdlp, monkeypatch):
    """A dry pool says 'not now', not 'this video is unprobeable'. Caching the
    refusal would poison the module-wide probe cache for the process lifetime."""
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.calls.clear()
    acquires.script = [_decision(False, wait_s=1.0, source="user")]

    probe = youtube_service._make_rss_probe()
    with pytest.raises(YtGovernorExhausted):
        probe("VzuPKrGl0z8")
    assert "VzuPKrGl0z8" not in youtube_service._RSS_SHORT_PROBE_CACHE, (
        "a refused probe must not be cached as a negative result"
    )


def test_sweep_enumeration_stops_the_whole_walk_on_a_dry_pool(acquires, no_ytdlp, monkeypatch):
    """The caption sweep's own enumerator: 3 tabs x N windows. A refusal must
    stop the WHOLE enumeration, not just the current tab — otherwise the next
    tab pays the bounded wait again, 3x per channel, on the scheduler's pass.
    """
    from routers import archive as archive_router

    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.calls.clear()
    acquires.script = [_decision(False, wait_s=30.0, source="auto")]

    items, truncated, total = archive_router._deep_enumerate("@chan")

    # ONE window attempted, not one per tab: the walk stopped at the first refusal.
    assert len(acquires.calls) == 1, (
        "the sweep must break out of the tab loop, not pay the wait per tab"
    )
    assert items == [] and total == 0
    assert truncated is True, "a stopped enumeration is an honest partial"


def test_sweep_windows_are_charged_once_each_when_allowed(acquires, no_ytdlp, monkeypatch):
    """No regression in the happy path: each window is still exactly one token
    and the sweep still walks every tab."""
    from routers import archive as archive_router

    ydl = _FakeYdl(entries=[
        {"id": f"v{i:011d}", "title": f"t{i}", "duration": 60} for i in range(3)
    ])
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts, **_control: _ctx(ydl))
    acquires.calls.clear()

    items, truncated, total = archive_router._deep_enumerate("@chan")

    assert [c[1] for c in acquires.calls] == ["auto"] * len(acquires.calls)
    assert {c[2] for c in acquires.calls} == {"yt_channel_list"}
    # 3 tabs, each exhausting after one non-saturated window.
    assert len(acquires.calls) == 3
    assert total == len(items)


def test_sweep_does_not_charge_twice_per_window(acquires, no_ytdlp, monkeypatch):
    """The stopgap in routers/archive.py is loop CONTROL only. It must not draw
    a token of its own — that is the exact double-charge the task warns about.
    """
    import inspect

    from routers import archive as archive_router

    assert not _calls_named(archive_router._deep_enumerate, "acquire"), (
        "_deep_enumerate must not draw its own token; the chokepoint already did"
    )
    assert "rate_budget" not in inspect.getsource(archive_router._deep_enumerate)


# --- a refusal is never a plausible-looking empty result -------------------

def test_refusal_is_not_swallowed_into_an_empty_listing(acquires, no_ytdlp, monkeypatch):
    """If the tab-walk refusal were inside the best-effort handler it would
    return [] with fetch_failed=True, get cached as a valid payload, and read to
    the caption sweep as 'this channel has no videos'."""
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.script = [_decision(False, wait_s=1.0, source="auto")]
    with pytest.raises(YtGovernorExhausted):
        youtube_service.list_channel_videos_sync("@chan", 5, source="auto")


def test_refusal_is_not_swallowed_into_empty_search_hits(acquires, no_ytdlp, monkeypatch):
    """Same for the search fallback: the router must be able to say 'rate
    limited' instead of handing the user a silent empty hit list."""
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.script = [_decision(False, wait_s=1.0)]
    with pytest.raises(YtGovernorExhausted):
        youtube_service.search_channel_videos_sync("chan", "q", 5)


def test_refusal_never_reads_as_a_bot_gate_or_a_permanent_verdict(acquires, no_ytdlp, monkeypatch):
    """The refusal text is load-bearing: yt_gate classifies bot walls and
    archive_transcribe decides 'blocked' (IRREVERSIBLE) from it."""
    from services.yt_gate import classify_youtube_gate_error
    from services.youtube_diag import is_age_gate_error, is_age_gate_job_error

    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.script = [_decision(False, wait_s=1.0, source="auto")]
    with pytest.raises(YtGovernorExhausted) as exc:
        youtube_service.list_channel_videos_sync("@chan", 5, source="auto")
    obj, msg = exc.value, str(exc.value)

    assert not classify_youtube_gate_error(obj), msg
    assert not archive_ytdlp._is_gate_error(obj), msg
    assert not is_age_gate_error(obj), msg
    assert not archive_ytdlp._is_permanent_download_error(obj), msg
    assert not is_age_gate_job_error(msg), msg
    assert "rate limit" in msg.lower()


# --- the gate is actually reached (no dead-path gate) -----------------------

def test_gate_is_reached_from_the_real_chokepoint_not_a_wrapper(acquires, no_ytdlp):
    """Guards against a gate that only a test can reach: the token must be drawn
    by the function the app actually calls, for the real listing entry point."""
    import inspect

    src = inspect.getsource(youtube_service.list_channel_videos_sync)
    assert "_governor_admit_channel_walk" in src, (
        "the app-facing entry point must carry the gate"
    )
    # ...and it must be drawn before yt-dlp is USED, not per inner request.
    # Matched on the `with` statement, not any import above it.
    assert src.index("_governor_admit_channel_walk") < src.index(
        "with ytdlp_guard.guarded_youtube_dl_channel"
    ), "the gate is the operation's entry point: it runs before the extract"


def test_production_caller_reaches_the_gate(acquires, no_ytdlp, monkeypatch):
    """The production chain, end to end, with the scheduler in the loop.

    archive_scheduler._run_pass -> _ingest_youtube -> routers.archive.
    _run_channel_caption_ingest -> _deep_enumerate -> list_channel_videos_sync.

    This is the chain that makes the gate worth anything; if it breaks, the
    scheduler quietly stops governing the app's own channel walk.
    """
    from services import archive_scheduler

    called: list[str] = []

    def _fake_ingest(handle: str, budget: int = 0, **kw):
        called.append(handle)
        from routers import archive as archive_router
        return archive_router._deep_enumerate(handle)

    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    # Drive the sweep's own enumerator, the way the scheduler does.
    monkeypatch.setattr(
        "routers.archive._run_channel_caption_ingest", _fake_ingest, raising=True
    )
    acquires.calls.clear()

    archive_scheduler._ingest_youtube({"youtubeSlug": "@chan", "platforms": ["youtube"]})

    assert called == ["@chan"], "the scheduler must reach the caption ingest"
    assert [c[2] for c in acquires.calls], (
        "the scheduler's channel walk must draw a token — the gate is live"
    )
    assert all(c[1] == "auto" for c in acquires.calls), (
        "the scheduler is background work and may pace"
    )


def test_ytdlp_guard_is_the_only_yt_dlp_seam(acquires, no_ytdlp):
    """Guards the network guarantee: this module reaches yt-dlp only through
    the guarded channel context manager, never a bare YoutubeDL."""
    import inspect

    src = inspect.getsource(youtube_service)
    assert "yt_dlp.YoutubeDL" not in src
    for call in ("_governor_admit_channel_walk",):
        assert call in src


def test_module_level_self_checks_still_hold():
    """The file's own asserts (channel_search_url / playlist url shapes) are
    load-bearing; importing the edited module must not have broken them."""
    import importlib

    importlib.reload(youtube_service)
