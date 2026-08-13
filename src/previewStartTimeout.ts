import type { PreviewRetryStage } from './previewRetry';

/**
 * Hard ceiling for the preview 'Starting…' phase — from the moment
 * `setPreviewVideoLoading(true)` fires until the player is ready (`canplay`)
 * or a terminal error surfaces. Covers the session-create POST, the attach
 * effect, the HLS manifest/fragment loads and the first frame.
 *
 * The backend YouTube create worst case is the 8s fast race + 24s fallback
 * chain; anything past 20s is a hung create (stuck yt-dlp/innerTube pass) or
 * a session whose playback never starts (e.g. window-HLS mux failure serving
 * an empty VOD playlist forever). The guard turns both into the retry UI
 * instead of an infinite spinner.
 */
export const PREVIEW_START_TIMEOUT_MS = 20_000;

export interface PreviewStartTimeoutCallbacks {
  /**
   * The start phase expired without ready/error. Return true when the
   * callback performed the terminal teardown (the guard then aborts the
   * in-flight create fetch); return false when the phase was superseded
   * (the fetch is left running — its in-flight dedup may serve the newer
   * open, and the superseded open's own continuation is discarded by its
   * generation guard).
   */
  onTimeout: (mediaUrl: string, stage: PreviewRetryStage) => boolean;
}

/**
 * One-shot guard for a single preview-open phase. One instance per open:
 * `start()` arms the timer, `markCreateResolved()` transitions the stage
 * classification to 'playback', `markReady()`/`settle()` end the phase, and
 * a handled timeout aborts the in-flight create fetch so a RETRY click never
 * inherits the hung POST (the dedup entry is freed by the abort rejection).
 */
export class PreviewStartTimeout {
  private timer: number | null = null;
  private controller: AbortController | null = null;
  private createDone = false;
  private ready = false;
  private settled = false;

  constructor(
    private readonly mediaUrl: string,
    private readonly callbacks: PreviewStartTimeoutCallbacks,
  ) {}

  /** Abort signal for the in-flight create POST (null when none in flight). */
  get signal(): AbortSignal | null {
    return this.controller?.signal ?? null;
  }

  /** True once the timeout performed its teardown — callers must not double-handle. */
  get handled(): boolean {
    return this.settled;
  }

  /** True when the session-create POST resolved (later timeouts are the playback stage). */
  get createResolved(): boolean {
    return this.createDone;
  }

  /** Arm the timeout for a new start phase (clears any previous phase's timer). */
  start(): void {
    this.clearTimer();
    this.settled = false;
    this.createDone = false;
    this.ready = false;
    const controller = new AbortController();
    this.controller = controller;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      if (this.settled || this.ready) return;
      this.settled = true;
      const stage: PreviewRetryStage = this.createDone ? 'playback' : 'session';
      let handled = false;
      try {
        handled = this.callbacks.onTimeout(this.mediaUrl, stage);
      } finally {
        if (handled) {
          controller.abort();
          this.controller = null;
        } else {
          // Superseded — leave the fetch running (dedup may serve the newer open).
          this.controller = null;
        }
      }
    }, PREVIEW_START_TIMEOUT_MS);
  }

  /** The create POST resolved — the attach phase is now the covered stage. */
  markCreateResolved(): void {
    this.createDone = true;
    this.controller = null;
  }

  /** Playback became ready — the phase is over; a pending timer is inert. */
  markReady(): void {
    this.ready = true;
    this.clearTimer();
  }

  /** A terminal error already surfaced — stop the timer and any in-flight abort. */
  settle(): void {
    this.settled = true;
    this.clearTimer();
    this.controller?.abort();
    this.controller = null;
  }

  private clearTimer(): void {
    if (this.timer != null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
