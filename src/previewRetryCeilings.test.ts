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

const STAGES: PreviewRetryStage[] = ['session', 'playback'];

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