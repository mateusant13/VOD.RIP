import { useSyncExternalStore } from 'react';

/**
 * External store for the live preview playhead (~4 Hz timeupdate ticks).
 * App no longer holds `previewTimeUi` state — it writes into this store, so a
 * tick rebuilds ONLY the subscribing consumers (timeline bar + chat panel
 * wrapper) instead of the whole App tree.
 *
 * getSnapshot MUST be the stable module fn below: `useSyncExternalStore` with a
 * snapshot that allocates a fresh object per call is an infinite-render
 * landmine (mirrors the useFeature finding). A bare number snapshot is stable.
 */
let current = 0;
const listeners = new Set<() => void>();

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function getSnapshot(): number {
  return current;
}

export function setPreviewTime(t: number): void {
  if (t === current) return;
  current = t;
  for (const cb of listeners) cb();
}

export function resetPreviewTime(): void {
  setPreviewTime(0);
}

export function getPreviewTime(): number {
  return current;
}

export function usePreviewTime(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}