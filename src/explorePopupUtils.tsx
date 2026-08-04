import { useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, Dispatch, MutableRefObject, PointerEvent as ReactPointerEvent, SetStateAction } from 'react';

export type PanelPos = { x: number; y: number };

export const VIEWPORT_EDGE_LOCK = 40;
export const EXPLORE_PANEL_DEFAULT_W = 288;
export const EXPLORE_PANEL_MIN_W = 100;
export const EXPLORE_PANEL_MAX_W = 960;
export const EXPLORE_PANEL_CHROME_H_EST = 156;
export const EXPLORE_PANEL_PAD_V = 24;
export const EXPLORE_PANEL_PAD_H = 24;
export const EXPLORE_VIDEO_ASPECT_DEFAULT = 16 / 9;
/** Free-form (non-aspect-locked) panel bounds — search popup window chrome. */
export const EXPLORE_PANEL_BOX_MIN_W = 320;
export const EXPLORE_PANEL_BOX_MIN_H = 280;
const CARD_BORDER_PX = 2;

export type ResizeEdge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

const RESIZE_EDGE_CURSORS: Record<ResizeEdge, string> = {
  n: 'ns-resize',
  s: 'ns-resize',
  e: 'ew-resize',
  w: 'ew-resize',
  ne: 'nesw-resize',
  nw: 'nwse-resize',
  se: 'nwse-resize',
  sw: 'nesw-resize',
};

export function panelResizeHandleInset(compact: boolean): number {
  return CARD_BORDER_PX + (compact ? 4 : 6);
}

function viewportContentBox(shadowPad = panelResizeHandleInset(false)): { maxW: number; maxH: number } {
  return {
    maxW: Math.max(200, window.innerWidth - VIEWPORT_EDGE_LOCK * 2 - shadowPad),
    maxH: Math.max(180, window.innerHeight - VIEWPORT_EDGE_LOCK * 2 - shadowPad),
  };
}

function exploreViewportBox(): { maxW: number; maxH: number } {
  const shadowPad = panelResizeHandleInset(true);
  const box = viewportContentBox(shadowPad);
  return {
    maxW: Math.max(EXPLORE_PANEL_MIN_W, box.maxW),
    maxH: Math.max(140, box.maxH),
  };
}

function maxExplorePanelWidth(chromeH: number, aspect: number): number {
  const { maxW, maxH } = exploreViewportBox();
  const capW = Math.min(EXPLORE_PANEL_MAX_W, maxW);
  const videoMaxW = capW - EXPLORE_PANEL_PAD_H;
  const videoMaxH = Math.max(80, maxH - chromeH - EXPLORE_PANEL_PAD_V);
  const videoMaxWFromH = videoMaxH * aspect;
  return Math.floor(Math.min(videoMaxW, videoMaxWFromH) + EXPLORE_PANEL_PAD_H);
}

export function clampExplorePanelWidth(width: number, chromeH: number, aspect: number): number {
  const maxW = maxExplorePanelWidth(chromeH, aspect);
  return Math.min(maxW, Math.max(EXPLORE_PANEL_MIN_W, width));
}

/** Clamp a free-form panel size to [min, viewport] — width and height independently.
 *  When the viewport is smaller than the minimum, the minimum wins (panel may
 *  overflow a degenerate viewport rather than become unusable). */
export function clampExplorePanelBox(
  size: { w: number; h: number },
  viewport: { w: number; h: number },
  min: { w: number; h: number },
): { w: number; h: number } {
  return {
    w: Math.min(Math.max(min.w, size.w), Math.max(min.w, viewport.w)),
    h: Math.min(Math.max(min.h, size.h), Math.max(min.h, viewport.h)),
  };
}

export function defaultExplorePopupPosition(panelW: number, panelH: number, stackIndex = 0): PanelPos {
  const shadowPad = panelResizeHandleInset(true);
  const stagger = stackIndex * 28;
  return {
    x: window.innerWidth - VIEWPORT_EDGE_LOCK - panelW - shadowPad - stagger,
    y: window.innerHeight - VIEWPORT_EDGE_LOCK - panelH - shadowPad - stagger,
  };
}

export function applyExplorePopupWindowPosition(el: HTMLElement, pos: PanelPos) {
  el.style.position = 'fixed';
  el.style.top = `${pos.y}px`;
  el.style.left = `${pos.x}px`;
  el.style.right = 'auto';
  el.style.bottom = 'auto';
}

export function applyExplorePopupFullscreenPosition(el: HTMLElement) {
  el.style.position = 'fixed';
  el.style.top = '0';
  el.style.left = '0';
  el.style.right = '0';
  el.style.bottom = '0';
}

export function layoutExplorePopupWindow(
  el: HTMLElement,
  width: number,
  posRef: MutableRefObject<PanelPos | null>,
  stackIndex: number,
): PanelPos {
  el.style.width = `${width}px`;
  el.style.height = '';
  if (!posRef.current) {
    posRef.current = defaultExplorePopupPosition(el.offsetWidth, el.offsetHeight, stackIndex);
  }
  applyExplorePopupWindowPosition(el, posRef.current);
  return posRef.current;
}

function edgeAffectsWest(edge: ResizeEdge): boolean {
  return edge === 'w' || edge === 'nw' || edge === 'sw';
}

function edgeAffectsNorth(edge: ResizeEdge): boolean {
  return edge === 'n' || edge === 'ne' || edge === 'nw';
}

/** rAF-coalesced move loop shared by every drag/resize helper: pointermove
 *  only records the latest coordinates and one requestAnimationFrame flush
 *  applies them per display frame. A high-polling mouse (240Hz+) delivers far
 *  more pointermove events than frames; without coalescing each event does a
 *  style write (+ layout read) — the lag the user reported.
 *  `flushSync()` applies the pending coordinates immediately (pointerup path)
 *  so the final value never drops the last event's delta. */
export function makeRafMoveLoop(apply: (lastX: number, lastY: number) => void): {
  onMove: (x: number, y: number) => void;
  flushSync: () => void;
} {
  let rafId = 0;
  let pending = false;
  let lastX = 0;
  let lastY = 0;
  const flush = () => {
    rafId = 0;
    pending = false;
    apply(lastX, lastY);
  };
  return {
    onMove(x: number, y: number) {
      lastX = x;
      lastY = y;
      pending = true;
      if (!rafId) rafId = requestAnimationFrame(flush);
    },
    flushSync() {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
      if (pending) {
        pending = false;
        apply(lastX, lastY);
      }
    },
  };
}

/** Disable CSS transitions on the dragged element for the drag duration —
 *  a `transition: width 0.3s` on a resize target makes it chase the cursor
 *  (every event restarts the ease). Returns the previous value for restore. */
export function suspendPanelTransitions(el: HTMLElement | null): string | null {
  if (!el) return null;
  const prev = el.style.transition;
  el.style.transition = 'none';
  return prev;
}

export function restorePanelTransitions(el: HTMLElement | null, prev: string | null): void {
  if (!el) return;
  el.style.transition = prev ?? '';
}

function widthDeltaFromEdge(edge: ResizeEdge, dx: number, dy: number, aspect: number): number {
  switch (edge) {
    case 'e': return dx;
    case 'w': return -dx;
    case 's': return dy * aspect;
    case 'n': return -dy * aspect;
    case 'se': return Math.max(dx, dy * aspect);
    case 'sw': return Math.max(-dx, dy * aspect);
    case 'ne': return Math.max(dx, -dy * aspect);
    case 'nw': return Math.max(-dx, -dy * aspect);
    default: return dx;
  }
}

export function PanelResizeHandles({
  onPointerDown,
}: {
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => void;
}) {
  // Hosts with overflow hidden/clip can't paint handles outside their padding
  // box — detect that and hug the inner edge instead of straddling the border.
  const hostRef = useRef<HTMLDivElement>(null);
  const [clipsOverflow, setClipsOverflow] = useState(false);
  useLayoutEffect(() => {
    // The containing block (offsetParent) is the panel the handles are
    // positioned against — the DOM parent may be an inner scroll container.
    // overflow: clip hosts can still paint outside their padding box as long
    // as the clip margin covers the strip overhang; anything less clips.
    const host = hostRef.current?.offsetParent as HTMLElement | null;
    if (!host) return;
    const cs = getComputedStyle(host);
    const margin = parseFloat(cs.overflowClipMargin) || 0;
    setClipsOverflow(cs.overflow === 'hidden' || (cs.overflow === 'clip' && margin < CARD_BORDER_PX));
  });

  // Cursor is applied directly per edge (not group-hover): the handle is always
  // inside its own panel, so no .group ancestor is required — fixes main panel.
  const hit = 'absolute z-50 pointer-events-auto select-none touch-none';
  const edgePad = 12;
  // Edge strips straddle the host's border (2px out, 2px border, 2px in) so the
  // resize cursor appears at the visible edge. Clipped hosts can't paint
  // outside: the strip hugs the padding-box edge (border-box 0..6, still fully
  // inside the padding box).
  const edgeOff = clipsOverflow ? 0 : CARD_BORDER_PX + 2;
  const cornerOff = clipsOverflow ? 0 : CARD_BORDER_PX + 2;

  const edgeProps = (edge: ResizeEdge, style: CSSProperties, hoverCursorClass: string, sizeClass = '') => ({
    'data-panel-resize': true as const,
    'aria-hidden': true as const,
    onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => onPointerDown(e, edge),
    style: { ...style, touchAction: 'none' },
    className: `${hit} ${hoverCursorClass} ${sizeClass}`.trim(),
  });

  return (
    <div ref={hostRef} className="absolute inset-0 z-50 pointer-events-none" aria-hidden="true">
      <div {...edgeProps('n', { top: -edgeOff, left: edgePad, right: edgePad, height: 6 }, 'cursor-ns-resize')} />
      <div {...edgeProps('s', { bottom: -edgeOff, left: edgePad, right: edgePad, height: 6 }, 'cursor-ns-resize')} />
      <div {...edgeProps('e', { right: -edgeOff, top: edgePad, bottom: edgePad, width: 6 }, 'cursor-ew-resize')} />
      <div {...edgeProps('w', { left: -edgeOff, top: edgePad, bottom: edgePad, width: 6 }, 'cursor-ew-resize')} />
      <div {...edgeProps('nw', { top: -cornerOff, left: -cornerOff }, 'cursor-nwse-resize', 'w-4 h-4')} />
      <div {...edgeProps('ne', { top: -cornerOff, right: -cornerOff }, 'cursor-nesw-resize', 'w-4 h-4')} />
      <div {...edgeProps('sw', { bottom: -cornerOff, left: -cornerOff }, 'cursor-nesw-resize', 'w-4 h-4')} />
      <div {...edgeProps('se', { bottom: -cornerOff, right: -cornerOff }, 'cursor-nwse-resize', 'w-4 h-4')} />
    </div>
  );
}

export function startExplorePanelWidthResize(
  e: ReactPointerEvent<HTMLDivElement>,
  edge: ResizeEdge,
  widthRef: MutableRefObject<number>,
  setWidth: Dispatch<SetStateAction<number>>,
  opts: {
    panelEl: HTMLElement | null;
    clampWidth: (w: number) => number;
    aspect: number;
    posRef?: MutableRefObject<PanelPos | null>;
    setPos?: Dispatch<SetStateAction<PanelPos | null>>;
    onResizeMove?: (w: number) => void;
  },
) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const startW = widthRef.current;
  const startPos = opts.posRef?.current ? { ...opts.posRef.current } : null;
  const panelEl = opts.panelEl;
  const clamp = opts.clampWidth;
  // Geometry reads hoisted to drag start (before any writes): reading
  // offsetHeight between width writes forces a synchronous layout per event
  // (read-after-write thrash). Viewport dims are cached too — they cannot
  // change during a pointer drag.
  const startPanelH = panelEl ? panelEl.offsetHeight || 1 : 1;
  const viewport = { w: window.innerWidth, h: window.innerHeight };
  const prevTransition = suspendPanelTransitions(panelEl);

  if (panelEl) {
    panelEl.style.willChange = 'width';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = RESIZE_EDGE_CURSORS[edge];

  const applyWidthAndPos = (rawNextW: number) => {
    let nextW = clamp(rawNextW);
    if (panelEl) {
      panelEl.style.width = `${nextW}px`;
      panelEl.style.height = '';
    }
    if (startPos && opts.posRef && panelEl) {
      const inset = panelResizeHandleInset(true);
      const margin = VIEWPORT_EDGE_LOCK + inset;
      let x = startPos.x;
      let y = startPos.y;
      if (edgeAffectsWest(edge)) {
        x = startPos.x + startW - nextW;
      }
      if (edgeAffectsNorth(edge)) {
        y = startPos.y - (nextW - startW) / opts.aspect;
      }

      const minX = margin;
      const maxX = viewport.w - margin - nextW;
      if (edgeAffectsWest(edge)) {
        if (x < minX) {
          x = minX;
          nextW = clamp(startPos.x + startW - x);
          x = startPos.x + startW - nextW;
        }
      } else {
        x = Math.max(minX, Math.min(x, maxX));
        const rightBound = viewport.w - margin;
        if (x + nextW > rightBound) {
          nextW = clamp(rightBound - x);
        }
      }

      const panelH = startPanelH;
      const minY = margin;
      const maxY = viewport.h - margin - panelH;
      if (edgeAffectsNorth(edge) && y < minY) {
        y = minY;
      } else {
        y = Math.max(minY, Math.min(y, maxY));
      }

      widthRef.current = nextW;
      if (panelEl) {
        panelEl.style.width = `${nextW}px`;
      }
      const pos = { x, y };
      opts.posRef.current = pos;
      applyExplorePopupWindowPosition(panelEl, pos);
    } else {
      widthRef.current = nextW;
    }
    opts.onResizeMove?.(nextW);
  };

  const loop = makeRafMoveLoop((x, y) => {
    const delta = widthDeltaFromEdge(edge, x - startX, y - startY, opts.aspect);
    applyWidthAndPos(clamp(startW + delta));
  });

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    loop.onMove(ev.clientX, ev.clientY);
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    loop.flushSync();
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
    restorePanelTransitions(panelEl, prevTransition);
    if (panelEl) {
      panelEl.style.willChange = '';
    }
    const finalW = clamp(widthRef.current);
    applyWidthAndPos(finalW);
    setWidth(finalW);
    if (opts.setPos && opts.posRef?.current) {
      opts.setPos({ ...opts.posRef.current });
    }
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}

/** Free-form panel resize: width AND height move independently (no aspect
 *  lock). West/north edges shift the panel so the opposite edge stays put.
 *  Size clamps to [min, viewport-margins]; west/north over-drags shrink the
 *  panel back inside the viewport instead of pushing it off-screen. */
export function startExplorePanelBoxResize(
  e: ReactPointerEvent<HTMLDivElement>,
  edge: ResizeEdge,
  sizeRef: MutableRefObject<{ w: number; h: number }>,
  setSize: (next: { w: number; h: number }) => void,
  opts: {
    panelEl: HTMLElement | null;
    min?: { w: number; h: number };
    posRef?: MutableRefObject<PanelPos | null>;
    setPos?: Dispatch<SetStateAction<PanelPos | null>>;
  },
) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const start = { ...sizeRef.current };
  const min = opts.min ?? { w: EXPLORE_PANEL_BOX_MIN_W, h: EXPLORE_PANEL_BOX_MIN_H };
  const startPos = opts.posRef?.current ? { ...opts.posRef.current } : null;
  const panelEl = opts.panelEl;
  const prevTransition = suspendPanelTransitions(panelEl);

  if (panelEl) {
    panelEl.style.willChange = 'width, height';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = RESIZE_EDGE_CURSORS[edge];

  // Viewport + margins cached at drag start — they cannot change mid-drag.
  const margin = VIEWPORT_EDGE_LOCK + panelResizeHandleInset(true);
  const viewportBox = (): { w: number; h: number } => ({
    w: Math.max(min.w, window.innerWidth - margin * 2),
    h: Math.max(min.h, window.innerHeight - margin * 2),
  });
  const viewport = viewportBox();

  const applySizeAndPos = (rawNext: { w: number; h: number }) => {
    let next = clampExplorePanelBox(rawNext, viewport, min);
    if (panelEl) {
      panelEl.style.width = `${next.w}px`;
      panelEl.style.height = `${next.h}px`;
    }
    if (startPos && opts.posRef && panelEl) {
      let x = startPos.x;
      let y = startPos.y;
      if (edgeAffectsWest(edge)) x = startPos.x + start.w - next.w;
      if (edgeAffectsNorth(edge)) y = startPos.y + start.h - next.h;

      const minX = margin;
      const minY = margin;
      if (edgeAffectsWest(edge)) {
        if (x < minX) {
          x = minX;
          next = clampExplorePanelBox(
            { w: startPos.x + start.w - x, h: next.h },
            viewport,
            min,
          );
          x = startPos.x + start.w - next.w;
        }
      } else {
        x = Math.max(minX, Math.min(x, viewport.w + margin - next.w));
        if (x + next.w > viewport.w + margin) {
          next = clampExplorePanelBox(
            { w: viewport.w + margin - x, h: next.h },
            viewport,
            min,
          );
        }
      }
      if (edgeAffectsNorth(edge)) {
        if (y < minY) {
          y = minY;
          next = clampExplorePanelBox(
            { w: next.w, h: startPos.y + start.h - y },
            viewport,
            min,
          );
          y = startPos.y + start.h - next.h;
        }
      } else {
        y = Math.max(minY, Math.min(y, viewport.h + margin - next.h));
        if (y + next.h > viewport.h + margin) {
          next = clampExplorePanelBox(
            { w: next.w, h: viewport.h + margin - y },
            viewport,
            min,
          );
        }
      }

      sizeRef.current = { ...next };
      if (panelEl) {
        panelEl.style.width = `${next.w}px`;
        panelEl.style.height = `${next.h}px`;
      }
      const pos = { x, y };
      opts.posRef.current = pos;
      applyExplorePopupWindowPosition(panelEl, pos);
    } else {
      sizeRef.current = { ...next };
    }
  };

  const loop = makeRafMoveLoop((x, y) => {
    const dx = x - startX;
    const dy = y - startY;
    const dw = edge === 'e' || edge === 'ne' || edge === 'se' ? dx
      : edge === 'w' || edge === 'nw' || edge === 'sw' ? -dx : 0;
    const dh = edge === 's' || edge === 'se' || edge === 'sw' ? dy
      : edge === 'n' || edge === 'ne' || edge === 'nw' ? -dy : 0;
    applySizeAndPos({ w: start.w + dw, h: start.h + dh });
  });

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    loop.onMove(ev.clientX, ev.clientY);
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    loop.flushSync();
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
    restorePanelTransitions(panelEl, prevTransition);
    if (panelEl) {
      panelEl.style.willChange = '';
    }
    const finalSize = clampExplorePanelBox(sizeRef.current, viewport, min);
    applySizeAndPos(finalSize);
    setSize({ ...finalSize });
    if (opts.setPos && opts.posRef?.current) {
      opts.setPos({ ...opts.posRef.current });
    }
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}

export function startFloatingPanelDrag(
  e: ReactPointerEvent<HTMLElement>,
  posRef: MutableRefObject<PanelPos | null>,
  setPos: Dispatch<SetStateAction<PanelPos | null>>,
  panelEl: HTMLElement | null,
) {
  if ((e.target as HTMLElement).closest('button, input, select, textarea, a, [role="slider"]')) return;
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const startPos = { ...(posRef.current ?? { x: 0, y: 0 }) };
  const prevTransition = suspendPanelTransitions(panelEl);

  if (panelEl) {
    panelEl.style.willChange = 'top, left';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = 'grabbing';

  // Size + viewport read once at drag start — the panel does not resize while
  // being dragged, so per-move offsetWidth/offsetHeight reads would only force
  // extra layout passes.
  const startW = panelEl?.offsetWidth ?? 0;
  const startH = panelEl?.offsetHeight ?? 0;
  const inset = panelResizeHandleInset(true);
  const margin = VIEWPORT_EDGE_LOCK + inset;

  const clampFloatingPos = (next: PanelPos): PanelPos => {
    const minX = margin;
    const minY = margin;
    const maxX = window.innerWidth - margin - startW;
    const maxY = window.innerHeight - margin - startH;
    return {
      x: Math.max(minX, Math.min(next.x, maxX)),
      y: Math.max(minY, Math.min(next.y, maxY)),
    };
  };

  const loop = makeRafMoveLoop((x, y) => {
    const next = clampFloatingPos({
      x: startPos.x + x - startX,
      y: startPos.y + y - startY,
    });
    posRef.current = next;
    if (panelEl) {
      applyExplorePopupWindowPosition(panelEl, next);
    }
  });

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    loop.onMove(ev.clientX, ev.clientY);
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    loop.flushSync();
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
    restorePanelTransitions(panelEl, prevTransition);
    if (panelEl) {
      panelEl.style.willChange = '';
    }
    setPos(posRef.current ? { ...posRef.current } : null);
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}
