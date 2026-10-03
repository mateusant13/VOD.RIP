"""The per-platform request counter that fills the rate-limit load columns.

`rate_limit_events` has been recording WHEN we get limited for a while,
but `recent_requests` / `in_flight` were NULL on every real row, so
`rate_limit_summary` answered the owner's actual question ("a media de
quando atingimos rate limit") with null. This module pins the counter
that fixes it and, just as importantly, the properties that make it safe
to put on the hottest paths in the app:

  1. the counter is exact under concurrent increments (12 HLS fetchers
     plus a transcribe pool hit the same bucket);
  2. buckets are bounded and reused — a 24/7 worker grows by zero bytes;
  3. NULL means "not measured" and NEVER becomes 0;
  4. the hot paths add ZERO DB writes (this is the property that lets the
     instrumentation live on the segment/comment paths at all);
  5. origin is threaded to the recorders, so Kick rows stop being
     unconditionally 'auto', while unlabelled callers still default to
     the conservative 'auto';
  6. the gate recorders turn a live count into a real
     mean/p95/observed_rate in the summary.

Every test injects the clock (services.rl_counter.set_clock) and resets
the counter, so nothing here depends on wall-clock time or on test order.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

os.environ["VODRIP_ARCHIVE_DB"] = str(
    Path(tempfile.mkdtemp(prefix="rl-counter-")) / "archive.db")

import pytest  # noqa: E402

from services import archive_db, kick_api_service, kick_gate, rl_counter, yt_gate  # noqa: E402
from services import archive_twitch, twitch_gql_service, ytdlp_hls  # noqa: E402


# --- fixtures --------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _scratch_db():
    """Pin the shared archive connection to THIS module's scratch DB."""
    prev = os.environ.get("VODRIP_ARCHIVE_DB")
    os.environ["VODRIP_ARCHIVE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="rl-counter-")) / "archive.db")
    archive_db._conn = None
    archive_db._schema_ready = False
    yield
    if prev is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = prev
    archive_db._conn = None
    archive_db._schema_ready = False


@pytest.fixture(scope="module", autouse=True)
def _no_governor_pacing():
    """Drop the governor's real-time wait to zero for this module only.

    The counter paths this module exercises (200 comment pages, 240 HLS
    segments) are ALSO governed on main: archive_twitch paces every
    background comment page against the learned Twitch budget and can sleep
    up to rate_budget.MAX_AUTO_WAIT_S per call. That pacing is correct
    production behaviour and is left completely alone — but none of these
    tests assert on pacing, they assert on counts and on zero DB writes, so
    paying real wall-clock for it would turn a fast hermetic module into a
    multi-hour one.

    Only the wait is neutralised: _governor_admit() still calls acquire()
    and still does its accounting, so the budget is exercised exactly as it
    is in production and only the time.sleep() is skipped. Patching the
    consumer modules' own copies of the constant is required because both
    import it by value (``from services.rate_budget import ...``).
    """
    touched = (archive_twitch, kick_api_service, ytdlp_hls)
    saved = [(m, getattr(m, "MAX_AUTO_WAIT_S", None)) for m in touched]
    for m in touched:
        if hasattr(m, "MAX_AUTO_WAIT_S"):
            m.MAX_AUTO_WAIT_S = 0.0
    yield
    for m, val in saved:
        if val is not None:
            m.MAX_AUTO_WAIT_S = val


@pytest.fixture(autouse=True)
def _clean():
    """Empty the counter, the event table and the gates around every test.

    The counter is process-global by design (that is what makes it cheap),
    so a leak here would poison a later test's counts — and the sibling
    module's "unmeasured rows stay NULL" test. Reset both ways.
    """
    rl_counter.reset()
    archive_db.execute("DELETE FROM rate_limit_events")
    yt_gate.clear_youtube_gate()
    kick_gate.clear_kick_gate()
    yield
    try:
        archive_db.execute("DELETE FROM rate_limit_events")
    except Exception:
        pass
    rl_counter.reset()
    yt_gate.clear_youtube_gate()
    kick_gate.clear_kick_gate()


class FakeClock:
    """Injected monotonic clock. No sleeps, no wall-clock, fully ordered."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


@pytest.fixture
def clock():
    c = FakeClock()
    rl_counter.set_clock(c)
    yield c
    rl_counter.reset_clock()


def _rows() -> list[dict]:
    return archive_db.recent_rate_limits(since_hours=24 * 30, limit=2000)


# --- 1. NULL means "not measured", never 0 ---------------------------------

def test_untouched_platform_reports_none_not_zero():
    """The whole NULL contract: a platform we never talked to has no load
    to report. Coercing to 0 would read as a clean window and poison the
    mean/p95 in the summary."""
    assert rl_counter.recent_requests("kick") is None
    assert rl_counter.in_flight("kick") is None
    assert rl_counter.total_requests("kick") is None
    assert rl_counter.peek("kick") is None


def test_touched_platform_reports_a_measured_zero():
    """...and a platform we DID talk to reports 0 when the window is
    empty. That is a real measurement, not a fabrication."""
    rl_counter.count_request("kick")
    clock_advance = None  # noqa: F841 - readability: the window has not moved
    assert rl_counter.recent_requests("kick") == 1
    c = FakeClock(start=10_000.0 + rl_counter.WINDOW_SEC * 3)
    rl_counter.set_clock(c)
    assert rl_counter.recent_requests("kick") == 0, (
        "a platform that went quiet must report an empty window, not its "
        "stale count"
    )


def test_unmeasured_gate_row_keeps_null(clock):
    """A gate firing in a process that never counted that platform leaves
    the columns NULL (and the summary's stats NULL), not 0."""
    yt_gate.note_youtube_gate("Sign in to confirm you're not a bot", freeze_sec=120)
    row = _rows()[0]
    assert row["recent_requests"] is None
    assert row["in_flight"] is None
    group = archive_db.rate_limit_summary()["groups"][0]
    assert group["requests_at_limit_mean"] is None
    assert group["requests_at_limit_p95"] is None
    assert group["observed_rate_per_min"] is None


# --- 2. the window / bucket maths ------------------------------------------

def test_window_excludes_buckets_older_than_it(clock):
    """Only the trailing WINDOW_SEC counts; older traffic must age out.

    Four bursts of four requests, one every 30s. At the end only the
    newest burst is inside the 60s window; 30s later the window is empty.
    """
    for _ in range(4):
        for _ in range(4):
            rl_counter.count_request("twitch")
        clock.advance(rl_counter.WINDOW_SEC / 2)
    assert rl_counter.recent_requests("twitch") == 4, (
        "three bursts ago (90s) must have aged out of the 60s window"
    )
    clock.advance(rl_counter.WINDOW_SEC)
    assert rl_counter.recent_requests("twitch") == 0, (
        "a measured, empty window is 0 - unlike an unmeasured platform, "
        "which is None"
    )


def test_bucket_rollover_resets_the_live_count(clock):
    """A new bucket starts at 0 rather than continuing the previous total —
    otherwise a platform would appear to accelerate every 10 s."""
    rl_counter.count_request("youtube")
    assert rl_counter.recent_requests("youtube") == 1
    clock.advance(rl_counter.BUCKET_SEC)
    rl_counter.count_request("youtube")
    # Trailing window still sees both, but the newest bucket holds one.
    assert rl_counter.recent_requests("youtube") == 2
    assert rl_counter.peek("youtube") is not None


def test_registry_is_bounded_to_the_declared_platforms(clock):
    """Memory bound part 1: unknown labels can only ever land on an
    existing bucket, so a caller passing garbage cannot grow the map."""
    for weird in ("", "tiktok", "X_Twitter", "twitch ", None, 12345, "Kick"):
        rl_counter.count_request(weird)
    live = {k for k in rl_counter.snapshot()}
    assert live <= set(rl_counter.PLATFORMS), live
    assert rl_counter.normalize_platform("Kick") == "kick"
    assert rl_counter.normalize_platform("YouTube") == "youtube"
    assert rl_counter.normalize_platform("tiktok") == "other"


def test_bucket_ring_is_fixed_size_and_reused(clock):
    """Memory bound part 2: a fixed ring per platform, reused forever.

    A week of counting must allocate nothing new — this walks far more
    windows than the ring has slots and pins the object count.
    """
    counters = rl_counter._counters_for("twitch")
    ring = counters._buckets
    assert len(ring) == rl_counter.WINDOW_BUCKETS
    ids_before = {id(b) for b in ring}
    # 20x more windows than the ring has slots.
    for _ in range(rl_counter.WINDOW_BUCKETS * 20):
        rl_counter.count_request("twitch")
        clock.advance(rl_counter.BUCKET_SEC)
    assert {id(b) for b in counters._buckets} == ids_before, (
        "the ring must be reused in place, not reallocated"
    )
    assert len(counters._buckets) == rl_counter.WINDOW_BUCKETS
    # The lifetime count is still exactly right after all that churn, and
    # the reported window stays bounded by the ring no matter how much
    # lifetime traffic passed through it (this is what keeps a 24/7
    # worker's number from creeping upward over days).
    assert counters._total == rl_counter.WINDOW_BUCKETS * 20
    assert 0 < counters.recent() <= rl_counter.WINDOW_BUCKETS


# --- 3. concurrency --------------------------------------------------------

def test_concurrent_increments_are_exact(clock):
    """The 12-fetcher HLS pool plus a transcribe pool land on one bucket.

    An unsynchronised `bucket.count += 1` loses increments here; the
    per-platform lock is what makes the count exact, so assert EXACTLY.
    """
    threads, per_thread = 12, 400
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()  # maximise real contention
        for _ in range(per_thread):
            rl_counter.count_request("kick")

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    assert rl_counter.total_requests("kick") == threads * per_thread
    assert rl_counter.recent_requests("kick") == threads * per_thread


def test_in_flight_gauge_is_balanced_under_threads(clock):
    """in_flight must be exactly the number of scopes currently open, and
    return to 0 afterwards.

    Deterministic by construction: a barrier makes all 8 holders sit
    inside their scope at the same instant, so the expected value is 8
    and not 'probably more than one'.
    """
    holders = 8
    all_inside = threading.Barrier(holders + 1)  # +1 = this thread
    release = threading.Event()

    def holder() -> None:
        with rl_counter.request_scope("hls"):
            all_inside.wait()      # every holder is now in flight at once
            release.wait(10)

    threads = [threading.Thread(target=holder) for _ in range(holders)]
    for t in threads:
        t.start()
    all_inside.wait()  # returns only once all 8 are inside their scopes
    try:
        # A holder cannot leave its scope until release is set, so the
        # gauge is exactly 8 here - no polling, no sleep, no flake.
        assert rl_counter.in_flight("hls") == holders
    finally:
        release.set()
        for t in threads:
            t.join(10)

    assert rl_counter.in_flight("hls") == 0, "every scope must decrement"
    assert rl_counter.total_requests("hls") == holders, (
        "a scope is exactly one counted request"
    )


def test_request_scope_never_swallows(clock):
    """A scope that sees an exception must still decrement, and must not
    swallow the error."""
    with pytest.raises(ValueError):
        with rl_counter.request_scope("kick"):
            raise ValueError("boom")
    assert rl_counter.in_flight("kick") == 0
    assert rl_counter.total_requests("kick") == 1, (
        "the attempt still happened - a failed request is still a request"
    )


# --- 4. the hot paths add NO db writes -------------------------------------

class _WriteSpy:
    """Counts every archive_db write (execute) while a burst runs."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self._orig = archive_db.execute

    def __enter__(self) -> "_WriteSpy":
        import services.archive_db as adb

        def spy(sql, *a, **k):
            self.writes.append(str(sql).strip().split()[0].upper())
            return self._orig(sql, *a, **k)

        adb.execute = spy
        return self

    def __exit__(self, *exc: object) -> bool:
        import services.archive_db as adb

        adb.execute = self._orig
        return False


def test_segment_path_adds_no_db_writes(monkeypatch, tmp_path, clock):
    """THE cost constraint: the segment path is ~2,400 calls per 4h VOD
    across 12 threads, so a single INSERT there would be a disaster
    (archive_db.execute takes a process-global write lock).

    Drive 240 real segment fetches through the actual
    _download_segments pool and assert ZERO DB writes, while the counter
    still sees every single request.
    """
    fetched: list[str] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_content(self, n):
            return iter([b"x" * 2048])

        def close(self):
            return None

    def fake_get(url, **kw):
        fetched.append(url)
        return _Resp()

    monkeypatch.setattr(ytdlp_hls.requests, "get", fake_get)
    segments = [{"url": f"https://cdn.example/seg{i}.ts", "duration": 2.0}
                for i in range(240)]

    with _WriteSpy() as spy:
        files = ytdlp_hls._download_segments(
            segments, {}, str(tmp_path), platform="youtube", origin="auto"
        )

    assert len(files) == 240
    assert len(fetched) == 240
    assert spy.writes == [], (
        "the segment path must not write to the DB per request; got %r"
        % (spy.writes[:5],)
    )
    assert rl_counter.total_requests("youtube") == 240, (
        "and yet the count is exact: instrumentation without persistence"
    )
    assert rl_counter.in_flight("youtube") == 0


def test_comment_page_path_adds_no_db_writes(monkeypatch, clock):
    """Same property for the highest-volume rate-limitable call: 200 chat
    pages, zero DB writes, exact count."""
    import io as _io
    import urllib.request

    body = _io.BytesIO(
        b'{"data": {"video": {"comments": {"edges": []}}}}'
    )

    class _Resp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    def fake_urlopen(req, timeout=None):
        return _Resp(body.getvalue())

    monkeypatch.setattr(archive_twitch.urllib.request, "urlopen", fake_urlopen)

    with _WriteSpy() as spy:
        for i in range(200):
            archive_twitch._post_comments_page("12345", i * 60, 100)

    assert spy.writes == [], (
        "the comment-page path must not write to the DB per request; got %r"
        % (spy.writes[:5],)
    )
    assert rl_counter.total_requests("twitch") == 200
    assert rl_counter.in_flight("twitch") == 0


def test_twitch_gql_both_families_are_counted(monkeypatch, clock):
    """Both GQL request families funnel through the same counter, and a
    persisted-hash fallback counts twice (the platform saw two requests)."""
    import io as _io

    class _Resp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    calls = {"persisted": 0}

    def fake_urlopen(req, timeout=None):
        # The persisted-query family unwraps a list body, so a hash miss
        # comes back as a one-element list; the plain family wants a dict.
        if b'"extensions"' in (req.data or b""):
            calls["persisted"] += 1
            if calls["persisted"] == 1:
                return _Resp(
                    b'[{"errors": [{"message": "PersistedQueryNotFound"}]}]'
                )
        return _Resp(b'{"data": {"ok": true}}')

    monkeypatch.setattr(twitch_gql_service.urllib.request, "urlopen", fake_urlopen)

    twitch_gql_service._gql_request("query Q {}", {})
    assert rl_counter.total_requests("twitch") == 1

    twitch_gql_service._gql_persisted_with_fallback("Op", "hash-a", "hash-b", {})
    assert rl_counter.total_requests("twitch") == 3, (
        "the fallback is a second real request and must be counted"
    )


# --- 5. the gate recorders feed the live count -----------------------------

def test_youtube_gate_row_carries_the_live_count(clock):
    """A bot-gate event in a process that HAS been hitting YouTube reports
    the real load — the number the owner asked for."""
    for _ in range(42):
        with rl_counter.request_scope("youtube"):
            pass
    yt_gate.note_youtube_gate(
        "Sign in to confirm you're not a bot", freeze_sec=120, surface="metadata",
    )
    row = _rows()[0]
    assert row["recent_requests"] == 42
    assert row["in_flight"] == 0
    assert row["origin"] == "auto"


def test_kick_gate_row_carries_the_live_count(clock):
    for _ in range(17):
        rl_counter.count_request("kick")
    kick_gate.note_kick_gate_event("403 on /api/v2/channels/x", kind="http_403")
    row = _rows()[0]
    assert row["recent_requests"] == 17
    assert row["in_flight"] == 0


def test_summary_reports_a_real_rate_for_live_data(clock):
    """The end-to-end answer: bursts of real requests, a gate event after
    each, and a summary whose mean/p95/observed_rate are all non-null.

    freeze_sec grows per arm because yt_gate is longest-wins: an arm that
    does not extend the freeze is a no-op by design (that is the gate's
    own semantics, untouched here), so the test must not re-arm inside a
    single monotonic tick.
    """
    for i, burst in enumerate((10, 20, 30, 100)):
        for _ in range(burst):
            rl_counter.count_request("youtube")
        yt_gate.note_youtube_gate(
            f"Sign in to confirm you're not a bot (after {burst})",
            freeze_sec=60 * (i + 1),
        )
        clock.advance(rl_counter.WINDOW_SEC + rl_counter.BUCKET_SEC)

    summary = archive_db.rate_limit_summary(since_hours=24)
    yt = next(g for g in summary["groups"] if g["platform"] == "youtube")
    assert yt["count"] == 4
    assert yt["requests_at_limit_mean"] == 40.0, "mean of 10/20/30/100"
    assert yt["requests_at_limit_p95"] == 100.0
    assert yt["observed_rate_per_min"] == 40.0, (
        "recent_requests is a trailing-60s count, so its mean IS req/min"
    )


def test_observed_rate_is_not_divided_by_the_event_gap(clock):
    """The unit bug this lane fixes.

    The sibling computed the observed rate as
    `recent_requests / (gap_between_events / 60)`, treating the column as
    a cumulative counter. Two limits 5 minutes apart, both at exactly 40
    requests in the trailing minute, must report 40 req/min; dividing by
    the gap would have reported 8.
    """
    for _ in range(40):
        rl_counter.count_request("kick")
    kick_gate.note_kick_gate_event("429 rate-limited", kind="http_429")
    clock.advance(300.0)          # 5 minutes between the two limits
    for _ in range(40):
        rl_counter.count_request("kick")
    kick_gate.note_kick_gate_event("429 rate-limited again", kind="http_429")

    kick = next(g for g in archive_db.rate_limit_summary()["groups"]
                if g["platform"] == "kick")
    assert kick["count"] == 2
    assert kick["requests_at_limit_mean"] == 40.0
    assert kick["observed_rate_per_min"] == 40.0, (
        "a 5-minute gap must not deflate the rate to 8 req/min"
    )


# --- 6. origin threading ---------------------------------------------------

def test_kick_egress_is_counted_and_defaults_to_auto(monkeypatch, clock):
    """An unlabelled Kick caller still COUNTS, and is still labelled 'auto'
    (the conservative default). The transport is stubbed - curl_cffi is
    imported lazily inside _get_json."""
    class _Resp:
        status_code = 200

        def json(self):
            return {"clips": []}

    import sys
    fake_mod = type(sys)("curl_cffi_fake")
    fake_requests = type(sys)("curl_cffi_requests_fake")
    fake_requests.get = lambda *a, **k: _Resp()
    fake_mod.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_mod)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    kick_api_service._get_json("/api/v2/channels/x/clips", "https://kick.com/x/clips")
    assert rl_counter.total_requests("kick") == 1

    kick_gate.note_kick_gate_event("403 on /api/v2/channels/x", kind="http_403")
    assert _rows()[0]["origin"] == "auto"


def test_kick_origin_is_threaded_where_it_is_knowable(clock):
    """The on-demand preview resolves a Kick stream while the user waits,
    so that row must be 'user' — not 'auto'."""
    kick_gate.note_kick_gate_event(
        "403 on /api/v1/video/x", kind="http_403", surface="metadata", origin="user"
    )
    assert _rows()[0]["origin"] == "user"
    # ...and the summary separates the two lanes, which is the split an
    # adaptive throttle would act on.
    archive_db.execute("DELETE FROM rate_limit_events")
    for _ in range(5):
        rl_counter.count_request("kick")
    kick_gate.note_kick_gate_event("403", kind="http_403", origin="auto")
    kick_gate.note_kick_gate_event("403", kind="http_403", origin="user")
    groups = {g["origin"]: g for g in archive_db.rate_limit_summary()["groups"]}
    assert set(groups) == {"auto", "user"}


def test_kick_public_api_accepts_origin(monkeypatch, clock):
    """The origin parameter is threaded all the way from the public Kick
    helpers down to the gate event, not just accepted and dropped."""
    seen: dict = {}

    def fake_get_json(path, referer, *, timeout=15.0, origin="auto"):
        seen["path"] = path
        seen["origin"] = origin
        kick_gate.note_kick_gate_event(
            f"403 on {path}", kind="http_403", surface="metadata", origin=origin
        )
        raise kick_api_service.KickGateError("blocked")

    monkeypatch.setattr(kick_api_service, "_get_json", fake_get_json)

    with pytest.raises(kick_api_service.KickGateError):
        kick_api_service.get_channel_api("https://kick.com/someone", origin="user")
    assert seen["origin"] == "user"
    assert _rows()[0]["origin"] == "user"

    with pytest.raises(kick_api_service.KickGateError):
        kick_api_service.get_channel_api("https://kick.com/someone")
    assert seen["origin"] == "auto", "an unlabelled caller keeps the safe default"


def test_twitch_comment_page_lane_flag_is_reachable_for_a_future_recorder(clock):
    """Deliberate scope decision, pinned so it is not lost.

    _post_comments_page takes NO origin parameter: nothing on the chat
    path records a rate_limit_events row yet, so an origin argument would
    label nothing. The lane flag that decides it (`interactive`, computed
    as job_id is None in backfill_chat) is one line above at the
    _fetch_page_with_backoff call site — a future Twitch recorder threads
    it in from there. This test pins that the counting still happens on
    the interactive lane and that the seam is where we said it is.
    """
    import inspect

    src = inspect.getsource(archive_twitch._post_comments_page)
    assert "rl_counter.request_scope" in src, "the chat page must be counted"
    assert "origin" not in inspect.signature(
        archive_twitch._post_comments_page
    ).parameters, "no origin param until there is a recorder to label"

    backoff_src = inspect.getsource(archive_twitch._fetch_page_with_backoff)
    # Normalised to one line: the governor lane reflowed this call across
    # several lines, so the old single-line literal no longer matches the
    # source text even though the seam is exactly where it was. What is
    # pinned here is the seam itself and that the lane flag is actually
    # THREADED into the call, not merely accepted as a parameter.
    flat = " ".join(backoff_src.split())
    assert "_post_comments_page(" in flat, "the seam is still this call site"
    assert "video_id, int(last_seen), page_size, interactive=interactive" in flat, (
        "the lane flag must reach the page fetch from here: %r" % (flat,)
    )
    params = inspect.signature(archive_twitch._fetch_page_with_backoff).parameters
    assert "interactive" in params, (
        "the lane flag is the seam a future recorder uses: %r" % (list(params),)
    )
    assert params["interactive"].default is False


# --- 7. the counter is a leaf (no import cycle) ----------------------------

def test_counter_module_is_a_stdlib_leaf():
    """archive_transcribe deliberately avoids importing deps at module
    scope and test_circular_import guards it; the counter must be
    importable from every chokepoint without pulling anything in."""
    import ast
    from pathlib import Path as _Path

    src = _Path(rl_counter.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert not (imported & {"services"}), (
        "rl_counter must not import services.* at module scope: %r" % (imported,)
    )
    assert imported <= {"__future__", "threading", "time", "typing"}, imported


def test_reset_and_clock_injection_are_test_only():
    """`reset()` and `set_clock()` are a test affordance, not API.

    reset() clears the registry, so a worker that had already fetched a
    platform's counters would keep incrementing a detached object and its
    counts would vanish from the next snapshot. That is harmless while
    only tests call it — and only tests may. Grep the tree and pin it,
    because "tests only" in a docstring is not a guarantee.
    """
    import re
    from pathlib import Path as _Path

    backend = _Path(rl_counter.__file__).resolve().parent.parent
    pattern = re.compile(r"rl_counter\.(reset|set_clock|reset_clock)\s*\(")
    this_file = _Path(__file__).resolve()
    foreign = []
    for path in backend.rglob("*.py"):
        if path.resolve() == this_file:
            continue
        if path.name == _Path(rl_counter.__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                foreign.append(
                    "%s:%d" % (path.relative_to(backend).as_posix(), lineno)
                )
    assert foreign == [], (
        "reset()/set_clock() are a TEST affordance - a production caller "
        "would drop counts into a detached counter. Offending: %r" % (foreign,)
    )
