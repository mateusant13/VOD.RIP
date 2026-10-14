"""HOLDS: for every preview-retry stage, the FIRST-OUTPUT ceiling is at or under
that stage's TOTAL ceiling, and the two tables cover exactly the same stages.

WHY THIS IS A LAW AND NOT A TEST
A preview stage carries two deadlines. The first-output ceiling is the deadline
for "not one byte has arrived" - silence. The total bounds the same stage once
output exists, so a slow but alive pipeline is still bounded. The pair means
something only in that order:

    first-output( stage ) <= total( stage )

If a stage's first-output ceiling were above its total, the total would fire
first in every real failure, the silence/timeout distinction would collapse
back into one deadline, and every stall would be reported as a timeout instead
of as silence. Nothing in the UI contradicts that - an error still appears.
Nothing in a pass-rate contradicts that either - healthy probes still pass.
Each table looks plausible alone.

HOW IT IS CHECKED
No TypeScript is executed. The law reads src/previewRetry.ts as text, the way a
reader does, and checks:

WHAT THIS LAW IS NOT
It is a shape check over source text, and it cannot prove that the object it
finds is the one a module imports at runtime, that the matched text is not
inside a comment or a shadowing local, or that a deadline is honoured. The
runtime half is covered by src/previewRetryCeilings.test.ts, which imports the
real tables and the real previewDeadlineFire and grades them against an oracle
written from the policy. The pair is deliberate: the test cannot see a call
site that stopped reading a table, and this law cannot see a table that is read
and then ignored. Neither is claimed to cover the other.

  DECLARED ONCE    each ceiling table is declared exactly once as an object
                   literal, so no second definition can shadow the first
  STAGES DECLARED  PreviewRetryStage names the stage universe
  SAME STAGES      both tables carry exactly the same stage keys, and no key
                   twice - a duplicate is one that wins silently
  LITERAL NUMBERS  every entry is a stage: integer_ms literal; a ceiling
                   written as an expression, a comment or prose is not a
                   number a reader can check
  ORDER            for every stage, first-output <= total, reported per stage
  NOT DEGENERATE   every ceiling is a positive number of ms under 15 minutes;
                   a zero hides a deadline, and a 15-minute preview is not a
                   preview
  CONSUMED         previewDeadlineFire reads both tables by stage. A ceiling
                   nothing consumes can drift forever and be read by nobody.

RED INPUT red/preview-ceiling-first-output-above-total.json
Inverts both stages: first-output session 45_000 -> 80_000 (above its 75_000
total) and playback 20_000 -> 65_000 (above its 60_000 total). Two violations,
each reported by stage name, so the red arm proves the law reads every stage
rather than the first one it happens to find.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lawchecks as lc

LAW = 'preview-ceiling-order-first-output-under-stage-total'

SRC_REL = 'src/previewRetry.ts'
STAGE_TYPE = 'PreviewRetryStage'
FIRST_TABLE = 'PREVIEW_FIRST_OUTPUT_CEILING_MS'
TOTAL_TABLE = 'PREVIEW_STAGE_TOTAL_CEILING_MS'
CONSUMER = 'previewDeadlineFire'
MAX_CEILING_MS = 900_000

_ENTRY = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([0-9_]+)\s*,?\s*$')
_UNION = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'")


def _object_body(text, marker, findings, what):
    """Return the source between the braces of the object literal after marker.

    None means the declaration could not be read; that is a finding, not an
    empty answer, because a ceiling nobody can find cannot be checked.
    """
    if text.count(marker) != 1:
        findings.append(
            f'{SRC_REL} declares {what} {text.count(marker)} time(s), '
            + 'expected exactly one - a second definition shadows the first')
        return None
    at = text.index(marker)
    open_at = text.find('{', at)
    if open_at < 0:
        findings.append(f'{what} is declared but has no object literal to read')
        return None
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    findings.append(f'{what} is declared but its object literal never closes')
    return None


def _stage_type(text, findings):
    """The stage universe, from the union this law trusts for the key set."""
    marker = 'export type ' + STAGE_TYPE
    if text.count(marker) != 1:
        findings.append(
            f'{SRC_REL} declares type {STAGE_TYPE} {text.count(marker)} time(s), '
            + 'expected exactly one')
        return []
    at = text.index(marker)
    end = text.find(';', at)
    if end < 0:
        findings.append(
            f'{SRC_REL} declares type {STAGE_TYPE} but the declaration never '
            + 'ends with a semicolon, so the stage universe cannot be read')
        return []
    union = _UNION.findall(text[at:end])
    if not union:
        # An empty union would silently disable the coverage check below, and
        # 'never' is a legal TypeScript spelling of exactly that state.
        findings.append(
            f'{SRC_REL} declares type {STAGE_TYPE} with no readable stage '
            + 'name, so no stage universe can be checked')
    return union


def _table(text, name, findings):
    """Return {stage: ms} for one ceiling table, or {} when it cannot be read."""
    body = _object_body(text, 'export const ' + name, findings, name)
    if not body:
        return {}
    entries = {}
    for line in body.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('//'):
            continue
        m = _ENTRY.match(line)
        if not m:
            findings.append(
                f'{name}: {stripped!r} is not a stage: integer_ms literal - '
                + 'a ceiling nobody can read is not a ceiling')
            continue
        stage = m.group(1)
        if stage in entries:
            findings.append(
                f'{name} lists {stage} twice - the later entry wins silently')
            continue
        entries[stage] = int(m.group(2).replace('_', ''))
    if not entries:
        findings.append(f'{name} carries no stage entries')
    return entries


def _consumer_body(text, findings):
    """The body of the function that spends these ceilings."""
    marker = 'export function ' + CONSUMER
    if text.count(marker) != 1:
        findings.append(
            f'{SRC_REL} defines {CONSUMER} {text.count(marker)} time(s), '
            + 'expected exactly one')
        return ''
    return _object_body(text, marker, findings, CONSUMER) or ''


def _degenerate(label, entries, findings):
    for stage in sorted(entries):
        value = entries[stage]
        if value <= 0:
            findings.append(
                f'{label}[{stage}] is {value} ms - a zero ceiling hides a deadline')
        elif value > MAX_CEILING_MS:
            findings.append(
                f'{label}[{stage}] is {value} ms, over {MAX_CEILING_MS} ms - '
                + 'a preview stage that long is not a preview')


def findings_for(text: str) -> tuple[list[str], str]:
    """(findings, the passing sentence) for one image of src/previewRetry.ts.

    Separated from main() so a test can hand the checker a synthetic image of
    the file: a guard whose failure cannot be produced is not a guard, and the
    only way to show that one CAN fail is to feed the checker the source that
    breaks it.
    """
    findings = []
    universe = _stage_type(text, findings)
    first = _table(text, FIRST_TABLE, findings)
    total = _table(text, TOTAL_TABLE, findings)
    body = _consumer_body(text, findings)

    for label, entries in ((FIRST_TABLE, first), (TOTAL_TABLE, total)):
        for stage in sorted(entries):
            if universe and stage not in universe:
                findings.append(
                    f'{label} carries {stage}, which is not a stage in '
                    + f'{STAGE_TYPE} - a ceiling for a stage that does not exist')

    if first and total:
        for stage in sorted(set(first) - set(total)):
            findings.append(
                f'{FIRST_TABLE} covers {stage} but {TOTAL_TABLE} does not')
        for stage in sorted(set(total) - set(first)):
            findings.append(
                f'{TOTAL_TABLE} covers {stage} but {FIRST_TABLE} does not')

    # The union IS the stage universe, so a stage that exists and carries no
    # ceiling in EITHER table is a stage whose deadline pair nobody set.  The
    # tables used to be compared only against each other, so adding a stage to
    # PreviewRetryStage and to neither table left this law green about a stage
    # the law's own HOLDS sentence - "for every preview-retry stage" - names.
    if universe:
        for stage in sorted(set(universe) - set(first) - set(total)):
            findings.append(
                f'{STAGE_TYPE} declares {stage}, but neither ceiling table '
                + 'carries it - a stage with no ceilings has no deadline pair')

    _degenerate(FIRST_TABLE, first, findings)
    _degenerate(TOTAL_TABLE, total, findings)

    for stage in sorted(set(first) & set(total)):
        if first[stage] > total[stage]:
            findings.append(
                f'{stage}: first-output {first[stage]} ms exceeds its stage total '
                + f'{total[stage]} ms by {first[stage] - total[stage]} ms - the total '
                + 'fires first, so every stall is reported as a timeout')

    if body:
        for label in (FIRST_TABLE, TOTAL_TABLE):
            if (label + '[stage') not in body:
                findings.append(
                    f'{CONSUMER} never reads {label} - a ceiling nothing '
                    + 'consumes can drift forever and be read by nobody')

    if findings:
        # The sentence is only built for a green image: a mismatched key set
        # must read as the finding it is, not as a crash while describing it.
        return findings, ''
    pairs = ', '.join(
        f'{stage} {first[stage]}<= {total[stage]}' for stage in sorted(first))
    return findings, pairs


def main() -> int:
    root = lc.repo_root()
    lc.law_name(LAW)

    files = lc.load_named_files(root, [SRC_REL])
    mutated = lc.apply_red_input(files, root)
    tree = mutated if mutated is not None else files
    text = tree[SRC_REL]

    findings, pairs = findings_for(text)

    if findings:
        return lc.finish(
            lc.WORD_FAIL,
            f'{len(findings)} finding(s): ' + ' | '.join(findings[:4]))

    return lc.finish(
        lc.WORD_PASS,
        f'first-output ceilings are under their stage totals ({pairs})')


if __name__ == '__main__':
    raise SystemExit(lc.run(main))
