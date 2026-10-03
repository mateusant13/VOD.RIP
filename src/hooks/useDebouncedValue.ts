import { useEffect, useState } from 'react';

/**
 * Trailing-debounced mirror of `value`: the returned value only catches up
 * `delayMs` after the input stops changing. Keeps a keystroke off the
 * expensive path (the preview panel re-filters up to 20k rows; the archive
 * popup runs a request) while the input itself stays controlled by the
 * caller, so typing never feels laggy.
 *
 * The first value passes through immediately — an already-settled value
 * should not wait out a timer.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    if (value === debounced) return;
    const id = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(id);
  }, [value, delayMs, debounced]);
  return debounced;
}

export default useDebouncedValue;
