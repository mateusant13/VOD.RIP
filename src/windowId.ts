/**
 * Stable unique ids for floating windows.
 *
 * WHY THIS EXISTS: `crypto.randomUUID()` is only exposed in a SECURE CONTEXT
 * (https, or http on localhost). Reached over plain http on a LAN address the
 * property is `undefined`, so the bare call raised
 * `TypeError: crypto.randomUUID is not a function`.
 *
 * That throw mattered more than a normal event-handler exception, because both
 * call sites pass the id through a `setState` UPDATER. React invokes an updater
 * during the update, so the TypeError surfaces as a render-phase error - which,
 * with no error boundary above it, unmounts the whole app and presents the user
 * with a blank screen. The gesture that reaches `openExplorePlayer` is dragging
 * a preview into a frame cell (`handleDropCell`), so "drag a preview in and the
 * screen goes blank" is exactly this failure.
 *
 * `crypto.getRandomValues` is NOT secure-context gated, so it is the fallback
 * that keeps ids collision-free rather than degrading to a counter.
 */

function randomHex(bytes: number): string {
  const out = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(out);
  return Array.from(out, (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * A unique id for a preview window.
 *
 * Prefers `crypto.randomUUID`, falls back to `getRandomValues` on an insecure
 * context, and only degrades to a counter if neither exists. The counter is a
 * last resort rather than a silent one: it is still unique within the session,
 * which is all these ids are used for (z-order ranking, frame cell keys and
 * pause-map entries, all in-memory).
 */
export function newWindowId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  if (c && typeof c.getRandomValues === 'function') {
    // RFC-4122-shaped so the id is indistinguishable in logs from a real UUID.
    return `${randomHex(4)}-${randomHex(2)}-4${randomHex(1).slice(1)}-a${randomHex(1).slice(1)}-${randomHex(6)}`;
  }
  lastIdCounter += 1;
  return `window-${Date.now().toString(36)}-${lastIdCounter.toString(36)}`;
}

let lastIdCounter = 0;
