"""Judgement layer for the VOD.RIP observability probe.

Pure logic. The only I/O is writing/reading the verdict file. Every decision the
probe makes lives here so it can be unit-tested without a server, a network, or a
clock, which is the only way to prove a gate bites before trusting it on a live app.

THE DEFECT THIS EXISTS TO PREVENT
---------------------------------
A point-in-time ``GET /api/health -> 200`` was reported as "healthy, verified,
both endpoints 200" while the app was unusable. The same call measured 10,891 ms
on one poll and 0.0 ms thirty seconds later. A single 200 is a SAMPLE, not a
measurement, and the difference between those two is the whole bug.

So three rules, enforced here rather than in prose:

1. A point-in-time 200 is not health. ``summarise`` refuses to grade a check from
   one sample without saying so; the population ``n`` and the window travel with
   every verdict.
2. Unreachable is its own state. A transport failure has no ``status``, therefore
   no measurement exists, therefore the verdict is ``not_measured`` - never
   ``pass``, never a 0 ms stand-in. A 0 ms pass is the exact shape of the lie this
   module refuses to tell.
3. Over budget is a FAILURE, not an observation. Latency has a budget, the budget
   is recorded next to the verdict, and exceeding it fails.

``not_measured`` is a first-class verdict, not an error and not a pass. A gate that
cannot distinguish "green" from "never ran" cannot catch anything.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# The three verdicts. Anything outside this set is a bug in the caller, and
# `validate_verdict` treats it as one.
PASS = "pass"
FAIL = "fail"
NOT_MEASURED = "not_measured"
VERDICTS = frozenset({PASS, FAIL, NOT_MEASURED})

# Worst-of ordering. A single fail outranks any number of not_measured, and
# not_measured outranks pass, so the rolled-up verdict can never be greener than
# its weakest member.
SEVERITY = {PASS: 0, NOT_MEASURED: 1, FAIL: 2}

VERDICT_SCHEMA = "vodrip.verify.verdict/1"

# Latency budgets, chosen from the failure this exists to prevent:
#   - /api/health measured 10,891 ms on a live-but-starved app; a healthy warm
#     call on loopback measures 0-15 ms. 1000 ms leaves ~60x headroom over the
#     warm floor, so a 1 s call is a real signal and not jitter.
#   - a real YouTube preview session measured 22,875 ms server-side while
#     degraded, and 32-922 ms warm. 3000 ms is above the warm distribution and
#     far below the degraded one.
DEFAULT_HEALTH_BUDGET_MS = 1000.0
DEFAULT_PREVIEW_BUDGET_MS = 3000.0

# A gap is only an `unreachable_window` if it is longer than the poll interval
# could explain. Three missed intervals, and never less than interval + 1 s, so a
# slow-but-working probe is never accused of an outage.
DEFAULT_GAP_FACTOR = 3.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# latency
# --------------------------------------------------------------------------
def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile. ``None`` for an empty population.

    ``None`` and not ``0.0``: an empty population has no percentile, and
    returning 0.0 would make "nothing measured" look like "measured instantly".
    That confusion is the bug this module exists to prevent, so it is closed here
    rather than left to each caller.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    k = (len(vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return float(vals[lo] + (vals[hi] - vals[lo]) * (k - lo))


# --------------------------------------------------------------------------
# per-sample judgement
# --------------------------------------------------------------------------
def classify(sample: dict | None, budget_ms: float | None = None) -> dict:
    """Grade ONE sample against a budget. Never raises.

    Returns ``{verdict, reason, reason_class, budget_ms, ms, status}``.

    The three-way split that matters:

    * ``status is None``  -> transport failed, nothing answered. ``not_measured``.
      This is the branch that must never be allowed to read as a pass.
    * ``status >= 400``   -> something answered, and the answer was an error.
      ``fail``: a measurement exists and it is bad.
    * ``200 <= status < 400 and ms > budget`` -> ``fail``: over budget is a
      failure, not a note.
    """
    if sample is None:
        return _verdict(NOT_MEASURED, "no sample was collected", "absent", budget_ms, None, None)

    status = sample.get("status")
    ms = sample.get("ms")
    err = sample.get("error")
    ok = sample.get("ok")

    if status is None:
        detail = err or "no HTTP status: the endpoint did not answer"
        return _verdict(NOT_MEASURED, f"unreachable: {detail}", "unreachable", budget_ms, ms, None)

    if ok is False or not (200 <= int(status) < 400):
        return _verdict(FAIL, f"HTTP {status}" + (f" ({err})" if err else ""),
                        "http_error", budget_ms, ms, int(status))

    if ms is None:
        return _verdict(NOT_MEASURED, f"HTTP {status} answered but no timing was recorded",
                        "no_timing", budget_ms, None, int(status))

    if budget_ms is not None and ms > budget_ms:
        return _verdict(FAIL, f"{ms:.1f} ms over the {budget_ms:.0f} ms budget",
                        "over_budget", budget_ms, ms, int(status))

    where = f"within the {budget_ms:.0f} ms budget" if budget_ms is not None else "answered"
    return _verdict(PASS, f"HTTP {status} in {ms:.1f} ms, {where}",
                    "within_budget", budget_ms, ms, int(status))


def _verdict(verdict: str, reason: str, reason_class: str,
             budget_ms: float | None, ms: float | None, status: int | None) -> dict:
    return {
        "verdict": verdict,
        "reason": reason,
        "reason_class": reason_class,
        "budget_ms": budget_ms,
        "ms": None if ms is None else round(float(ms), 1),
        "status": status,
    }


# --------------------------------------------------------------------------
# process identity, restarts, and gaps
# --------------------------------------------------------------------------
def identity(sample: dict | None) -> tuple[Any, Any] | None:
    """The (pid, start_time) pair that names a serving process.

    A pid alone is not an identity: Windows recycles pids. The pair is the
    identity, so a recycled pid reads as a restart rather than as continuity.
    ``None`` when either half is missing - an unknown identity is not a
    continuity claim.
    """
    if not sample:
        return None
    pid, start = sample.get("pid"), sample.get("proc_start")
    if pid is None or start is None:
        return None
    return (pid, start)


def detect_events(prev: dict | None, cur: dict | None, nominal_interval_s: float,
                  gap_factor: float = DEFAULT_GAP_FACTOR) -> list[dict]:
    """Events between two consecutive samples.

    * ``restart``          - the serving process identity changed.
    * ``unreachable_window`` - the gap is longer than the poll interval can
      explain, with the duration attached. Its absence while the app was down
      is how an 18-hour stale heartbeat was reported as an owner mystery.
    * ``unreachable`` / ``recovered`` - the pass/unreachable edges.
    """
    if not prev or not cur:
        return []
    events: list[dict] = []

    prev_epoch, cur_epoch = prev.get("epoch"), cur.get("epoch")
    if prev_epoch is not None and cur_epoch is not None:
        gap = float(cur_epoch) - float(prev_epoch)
        threshold = max(nominal_interval_s * gap_factor, nominal_interval_s + 1.0)
        if gap > threshold:
            events.append({
                "event": "unreachable_window",
                "duration_s": round(gap, 3),
                "expected_max_s": round(threshold, 3),
                "from_ts": prev.get("ts"),
                "to_ts": cur.get("ts"),
            })

    pv = (prev.get("verdict") or classify(prev, cur.get("budget_ms"))["verdict"])
    cv = (cur.get("verdict") or classify(cur, cur.get("budget_ms"))["verdict"])
    if pv == PASS and cv == NOT_MEASURED:
        events.append({"event": "unreachable", "ts": cur.get("ts"),
                       "reason": cur.get("reason") or classify(cur, cur.get("budget_ms"))["reason"]})
    if pv == NOT_MEASURED and cv == PASS:
        events.append({"event": "recovered", "ts": cur.get("ts")})

    pi, ci = identity(prev), identity(cur)
    if pi and ci and pi != ci:
        events.append({
            "event": "restart",
            "from": {"pid": pi[0], "proc_start": pi[1]},
            "to": {"pid": ci[0], "proc_start": ci[1]},
            "gap_s": (None if prev_epoch is None or cur_epoch is None
                      else round(float(cur_epoch) - float(prev_epoch), 3)),
        })
    return events


def fold_events(samples: Sequence[dict], nominal_interval_s: float,
                gap_factor: float = DEFAULT_GAP_FACTOR) -> list[dict]:
    out: list[dict] = []
    for prev, cur in zip(samples, samples[1:]):
        out.extend(detect_events(prev, cur, nominal_interval_s, gap_factor))
    return out


# --------------------------------------------------------------------------
# the distribution, which is the product
# --------------------------------------------------------------------------
def summarise(name: str, samples: Sequence[dict], budget_ms: float | None,
              window: str, nominal_interval_s: float = 0.0,
              gap_factor: float = DEFAULT_GAP_FACTOR) -> dict:
    """Summarise a check over a window. The distribution IS the result.

    A single sample yields a verdict, but the verdict carries ``n=1`` and the
    caller is expected to say so; nothing here upgrades one sample into a
    health claim. Latency percentiles are computed only over samples that
    actually answered, so an unreachable attempt never contributes a 0 ms.
    """
    samples = list(samples)
    n = len(samples)
    graded = [classify(s, s.get("budget_ms", budget_ms)) for s in samples]
    events = fold_events(samples, nominal_interval_s, gap_factor) if nominal_interval_s > 0 else []

    measured = [g for g in graded if g["reason_class"] != "unreachable"]
    lats = [g["ms"] for g in measured if g["ms"] is not None]

    check: dict[str, Any] = {
        "check": name,
        "verdict": NOT_MEASURED,
        "reason": "",
        "reason_class": "",
        "budget_ms": budget_ms,
        "window": window,
        "n": n,
        "n_measured": len(measured),
        "pass_count": sum(1 for g in graded if g["verdict"] == PASS),
        "fail_count": sum(1 for g in graded if g["verdict"] == FAIL),
        "not_measured_count": sum(1 for g in graded if g["verdict"] == NOT_MEASURED),
        "p50_ms": _r(percentile(lats, 50)),
        "p95_ms": _r(percentile(lats, 95)),
        "max_ms": _r(max(lats)) if lats else None,
        "over_budget_count": sum(1 for g in graded if g["reason_class"] == "over_budget"),
        "unreachable_count": sum(1 for g in graded if g["reason_class"] == "unreachable"),
        "http_error_count": sum(1 for g in graded if g["reason_class"] == "http_error"),
        "restarts": sum(1 for e in events if e["event"] == "restart"),
        "unreachable_windows": [e for e in events if e["event"] == "unreachable_window"],
        "failures": [],
        "failures_truncated": 0,
    }

    # Bounded failure list, oldest first, so a long window cannot produce a
    # verdict file nobody reads.
    fail_pairs = [(s, g) for s, g in zip(samples, graded) if g["verdict"] == FAIL]
    check["failures"] = [
        {"ts": s.get("ts"), "reason": g["reason"], "ms": g["ms"], "status": g["status"]}
        for s, g in fail_pairs[:5]
    ]
    check["failures_truncated"] = max(0, len(fail_pairs) - 5)

    if n == 0:
        check["reason"] = f"not measured: no {name} samples in the {window} window"
        check["reason_class"] = "no_population"
    elif not measured:
        check["reason"] = (f"not measured: {n} {name} attempt(s) in the {window} window, "
                           f"0 of them reached the endpoint")
        check["reason_class"] = "all_unreachable"
    elif fail_pairs:
        first = fail_pairs[0][1]
        extra = f" (+{len(fail_pairs) - 1} more)" if len(fail_pairs) > 1 else ""
        check["verdict"] = FAIL
        check["reason"] = f"{len(fail_pairs)}/{n} over or failing: {first['reason']}{extra}"
        check["reason_class"] = first["reason_class"]
    else:
        check["verdict"] = PASS
        check["reason"] = (f"{len(measured)}/{n} answered within budget; "
                           f"p50 {_r(check['p50_ms'])} ms, p95 {_r(check['p95_ms'])} ms, "
                           f"max {_r(check['max_ms'])} ms")
        check["reason_class"] = "within_budget"

    if len(samples) == 1 and check["verdict"] == PASS:
        # A point-in-time 200, named as such, so it can never be read as health.
        check["reason"] += " - SINGLE SAMPLE, this is a point-in-time reading, not a distribution"
        check["single_sample"] = True
    else:
        check["single_sample"] = False

    check["restarts"] = sum(1 for e in events if e["event"] == "restart")
    return check


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 1)


# --------------------------------------------------------------------------
# the machine-readable verdict file
# --------------------------------------------------------------------------
def build_verdict(checks: Sequence[dict], meta: dict | None = None) -> dict:
    checks = [dict(c) for c in checks]
    totals = {
        PASS: sum(1 for c in checks if c.get("verdict") == PASS),
        FAIL: sum(1 for c in checks if c.get("verdict") == FAIL),
        NOT_MEASURED: sum(1 for c in checks if c.get("verdict") == NOT_MEASURED),
    }
    overall = PASS
    for c in checks:
        if SEVERITY.get(c.get("verdict"), 2) > SEVERITY[overall]:
            overall = c.get("verdict")
    doc = {
        "schema": VERDICT_SCHEMA,
        "generated_at": utc_now_iso(),
        "meta": meta or {},
        "checks": checks,
        "totals": totals,
        "verdict": overall,
        "not_measured": [c["check"] for c in checks if c.get("verdict") == NOT_MEASURED],
        "failed": [c["check"] for c in checks if c.get("verdict") == FAIL],
    }
    doc["exit_code"] = gate_exit_code(checks, strict=bool((meta or {}).get("strict")))
    return doc


def write_verdict(path: str | os.PathLike, verdict: dict) -> Path:
    """Write the verdict file atomically, so a reader never sees a half file.

    A truncated verdict JSON would be a gate that silently stops being readable
    at exactly the moment it is being read.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(verdict, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def load_verdict(path: str | os.PathLike) -> dict | None:
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def validate_verdict(doc: Any) -> list[str]:
    """Return a list of contract violations. Empty list means well-formed.

    This is the machine check that ``pass`` and ``not_measured`` are genuinely
    different things in the file, and that a check that never ran cannot be
    recorded as a pass. The tests drive it directly.
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["verdict document is not an object"]
    if doc.get("schema") != VERDICT_SCHEMA:
        problems.append(f"schema is {doc.get('schema')!r}, expected {VERDICT_SCHEMA!r}")
    checks = doc.get("checks")
    if not isinstance(checks, list) or not checks:
        return problems + ["verdict document carries no checks"]

    seen: set[str] = set()
    for c in checks:
        if not isinstance(c, dict):
            problems.append("a check entry is not an object")
            continue
        name = str(c.get("check"))
        if name in seen:
            problems.append(f"duplicate check {name!r}")
        seen.add(name)
        v = c.get("verdict")
        if v not in VERDICTS:
            problems.append(f"{name}: verdict {v!r} is not one of {sorted(VERDICTS)}")
            continue
        if not str(c.get("reason") or "").strip():
            problems.append(f"{name}: a verdict without a reason is not a verdict")
        if not isinstance(c.get("n"), int) or c.get("n") < 0:
            problems.append(f"{name}: population n is {c.get('n')!r}, expected a count >= 0")
        if v == NOT_MEASURED:
            if c.get("n_measured"):
                problems.append(f"{name}: not_measured but n_measured={c.get('n_measured')}")
            if c.get("p50_ms") is not None or c.get("p95_ms") is not None:
                problems.append(f"{name}: not_measured but carries latency percentiles")
        if v == PASS and c.get("n_measured", 0) < 1:
            problems.append(f"{name}: pass with n_measured={c.get('n_measured')} - "
                            "an unmeasured check cannot be a pass")
        if v == FAIL and c.get("n_measured", 0) < 1 and c.get("reason_class") != "absent":
            problems.append(f"{name}: fail with nothing measured and reason_class="
                            f"{c.get('reason_class')!r}")
    totals = doc.get("totals")
    if isinstance(totals, dict):
        for key in (PASS, FAIL, NOT_MEASURED):
            want = sum(1 for c in checks if isinstance(c, dict) and c.get("verdict") == key)
            if totals.get(key) != want:
                problems.append(f"totals[{key}]={totals.get(key)!r} but {want} checks carry it")
    if doc.get("verdict") in (FAIL, NOT_MEASURED):
        listed = doc.get("not_measured")
        if not isinstance(listed, list):
            problems.append("not_measured roll-up list is missing or not a list")
        else:
            for c in checks:
                if isinstance(c, dict) and c.get("verdict") == NOT_MEASURED \
                        and c.get("check") not in listed:
                    problems.append(f"{c.get('check')}: a not_measured check is missing from "
                                    f"the not_measured roll-up list")
    failed_list = doc.get("failed")
    if not isinstance(failed_list, list):
        problems.append("failed roll-up list is missing or not a list")
    else:
        for c in checks:
            if isinstance(c, dict) and c.get("verdict") == FAIL and c.get("check") not in failed_list:
                problems.append(f"{c.get('check')}: a failed check is missing from the "
                                f"failed roll-up list")
    return problems


def gate_exit_code(checks: Iterable[dict], strict: bool = False) -> int:
    """0 all pass · 1 any fail · 3 strict and something not_measured.

    ``not_measured`` is 0 by default so an unreachable app is not reported as a
    broken build, but it is never silently a pass: it is printed as
    NOT_MEASURED, listed in the verdict file, and ``--strict`` turns it red.
    """
    checks = list(checks)
    if any(c.get("verdict") == FAIL for c in checks):
        return 1
    if strict and any(c.get("verdict") == NOT_MEASURED for c in checks):
        return 3
    return 0
