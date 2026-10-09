"""Lane B — preview ASR worker: bounded, cancellable, concurrent, honest about silence.

Every test here can FAIL. Each one carries a ``MUTATION:`` line naming the
single edit that turns it red, because a test that cannot fail is decoration.
The four properties asserted are the four that were broken or absent in the
code this lane inherited:

  1. a silent model is ``stalled``, never ``ok``;
  2. cancellation really stops work (counted, not assumed);
  3. three requests at once genuinely OVERLAP (concurrency asserted, not just
     completion);
  4. the head bound is real — a whole-VOD request is clamped and the clamp is
     passed down to the fetcher.

The ASR backend is faked, and ``soundfile`` is faked, so these run on a box
with no CUDA and no sherpa-onnx. That is deliberate: a test that skips
because the model would not load is a test that never goes red.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeBackend:
    """Stands in for ``services.archive_transcribe``. Records every call."""

    def __init__(self, *, segments=None, fetch_s=0.0, peak=0.5):
        self.calls: list[dict] = []
        self._segments = segments or self._default_segments
        self._fetch_s = fetch_s
        self.peak = peak
        self.lock = threading.Lock()
        self.live = 0          # concurrent entries into the "native" part
        self.peak_live = 0
        self.segment_calls = 0

    # -- native surface -------------------------------------------------
    def fetch(self, platform, video_id, channel, out_wav, seconds, *, timeout_s=120.0):
        with self.lock:
            self.calls.append({"op": "fetch", "platform": platform, "video_id": video_id,
                               "channel": channel, "seconds": seconds, "timeout_s": timeout_s})
            self.live += 1
            self.peak_live = max(self.peak_live, self.live)
        try:
            if self._fetch_s:
                time.sleep(self._fetch_s)
            out_wav.write_bytes(b"RIFF")   # never parsed by the fake sf
            return seconds
        finally:
            with self.lock:
                self.live -= 1

    def _default_segments(self, audio, sr, *, max_sec=None):
        with self.lock:
            self.segment_calls += 1
        return [{"start_sec": 0.0, "end_sec": 1.0,
                 "text": "hello from chunk %d" % self.segment_calls,
                 "words": [{"w": "hello", "s": 0.0}]}]

    def segments(self, audio, sr, *, max_sec=None):
        # The DECODE is the stage the owner's "3 channels at once" ask actually
        # contends on: three requests are "live" exactly while they are inside
        # the model. `live`/`peak_live` used to be tracked only in fetch(), whose
        # dwell is 0.0s by default, so any assertion about overlap read a
        # thread-scheduling race rather than the product's behaviour.
        with self.lock:
            self.live += 1
            self.peak_live = max(self.peak_live, self.live)
        try:
            return self._segments(audio, sr, max_sec=max_sec)
        finally:
            with self.lock:
                self.live -= 1

    def permanent(self, exc):
        return isinstance(exc, PermanentBoom)

    def model(self):
        return object()

    def device(self):
        return ("cuda", "int8")

    def cuda_ok(self):
        return True


class PermanentBoom(Exception):
    pass


class TransientBoom(Exception):
    pass


@pytest.fixture(autouse=True)
def fake_soundfile(monkeypatch):
    """A soundfile that yields a numpy array of the requested length."""
    import numpy as np

    holder = {"sec": 60.0, "peak": 0.5}

    def read(path, dtype=None, always_2d=False):
        n = int(holder["sec"] * 16000)
        return np.full(n, holder["peak"], dtype=np.float32), 16000

    mod = types.ModuleType("soundfile")
    mod.read = read
    monkeypatch.setitem(sys.modules, "soundfile", mod)
    return holder


@pytest.fixture
def worker(monkeypatch, fake_soundfile):
    import services.preview_asr_worker as w

    importlib.reload(w)
    be = FakeBackend(peak=fake_soundfile["peak"])
    monkeypatch.setattr(w, "_backend_factory", lambda: be)
    w._inflight.clear()
    return w, be


def _kill_inflight(w):
    for ev in list(w._inflight.values()):
        ev.set()


# ==========================================================================
# 1. SILENCE IS NAMED AS SILENCE
# ==========================================================================
def test_stalled_model_is_not_reported_as_ok(worker):
    """A model that never yields a row is ``stalled`` — never ``ok``.

    SCOPE, stated honestly: this is the COOPERATIVE ceiling — the worker sees
    it at a chunk boundary. It CANNOT interrupt a chunk already blocked inside
    native decode; nothing in Python can. The AUTHORITATIVE ceiling is the
    supervisor's, which cancels and escalates over the pipe, and it is proved
    by ``test_supervisor_fires_first_output_ceiling_on_wedged_child`` below.

    MUTATION: in ``_asr``, change the ``outcome="stalled"`` branch inside the
    decode loop to ``outcome="ok"`` (or raise ``FIRST_OUTPUT_DEFAULT_S`` to a
    value larger than ``total_s`` so the ceiling can never fire first).
    """
    w, be = worker

    def never_speaks(audio, sr, *, max_sec=None):
        time.sleep(0.08)         # slow, but returns: a boundary is reachable
        return []                # ...and never produces a row

    be._segments = never_speaks
    t0 = time.monotonic()
    out = w._asr("twitch", "1", "xqc", 60.0, chunk_s=5.0,
                 first_output_s=0.3, total_s=20.0)
    took = time.monotonic() - t0

    assert out["ok"] is False, "a hung model must not be reported ok"
    assert out["outcome"] == "stalled", out
    assert out["rows"] == []
    assert out["first_row_s"] is None
    assert took < 5.0, "stall ceiling must fire on its own clock, took %.1fs" % took


def test_silence_is_not_collapsed_into_slow(worker):
    """``stalled`` and ``slow`` are different strings; the stall must not leak.

    MUTATION: return ``"slow"`` from the stalled branch, or drop ``outcome``
    from the reply entirely so the caller sees ``None``.
    """
    w, be = worker
    be._segments = lambda a, sr, *, max_sec=None: (time.sleep(0.08), [])[1]
    out = w._asr("twitch", "1", "xqc", 60.0, chunk_s=5.0,
                 first_output_s=0.3, total_s=20.0)
    assert out["outcome"] != "slow", "silence reported as slowness"
    assert out["outcome"] == "stalled"


def test_quiet_audio_is_silent_not_stalled(worker, fake_soundfile):
    """Zero rows + genuinely mute audio is a SETTLED FACT (``silent``).

    This is the discriminator that makes ``stalled`` mean something: if quiet
    audio were also called stalled, "stalled" would just mean "no rows".

    MUTATION: set ``_SILENCE_PEAK = 0.0`` so mute audio stops counting as
    quiet and the outcome becomes ``stalled``.
    """
    w, be = worker
    fake_soundfile["sec"] = 30.0
    fake_soundfile["peak"] = 0.0          # digital silence
    be._segments = lambda a, sr, *, max_sec=None: []
    out = w._asr("twitch", "1", "xqc", 30.0, chunk_s=30.0,
                 first_output_s=5.0, total_s=20.0)
    assert out["outcome"] == "silent", out
    assert out["ok"] is False


def test_slow_is_reported_with_partial_rows(worker, fake_soundfile):
    """Rows in hand + budget spent = ``slow`` WITH the rows it did get.

    MUTATION: return ``ok=False`` in the post-decode ``slow`` branch, or
    overwrite ``out["rows"] = rows`` with ``[]``.
    """
    w, be = worker
    fake_soundfile["sec"] = 60.0

    def slow_sections(audio, sr, *, max_sec=None):
        with be.lock:
            be.segment_calls += 1
        time.sleep(0.25)        # each 30 s chunk costs 250 ms
        return [{"start_sec": 0.0, "end_sec": 1.0, "text": "row %d" % be.segment_calls}]

    be._segments = slow_sections
    out = w._asr("twitch", "1", "xqc", 60.0, chunk_s=30.0,
                 first_output_s=30.0, total_s=0.5)
    assert out["outcome"] == "slow", out
    assert out.get("partial") is True
    assert len(out["rows"]) >= 1, "partial rows must survive a slow finish"


# ==========================================================================
# 2. CANCELLATION IS REAL
# ==========================================================================
def test_cancel_event_stops_decode_work(worker, fake_soundfile):
    """Setting the cancel event ends the work; the counter PROVES it.

    MUTATION: delete the ``_cancel_check("decode")`` call at the top of the
    decode loop, or drop the ``cancel=cancel`` argument from the ``_asr``
    call — the counter then keeps climbing past the cancel.
    """
    w, be = worker
    fake_soundfile["sec"] = 600.0          # 20 chunks' worth
    ev = threading.Event()

    def counted(audio, sr, *, max_sec=None):
        with be.lock:
            be.segment_calls += 1
            n = be.segment_calls
        time.sleep(0.05)
        return [{"start_sec": 0.0, "end_sec": 1.0, "text": "row %d" % n}]

    be._segments = counted
    out_box = {}

    def run():
        out_box["out"] = w._asr("twitch", "1", "xqc", 600.0, chunk_s=30.0,
                                first_output_s=30.0, total_s=60.0, cancel=ev)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.2)                 # let a few chunks land
    ev.set()                        # the caller gives up
    th.join(timeout=10)

    assert not th.is_alive(), "cancelled decode never returned — that is a wedge"
    at_cancel = be.segment_calls
    assert out_box["out"]["outcome"] == "cancelled", out_box["out"]
    assert out_box["out"]["ok"] is False
    time.sleep(0.3)                 # a mutant would keep decoding here
    assert be.segment_calls == at_cancel, (
        "decode kept running after cancel: %d -> %d" % (at_cancel, be.segment_calls))


def test_cancel_before_start_never_touches_the_backend(worker):
    """A pre-cancelled request must not spend a single fetch.

    MUTATION: move ``_cancel_check("pre_fetch")`` to AFTER the fetch, or
    remove it entirely.
    """
    w, be = worker
    ev = threading.Event()
    ev.set()
    out = w._asr("twitch", "1", "xqc", 60.0, chunk_s=30.0,
                 first_output_s=5.0, total_s=20.0, cancel=ev)
    assert out["outcome"] == "cancelled"
    assert out["stage"] == "pre_fetch"
    assert be.calls == [], "a cancelled request still fetched audio"


def test_cancel_acks_the_supervisor(worker):
    """``cancel`` is acknowledged with ``delivered`` — the escalation signal.

    MUTATION: return ``delivered: False`` unconditionally in the cancel
    branch of ``_handle``; the supervisor would then kill a child that had
    already stopped, and ``_cancel``'s escalation would never be exercised.
    """
    w, be = worker
    seen = {}
    w._emit = lambda obj: seen.update(obj)

    ev = w._register("r9")
    out = w._handle({"op": "cancel", "id": "r9"})
    _kill_inflight(w)

    assert out is True, "cancel must not stop the child"
    assert seen["id"] == "r9"
    assert seen["delivered"] is True, seen
    assert ev.is_set()


def test_cancel_of_unknown_id_is_not_delivered(worker):
    """Unknown id => ``delivered: False``, not a false "I stopped it".

    MUTATION: hardcode ``delivered: True`` in the cancel branch.
    """
    w, _ = worker
    seen = {}
    w._emit = lambda obj: seen.update(obj)
    w._handle({"op": "cancel", "id": "never-existed"})
    assert seen["delivered"] is False
    assert seen["outcome"] == "not_running"


# ==========================================================================
# 3. THREE CHANNELS AT THE SAME TIME MUST NOT SERIALISE
# ==========================================================================
def test_three_concurrent_asr_requests_overlap(worker, fake_soundfile):
    """Assert OVERLAP (peak concurrent native decodes), not just completion.

    The barrier is what makes this deterministic. Three threads that each sleep
    a fixed 0.30s do NOT prove overlap -- whether the third thread is even
    scheduled inside that window is a property of the machine, not of the code,
    and a `sleep`-based version of this test goes red under load while the
    product is fine. A ``Barrier(3)`` inverts the burden of proof: the three
    requests cannot complete until all three are inside the decode AT THE SAME
    TIME, so overlap is demanded rather than hoped for.

    MUTATION: set ``MAX_CONCURRENT_ASR = 1`` (or acquire ``_slots`` in
    ``_handle`` instead of in the worker thread). Only one request then reaches
    the decode, the barrier is never satisfied, and this fails -- which is
    exactly the inherited bug, where the supervisor held one lock and three
    previews queued behind each other.
    """
    w, be = worker
    fake_soundfile["sec"] = 30.0
    import threading as _th
    w._slots = _th.BoundedSemaphore(3)

    # Demanded, not raced: all three must be in the model simultaneously.
    rendezvous = _th.Barrier(3, timeout=10.0)

    def overlapping(audio, sr, *, max_sec=None):
        try:
            rendezvous.wait()
        except _th.BrokenBarrierError:
            raise AssertionError(
                "only %d of 3 requests reached the decode together -- the "
                "requests serialised." % (be.peak_live + 1))
        return [{"start_sec": 0.0, "end_sec": 1.0, "text": "x"}]

    be._segments = overlapping
    replies = []
    w._emit = replies.append

    channels = [("twitch", "a", "xqc"), ("twitch", "b", "shroud"),
                ("kick", "c", "trainwreckstv")]
    for i, (plat, vid, ch) in enumerate(channels):
        w._handle({"op": "asr", "id": "r%d" % i, "platform": plat,
                   "video_id": vid, "channel": ch, "head_sec": 30.0,
                   "first_output_s": 10.0, "total_s": 20.0})
    deadline = time.monotonic() + 20
    while len([r for r in replies if r.get("event") != "rows"]) < 3 \
            and time.monotonic() < deadline:
        time.sleep(0.02)

    finals = [r for r in replies if r.get("event") != "rows"]
    assert len(finals) == 3, "not all three replied: %d" % len(finals)
    assert be.peak_live == 3, (
        "three previews did NOT overlap (peak concurrent = %d). Serialised."
        % be.peak_live)
    ids = sorted(r["id"] for r in finals)
    assert ids == ["r0", "r1", "r2"], ids


def test_each_reply_carries_its_own_id(worker, fake_soundfile):
    """Ids are echoed, so concurrent replies cannot be swapped.

    MUTATION: remove ``out["id"] = rid`` before ``_emit(out)`` in
    ``_run_asr``. With three overlapping requests the supervisor's demux has
    nothing to match on and hands callers the wrong VOD's words.
    """
    w, be = worker
    fake_soundfile["sec"] = 20.0

    def jitter(audio, sr, *, max_sec=None):
        time.sleep(0.10 * (int(audio[0] * 1000) % 3 + 1))
        return [{"start_sec": 0.0, "end_sec": 1.0, "text": "t"}]

    be._segments = jitter
    replies = []
    w._emit = replies.append
    for i in range(3):
        w._handle({"op": "asr", "id": "id%d" % i, "platform": "twitch",
                   "video_id": str(i), "channel": "c", "head_sec": 20.0,
                   "first_output_s": 10.0, "total_s": 20.0})
    deadline = time.monotonic() + 20
    while len([r for r in replies if r.get("event") != "rows"]) < 3 \
            and time.monotonic() < deadline:
        time.sleep(0.02)
    finals = [r for r in replies if r.get("event") != "rows"]
    assert len(finals) == 3, finals
    assert sorted(r["id"] for r in finals) == ["id0", "id1", "id2"]


def test_streaming_rows_event_precedes_the_final_reply(worker, fake_soundfile):
    """Rows stream as ``event: rows`` so a caller can time the FIRST one.

    MUTATION: drop the ``_on_rows(fresh, first_row_s)`` call in the decode
    loop; no rows event is emitted, the supervisor's first-output ceiling can
    never be satisfied, and every preview is declared stalled.
    """
    w, be = worker
    fake_soundfile["sec"] = 60.0
    replies = []
    w._emit = replies.append
    w._handle({"op": "asr", "id": "s1", "platform": "twitch", "video_id": "1",
               "channel": "c", "head_sec": 60.0, "first_output_s": 10.0,
               "total_s": 20.0})
    deadline = time.monotonic() + 20
    while len(replies) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    kinds = [r.get("event") for r in replies]
    assert "rows" in kinds, "no streaming rows event: %s" % kinds
    assert replies[-1].get("event") is None, "final reply must come last"
    assert replies[-1]["outcome"] == "ok"
    assert replies[-1]["first_row_s"] is not None


# ==========================================================================
# 4. THE HEAD BOUND IS REAL
# ==========================================================================
def test_whole_vod_request_is_clamped(worker, fake_soundfile):
    """4 hours asked for => 180 s, and the FETCHER is told the clamped value.

    MUTATION: raise ``HEAD_MAX_S`` to 14400, or return ``h`` unclamped in
    ``clamp_head_sec``. Transcribing a whole VOD to fill a preview panel is
    precisely the bug the bound exists to stop.
    """
    w, be = worker
    fake_soundfile["sec"] = 30.0
    out = w._asr("twitch", "1", "xqc", 14400.0, chunk_s=30.0,
                 first_output_s=10.0, total_s=20.0)
    assert out["head_sec"] == w.HEAD_MAX_S, out
    assert out["head_clamped"] is True
    fetches = [c for c in be.calls if c["op"] == "fetch"]
    assert len(fetches) == 1
    assert fetches[0]["seconds"] == w.HEAD_MAX_S, (
        "clamp was not passed down to the fetcher: %s" % fetches[0]["seconds"])


def test_head_bound_is_configurable_and_ceiling_hard(worker):
    """``clamp_head_sec`` honours small asks and refuses large ones.

    MUTATION: delete the ``if h > HEAD_MAX_S`` branch.
    """
    w, _ = worker
    assert w.clamp_head_sec(90.0) == (90.0, False)
    assert w.clamp_head_sec(10 ** 9) == (w.HEAD_MAX_S, True)
    assert w.clamp_head_sec(-5) == (w.HEAD_DEFAULT_S, True)
    # Garbage is DEFAULTED, not clamped: nothing was cut off a caller's ask.
    # MUTATION: return the raw value instead of ``_fnum(...)``'s default.
    assert w.clamp_head_sec("nonsense") == (w.HEAD_DEFAULT_S, False)


def test_head_bound_default_is_documented_in_the_module(worker):
    """The default is stated where the code is, not only in a report.

    MUTATION: change ``HEAD_DEFAULT_S`` without updating the comment block
    above it.
    """
    w, _ = worker
    assert w.HEAD_DEFAULT_S == 120.0
    assert w.HEAD_MAX_S == 180.0
    src = Path(w.__file__).read_text(encoding="utf-8")
    assert "HEAD_DEFAULT_S = 120.0" in src and "HEAD_MAX_S = 180.0" in src
    assert "4-hour Twitch VOD" in src, "the reasoning for the bound was deleted"


# ==========================================================================
# 5. NO SHARED-FILE WRITES (the O_APPEND law in AGENTS.md)
# ==========================================================================
def test_temp_dir_is_per_request_and_removed(worker, fake_soundfile):
    """Each request gets its OWN temp dir; nothing shared is appended to.

    The ``append_ledger`` incident in ``I:\\!manager`` AGENTS.md lost 31-40
    records per trial on Windows because two processes interleaved between
    seek-to-end and write. This worker must not be able to hit that shape.

    MUTATION: replace the per-request ``mkdtemp`` with a FIXED shared path —
    three concurrent requests then append to one file and this fails.
    """
    w, be = worker
    fake_soundfile["sec"] = 30.0
    seen_paths = []
    be._orig_fetch = be.fetch

    def spy(platform, video_id, channel, out_wav, seconds, *, timeout_s=120.0):
        seen_paths.append(str(out_wav))
        return be._orig_fetch(platform, video_id, channel, out_wav, seconds,
                              timeout_s=timeout_s)

    be.fetch = spy
    for i in range(3):
        w._asr("twitch", str(i), "c", 20.0, chunk_s=30.0,
               first_output_s=10.0, total_s=20.0)
    assert len(set(seen_paths)) == 3, "requests shared one output file: %s" % seen_paths
    for p in seen_paths:
        assert not Path(p).exists(), "temp wav left behind: %s" % p


def test_permanent_error_is_distinguished_from_transient(worker):
    """A gone VOD is cacheable; a network blip is not. They differ in the reply.

    MUTATION: drop the ``permanent=permanent`` key from the reply, or return
    ``False`` unconditionally — the router then re-pays a doomed fetch on every
    tab switch.
    """
    w, be = worker

    def boom(*_a, **_k):
        raise PermanentBoom("gone")

    be.fetch = boom
    out = w._asr("twitch", "1", "xqc", 20.0, chunk_s=30.0,
                 first_output_s=5.0, total_s=10.0)
    assert out["outcome"] == "permanent"
    assert out["permanent"] is True

    def blip(*_a, **_k):
        raise TransientBoom("network")

    be.fetch = blip
    out2 = w._asr("twitch", "1", "xqc", 20.0, chunk_s=30.0,
                  first_output_s=5.0, total_s=10.0)
    assert out2["outcome"] == "unavailable"
    assert out2["permanent"] is False


# ==========================================================================
# 6. THE SUPERVISOR: THE AUTHORITATIVE, NON-COOPERATIVE CEILING
# ==========================================================================
# The worker ceiling is cooperative — it fires at a chunk boundary. A chunk
# already blocked inside native decode cannot be interrupted by any Python
# construct. The supervisor is therefore the real ceiling: it watches the
# PIPE, and a pipe is observable no matter what the child thread is doing.


class FakeChildSidecar:
    """Drives the real ``_Sidecar`` roundtrip against a scripted fake child."""

    def __new__(cls, script):
        from services import preview_asr_sidecar as sc

        side = sc._Sidecar()

        class Child:
            stdin = None
            stdout = None
            stderr = None

            def poll(self):
                return None

            def terminate(self):
                Child.killed = True

            def kill(self):
                Child.killed = True

        Child.killed = False
        side._ensure = lambda: Child()
        side.writes = []
        side.child = Child

        def write_line(proc, payload):
            side.writes.append(payload)
            script(payload, side)

        side._write_line = write_line
        side._script = script
        return side


def _reply(side, rid, msg):
    q = side._pending.get(str(rid))
    if q is not None:
        q.put(msg)


def test_supervisor_fires_first_output_ceiling_on_wedged_child():
    """A child that never says a word is CANCELLED and named ``stalled``.

    This is the bounded-return-by-form proof. The child "never" answers, and
    the call still returns inside the first-output ceiling — because the wait
    is a queue read with its own deadline, not a bare ``join``.

    MUTATION: delete the ``if first_row_s is None and now >= deadline_first``
    branch in ``_Sidecar._roundtrip``. The stall then waits for the TOTAL
    ceiling, and the reported failure becomes a generic TimeoutError — the
    exact "silence counted as slowness" collapse this lane exists to stop.
    """
    from services import preview_asr_sidecar as sc

    def script(payload, side):
        if payload.get("op") == "cancel":
            _reply(side, payload["id"] + ":ack",
                   {"ok": True, "id": payload["id"], "delivered": True})
        # an "asr" op gets NO reply. ever.

    side = FakeChildSidecar(script)
    t0 = time.monotonic()
    with pytest.raises(sc.SidecarStalled):
        side.request({"op": "asr", "platform": "twitch", "video_id": "1",
                      "channel": "xqc"},
                     timeout=30.0, first_output_s=0.4)
    took = time.monotonic() - t0
    assert took < 2.0, (
        "a silent child held the caller %.1fs — the ceiling is not enforced" % took)
    assert not side.child.killed, (
        "child should have acked the cancel, not been killed")


def test_supervisor_total_ceiling_is_distinct_from_the_stall_ceiling():
    """Rows arriving means the stall ceiling is satisfied; the total one fires.

    MUTATION: replace the ``if now >= deadline_total`` branch with a no-op —
    the caller then waits forever once rows have started arriving.
    """
    from services import preview_asr_sidecar as sc

    def script(payload, side):
        rid = payload["id"]
        if payload.get("op") == "asr":
            threading.Timer(0.05, lambda: _reply(
                side, rid, {"ok": True, "id": rid, "event": "rows",
                            "rows": [{"start_sec": 0.0, "text": "hi"}]})).start()
            # ...and the FINAL reply never comes (decode wedges after row 1)
        elif payload.get("op") == "cancel":
            _reply(side, rid + ":ack", {"ok": True, "id": rid, "delivered": True})

    side = FakeChildSidecar(script)
    with pytest.raises(TimeoutError):
        side.request({"op": "asr", "platform": "twitch", "video_id": "1",
                      "channel": "c"},
                     timeout=0.6, first_output_s=5.0)


def test_supervisor_records_first_row_time_on_success():
    """A reply that succeeded still carries ``first_row_s`` — the discriminator.

    MUTATION: delete the ``msg.setdefault("first_row_s", ...)`` line.
    """
    from services import preview_asr_sidecar as sc

    def script(payload, side):
        rid = payload["id"]
        if payload.get("op") == "asr":
            threading.Timer(0.05, lambda: _reply(
                side, rid, {"ok": True, "id": rid, "event": "rows",
                            "rows": [{"start_sec": 0.0, "text": "hi"}]})).start()
            threading.Timer(0.15, lambda: _reply(
                side, rid, {"ok": True, "id": rid, "outcome": "ok",
                            "rows": [{"start_sec": 0.0, "text": "hi"}],
                            "covered_sec": 30.0})).start()

    side = FakeChildSidecar(script)
    out = side.request({"op": "asr", "platform": "twitch", "video_id": "1",
                        "channel": "c"}, timeout=5.0, first_output_s=2.0)
    assert out["outcome"] == "ok"
    assert out["first_row_s"] is not None
    assert 0.0 < out["first_row_s"] < 2.0, out


def test_three_supervisor_requests_do_not_serialise():
    """The inherited global lock is gone: three roundtrips overlap.

    MUTATION: re-introduce ``with self._lock:`` around ``_roundtrip`` (what
    the file this lane inherited actually did). ``peak_outstanding`` then
    equals 1 and the owner's three-channel test fails on a design choice
    rather than on the GPU.
    """
    from services import preview_asr_sidecar as sc

    state = {"inflight": 0, "peak": 0}
    guard = threading.Lock()

    def script(payload, side):
        rid = payload["id"]
        if payload.get("op") != "asr":
            return

        def answer():
            with guard:
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
            time.sleep(0.30)
            with guard:
                state["inflight"] -= 1
            _reply(side, rid, {"ok": True, "id": rid, "event": "rows",
                               "rows": [{"start_sec": 0.0, "text": "hi"}]})
            _reply(side, rid, {"ok": True, "id": rid, "outcome": "ok",
                               "rows": [{"start_sec": 0.0, "text": "hi"}],
                               "covered_sec": 30.0})

        threading.Thread(target=answer, daemon=True).start()

    side = FakeChildSidecar(script)
    results = []
    t0 = time.monotonic()

    def call(vid):
        out = side.request({"op": "asr", "platform": "twitch", "video_id": vid,
                            "channel": "c"}, timeout=10.0, first_output_s=5.0)
        results.append(out)

    threads = [threading.Thread(target=call, args=(v,)) for v in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.monotonic() - t0

    assert len(results) == 3, "not all three returned: %d" % len(results)
    assert state["peak"] == 3, (
        "three previews did NOT overlap (peak outstanding = %d). Serialised."
        % state["peak"])
    assert elapsed < 0.80, "three 0.30s roundtrips took %.2fs — they queued." % elapsed


def test_supervisor_orphans_do_not_cross_wired():
    """An event for an unregistered id is DROPPED, never handed to another.

    MUTATION: in ``_read_loop``/the demux, remove the ``if q is None: continue``
    guard and queue orphans onto an arbitrary pending request — callers then
    get another VOD's words, which is worse than being slow.
    """
    from services import preview_asr_sidecar as sc

    def script(payload, side):
        rid = payload["id"]
        if payload.get("op") != "asr":
            return
        # A late row from a request the caller already abandoned...
        _reply(side, rid, {"ok": True, "id": rid, "event": "rows",
                           "rows": [{"start_sec": 0.0, "text": "LATE"}]})
        time.sleep(0.05)
        _reply(side, rid + ":orphan", {"ok": True, "id": rid + ":orphan",
                                       "event": "rows",
                                       "rows": [{"start_sec": 0.0, "text": "WRONG"}]})
        _reply(side, rid, {"ok": True, "id": rid, "outcome": "ok",
                           "rows": [{"start_sec": 0.0, "text": "mine"}],
                           "covered_sec": 30.0})

    side = FakeChildSidecar(script)
    seen = []
    out = side.request({"op": "asr", "platform": "twitch", "video_id": "v",
                        "channel": "c"}, timeout=5.0, first_output_s=3.0,
                       on_rows=lambda rows, _fs: seen.extend(rows))
    texts = [r["text"] for r in out["rows"]]
    assert texts == ["mine"], texts
    assert "WRONG" not in texts, "an orphan row was delivered to the wrong caller"


def test_transport_failure_is_retried_once_then_raised():
    """A dead child respawns once; a second failure is a hard error.

    MUTATION: set the retry tuple to ``(1,)``, or drop the ``self.stop()``
    before the retry — then the dead pipe is reused and the caller waits on a
    corpse for the full ceiling.
    """
    from services import preview_asr_sidecar as sc

    side = FakeChildSidecar(lambda payload, side: None)
    attempts = {"n": 0}
    real = side._roundtrip

    def counting(*a, **k):
        attempts["n"] += 1
        raise OSError("pipe is gone")

    side._roundtrip = counting
    side.stop = lambda: None
    with pytest.raises(sc.SidecarUnavailable):
        side.request({"op": "asr", "platform": "twitch", "video_id": "1",
                      "channel": "c"}, timeout=1.0, first_output_s=0.5)
    assert attempts["n"] == 2, attempts


def test_deadline_never_raises_a_bare_timeout_from_join():
    """No ``join(timeout)`` anywhere in the request path — form, not intent.

    A join-based wait is the inherited bug: the reading thread outlives the
    timeout and steals the NEXT reply, desyncing the pipe for every caller.

    MUTATION: reintroduce ``t.join(timeout); if t.is_alive(): raise
    TimeoutError`` in ``_roundtrip``.
    """
    from services import preview_asr_sidecar as sc

    src = Path(sc.__file__).read_text(encoding="utf-8")
    body = src.split("def _roundtrip", 1)[1].split("def _cancel", 1)[0]
    assert ".join(" not in body, "_roundtrip waits with join() again"
    assert "q.get(timeout=" in body, "the deadline-bearing queue read is gone"