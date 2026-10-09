"""Supervisor for the preview-ASR sidecar process.

Holds one long-lived ``python -m services.preview_asr_worker`` child, speaks
the newline-JSON protocol to it, and respawns it when it dies. Nothing here
imports sherpa-onnx or CUDA: the API process must not touch the ASR native
stack, because a native fault there takes the whole web server with it
(measured: ``Windows fatal exception: access violation`` killed the API on
2026-10-06 while a preview decode was in flight).

Concurrency: NO GLOBAL LOCK. (See "Why the lock is gone" below.)
Requests are correlated by ``id`` through a single demultiplexer thread, so
N previews can be in flight in one child at the same time — which is what the
owner's test actually is: THREE CHANNELS AT THE SAME TIME.

Why the lock is gone
--------------------
The first cut of this file held one ``threading.Lock`` around the entire
request/response cycle and said so in the module docstring: "requests are
serialised with a lock … two previews decoding at once would contend for the
same CUDA context". Measured consequence: the owner's three-channel test
cannot pass, because request 2 waits for request 1 to finish decoding before
its own byte is even written to the pipe. Three 4-second decodes became 12
seconds and the 5-second promise died on a design choice, not on the GPU.

The stated CUDA concern is real but it is answered by CONTAINMENT, not by a
lock: the decode already lives in a separate process precisely because a
native fault must not take the web server down. One child fault now costs one
preview — the supervisor sees the pipe close and respawns — instead of the
whole app. Given that containment, serialising buys nothing and costs the
owner's headline requirement, so the lock is removed and the native stack's
thread-safety is relied on explicitly: ``archive_transcribe._parakeet_model``
keeps ONE model per device and hands each thread its own ``create_stream()``.

What replaced the lock is not "no synchronisation" — it is per-request state:

  * one ``threading.Lock`` held only across a single ``stdin.write`` of one
    line (a pipe write is not atomic across threads, and interleaved bytes
    would corrupt the protocol for EVERY caller);
  * one reader thread per child, demultiplexing lines to per-``id`` queues.

That is a demultiplexer, not a serialiser: it never holds anything while a
decode runs.

First-output ceiling
--------------------
``transcribe_head`` enforces TWO deadlines, and the distinction is the point:

  * ``first_output_s`` — no caption row has arrived by now → the request is
    CANCELLED and reported as ``stalled``. Silence is named as silence.
  * ``total_s`` — the request is cancelled and reported as ``slow``.

A wall ceiling alone cannot tell those apart, and this repo has already paid
for confusing them (``STATE.md``: 5 of 94 health attempts "never reached the
endpoint at all; those are ``not_measured``, and they are excluded from the
latency percentiles rather than counted as 0 ms"). A 4.9 s answer must say
whether it was 4.9 s of work or 4.9 s of a hung model.

Cancellation is real: it sends ``{"op": "cancel", "id": ...}``, WAITS for the
child's ack, and escalates to ``terminate()``/``kill()`` if the child does not
ack. Nothing here can be left blocking on a wedged child.

Failure policy: a sidecar that raises, dies, or returns ``ok: false`` is torn
down and replaced on the next request. One failed preview must never leave
the endpoint permanently broken.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_STARTUP_TIMEOUT_S = 180.0

# Ceilings. Both are overridable per call and both are REPORTED back, so a
# caller can see which one fired. Defaults are reasoned in the worker module
# (FIRST_OUTPUT_DEFAULT_S / TOTAL_DEFAULT_S); they live here as the
# supervisor-side mirror because the supervisor is what enforces them.
FIRST_OUTPUT_S = float(os.environ.get("VODRIP_PREVIEW_ASR_FIRST_OUTPUT_S", "4.0"))
TOTAL_S = float(os.environ.get("VODRIP_PREVIEW_ASR_TOTAL_S", "20.0"))
HEAD_DEFAULT_S = 120.0

# Hard child bound: even if every reply mechanism were broken, the supervisor
# returns in total_s + this. A ceiling a hung child can outlive is not a
# ceiling.
_HARD_STOP_GRACE_S = 2.0

_ids = itertools.count(1)


class SidecarUnavailable(RuntimeError):
    """The sidecar could not be reached or answered unusably."""


class SidecarPermanent(RuntimeError):
    """The VOD will never yield audio (deleted / sub-only / geo)."""


class SidecarStalled(RuntimeError):
    """No caption row arrived inside the first-output ceiling. NOT "slow".

    This is silence, named as silence. It is deliberately a distinct type so
    that no caller can collapse it into a generic timeout by accident — a
    caller that wants partial captions on a stall has to say so on purpose.
    """

    def __init__(self, message: str, *, first_row_s: float | None = None) -> None:
        super().__init__(message)
        self.first_row_s = first_row_s


class SidecarCancelled(RuntimeError):
    """The caller cancelled the request; the child was told and stopped."""


class _Sidecar:
    """One child process + a demultiplexer. No lock around the work."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()   # one pipe write at a time, only
        self._state_lock = threading.Lock()   # guards _proc swap only
        self._pending: dict[str, "queue.Queue[dict]"] = {}
        self._reader: threading.Thread | None = None
        self._provider: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def _spawn(self) -> None:
        env = dict(os.environ)
        # The child must not inherit the app's daemons flags.
        env.pop("VODRIP_NO_DAEMONS", None)
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "services.preview_asr_worker"],
            cwd=str(_BACKEND_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr stays a PIPE and is drained to the app log: the previous
            # DEVNULL threw away the native crash trace that this whole file
            # exists to survive.
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._state_lock:
            self._proc = proc
            self._pending = {}
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), daemon=True, name="sidecar-reader")
        self._reader.start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True,
                         name="sidecar-stderr").start()
        try:
            info = self._roundtrip({"op": "warm"}, rid="warm-%d" % next(_ids),
                                   first_output_s=_STARTUP_TIMEOUT_S,
                                   total_s=_STARTUP_TIMEOUT_S)
        except Exception:
            self.stop()
            raise
        if not info.get("ok"):
            self.stop()
            raise RuntimeError("preview ASR sidecar failed to warm: %s" % info.get("error"))
        self._provider = info.get("provider")
        logger.info(
            "preview ASR sidecar ready (pid=%s provider=%s load=%.1fs)",
            proc.pid, self._provider, float(info.get("load_s") or 0.0),
        )

    def _alive(self) -> bool:
        with self._state_lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    def stop(self) -> None:
        with self._state_lock:
            proc, self._proc = self._proc, None
            pending, self._pending = self._pending, {}
        for q in pending.values():
            q.put({"ok": False, "outcome": "unavailable", "error": "sidecar stopped"})
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                self._write_line(proc, {"op": "shutdown"})
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            for closer in (proc.terminate, proc.kill):
                try:
                    closer()
                    proc.wait(timeout=2)
                    break
                except Exception:  # noqa: BLE001
                    continue

    # -- io ----------------------------------------------------------------
    def _write_line(self, proc: subprocess.Popen, payload: dict) -> None:
        """One line, whole. The only lock in the request path.

        Deliberately NOT held across a decode: it is scoped to a single
        ``write`` so two threads cannot interleave bytes on one pipe.
        """
        data = json.dumps(payload) + "\n"
        with self._write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                logger.warning("preview-asr-sidecar: %s", line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def _read_loop(self, proc: subprocess.Popen) -> None:
        """Demultiplex stdout by id. Never blocks a caller's request."""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001
                    logger.warning("preview ASR sidecar emitted non-JSON: %.120s", line)
                    continue
                rid = str(msg.get("id") or "")
                with self._state_lock:
                    q = self._pending.get(rid)
                if q is None:
                    # Unsolicited (late row from a cancelled request). Dropping
                    # it is correct: its caller has already been answered, and
                    # handing it to another id would be the wrong VOD's words.
                    logger.debug("preview ASR sidecar dropped orphan event id=%s", rid)
                    continue
                q.put(msg)
        except Exception:  # noqa: BLE001 - pipe died; requests fail by ceiling
            pass
        finally:
            with self._state_lock:
                pending, self._pending = self._pending, {}
            for q in pending.values():
                q.put({"ok": False, "outcome": "unavailable",
                       "error": "sidecar closed its output (died?)"})

    def _ensure(self) -> subprocess.Popen:
        with self._state_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            return proc
        self.stop()
        self._spawn()
        with self._state_lock:
            assert self._proc is not None
            return self._proc

    def _register(self, rid: str) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue()
        with self._state_lock:
            self._pending[rid] = q
        return q

    def _unregister(self, rid: str) -> None:
        with self._state_lock:
            self._pending.pop(rid, None)

    def _roundtrip(self, payload: dict, *, rid: str, first_output_s: float,
                   total_s: float, on_rows=None) -> dict:
        """One request, two ceilings, guaranteed to return.

        Never raises TimeoutError from a bare join: every wait is a queue read
        with its own deadline, so a wedged child cannot wedge this thread past
        ``total_s + _HARD_STOP_GRACE_S``.
        """
        proc = self._ensure()
        q = self._register(rid)
        t0 = time.monotonic()
        try:
            self._write_line(proc, dict(payload, id=rid))

            first_row_s: float | None = None
            deadline_first = t0 + first_output_s
            deadline_total = t0 + total_s

            while True:
                now = time.monotonic()
                if first_row_s is None and now >= deadline_first:
                    # SILENCE, not slowness. Cancel first, then answer.
                    self._cancel(proc, rid)
                    raise SidecarStalled(
                        "no caption row within %.1fs (first-output ceiling)"
                        % first_output_s, first_row_s=None)
                if now >= deadline_total:
                    self._cancel(proc, rid)
                    raise TimeoutError(
                        "preview ASR exceeded total ceiling %.1fs" % total_s)

                wait = min(deadline_first, deadline_total) - now if first_row_s is None \
                    else deadline_total - now
                try:
                    msg = q.get(timeout=max(0.01, min(wait, 0.25)))
                except queue.Empty:
                    continue

                if msg.get("event") == "rows":
                    if first_row_s is None:
                        first_row_s = time.monotonic() - t0
                    if on_rows is not None:
                        on_rows(msg.get("rows") or [], msg.get("first_row_s"))
                    continue

                msg.setdefault("first_row_s", None if first_row_s is None
                               else round(first_row_s, 3))
                msg["elapsed_s"] = round(time.monotonic() - t0, 3)
                return msg
        finally:
            self._unregister(rid)

    def _cancel(self, proc: subprocess.Popen, rid: str) -> None:
        """Deliver a cancel and WAIT for the ack; escalate if it never comes."""
        ack_q = self._register(rid + ":ack")
        try:
            # Registered BEFORE the write, so the ack cannot arrive into a void
            # and be dropped as an orphan.
            self._write_line(proc, {"op": "cancel", "id": rid})
            try:
                ack = ack_q.get(timeout=_HARD_STOP_GRACE_S)
            except queue.Empty:
                ack = None
            if ack and ack.get("delivered"):
                return  # child SAW it and will stop at its next checkpoint
            logger.warning("preview ASR sidecar cancel not acked; escalating")
            for closer in (proc.terminate, proc.kill):
                try:
                    closer()
                    return
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._unregister(rid + ":ack")

    def request(self, payload: dict, *, timeout: float = TOTAL_S,
                first_output_s: float = FIRST_OUTPUT_S,
                on_rows=None) -> dict:
        """Send one request, restarting the child if it is not there."""
        rid = str(payload.get("id") or next(_ids))
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                return self._roundtrip(
                    payload, rid=rid, first_output_s=first_output_s,
                    total_s=timeout, on_rows=on_rows)
            except (SidecarStalled, TimeoutError, SidecarCancelled):
                # A deadline is a verdict about the WORK, not about transport.
                # Retrying here would double the user's wait and could mask a
                # genuinely hung model behind a lucky retry.
                raise
            except Exception as exc:  # noqa: BLE001 - respawn and retry once
                last = exc
                logger.warning("preview ASR sidecar request failed (attempt %d/2): %s",
                               attempt, exc)
                self.stop()
                if attempt == 2:
                    break
        raise SidecarUnavailable("preview ASR sidecar unavailable: %s" % last)


_sidecar = _Sidecar()


def sidecar_status() -> dict:
    """Cheap health read for diagnostics; never raises."""
    with _sidecar._state_lock:  # noqa: SLF001
        inflight = len(_sidecar._pending)
        proc = _sidecar._proc
    return {
        "running": proc is not None and proc.poll() is None,
        "pid": proc.pid if proc else None,
        "provider": _sidecar._provider,
        "inflight": inflight,
        "first_output_s": FIRST_OUTPUT_S,
        "total_s": TOTAL_S,
    }


def transcribe_head(
    platform: str, video_id: str, channel: str, head_sec: float = HEAD_DEFAULT_S,
    *, first_output_s: float = FIRST_OUTPUT_S, total_s: float = TOTAL_S,
    on_rows=None,
) -> dict:
    """``{"rows": [...], "covered_sec": float, "wall_s": float, "outcome": str}``.

    Raises ``SidecarPermanent`` for a VOD that can never serve audio,
    ``SidecarStalled`` when nothing arrives inside the first-output ceiling,
    ``TimeoutError`` when the total ceiling fires, and ``SidecarUnavailable``
    for transport failures. Those are four different answers and the caller
    must not collapse them: the first two are settled facts worth caching, the
    third is worth retrying, and the last is an infrastructure problem.
    """
    t0 = time.monotonic()
    out = _sidecar.request(
        {
            "op": "asr",
            "platform": platform,
            "video_id": video_id,
            "channel": channel,
            "head_sec": float(head_sec),
            "first_output_s": float(first_output_s),
            "total_s": float(total_s),
        },
        timeout=total_s,
        first_output_s=first_output_s,
        on_rows=on_rows,
    )
    if not out.get("ok"):
        outcome = out.get("outcome")
        if out.get("permanent"):
            raise SidecarPermanent(out.get("error") or "VOD audio permanently unavailable")
        if outcome == "stalled":
            raise SidecarStalled(
                out.get("error") or "no caption row inside the first-output ceiling",
                first_row_s=out.get("first_row_s"))
        if outcome == "cancelled":
            raise SidecarCancelled(out.get("error") or "request cancelled")
        raise SidecarUnavailable(out.get("error") or "sidecar reported failure")
    out.setdefault("wall_s", round(time.monotonic() - t0, 3))
    out.setdefault("outcome", "ok")
    return out