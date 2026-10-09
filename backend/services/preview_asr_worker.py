"""Preview-ASR sidecar WORKER — a separate process that transcribes a VOD's head.

Why this exists at all
----------------------
The obvious implementation runs the slice fetch and the Parakeet decode in
the web API process, reusing the recognizer the archive worker already has.
Measured 2026-10-06: doing exactly that crashed the whole API with
``Windows fatal exception: access violation`` — a NATIVE fault, in the CUDA /
onnxruntime stack, which no Python ``try`` can catch. The web server died
with it and the owner's app went down.

A web server is a wrong host for a CUDA inference process. It has a thread
pool doing yt-dlp extraction, an SSE fan-out, and the archive worker lanes;
a native crash in one of them takes all of it with it. So the decode moved
OUT of the API process into this one.

What that buys, beyond not crashing the app:

  * The model load (~5.4 s measured) is paid ONCE per sidecar lifetime, not
    once per request. A warm preview is a few seconds instead of ~20.
  * A sidecar crash costs one preview, and the next request transparently
    respawns it. The API keeps serving.
  * The idle-closer that unloads the recognizer while the archive lanes may
    still hold a native context never runs here — this process owns its model
    outright and exits with it.

Protocol: newline-delimited JSON on stdin/stdout, one REQUEST PER LINE, and
every reply carries the ``id`` it answers.

  -> {"op": "warm"}
  <- {"ok": true, "provider": "cuda", "load_s": 5.4}

  -> {"op": "asr", "id": "r1", "platform": "twitch", "video_id": "123",
      "channel": "xqc", "head_sec": 120}
  <- {"ok": true, "id": "r1", "outcome": "ok", "rows": [{"start_sec": 0.0,
      "text": "..."}], "covered_sec": 120.0, "first_row_s": 2.4}

  -> {"op": "cancel", "id": "r1"}
  <- {"ok": true, "id": "r1", "outcome": "cancelled"}

  -> {"op": "ping"}
  <- {"ok": true, "pid": 1234}

stdout carries ONLY protocol lines — every library log goes to stderr — so a
stray log line can never corrupt a response. No media URL is ever printed.

Why ``id`` is not optional
-------------------------
The first cut of this file answered requests strictly in arrival order on a
single thread, so the supervisor had to hold ONE GLOBAL LOCK to know which
line belonged to which caller. Two replies arriving out of order then get
swapped, and the caption strip shows the wrong VOD's words — which is worse
than being slow. Correlation by ``id`` is what makes concurrency safe, and it
is a precondition for running three channels at once (the owner's ask), so it
is enforced here rather than assumed by the caller.

Bounded, not serialised
-----------------------
Each request runs on its own thread from a bounded pool. The bound is
``MAX_CONCURRENT_ASR`` (default 3, ``VODRIP_PREVIEW_ASR_CONCURRENCY``), which
is a CEILING, not a lock: three simultaneous previews genuinely overlap. The
recognizer itself is documented as thread-safe for exactly this reason —
``archive_transcribe._parakeet_model`` keeps ONE model per device and hands
threads their own ``create_stream()``.

Why a ceiling above 1 is safe HERE and was fatal in the API
-----------------------------------------------------------
The crash that created this file was a native fault in a process that also
served HTTP. A native fault HERE costs one preview: the supervisor notices the
child died and respawns it. That containment is the entire reason the decode
was moved out of the web server, and it is what buys the right to run more
than one at a time inside the child.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("vodrip.preview_asr_worker")

# --- bounds -----------------------------------------------------------------
# HEAD: the preview caption strip needs the first seconds of a VOD. Decoding a
# 4-hour Twitch VOD to fill a preview panel IS the bug this bound exists to
# stop. 120 s is two minutes of captions — enough for a panel that follows the
# playhead from 0 — and cost is linear in this number, so it is the knob with
# the worst ratio of value to GPU-seconds past ~180 s. HARD CEILING 180 s:
# a caller asking for a whole VOD is clamped, not obeyed.
HEAD_DEFAULT_S = 120.0
HEAD_MAX_S = 180.0

# Decode in slices so that (a) a caption row can be produced early instead of
# only at the end, (b) cancellation is checked between slices, and (c) the
# first-output ceiling means something. 30 s is one caption-paragraph.
CHUNK_DEFAULT_S = 30.0

# FIRST-OUTPUT ceiling: if no caption row has arrived this many ms after the
# request started, the answer is "stalled" — a hung model or a dead fetch —
# NOT "slow". 4.0 s keeps the promise inside the owner's 5-second play budget,
# so captions that CAN arrive do so while the user is still watching the
# start of the VOD rather than after they have given up on it.
FIRST_OUTPUT_DEFAULT_S = 4.0

# TOTAL ceiling: whole head, fetch included. 20 s covers a warm GPU decode
# (~2-6 s) and a CPU decode (~15-25 s) with the fetch on top, while still
# failing early enough to be worth retrying.
TOTAL_DEFAULT_S = 20.0

# Ceiling on simultaneous decodes in this process. A bound, not a lock.
MAX_CONCURRENT_ASR = max(1, int(os.environ.get("VODRIP_PREVIEW_ASR_CONCURRENCY", "3") or 3))

# Below this peak amplitude the head is genuinely mute; zero rows from quiet
# audio is a SETTLED FACT ("silent"), which is a different answer from
# "stalled" (nothing arrived, and we do not know why).
_SILENCE_PEAK = 1e-4

_out_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_ASR)
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()


def _emit(obj: dict) -> None:
    """Write exactly one protocol line to stdout, whole, under a lock.

    stdout is the protocol channel; two threads interleaving a partial line
    would desync the supervisor permanently.
    """
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _now() -> float:
    return time.monotonic()


def _fnum(v, default: float) -> float:
    """Best-effort float; a hostile/garbage field becomes the default."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return f


def clamp_head_sec(head_sec: float) -> tuple[float, bool]:
    """``(effective_head_sec, was_clamped)``.

    The clamp is the point: a caller that asks for 14400 s gets 180 s and is
    told it was clamped, rather than pinning the GPU for four hours.
    """
    h = _fnum(head_sec, HEAD_DEFAULT_S)
    if h <= 0:
        return HEAD_DEFAULT_S, True
    if h > HEAD_MAX_S:
        return HEAD_MAX_S, True
    return h, False


# --- the ASR backend seam ----------------------------------------------------
def _real_backend():
    """Resolve the native backend. Overridden in tests; never called there.

    Kept as a function so the deadline, cancellation and concurrency logic
    above it is testable on a machine with no CUDA and no sherpa-onnx — which
    is what lets ``test_preview_asr_worker.py`` go red on a real mutation
    instead of skipping because the model would not load.
    """
    from services import archive_transcribe as at

    class _Backend:
        fetch = staticmethod(at._fetch_remote_audio_slice)
        segments = staticmethod(at.parakeet_segments_from_array)
        permanent = staticmethod(at._is_remote_permanent_error)
        model = staticmethod(at._parakeet_model)
        device = staticmethod(at._detect_device)
        cuda_ok = staticmethod(at._parakeet_cuda_available)

    return _Backend()


_backend_factory = _real_backend


def _warm() -> dict:
    t0 = _now()
    try:
        be = _backend_factory()
        be.model()
    except Exception as exc:  # noqa: BLE001 - reported, not raised at the caller
        return {"ok": False, "outcome": "unavailable",
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    load_s = _now() - t0
    try:
        provider = be.device()[0]
        cuda = bool(be.cuda_ok())
    except Exception:  # noqa: BLE001
        provider, cuda = "unknown", False
    return {"ok": True, "outcome": "ok", "provider": provider, "cuda": cuda,
            "load_s": round(load_s, 3)}


def _peak_amplitude(audio) -> float:
    """Peak |sample| without assuming numpy is importable here."""
    try:
        import numpy as np

        return float(np.abs(np.asarray(audio, dtype="float64")).max()) if len(audio) else 0.0
    except Exception:  # noqa: BLE001
        try:
            return max(abs(x) for x in audio)
        except Exception:  # noqa: BLE001
            return 0.0


def _asr(platform: str, video_id: str, channel: str, head_sec: float,
         *, chunk_s: float, first_output_s: float, total_s: float,
         cancel: threading.Event | None = None, on_rows=None) -> dict:
    """Transcribe the HEAD of one VOD. Returns a dict; never raises.

    Deadline accounting is the product here. A total wall ceiling alone cannot
    tell the caller whether it waited 4.9 s for work or 4.9 s on a hung model,
    and this repo has already shipped reports that made exactly that
    confusion (``STATE.md``: attempts that "never reached the endpoint at all"
    are ``not_measured``, "excluded from the latency percentiles rather than
    counted as 0 ms"). So every reply states WHERE the time went and what the
    outcome MEANT.

    ``on_rows(new_rows, first_row_s)`` is called as each chunk lands. That is
    what makes the first-output ceiling OBSERVABLE from outside this process:
    the supervisor sees the first caption row arrive on the pipe, so the
    ceiling is enforced by the caller rather than trusted from a child's own
    self-report.
    """
    import shutil
    import tempfile

    t_start = _now()
    head_sec, clamped = clamp_head_sec(head_sec)
    if first_output_s > total_s:
        first_output_s = total_s

    def _elapsed() -> float:
        return _now() - t_start

    def _cancel_check(stage: str) -> None:
        if cancel is not None and cancel.is_set():
            raise _Cancelled(stage)

    out = {
        "ok": True,
        "outcome": "ok",
        "head_sec": round(head_sec, 3),
        "head_clamped": clamped,
        "rows": [],
        "covered_sec": 0.0,
    }

    try:
        _cancel_check("pre_fetch")
        be = _backend_factory()
    except _Cancelled as exc:
        out.update(ok=False, outcome="cancelled", stage=exc.stage)
        return out
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, outcome="unavailable",
                   error="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out

    outdir = Path(tempfile.mkdtemp(prefix="vodrip-sidecar-asr-"))
    try:
        wav = outdir / "head.wav"
        fetch_t0 = _now()
        # The fetch is bounded by the SAME budget as the decode: a fetch that
        # cannot land inside the request's ceiling is a failed request, and an
        # unbounded one is a wedged child holding a concurrency slot.
        covered = be.fetch(platform, video_id, channel, wav, head_sec,
                           timeout_s=max(1.0, total_s))
        fetch_s = _now() - fetch_t0
        out["covered_sec"] = round(float(covered or 0.0), 3)
        out["fetch_s"] = round(fetch_s, 3)

        _cancel_check("post_fetch")
        if _elapsed() >= total_s:
            # Audio arrived but the budget did not survive the network. That is
            # silence under the ceiling: named ``stalled``, never ``slow``.
            out.update(ok=False, outcome="stalled", stage="fetch",
                       first_row_s=None)
            return out

        import soundfile as sf

        audio, sr = sf.read(str(wav), dtype="float32", always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        out["peak"] = round(_peak_amplitude(audio), 6)

        rows: list[dict] = []
        first_row_s: float | None = None
        chunk = max(5.0, _fnum(chunk_s, CHUNK_DEFAULT_S))
        n = int(len(audio))
        sr_n = int(sr) or 16000
        offset = 0
        while offset < n:
            _cancel_check("decode")
            if _elapsed() >= total_s:
                break
            if first_row_s is None and _elapsed() >= first_output_s:
                # The ceiling that makes this file worth existing: no row has
                # appeared yet, so this is a hung decode, and saying "slow"
                # would hide that.
                out.update(ok=False, outcome="stalled", stage="decode",
                           rows=rows, first_row_s=None)
                return out
            stop = min(offset + int(chunk * sr_n), n)
            segs = be.segments(audio[offset:stop], sr, max_sec=chunk)
            fresh: list[dict] = []
            for s in segs or []:
                start = float(s.get("start_sec", 0.0)) + offset / sr_n
                end = float(s.get("end_sec", 0.0)) + offset / sr_n
                row = {
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                    "text": s.get("text", ""),
                }
                # Word timings survive: a caption strip wants them, and the
                # first cut of this file dropped them on the floor.
                if s.get("words"):
                    row["words"] = s["words"]
                rows.append(row)
                fresh.append(row)
                if first_row_s is None and row.get("text"):
                    first_row_s = _elapsed()
            offset = stop
            if fresh and on_rows is not None:
                try:
                    on_rows(fresh, first_row_s)
                except Exception:  # noqa: BLE001 - a broken pipe ends the reply,
                    pass          # not the decode; the final event still lands

        decode_s = _elapsed() - fetch_s
        out["decode_s"] = round(decode_s, 3)
        out["first_row_s"] = None if first_row_s is None else round(first_row_s, 3)

        if first_row_s is None:
            # Completed inside budget with nothing to show. Quiet audio is a
            # SETTLED FACT the caller may cache; a stalled one is not. The
            # peak amplitude is what tells the two apart honestly.
            quiet = out.get("peak", 0.0) < _SILENCE_PEAK
            out["outcome"] = "silent" if quiet else "stalled"
            out["ok"] = False
            return out

        out["rows"] = rows
        if _elapsed() >= total_s:
            # Rows in hand, budget spent: usable partial answer, named as
            # partial so the caller can decide to show it or not.
            out["outcome"] = "slow"
            out["partial"] = True
        return out

    except _Cancelled as exc:
        out.update(ok=False, outcome="cancelled", stage=exc.stage)
        return out
    except Exception as exc:  # noqa: BLE001
        # A VOD that is deleted / sub-only / geo-blocked will never serve
        # audio. Flagging it lets the router answer "no subtitles" and CACHE
        # that, instead of re-paying a doomed fetch on every tab switch.
        try:
            permanent = bool(be.permanent(exc))
        except Exception:  # noqa: BLE001
            permanent = False
        out.update(ok=False, outcome="permanent" if permanent else "unavailable",
                   permanent=permanent,
                   error="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return out
    finally:
        out["wall_s"] = round(_elapsed(), 3)
        # PER-REQUEST temp dir: no shared file is ever appended to, so the
        # O_APPEND/lockfile law in AGENTS.md does not bind here by design.
        shutil.rmtree(outdir, ignore_errors=True)


class _Cancelled(Exception):
    def __init__(self, stage: str) -> None:
        super().__init__("cancelled at %s" % stage)
        self.stage = stage


def _register(rid: str) -> threading.Event:
    ev = threading.Event()
    with _inflight_lock:
        _inflight[rid] = ev
    return ev


def _unregister(rid: str) -> None:
    with _inflight_lock:
        _inflight.pop(rid, None)


def _run_asr(rid: str, req: dict) -> None:
    """One request, one thread, bounded slot, cancellable, always one reply."""
    cancel = _register(rid)
    acquired = _slots.acquire(timeout=5.0)
    try:
        if not acquired:
            _emit({"ok": False, "id": rid, "outcome": "unavailable",
                   "error": "no decode slot within 5s (all %d busy)"
                             % MAX_CONCURRENT_ASR})
            return
        if cancel.is_set():
            _emit({"ok": False, "id": rid, "outcome": "cancelled", "stage": "queued"})
            return
        # Stream each landed chunk as an EVENT the supervisor can time. The
        # final reply still carries the whole row set, so this is additive.
        def _on_rows(fresh, first_row_s):
            _emit({"ok": True, "id": rid, "event": "rows", "rows": fresh,
                   "first_row_s": None if first_row_s is None else round(first_row_s, 3),
                   "at_s": round(_now() - t_start_ref[0], 3)})

        t_start_ref = [_now()]
        out = _asr(
            str(req["platform"]), str(req["video_id"]),
            str(req.get("channel") or ""),
            req.get("head_sec", HEAD_DEFAULT_S),
            chunk_s=req.get("chunk_s", CHUNK_DEFAULT_S),
            first_output_s=_fnum(req.get("first_output_s"), FIRST_OUTPUT_DEFAULT_S),
            total_s=_fnum(req.get("total_s"), TOTAL_DEFAULT_S),
            cancel=cancel,
            on_rows=_on_rows,
        )
        out["id"] = rid
        _emit(out)
    except KeyError as exc:
        _emit({"ok": False, "id": rid, "error": "missing field %s" % exc})
    except Exception as exc:  # noqa: BLE001 - a sidecar error is data, not death
        _emit({"ok": False, "id": rid, "outcome": "unavailable",
               "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
    finally:
        if acquired:
            _slots.release()
        _unregister(rid)


def _handle(req: dict) -> bool:
    """Dispatch one request. Returns False when the child should exit."""
    op = req.get("op")
    rid = req.get("id")

    if op == "cancel":
        with _inflight_lock:
            ev = _inflight.get(str(rid))
        # Acked either way: the supervisor needs to know whether the child
        # SAW the cancel, because that decides kill-vs-wait escalation.
        _emit({"ok": True, "id": rid, "outcome": "cancelled" if ev is not None
               else "not_running", "delivered": ev is not None})
        return True

    if op == "warm":
        out = _warm()
        out["id"] = rid
        _emit(out)
        return True

    if op == "ping":
        _emit({"ok": True, "id": rid, "outcome": "ok", "pid": os.getpid(),
               "inflight": len(_inflight), "concurrency": MAX_CONCURRENT_ASR})
        return True

    if op == "shutdown":
        _emit({"ok": True, "id": rid, "outcome": "ok"})
        return False

    if op == "asr":
        t = threading.Thread(target=_run_asr, args=(str(rid), req), daemon=True,
                             name="asr-%s" % rid)
        t.start()
        return True

    _emit({"ok": False, "id": rid, "error": "unknown op %r" % (op,)})
    return True


def _bootstrap_stdio() -> None:
    """Force UTF-8 on the protocol pipes.

    Called from __main__ ONLY: importing this module from a test must not
    reconfigure pytest's captured stdout.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    # Import the backend package by path: the sidecar is launched with an
    # explicit cwd/interpreter and cannot rely on being run from backend/.
    for p in (os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
        if p and p not in sys.path:
            sys.path.insert(0, p)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            _emit({"ok": False, "error": "bad json: %s" % exc})
            continue
        if not _handle(req):
            return 0
    # stdin closed: in-flight threads are daemonised, so the child exits and
    # the supervisor respawns rather than leaking a half-decoded slot.
    return 0


if __name__ == "__main__":
    _bootstrap_stdio()
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("VODRIP_PREVIEW_ASR_LOGLEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(main())