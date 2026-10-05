#!/usr/bin/env python3
"""Reversible archival of dead scratch out of a live repo working area.

Nothing here deletes. Every move is a rename recorded in MANIFEST.tsv, and
`restore` puts every file back at its original path with a verified sha256.

Liveness is *measured*, never asserted:
  * a mtime-delta window (a file whose size/mtime changes is being written)
  * a Windows Restart Manager handle query (a file held open by a process)
  * a hard protected-name list that can never be moved

And non-candidacy is read from the repo's own vocabulary, not guessed:
  * a path in `git ls-files` has version history, so it is recoverable by
    definition and is *not* dead scratch. That is a fact, not a heuristic.

`classify` prints four labels, each a positive statement:

  live         demonstrably in use right now: protected, growing, or held open
  tracked      versioned by git -- never dead scratch, whatever its mtime
  dead-scratch declared dead AND measured idle -- the only thing archive moves
  unsure       NOT a synonym for "not scratch": an untracked, idle file that
               only an owner can adjudicate. Each entry names the decision.

Subcommands:
  classify   read-only inventory + classification of tmp/ and loose root files
  archive    move dead-scratch into tmp/_archive/<date>/ and write MANIFEST.tsv
  verify     manifest completeness check (the acceptance test); exit 1 on failure
  restore    move everything back from the manifest; exit 1 on failure
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

ARCHIVE_PARENT = os.path.join("tmp", "_archive")
# Overridden by --repo. This tool lives in a private worktree but must act on
# the real working tree, so the target is never inferred from __file__.
REPO_ROOT = ""

# ---------------------------------------------------------------------------
# Never move these, no matter how stale they look.
# ---------------------------------------------------------------------------
PROTECTED = {
    # the running liveness probe and its append-only output
    "tmp/liveness_probe.py",
    "tmp/liveness.jsonl",
    "tmp/liveness-probe.out",
    "tmp/liveness-probe.err",
    # dev supervisor streams
    "tmp/vodrip-devall-api.log",
    "tmp/vodrip-devall-web.log",
    "tmp/devall2.log",
    "tmp/devall2.err",
    # lock held by a running supervisor
    "tmp/dev-all.lock",
}

# Files that are stale command output from a completed run: tsc / vitest /
# pytest transcripts and their .exit markers. Keyed by repo-root-relative path.
DEAD_SCRATCH = {
    "tmp/tsc_main.txt",
    "tmp/tsc2.txt",
    "tmp/tsc3.txt",
    "tmp/tsc_final.txt",
    "tmp/tsc_f2.txt",
    "tmp/vitest_real.txt",
    "tmp/vt_final.txt",
    "tmp/pytest_final.txt",
    "tmp/pytest_final2.txt",
    "tmp/pf3.txt",
    "tmp/pf3.err",
    "tmp/final_full.txt",
    "tmp/final_full.exit",
    "tmp/final3.txt",
    "tmp/final3.exit",
    "tmp/guard-probe.txt",
    "tmp/guard-probe2.txt",
    # botched `> nul` redirect under PowerShell: findstr's stderr landed in a
    # literal file named `nul` (Windows reserved device name). 2026-07-03.
    "nul",
    # 1s / 0x0 degenerate MP4 test artifact, 2026-09-03, untracked+ignored
    "init.mp4",
}

# ---------------------------------------------------------------------------
# Windows long-path helper. `nul` is a reserved device name; the \\?\ prefix
# makes Windows treat it as an ordinary file. Required for stat/open/move.
# ---------------------------------------------------------------------------


def longp(path: str) -> str:
    """Prefix \\?\\ so Windows treats reserved device names as ordinary files.

    Deliberately uses normpath(), NOT abspath(). Win32 normalises a path whose
    final component is a reserved DOS device name (nul, con, aux, com1, ...)
    to the device path `\\\\.\\nul`, discarding the real directory. os.path.abspath
    inherits that behaviour, so abspath('<repo>\\nul') returns '\\\\.\\nul' and
    every stat on it fails with "network path not found". normpath() is pure
    string manipulation and never consults the device namespace.
    """
    if path.startswith("\\\\?\\"):
        return path
    if not os.path.isabs(path):
        raise ValueError("longp() requires an absolute path, got %r" % (path,))
    p = os.path.normpath(path)
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(longp(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Restart Manager: which PIDs hold a file open (read-only, takes no lock).
# ---------------------------------------------------------------------------

_CCH_RM_SESSION_KEY = 32
_CCH_RM_MAX_APP_NAME = 255
_CCH_RM_MAX_SVC_NAME = 63
_ERROR_MORE_DATA = 234


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (_CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (_CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", wintypes.DWORD),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]

_rstrtmgr = None


def _rm():
    global _rstrtmgr
    if _rstrtmgr is None:
        _rstrtmgr = ctypes.WinDLL("Rstrtmgr")
    return _rstrtmgr


def open_handlers(path: str):
    """Return {pid: app_name} for processes holding `path` open, or None."""
    if os.name != "nt":
        return {}
    dll = _rm()
    key = ctypes.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
    session = wintypes.DWORD(0)
    if dll.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return None
    found = {}
    try:
        plain = os.path.abspath(path)
        if plain.startswith("\\\\?\\"):
            plain = plain[4:]
        arr = (wintypes.LPCWSTR * 1)(plain)
        if dll.RmRegisterResources(session, 1, arr, 0, None, 0, None) != 0:
            return None
        needed = wintypes.UINT(0)
        got = wintypes.UINT(0)
        reasons = wintypes.DWORD(0)
        info = (_RM_PROCESS_INFO * 1)()
        rc = dll.RmGetList(
            session,
            ctypes.byref(needed),
            ctypes.byref(got),
            ctypes.byref(info),
            ctypes.byref(reasons),
        )
        if rc == _ERROR_MORE_DATA:
            info = (_RM_PROCESS_INFO * needed.value)()
            got = wintypes.UINT(needed.value)
            rc = dll.RmGetList(
                session,
                ctypes.byref(needed),
                ctypes.byref(got),
                ctypes.byref(info),
                ctypes.byref(reasons),
            )
        if rc == 0:
            for i in range(got.value):
                found[info[i].Process.dwProcessId] = info[i].strAppName
    finally:
        dll.RmEndSession(session)
    return found


# ---------------------------------------------------------------------------
# The repo's own vocabulary: what git already knows.
#
# "provenance not established" is not a useful verdict on AGENTS.md. A path in
# `git ls-files` has version history, so it is recoverable by definition and is
# not dead scratch. That is a fact about the repo, not a guess about intent.
# Only what git does NOT track, is not declared dead, and is not in use is a
# genuine open question -- and that is the sole meaning of `unsure`.
# ---------------------------------------------------------------------------

GIT_TIMEOUT = 30
_git_cache = {}


def _git(root, args, nul):
    """One read-only git call. Returns paths, or None on any failure at all.

    Every failure mode is a None, never an exception: this tool is read-only
    reporting for a live working tree, and a missing git must not take it down.
    """
    cmd = ["git", "-C", root] + list(args) + (["-z"] if nul else [])
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.decode("utf-8", "replace")
    if nul:
        return [p for p in text.split("\0") if p]
    return [p.strip() for p in text.splitlines() if p.strip()]


def git_vocabulary(root=None):
    """(tracked, ignored_untracked) as sets of repo-relative POSIX paths.

    Returns (None, None) when `root` is not a git work tree or git is missing.
    Callers then degrade to "every untracked idle file is an open question",
    which is the old behaviour -- correct, and noisy. The toplevel check keeps
    a sandbox under some other repo's tree from inheriting that repo's answers.
    """
    root = os.path.normpath(root or REPO_ROOT)
    if root in _git_cache:
        return _git_cache[root]
    result = (None, None)
    top = _git(root, ["rev-parse", "--show-toplevel"], nul=False)
    if top and len(top) == 1 and os.path.normpath(top[0]).lower() == root.lower():
        tracked = _git(root, ["ls-files"], nul=True)
        ignored = _git(
            root, ["ls-files", "--others", "--ignored", "--exclude-standard"], nul=True
        )
        if tracked is not None and ignored is not None:
            result = (set(tracked), set(ignored))
    _git_cache[root] = result
    return result


# ---------------------------------------------------------------------------
# classification vocabulary
# ---------------------------------------------------------------------------

# How much breaks if you archive the wrong file, by what the file IS.
KIND_LOSS = {"record": 2, "script": 1, "log": 0, "other": 1}
KIND_BY_EXT = {
    ".json": "record",
    ".md": "record",
    ".csv": "record",
    ".log": "log",
    ".py": "script",
    ".mjs": "script",
    ".js": "script",
    ".ps1": "script",
    ".ts": "script",
}
BAND_MEANING = {
    "p1": "act first",
    "p2": "worth an answer",
    "p3": "cheap either way",
}


def artifact_kind(rel_path):
    """`record` (a unique account of something that happened), `log`, `script`,
    `other`. Deliberately coarse: this orders questions, it does not judge."""
    return KIND_BY_EXT.get(os.path.splitext(os.path.basename(rel_path))[1].lower(), "other")


def consequence(size, kind):
    """Rank one open question: (score 0..4, band, reclaim_component).

    Two independent factors, because size alone ranks the wrong things. A
    350 KB superseded supervisor log deserves an owner's attention (bytes) but
    archiving it costs nothing, because it is regenerable. A 4 KB audit receipt
    is not worth the bytes, but archiving it destroys the only copy of a
    record. Summing them puts the expensive decision at the top instead of
    merely the biggest file, and still separates a 350 KB log from a 731 B
    utility script.
    """
    kb = size / 1024.0
    reclaim = 0 if kb < 16 else (1 if kb < 256 else 2)
    score = min(reclaim + KIND_LOSS.get(kind, 1), 4)
    band = "p1" if score >= 3 else ("p2" if score == 2 else "p3")
    return score, band, reclaim


def decision_for(ignored, references=0):
    """The choice actually on the table. `unsure` must never mean "unknown"."""
    if references:
        return (
            "referenced by %d tracked file(s) as a path: archiving it breaks live code -- "
            "keep it, or remove the reference first" % references
        )
    if ignored is True:
        return "gitignored + untracked: archive it, or keep it because something still uses it"
    if ignored is False:
        return "untracked and NOT ignored: git add it (never committed), or archive it"
    return "git vocabulary unavailable, so tracked/ignored is unknown: archive it, or keep it"


# ---------------------------------------------------------------------------
# Is anything actually reading this file?
#
# A leftover is only safe to archive if nothing in the repo still points at it.
# The counting rule that matters: a match is a PATH FORM -- the name as a path
# component, i.e. preceded or followed by a separator. Counting bare substrings
# inflates short names absurdly (measured on this repo: `{}` -> 1,181 hits,
# `200` -> 727) and every one of those is a non-match, so a substring counter
# reports a phantom dependency and a classifier built on it is safe by accident
# rather than by design. Path form is what a real consumer looks like:
#     __main_launcher__.py:294   base / "icon.ico",
#     deploy-dist.mjs:74        cpSync(winExe, join(root, 'VOD-RIP.EXE'))
# ---------------------------------------------------------------------------

# A mention that is not a dependency. Matched on the SAME LINE as the
# reference, never on the whole file: a file that both copies build outputs
# AND reads an icon at launch is a real consumer of the icon, and filtering on
# file-level content threw away backend/__main_launcher__.py -- the single most
# load-bearing reader of icon.ico in this repo.
#   .gitignore:16                     VOD-RIP.EXE        <- "do not track this"
#   scripts/deploy-dist.mjs:74        cpSync(..., 'VOD-RIP.EXE')  <- PRODUCES it
#   installer/installer.iss:10        #define AppExe      <- names the build output
PRODUCER_MARKERS = (
    "cpsync",
    "copyfile",
    "copy-item",
    "#define",
    "copied at build",
    "shutil.copy",
    "shutil.move",
    "outfile",
)


def _is_producer(path, line):
    """True when this LINE writes the file, rather than reading it.

    Deliberately a short list. A wider one starts deleting real consumers: a
    narration filter that dropped `console.log` also dropped the README line
    that tells a user which file to click, which is a genuine dependency for
    anyone following the docs.
    """
    if os.path.basename(path) in (".gitignore", ".gitattributes"):
        return True
    low = line.lower()
    return any(m in low for m in PRODUCER_MARKERS)


def reference_count(rel_path, git=None):
    """Tracked files that consume `rel_path`, matched in PATH FORM only.

    Returns (count, sample). `git` is injectable so tests can drive the
    counter from known content with no subprocess.

    Path form is the whole point. A substring counter on short names is
    nonsense -- measured on this repo, `{}` scores 1,181 hits and `200`
    scores 727, every one a non-match -- so it invents dependencies and the
    classifier above it looks careful for the wrong reason. What a real
    consumer looks like, in this repo:
        backend/__main_launcher__.py:294   base / "icon.ico",
    """
    name = os.path.basename(rel_path)
    if not name:
        return 0, []
    if git is None:
        git = _git_capture
    # Character classes, NOT lookbehind: git grep -E is POSIX ERE and rejects
    # `(?<!...)` outright ("Invalid preceding regular expression"). Measured
    # cost of getting this wrong: the pattern errors, the exit code is
    # non-zero, and a caller that treats that as "no matches" reports every
    # file as unreferenced -- the exact inverse of the truth.
    pattern = (
        "(^|[^A-Za-z0-9_.%-])" + re.escape(name) + "([^A-Za-z0-9_]|$)"
    )
    hits = []
    seen = set()
    for path, line in git(pattern):
        path = path.replace("\\", "/")
        if path == "tools/tmp_scratch_archive.py":
            continue  # the classifier naming a file is not a consumer of it
        if path.startswith("tools/tests/"):
            continue  # ditto for the tests that assert on those very names
        if path in seen:
            continue  # 5 reads of one file are one consumer, not five
        seen.add(path)
        if _is_producer(path, line):
            continue  # writes the file, or is a rule about the file
        hits.append(path)
    return len(hits), hits[:5]


def _git_capture(pattern):
    """Yield (path, matching_line) for tracked lines matching `pattern`."""
    out = _git(REPO_ROOT, ["grep", "-I", "-n", "-E", pattern, "--", "."], nul=False)
    for line in out or []:
        if ":" not in line:
            continue
        path, _, text = line.partition(":")
        path = path.strip()
        if path:
            yield path, text



# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def rel(path: str) -> str:
    """Repo-root-relative POSIX path, as a pure string operation.

    os.path.relpath() is unusable here for the same reason abspath() is: it
    normalises through the device namespace, so a path ending in a reserved
    name (nul) is reported as being on mount '\\\\.\\nul'.
    """
    p = os.path.normpath(path)
    root = os.path.normpath(REPO_ROOT)
    if p.lower() == root.lower():
        return "."
    prefix = root + os.sep
    if not p.lower().startswith(prefix.lower()):
        raise ValueError("%r is not under repo root %r" % (p, root))
    return p[len(prefix) :].replace("\\", "/")


def _stat(path: str):
    try:
        return os.stat(longp(path))
    except OSError:
        return None


def _listdir(path: str):
    try:
        return sorted(os.listdir(longp(path)))
    except OSError:
        return []


def inventory(window: int = 0, vocab=None, refcheck=None):
    """Return one record per candidate file, with liveness evidence attached.

    `refcheck` is injectable (path -> (count, sample)) so the reference rule
    can be exercised in tests without shelling out to git. Pass `False` to
    skip it entirely, which is what a non-git target gets.
    """
    if vocab is None:
        vocab = git_vocabulary()
    tracked, ignored = vocab
    if refcheck is None:
        refcheck = reference_count if tracked is not None else (lambda p: (0, []))
    recs = []
    todo = []
    tmp = os.path.join(REPO_ROOT, "tmp")
    for name in _listdir(tmp):
        p = os.path.join(tmp, name)
        if os.path.isfile(longp(p)):
            todo.append(p)
    for name in _listdir(REPO_ROOT):
        p = os.path.join(REPO_ROOT, name)
        if os.path.isfile(longp(p)):
            todo.append(p)

    before = {}
    if window:
        for p in todo:
            st = _stat(p)
            if st:
                before[p] = (st.st_size, st.st_mtime)
        time.sleep(window)
    for p in todo:
        st = _stat(p)
        if st is None:
            continue
        r = rel(p)
        growing = False
        if window and p in before:
            growing = before[p] != (st.st_size, st.st_mtime)
        handles = open_handlers(p)
        held = bool(handles) if handles is not None else None
        is_tracked = (r in tracked) if tracked is not None else None
        is_ignored = (r in ignored) if tracked is not None else None
        kind = artifact_kind(r)
        score, band, _reclaim = consequence(st.st_size, kind)
        nrefs, sample = 0, []
        if r in PROTECTED:
            cls = "live"
            why = "protected: on the never-move list"
        elif r in DEAD_SCRATCH and not growing and not held:
            cls = "dead-scratch"
            why = "declared dead-scratch, %+.0fs old, no writer, no handle" % (
                time.time() - st.st_mtime
            )
        elif r in DEAD_SCRATCH:
            cls = "live"
            why = "listed as scratch but growing=%s held=%s" % (growing, held)
        elif growing or held:
            cls = "live"
            why = "growing=%s held=%s" % (growing, held)
        elif is_tracked:
            # Authoritative, and it outranks staleness: a tracked path has
            # version history, so moving it out of the tree is a working-tree
            # change nobody asked for, and `git checkout` brings it back.
            cls = "tracked"
            why = "git-tracked: version history, recoverable by definition, never dead scratch"
        else:
            # An untracked, idle file is a question. Whether it is a SMALL
            # question depends on whether live code still points at it: a
            # 16 KB icon with 23 consumers is not "cheap either way", it is
            # the app's icon. Counted in path form, never as a substring.
            nrefs, sample = refcheck(r)
            if nrefs:
                score = min(score + 2, 4)
                band = "p1" if score >= 3 else ("p2" if score == 2 else "p3")
            cls = "unsure"
            why = "%s [%s %s]" % (decision_for(is_ignored, nrefs), band, BAND_MEANING[band])
            if sample:
                why += "  <- %s" % ", ".join(sample[:2])
        recs.append(
            {
                "rel": r,
                "abs": p,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "class": cls,
                "why": why,
                "growing": growing,
                "handles": handles,
                "tracked": is_tracked,
                "ignored": is_ignored,
                "kind": kind,
                "score": score,
                "nrefs": nrefs,
                "band": band,
            }
        )
    return recs


def _already_archived():
    """Declared candidates that already sit in any dated archive dir."""
    out = set()
    base = os.path.join(REPO_ROOT, ARCHIVE_PARENT)
    for date in _listdir(base):
        d = os.path.join(base, date)
        for name in _listdir(os.path.join(d, "tmp")):
            out.add("tmp/%s" % name)
        for name in _listdir(os.path.join(d, "root")):
            out.add(name)
    return out


def missing_candidates(recs):
    """Declared archive candidates that the inventory failed to see.

    A candidate that cannot be stat'd is the dangerous case: it means the
    enumerator silently skipped a file we promised to move. Report it rather
    than archiving the rest and reporting success. Already-archived members do
    not count, so this stays idempotent after an archive run.
    """
    seen = {r["rel"] for r in recs}
    return sorted(DEAD_SCRATCH - seen - _already_archived())


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

MANIFEST_NAME = "MANIFEST.tsv"
COLUMNS = ("archived_rel", "original_rel", "size", "mtime_iso", "sha256")


def manifest_path(date: str) -> str:
    return os.path.join(REPO_ROOT, ARCHIVE_PARENT, date, MANIFEST_NAME)


def read_manifest(date: str):
    path = manifest_path(date)
    rows = []
    header = None
    with open(longp(path), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if not line or line.startswith("#"):
                continue
            if header is None:
                header = line.split("\t")
                continue
            rows.append(dict(zip(header, line.split("\t"))))
    return header, rows


def write_manifest(date: str, rows, archive_root: str, created: str):
    path = manifest_path(date)
    os.makedirs(longp(os.path.dirname(path)), exist_ok=True)
    with open(longp(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# VOD.RIP tmp scratch archive manifest\n")
        fh.write("# created: %s\n" % created)
        fh.write("# archive_root: %s\n" % archive_root.replace("\\", "/"))
        fh.write("# restore: python tools/tmp_scratch_archive.py restore --date %s\n" % date)
        fh.write("\t".join(COLUMNS) + "\n")
        for r in rows:
            fh.write(
                "\t".join(
                    (
                        r["archived_rel"],
                        r["original_rel"],
                        str(r["size"]),
                        r["mtime_iso"],
                        r["sha256"],
                    )
                )
                + "\n"
            )
    return path


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


CLASS_BLURB = (
    ("live", "in use right now: protected, growing, or held open"),
    ("tracked", "versioned by git - never dead scratch, whatever its mtime"),
    ("dead-scratch", "declared dead and measured idle - the only thing archive moves"),
    ("unsure", "NEEDS A DECISION: untracked, idle, owner must choose"),
)


def cmd_classify(args):
    recs = inventory(window=args.window)
    tracked, _ignored = git_vocabulary()
    counts = {}
    for r in recs:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    unsure = [r for r in recs if r["class"] == "unsure"]
    unsure.sort(key=lambda x: (-x["score"], -x["size"], x["rel"]))

    print("repo_root: %s" % REPO_ROOT)
    if args.window:
        print(
            "liveness: %ds mtime-delta window + Restart Manager open handles"
            % args.window
        )
    if tracked is None:
        print("git vocabulary: UNAVAILABLE (not a git work tree?) - cannot rule anything out")
    else:
        print("git vocabulary: %d tracked path(s) from `git ls-files`" % len(tracked))

    print("")
    print("HEADLINE")
    for cls, blurb in CLASS_BLURB:
        print("  %-13s %4d  %s" % (cls, counts.get(cls, 0), blurb))

    if unsure:
        bands = {}
        for r in unsure:
            bands[r["band"]] = bands.get(r["band"], 0) + 1
        print(
            "  %-13s      %s"
            % (
                "",
                "  ".join(
                    "%s=%d(%s)" % (b, bands.get(b, 0), BAND_MEANING[b]) for b in ("p1", "p2", "p3")
                ),
            )
        )

    if unsure:
        print("")
        top = len(unsure) if args.top <= 0 else min(args.top, len(unsure))
        print("DECISIONS (%d) ranked by consequence, not by size alone:" % len(unsure))
        for r in unsure[:top]:
            print(
                "  %-3s %-40s %9d  %6.1fK  %-6s %s"
                % (
                    r["band"],
                    r["rel"],
                    r["size"],
                    r["size"] / 1024.0,
                    r["kind"],
                    r["why"],
                )
            )
        if top < len(unsure):
            print("  ... and %d more (--top 0 lists all, --detail lists every file)" % (
                len(unsure) - top
            ))

    print("")
    print("counts: " + ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)))

    if args.detail:
        print("")
        print("DETAIL")
        print("%-36s %-13s %10s  %s" % ("PATH", "CLASS", "BYTES", "EVIDENCE"))
        print("-" * 118)
        for r in sorted(recs, key=lambda x: (x["class"], x["rel"])):
            h = r["handles"]
            hs = "n/a" if h is None else (", ".join("pid=%s" % k for k in h) or "-")
            print(
                "%-36s %-13s %10d  growing=%-5s held=%-12s %s"
                % (r["rel"], r["class"], r["size"], r["growing"], hs, r["why"])
            )

    missing = missing_candidates(recs)
    if missing:
        print("")
        print("MISSING CANDIDATE(S) -- declared but not visible to the enumerator:")
        for m in missing:
            print("  ! %s" % m)
        return 2
    return 0


def cmd_archive(args):
    recs = inventory(window=args.window)
    missing = missing_candidates(recs)
    if missing:
        print("REFUSING to archive: declared candidate(s) invisible to the enumerator.")
        print("Archiving the rest and calling it success is how data goes missing.")
        for m in missing:
            print("  ! %s" % m)
        return 2
    moved, skipped = [], []
    for r in recs:
        if r["class"] != "dead-scratch":
            skipped.append(r)
            continue
        st = _stat(r["abs"])
        # origin dir keeps the manifest unambiguous and the rollback derivable
        origin = "tmp" if r["rel"].startswith("tmp/") else "root"
        archived_rel = "%s/%s" % (origin, os.path.basename(r["rel"]))
        dest = os.path.join(REPO_ROOT, ARCHIVE_PARENT, args.date, *archived_rel.split("/"))
        os.makedirs(longp(os.path.dirname(dest)), exist_ok=True)
        if os.path.exists(longp(dest)):
            raise SystemExit("refusing to overwrite existing archive member: %s" % dest)
        os.replace(longp(r["abs"]), longp(dest))
        moved.append(
            {
                "archived_rel": archived_rel,
                "original_rel": r["rel"],
                "size": r["size"],
                "mtime_iso": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime(st.st_mtime)
                ),
                "sha256": sha256_of(dest),
            }
        )

    archive_root = os.path.join(REPO_ROOT, ARCHIVE_PARENT, args.date)
    path = write_manifest(args.date, moved, archive_root, time.strftime("%Y-%m-%d %H:%M:%S"))
    total = sum(m["size"] for m in moved)
    print("archived %d file(s), %d bytes" % (len(moved), total))
    print("manifest: %s" % rel(path))
    print("left in place: %d" % len(skipped))
    for m in moved:
        print("  + %-14s %10d  %s" % (m["archived_rel"], m["size"], m["original_rel"]))
    return 0


def verify_archive(date):
    """Check one dated archive. Returns (problems, nrows, n_on_disk).

    `problems` empty means the archive is sound. Every failure mode the brief
    cares about is covered:
      * a file in the archive dir that no manifest row accounts for (data loss)
      * a manifest row whose sha256 or size no longer matches the member
      * a manifest row with no member on disk
      * an original_rel that could not be driven back to a safe path
      * a protected/live path that somehow got archived
    """
    header, rows = read_manifest(date)
    archive_root = os.path.join(REPO_ROOT, ARCHIVE_PARENT, date)
    problems = []

    if header != list(COLUMNS):
        problems.append("manifest header %r != %r" % (header, list(COLUMNS)))

    # 1. every manifest row must resolve to an existing archive member
    listed = set()
    for r in rows:
        listed.add(r["archived_rel"])
        member = os.path.join(archive_root, *r["archived_rel"].split("/"))
        if not os.path.isfile(longp(member)):
            problems.append("manifest row has no file in archive: %s" % r["archived_rel"])
            continue
        actual = sha256_of(member)
        if actual != r["sha256"]:
            problems.append(
                "sha256 mismatch %s: manifest=%s actual=%s"
                % (r["archived_rel"], r["sha256"][:12], actual[:12])
            )
        size = os.stat(longp(member)).st_size
        if size != int(r["size"]):
            problems.append(
                "size mismatch %s: manifest=%s actual=%s" % (r["archived_rel"], r["size"], size)
            )

    # 2. every file physically in the archive dir must be in the manifest
    on_disk = set()
    base = longp(archive_root)
    for dirpath, _dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base).replace("\\", "/")
        for fn in filenames:
            if rel_dir == ".":
                rel_dir = ""
            if (rel_dir + "/" + fn).strip("/") == MANIFEST_NAME:
                continue
            on_disk.add(((rel_dir + "/" + fn) if rel_dir else fn).strip("/"))
    for extra in sorted(on_disk - listed):
        problems.append("file in archive dir is NOT in manifest (data loss): %s" % extra)

    # 3. every original_rel must be a well-formed repo-relative path we can restore
    for r in rows:
        o = r["original_rel"]
        if o.startswith("/") or ":" in o or ".." in o.split("/"):
            problems.append("unrecoverable original_rel: %s" % o)

    # 4. no protected path may appear in the manifest
    for r in rows:
        if r["original_rel"] in PROTECTED:
            problems.append("PROTECTED live path was archived: %s" % r["original_rel"])

    return problems, len(rows), len(on_disk)


def cmd_verify(args):
    problems, nrows, n_disk = verify_archive(args.date)
    archive_root = os.path.join(REPO_ROOT, ARCHIVE_PARENT, args.date)
    print("archive: %s" % rel(archive_root))
    print("manifest rows: %d   files in archive dir: %d" % (nrows, n_disk))
    if problems:
        print("\nFAIL (%d problem(s)):" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("PASS: manifest is complete, every digest matches, every path recoverable")
    return 0


def cmd_restore(args):
    date = args.date
    _header, rows = read_manifest(date)
    archive_root = os.path.join(REPO_ROOT, ARCHIVE_PARENT, date)
    problems = []
    restored = 0
    for r in rows:
        member = os.path.join(archive_root, *r["archived_rel"].split("/"))
        target = os.path.join(REPO_ROOT, *r["original_rel"].split("/"))
        if not os.path.isfile(longp(member)):
            problems.append("missing archive member: %s" % r["archived_rel"])
            continue
        if os.path.exists(longp(target)):
            problems.append("refusing to overwrite existing target: %s" % r["original_rel"])
            continue
        os.makedirs(longp(os.path.dirname(target)), exist_ok=True)
        os.replace(longp(member), longp(target))
        if sha256_of(target) != r["sha256"]:
            problems.append("sha256 mismatch after restore: %s" % r["original_rel"])
            continue
        restored += 1
    if problems:
        print("FAIL (%d problem(s)):" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("restored %d/%d file(s) to their original paths, all sha256 verified" % (
        restored, len(rows)))
    return 0


def cmd_selftest(args):
    """Prove verify() bites. Builds a throwaway sandbox, injects each fault,
    and asserts verify REJECTS it. A completeness check that only ever passes
    is not a check."""
    import shutil
    import tempfile

    global REPO_ROOT
    saved_root = REPO_ROOT
    sandbox = tempfile.mkdtemp(prefix="scratch-archive-selftest-")
    date = "2026-01-01"
    failures = []
    try:
        REPO_ROOT = sandbox
        tmpdir = os.path.join(sandbox, "tmp")
        os.makedirs(longp(tmpdir), exist_ok=True)
        names = ["alpha.txt", "beta.txt", "gamma.bin"]
        payload = {"alpha.txt": b"alpha", "beta.txt": b"beta", "gamma.bin": b"\x00\x01\x02"}
        for n in names:
            with open(longp(os.path.join(tmpdir, n)), "wb") as fh:
                fh.write(payload[n])

        moved = []
        for n in names:
            st = os.stat(longp(os.path.join(tmpdir, n)))
            dest = os.path.join(sandbox, ARCHIVE_PARENT, date, "tmp", n)
            os.makedirs(longp(os.path.dirname(dest)), exist_ok=True)
            os.replace(longp(os.path.join(tmpdir, n)), longp(dest))
            moved.append(
                {
                    "archived_rel": "tmp/%s" % n,
                    "original_rel": "tmp/%s" % n,
                    "size": st.st_size,
                    "mtime_iso": time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z", time.localtime(st.st_mtime)
                    ),
                    "sha256": sha256_of(dest),
                }
            )
        write_manifest(date, moved, os.path.join(sandbox, ARCHIVE_PARENT, date), "selftest")

        def expect(name, want_problem, fault):
            problems, _r, _d = verify_archive(date)
            if not problems:
                failures.append("%s: verify PASSED but should have failed" % name)
                return
            if want_problem not in " | ".join(problems):
                failures.append(
                    "%s: failed, but not with %r (got: %s)" % (name, want_problem, problems)
                )
                return
            print("  bites  %-28s -> %s" % (name, problems[0]))

        def healthy():
            problems, _r, _d = verify_archive(date)
            if problems:
                failures.append("clean sandbox did not pass: %s" % problems)
                return
            print("  pass   %-28s (clean archive accepted)" % "untampered")

        mpath = manifest_path(date)
        good = open(longp(mpath), encoding="utf-8").read()
        victim = os.path.join(sandbox, ARCHIVE_PARENT, date, "tmp", "beta.txt")
        blob = open(longp(victim), "rb").read()

        print("selftest sandbox: 3 files, 1 manifest")
        healthy()

        # fault 1: a manifest row silently omits a file that IS in the archive
        with open(longp(victim), "wb") as fh:
            fh.write(blob)
        kept = [
            l
            for l in good.split("\n")
            if not l.startswith("tmp/beta.txt\t")
        ]
        open(longp(mpath), "w", encoding="utf-8", newline="\n").write("\n".join(kept))
        expect("row omitted from manifest", "NOT in manifest", None)

        # fault 2: member contents change after archiving
        open(longp(mpath), "w", encoding="utf-8", newline="\n").write(good)
        with open(longp(victim), "wb") as fh:
            fh.write(b"TAMPERED")
        expect("member byte-edited", "sha256 mismatch", None)

        # fault 3: a manifest row with no member on disk
        open(longp(mpath), "w", encoding="utf-8", newline="\n").write(good)
        os.remove(longp(victim))
        expect("member missing on disk", "no file in archive", None)

        # fault 4: an unrecoverable original_rel
        os.makedirs(longp(os.path.dirname(victim)), exist_ok=True)
        with open(longp(victim), "wb") as fh:
            fh.write(blob)
        bad = good.replace("tmp/beta.txt\ttmp/beta.txt", "tmp/beta.txt\t../escape")
        open(longp(mpath), "w", encoding="utf-8", newline="\n").write(bad)
        expect("path-traversal original_rel", "unrecoverable original_rel", None)

        # fault 5: a protected/live path smuggled into the manifest
        open(longp(mpath), "w", encoding="utf-8", newline="\n").write(
            good.replace("tmp/beta.txt\ttmp/beta.txt", "tmp/beta.txt\ttmp/liveness.jsonl")
        )
        expect("protected live path archived", "PROTECTED live path", None)

        # restore the good manifest and confirm the sandbox passes again
        open(longp(mpath), "w", encoding="utf-8", newline="\n").write(good)
        healthy()
    finally:
        REPO_ROOT = saved_root
        shutil.rmtree(sandbox, ignore_errors=True)

    if failures:
        print("\nSELFTEST FAILED (%d):" % len(failures))
        for f in failures:
            print("  - %s" % f)
        return 1
    print("\nSELFTEST PASSED: every fault class was detected")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo",
        required=True,
        help="repo root to operate on (this tool may live in another worktree)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("classify")
    p.add_argument("--window", type=int, default=30, help="mtime-delta seconds (0=skip)")
    p.add_argument(
        "--detail", action="store_true", help="also print every file, not just the decisions"
    )
    p.add_argument(
        "--top", type=int, default=12, help="max decisions to print (0 = all)"
    )
    p.set_defaults(fn=cmd_classify)

    p = sub.add_parser("archive")
    p.add_argument("--date", required=True)
    p.add_argument("--window", type=int, default=30)
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("verify")
    p.add_argument("--date", required=True)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("restore")
    p.add_argument("--date", required=True)
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("selftest")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    global REPO_ROOT
    REPO_ROOT = os.path.abspath(args.repo)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
