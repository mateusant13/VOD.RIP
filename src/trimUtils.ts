/**
 * Trim/range helper functions extracted from App.tsx.
 */

export interface TrimRangeOpts {
  seek?: 'in' | 'out';
  move?: 'in' | 'out';
  fixedEnd?: number;
  fixedStart?: number;
}

export function clampTrimEndpoints(
  rawStart: number,
  rawEnd: number,
  dur: number,
  currentStart: number,
  currentEnd: number,
  opts?: TrimRangeOpts,
): { start: number; end: number } {
  let start: number;
  let end: number;

  if (opts?.move === 'in') {
    const pinnedEnd = Math.min(dur, Math.max(1, Math.floor(opts.fixedEnd ?? currentEnd)));
    end = pinnedEnd;
    start = Math.max(0, Math.min(Math.floor(rawStart), pinnedEnd - 1));
  } else if (opts?.move === 'out') {
    const pinnedStart = Math.max(0, Math.min(
      Math.floor(opts.fixedStart ?? currentStart),
      dur - 1,
    ));
    start = pinnedStart;
    end = Math.min(dur, Math.max(Math.floor(rawEnd), pinnedStart + 1));
  } else {
    start = Math.floor(rawStart);
    end = Math.floor(rawEnd);
    if (start >= end) {
      if (opts?.seek === 'in') {
        end = Math.min(dur, start + 1);
      } else {
        start = Math.max(0, end - 1);
      }
    }
    start = Math.max(0, Math.min(start, dur - 1));
    end = Math.min(dur, Math.max(end, start + 1));
  }

  return { start, end };
}

/** Start: button − extends clip (earlier), + trims. End: − trims, + extends. */

export function trimButtonDeltaForEndpoint(which: 'in' | 'out', buttonDelta: number): number {
  return which === 'in' ? -buttonDelta : buttonDelta;
}

/** Move the active in/out endpoint by delta seconds (+ extends clip that way). */

export function adjustTrimEndpointByDelta(
  start: number,
  end: number,
  dur: number,
  which: 'in' | 'out',
  delta: number,
): { start: number; end: number } {
  const minLen = 1;
  if (which === 'in') {
    const newStart = Math.max(0, Math.min(end - minLen, start - delta));
    return { start: newStart, end };
  }
  const newEnd = Math.min(dur, Math.max(start + minLen, end + delta));
  return { start, end: newEnd };
}

