"""Twitch and Kick must actually reach the rate governor.

THE DEFECT THIS FILE EXISTS FOR
-------------------------------
On the live archive (H:\\VOD.RIP-data\\archive.db, read-only) ``rate_limit_events``
held 33 rows, every one of them ``youtube``. Twitch and Kick had produced ZERO
rows, ever - not because their writers were dead, but because the governor's
``note_limit`` seam is HTTP-429-only and neither platform limits that way:

  1. **Twitch** signals throttling as **HTTP 200 carrying an ``errors`` array**.
     ``twitch_gql_service`` saw it, raised ``RuntimeError``, and never reached
     ``note_limit`` - the ``e.code == 429`` branch is close to unreachable for
     Twitch's own limiting style.
  2. **Kick's** real limiter is a **Cloudflare 403**, which went only to
     ``kick_gate.note_kick_gate_event``. The governor's 429 branch there is
     nearly unreachable.

The consequence is not cosmetic: a platform the governor cannot hear is a
platform it cannot pace, so Twitch and Kick were paced only by their per-platform
gates - with no history and therefore no learned ceiling.

THE FALSE-POSITIVE RULE (the reason half this file is negative cases)
--------------------------------------------------------------------
A false positive here is WORSE than the old silence. An ``errors`` array is also
how Twitch reports a dead channel, a bad auth, a stale persisted hash or a
partial response, and a 500 or a parse error is not a rate limit at all. Any of
those lowering a ceiling would pace a platform that never limited us. So every
classifier added here is paired with a test proving the ordinary failure does
NOT write a row and does NOT move the ceiling.

THE PAYLOAD SHAPE (verified, not guessed)
-----------------------------------------
``body.get("errors")`` then ``body["errors"][0].get("message", ...)`` is what this
module's own code reads, and ``tests/test_rl_counter.py`` carries a real canned
body of that exact shape (``[{"errors": [{"message": "PersistedQueryNotFound"}]}]``).
``errors.jsonl`` holds NO Twitch rate-limit body (only transport and subscriber
errors), so the classifier keys on ``message`` ALONE and invents no field names.

No test here touches the network: ``urlopen`` and ``curl_cffi.requests.get`` are
patched per test, and every timing-sensitive assertion reads an injected clock.
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request

import pytest

from services import archive_db, kick_gate, rate_budget
from services import kick_api_service as k
from services import twitch_gql_service


class FakeClock:
    """Monotonic-by-construction clock. Nothing sleeps for real."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class _TwitchResp(io.BytesIO):
    """urlopen context-manager stand-in."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _KickResp:
    """curl_cffi response stand-in: status_code + json + raise_for_status."""

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture()
def clock():
    c = FakeClock()
    rate_budget.set_clock(c)
    rate_budget.reset()
    kick_gate.clear_kick_gate()
    yield c
    rate_budget.set_clock(time.monotonic)
    rate_budget.reset()
    # The Kick 403 path arms a module-global gate; leaving it armed would make
    # every LATER _get_json call fail fast with "frozen" and never reach the
    # network or the 403 branch at all.
    kick_gate.clear_kick_gate()


@pytest.fixture()
def db(monkeypatch, clock, tmp_path):
    """A scratch archive.db, emptied on both ends.

    Priming stays OFF so a neighbouring module's prime cannot pre-seed a ceiling
    and make a ceiling assertion pass or fail for the wrong reason.
    """
    monkeypatch.setenv("VODRIP_ARCHIVE_DB", str(tmp_path / "archive.db"))
    monkeypatch.setenv("VODRIP_RATE_BUDGET_PRIME", "0")
    archive_db._conn = None
    archive_db._schema_ready = False
    archive_db.execute("DELETE FROM rate_limit_events")
    yield
    archive_db.execute("DELETE FROM rate_limit_events")


def _rows():
    return [dict(r) for r in archive_db.query(
        "SELECT platform, kind, origin, context FROM rate_limit_events ORDER BY id"
    )]


def _ceiling(platform: str) -> float:
    return rate_budget.platform_status(platform)["ceiling_rpm"]


def _burn(clock, platform, requests, seconds=60.0, source="auto"):
    """Issue *requests* calls spread over *seconds* - i.e. requests/seconds*60 rpm."""
    step = seconds / requests
    for _ in range(requests):
        rate_budget.acquire(platform, source)
        clock.advance(step)


def _twitch_body(monkeypatch, raw: bytes) -> None:
    """Answer every Twitch GQL POST with *raw* bytes."""
    monkeypatch.setattr(
        twitch_gql_service.urllib.request, "urlopen",
        lambda req, timeout=None: _TwitchResp(raw),
    )


# --- 1. Twitch: 200-with-errors IS a rate limit -----------------------------

def test_twitch_200_with_errors_is_a_rate_limit_and_writes_a_row(db, clock, monkeypatch):
    """The signal that never reached the governor now does.

    A 200 whose errors array says we are being throttled is a platform trip.
    Pre-fix the RuntimeError was raised and the ceiling never moved, which is
    why Twitch has no history at all.
    """
    before = _ceiling("twitch")
    _burn(clock, "twitch", 20, 60.0)          # 20 rpm observed load
    _twitch_body(monkeypatch, json.dumps({
        "data": None,
        "errors": [{"message": "Too Many Requests"}],
    }).encode("utf-8"))

    # The caller still sees exactly what it saw before - a RuntimeError.
    with pytest.raises(RuntimeError, match="Too Many Requests"):
        twitch_gql_service._gql_request("query Q {}", {})

    rows = _rows()
    assert len(rows) == 1, f"a Twitch rate limit must write exactly one row, got {rows}"
    assert rows[0]["platform"] == "twitch"
    # `kind` is normalized against a CLOSED allowlist (archive_db.
    # RATE_LIMIT_KINDS) that has no class for "a rate limit reported inside a
    # 200 body", so the honest landing is "other" - the same class the
    # governor's own 3 existing live rows already carry.
    assert rows[0]["kind"] == "other"
    # The provenance the kind cannot hold still lands in the context the
    # governor formats, so the row is attributable to this seam.
    assert "ceiling_rpm=" in rows[0]["context"]
    assert _ceiling("twitch") < before, (
        "the governor must actually learn from the signal it was deaf to"
    )


def test_twitch_persisted_query_rate_limit_also_learns(db, clock, monkeypatch):
    """The persisted-query family unwraps a LIST body; the signal is identical.

    Pinned separately because the list-unwrap at the call site means a fix that
    only covered `_gql_request` would look complete and still miss half the
    GQL traffic.
    """
    before = _ceiling("twitch")
    _burn(clock, "twitch", 20, 60.0)
    _twitch_body(monkeypatch, json.dumps([
        {"data": None, "errors": [{"message": "Rate limit exceeded"}]},
    ]).encode("utf-8"))

    with pytest.raises(RuntimeError, match="Rate limit"):
        twitch_gql_service._gql_persisted("Op", "hash-a", {})

    rows = _rows()
    assert len(rows) == 1, f"expected one row, got {rows}"
    assert rows[0]["kind"] == "other"
    assert "ceiling_rpm=" in rows[0]["context"]
    assert _ceiling("twitch") < before


# --- 2. Twitch NEGATIVES: an errors array is not automatically a rate limit

@pytest.mark.parametrize("body, why", [
    ({"data": None, "errors": [{"message": "PersistedQueryNotFound"}]},
     "a stale persisted hash is a client bug, not throttling"),
    ({"data": None, "errors": [{"message": "User not found"}]},
     "a dead channel/user is not throttling"),
    ({"data": None, "errors": [{"message": "Missing client identifier"}]},
     "an auth failure is not throttling"),
    ({"data": None, "errors": [{"message": "Something went wrong"}]},
     "an opaque server-side error is not throttling"),
    ({"data": {"user": None}, "errors": [{"message": ""}]},
     "an empty message is not throttling"),
])
def test_twitch_non_rate_limit_errors_write_nothing(db, clock, monkeypatch, body, why):
    """Every ordinary `errors` payload must leave the ceiling untouched.

    Each case is a real way Twitch answers with a 200; pacing a platform on any
    of them is the failure this lane exists to avoid.
    """
    before = _ceiling("twitch")
    _burn(clock, "twitch", 20, 60.0)
    _twitch_body(monkeypatch, json.dumps(body).encode("utf-8"))

    with pytest.raises(RuntimeError):
        twitch_gql_service._gql_request("query Q {}", {})

    assert _rows() == [], f"{why}: must not write a row"
    assert _ceiling("twitch") == before, f"{why}: must not lower the ceiling"


def test_twitch_malformed_error_shapes_do_not_write_a_row(db, clock, monkeypatch):
    """An `errors` array that is not the expected shape must not write a row.

    Two shapes here reach a clean RuntimeError via the call site's own
    fallbacks. The pre-existing call site assumes ``body`` is a dict, that
    ``errors`` is a list of DICTS, and that it is non-empty - so a top-level
    list, a string ``errors``, or a bare string entry raises AttributeError or
    TypeError there. That is long-standing behaviour and out of this lane's
    scope; the classifier's own defensiveness against ALL of those shapes is
    pinned directly in `test_classifier_is_defensive_on_unreadable_shapes`.
    """
    before = _ceiling("twitch")
    for raw in (
        json.dumps({"errors": [{"no-message-key": 1}]}),
        json.dumps({"errors": [{"message": "A totally ordinary failure"}]}),
    ):
        archive_db.execute("DELETE FROM rate_limit_events")
        _twitch_body(monkeypatch, raw.encode("utf-8"))
        with pytest.raises(RuntimeError):
            twitch_gql_service._gql_request("query Q {}", {})
        assert _rows() == [], f"malformed body {raw} must not write a row"
    assert _ceiling("twitch") == before


@pytest.mark.parametrize("body", [
    None, [], "a string", 42,
    {},
    {"errors": None},
    {"errors": []},
    {"errors": "not-a-list"},
    {"errors": {"not": "a list"}},
    {"errors": ["a bare string entry"]},
    {"errors": [None, 7]},
    {"errors": [{"message": None}]},
    {"errors": [{"message": 12345}]},
    {"errors": [{"message": {"nested": "dict"}}]},
    {"errors": [{"message": ["a", "list"]}]},
    {"errors": [{"no-message-key": 1}]},
    {"errors": [{"message": "User not found"}]},
])
def test_classifier_is_defensive_on_unreadable_shapes(body):
    """Anything unreadable is False by construction, never an exception.

    A classifier that raises on a malformed payload would turn a parser bug into
    a failed request, and one that returns True on garbage would pace a platform
    that never limited us. Both are worse than the silence this lane replaces.
    """
    assert twitch_gql_service._gql_errors_indicate_rate_limit(body) is False


@pytest.mark.parametrize("message", [
    "Too Many Requests",
    "too many requests",
    "TOO MANY REQUESTS",
    "Rate limit exceeded",
    "rate-limit",
    "ratelimit",
    "You have exceeded the ratelimit for this token",
    "Your IP is temporarily blocked",
])
def test_classifier_accepts_rate_limit_phrases(message):
    """The positive half, pinned directly so the marker list cannot rot."""
    assert twitch_gql_service._gql_errors_indicate_rate_limit(
        {"data": None, "errors": [{"message": message}]}
    ) is True


def test_classifier_scans_every_error_not_just_the_first():
    """A partial-data response can carry the limit on a later entry."""
    assert twitch_gql_service._gql_errors_indicate_rate_limit({
        "data": {"user": None},
        "errors": [
            {"message": "User not found"},
            {"message": "Too Many Requests"},
        ],
    }) is True


def test_twitch_clean_200_writes_nothing(db, clock, monkeypatch):
    """The overwhelmingly common response: data, no errors, no row."""
    _burn(clock, "twitch", 20, 60.0)
    before = _ceiling("twitch")
    _twitch_body(monkeypatch, json.dumps({"data": {"ok": True}}).encode("utf-8"))

    assert twitch_gql_service._gql_request("query Q {}", {}) == {"ok": True}
    assert _rows() == [], "a successful query is not a rate-limit event"
    assert _ceiling("twitch") == before


def test_twitch_500_writes_nothing(db, clock, monkeypatch):
    """A server error is not a rate limit - and must not become one."""
    before = _ceiling("twitch")

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://gql.twitch.tv", 500, "Server Error", {}, io.BytesIO(b"boom"),
        )

    monkeypatch.setattr(twitch_gql_service.urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        twitch_gql_service._gql_request("query Q {}", {})

    assert _rows() == [], "a 500 must not write a rate-limit row"
    assert _ceiling("twitch") == before


def test_twitch_parse_error_writes_nothing(db, clock, monkeypatch):
    """A body that is not JSON at all must not become a rate limit."""
    before = _ceiling("twitch")
    _twitch_body(monkeypatch, b"<html>gateway timeout</html>")

    with pytest.raises(ValueError):
        twitch_gql_service._gql_request("query Q {}", {})

    assert _rows() == [], "a parse error must not write a rate-limit row"
    assert _ceiling("twitch") == before


# --- 3. Kick: the 403 must reach the governor -------------------------------

def test_kick_403_writes_a_governor_row(db, clock, monkeypatch):
    """A Cloudflare 403 now teaches the governor, not just the gate.

    Pre-fix this produced exactly one row (the gate's `http_403`) and the
    governor never heard about Kick at all.
    """
    before = _ceiling("kick")
    _burn(clock, "kick", 20, 60.0)
    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **kw: _KickResp(403))

    with pytest.raises(k.KickGateError, match="blocked"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")

    rows = _rows()
    # Two rows for one 403: the gate's own `http_403` and now the governor's.
    # Both are platform-trip history for kick; the point is that the governor
    # is no longer absent from the group it learns from.
    assert len([r for r in rows if r["platform"] == "kick"]) == 2, (
        f"a 403 must write the gate row AND the governor row, got {rows}"
    )
    assert all(r["platform"] == "kick" for r in rows)
    assert _ceiling("kick") < before, "the governor must learn from Kick's real limiter"


def test_kick_403_is_additive_and_preserves_the_gate(db, clock, monkeypatch):
    """The two treatments are complementary, not alternatives.

    The gate still arms and the same KickGateError still raises: this change
    alters only what the governor LEARNS, never what the caller sees.
    """
    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **kw: _KickResp(403))

    with pytest.raises(k.KickGateError, match="blocked"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")
    assert kick_gate.kick_gate_active(), "the reactive gate must still arm on a 403"

    rows = _rows()
    assert len(rows) == 2, f"gate row + governor row expected, got {rows}"
    # The gate's own history row is still written, unchanged.
    assert any(r["context"] == "403 on /api/v2/channels/xyz" for r in rows), (
        f"the gate's own row must survive, got {[r['context'] for r in rows]}"
    )
    # ...and the governor's row exists alongside it, distinguishable by the
    # context only _persist_event formats.
    assert sum("ceiling_rpm=" in r["context"] for r in rows) == 1, (
        f"exactly one governor row expected, got {[r['context'] for r in rows]}"
    )


# --- 4. Kick NEGATIVES ------------------------------------------------------

def test_kick_500_writes_no_governor_row(db, clock, monkeypatch):
    """5xx is retried and then raised; it is never a rate limit."""
    before = _ceiling("kick")
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **kw: _KickResp(500))

    with pytest.raises(RuntimeError, match="HTTP 500"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")

    assert _rows() == [], "a 500 must not write a rate-limit row"
    assert _ceiling("kick") == before


def test_kick_404_writes_nothing(db, clock, monkeypatch):
    """A missing channel is a user error, not throttling."""
    before = _ceiling("kick")
    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **kw: _KickResp(404))

    with pytest.raises(ValueError, match="not found"):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")

    assert _rows() == [], "a 404 must not write a rate-limit row"
    assert _ceiling("kick") == before


def test_kick_clean_200_writes_nothing(db, clock, monkeypatch):
    """The normal path stays silent - this is the volume case."""
    _burn(clock, "kick", 20, 60.0)
    before = _ceiling("kick")
    monkeypatch.setattr(
        "curl_cffi.requests.get", lambda *a, **kw: _KickResp(200, {"ok": True}),
    )

    assert k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips") == {"ok": True}
    assert _rows() == [], "a successful request is not a rate-limit event"
    assert _ceiling("kick") == before


def test_kick_parse_error_writes_nothing(db, clock, monkeypatch):
    """A 200 whose body will not parse must not become a rate limit."""
    before = _ceiling("kick")

    class _BadJson(_KickResp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1")

    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **kw: _BadJson(200))

    with pytest.raises(ValueError):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")

    assert _rows() == [], "a parse error must not write a rate-limit row"
    assert _ceiling("kick") == before


def test_kick_transport_error_never_reaches_note_limit(db, clock, monkeypatch):
    """A connection reset is a flake; the retry ladder owns it, not the governor."""
    before = _ceiling("kick")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def _flaky(*a, **kw):
        raise RuntimeError("curl error: Connection reset by peer")

    monkeypatch.setattr("curl_cffi.requests.get", _flaky)
    with pytest.raises(RuntimeError):
        k._get_json("/api/v2/channels/xyz", "https://kick.com/xyz/clips")

    assert _rows() == [], "a transport error must not write a rate-limit row"
    assert _ceiling("kick") == before


# --- 5. the governor's own invariants still hold on the new paths -----------

def test_repeated_twitch_rate_limits_cannot_ratchet_past_the_floor(db, clock, monkeypatch):
    """The new signal must not open a new way to run the ceiling to the floor.

    A stream of 200-with-errors trips is exactly the shape that could teach the
    history an absurdly low rate if the trip measurement were unbounded. The
    floor and the lower-only rule are what stop it.
    """
    for _ in range(12):
        _burn(clock, "twitch", 60, 60.0)
        _twitch_body(monkeypatch, json.dumps({
            "data": None, "errors": [{"message": "Too Many Requests"}],
        }).encode("utf-8"))
        with pytest.raises(RuntimeError):
            twitch_gql_service._gql_request("query Q {}", {})

    assert _ceiling("twitch") >= rate_budget._FLOOR_CEILING_RPM
    assert rate_budget.platform_status("twitch")["learning"]["events"] == 12


def test_a_rate_limit_event_can_never_raise_a_ceiling(db, clock):
    """An event is a drop, never a promotion - the governor's core asymmetry.

    Guards the property the module docstring leans on, through the seam the new
    signals now arrive on.
    """
    start = _ceiling("twitch")
    rate_budget.note_limit("twitch", kind="gql_200_ratelimit", status=429, source="auto")
    after = _ceiling("twitch")
    assert after <= start
    assert after >= rate_budget._FLOOR_CEILING_RPM
