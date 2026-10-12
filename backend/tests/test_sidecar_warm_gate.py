"""A Popen child is NOT a ready sidecar - the warm gate.

Measured in the owner log 2026-10-12 13:59: two preview-subtitles requests hit
a cold sidecar at once. Both spawned (pid=18524 provider=cuda load=12.3s AND
pid=41436 provider=cuda load=0.0s) because Popen sets _proc the instant the
process exists, while the CUDA model load behind it takes ~5-12s and
FIRST_OUTPUT_S is 4s. The loser's stop() killed the winner's in-flight rows,
and every preview-subtitles request in that window answered 502 with "no
caption row within 4.0s".

These pin the two halves: an unwarmed child is never handed out, and a warmed
child is reused rather than respawned.
"""

from __future__ import annotations

import threading
import time

from services.preview_asr_sidecar import _Sidecar


class _FakeProc:
    """Enough of Popen for stop(): poll, wait, terminate, kill, stdin."""

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _spy_spawn(side, stamp):
    def _spawn():
        stamp["n"] += 1
        side._proc = _FakeProc()
    return _spawn


def test_warm_child_is_reused_not_respawned(monkeypatch):
    side = _Sidecar()
    stamp = {"n": 0}
    monkeypatch.setattr(side, "_spawn", _spy_spawn(side, stamp))
    side._warm_done = True
    side._proc = _FakeProc()
    first = side._ensure()
    second = side._ensure()
    assert first is second
    assert stamp["n"] == 0, "a warmed child must be reused"


def test_unwarmed_child_is_never_handed_out(monkeypatch):
    """The race itself: the process exists but the model has not loaded."""
    side = _Sidecar()
    stamp = {"n": 0}
    side._proc = _FakeProc()   # Popen returned; CUDA load still running
    side._warm_done = False
    monkeypatch.setattr(side, "_spawn", _spy_spawn(side, stamp))
    proc = side._ensure()
    assert stamp["n"] == 1, ("a child that has not warmed must be replaced, "
                             "not handed to a 4s first-output ceiling")
    assert side._warm_done is True
    assert proc is side._proc


def test_eight_concurrent_requests_cost_one_spawn(monkeypatch):
    """Eight requests against a cold sidecar = ONE warm, not eight spawns."""
    side = _Sidecar()
    stamp = {"n": 0}
    barrier = threading.Barrier(8)
    out = []

    def slow_spawn():
        stamp["n"] += 1
        time.sleep(0.05)          # the model-load window
        side._proc = _FakeProc()

    monkeypatch.setattr(side, "_spawn", slow_spawn)

    def hit():
        barrier.wait()
        out.append(side._ensure())

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 8
    assert len({id(p) for p in out}) == 1, "everyone waits for the SAME warm"
    assert stamp["n"] == 1, "eight requests must not cost eight spawns"