/** Frame grid layout — shared by FrameOverlay and explore popup snap. */

export const FRAME_GRID_CELLS = 6;
export const FRAME_GRID_PADDING = 8;
export const FRAME_GRID_GAP = 8;
export const FRAME_DRAG_PREFIX = 'vodrip-frame:';

export type FrameRect = { x: number; y: number; w: number; h: number };

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

export function decodeFrameDragPayload(
  raw: string,
): { kind: 'popup'; id: string } | { kind: 'url'; url: string } {
  if (raw.startsWith(FRAME_DRAG_PREFIX)) {
    return { kind: 'popup', id: raw.slice(FRAME_DRAG_PREFIX.length) };
  }
  return { kind: 'url', url: raw };
}
