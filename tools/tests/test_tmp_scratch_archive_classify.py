"""Tests for tools/tmp_scratch_archive.py -- the classifier's vocabulary first.

The defect these exist for: `unsure` used to be the default bucket, so a
tracked `AGENTS.md` came back as "provenance not established" next to a real
leftover in tmp/. An open question has to be distinguishable from a file nobody
would ever question, or the report communicates nothing.

The module under test is located through VODRIP_SCRATCH_TOOL so the same suite
can be pointed at the previous revision to prove these assertions bite.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("VODRIP_SCRATCH_TOOL") or os.path.join(
    os.path.dirname(HERE), "tmp_scratch_archive.py"
)
# The worktree this tool may be tested from is not the live working tree, so the
# real-repo test takes its root from the environment when it is given one.
LIVE_ROOT = os.environ.get("VODRIP_SCRATCH_TEST_ROOT") or os.path.dirname(os.path.dirname(HERE))


def _load():
    spec = importlib.util.spec_from_file_location("tmp_scratch_archive_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    m = _load()
    # Defensive on purpose: this fixture must work against a revision of the
    # tool that predates the git vocabulary, so the vocabulary tests fail on
    # their own assertions instead of on a missing attribute.
    cache = getattr(m, "_git_cache", None)
    if isinstance(cache, dict):
        cache.clear()
    return m


def _git(cwd, *args):
    out = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, check=False
    )
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    return out.stdout


def _make_git_repo(root, commit=(), gitignore=()):
    """A throwaway repo whose git vocabulary is real, not faked."""
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    _git(root, "init", "-q")
    if gitignore:
        with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(gitignore) + "\n")
    for name in commit:
        p = os.path.join(root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("tracked content of %s\n" % name)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    return root


def _by_rel(recs):
    return {r["rel"]: r for r in recs}


def mod_consequence(size, filename):
    m = _load()
    return m.consequence(size, m.artifact_kind(filename))


# ---------------------------------------------------------------------------
# 1. git-tracked is a positive statement, not an open question
# ---------------------------------------------------------------------------


def test_git_tracked_file_is_a_live_project_file_not_an_ambiguous_candidate(mod, tmp_path):
    root = _make_git_repo(
        str(tmp_path), commit=["AGENTS.md", "README.md", ".gitignore"], gitignore=["tmp/"]
    )
    mod.REPO_ROOT = root
    recs = _by_rel(mod.inventory(window=0))

    for name in ("AGENTS.md", "README.md", ".gitignore"):
        assert name in recs, "%s missing from the inventory" % name
        assert recs[name]["class"] == "tracked", (
            "a git-tracked file came back as %r (%s); it has version history and is "
            "recoverable by definition, so it is not an ambiguous candidate"
            % (recs[name]["class"], recs[name]["why"])
        )
        assert recs[name]["tracked"] is True
        assert "git-tracked" in recs[name]["why"]

    # and the old wording must be gone entirely: "provenance not established"
    # was the default bucket, which is what made the report unusable.
    for r in recs.values():
        assert "provenance not established" not in r["why"]


def test_git_tracked_class_is_not_the_ambiguous_bucket(mod, tmp_path):
    root = _make_git_repo(str(tmp_path), commit=["README.md"])
    mod.REPO_ROOT = root
    recs = mod.inventory(window=0)
    tracked = [r["rel"] for r in recs if r["class"] == "tracked"]
    unsure = [r["rel"] for r in recs if r["class"] == "unsure"]
    assert "README.md" in tracked
    assert "README.md" not in unsure


# ---------------------------------------------------------------------------
# 2. the ambiguous case is preserved, and says which decision is needed
# ---------------------------------------------------------------------------


def test_untracked_gitignored_stale_tmp_file_stays_ambiguous_and_names_the_decision(
    mod, tmp_path
):
    root = _make_git_repo(str(tmp_path), commit=["README.md"], gitignore=["tmp/"])
    with open(os.path.join(root, "tmp", "leftover.log"), "w", encoding="utf-8") as fh:
        fh.write("x" * 4096)
    os.utime(os.path.join(root, "tmp", "leftover.log"), (time.time() - 86400,) * 2)
    mod.REPO_ROOT = root
    rec = _by_rel(mod.inventory(window=0))["tmp/leftover.log"]

    assert rec["class"] == "unsure", (
        "an untracked, gitignored, stale tmp/ file is a real question and must stay one"
    )
    assert rec["ignored"] is True
    assert rec["tracked"] is False
    # the entry has to say what the decision is, not just that nobody knows
    assert "archive it" in rec["why"]
    assert rec["band"] in ("p1", "p2", "p3")


def test_untracked_but_not_ignored_names_the_commit_decision(mod, tmp_path):
    root = _make_git_repo(str(tmp_path), commit=["README.md"], gitignore=["tmp/"])
    with open(os.path.join(root, "stray.txt"), "w", encoding="utf-8") as fh:
        fh.write("y" * 2048)
    mod.REPO_ROOT = root
    rec = _by_rel(mod.inventory(window=0))["stray.txt"]
    assert rec["class"] == "unsure"
    assert rec["ignored"] is False
    assert "git add" in rec["why"]


def test_a_file_in_a_non_git_tree_degrades_instead_of_crashing(mod, tmp_path):
    root = str(tmp_path / "not-a-repo")
    os.makedirs(os.path.join(root, "tmp"))
    with open(os.path.join(root, "tmp", "orphan.log"), "w", encoding="utf-8") as fh:
        fh.write("z")
    mod.REPO_ROOT = root
    assert mod.git_vocabulary() == (None, None)
    rec = _by_rel(mod.inventory(window=0))["tmp/orphan.log"]
    assert rec["class"] == "unsure"
    assert "git vocabulary unavailable" in rec["why"]


# ---------------------------------------------------------------------------
# 3. the headline stays small enough to act on
# ---------------------------------------------------------------------------


def test_real_repo_headline_counts_only_what_needs_a_decision(mod):
    if not os.path.isdir(LIVE_ROOT):
        pytest.skip("live repo root not available: %s" % LIVE_ROOT)
    mod.REPO_ROOT = LIVE_ROOT
    recs = mod.inventory(window=0)
    counts = {}
    for r in recs:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    unsure = {r["rel"] for r in recs if r["class"] == "unsure"}

    tracked, _ignored = mod.git_vocabulary()
    assert tracked, "expected a git vocabulary at %s" % LIVE_ROOT

    # The defect was never "13 files"; it was "AGENTS.md is in the list". A
    # fixed ceiling would rot the moment another lane leaves a file in tmp/ --
    # which is exactly what happened, and it is not a regression. So the
    # invariant is structural: the bucket contains no tracked path, and it is
    # small relative to what the old vocabulary called unsure.
    assert not (unsure & tracked), (
        "a git-tracked file is being reported as a question: %s" % sorted(unsure & tracked)
    )
    # Before the fix this repo produced 33 unsure entries, 23 of them tracked
    # project files. The bucket must stay a short list, not a share of the
    # working tree: 25 files are tracked at the root, and every one of them is
    # a non-question.
    root_files = {r["rel"] for r in recs if "/" not in r["rel"]}
    assert len(unsure) <= max(12, len(root_files)), (
        "ambiguous bucket is %d entries (%s)" % (len(unsure), sorted(unsure))
    )
    for name in ("README.md", "AGENTS.md", "index.html", ".gitignore", "LICENSE.txt"):
        if name in {r["rel"] for r in recs}:
            assert name not in unsure, "%s is a live project file" % name


# ---------------------------------------------------------------------------
# 4. consequence ranking: not size alone, and not size-blind
# ---------------------------------------------------------------------------


def test_big_log_outranks_a_tiny_script():
    big_log = mod_consequence(350_000, "devall-stderr.log")
    tiny_script = mod_consequence(731, "dirsizes.mjs")
    assert big_log[0] > tiny_script[0], (
        "a 350 KB superseded log and a 731 B utility script are not the same question"
    )
    assert big_log[1] == "p2"
    assert tiny_script[1] == "p3"


def test_kind_decides_when_size_cannot(mod):
    """Size alone must not rank. Holding size fixed, only the kind varies, and
    the kind decides: archiving a unique record is the expensive mistake."""
    # same bytes, different kind
    record = mod_consequence(50_000, "audit.json")   # a record
    script = mod_consequence(50_000, "sweep.py")      # a regenerable tool
    log = mod_consequence(50_000, "old.log")          # regenerable output
    assert record[0] > script[0] > log[0], (
        "at equal size the ranking must follow what breaks if you archive it: "
        "record > script > log (got %r, %r, %r)" % (record[0], script[0], log[0])
    )


def test_size_decides_when_kind_cannot(mod):
    """...and holding the kind fixed, size decides. Otherwise the score would
    be a constant and the ranking would carry no information at all."""
    small = mod_consequence(4_388, "sweep.py")
    big = mod_consequence(284_535, "sweep.py")
    assert big[0] > small[0]
    assert big[1] != small[1], "a 278 KB script and a 4 KB script are not one question"


# ---------------------------------------------------------------------------
# PRESERVED: liveness is measured, not asserted
# ---------------------------------------------------------------------------


def test_a_file_held_open_by_a_process_is_live_despite_a_static_mtime(mod, tmp_path):
    """The Restart Manager check, not the mtime delta, is what catches this.

    Reproduces the real case: a dev supervisor holding vodrip-devall-web.log
    with an mtime that never moves. mtime-only classification would archive a
    file that is being written.
    """
    root = _make_git_repo(str(tmp_path), commit=["README.md"], gitignore=["tmp/"])
    held = os.path.join(root, "tmp", "held-open.log")
    with open(held, "w", encoding="utf-8") as fh:
        fh.write("stable bytes\n")
    os.utime(held, (time.time() - 86400, time.time() - 86400))
    before = os.stat(held).st_mtime

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time\n"
            "f=open(sys.argv[1],'r+b')\n"
            "time.sleep(60)\n",
            held,
        ]
    )
    try:
        time.sleep(2.0)
        mod.REPO_ROOT = root
        # window=0 disables the mtime delta entirely: only the handle query and
        # the protected list can save this file.
        rec = _by_rel(mod.inventory(window=0))["tmp/held-open.log"]
        assert rec["handles"], "Restart Manager saw no holder"
        assert holder.pid in rec["handles"]
        assert rec["class"] == "live"
        assert os.stat(held).st_mtime == before, "the fixture must be static-mtime"
    finally:
        holder.terminate()
        holder.wait(timeout=30)


def test_protected_paths_are_live_and_never_dead_scratch(mod, tmp_path):
    root = _make_git_repo(str(tmp_path), commit=["README.md"], gitignore=["tmp/"])
    for rel in ("tmp/liveness.jsonl", "tmp/vodrip-devall-api.log", "tmp/dev-all.lock"):
        with open(os.path.join(root, *rel.split("/")), "w", encoding="utf-8") as fh:
            fh.write("live\n")
    mod.REPO_ROOT = root
    recs = _by_rel(mod.inventory(window=0))
    for rel in ("tmp/liveness.jsonl", "tmp/vodrip-devall-api.log", "tmp/dev-all.lock"):
        assert recs[rel]["class"] == "live", rel
        assert "protected" in recs[rel]["why"], rel
    # and the protected set outranks git-tracking, so a committed live file
    # still cannot be swept up
    assert all(r["class"] != "dead-scratch" for r in recs.values())


def test_growing_file_is_live_under_the_mtime_window(mod, tmp_path):
    """The other half of the liveness measurement: mtime/size delta, not handles.

    The writer appends on a loop rather than once at a fixed instant, because
    the window's open time depends on how fast this box starts a subprocess.
    A one-shot write can land before the window opens and the test then proves
    nothing. This one cannot miss.
    """
    root = _make_git_repo(str(tmp_path), commit=["README.md"], gitignore=["tmp/"])
    target = os.path.join(root, "tmp", "growing.log")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("a")
    os.utime(target, (time.time() - 86400, time.time() - 86400))
    stopper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time\n"
            "f=open(sys.argv[1],'ab',buffering=0)\n"
            "for _ in range(400):\n"
            "    f.write(b'x'*64)\n"
            "    f.flush()\n"
            "    time.sleep(0.1)\n",
            target,
        ]
    )
    try:
        time.sleep(2.0)  # let the writer get going, then open a window under it
        mod.REPO_ROOT = root
        rec = _by_rel(mod.inventory(window=4))["tmp/growing.log"]
        assert rec["growing"] is True, "a file being appended to was not seen growing"
        assert rec["class"] == "live"
    finally:
        stopper.terminate()
        stopper.wait(timeout=30)


# ---------------------------------------------------------------------------
# PRESERVED: reserved device names, the invisible-candidate refusal, the
# manifest completeness check, and a round-tripping rollback
# ---------------------------------------------------------------------------


def test_reserved_device_name_candidate_is_visible_and_moves(mod, tmp_path):
    """`nul` is a Windows reserved device name: abspath() collapses it into the
    device namespace, so isfile() fails and relpath() misreports the mount. The
    first archive run silently moved 17 of 18 declared candidates because of
    exactly this. longp() is the fix; this is the test that it still works."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    nul = os.path.join(root, "nul")
    with open(mod.longp(nul), "wb") as fh:
        fh.write(b"findstr stderr under powershell\n")
    mod.REPO_ROOT = root
    mod.DEAD_SCRATCH = {"nul"}

    recs = mod.inventory(window=0)
    assert "nul" in {r["rel"] for r in recs}, "the enumerator skipped the reserved name"
    assert mod.missing_candidates(recs) == [], "nul was reported invisible"
    rec = _by_rel(recs)["nul"]
    assert rec["class"] == "dead-scratch"

    rc = mod.main(["--repo", root, "archive", "--date", "2026-01-01", "--window", "0"])
    assert rc == 0
    assert not os.path.exists(mod.longp(nul))
    assert mod.verify_archive("2026-01-01")[0] == []
    rc = mod.main(["--repo", root, "restore", "--date", "2026-01-01"])
    assert rc == 0
    assert os.path.isfile(mod.longp(nul))


def test_archive_refuses_when_a_declared_candidate_is_invisible(mod, tmp_path):
    """Exit 2, not a partial archive reported as success."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    with open(os.path.join(root, "tmp", "visible.txt"), "w", encoding="utf-8") as fh:
        fh.write("here\n")
    mod.DEAD_SCRATCH = {"tmp/visible.txt", "tmp/ghost.txt"}
    rc = mod.main(["--repo", root, "archive", "--date", "2026-01-01", "--window", "0"])
    assert rc == 2, "archiving the rest and calling it success is how data goes missing"
    assert os.path.isfile(os.path.join(root, "tmp", "visible.txt")), (
        "nothing may be moved when a declared candidate is invisible"
    )


def test_selftest_rejects_five_fault_classes_and_accepts_clean_runs(mod, tmp_path):
    """verify() must reject: omitted row, byte-edited member, missing member,
    ../ traversal, smuggled protected path -- and accept two clean runs."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    assert mod.main(["--repo", root, "selftest"]) == 0


def test_manifest_round_trips_through_archive_verify_restore(mod, tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    names = {"tmp/pf3.txt": b"pytest transcript\n", "tmp/tsc_main.txt": b"tsc output\n"}
    for rel, blob in names.items():
        with open(os.path.join(root, *rel.split("/")), "wb") as fh:
            fh.write(blob)
    mod.DEAD_SCRATCH = set(names)

    assert mod.main(["--repo", root, "archive", "--date", "2026-01-01", "--window", "0"]) == 0
    assert not os.path.exists(os.path.join(root, "tmp", "pf3.txt"))
    assert mod.main(["--repo", root, "verify", "--date", "2026-01-01"]) == 0

    assert mod.main(["--repo", root, "restore", "--date", "2026-01-01"]) == 0
    for rel, blob in names.items():
        with open(os.path.join(root, *rel.split("/")), "rb") as fh:
            assert fh.read() == blob, "%s did not come back byte-identical" % rel


def test_nothing_in_the_repo_is_ever_deleted(mod):
    """The tool's contract is moves only: os.replace, never a delete.

    The single deletion in the file is cmd_selftest's own throwaway sandbox,
    which it created with tempfile.mkdtemp, and even that is pinned here.
    """
    src = open(TOOL, encoding="utf-8").read()
    head, sep, tail = src.partition("def cmd_selftest(")
    assert sep, "cmd_selftest is gone; this guard no longer knows what it is guarding"
    selftest, _, after = tail.partition("\ndef ")
    outside = head + after
    for banned in (
        "os.remove",
        "os.unlink",
        "shutil.rmtree",
        "os.rmdir",
        "RemoveFile",
        "DeleteFile",
        "RemoveDirectory",
    ):
        assert banned not in outside, (
            "%s appears outside cmd_selftest - this tool moves files, it never deletes them"
            % banned
        )
    assert "tempfile.mkdtemp" in selftest
    assert "shutil.rmtree(sandbox" in selftest
    assert selftest.count("os.remove") == 1, "the selftest's own tamper steps only"


# ---------------------------------------------------------------------------
# References: is anything actually reading this leftover?
#
# The rule is untracked AND git-ignored AND zero references in tracked files.
# The first two landed with the git vocabulary; the third is what a p3 label
# on icon.ico exposed. A 16 KB app icon that the onefile launcher reads is not
# "cheap either way", and the count that says so has to be a PATH-FORM count.
# ---------------------------------------------------------------------------


def _fake_grep(pairs):
    """A git-grep stand-in that HONOURS the pattern it is handed.

    It has to compile and apply the regex, not just yield the pairs: the whole
    claim under test is that the pattern rejects a name embedded in a longer
    one. A fake that ignored the pattern would pass every pair through and the
    assertion would be counting files, not matching anything.
    """
    def _grep(pattern):
        rx = re.compile(pattern)
        for path, line in pairs:
            if rx.search(line):
                yield path, line

    return _grep


def test_path_form_count_finds_a_real_consumer(mod):
    pairs = [("backend/app.py", '    base / "icon.ico",')]
    n, sample = mod.reference_count("icon.ico", git=_fake_grep(pairs))
    assert n == 1
    assert sample == ["backend/app.py"]


def test_a_shorter_name_is_not_a_substring_of_a_longer_one(mod):
    """`log.txt` must not be found inside `catalog.txt`.

    This is the substring trap in miniature. A bare substring count says the
    file mentioning `catalog.txt` also uses `log.txt`; on the live repo the same
    mistake scores `{}` at 1,181 hits. Every one of those is a non-match.
    """
    pairs = [("src/catalog.txt", "catalog contents"), ("src/mylog.txt", "x")]
    n, _ = mod.reference_count("log.txt", git=_fake_grep(pairs))
    assert n == 0, "a name embedded in a longer name is not a reference"


def test_a_producer_is_not_a_consumer(mod):
    """`cpSync(winExe, join(root, 'VOD-RIP.EXE'))` WRITES the file."""
    pairs = [("scripts/deploy-dist.mjs", "cpSync(winExe, join(root, 'VOD-RIP.EXE'));")]
    n, _ = mod.reference_count("VOD-RIP.EXE", git=_fake_grep(pairs))
    assert n == 0, "a file that produces a name does not depend on it"


def test_a_gitignore_rule_is_not_a_consumer(mod):
    """`.gitignore:16  VOD-RIP.EXE` is a statement ABOUT the file, not a use."""
    pairs = [(".gitignore", "VOD-RIP.EXE")]
    n, _ = mod.reference_count("VOD-RIP.EXE", git=_fake_grep(pairs))
    assert n == 0, "an ignore rule is not a dependency"


def test_the_producer_filter_is_scoped_to_the_line_not_the_file(mod):
    """Regression: file-level filtering discarded a real consumer.

    backend/__main_launcher__.py both copies build outputs and reads
    `base / "icon.ico"` for the onefile launcher. Filtering on whole-file
    content threw it away -- the most load-bearing reader of that icon in the
    repo, dropped by its own unrelated shutil.copy calls.
    """
    pairs = [("backend/__main_launcher__.py", '            base / "icon.ico",')]
    n, sample = mod.reference_count("icon.ico", git=_fake_grep(pairs))
    assert n == 1, "a file that copies things AND reads the icon consumes the icon"
    assert sample == ["backend/__main_launcher__.py"]


def test_five_reads_of_one_file_are_one_consumer(mod):
    pairs = [("index.html", "icon.ico x%d" % i) for i in range(5)]
    n, sample = mod.reference_count("icon.ico", git=_fake_grep(pairs))
    assert n == 1
    assert len(sample) == 1


def test_the_classifier_and_its_tests_are_not_consumers_of_themselves(mod):
    """Otherwise every file appears to depend on every name the tests assert on."""
    pairs = [
        ("tools/tmp_scratch_archive.py", 'name = "icon.ico"'),
        ("tools/tests/test_tmp_scratch_archive_classify.py", 'reference_count("icon.ico")'),
    ]
    n, _ = mod.reference_count("icon.ico", git=_fake_grep(pairs))
    assert n == 0


def test_the_grep_pattern_is_valid_posix_ere(mod):
    """`git grep -E` REJECTS lookbehind: "Invalid preceding regular expression".

    The failure is silent and inverted -- the pattern errors, the exit code is
    non-zero, and a caller that reads that as "no matches" reports every file as
    unreferenced. So the pattern is asserted ERE-legal, and separately checked
    to behave correctly as a regex.
    """
    import re as _re

    name = "icon.ico"
    pattern = "(^|[^A-Za-z0-9_.%-])" + _re.escape(name) + "([^A-Za-z0-9_]|$)"
    assert "(?<" not in pattern, "lookbehind is not POSIX ERE and git grep rejects it"
    rx = _re.compile(pattern)
    assert rx.search('base / "icon.ico",')
    assert not rx.search("catalog.txt")


def test_a_referenced_leftover_is_promoted_out_of_p3(mod, tmp_path):
    """The end-to-end effect: a small file WITH consumers is not `cheap either
    way`, and the entry says who reads it."""
    root = _make_git_repo(str(tmp_path), commit=["README.md"], gitignore=["tmp/"])
    target = os.path.join(root, "tmp", "asset.ico")
    with open(target, "wb") as fh:
        fh.write(b"\x00" * 800)  # 800 bytes: p3 on size alone
    os.utime(target, (time.time() - 86400, time.time() - 86400))
    with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as fh:
        fh.write('ICON = os.path.join(base, "asset.ico")\n')
    _git(root, "add", "app.py")
    _git(
        root,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "add app",
    )

    mod.REPO_ROOT = root
    plain = _by_rel(mod.inventory(window=0, refcheck=lambda p: (0, [])))["tmp/asset.ico"]
    assert plain["band"] == "p3", "fixture must be small enough to be p3 unaided"

    live = _by_rel(mod.inventory(window=0))["tmp/asset.ico"]
    assert live["nrefs"] == 1, "the tracked app.py reads the leftover"
    assert live["band"] != "p3", "a file the app still reads is not cheap either way"
    assert "referenced by 1 tracked file" in live["why"]
    assert "app.py" in live["why"]


def test_the_live_repo_icon_has_real_consumers(mod):
    """Measured, not asserted from memory: the app really does read icon.ico."""
    if not os.path.isdir(LIVE_ROOT):
        pytest.skip("live repo root not available: %s" % LIVE_ROOT)
    mod.REPO_ROOT = LIVE_ROOT
    tracked, _ = mod.git_vocabulary()
    if not tracked or "backend/__main_launcher__.py" not in tracked:
        pytest.skip("live repo layout changed")
    n, sample = mod.reference_count("icon.ico")
    assert n > 0, "icon.ico must show live consumers, or this rule proves nothing"
    assert any("__main_launcher__" in s or "index.html" in s for s in sample), sample
