/**
 * Pure fallback helper for the live player popup: when the opened live entry's
 * session fails (POST error/stall, hls.js fatal error, STALL_DETECTED), the
 * popup advances to the NEXT live entry for the same channel instead of
 * sitting on the spinner.
 *
 * Semantics: strictly forward, one attempt per entry. A failed entry is never
 * revisited by the fallback chain (the manual Retry button is the retry path),
 * so a channel live on N platforms gets at most N-1 automatic fallbacks before
 * the popup surfaces the error as usual. No DOM/hls.js imports — vitest-safe.
 */

export interface LiveFallbackEntry {
  url: string;
  title?: string;
  platform?: string;
  headers?: Record<string, string>;
}

/**
 * The entry to try after `currentIndex`, or null when the chain is exhausted:
 * empty/single-entry lists, an out-of-range index, or the last entry. There is
 * no wrap-back — looping to the start would retry the entry that just failed.
 */
export function nextLiveEntry<T extends LiveFallbackEntry>(
  entries: readonly T[],
  currentIndex: number,
): T | null {
  if (!entries || entries.length < 2) return null;
  const nextIndex = currentIndex + 1;
  if (nextIndex < 1 || nextIndex >= entries.length) return null;
  return entries[nextIndex];
}
