/**
 * Per-media preview retry state machine.
 *
 * A retry is always scoped to the SINGLE media that failed (`url`): the first
 * RETRY click re-runs only the failed stage (session create or playback
 * attach); after a retry fails, the next click re-runs the whole pipeline
 * end-to-end for that media. Retry counts reset when a retry succeeds or a
 * different media is opened.
 *
 * ── Deadline law (fail-fast is a MECHANISM, not an instruction) ────────────
 *
 * A wall-clock ceiling alone is forbidden: it cannot tell "slow" from "never
 * arriving", and those are different defects with different fixes. So every
 * deadline below is a PAIR — a first-output ceiling that fires when no byte has
 * arrived, plus a wall-clock total that bounds the stage once output exists.
 * WHERE the deadline was spent is the discriminator:
 *
 *   - spent BEFORE first output  → `stalled`  (named as SILENCE)
 *   - spent AFTER  first output  → `timeout`  (output arrived, never finished)
 *
 * The retry decision is a pure function of (stage, outcome). Nothing here
 * branches on a message string: `previewDeadlineMessageKey` returns an i18n
 * KEY, and callers render it with `t(key)`. A message is an output, never an
 * input — string-matching on one is the defect this module exists to prevent.
 *
 * The stages named here (`session`, `playback`) are the ones the code already
 * uses — `PreviewStartTimeout.createResolved` is the first-output signal for
 * `session`, `canplay`/`markReady` is it for `playback`. No invented taxonomy.
 */

export type PreviewRetryStage = 'session' | 'playback';

export type PreviewRetryMode = 'stage' | 'full';

export interface PreviewRetryState {
  /** Media URL the retry applies to — the scope guard. */
  url: string;
  /** Stage that failed — what a stage retry redoes. */
  stage: PreviewRetryStage;
  /** Failed retries already performed for this media. 0 = first click. */
  attempts: number;
}

/**
 * First click retries only the failed stage; every subsequent click after a
 * failed retry runs the full pipeline end-to-end for that media.
 */
export function previewRetryMode(state: PreviewRetryState): PreviewRetryMode {
  return state.attempts <= 0 ? 'stage' : 'full';
}

/**
 * Record a media failure. `wasRetry` is true when the failure happened while
 * a RETRY was in flight — that escalates the per-media attempt count so the
 * NEXT click goes full pipeline. Fresh failures (manual opens, new media)
 * always start at attempts = 0.
 */
export function previewRetryAfterError(
  prev: PreviewRetryState | null,
  url: string,
  stage: PreviewRetryStage,
  wasRetry: boolean,
): PreviewRetryState {
  const attempts = wasRetry && prev?.url === url ? prev.attempts + 1 : 0;
  return { url, stage, attempts };
}

// ── Ceilings ────────────────────────────────────────────────────────────────

/**
 * First-output ceiling per stage: the deadline for "not one byte has arrived".
 *
 * `session` is pinned to the backend's OWN hard cap on create
 * (`VODRIP_PREVIEW_CREATE_TIMEOUT_SEC`, default 45s — see the create-timeout
 * rationale in `previewStartTimeout.ts`). No first output can legitimately
 * arrive after the backend has already abandoned the create, so 45s is the
 * backend's boundary, not an invented number.
 *
 * `playback` covers attach + manifest + first fragment + first frame on a
 * session that already exists. UNMEASURED — no healthy-path sample backs it.
 * It is a proposal, and it is the one number here that a measurement should
 * replace.
 */
export const PREVIEW_FIRST_OUTPUT_CEILING_MS: Record<PreviewRetryStage, number> = {
  session: 45_000,
  playback: 20_000,
};

/**
 * Wall-clock TOTAL per stage — bounds the stage once output exists, so a slow
 * but alive pipeline is still bounded. Always >= the matching first-output
 * ceiling; a first-output ceiling above its total would be unreachable and the
 * silence/total distinction would collapse back into one deadline.
 *
 * `session` total = create (45s) + window-HLS mux poll (<=15s) + attach/first
 * frame margin (15s). `playback` total mirrors the 60s the single pre-split
 * ceiling allowed for the post-session phase.
 */
export const PREVIEW_STAGE_TOTAL_CEILING_MS: Record<PreviewRetryStage, number> = {
  session: 75_000,
  playback: 60_000,
};

// ── Classification ──────────────────────────────────────────────────────────

/**
 * Which side of the first-output line a deadline was spent on.
 * `stalled` = silence (no output at all). `timeout` = output arrived, stage
 * never completed.
 */
export type PreviewDeadlineOutcome = 'stalled' | 'timeout';

/** Which of the two ceilings a stage can trip. */
export type PreviewDeadlineFire = 'first-output' | 'total';

/** What a deadline failure means for the next RETRY click. */
export type PreviewDeadlineRetry = 'retry-stage' | 'escalate-full';

/**
 * Mechanical classification from the evidence the pipeline actually has:
 * `firstOutputArrived` is `PreviewStartTimeout.createResolved` for `session`
 * and "a frame was painted" for `playback`. There is no message argument, so
 * this cannot degrade into matching on a translated string.
 *
 * Takes no stage: whether a deadline was spent in silence or after output is
 * the same question in both stages. The stage decides the RETRY
 * (`previewDeadlineRetry`) and the key (`previewDeadlineMessageKey`), not this.
 */
export function classifyPreviewDeadline(
  firstOutputArrived: boolean,
): PreviewDeadlineOutcome {
  return firstOutputArrived ? 'timeout' : 'stalled';
}

/**
 * Which ceiling fires, computed from timings — no timers, fully testable.
 *
 * The first-output ceiling is judged on the ARRIVAL TIME of the first byte, not
 * on total elapsed: output that lands at 46s on a 45s silence ceiling is still
 * silence, even though a byte eventually arrived. Returns null while the stage
 * is still healthy (a fast success trips neither ceiling).
 */
export function previewDeadlineFire(
  stage: PreviewRetryStage,
  firstOutputAtMs: number | null,
  elapsedMs: number,
): PreviewDeadlineFire | null {
  const firstOutputCeiling = PREVIEW_FIRST_OUTPUT_CEILING_MS[stage];
  const totalCeiling = PREVIEW_STAGE_TOTAL_CEILING_MS[stage];

  // Nothing yet: only the first-output ceiling can fire, and silence wins over
  // the total because firstOutputCeiling <= totalCeiling always holds.
  if (firstOutputAtMs === null) {
    return elapsedMs >= firstOutputCeiling ? 'first-output' : null;
  }

  // Output arrived, but too late to count as on-time.
  if (firstOutputAtMs >= firstOutputCeiling) return 'first-output';

  return elapsedMs >= totalCeiling ? 'total' : null;
}

/**
 * Per-stage i18n KEY table. Both keys already exist in the pt and es
 * dictionaries (`i18n.tsx`), so this split needs no new translation and cannot
 * regress a translated user to English — `t()` falls back to the English source
 * text for a missing key.
 *
 * The split is the point: today `session` silence and `playback` overrun share
 * ONE key (`'Preview took too long — try again'`), which is exactly the
 * collapse this lane exists to undo. The two stages deliberately resolve to
 * the same key per outcome today; the table exists so a stage can diverge
 * without a signature change.
 */
const PREVIEW_DEADLINE_MESSAGE_KEYS: Record<
  PreviewRetryStage,
  Record<PreviewDeadlineOutcome, string>
> = {
  session: {
    stalled: 'No response from server',
    timeout: 'Preview took too long — try again',
  },
  playback: {
    stalled: 'No response from server',
    timeout: 'Preview took too long — try again',
  },
};

/** The i18n KEY for a deadline outcome — pass it to `t()`, never render it here. */
export function previewDeadlineMessageKey(
  stage: PreviewRetryStage,
  outcome: PreviewDeadlineOutcome,
): string {
  return PREVIEW_DEADLINE_MESSAGE_KEYS[stage][outcome];
}

/**
 * The mechanical retry decision — derived from the stage the deadline was
 * spent in, never from a message.
 *
 *   session + stalled  → escalate-full. The create POST never answered. A stage
 *     retry would re-issue the very request that did not land, so escalate
 *     (invalidate + re-mux) instead of spending a click repeating it.
 *   everything else    → retry-stage. The session exists; re-attach to it.
 *
 * The single escalation rule is the only branch, and it is on `stage`.
 */
export function previewDeadlineRetry(
  stage: PreviewRetryStage,
  outcome: PreviewDeadlineOutcome,
): PreviewDeadlineRetry {
  return stage === 'session' && outcome === 'stalled' ? 'escalate-full' : 'retry-stage';
}

/** Everything a call site needs, derived once so no branch is written there. */
export interface PreviewDeadlineVerdict {
  stage: PreviewRetryStage;
  outcome: PreviewDeadlineOutcome;
  /** Which side of the first-output line the deadline was spent on. */
  spent: 'before-first-output' | 'after-first-output';
  /** Pass to `t()` — an i18n KEY, never a pre-translated string. */
  messageKey: string;
  retry: PreviewDeadlineRetry;
}

/**
 * The single seam a call site uses: hand it the stage and whether output
 * arrived, and every downstream decision follows. Nothing here takes a message,
 * so a caller cannot branch on one.
 */
export function previewDeadlineVerdict(input: {
  stage: PreviewRetryStage;
  firstOutputArrived: boolean;
}): PreviewDeadlineVerdict {
  const { stage, firstOutputArrived } = input;
  const outcome = classifyPreviewDeadline(firstOutputArrived);
  return {
    stage,
    outcome,
    spent: firstOutputArrived ? 'after-first-output' : 'before-first-output',
    messageKey: previewDeadlineMessageKey(stage, outcome),
    retry: previewDeadlineRetry(stage, outcome),
  };
}