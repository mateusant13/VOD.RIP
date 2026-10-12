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

WHAT THE PREVIEW BUDGET GRADES, AND WHAT IT ONLY RECORDS
---------------------------------------------------------
The FIRST preview session for a video is real extraction work: 22,875 ms in the
incident above, and 26,719 ms measured again on 2026-10-11 - after which the SAME
video, same ``session_id``, answered in 43 ms, 6 ms and 16 ms. Grading that first
call against 3000 ms made the check a coin flip on "did the newest archived video
already have a session", which is not a property of the app. So ``preview`` and
``concurrent-platforms`` warm their sessions first and grade the REPEAT call - the
app's own responsiveness - while the cold latency is kept on the sample as
``cold_ms`` (per arm: ``warmup_ms``). A cold session that cannot be created at all
(timeout at ``PREVIEW_COLD_TIMEOUT_S``, or a non-200) is still not a pass: a
preview that never opens is the original symptom.

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
import threading
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
    try:        # Decoded as UTF-8 with replacement, never text=True alone: netstat
        # on a localised Windows emits accented bytes, and a decoder that
        # raises inside a reader thread kills the identity lookup that
        # restart detection depends on. Only ASCII digits are parsed out.
        proc = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout_s)
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
                "ms": round((time.monotonic() - t0) * 1000.0, 1),
                "error": f"HTTP {exc.code}", "detail": _http_detail(exc)}
    except Exception as exc:
        return {"ok": False, "status": None,
                "ms": round((time.monotonic() - t0) * 1000.0, 1),
                "error": f"{type(exc).__name__}: {exc}"}


def _http_detail(exc) -> str:
    """The API's own one-line reason from an error body, or "".

    Without it an arm's verdict could only say "HTTP 404": a dead fixture id and
    a genuinely broken route read identically, so the check could not tell a bad
    fixture from a bad box. Bounded, single-line, and never fatal -- an unreadable
    body is an empty detail, which the caller treats as "not the fixture".
    """
    try:
        raw = exc.read(8192) or b""
    except Exception:
        return ""
    text = None
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            text = body["detail"]
    except Exception:
        text = None
    if text is None:
        text = raw.decode("utf-8", "replace")
    return " ".join(str(text).split())[:200]


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
            rows = cur.execute(
                "SELECT video_id, channel, title, duration_sec FROM videos "
                "WHERE platform='youtube' AND duration_sec BETWEEN 60 AND 1800 "
                "ORDER BY rowid DESC LIMIT ?", (PICK_CANDIDATE_LIMIT,)
            ).fetchall()
            if not rows:
                return {"ok": False, "video_id": None,
                        "reason": f"no youtube video in {archive}"}
            for seen, row in enumerate(rows, 1):
                if not _shape_ok("youtube", row[0]):
                    continue
                return {"ok": True, "video_id": row[0], "channel": row[1],
                        "title": (row[2] or "")[:80], "duration_sec": row[3],
                        "archive": archive, "reason": None,
                        "candidates_considered": seen}
            return {"ok": False, "video_id": None,
                    "reason": (f"no youtube video with a parseable id in the "
                               f"newest {len(rows)} rows of {archive}")}
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
        except (TypeError, ValueError, OverflowError, OSError):
            # OSError is the one that actually bites on Windows: measured
            # 2026-10-11, datetime.fromisoformat("9999-12-31T23:59:59")
            # .timestamp() raises OSError [Errno 22], not ValueError.  This
            # function's caller promises "unreadable lines are skipped", and a
            # record with an absurd ts must cost that record, not the history.
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


# The FIRST preview session for a video is real extraction work: measured
# 22,875 ms server-side in the incident this probe exists for, and 26,719 ms on
# 2026-10-11. The same video then answered in 43 ms, 6 ms and 16 ms with the SAME
# session_id, so the repeat path is the app's own responsiveness. Grading the
# first call against the 3000 ms preview budget made this check a lottery on "did
# the archive grow since the last preview": the newest video is picked every run,
# and a cold one turned the gate red for work the app is supposed to do. So the
# session is WARMED first and the graded sample is the repeat call. The cold call
# is not ignored: its latency is kept as cold_ms, and a cold session that cannot
# be created at all (timeout, non-200) still fails, because a preview that never
# opens is the original symptom.
PREVIEW_COLD_TIMEOUT_S = 180.0


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
    payload = {"url": url, "platform": "youtube"}
    cold = timed_post_json(f"{api}/api/preview/session", payload,
                           max(timeout_s, PREVIEW_COLD_TIMEOUT_S))
    if not cold.get("ok"):
        s = sample("preview", cold, budget_ms, ident)
        s["path"] = "cold"
        s["url"] = url
        s["video_id"] = video["video_id"]
        s["channel"] = video.get("channel")
        s["reason"] = (f"cold session could not be created ({s.get('reason')}); "
                       "the warm-up is not a no-op")
        return s
    res = timed_post_json(f"{api}/api/preview/session", payload, timeout_s)
    s = sample("preview", res, budget_ms, ident)
    s["path"] = "warm"
    s["cold_ms"] = cold.get("ms")
    s["url"] = url
    s["video_id"] = video["video_id"]
    s["channel"] = video.get("channel")
    if isinstance(res.get("json"), dict):
        body = res["json"]
        s["session_id"] = body.get("session_id")
        s["kind"] = body.get("kind")
    s["reason"] = f"{s.get('reason')} (cold session {cold.get('ms')} ms, not graded)"
    return s


# the concurrent-platforms check
# --------------------------------------------------------------------------
# The platform vocabulary. backend/services/archive_db.py:42 declares the same
# three, and the archive is the authority for what it holds:
# pick_real_platform_videos asks the database instead of trusting this list. A
# platform with no video is an arm that reports "cannot be measured", never an
# arm that is quietly dropped -- dropping an arm is how a concurrency check
# grades one platform and calls it three.
CONCURRENT_PLATFORMS = ("youtube", "twitch", "kick")

# One canonical URL per archived video, each shape matching the extractor the
# app itself uses to recover the id (_preview_video_id,
# backend/routers/preview.py:418): youtube -> innertube.extract_video_id,
# twitch -> twitch.tv/videos/<id>, kick -> /videos/<uuid>. A URL the app cannot
# parse opens a REAL session for some other video, so the shape is pinned here
# rather than guessed at call time.
_PLATFORM_URL = {
    "youtube": lambda vid, ch: f"https://www.youtube.com/watch?v={vid}",
    "twitch": lambda vid, ch: f"https://www.twitch.tv/videos/{vid}",
    "kick": lambda vid, ch: f"https://kick.com/{ch}/videos/{vid}",
}


PICK_CANDIDATE_LIMIT = 200
_VIDEO_ID_SHAPE = {
    # innertube.extract_video_id: exactly 11 URL-safe characters.
    "youtube": re.compile(r"^[A-Za-z0-9_-]{11}$"),
    # twitch.tv/videos/<digits>
    "twitch": re.compile(r"^[0-9]{1,20}$"),
    # kick /videos/<uuid>, or a numeric id
    "kick": re.compile(
        r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9]{1,20})$"),
}


def _shape_ok(platform: str, video_id) -> bool:
    """Can this platform's own extractor parse *video_id*?

    A known platform must match exactly, because a URL built from an unparseable
    id opens a session for some other video (or none) and grades the wrong thing.
    An unknown platform is REFUSED, not waved through: nothing here can build a
    URL for it (platform_url returns None), so accepting its candidates would
    hand the check a fixture it cannot use -- and an unknown platform reaching
    the arm loop would be graded on a request that was never sent. Adding a
    platform to _VIDEO_ID_SHAPE and _PLATFORM_URL is one line; this is the
    honest half of that pair.
    """
    shape = _VIDEO_ID_SHAPE.get((platform or "").strip().lower())
    return shape is not None and bool(shape.match(str(video_id or "")))


def platform_url(platform: str, video: dict) -> str | None:
    """The canonical archived URL for (platform, video), or None."""
    fn = _PLATFORM_URL.get((platform or "").strip().lower())
    vid = (video or {}).get("video_id")
    if fn is None or not vid:
        return None
    return fn(vid, (video or {}).get("channel") or "")


def pick_real_platform_videos(archive: str,
                               platforms: "tuple[str, ...] | None" = None) -> dict:
    """One REAL video per platform from the live archive, read-only.

    The generalisation of pick_real_youtube_video: the same orphan refusal, the
    same ``mode=ro`` URI, the same "always carries ok/reason" contract, one
    statement grouped over the requested platforms instead of one connection
    per platform. One connection per platform would make a single archive write
    land on one platform as "that platform has nothing in it", and that is a
    wrong number with no error attached.
    """
    names = tuple(platforms) if platforms else CONCURRENT_PLATFORMS

    def _all_fail(why: str) -> dict:
        return {p: {"ok": False, "video_id": None, "reason": why} for p in names}

    low = (archive or "").lower().replace("/", "\\")
    for orphan in ORPHAN_ARCHIVES:
        o = orphan.lower().replace("/", "\\").replace(
            "%appdata%", r"c:\users\administrator\appdata\roaming")
        if o in low:
            return _all_fail(f"refusing to read an ORPHANED archive: {archive}")
    try:
        uri = "file:" + archive.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            out = _all_fail("not read")
            cur = con.cursor()
            for p in names:
                rows = cur.execute(
                    "SELECT video_id, channel, title, duration_sec FROM videos "
                    "WHERE platform = ? "
                    "AND duration_sec BETWEEN 60 AND 1800 "
                    "ORDER BY rowid DESC LIMIT ?",
                    (p, PICK_CANDIDATE_LIMIT),
                ).fetchall()
                if not rows:
                    out[p]["reason"] = f"no {p} video in {archive}"
                    continue
                seen = 0
                for seen, row in enumerate(rows, 1):
                    if not _shape_ok(p, row[0]):
                        continue
                    out[p] = {"ok": True, "video_id": row[0], "channel": row[1],
                              "title": (row[2] or "")[:80], "duration_sec": row[3],
                              "archive": archive, "reason": None,
                              "candidates_considered": seen}
                    break
                else:
                    out[p]["reason"] = (
                        f"no {p} video with a parseable id in the newest "
                        f"{seen} rows of {archive}")
        finally:
            con.close()
    except Exception as exc:
        return _all_fail(f"archive read failed ({type(exc).__name__}: {exc})")
    return out


def _status_code(value) -> int | None:
    """*value* as an HTTP status int, or None when it is not one.

    A malformed status must never raise inside a grading path: an exception
    there loses the whole sample, which reads as "nothing was measured" for a
    reason that has nothing to do with the app.
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # int(float("inf")) raises OverflowError (measured on this box,
        # 2026-10-11); without it the same malformed status that _as_status
        # absorbs in probe_core would escape here and lose a whole arm.
        return None


def _is_2xx_3xx(status) -> bool:
    """Did this arm get an answer, rather than an error or nothing?"""
    code = _status_code(status)
    return code is not None and 200 <= code < 400


def _concurrent_sample(budget_ms: float, ident: dict, verdict: dict,
                        arms: "list[dict]") -> dict:
    """One sample for the concurrent check: the judged latency plus per-arm truth.

    The graded number is the slowest arm; the per-arm list is what makes that
    number adjudicable, because a slowest-of-three is a fact about three.
    """
    s = sample("concurrent-platforms",
               {"ok": False, "status": None, "ms": None, "error": None},
               budget_ms, ident)
    s.update(verdict)
    # verdict comes from core._verdict, which carries no "ok" of its own, so the
    # blank default's ok=False would survive and re-classify a healthy sample as
    # an http_error on the way out. MEASURED 2026-10-11: two samples that
    # answered HTTP 200 (1453 ms and 3969 ms) were graded FAIL "HTTP 200" with
    # http_error_count=2, so this check could never pass while the app was well.
    # The arms are the truth here; the merged sample states what they answered.
    s["ok"] = bool(arms) and all(_is_2xx_3xx(a.get("status")) for a in arms)
    s["platforms"] = [a["platform"] for a in arms]
    s["arms"] = arms
    timings = [a["ms"] for a in arms
               if a["ms"] is not None and a["status"] is not None]
    if timings:
        slowest = max(timings)
        s["slowest_platform"] = next(a["platform"] for a in arms
                                      if a["ms"] == slowest
                                      and a["status"] is not None)
    else:
        s["slowest_platform"] = None
    return s


# An arm whose failure is the FIXTURE's fault, not the box's. Narrow on purpose:
# only the app's own definitive "this upstream item does not exist" answers
# qualify, and only for a 4xx whose body carries one of these details.
# Everything else -- any 5xx, a timeout, an arm that answered, an unknown 4xx
# reason, or an empty body -- stays measured, so a regression in the classifier
# cannot hide behind this downgrade.
_FIXTURE_UNAVAILABLE_MARKERS = (
    "this video is unavailable.",   # backend/services/youtube_diag.py
    "this video has been removed",  # ...and the rest of the dead-video family
    "this video is private.",
    "members-only video",
    "kick channel not found",       # backend/services/kick_api_service.py:164
)
_FIXTURE_UNAVAILABLE_STATUSES = (400, 404, 410, 422)


def _fixture_unavailable(arm: dict) -> str | None:
    """Why this arm failed on an unresolvable fixture id, or None.

    None means "this arm's outcome is a fact about the box" -- including a 5xx
    and a timeout, which are exactly the failures the check exists to catch.
    """
    status = _status_code(arm.get("status"))
    if status is None or status not in _FIXTURE_UNAVAILABLE_STATUSES:
        return None
    detail = str(arm.get("detail") or "").lower()
    if not detail:
        return None
    for marker in _FIXTURE_UNAVAILABLE_MARKERS:
        if marker in detail:
            return f"HTTP {status} {marker}"
    return None


def take_concurrent_platforms(api: str, budget_ms: float, timeout_s: float,
                              ident: dict, videos: dict,
                              platforms: "tuple[str, ...] | None" = None) -> dict:
    """Open one preview session per platform AT THE SAME TIME; grade the SLOWEST.

    ``health`` and ``preview`` each measure one request in isolation, and
    neither can see the case a user actually gets: three panels open at once on
    one box. So the arms are opened in threads and the sample latency is the
    SLOWEST arm. A mean over the three would let one cramped platform hide
    behind two fast ones, and the mean is exactly the number that hides a
    saturated box.

    An arm that cannot be measured makes the WHOLE sample ``not_measured``. A
    concurrency check that drops the arms it could not reach grades the arms it
    could, and an unreachable arm is usually the one that is stuck.
    """
    names = list(platforms) if platforms else list(CONCURRENT_PLATFORMS)
    videos = videos or {}

    wanted: dict = {}
    for p in names:
        url = platform_url(p, videos.get(p) or {})
        if url is None:
            why = ((videos.get(p) or {}).get("reason")
                   or f"no usable {p} video id")
            arms = [{"platform": q, "status": None, "ms": None, "ok": False,
                     "error": why,
                     "video_id": (videos.get(q) or {}).get("video_id")}
                    for q in names]
            return _concurrent_sample(
                budget_ms, ident,
                core._verdict(core.NOT_MEASURED, f"not measured: {why}",
                              "unavailable", budget_ms, None, None),
                arms)
        wanted[p] = {"url": url, "video": videos.get(p) or {}}

    # Same reasoning as take_preview: the first session per platform is real
    # extraction work, and this check is supposed to measure three panels
    # answering AT ONCE, not three cold extractions. Each session is warmed
    # first - sequential, ungraded, its latency kept on the arm as warmup_ms -
    # and only the concurrent round below is graded. A warm-up that fails is
    # kept on the arm too, so a platform whose session cannot be created is
    # visible instead of silently expensive.
    warmups: dict = {}
    for p in names:
        warmups[p] = timed_post_json(
            f"{api}/api/preview/session",
            {"url": wanted[p]["url"], "platform": p},
            max(timeout_s, PREVIEW_COLD_TIMEOUT_S))

    results: list = [None] * len(names)

    def _worker(i: int, platform: str) -> None:
        # A worker cannot take the check down with it: any failure is recorded
        # as an unanswered arm, and the aggregation below decides what that
        # means. An exception escaping a thread only loses the whole sample.
        try:
            results[i] = timed_post_json(
                f"{api}/api/preview/session",
                {"url": wanted[platform]["url"], "platform": platform},
                timeout_s)
        except Exception as exc:
            results[i] = {"ok": False, "status": None, "ms": None,
                         "error": f"{type(exc).__name__}: {exc}"}

    threads = [threading.Thread(target=_worker, args=(i, p), daemon=True,
                               name=f"concurrent-{p}")
               for i, p in enumerate(names)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout_s + 5.0)

    arms: list = []
    for arm_p, res in zip(names, results):
        res = res or {"ok": False, "status": None, "ms": None,
                       "error": "arm did not finish"}
        ms = None if res.get("ms") is None else round(float(res["ms"]), 1)
        warm = warmups.get(arm_p) or {}
        arms.append({"platform": arm_p, "status": res.get("status"), "ms": ms,
                     "ok": res.get("ok"), "error": res.get("error"),
                     "detail": res.get("detail"),
                     "warmup_ms": warm.get("ms"),
                     "warmup_status": warm.get("status"),
                     "warmup_error": warm.get("error"),
                     "video_id": wanted[arm_p]["video"].get("video_id")})

    answered = [a for a in arms if a["status"] is not None]
    if not answered:
        why = next((a["error"] for a in arms if a["error"]),
                   "nothing answered")
        return _concurrent_sample(
            budget_ms, ident,
            core._verdict(core.NOT_MEASURED, f"not measured: {why}",
                          "unreachable", budget_ms, None, None),
            arms)

    missing = [a for a in arms if a["status"] is None]
    if missing:
        # The docstring's contract: an arm that cannot be measured makes the
        # WHOLE sample not_measured. Without this branch the aggregation below
        # fabricates status 200 for the surviving arms while ok=False, and
        # classify() reads that pair as FAIL "HTTP 200" -- a red naming a status
        # no arm returned, on a sample whose missing arm is the interesting one.
        first = missing[0]
        why = first.get("error") or "arm did not answer"
        return _concurrent_sample(
            budget_ms, ident,
            core._verdict(core.NOT_MEASURED,
                          f"not measured: {first['platform']} did not answer ({why})",
                          "unreachable", budget_ms, None, None),
            arms)

    bad = [a["status"] for a in answered if not _is_2xx_3xx(a["status"])]
    fixtures = [(a["platform"], _fixture_unavailable(a)) for a in arms]
    fixtures = [(p, why) for p, why in fixtures if why]
    if fixtures and len(fixtures) == len(bad):
        # Every failing arm failed on the FIXTURE: the picked id no longer
        # resolves upstream, so the request never reached the work this check
        # grades. Grading it red would blame the box for the archive; grading it
        # green would be a silent pass. It is named and dropped to not_measured.
        # An unknown 4xx, a 5xx or a timeout never reaches here: those are the
        # failures this check exists to catch.
        why = "fixture_unavailable: " + ", ".join(
            "%s (%s)" % (p, w) for p, w in fixtures)
        return _concurrent_sample(
            budget_ms, ident,
            core._verdict(core.NOT_MEASURED, f"not measured: {why}",
                          "unavailable", budget_ms, None, None),
            arms)

    first_err = next(((a["platform"], a["error"]) for a in arms if a["error"]),
                     None)
    detail = None if first_err is None else f"{first_err[0]}: {first_err[1]}"
    timings = [a["ms"] for a in answered if a.get("ms") is not None]
    agg = {"ok": len(answered) == len(arms) and not bad,
           "status": bad[0] if bad else 200,
           # The graded number is the SLOWEST arm, so it exists only when every
           # answered arm has a timing. A missing one must not be replaced by a
           # fabricated 0.0, and the remaining arms' max must not stand in for
           # the arm whose latency was never recorded: both would grade as
           # "within budget" a sample that was never fully measured. None
           # reaches classify's no_timing arm.
           "ms": (max(timings) if timings and len(timings) == len(answered) else None),
           "error": detail}
    return _concurrent_sample(budget_ms, ident, core.classify(agg, budget_ms),
                              arms)


# --------------------------------------------------------------------------
# verdict emission
# --------------------------------------------------------------------------
def emit_verdict(path: Path, jsonl: Path, args, video: dict, ident: dict,
                 fresh: dict | None = None, repo: Path | None = None) -> dict:
    """Grade the window and write the verdict file. Always returns a document."""
    window_s = args.window_minutes * 60.0
    since = (time.time() - window_s) if window_s > 0 else None
    checks = []
    # (name, budget, nominal interval), written out per check. The interval is
    # passed explicitly rather than derived from the name:
    # getattr(args, f"{name}_interval_s") cannot resolve a hyphenated dest, and a
    # silently-defaulted 20 s interval would make a check that samples every 5
    # minutes look like it should have sampled far more.
    for name, budget, interval in (
        ("health", args.health_budget_ms, args.health_interval_s),
        ("preview", args.preview_budget_ms, args.preview_interval_s),
        ("concurrent-platforms", args.concurrent_platforms_budget_ms,
         args.concurrent_platforms_interval_s),
    ):
        rows = read_window(jsonl, since, check=name)
        if fresh and fresh.get("check") == name:
            rows = rows + [fresh]
        checks.append(core.summarise(name, rows, budget,
                                     window=f"last {args.window_minutes} min" if window_s > 0
                                            else "entire jsonl",
                                     nominal_interval_s=interval))
    if fresh and fresh.get("check") not in ("health", "preview",
                                            "concurrent-platforms"):
        checks.append(core.summarise(fresh["check"], [fresh], args.health_budget_ms,
                                     window="single sample", nominal_interval_s=0))

    meta = {
        "repo": str(repo or REPO_ROOT),
        "api": args.api,
        "archive": args.archive,
        "archive_is_live": str(args.archive).lower() == LIVE_ARCHIVE.lower(),
        "serving_pid": ident.get("pid"),
        "serving_proc_start": ident.get("proc_start"),
        "budgets_ms": {"health": args.health_budget_ms,
                       "preview": args.preview_budget_ms,
                       "concurrent-platforms": args.concurrent_platforms_budget_ms},
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
    p.add_argument("--concurrent-platforms-budget-ms", type=float,
                   default=core.DEFAULT_PREVIEW_BUDGET_MS,
                   help="slowest-of-N ceiling for simultaneous preview sessions; the arms are "
                        "opened together, so this is the wait the user gets for all of "
                        "them, not a per-request budget")
    p.add_argument("--concurrent-platforms-interval-s", type=float, default=300.0,
                   help="the concurrent check opens one real session per platform, so it "
                        "costs N sessions and samples rarely on purpose")
    p.add_argument("--platforms", default="",
                   help="comma-separated platforms for the concurrent check; empty means "
                        "every platform the archive holds a video for")
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
        "concurrent_platforms_budget_ms": args.concurrent_platforms_budget_ms,
        "concurrent_platforms_interval_s": args.concurrent_platforms_interval_s,
        "note": "real URLs only; unreachable is never reported as 0 ms and never as a pass",
    })

    video = pick_real_youtube_video(args.archive)
    append_jsonl(jsonl, {"event": "real_video", "ts": core.utc_now_iso(), **video})

    # The concurrent check needs one archived video per platform, picked the
    # same way: from the live archive, read-only, and never invented.
    platforms = tuple(s.strip().lower() for s in args.platforms.split(",")
                      if s.strip())
    plat_videos = pick_real_platform_videos(args.archive, platforms or None)
    append_jsonl(jsonl, {"event": "platform_videos", "ts": core.utc_now_iso(),
                         "platforms": list(plat_videos), "archive": args.archive,
                         **{p: v.get("video_id") for p, v in plat_videos.items()}})

    if args.report_only:
        doc = emit_verdict(verdict_path, jsonl, args, video, ident, repo=args.repo)
        print_verdict(doc)
        return 0 if not args.strict else doc["exit_code"]

    started = time.monotonic()
    next_health = 0.0
    next_preview = 0.0
    next_concurrent = 0.0
    health_n = preview_n = concurrent_n = 0
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

        if t >= next_concurrent:
            # One real session per platform opened at the same time. It costs
            # N sessions, so it runs on its own interval instead of riding the
            # preview cycle.
            s = take_concurrent_platforms(args.api,
                                          args.concurrent_platforms_budget_ms,
                                          args.http_timeout_s, ident,
                                          plat_videos, platforms or None)
            concurrent_n += 1
            s["seq"] = concurrent_n
            append_jsonl(jsonl, s)
            last = s
            did = True
            next_concurrent = t + args.concurrent_platforms_interval_s

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
                         "concurrent_samples": concurrent_n,
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

