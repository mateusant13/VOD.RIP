/**
 * One playing preview at a time. Main, explore, clip, and live players
 * register a pause callback; opening or playing any of them pauses the rest.
 *
 * Auto-pause guard: a preview that finishes loading (async, seconds after it
 * was opened) pauses the other players so IT becomes the one playing. That
 * must never kill a playback the user explicitly resumed moments ago — any
 * user-initiated play/resume within UNPAUSE_GUARD_MS before the load-complete
 * event suppresses that event's auto-pause (and any unpause that happened
 * while the loading preview was loading suppresses it too, the "mini preview
 * open and loading" rule). User-initiated plays (togglePlay & co.) still
 * pause the rest via the plain pauseOtherPreviews() — only the AUTOMATIC
 * load-complete pause is guarded.
 */
type PauseFn = () => void;

const listeners = new Set<PauseFn>();

/** How long a user-initiated unpause shields other players from an
 *  auto-pause triggered by a preview finishing its async load. */
export const UNPAUSE_GUARD_MS = 2000;

let lastUserUnpauseAt = 0;

/** Record a user-initiated play/resume (togglePlay, click-to-play, space). */
export function noteUserUnpause(): void {
  lastUserUnpauseAt = Date.now();
}

export function recentlyUserUnpaused(now = Date.now()): boolean {
  return now - lastUserUnpauseAt <= UNPAUSE_GUARD_MS;
}

/** Test-only reset of the module-level unpause timestamp. */
export function resetPreviewPlaybackBusForTests(): void {
  lastUserUnpauseAt = 0;
}

export function registerPreviewPlayback(pause: PauseFn): () => void {
  listeners.add(pause);
  return () => {
    listeners.delete(pause);
  };
}

export function pauseOtherPreviews(except?: PauseFn): void {
  for (const pause of listeners) {
    if (pause !== except) {
      try { pause(); } catch { /* ignore */ }
    }
  }
}

/**
 * Auto-pause from a preview finishing loading: pauses the other players
 * UNLESS the user unpaused something recently (within UNPAUSE_GUARD_MS), or
 * at/after `loadStartedAt` — an async load must never kill a playback the
 * user explicitly resumed while it was loading.
 */
export function autoPauseOtherPreviews(loadStartedAt?: number): void {
  if (recentlyUserUnpaused()) return;
  if (loadStartedAt != null && lastUserUnpauseAt >= loadStartedAt) return;
  pauseOtherPreviews();
}
