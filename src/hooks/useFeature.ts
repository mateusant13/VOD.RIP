import { useSyncExternalStore, useCallback } from 'react';
import { getEnabledFeatures } from '../lib/featureFlags';

let version = 0;
const subs = new Set<() => void>();
function subscribe(cb: () => void) { subs.add(cb); return () => subs.delete(cb); }
export function notifyFeatureChange(): void { version++; for (const cb of subs) cb(); }

export function useFeature(id: string): boolean {
  const snap = useSyncExternalStore(
    subscribe,
    () => !!getEnabledFeatures()[id],
    () => false,
  );
  return snap;
}

export function useEnabledFeatures(): Record<string, boolean> {
  return useSyncExternalStore(subscribe, () => getEnabledFeatures(), () => ({} as Record<string, boolean>));
}
