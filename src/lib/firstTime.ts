/**
 * Minimal namespaced first-time/tutorial flag system.
 *
 * localStorage keys are namespaced under `vodrip.firstTime.` so a Tutorial
 * reset (resetAll) can clear every first-run message the app knows about
 * without touching any other stored state. `isFirstTime(name)` is true while
 * the flag has never been set — no timer, no expiry: "seen" sticks until the
 * user explicitly re-arms the tutorials.
 */

const PREFIX = 'vodrip.firstTime.';

export function isFirstTime(name: string): boolean {
  try {
    return localStorage.getItem(PREFIX + name) !== '1';
  } catch {
    // storage blocked (private mode / quota) — assume first time so the
    // message still gets a chance to show; markSeen below also no-ops.
    return true;
  }
}

export function markSeen(name: string): void {
  try {
    localStorage.setItem(PREFIX + name, '1');
  } catch {
    /* storage unavailable — the flag simply won't persist this session */
  }
}

/** Re-arm every first-time message (Tutorial button in Settings). */
export function resetAll(): void {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith(PREFIX)) doomed.push(key);
    }
    for (const key of doomed) localStorage.removeItem(key);
  } catch {
    /* storage unavailable */
  }
}
