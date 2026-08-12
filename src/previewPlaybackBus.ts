/**
 * One playing preview at a time. Main, explore, clip, and live players
 * register a pause callback; opening or playing any of them pauses the rest.
 */
type PauseFn = () => void;

const listeners = new Set<PauseFn>();

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
