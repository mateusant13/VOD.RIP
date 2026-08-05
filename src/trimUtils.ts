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

/** Visible [start, end] seconds of the preview trim rail for a zoom level. */
export interface TrimViewWindow {
  start: number;
  end: number;
}

export const TRIM_ZOOM_MIN = 1;
export const TRIM_ZOOM_MAX = 64;
/** Per wheel notch — ×1.25 in, ÷1.25 out (log-ish so 14h VODs stay usable). */
export const TRIM_ZOOM_STEP = 1.25;

/** Clamp a zoom level to the supported range. */
export function clampTrimZoom(zoom: number): number {
  if (Number.isNaN(zoom)) return TRIM_ZOOM_MIN;
  return Math.min(TRIM_ZOOM_MAX, Math.max(TRIM_ZOOM_MIN, zoom));
}

/**
 * The rail window (seconds) shown at a zoom level. zoom=1 → the full duration
 * (pixel-identical to the unzoomed rail). Otherwise the window is anchored at
 * anchorFrac (0..1 of the full duration) and clamped to the duration bounds.
 */
export function zoomWindowFromView(
  zoom: number,
  anchorFrac: number,
  durationSec: number,
): TrimViewWindow {
  const dur = Math.max(0, durationSec);
  if (dur <= 0 || zoom <= TRIM_ZOOM_MIN) return { start: 0, end: dur };
  const z = clampTrimZoom(zoom);
  const width = dur / z;
  const anchor = Math.max(0, Math.min(1, anchorFrac)) * dur;
  let start = anchor - width / 2;
  if (start < 0) start = 0;
  if (start + width > dur) start = Math.max(0, dur - width);
  return { start, end: start + width };
}

/** Second value at a rail fraction (0..1) within a zoomed window. */
export function fracToSec(frac: number, view: TrimViewWindow): number {
  return view.start + frac * (view.end - view.start);
}

/** Rail fraction (clamped to 0..1) of a second value within a zoomed window. */
export function secToFrac(sec: number, view: TrimViewWindow): number {
  const width = view.end - view.start;
  if (width <= 0) return 0;
  return Math.max(0, Math.min(1, (sec - view.start) / width));
}

/**
 * Zoom the current window by `factor` around a cursor position: the second
 * under the cursor keeps its rail fraction after the zoom. Returns the new
 * zoom level and the anchor fraction (window centre as 0..1 of the duration),
 * so the rail state stays fully described by (zoom, anchorFrac).
 */
export function zoomTrimViewAround(
  view: TrimViewWindow,
  cursorFrac: number,
  factor: number,
  durationSec: number,
): { zoom: number; anchorFrac: number } {
  const dur = Math.max(0, durationSec);
  if (dur <= 0) return { zoom: TRIM_ZOOM_MIN, anchorFrac: 0.5 };
  const curFrac = Math.max(0, Math.min(1, cursorFrac));
  const cursorSec = fracToSec(curFrac, view);
  const width = view.end - view.start;
  const newWidth = Math.max(1e-9, width / factor);
  let start = cursorSec - curFrac * newWidth;
  let end = start + newWidth;
  if (end > dur) {
    end = dur;
    start = Math.max(0, dur - newWidth);
  }
  if (start < 0) {
    start = 0;
    end = Math.min(dur, newWidth);
  }
  const zoom = clampTrimZoom(dur / (end - start));
  return { zoom, anchorFrac: (start + (end - start) / 2) / dur };
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

