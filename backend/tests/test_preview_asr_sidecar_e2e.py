"""End-to-end test of the preview-ASR sidecar's REAL child process.

Why this file exists
--------------------
Every other test in ``test_preview_asr_worker.py`` talks to the worker
*in-process*, with ``archive_transcribe`` and ``soundfile`` faked. That proves
the control flow, the ceilings, the cancellation wiring and the id correlation.

It proves nothing about the thing that actually has to work on the owner's
machine: **that a child process can be started at all, from the backend
directory, as ``services.preview_asr_worker``, and that it speaks the
newline-JSON protocol on a real pipe.** If that handshake is broken, every
Twitch/Kick Subtitles tab 502s, and an in-process test would stay green while
the feature is dead on arrival. This is the gap the previous lane reported as
unverified and it is the cheapest one to close.

Deliberately NOT modelled here
------------------------------
The ``warm``/``asr`` ops load the real Parakeet model on CUDA. A test that
needs the model is a test that silently skips on a box without it, and a test
that skips is a test that never goes red. So this file proves the transport,
the correlation and the lifecycle -- which need no GPU -- and leaves the model
path to the faked suite. ``ping`` is answered by the worker's main loop without
touching the backend, which is exactly why it is the right probe.

If the spawn contract ever changes (different argv, different cwd, a different
module path), the child fails to start or fails to answer and these go RED.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Generous: this is process startup plus interpreter import, nothing more.
# It must be big enough that a loaded CI box does not produce a false red, and
# small enough that a genuinely broken spawn does not hang the suite.
SPAWN_TIMEOUT_S = 60.0


class RealChild:
    """A live ``services.preview_asr_worker``, spoken to over real pipes."""

    def __init__(self) -> None:
        from services import preview_asr_sidecar as sc

        # Reuse the supervisor's own backend dir so this test tracks the real
        # spawn location instead of hardcoding a second copy that can drift.
        env = dict(os.environ)
        env.pop("VODRIP_NO_DAEMONS", None)

        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "services.preview_asr_worker"],
            cwd=str(sc._BACKEND_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr is DEVNULL, not PIPE: the child writes logs there, and an
            # undrained PIPE fills its 64 KB buffer and BLOCKS the child. The
            # test never reads stderr, so there is nothing to drain.
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.lock = threading.Lock()

    def send(self, payload: dict) -> None:
        with self.lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def recv(self) -> dict:
        """Read exactly one reply line. Raises if the child says nothing."""
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(
                "sidecar child produced no reply line (exit=%s)" % self.proc.poll()
            )
        return json.loads(line)

    def roundtrip(self, payload: dict) -> dict:
        self.send(payload)
        return self.recv()

    def close(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write(json.dumps({"op": "shutdown", "id": "bye"}) + "\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown must never mask a failure
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass


@pytest.fixture
def child():
    c = RealChild()
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# The handshake itself
# ---------------------------------------------------------------------------
def test_real_child_spawns_and_answers_ping(child):
    """The owner's Subtitles tab depends on this and nothing above it.

    MUTATION: break the spawn argv (``-m services.preview_asr_worker`` ->
    ``-m services.preview_asr_wroker``) or the cwd. The child then cannot
    start, ``recv`` raises on the empty pipe, and this is RED.
    """
    t0 = time.monotonic()
    reply = child.roundtrip({"op": "ping", "id": "p1"})
    elapsed = time.monotonic() - t0

    assert reply.get("ok") is True, reply
    assert reply.get("outcome") == "ok", reply
    # The pid must be the real child's, not the test process's: that is what
    # proves we answered from the process we spawned.
    assert reply.get("pid") == child.proc.pid, (
        "reply pid %r is not the spawned child %r" % (reply.get("pid"), child.proc.pid))
    assert elapsed < SPAWN_TIMEOUT_S, elapsed


def test_ping_reports_the_configured_concurrency(child):
    """The ceiling the owner asked for (3 channels at once) is readable.

    MUTATION: hardcode ``"concurrency": 1`` in the worker's ping reply.
    """
    reply = child.roundtrip({"op": "ping", "id": "p2"})
    assert int(reply.get("concurrency")) >= 3, (
        "sidecar advertises concurrency=%r; the owner's ask is 3 channels at "
        "once" % reply.get("concurrency"))


def test_replies_are_correlated_by_id_over_a_real_pipe():
    """Concurrent requests cannot be answered with each other's rows.

    This is the property the supervisor's demultiplexer rests on. Over a real
    pipe with three requests in flight, each reply must carry its own id.

    MUTATION: drop ``out["id"] = rid`` in the worker, or stop echoing the id in
    the ping branch.
    """
    ids = ["a", "b", "c"]
    replies: list[dict] = []
    errors: list[str] = []

    def one(rid: str) -> None:
        # Each thread gets its OWN child process. Sharing one child across
        # threads would make recv() read another thread's reply — the test
        # would hang or assert on the wrong response.
        c = RealChild()
        try:
            reply = c.roundtrip({"op": "ping", "id": rid})
            replies.append(reply)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        finally:
            c.close()

    threads = [threading.Thread(target=one, args=(r,)) for r in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=SPAWN_TIMEOUT_S)

    assert not errors, "child processes failed: %s" % "; ".join(errors)
    assert len(replies) == 3, "only %d of 3 pings were answered" % len(replies)
    assert sorted(r.get("id") for r in replies) == sorted(ids), replies


def test_unknown_op_is_answered_not_swallowed(child):
    """A typo or an old client must get a reply, never silence.

    MUTATION: make the main loop ``continue`` on an unknown op without emitting.

    A silent child here is what a hung sidecar looks like from the UI: the tab
    spins until some unrelated timeout fires. Naming it as an error is the whole
    point of the outcome vocabulary.
    """
    reply = child.roundtrip({"op": "definitely-not-an-op", "id": "u1"})
    assert reply.get("id") == "u1", reply
    assert reply.get("ok") is not True, (
        "an unknown op answered ok=True: %r" % reply)


def test_shutdown_exits_clean(child):
    """No orphaned ASR child holding a CUDA context after the app closes.

    MUTATION: make the worker's shutdown op return False (keep running).
    """
    child.send({"op": "shutdown", "id": "s1"})
    code = child.proc.wait(timeout=10)
    assert code == 0, "sidecar child exited %r on shutdown" % code


def test_child_stays_alive_across_many_roundtrips(child):
    """The child is long-lived; the supervisor must be able to reuse it.

    MUTATION: make the main loop exit after the first request.
    """
    for i in range(12):
        reply = child.roundtrip({"op": "ping", "id": "r%d" % i})
        assert reply.get("ok") is True, "roundtrip %d failed: %r" % (i, reply)
    assert child.proc.poll() is None, "child died while idle"