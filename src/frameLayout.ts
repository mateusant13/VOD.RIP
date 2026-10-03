/** Frame grid layout — shared by FrameOverlay and explore popup snap. */

export const FRAME_GRID_CELLS = 6;
export const FRAME_GRID_PADDING = 8;
export const FRAME_GRID_GAP = 8;
export const FRAME_DRAG_PREFIX = 'vodrip-frame:';

/**
 * How far outside a cell a release still snaps to that cell.
 *
 * The 8px grid gap and the 8px outer padding are DEAD ZONES under a strict
 * rect hit-test: releasing there used to hit no cell and silently do nothing
 * ("I dropped it and nothing happened"). A nearest-cell hit-test with a
 * threshold closes them.
 *
 * 32px is chosen from the geometry itself, not by feel:
 *  - 4x the 8px gap, so every point inside a gap is comfortably claimed;
 *  - ~the 32px resize gutter the floating panels already reserve, so a
 *    release just outside the grid edge still snaps;
 *  - << the smallest cell (>=120px tall), so it can never override an
 *    intentional drop well inside a neighbouring cell.
 */
export const FRAME_SNAP_THRESHOLD_PX = 32;

/** Attribute every frame-capable floating window carries on its root node. */
export const FRAME_WINDOW_ATTR = 'data-frame-window';

export type FrameRect = { x: number; y: number; w: number; h: number };

/** Window families that can occupy a frame cell. */
export type FrameWindowKind = 'explore' | 'live';
/** Identity of a frame window: which family + its per-family stable id. */
export type FrameWindowRef = { kind: FrameWindowKind; id: string };

export function frameWindowKey(kind: FrameWindowKind, id: string | number): string {
  return `${kind}:${id}`;
}

/** Inverse of {@link frameWindowKey}; null when the key is not a window key. */
export function parseFrameWindowKey(key: string): FrameWindowRef | null {
  const sep = key.indexOf(':');
  if (sep <= 0) return null;
  const kind = key.slice(0, sep);
  if (kind !== 'explore' && kind !== 'live') return null;
  return { kind, id: key.slice(sep + 1) };
}

export function frameGridColumns(viewportWidth = window.innerWidth): number {
  return viewportWidth > 1100 ? 3 : 2;
}

export function getFrameCellRect(
  index: number,
  viewportW = window.innerWidth,
  viewportH = window.innerHeight,
): FrameRect {
  const cols = frameGridColumns(viewportW);
  const rows = FRAME_GRID_CELLS / cols;
  const pad = FRAME_GRID_PADDING;
  const gap = FRAME_GRID_GAP;
  const innerW = Math.max(0, viewportW - pad * 2);
  const innerH = Math.max(0, viewportH - pad * 2);
  const cellW = (innerW - gap * (cols - 1)) / cols;
  const cellH = (innerH - gap * (rows - 1)) / rows;
  const col = index % cols;
  const row = Math.floor(index / cols);
  return {
    x: pad + col * (cellW + gap),
    y: pad + row * (cellH + gap),
    w: cellW,
    h: cellH,
  };
}

export function encodeFrameDragPopupId(popupId: string): string {
  return `${FRAME_DRAG_PREFIX}${popupId}`;
}

/** Squared distance from (x, y) to the nearest edge/inside of `r` (0 if inside). */
function distanceToRect(r: FrameRect, x: number, y: number): number {
  const dx = x < r.x ? r.x - x : x > r.x + r.w ? x - (r.x + r.w) : 0;
  const dy = y < r.y ? r.y - y : y > r.y + r.h ? y - (r.y + r.h) : 0;
  if (dx === 0 && dy === 0) return 0;
  return Math.hypot(dx, dy);
}

/**
 * Which frame cell a pointer at (x, y) should snap to, or null.
 *
 * Exact hit first, then the NEAREST cell within `threshold` — this is what
 * makes a release in a gap/padding still snap instead of silently doing
 * nothing. Same geometry source as the rendered cells, so the hit-test can
 * never disagree with what the user sees.
 */
export function frameCellIndexAt(
  x: number,
  y: number,
  viewportW = window.innerWidth,
  viewportH = window.innerHeight,
  threshold = FRAME_SNAP_THRESHOLD_PX,
): number | null {
  let best = -1;
  let bestDist = Infinity;
  for (let i = 0; i < FRAME_GRID_CELLS; i++) {
    const d = distanceToRect(getFrameCellRect(i, viewportW, viewportH), x, y);
    if (d === 0) return i;
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return bestDist <= threshold ? best : null;
}

export function decodeFrameDragPayload(
  raw: string,
): { kind: 'popup'; id: string } | { kind: 'url'; url: string } {
  if (raw.startsWith(FRAME_DRAG_PREFIX)) {
    return { kind: 'popup', id: raw.slice(FRAME_DRAG_PREFIX.length) };
  }
  return { kind: 'url', url: raw };
}
