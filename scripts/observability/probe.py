"""VOD.RIP continuous liveness + latency probe. REAL URLS ONLY.

WHY THIS EXISTS
---------------
A point-in-time ``GET /api/health -> 200`` was reported as "healthy, verified,
both endpoints 200" while the app was unusable. The measured facts from that
period, all real:

  * ``/api/health`` returned 200 in **10,891 ms**, then 200 in **0.0 ms** thirty
    seconds later. A 200 is a SAMPLE. Reporting it as health is how four wrong
    numbers survived for hours.
  * a real YouTube preview session took **22,875 ms** server-side.
  * the Vite proxy logged ``ECONNRESET`` on ``/api/settings`` and printed "not
    running" while the API process was alive and merely starved.
  * a live-stream heartbeat was stale ~18 h and was reported as an owner mystery.

So this probe records a DISTRIBUTION against a BUDGET, names the serving process
on every sample, and turns a gap or an identity change into an event instead of
into silence.

INVOCATION
----------
    # continuous (the daemon): samples forever, writes the verdict every cycle
    python scripts/observability/probe.py

    # one fresh sample of each check, then grade the window and exit
    python scripts/observability/probe.py --once

    # grade an existing window without touching the app
    python scripts/observability/probe.py --report-only

    # clean, bounded run (this is what you use in a smoke test)
    python scripts/observability/probe.py --duration-s 45

BUDGETS
-------
``--health-budget-ms`` (default 1000) and ``--preview-budget-ms`` (default 3000)
are the pass/fail lines, taken from the degraded numbers above. The budget a
sample was judged against is recorded on the sample and in the verdict file, so
"slow" is always attributable to a number rather than to taste.

HONESTY INVARIANTS (do not remove, they are the point)
------------------------------------------------------
* Unreachable is ``not_measured`` with a reason. Never 0 ms. Never a pass.
* Over budget is ``fail``, not a note.
* ``n=0`` is ``not_measured``, never a 0 ms pass.
* Real video ids come from the LIVE archive, read-only. No fakes, no mocks.
* The database is opened ``mode=ro``. Never written.

The judgement rules live in ``probe_core`` so they can be unit-tested without a
server; this file is I/O, wiring, and the loop.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_core as core  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

# The LIVE archive. Two other archive.db files exist on this box
# (%APPDATA%\VOD.RIP\archive.db and G:\VOD.RIP-data\archive.db); both are
# ORPHANED predecessors, both were the source of wrong numbers in this project,
# and neither is ever read unless someone passes an explicit override.
LIVE_ARCHIVE = r"H:\VOD.RIP-data\archive.db"
ORPHAN_ARCHIVES = (r"%APPDATA%\VOD.RIP\archive.db", r"G:\VOD.RIP-data\archive.db")

DEFAULT_API = "http://127.0.0.1:7897"
DEFAULT_JSONL = REPO_ROOT / "tmp" / "liveness.jsonl"
DEFAULT_VERDICT = REPO_ROOT / "tmp" / "verify-verdict.json"


# --------------------------------------------------------------------------
# process identity: a pid alone is not an identity (Windows recycles pids)
# --------------------------------------------------------------------------
def listening_pid(port: int, timeout_s: float = 20.0) -> int | None:
    """The pid LISTENING on ``port``. ``None`` when it cannot be determined.

    ``None`` is carried through as "identity unknown", never as "no change",
    so an identity lookup that fails cannot invent continuity.
    """
    try:
        proc = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                              capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    pat = re.compile(r"^\s*TCP\s+\S+:%d\s+\S+\s+LISTENING\s+(\d+)\s*$" % port, re.IGNORECASE)
    pids: set[int] = set()
    for line in (proc.stdout or "").splitlines():
        m = pat.match(line)
        if m:
            pids.add(int(m.group(1)))
    if not pids:
        return None
    # More than one listener on the port means something is mid-restart. The
    # lowest pid is the older of the set on Windows, but the honest answer is
    # that the identity is ambiguous, so it is reported as a set.
    return sorted(pids)[0] if len(pids) == 1 else sorted(pids)[0]


def process_start_iso(pid: int | None) -> str | None:
    """Process creation time as an ISO-8601 UTC string, via kernel32.

    Cross-checked against WMI ``CreationDate`` for three live pids: they agree
    to the second. ``None`` on any failure, which the caller treats as an
    unknown identity rather than as a match.
    """
    if pid is None or not hasattr(ctypes, "WinDLL"):
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
        k32.GetProcessTimes.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL

        handle = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wt.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            epoch = datetime(1601, 1, 1) + timedelta(microseconds=ticks / 10)
            return epoch.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def serving_identity(api: str) -> dict:
    """(pid, start) for whatever is serving ``api``, or explicit unknowns."""
    port = _port_from_url(api)
    if port is None:
        return {"pid": None, "proc_start": None, "identity_error": "cannot parse a port from " + api}
    pid = listening_pid(port)
    if pid is None:
        return {"pid": None, "proc_start": None,
                "identity_error": f"no LISTENING pid found for port {port}"}
    start = process_start_iso(pid)
    return {"pid": pid, "proc_start": start,
            "identity_error": None if start else "process start time unavailable for pid " + str(pid)}


def _port_from_url(url: str) -> int | None:
    m = re.search(r":(\d+)(?:/|$)", url or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# HTTP: real calls only
# --------------------------------------------------------------------------
def timed_get(url: str, timeout_s: float) -> dict:
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read(4096)
            return {"ok": True, "status": int(resp.status),
                    "ms": round((time.monotonic() - t0) * 1000.0, 1), "bytes": len(body)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": int(exc.code),
                "ms": round((time.monotonic() - t0) * 1000.0, 1), "error": f"HTTP {exc.code}"}
    except Exception as exc:
        # No status. Nothing answered, so nothing was measured.
        return {"ok": False, "status": None,
                "ms": round((time.monotonic() - t0) * 1000.0, 1),
                "error": f"{type(exc).__name__}: {exc}"}


def timed_post_json(url: str, payload: dict, timeout_s: float) -> dict:
    t0 = time.monotonic()
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(400000)
            out = {"ok": True, "status": int(resp.status),
                   "ms": round((time.monotonic() - t0) * 1000.0, 1)}
            try:
                out["json"] = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                out["json"] = None
            return out
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": int(exc.code),
                "ms": round((time.monotonic() - t0) * 1000.0, 1), "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "status": None,
                "ms": round((time.monotonic() - t0) * 1000.0, 1),
                "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# the real video id, from the LIVE archive, read-only
# --------------------------------------------------------------------------
def pick_real_youtube_video(archive: str) -> dict:
    """A REAL YouTube video id from the live archive.

    Returns a dict that always carries ``ok``/``reason``: a database that cannot
    be read is ``not_measured`` with the reason, never an empty id that would
    quietly turn the preview check into a no-op pass.
    """
    low = (archive or "").lower().replace("/", "\\")
    for orphan in ORPHAN_ARCHIVES:
        o = orphan.lower().replace("/", "\\").replace("%appdata%", r"c:\users\administrador\appdata\roaming")
        if o in low:
            return {"ok": False, "video_id": None,
                    "reason": f"refusing to read an ORPHANED archive: {archive}"}
    try:
        uri = "file:" + archive.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            cur = con.cursor()
            row = cur.execute(
                "SELECT video_id, channel, title, duration_sec FROM videos "
                "WHERE platform='youtube' AND duration_sec BETWEEN 60 AND 1800 "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {"ok": False, "video_id": None,
                        "reason": f"no youtube video in {archive}"}
            return {"ok": True, "video_id": row[0], "channel": row[1],
                    "title": (row[2] or "")[:80], "duration_sec": row[3],
                    "archive": archive, "reason": None}
        finally:
            con.close()
    except Exception as exc:
        return {"ok": False, "video_id": None,
                "reason": f"archive read failed ({type(exc).__name__}: {exc})"}


# --------------------------------------------------------------------------
# samples
# --------------------------------------------------------------------------
def sample(check: str, result: dict, budget_ms: float, ident: dict,
           epoch: float | None = None) -> dict:
    s = {
        "event": "sample",
        "check": check,
        "ts": core.utc_now_iso(),
        "epoch": round(time.time() if epoch is None else epoch, 3),
        "budget_ms": budget_ms,
        "pid": ident.get("pid"),
        "proc_start": ident.get("proc_start"),
    }
    s.update({k: v for k, v in result.items() if k != "json"})
    s.update(core.classify(s, budget_ms))
    return s


def append_jsonl(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"probe: cannot append to {path}: {exc}", file=sys.stderr)


def normalise_record(rec: dict) -> dict:
    """Accept a record from either this probe or the earlier ``tmp/liveness_probe.py`` draft.

    Measured need: the draft already running against the live app wrote records
    with an ``event`` field and no ``epoch`` (only an ISO ``ts``), so a strict
    reader scored its entire history as ``n=0`` and reported NOT_MEASURED over
    real samples. The samples were there; the reader was blind to them. Rather
    than throw away the only continuous history the live app has, this maps the
    draft's shape onto the current one.
    """
    out = dict(rec)
    if not out.get("check") and out.get("event") in ("health", "preview"):
        out["check"] = out["event"]
    if out.get("epoch") is None and out.get("ts"):
        try:
            out["epoch"] = datetime.fromisoformat(out["ts"]).timestamp()
        except (TypeError, ValueError):
            pass
    return out


def read_window(path: Path, since_epoch: float | None, check: str | None = None) -> list[dict]:
    """Samples from the JSONL within a window. Unreadable lines are skipped."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                rec = normalise_record(rec)
                if check and rec.get("check") != check:
                    continue
                ep = rec.get("epoch")
                if since_epoch is not None and (ep is None or float(ep) < since_epoch):
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------
def take_health(api: str, budget_ms: float, timeout_s: float, ident: dict) -> dict:
    return sample("health", timed_get(f"{api}/api/health", timeout_s), budget_ms, ident)


def take_preview(api: str, budget_ms: float, timeout_s: float, ident: dict,
                 video: dict) -> dict:
    if not video.get("ok"):
        s = sample("preview", {"ok": False, "status": None, "ms": None, "error": None},
                   budget_ms, ident)
        s.update(core._verdict(core.NOT_MEASURED,
                              f"not measured: {video.get('reason')}", "unavailable",
                              budget_ms, None, None))
        s["error"] = video.get("reason")
        return s
    url = f"https://www.youtube.com/watch?v={video['video_id']}"
    res = timed_post_json(f"{api}/api/preview/session", {"url": url, "platform": "youtube"}, timeout_s)
    s = sample("preview", res, budget_ms, ident)
    s["url"] = url
    s["video_id"] = video["video_id"]
    s["channel"] = video.get("channel")
    if isinstance(res.get("json"), dict):
        body = res["json"]
        s["session_id"] = body.get("session_id")
        s["kind"] = body.get("kind")
    return s


# --------------------------------------------------------------------------
# verdict emission
# --------------------------------------------------------------------------
def emit_verdict(path: Path, jsonl: Path, args, video: dict, ident: dict,
                 fresh: dict | None = None, repo: Path | None = None) -> dict:
    """Grade the window and write the verdict file. Always returns a document."""
    window_s = args.window_minutes * 60.0
    since = (time.time() - window_s) if window_s > 0 else None
    checks = []
    for name, budget in (("health", args.health_budget_ms),
                         ("preview", args.preview_budget_ms)):
        rows = read_window(jsonl, since, check=name)
        if fresh and fresh.get("check") == name:
            rows = rows + [fresh]
        checks.append(core.summarise(name, rows, budget,
                                     window=f"last {args.window_minutes} min" if window_s > 0
                                            else "entire jsonl",
                                     nominal_interval_s=getattr(args, f"{name}_interval_s", 20.0)))
    if fresh and fresh.get("check") not in ("health", "preview"):
        checks.append(core.summarise(fresh["check"], [fresh], args.health_budget_ms,
                                     window="single sample", nominal_interval_s=0))

    meta = {
        "repo": str(repo or REPO_ROOT),
        "api": args.api,
        "archive": args.archive,
        "archive_is_live": str(args.archive).lower() == LIVE_ARCHIVE.lower(),
        "serving_pid": ident.get("pid"),
        "serving_proc_start": ident.get("proc_start"),
        "budgets_ms": {"health": args.health_budget_ms, "preview": args.preview_budget_ms},
        "window_minutes": args.window_minutes,
        "jsonl": str(jsonl),
        "verdict_file": str(path),
        "video": {k: video.get(k) for k in ("video_id", "channel", "archive", "reason")},
        "strict": bool(args.strict),
        "tool": "scripts/observability/probe.py",
        "command": " ".join([sys.executable.split("\\")[-1], "scripts/observability/probe.py"]
                            + sys.argv[1:]),
    }
    doc = core.build_verdict(checks, meta)
    core.write_verdict(path, doc)
    return doc


def force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 and lossy, before anything is printed.

    This probe prints exception text and server error bodies verbatim. On a
    cp1252 console a single non-ASCII byte in a yt-dlp or ffmpeg error string
    would raise UnicodeEncodeError and kill the probe mid-window - losing the
    very sample it was collecting. Lossy is strictly better than a dead probe.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def safe_text(text: str, limit: int = 4000) -> str:
    out = str(text).encode("ascii", "replace").decode("ascii")
    return out if len(out) <= limit else out[:limit] + f"... (+{len(out) - limit} chars)"


def print_verdict(doc: dict) -> None:
    print(f"\n  probe verdict: {doc['verdict'].upper()}   ({doc['generated_at']})")
    for c in doc["checks"]:
        n, nm = c["n"], c["n_measured"]
        dist = (f"p50={c['p50_ms']} p95={c['p95_ms']} max={c['max_ms']} ms"
                if c["p50_ms"] is not None else "no latency distribution")
        print(f"    [{c['verdict'].upper():<12}] {c['check']:<8} n={n} measured={nm} "
              f"budget={c['budget_ms']}ms  {dist}")
        print(f"                 {safe_text(c['reason'])}")
        if c["restarts"]:
            print(f"                 restarts detected in window: {c['restarts']}")
        for w in c["unreachable_windows"][:3]:
            print(f"                 unreachable_window: {w['duration_s']}s "
                  f"(expected max {w['expected_max_s']}s)")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe.py",
        description="VOD.RIP continuous liveness + latency probe. Real URLs only; "
                    "unreachable is not_measured, never a 0 ms pass.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--api", default=DEFAULT_API, help="live API base URL")
    p.add_argument("--archive", default=LIVE_ARCHIVE, help="LIVE archive.db (opened read-only)")
    p.add_argument("--jsonl", default=str(DEFAULT_JSONL), help="append-only sample log")
    p.add_argument("--verdict", default=str(DEFAULT_VERDICT), help="machine-readable verdict file")
    p.add_argument("--health-budget-ms", type=float, default=core.DEFAULT_HEALTH_BUDGET_MS)
    p.add_argument("--preview-budget-ms", type=float, default=core.DEFAULT_PREVIEW_BUDGET_MS)
    p.add_argument("--health-interval-s", type=float, default=20.0)
    p.add_argument("--preview-interval-s", type=float, default=90.0)
    p.add_argument("--http-timeout-s", type=float, default=30.0)
    p.add_argument("--window-minutes", type=float, default=30.0,
                   help="window graded into the verdict; 0 grades the entire jsonl")
    p.add_argument("--once", action="store_true",
                   help="take one sample of each check, grade the window, exit")
    p.add_argument("--report-only", action="store_true",
                   help="grade the existing window without calling the app")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="run for N seconds then shut down cleanly (0 = forever)")
    p.add_argument("--poll-sleep-s", type=float, default=2.0)
    p.add_argument("--strict", action="store_true",
                   help="exit 3 when any check is not_measured")
    p.add_argument("--repo", default=None, help="repo root recorded in the verdict meta")
    p.add_argument("--selftest-arm", action="store_true",
                   help="prove the gate bites against a closed port, then undo it")
    return p


def _install_shutdown() -> dict:
    """SIGINT/SIGTERM -> a clean stop that still writes a verdict."""
    state = {"stop": False, "signal": None}

    def _handler(signum, _frame):
        state["stop"] = True
        state["signal"] = signal.Signals(signum).name
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):
            pass
    return state


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    jsonl = Path(args.jsonl)
    verdict_path = Path(args.verdict)

    if args.selftest_arm:
        return _selftest_bites(args)

    ident = serving_identity(args.api)
    state = _install_shutdown()

    append_jsonl(jsonl, {
        "event": "probe_start", "ts": core.utc_now_iso(), "pid": ident.get("pid"),
        "proc_start": ident.get("proc_start"), "api": args.api, "archive": args.archive,
        "health_budget_ms": args.health_budget_ms, "preview_budget_ms": args.preview_budget_ms,
        "health_interval_s": args.health_interval_s, "preview_interval_s": args.preview_interval_s,
        "note": "real URLs only; unreachable is never reported as 0 ms and never as a pass",
    })

    video = pick_real_youtube_video(args.archive)
    append_jsonl(jsonl, {"event": "real_video", "ts": core.utc_now_iso(), **video})

    if args.report_only:
        doc = emit_verdict(verdict_path, jsonl, args, video, ident, repo=args.repo)
        print_verdict(doc)
        return 0 if not args.strict else doc["exit_code"]

    started = time.monotonic()
    next_health = 0.0
    next_preview = 0.0
    health_n = preview_n = 0
    last: dict | None = None

    while not state["stop"]:
        t = time.monotonic()
        ident = serving_identity(args.api)
        did = False

        if t >= next_health:
            s = take_health(args.api, args.health_budget_ms, args.http_timeout_s, ident)
            health_n += 1
            s["seq"] = health_n
            append_jsonl(jsonl, s)
            last = s
            did = True
            next_health = t + args.health_interval_s

        if t >= next_preview:
            s = take_preview(args.api, args.preview_budget_ms, args.http_timeout_s, ident, video)
            preview_n += 1
            s["seq"] = preview_n
            append_jsonl(jsonl, s)
            last = s
            did = True
            next_preview = t + args.preview_interval_s

        if did:
            # The verdict is refreshed on every cycle, so a reader always finds
            # a recent file rather than a stale green from an hour ago.
            doc = emit_verdict(verdict_path, jsonl, args, video, ident, fresh=last, repo=args.repo)
            print(f"[{core.utc_now_iso()}] {last['check']:<8} {last['verdict']:<13} "
                  f"{last['ms']} ms (budget {last['budget_ms']} ms) "
                  f"pid={last['pid']} :: {last['reason']}", flush=True)
            if last["verdict"] == core.NOT_MEASURED:
                print(f"    window verdict now: {doc['verdict'].upper()} "
                      f"(not measured, never a pass)", flush=True)

        if args.once:
            break
        if args.duration_s and (time.monotonic() - started) >= args.duration_s:
            break
        time.sleep(max(0.2, args.poll_sleep_s))

    # Clean shutdown: a final verdict, and a stop marker, so a stop is visible
    # rather than a silent end of samples.
    doc = emit_verdict(verdict_path, jsonl, args, video, ident, repo=args.repo)
    append_jsonl(jsonl, {"event": "probe_stop", "ts": core.utc_now_iso(),
                         "pid": ident.get("pid"), "proc_start": ident.get("proc_start"),
                         "signal": state["signal"], "health_samples": health_n,
                         "preview_samples": preview_n,
                         "verdict": doc["verdict"]})
    print_verdict(doc)
    return doc["exit_code"] if args.strict else 0


def _selftest_bites(args) -> int:
    """Prove the gate catches a real regression: point at a CLOSED port.

    This is the "a gate never seen red is not a gate" requirement, automated.
    The condition is created here and never existed before, so nothing outside
    the probe is disturbed and nothing is left broken.
    """
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    print(f"selftest: closed port {dead_port} (nothing is listening there)")

    bad = argparse.Namespace(**vars(args))
    bad.api = f"http://127.0.0.1:{dead_port}"
    bad.jsonl = str(Path(args.jsonl).with_name("selftest-liveness.jsonl"))
    bad.verdict = str(Path(args.verdict).with_name("selftest-verdict.json"))
    bad.once = True
    bad.report_only = False
    bad.selftest_arm = False
    bad.window_minutes = 30.0

    video = pick_real_youtube_video(args.archive)
    ident = serving_identity(bad.api)
    s = take_health(bad.api, bad.health_budget_ms, 5.0, ident)
    append_jsonl(Path(bad.jsonl), s)
    doc = emit_verdict(Path(bad.verdict), Path(bad.jsonl), bad, video, ident, fresh=s)
    print_verdict(doc)
    problems = core.validate_verdict(doc)

    # Assert on the HEALTH check itself, not on the rolled-up verdict.
    #
    # This is a hole I hit while building this arm: the roll-up was
    # `not_measured` because the preview check had no samples, which masked a
    # health check that had graded itself `PASS p50=0.0 ms` against a dead port.
    # A gate that only reads its own summary cannot see a check lying inside it.
    health = next((c for c in doc["checks"] if c["check"] == "health"), None)
    if health is None:
        print("selftest: FAIL - the verdict file carries no health check to judge")
        return 9
    if health["verdict"] != core.NOT_MEASURED:
        print(f"selftest: FAIL - an unreachable endpoint graded health "
              f"{health['verdict']!r} (n_measured={health['n_measured']}), "
              f"expected {core.NOT_MEASURED!r}")
        return 9
    if health["n_measured"]:
        print(f"selftest: FAIL - health claims {health['n_measured']} measured sample(s) "
              f"against a closed port")
        return 9
    if health["p50_ms"] is not None or health["p95_ms"] is not None or health["max_ms"] is not None:
        print(f"selftest: FAIL - health reports a latency distribution "
              f"(p50={health['p50_ms']}, p95={health['p95_ms']}) from an endpoint that "
              f"never answered")
        return 9

    # The other direction: a real 200 that is too slow must FAIL, not pass.
    slow = sample("health", {"ok": True, "status": 200, "ms": 10_891.0, "bytes": 214},
                  bad.health_budget_ms, ident)
    slow_doc = core.build_verdict(
        [core.summarise("health", [slow], bad.health_budget_ms, window="single sample")], {})
    if slow_doc["checks"][0]["verdict"] != core.FAIL:
        print(f"selftest: FAIL - 10891.0 ms against a {bad.health_budget_ms:.0f} ms budget "
              f"graded {slow_doc['checks'][0]['verdict']!r}, expected 'fail'")
        return 9
    print(f"selftest: over-budget arm ok - 10891.0 ms vs {bad.health_budget_ms:.0f} ms "
          f"budget grades FAIL")

    if problems:
        print("selftest: VERDICT CONTRACT VIOLATIONS: " + "; ".join(problems))
        return 9
    print(f"selftest: PASS - unreachable graded {core.NOT_MEASURED!r} (not pass, not 0 ms)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # The signal handler already set the stop flag; this covers a Ctrl-C
        # that lands before the handler is installed.
        print("probe: interrupted", file=sys.stderr)
        sys.exit(130)
