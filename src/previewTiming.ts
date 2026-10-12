import { apiPost } from './hooks/useApiClient';

export type PreviewTimingSurface = 'main' | 'explore';

type TimingPayload = {
  platform: string;
  surface: PreviewTimingSurface;
  event: string;
  session_id?: string;
  open_ms?: number;
  seek_ms?: number;
  detail?: string;
};

/** Fire-and-forget — surfaces in uvicorn console via POST /api/preview/timing. */
function emitTiming(payload: TimingPayload): void {
  void apiPost<{ ok: boolean }>('/api/preview/timing', payload).catch(() => {});
}

/** Gesture start (pointerdown/click) captured before React mount so the
 *  click→markOpen gap (main-thread stalls, re-render storms) is measurable. */
let _gestureStartMs = 0;

export function notePreviewGesture(): void {
  _gestureStartMs = performance.now();
}

/** Per-preview timing tracker (main panel or channel explore popup). */
export class PreviewTiming {
  private tOpen = 0;
  private tSeek = 0;
  private sessionId = '';
  private firstPlayableSent = false;
  private clickToOpenMs = -1;
  /** Key of the open this tracker is measuring (see isRetryOf). */
  private openedKey = '';

  constructor(
    private readonly platform: string,
    private readonly surface: PreviewTimingSurface,
  ) {}

  markOpen(detail?: string): void {
    this.tOpen = performance.now();
    this.tSeek = 0;
    this.firstPlayableSent = false;
    // Consume the gesture timestamp — only trust it when it's fresh (<15s).
    const gap = _gestureStartMs > 0 ? this.tOpen - _gestureStartMs : -1;
    this.clickToOpenMs = gap >= 0 && gap < 15_000 ? gap : -1;
    _gestureStartMs = 0;
    this.openedKey = detail ?? '';
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event: 'preview_open',
      open_ms: 0,
      detail:
        `${detail ?? ''} click→open=` +
        (this.clickToOpenMs >= 0 ? `${this.clickToOpenMs.toFixed(0)}ms` : 'unknown'),
    });
  }

  /**
   * Is this the SAME open we already measured, re-running?
   *
   * The popup's open effect depends on sessionRetryTick, so a retry re-runs it.
   * Calling markOpen again there was wrong twice over: the gesture had already
   * been consumed, so it reported click→open=-1, and it RESTARTED the clock, so
   * a retried open looked faster than the click the user actually made — the
   * metric improving as the experience got worse. A retry keeps the original
   * origin and is recorded as its own event.
   */
  isRetryOf(detail?: string): boolean {
    return this.tOpen > 0 && this.openedKey === (detail ?? '');
  }

  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
  }

  private openMs(): number {
    return this.tOpen > 0 ? performance.now() - this.tOpen : 0;
  }

  mark(event: string, detail?: string): void {
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event,
      session_id: this.sessionId || undefined,
      open_ms: this.openMs(),
      detail,
    });
  }

  markFirstPlayable(detail?: string): void {
    if (this.firstPlayableSent) return;
    this.firstPlayableSent = true;
    const ms = this.openMs();
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event: 'first_playable',
      session_id: this.sessionId || undefined,
      open_ms: ms,
      detail: detail ?? `open→playable=${ms.toFixed(0)}ms`,
    });
    // Full-path summary: the one line that accounts for the whole click.
    //
    // When no gesture was captured, clickToOpenMs is -1 and this used to report
    // `click→playable = open→playable` — a number wearing the name of a segment
    // it did not contain. On the explore surface that was EVERY click (the
    // popup never called notePreviewGesture), so the log read green while the
    // user waited ~26 s. An unmeasurable segment is now reported as unknown and
    // never silently substituted.
    const measured = this.clickToOpenMs >= 0;
    const total = measured ? this.clickToOpenMs + ms : -1;
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event: 'full_path',
      session_id: this.sessionId || undefined,
      open_ms: measured ? total : ms,
      detail: measured
        ? `click→open=${this.clickToOpenMs.toFixed(0)} open→playable=${ms.toFixed(0)} ` +
          `click→playable=${total.toFixed(0)}ms`
        : `click→open=unknown open→playable=${ms.toFixed(0)} ` +
          `click→playable=unknown (no gesture captured — this is open→playable only)`,
    });
  }

  /**
   * Terminal event for a click that never reached a playable frame.
   *
   * Without it the last word in the log was the fast `preview_open` line
   * (20-29 ms), so a click that hung for 26 s and then failed read as a success
   * in the very log kept to catch it. A failed click must state its own
   * duration.
   */
  markFailed(reason: string): void {
    // A failure AFTER the first playable frame is not a failed click: the
    // preview opened and played, so this is a stall/playback event. Reporting
    // it as preview_failed would make a working open look broken.
    if (this.firstPlayableSent) return;
    const ms = this.openMs();
    const total = this.clickToOpenMs >= 0 ? this.clickToOpenMs + ms : -1;
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event: 'preview_failed',
      session_id: this.sessionId || undefined,
      open_ms: ms,
      detail:
        `click→fail=${total >= 0 ? `${total.toFixed(0)}ms` : 'unknown'} ` +
        `open→fail=${ms.toFixed(0)}ms reason=${reason}`,
    });
  }

  markSeekStart(positionSec: number): void {
    this.tSeek = performance.now();
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event: 'seek_start',
      session_id: this.sessionId || undefined,
      open_ms: this.openMs(),
      detail: `pos=${positionSec.toFixed(1)}s`,
    });
  }

  markSeekPlayable(detail?: string): void {
    const seekMs = this.tSeek > 0 ? performance.now() - this.tSeek : 0;
    emitTiming({
      platform: this.platform,
      surface: this.surface,
      event: 'seek_playable',
      session_id: this.sessionId || undefined,
      open_ms: this.openMs(),
      seek_ms: seekMs,
      detail: detail ?? `seek→playable=${seekMs.toFixed(0)}ms`,
    });
    this.tSeek = 0;
  }
}

/** Wait until <video> is actually playing, then log seek_playable once. */
export function waitVideoPlayable(
  video: HTMLVideoElement,
  timing: PreviewTiming,
  timeoutMs = 30_000,
): void {
  if (video.readyState >= 3 && !video.paused && video.currentTime > 0.02) {
    timing.markSeekPlayable();
    return;
  }
  const t0 = performance.now();
  const onPlaying = () => {
    if (video.currentTime > 0.01 || video.readyState >= 3) {
      cleanup();
      timing.markSeekPlayable();
    }
  };
  const tick = () => {
    if (performance.now() - t0 > timeoutMs) {
      cleanup();
      timing.markSeekPlayable('seek_playable_timeout');
      return;
    }
    if (video.readyState >= 3 && !video.paused && video.currentTime > 0.02) {
      cleanup();
      timing.markSeekPlayable();
      return;
    }
    if (video.paused && video.readyState >= 2) {
      video.muted = true;
      void video.play().catch(() => {});
    }
    raf = requestAnimationFrame(tick);
  };
  let raf = requestAnimationFrame(tick);
  const cleanup = () => {
    video.removeEventListener('playing', onPlaying);
    if (raf) cancelAnimationFrame(raf);
  };
  video.addEventListener('playing', onPlaying);
}
