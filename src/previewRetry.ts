/**
 * Per-media preview retry state machine.
 *
 * A retry is always scoped to the SINGLE media that failed (`url`): the first
 * RETRY click re-runs only the failed stage (session create or playback
 * attach); after a retry fails, the next click re-runs the whole pipeline
 * end-to-end for that media. Retry counts reset when a retry succeeds or a
 * different media is opened.
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
