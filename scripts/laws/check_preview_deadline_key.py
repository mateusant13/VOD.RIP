"""LAW: preview-deadline-call-sites-resolve-to-table.

The invariant
------------
A deadline message is an OUTPUT, never an input.  Every site that reaches a
preview deadline must resolve its user-visible wording from the verdict table
(src/previewRetry.ts) by asking for a KEY, so that the stage the deadline was
spent in can diverge without a signature change and without a translator ever
seeing a string that was spelled at a call site.

Four things are checked over the whole src/ tree, in this order:

  1. NO ORPHAN LITERAL - a deadline message literal may appear only where it
     legitimately lives: in the table itself, in the dictionary, in a test that
     pins the table, or at the ONE enumerated live-session site of
     src/components/LivePlayerPopup.tsx (the post-accept stall abort, whose
     wording is deliberately distinct and pinned at exactly 1).  The needles are
     ASCII PREFIXES, so a quote or dash variant of the same sentence still hits.
     Every enumerated exception is PINNED to an exact count, so a documented
     exception cannot quietly expand back into a deadline nobody reviewed.  A pin
     of 0 means the count MUST stay at zero: the two live-create sites used to
     spell 'No response from server' there and were wired to the table on
     2026-10-10, so that allowance was RETIRED, not left at its old value.

  2. GUARDED - every non-test file that constructs new PreviewStartTimeout(
     must reference previewDeadlineVerdict(.  Without this, the fastest way to
     turn check 1 green is to delete the guard, and a law that can be
     satisfied by destroying its own subject is worse than no law.

  3. TABLE INTACT - src/previewRetry.ts must still declare
     PREVIEW_DEADLINE_MESSAGE_KEYS with BOTH outcome keys (stalled, timeout)
     under BOTH stages (session, playback), each with a non-empty message.

  4. GUARD PRESENT - the pinned minimum set {App.tsx, ChannelExplorePopup.tsx}
     must still construct the guard.  A deleted deadline FAILs this law.

WHAT THIS LAW IS NOT
It reads text, not TypeScript.  It cannot prove that the object it finds under
PREVIEW_DEADLINE_MESSAGE_KEYS is the one a call site imports at runtime, that
the matched text is not a comment or a shadowing local, or that a resolved
message reaches a user.  What it can do is refuse AMBIGUITY in the table it
reads - a stage or an outcome declared twice is one that wins silently - and
refuse to pass when the table, the dictionary, the guards or the pinned
exception counts have moved.  The runtime half is covered by the vitest suite,
which imports the real table and the real deadline logic.

Red input: an exact-text document that re-spells the deadline literals at their
call sites (red/deadline-call-site-literal.json).  The mutations are applied to
the checker's in-memory image of the tree only; the working tree is never
written to, so no law run can leave the repo broken.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lawchecks as lc  # noqa: E402

#: ASCII-prefixed needles.  The dash and the quote glyphs are deliberately NOT
#: part of the pattern: the point is to catch every re-spelling of the same
#: sentence, not the one exact byte sequence.
NEEDLES = (
    "Preview took too long",
    "No response from server",
    "Live session is taking too long to start",
)

#: Where a deadline literal may live, and how many times.  None = unbounded (the
#: table may be edited freely, a dictionary may gain a language, a test may pin
#: more of the table).  An INTEGER is an exact pin: the count must match, so
#: adding one more literal there is a finding a reviewer has to look at.
ALLOWED = {
    "src/previewRetry.ts": None,
    "src/i18n.tsx": None,
    "src/components/LivePlayerPopup.tsx": {
        "Preview took too long": 0,
        "No response from server": 0,
        "Live session is taking too long to start": 1,
    },
}

#: Test files may pin the table without becoming part of it.
TEST_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".spec.ts",
    ".spec.tsx",
)

#: Files that MUST still arm the start-phase deadline guard.
REQUIRED_GUARDS = ("src/App.tsx", "src/ChannelExplorePopup.tsx")

#: Files that MUST resolve the deadline through the verdict table.
REQUIRED_TABLE_USE = ("src/App.tsx", "src/ChannelExplorePopup.tsx")

TABLE_MODULE = "src/previewRetry.ts"
TABLE_NAME = "PREVIEW_DEADLINE_MESSAGE_KEYS"
REQUIRED_STAGES = ("session", "playback")
REQUIRED_OUTCOMES = ("stalled", "timeout")

GUARD_RE = re.compile(r"new\s+PreviewStartTimeout\s*\(", re.M)
VERDICT_RE = re.compile(r"previewDeadlineVerdict\s*\(", re.M)
STAGE_BLOCK_RE = re.compile(r"\n  (\w+):\s*\{(.*?)\n  \}", re.S)


def is_test(rel: str) -> bool:
    return rel.rsplit("/", 1)[-1].endswith(TEST_SUFFIXES)


def allow_bucket(rel: str):
    """Classify a source file: pinned dict, unbounded, or not allowed at all."""
    if rel in ALLOWED and ALLOWED[rel] is not None:
        return "pinned", ALLOWED[rel]
    if rel in ALLOWED:
        return "unbounded", None
    if is_test(rel):
        return "unbounded", None
    return None, None


def check_no_orphan_literal(tree: dict[str, str]) -> list[str]:
    """Check 1: no deadline literal outside the places it may live."""
    findings: list[str] = []
    for needle in NEEDLES:
        for rel in sorted(tree):
            lines = lc.lines_with(tree[rel], needle)
            if not lines:
                continue
            bucket, pins = allow_bucket(rel)
            where = rel + ":" + ",".join(str(n) for n in lines)
            if bucket is None:
                findings.append(f"{needle!r} at {where} is outside the table, the dictionary or a test")
                continue
            if bucket == "unbounded":
                continue
            if needle not in pins:
                findings.append(f"{needle!r} at {where} is not a pinned exception for {rel}")
                continue
            want = pins[needle]
            # Count OCCURRENCES, not lines: two literals on one line are two
            # literals, and a pin that counted lines would read them as one.
            seen = tree[rel].count(needle)
            if seen != want:
                findings.append(f"{needle!r} at {where} appears {seen} time(s), pinned at {want}")
    return findings


def check_guarded(tree: dict[str, str]) -> list[str]:
    """Check 2: a guard with no verdict is a guard a reader cannot trust."""
    findings: list[str] = []
    for rel in sorted(tree):
        if is_test(rel):
            continue
        if GUARD_RE.search(tree[rel]) and not VERDICT_RE.search(tree[rel]):
            findings.append(f"{rel} constructs new PreviewStartTimeout( but never calls previewDeadlineVerdict(")
    for rel in REQUIRED_TABLE_USE:
        if rel not in tree or not VERDICT_RE.search(tree[rel]):
            findings.append(f"{rel} must resolve its deadline wording through previewDeadlineVerdict(")
    return findings


def _value_after(body: str, key: re.Match) -> str | None:
    """The quoted string on the right-hand side of a KEY: ... line, or None."""
    tail = body[key.end():].lstrip()
    if not tail or tail[0] not in ("'", chr(34)) or len(tail) < 2:
        return None
    inner = tail[1:].split(tail[0], 1)[0]
    return None if not inner.strip() else inner


def check_table(tree: dict[str, str]) -> list[str]:
    """Check 3: the verdict table still declares every stage and every outcome."""
    findings: list[str] = []
    rel = TABLE_MODULE
    if rel not in tree:
        return [f"{rel} is missing - the verdict table is the subject of this law"]
    text = tree[rel]
    if TABLE_NAME not in text:
        return [f"{TABLE_NAME} is not declared in {rel} - the verdict table was deleted"]
    block = text[text.index(TABLE_NAME):]
    pairs = STAGE_BLOCK_RE.findall(block)
    counts: dict[str, int] = {}
    for name, _body in pairs:
        counts[name] = counts.get(name, 0) + 1
    for name in sorted(n for n, c in counts.items() if c > 1):
        findings.append(
            f"{TABLE_NAME} in {rel} declares stage {name} {counts[name]} times - "
            "a duplicate stage wins silently, so the key a call site resolves is ambiguous")
    stages = dict(pairs)
    missing = [s for s in REQUIRED_STAGES if s not in stages]
    if missing:
        findings.append(f"{TABLE_NAME} in {rel} must declare stage(s) {missing}, saw {sorted(stages)}")
    for stage in REQUIRED_STAGES:
        if stage not in stages:
            continue
        body = stages[stage]
        for outcome in REQUIRED_OUTCOMES:
            hits = re.findall(r"\b" + outcome + r"\b\s*:", body)
            if not hits:
                findings.append(f"{rel} stage {stage} has no {outcome} key")
                continue
            if len(hits) > 1:
                findings.append(
                    f"{rel} stage {stage} declares {outcome} {len(hits)} times - "
                    "the later key wins silently, so the message a reader sees is not the one they read")
                continue
            key = re.search(r"\b" + outcome + r"\b\s*:", body)
            if _value_after(body, key) is None:
                findings.append(f"{rel} stage {stage}.{outcome} has no non-empty message")
    return findings


def check_guard_present(tree: dict[str, str]) -> list[str]:
    """Check 4: the guard still exists.  A deleted deadline is not a pass."""
    findings: list[str] = []
    for rel in REQUIRED_GUARDS:
        if rel not in tree:
            findings.append(f"{rel} is missing - a deleted deadline FAILs this law")
        elif not GUARD_RE.search(tree[rel]):
            findings.append(f"{rel} no longer constructs new PreviewStartTimeout(")
    return findings


def main() -> int:
    root = lc.repo_root()
    lc.law_name("preview-deadline-call-sites-resolve-to-table")
    tree = lc.load_tree(root, "src")

    red = lc.apply_red_input(tree, root)
    if red is not None:
        tree = red

    findings: list[str] = []
    findings.extend(check_no_orphan_literal(tree))
    findings.extend(check_guarded(tree))
    findings.extend(check_table(tree))
    findings.extend(check_guard_present(tree))

    if findings:
        head = " | ".join(findings[:4])
        return lc.finish(lc.WORD_FAIL, f"{len(findings)} finding(s): {head}")
    return lc.finish(
        lc.WORD_PASS,
        "every deadline call site this text reader can see resolves to a key; "
        "guards present and wired",
    )


if __name__ == "__main__":
    raise SystemExit(lc.run(main))
