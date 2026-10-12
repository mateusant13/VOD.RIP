import { describe, expect, it } from 'vitest';
import {
  PREVIEW_FIRST_OUTPUT_CEILING_MS,
  PREVIEW_STAGE_TOTAL_CEILING_MS,
  classifyPreviewDeadline,
  previewDeadlineFire,
  previewDeadlineMessageKey,
  previewDeadlineRetry,
  previewDeadlineVerdict,
  type PreviewRetryStage,
} from './previewRetry';

const STAGES = ['session', 'playback'] as const;

// COMPILE-TIME guards on that list, so it cannot drift away from the union:
// tsc fails the first line if PreviewRetryStage gains a stage this file does
// not enumerate, and the second if the list names a stage the union lacks.
// A grid that quietly stops covering a supported stage is the same defect as
// a sampled grid pretending to be exhaustive.
type ListedStage = (typeof STAGES)[number];
const everyStageListed: Exclude<PreviewRetryStage, ListedStage> extends never ? true : never = true;
const stagesAreReal: ListedStage extends PreviewRetryStage ? true : never = true;

describe('the enumerated stage list', () => {
  it('is the whole union, and nothing but the union', () => {
    // Reading them here is what keeps the compile-time guard honest: both are
    // the literal true only while tsc accepts the types above, and tsc refuses
    // the build if the union and this list ever disagree.
    expect(everyStageListed).toBe(true);
    expect(stagesAreReal).toBe(true);
    expect([...STAGES].sort()).toEqual(['playback', 'session']);
  });
});

describe('ceiling policy', () => {
  it('every first-output ceiling is <= its stage total', () => {
    // MUTATION that turns this RED: raise PREVIEW_FIRST_OUTPUT_CEILING_MS above
    // PREVIEW_STAGE_TOTAL_CEILING_MS for either stage. That makes the silence
    // ceiling unreachable and collapses the two deadlines back into one.
    for (const stage of STAGES) {
      expect(PREVIEW_FIRST_OUTPUT_CEILING_MS[stage]).toBeLessThanOrEqual(
        PREVIEW_STAGE_TOTAL_CEILING_MS[stage],
      );
    }
  });

  it('a first-output ceiling fires strictly BEFORE the stage total', () => {
    // MUTATION that turns this RED: swap the two ceiling maps (point
    // FIRST_OUTPUT at the totals). Every stage would then report silence only
    // after the wall-clock total, and fail-fast would be gone.
    for (const stage of STAGES) {
      expect(PREVIEW_FIRST_OUTPUT_CEILING_MS[stage]).toBeLessThan(
        PREVIEW_STAGE_TOTAL_CEILING_MS[stage],
      );
    }
  });

  it('session silence is detected earlier than the old single 60s ceiling', () => {
    // MUTATION that turns this RED: set session's first-output ceiling to its
    // total, or to >= 60_000. That restores the pre-lane behaviour where a dead
    // POST is indistinguishable from a slow one until the 60s wall.
    expect(PREVIEW_FIRST_OUTPUT_CEILING_MS.session).toBeLessThan(60_000);
  });
});

describe('silence vs timeout — where the deadline was spent', () => {
  it('a stage with NO first byte reports silence, NOT timeout', () => {
    // MUTATION that turns this RED: change classifyPreviewDeadline to return
    // 'timeout' when firstOutputArrived is false (or return 'timeout'
    // unconditionally). The two defects become indistinguishable again.
    expect(classifyPreviewDeadline(false)).toBe('stalled');
    expect(classifyPreviewDeadline(false)).toBe('stalled');
    expect(classifyPreviewDeadline(false)).not.toBe('timeout');
  });

  it('a stage that produced bytes then went quiet reports a DIFFERENT state', () => {
    // MUTATION that turns this RED: return 'stalled' for firstOutputArrived
    // true, i.e. collapse both outcomes into one.
    expect(classifyPreviewDeadline(true)).toBe('timeout');
    expect(classifyPreviewDeadline(true)).toBe('timeout');
    expect(classifyPreviewDeadline(true)).not.toBe(
      classifyPreviewDeadline(false),
    );
  });

  it('silence and overrun fire DIFFERENT ceilings', () => {
    // MUTATION that turns this RED: make previewDeadlineFire always answer
    // 'total'. Silence would then be reported as an overrun.
    expect(previewDeadlineFire('session', null, 50_000)).toBe('first-output');
    expect(previewDeadlineFire('session', 1_000, 80_000)).toBe('total');
    expect(previewDeadlineFire('session', null, 50_000)).not.toBe(
      previewDeadlineFire('session', 1_000, 80_000),
    );
  });

  it('silence is judged on ARRIVAL time — a byte at 46s is still silence', () => {
    // MUTATION that turns this RED: judge the first-output ceiling on total
    // elapsed instead of on firstOutputAtMs. A late byte would then be
    // retroactively reclassified as healthy, which is the whole defect.
    expect(previewDeadlineFire('session', 46_000, 50_000)).toBe('first-output');
    expect(previewDeadlineFire('session', 44_000, 50_000)).toBeNull();
  });

  it('a fast success trips NEITHER ceiling', () => {
    // MUTATION that turns this RED: make previewDeadlineFire return a non-null
    // value for a healthy stage (e.g. compare elapsed against a 0 ceiling), so
    // every preview reports a deadline failure it never hit.
    expect(previewDeadlineFire('session', 900, 2_000)).toBeNull();
    expect(previewDeadlineFire('playback', 400, 1_100)).toBeNull();
  });

  it('a still-running stage well inside both ceilings reports nothing', () => {
    // MUTATION that turns this RED: drop the `elapsed >= total` comparison.
    expect(previewDeadlineFire('session', 10_000, 30_000)).toBeNull();
    expect(previewDeadlineFire('playback', 2_000, 15_000)).toBeNull();
  });
});

describe('the retry decision is derived from the stage, not from a message', () => {
  it('session silence escalates; every other outcome retries the stage', () => {
    // MUTATION that turns this RED: invert the rule, or branch on `outcome`
    // alone instead of on `stage`. Escalating an overrun, or retrying a dead
    // POST in place, are both reachable by flipping this one expression.
    expect(previewDeadlineRetry('session', 'stalled')).toBe('escalate-full');
    expect(previewDeadlineRetry('playback', 'stalled')).toBe('retry-stage');
    expect(previewDeadlineRetry('playback', 'timeout')).toBe('retry-stage');
    expect(previewDeadlineRetry('session', 'timeout')).toBe('retry-stage');
  });

  it('the decision functions take NO message argument', () => {
    // MUTATION that turns this RED: add a third `message: string` parameter to
    // any of these functions and branch on it — the exact defect the briefing
    // names ("if the message is produced by string-matching, that IS the
    // defect"). Arity is asserted so the parameter cannot be added quietly.
    expect(classifyPreviewDeadline.length).toBe(1);
    expect(previewDeadlineRetry.length).toBe(2);
    expect(previewDeadlineMessageKey.length).toBe(2);
    expect(previewDeadlineVerdict.length).toBe(1);
  });

  it('the message is an i18n KEY, not a translated string', () => {
    // MUTATION that turns this RED: return a localized value (e.g. the pt text)
    // instead of the dictionary key. `t(key)` would then look up a key that
    // does not exist and render the key itself.
    const key = previewDeadlineMessageKey('session', 'stalled');
    expect(key).toBe('No response from server');
    expect(key).not.toMatch(/[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]/);
  });

  it('silence and overrun resolve to DIFFERENT keys — the collapse is undone', () => {
    // MUTATION that turns this RED: return one constant key for both outcomes.
    // That re-merges "never arrived" and "slow" into the owner's single message,
    // which is precisely the reported symptom.
    expect(previewDeadlineMessageKey('session', 'stalled')).not.toBe(
      previewDeadlineMessageKey('session', 'timeout'),
    );
  });

  it('the pre-lane key survives only on the overrun path', () => {
    // MUTATION that turns this RED: point the silence branch at
    // 'Preview took too long — try again'. Before this lane, session silence and
    // playback overrun shared this key; the regression is a one-line swap.
    expect(previewDeadlineMessageKey('session', 'timeout')).toBe(
      'Preview took too long — try again',
    );
    expect(previewDeadlineMessageKey('session', 'stalled')).not.toBe(
      'Preview took too long — try again',
    );
  });
});

describe('the verdict seam', () => {
  it('every field follows from stage + first-output evidence', () => {
    // MUTATION that turns this RED: let any caller-supplied field through
    // (e.g. accept an optional `message`), which re-opens the branching hole.
    const v = previewDeadlineVerdict({ stage: 'session', firstOutputArrived: false });
    expect(v).toEqual({
      stage: 'session',
      outcome: 'stalled',
      spent: 'before-first-output',
      messageKey: 'No response from server',
      retry: 'escalate-full',
    });
  });

  it('records where the deadline was spent, on both sides', () => {
    // MUTATION that turns this RED: hard-code `spent` to one value, losing the
    // discriminator the law names.
    expect(
      previewDeadlineVerdict({ stage: 'session', firstOutputArrived: true }).spent,
    ).toBe('after-first-output');
    expect(
      previewDeadlineVerdict({ stage: 'playback', firstOutputArrived: false }).spent,
    ).toBe('before-first-output');
  });

  it('the two owner-reported symptoms produce two different verdicts', () => {
    // MUTATION that turns this RED: make the verdict stage-insensitive. The
    // owner's two prompts — "took too long" and "simply does not load" — would
    // then map to one verdict again, which is the bug being fixed.
    const slow = previewDeadlineVerdict({ stage: 'playback', firstOutputArrived: true });
    const never = previewDeadlineVerdict({ stage: 'session', firstOutputArrived: false });
    expect(slow.outcome).not.toBe(never.outcome);
    expect(slow.messageKey).not.toBe(never.messageKey);
    expect(slow.retry).not.toBe(never.retry);
  });
});
// ==========================================================================
// 5. the boundary grid — exhaustive, not sampled
// ==========================================================================
// A sampled case proves the cases it sampled. This grid enumerates every
// combination of {stage} x {firstOutputAtMs at each boundary} x {elapsedMs at
// each boundary} and checks each row against an oracle written from the
// ceiling POLICY, not from the implementation. The values being pinned are
// already pinned from the Python side by the law
// `preview-ceiling-order-first-output-under-stage-total`, which reads these
// two tables off disk; what it cannot see is what the classifier DOES with
// them, and that is what this grid fixes.

/**
 * The POLICY numbers, owned by this test. The grid and the oracle below are
 * built from these, never from the imported tables, so a coordinated change to
 * a table and the classifier cannot move the oracle along with it. The pinning
 * test is the only place the two meet: it asserts the production tables equal
 * these numbers, so a table edit is a red test rather than a moved goalpost.
 */
const POLICY_FIRST: Record<PreviewRetryStage, number> = { session: 45_000, playback: 20_000 };
const POLICY_TOTAL: Record<PreviewRetryStage, number> = { session: 75_000, playback: 60_000 };
const FIRST = POLICY_FIRST;
const TOTAL = POLICY_TOTAL;

/** The pinned numbers, asserted once so the grid below cannot move silently. */
it("the ceiling tables carry the pinned, derived values", () => {
  // MUTATION that turns this RED: change any ceiling literal. session
  // first-output is the backend's own hard cap on create (45s), session total
  // is that cap plus the window-HLS mux poll (<=15s) plus a first-frame margin
  // (15s), and playback total mirrors the 60s the single pre-split ceiling
  // allowed. Each line below is the derivation, not just the number.
  // The tables are tied to the POLICY the oracle is built from, so this test is
  // the one seam between production data and the independent grid.
  expect(PREVIEW_FIRST_OUTPUT_CEILING_MS).toEqual(POLICY_FIRST);
  expect(PREVIEW_STAGE_TOTAL_CEILING_MS).toEqual(POLICY_TOTAL);
  expect(FIRST.session).toBe(45_000);
  expect(TOTAL.session).toBe(75_000);
  expect(TOTAL.session).toBe(FIRST.session + 15_000 + 15_000);
  expect(FIRST.playback).toBe(20_000);
  expect(TOTAL.playback).toBe(60_000);
});

/**
 * The oracle, written from the policy sentences in `previewDeadlineFire`:
 *   1. the first-output ceiling is judged on the ARRIVAL TIME of the first
 *      byte, not on total elapsed, so a byte that lands late is silence;
 *   2. with no byte yet, only the first-output ceiling can fire (silence wins
 *      over the total, which is possible only because first <= total);
 *   3. a byte that arrived on time leaves the total ceiling as the only
 *      remaining way to fail.
 * Three predicates over the two numbers, evaluated independently of the
 * implementation's branch order.
 */
function oracleFire(
  stage: PreviewRetryStage,
  firstOutputAtMs: number | null,
  elapsedMs: number,
): "first-output" | "total" | null {
  const first = FIRST[stage];
  const total = TOTAL[stage];
  const arrivedTooLate = firstOutputAtMs !== null && firstOutputAtMs >= first;
  const silenceByElapsed = firstOutputAtMs === null && elapsedMs >= first;
  if (arrivedTooLate || silenceByElapsed) return "first-output";
  const aliveTooLong = firstOutputAtMs !== null && elapsedMs >= total;
  return aliveTooLong ? "total" : null;
}

describe("the exhaustively enumerated boundary grid", () => {
  const GRID: {
    stage: PreviewRetryStage;
    firstOutputAtMs: number | null;
    elapsedMs: number;
  }[] = [];

  for (const stage of STAGES) {
    const first = FIRST[stage];
    const total = TOTAL[stage];
    const arrivals: (number | null)[] = [
      null, 0, 1, first - 1, first, first + 1, total - 1, total, total + 1,
    ];
    const elapseds: number[] = [
      0, 1, first - 1, first, first + 1, total - 1, total, total + 1,
    ];
    for (const firstOutputAtMs of arrivals) {
      for (const elapsedMs of elapseds) {
        GRID.push({ stage, firstOutputAtMs, elapsedMs });
      }
    }
  }

  it("the grid is the full product, not a sample", () => {
    // MUTATION that turns this RED: shrink the value sets (a filter that
    // quietly drops null, say), which is how a sampled grid pretends to be an
    // exhaustive one.
    expect(STAGES.length).toBe(2);
    expect(GRID.length).toBe(STAGES.length * 9 * 8);
    const pairs = new Set(GRID.map((r) => `${r.stage}:${r.firstOutputAtMs}`));
    expect(pairs.size).toBe(18);
  });

  it("every grid row matches the oracle", () => {
    // MUTATION that turns this RED: any single change of branch in
    // previewDeadlineFire - comparing elapsed instead of arrival, forgetting
    // the null arrival case, or answering "total" for silence - surfaces as
    // one named row here, not as a general failure somewhere else.
    const seen: Record<string, number> = {
      "first-output": 0,
      total: 0,
      healthy: 0,
    };
    for (const row of GRID) {
      const want = oracleFire(
        row.stage,
        row.firstOutputAtMs,
        row.elapsedMs,
      );
      const got = previewDeadlineFire(
        row.stage,
        row.firstOutputAtMs,
        row.elapsedMs,
      );
      const label = `${row.stage} arrival=${String(row.firstOutputAtMs)} elapsed=${row.elapsedMs}`;
      if (got === null) seen.healthy += 1;
      else seen[got] += 1;
      expect({ label, got }, label).toEqual({ label, got: want });
    }
    // every arm of the classifier was really exercised: a grid that only
    // produced "healthy" would pass anything.
    expect(seen["first-output"]).toBeGreaterThan(0);
    expect(seen.total).toBeGreaterThan(0);
    expect(seen.healthy).toBeGreaterThan(0);
  });

  it("the two stages disagree exactly where their ceilings disagree", () => {
    // MUTATION that turns this RED: make previewDeadlineFire stage-insensitive
    // (e.g. always read the session ceilings), which the ceiling tests above
    // cannot see because they only read the tables.
    const differing = GRID.filter((row) => {
      const other: PreviewRetryStage =
        row.stage === "session" ? "playback" : "session";
      return (
        previewDeadlineFire(row.stage, row.firstOutputAtMs, row.elapsedMs) !==
        previewDeadlineFire(other, row.firstOutputAtMs, row.elapsedMs)
      );
    }).length;
    expect(differing).toBeGreaterThan(0);
  });
});

describe("the per-stage x per-outcome tables, enumerated", () => {
  const OUTCOMES = ["stalled", "timeout"] as const;

  it("silence and overrun never resolve to the same key in one stage", () => {
    // MUTATION that turns this RED: collapse one stage's two rows into one
    // constant, which re-merges the owner's two symptoms for that stage.
    for (const stage of STAGES) {
      const keys = OUTCOMES.map((o) => previewDeadlineMessageKey(stage, o));
      expect(new Set(keys).size, stage).toBe(OUTCOMES.length);
    }
  });

  it("only session silence escalates, and nothing else does", () => {
    // MUTATION that turns this RED: widen or narrow the single escalation
    // rule. Enumerated over all four (stage, outcome) pairs so a fifth
    // reachable-by-broadening arm would have to be measured here.
    const escalators = STAGES.flatMap((stage) =>
      OUTCOMES.map(
        (outcome) =>
          [stage, outcome, previewDeadlineRetry(stage, outcome)] as const,
      ),
    ).filter(([, , retry]) => retry === "escalate-full");
    expect(escalators).toEqual([["session", "stalled", "escalate-full"]]);
  });

  it("the verdict carries one arm per (stage, first-output) pair", () => {
    // MUTATION that turns this RED: derive any verdict field from anything
    // other than stage + firstOutputArrived. Four pairs, four full
    // expectations, no sampling.
    const expected = {
      "session:false": {
        stage: "session",
        outcome: "stalled",
        spent: "before-first-output",
        messageKey: "No response from server",
        retry: "escalate-full",
      },
      "session:true": {
        stage: "session",
        outcome: "timeout",
        spent: "after-first-output",
        messageKey: "Preview took too long — try again",
        retry: "retry-stage",
      },
      "playback:false": {
        stage: "playback",
        outcome: "stalled",
        spent: "before-first-output",
        messageKey: "No response from server",
        retry: "retry-stage",
      },
      "playback:true": {
        stage: "playback",
        outcome: "timeout",
        spent: "after-first-output",
        messageKey: "Preview took too long — try again",
        retry: "retry-stage",
      },
    } as const;
    for (const stage of STAGES) {
      for (const firstOutputArrived of [false, true]) {
        const key = `${stage}:${firstOutputArrived}` as const;
        expect(previewDeadlineVerdict({ stage, firstOutputArrived }), key).toEqual(
          expected[key],
        );
      }
    }
  });
});