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
  insetPx,
}: {
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => void;
  insetPx: number;
}) {
  const hit = 'absolute z-50 pointer-events-auto select-none touch-none';
  const edgePad = 12;

  const edgeProps = (edge: ResizeEdge, style: CSSProperties, hoverCursorClass: string, sizeClass = '') => ({
    'data-panel-resize': true as const,
    'aria-hidden': true as const,
    onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => onPointerDown(e, edge),
    style: { ...style, touchAction: 'none' },
    className: `${hit} cursor-default ${hoverCursorClass} ${sizeClass}`.trim(),
  });

  return (
    <>
      <div {...edgeProps('n', { top: -insetPx - 3, left: edgePad, right: edgePad, height: 6 }, 'group-hover:cursor-ns-resize')} />
      <div {...edgeProps('s', { bottom: -insetPx - 3, left: edgePad, right: edgePad, height: 6 }, 'group-hover:cursor-ns-resize')} />
      <div {...edgeProps('e', { right: -insetPx - 3, top: edgePad, bottom: edgePad, width: 6 }, 'group-hover:cursor-ew-resize')} />
      <div {...edgeProps('w', { left: -insetPx - 3, top: edgePad, bottom: edgePad, width: 6 }, 'group-hover:cursor-ew-resize')} />
      <div {...edgeProps('nw', { top: -insetPx, left: -insetPx }, 'group-hover:cursor-nwse-resize', 'w-4 h-4')} />
      <div {...edgeProps('ne', { top: -insetPx, right: -insetPx }, 'group-hover:cursor-nesw-resize', 'w-4 h-4')} />
      <div {...edgeProps('sw', { bottom: -insetPx, left: -insetPx }, 'group-hover:cursor-nesw-resize', 'w-4 h-4')} />
      <div {...edgeProps('se', { bottom: -insetPx, right: -insetPx }, 'group-hover:cursor-nwse-resize', 'w-4 h-4')} />
    </>
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

  if (panelEl) {
    panelEl.style.willChange = 'width';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = RESIZE_EDGE_CURSORS[edge];

  const applyWidthAndPos = (nextW: number) => {
    widthRef.current = nextW;
    if (panelEl) {
      panelEl.style.width = `${nextW}px`;
      panelEl.style.height = '';
    }
    if (startPos && opts.posRef && panelEl) {
      let x = startPos.x;
      let y = startPos.y;
      if (edgeAffectsWest(edge)) {
        x = startPos.x + startW - nextW;
      }
      if (edgeAffectsNorth(edge)) {
        y = startPos.y - (nextW - startW) / opts.aspect;
      }
      const pos = { x, y };
      opts.posRef.current = pos;
      applyExplorePopupWindowPosition(panelEl, pos);
    }
  };

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    const delta = widthDeltaFromEdge(edge, ev.clientX - startX, ev.clientY - startY, opts.aspect);
    applyWidthAndPos(clamp(startW + delta));
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
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

export function startFloatingPanelDrag(
  e: ReactPointerEvent<HTMLElement>,
  posRef: MutableRefObject<PanelPos>,
  setPos: Dispatch<SetStateAction<PanelPos>>,
  panelEl: HTMLElement | null,
) {
  if ((e.target as HTMLElement).closest('button, input, select, textarea, a, [role="slider"]')) return;
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const startPos = { ...posRef.current };

  if (panelEl) {
    panelEl.style.willChange = 'top, left';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = 'grabbing';

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    const next = {
      x: startPos.x + ev.clientX - startX,
      y: startPos.y + ev.clientY - startY,
    };
    posRef.current = next;
    if (panelEl) {
      applyExplorePopupWindowPosition(panelEl, next);
    }
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
    if (panelEl) {
      panelEl.style.willChange = '';
    }
    setPos({ ...posRef.current });
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}
