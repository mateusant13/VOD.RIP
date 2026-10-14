"""LAW: probe-concurrent-platforms-printout-is-measured.

The invariant
------------
A check that cannot reach its subject must print not_measured, and the printed
verdict must be the verdict that the samples it stored actually imply.

The concurrent-platforms check opens one preview session per platform at the
same time and grades the SLOWEST arm, which makes it the only check in the
probe whose latency is a claim about three separate requests.  That also
makes it the easiest one to fake: drop the arms that did not answer, average
the rest, or grade an empty window as within_budget, and the number still
looks like a measurement.  An empty window graded PASS is exactly the failure
this project already reported once ("healthy, verified" while unusable), so it
is pinned here as a red-arm observable rather than trusted.

What is measured, in order:

  1. THE OUTCOME EXISTS - scripts/observability/probe.py must still declare
     the concurrent-platforms check.  A deleted subject cannot be measured.

  2. IT RUNS OFFLINE - the probe is materialised from the image of this law
     (possibly red-mutated) into scratch and run once against a CLOSED
     loopback port with a synthetic 3-platform archive.  The working tree is
     never written to.  A probe that cannot run is NO_VERDICT, never a pass.

  3. THE DOC IS WELL-FORMED - probe_core.validate_verdict must find no
     contract violation.  Its rules are load-bearing here: not_measured may
     not carry n_measured or percentiles, and PASS may not carry n_measured
     of zero.

  4. IT WAS SAMPLED - n >= 1.  Declared but never sampled is a FAIL.

  5. THE PRINTOUT IS THE MIRROR - every machine field is recomputed from the
     stored samples using the probe_core of the repo (the rubric, not the
     subject) and must match.  This is the mirror discipline: a printout that
     no longer equals what its own samples say is a lie, however consistent
     it looks with itself.

  6. THE ARMS ARE ALL THERE - three attempts, one per platform, every one
     unanswered against a closed port.  A missing or an answered arm offline
     is a FAIL.

  7. THE VERDICT IS HONEST - offline, not_measured with n_measured == 0 and
     no percentiles is the ONLY pass.  PASS offline is impossible; FAIL
     offline is a false alarm; not_measured WITH measurements is worse than
     either.

Red input: red/concurrent-platforms-empty-window-passes.json - a one-token
mutation of probe_core.summarise that turns "no arm reached the endpoint"
from not_measured into a PASS: an empty window reported as healthy.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lawchecks as lc  # noqa: E402

LAW = "probe-concurrent-platforms-printout-is-measured"

OBS_DIR = "scripts/observability"
PROBE_REL = OBS_DIR + "/probe.py"
CORE_REL = OBS_DIR + "/probe_core.py"
CHECK = "concurrent-platforms"
PLATFORMS = ("youtube", "twitch", "kick")
PROBE_TIMEOUT_S = 120.0

# The machine-readable part of a summary.  A wording change is not a finding;
# a number change is, because the number is what a reader repeats.
GRADED_FIELDS = (
    "verdict", "n", "n_measured", "pass_count", "fail_count",
    "not_measured_count", "over_budget_count", "unreachable_count",
    "p50_ms", "p95_ms", "max_ms",
)

#: Fields summarise() returns that this checker CANNOT mirror, each for a
#: measured reason.  The event fold (restarts, unreachable_windows) needs the
#: probe's own nominal poll interval, which the printout does not carry, and
#: "check" is the name the caller already matched on.  Everything else
#: summarise() returns must match the printout - a new field must not be able to
#: appear in the verdict file ungraded just because this list was written before
#: it existed.  The window label is NOT exempt: it is read FROM the printout and
#: handed back to summarise(), so the whole reason sentence is graded, label and
#: all (measured 2026-10-11: it was the one field a first, narrower mirror read
#: as a mismatch).
UNGRADED_FIELDS = ("check", "restarts", "unreachable_windows")

# One video per platform, each id/size pair shaped like a real archived row
# and inside the duration band pick_real_platform_videos accepts (60..1800).
SYNTHETIC_ROWS = (
    ("youtube", "dQw4w9WgXcQ", "law-runner", "[law] synthetic youtube", 300),
    ("twitch", "1234567890", "law-runner", "[law] synthetic twitch", 300),
    ("kick", "0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b", "law-runner",
     "[law] synthetic kick", 300),
)


def _clean_core():
    """Import the probe_core of the repo as the rubric.

    The rubric must come from the trusted copy, never from the image under
    test: a law that grades a module using the code of that same module
    cannot see it lying, which is the whole reason this check exists.
    """
    obs = (lc.repo_root() / OBS_DIR).resolve()
    if str(obs) not in sys.path:
        sys.path.insert(0, str(obs))
    sys.modules.pop("probe_core", None)
    import probe_core as core  # noqa: PLC0415

    return core


def _scratch() -> Path:
    """A per-run scratch dir that is NOT tempfile.gettempdir().

    On this box that is G:/Temp, whose session-end wipe deletes vodrip-*;
    the evidence of this law must outlive the run that produced it.
    """
    raw = os.environ.get(lc.ENV_SCRATCH)
    base = Path(raw) if raw and raw.strip() else lc.repo_root() / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _materialise(image: dict, out: Path) -> Path:
    """Write the (possibly red-mutated) image to scratch, same layout.

    probe.py resolves its sibling through Path(__file__).parent, so placing
    both files side by side keeps the import honest without touching the
    working tree.
    """
    obs = out / OBS_DIR
    obs.mkdir(parents=True, exist_ok=True)
    for rel in (PROBE_REL, CORE_REL):
        (obs / Path(rel).name).write_text(image[rel], encoding="utf-8",
                                          newline="")
    return obs / Path(PROBE_REL).name


def _synthetic_archive(path: Path) -> str:
    """One video per platform, so every arm has something to open."""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE videos (platform TEXT, video_id TEXT, "
            "channel TEXT, title TEXT, duration_sec INTEGER)"
        )
        con.executemany(
            "INSERT INTO videos (platform, video_id, channel, title, "
            "duration_sec) VALUES (?, ?, ?, ?, ?)", SYNTHETIC_ROWS
        )
        con.commit()
    finally:
        con.close()
    return str(path)


def _closed_port() -> int:
    """A loopback port with nothing listening on it, guaranteed closed.

    Bind and release: the port is free at the moment of release and the
    probe run that follows is short, so the odds of reuse inside that
    window are negligible.  This is the only way to exercise the
    unreachable path without a network dependency.
    """
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _tail(text: str, limit: int = 200) -> str:
    """The last lines of a child traceback, as one short ASCII line."""
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    return ' / '.join(lines[-3:])[-limit:]


def _parse_samples(raw: str) -> list[dict]:
    """Every non-empty line of the sample file, or NO_VERDICT.

    A line that will not parse is EVIDENCE LOST.  The whole job of this law is
    to mirror the printed verdict against the samples that produced it, so a
    sample it cannot read has to stop the comparison rather than shrink it: a
    mirror computed over the surviving lines is a mirror of a file that never
    existed.  Skipping malformed lines was measured to be reachable - it is the
    same shape as reading the wrong archive, a wrong answer that looks like a
    finding.
    """
    samples = []
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(json.loads(line))
        except ValueError as exc:
            raise lc.NoVerdict(
                f'sample line {i} of the probe run is not JSON, so the window '
                f'it belongs to cannot be mirrored: {exc}') from exc
    return samples


def _run_probe(probe: Path, out: Path) -> dict:
    """Run the (possibly red-mutated) probe once, offline, and collect it.

    Returns the parsed verdict doc plus every sample the run stored, so the
    printout can be mirrored against the samples that produced it.  A probe
    that cannot run, times out, or writes no readable doc is NO_VERDICT by
    the contract: a law that cannot see its subject is not evidence.
    """
    api = 'http://127.0.0.1:%d' % _closed_port()
    archive = _synthetic_archive(out / 'synthetic-archive.db')
    jsonl = out / 'samples.jsonl'
    verdict = out / 'verdict.json'
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    argv = [
        sys.executable, str(probe),
        '--once',
        '--api', api,
        '--archive', archive,
        '--jsonl', str(jsonl),
        '--verdict', str(verdict),
    ]
    try:
        proc = subprocess.run(
            argv, cwd=str(out), env=env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise lc.NoVerdict(
            f'the probe did not finish within {PROBE_TIMEOUT_S:g}s'
        ) from exc
    except OSError as exc:
        raise lc.NoVerdict(f'could not start the probe: {exc}') from exc
    if proc.returncode != 0:
        raise lc.NoVerdict(
            f'the probe exited {proc.returncode} offline, expected 0: '
            f'{_tail(proc.stderr)}')
    try:
        doc = json.loads(verdict.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise lc.NoVerdict(
            f'the probe wrote no readable verdict doc: {exc}') from exc
    if not jsonl.exists():
        raise lc.NoVerdict(
            f'the probe stored no sample file at {jsonl.name}, so the printed '
            'verdict cannot be mirrored against the samples behind it')
    try:
        raw = jsonl.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        raise lc.NoVerdict(f'the probe wrote no readable sample file: {exc}') from exc
    return {'doc': doc, 'samples': _parse_samples(raw)}



def mirror_findings(core, entry: dict, samples: list[dict]) -> list[str]:
    """Every way the printed check can disagree with its own samples.

    The law exists because the printout is the product: "not measured, N
    attempts, none reached the endpoint" is the whole result of an offline run
    and nothing else re-derives it.  Each rule below is a sentence the printout
    must not be able to say while its own samples say otherwise - including the
    one that matters most offline, ``pass`` with nothing measured.

    A pure function of (entry, samples): no clock, no network, no file.  That is
    what lets a test drive it without running a probe, so the mirror itself can
    be shown to fail rather than assumed to work.
    """
    expected = core.summarise(CHECK, samples, entry.get('budget_ms'),
                              entry.get('window') or 'law-window', 0.0)
    findings: list[str] = []
    for field in sorted((set(expected) | set(GRADED_FIELDS)) - set(UNGRADED_FIELDS)):
        got = entry.get(field)
        want = expected.get(field)
        if got != want:
            findings.append(
                f'{CHECK}.{field} printed {got!r}, the {len(samples)} '
                f'sample(s) say {want!r}')

    arms = []
    for s in samples:
        for a in (s.get('arms') or []):
            arms.append(a)
    if not arms:
        findings.append(
            f'{CHECK} recorded no arm detail - a slowest-of-three is '
            'a fact about three attempts')
    else:
        seen = sorted(str(a.get('platform')) for a in arms)
        if seen != sorted(PLATFORMS):
            findings.append(f'{CHECK} attempted {seen}, expected {sorted(PLATFORMS)}')
        answered = [a for a in arms if a.get('status') is not None]
        if answered:
            codes = [a.get('status') for a in answered]
            findings.append(f'{CHECK} reached the endpoint offline: {codes}')

    verdict = entry.get('verdict')
    measured = entry.get('n_measured') or 0
    p50 = entry.get('p50_ms')
    reason = entry.get('reason')
    if verdict == core.PASS and measured < 1:
        findings.append(
            f'{CHECK} printed pass with n_measured={measured} - '
            'an empty window is not a pass')
    if verdict != core.NOT_MEASURED:
        findings.append(
            f'{CHECK} printed {verdict!r} offline against a closed port: '
            f'{reason}')
    elif measured:
        findings.append(
            f'{CHECK} printed not_measured but carries {measured} '
            'measurement(s)')
    elif p50 is not None:
        findings.append(
            f'{CHECK} printed not_measured but carries p50 {p50!r} ms')
    return findings


def main() -> int:
    root = lc.repo_root()
    lc.law_name(LAW)
    core = _clean_core()

    tree = lc.load_named_files(root, [PROBE_REL, CORE_REL])
    mutated = lc.apply_red_input(tree, root)
    image = mutated if mutated is not None else tree

    if CHECK not in image[PROBE_REL]:
        return lc.finish(
            lc.WORD_FAIL,
            f'{PROBE_REL} no longer declares a {CHECK} check - '
            'the subject does not exist')

    out = _scratch()
    probe_copy = _materialise(image, out)
    run = _run_probe(probe_copy, out)
    doc = run['doc']
    stored = run['samples']

    problems = core.validate_verdict(doc)
    mine = [q for q in problems if q.startswith(CHECK + ':')]
    if problems:
        return lc.finish(
            lc.WORD_FAIL,
            f'the offline verdict doc is malformed: '
            + (mine[0] if mine else problems[0]))

    entry = None
    for cand in doc.get('checks', []):
        if cand.get('check') == CHECK:
            entry = cand
            break
    if entry is None:
        return lc.finish(
            lc.WORD_FAIL,
            f'the verdict doc carries no {CHECK} entry - '
            'declared but never sampled')

    n = entry.get('n')
    if not isinstance(n, int) or n < 1:
        return lc.finish(lc.WORD_FAIL, f'{CHECK} was never sampled: n={n!r}')

    samples = [s for s in stored if s.get('check') == CHECK]
    findings = mirror_findings(core, entry, samples)
    if findings:
        return lc.finish(
            lc.WORD_FAIL,
            f'{len(findings)} finding(s): '
            + ' | '.join(str(f) for f in findings[:4]))
    return lc.finish(
        lc.WORD_PASS,
        f'{CHECK} printed not_measured offline: n={n}, '
        f'{entry.get("unreachable_count")} unreachable, no percentiles, and the '
        'printout equals what its own samples say')


if __name__ == '__main__':
    raise SystemExit(lc.run(main))
