/**
 * Layout/panel utility functions extracted from App.tsx.
 */
import { panelMaxWidthCap, readUiScale } from './uiScale';
import { panelResizeHandleInset, type ResizeEdge } from './explorePopupUtils';
import type { PanelSize, PanelPos, LayoutPanelKey, LayoutPanelBoundsInput, PersistedPanelLayout } from './types';
import type { MutableRefObject, Dispatch, SetStateAction, PointerEvent as ReactPointerEvent } from 'react';

export function panelMaxW(): number {
  return panelMaxWidthCap();
}
/** Minimum clear space between panel chrome (incl. shadow) and viewport edges. */
export function panelMaxHeight() {
  return Math.round(window.innerHeight * 0.92);
}
export function viewportContentBox(shadowPad = panelResizeHandleInset(false)): { maxW: number; maxH: number } {
  const { usableWidth } = layoutRowEdgeInsets(shadowPad);
  return {
    maxW: Math.max(PANEL_MIN.w, usableWidth),
    maxH: Math.max(PANEL_MIN.h, window.innerHeight - VIEWPORT_EDGE_LOCK * 2 - shadowPad),
  };
}
export function layoutRowEdgeInsets(shadowPad = panelResizeHandleInset(false)): {
  left: number;
  right: number;
  usableWidth: number;
} {
  const left = VIEWPORT_EDGE_LOCK + shadowPad;
  const right = VIEWPORT_EDGE_LOCK + shadowPad;
  const usableWidth = Math.max(PANEL_MIN.w, window.innerWidth - left - right);
  return { left, right, usableWidth };
}
export function layoutRowGap(previewOpen: boolean, urlPanelAside: boolean): number {
  const count = (previewOpen ? 1 : 0) + (urlPanelAside ? 1 : 0) + 1;
  if (count <= 1) return 0;
  return previewOpen && urlPanelAside ? LAYOUT_ROW_GAP_TRIPLE : LAYOUT_ROW_GAP_SPLIT;
}
export function layoutMaxPanelWidth(target: LayoutPanelKey, layout: LayoutPanelBoundsInput): number {
  const { maxW } = viewportContentBox();
  const count = (layout.previewOpen ? 1 : 0) + (layout.urlPanelAside ? 1 : 0) + 1;
  const gapTotal = Math.max(0, count - 1) * layoutRowGap(layout.previewOpen, layout.urlPanelAside);

  let othersW = 0;
  if (layout.previewOpen && target !== 'preview') othersW += layout.preview.w;
  if (layout.urlPanelAside && target !== 'urlAside') othersW += layout.urlAside.w;
  if (target !== 'main') othersW += layout.main.w;

  return Math.max(PANEL_MIN.w, Math.min(panelMaxW(), maxW - othersW - gapTotal));
}
export function layoutMaxPanelHeight(): number {
  return Math.min(panelMaxHeight(), viewportContentBox().maxH);
}
export function clampPanelSizeForLayout(
  target: LayoutPanelKey,
  size: PanelSize,
  layout: LayoutPanelBoundsInput,
): PanelSize {
  const maxW = layoutMaxPanelWidth(target, layout);
  const maxH = layoutMaxPanelHeight();
  return {
    w: Math.min(maxW, Math.max(PANEL_MIN.w, size.w)),
    h: Math.min(maxH, Math.max(PANEL_MIN.h, size.h)),
  };
}
function layoutRowWidthBudget(layout: LayoutPanelBoundsInput): number {
  const { usableWidth } = layoutRowEdgeInsets();
  const count = (layout.previewOpen ? 1 : 0) + (layout.urlPanelAside ? 1 : 0) + 1;
  const gapTotal = Math.max(0, count - 1) * layoutRowGap(layout.previewOpen, layout.urlPanelAside);
  return usableWidth - gapTotal;
}

/** Shrink siblings when preview grows so the row stays within the viewport budget. */
export function resizeLayoutWithPreviewWidth(
  layout: LayoutPanelBoundsInput,
  desiredPreviewW: number,
): { preview: PanelSize; urlAside: PanelSize; main: PanelSize } {
  let preview = { ...layout.preview, w: desiredPreviewW };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };

  type Slot = { get: () => number; set: (w: number) => void; minW: number };
  const siblingSlots: Slot[] = [];
  if (layout.urlPanelAside) {
    siblingSlots.push({
      get: () => urlAside.w,
      set: (w) => { urlAside = { ...urlAside, w }; },
      minW: PANEL_MIN.w,
    });
  }
  siblingSlots.push({
    get: () => main.w,
    set: (w) => { main = { ...main, w }; },
    minW: PANEL_MIN.w,
  });

  const budget = layoutRowWidthBudget(layout);
  const minPreviewW = PREVIEW_PANEL_MIN_W;
  const minSiblingTotal = siblingSlots.reduce((sum, slot) => sum + slot.minW, 0);
  const maxPreview = Math.max(minPreviewW, budget - minSiblingTotal);
  preview = { ...preview, w: Math.min(preview.w, maxPreview) };

  let total = preview.w + siblingSlots.reduce((sum, slot) => sum + slot.get(), 0);
  if (total <= budget) {
    return { preview, urlAside, main };
  }

  let overflow = total - budget;
  const flexTotal = siblingSlots.reduce((sum, slot) => sum + (slot.get() - slot.minW), 0);
  if (flexTotal > 0) {
    for (const slot of siblingSlots) {
      const excess = slot.get() - slot.minW;
      const shave = Math.min(excess, Math.ceil(overflow * (excess / flexTotal)));
      slot.set(slot.get() - shave);
    }
  }

  return shrinkLayoutPanelsToFit({ ...layout, preview, urlAside, main });
}

/** Shrink visible panel widths proportionally when the row exceeds the viewport. */
export function shrinkLayoutPanelsToFit(layout: LayoutPanelBoundsInput): {
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
} {
  let preview = { ...layout.preview };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };

  type Slot = {
    get: () => number;
    set: (w: number) => void;
    minW: number;
  };
  const slots: Slot[] = [];
  if (layout.previewOpen) {
    slots.push({
      get: () => preview.w,
      set: (w) => { preview = { ...preview, w }; },
      minW: PREVIEW_PANEL_MIN_W,
    });
  }
  if (layout.urlPanelAside) {
    slots.push({
      get: () => urlAside.w,
      set: (w) => { urlAside = { ...urlAside, w }; },
      minW: PANEL_MIN.w,
    });
  }
  slots.push({
    get: () => main.w,
    set: (w) => { main = { ...main, w }; },
    minW: PANEL_MIN.w,
  });

  const available = layoutRowWidthBudget(layout);
  let total = slots.reduce((sum, slot) => sum + slot.get(), 0);
  if (total <= available) return { preview, urlAside, main };

  const scale = available / total;
  for (const slot of slots) {
    slot.set(Math.max(slot.minW, Math.floor(slot.get() * scale)));
  }
  total = slots.reduce((sum, slot) => sum + slot.get(), 0);

  let guard = 0;
  while (total > available && guard++ < 64) {
    const overflow = total - available;
    const flexible = slots.filter((slot) => slot.get() > slot.minW);
    if (flexible.length === 0) break;
    const flexTotal = flexible.reduce((sum, slot) => sum + (slot.get() - slot.minW), 0);
    if (flexTotal <= 0) break;
    for (const slot of flexible) {
      const excess = slot.get() - slot.minW;
      const shave = Math.min(excess, Math.ceil(overflow * (excess / flexTotal)));
      slot.set(slot.get() - shave);
    }
    total = slots.reduce((sum, slot) => sum + slot.get(), 0);
  }

  const result = { preview, urlAside, main };
  if (typeof window !== 'undefined') {
    let rowTotal = 0;
    if (layout.previewOpen) rowTotal += result.preview.w;
    if (layout.urlPanelAside) rowTotal += result.urlAside.w;
    rowTotal += result.main.w;
    const budget = layoutRowWidthBudget(layout);
    console.assert(rowTotal <= budget, 'shrinkLayoutPanelsToFit overflow');
  }
  return result;
}

export function clampAllLayoutPanels(layout: LayoutPanelBoundsInput): {
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
} {
  const maxH = layoutMaxPanelHeight();
  let preview = { ...layout.preview };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };
  const snapshot = (): LayoutPanelBoundsInput => ({
    ...layout,
    preview,
    urlAside,
    main,
  });

  if (layout.previewOpen) {
    const w = clampPreviewPanelWidth(
      preview.w,
      PREVIEW_PANEL_CHROME_H_EST,
      PREVIEW_VIDEO_ASPECT_DEFAULT,
      snapshot(),
    );
    preview = { w, h: preview.h };
  }
  if (layout.urlPanelAside) {
    urlAside = clampPanelSizeForLayout('urlAside', { ...urlAside, h: Math.min(urlAside.h, maxH) }, snapshot());
  }
  main = clampPanelSizeForLayout('main', { ...main, h: Math.min(main.h, maxH) }, snapshot());

  return shrinkLayoutPanelsToFit({ ...layout, preview, urlAside, main });
}
export function maxPreviewPanelWidth(
  chromeH: number,
  aspect: number,
  layout: LayoutPanelBoundsInput,
): number {
  const shadowPad = panelResizeHandleInset(true);
  const { maxH } = viewportContentBox(shadowPad);
  const capW = Math.min(panelMaxW(), layoutMaxPanelWidth('preview', layout));
  const videoMaxW = capW - PREVIEW_PANEL_PAD_H;
  const videoMaxH = Math.max(100, maxH - chromeH - PREVIEW_PANEL_PAD_H);
  const videoMaxWFromH = videoMaxH * aspect;
  return Math.floor(Math.min(videoMaxW, videoMaxWFromH) + PREVIEW_PANEL_PAD_H);
}
export function clampPreviewPanelWidth(
  width: number,
  chromeH: number,
  aspect: number,
  layout: LayoutPanelBoundsInput,
): number {
  const minW = Math.min(PREVIEW_PANEL_MIN_W, maxPreviewPanelWidth(chromeH, aspect, layout));
  const maxW = maxPreviewPanelWidth(chromeH, aspect, layout);
  return Math.min(maxW, Math.max(minW, width));
}
export function applyExplorePopupWindowPosition(el: HTMLElement, pos: PanelPos) {
  el.style.position = 'fixed';
  el.style.top = `${pos.y}px`;
  el.style.left = `${pos.x}px`;
  el.style.right = 'auto';
  el.style.bottom = 'auto';
  el.style.zIndex = String(EXPLORE_POPUP_Z);
}
export function edgeAffectsWest(edge: ResizeEdge): boolean {
  return edge === 'w' || edge === 'nw' || edge === 'sw';
}
export function edgeAffectsNorth(edge: ResizeEdge): boolean {
  return edge === 'n' || edge === 'ne' || edge === 'nw';
}
export function calcPanelSizeFromEdge(
  edge: ResizeEdge,
  startW: number,
  startH: number,
  dx: number,
  dy: number,
): PanelSize {
  let w = startW;
  let h = startH;
  if (edge === 'e' || edge === 'ne' || edge === 'se') w = startW + dx;
  else if (edge === 'w' || edge === 'nw' || edge === 'sw') w = startW - dx;
  if (edge === 's' || edge === 'se' || edge === 'sw') h = startH + dy;
  else if (edge === 'n' || edge === 'ne' || edge === 'nw') h = startH - dy;
  return { w, h };
}
export function widthDeltaFromEdge(edge: ResizeEdge, dx: number, dy: number, aspect: number): number {
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
export function applyPanelSize(el: HTMLElement, size: PanelSize) {
  el.style.width = `${size.w}px`;
  el.style.height = `${size.h}px`;
}
export function startPanelResizeDrag(
  e: ReactPointerEvent<HTMLDivElement>,
  edge: ResizeEdge,
  sizeRef: MutableRefObject<PanelSize>,
  setSize: Dispatch<SetStateAction<PanelSize>>,
  opts?: {
    maxW?: number;
    maxH?: number;
    panelEl?: HTMLElement | null;
    clampSize?: (size: PanelSize) => PanelSize;
    onResizeEnd?: () => void;
  },
) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const { w: startW, h: startH } = sizeRef.current;
  const maxW = opts?.maxW ?? panelMaxW();
  const maxH = opts?.maxH ?? panelMaxHeight();
  const panelEl = opts?.panelEl ?? null;

  if (panelEl) {
    panelEl.style.willChange = 'width, height';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = RESIZE_EDGE_CURSORS[edge];

  const calcSize = (clientX: number, clientY: number): PanelSize => {
    const raw = calcPanelSizeFromEdge(edge, startW, startH, clientX - startX, clientY - startY);
    return {
      w: Math.min(maxW, Math.max(PANEL_MIN.w, raw.w)),
      h: Math.min(maxH, Math.max(PANEL_MIN.h, raw.h)),
    };
  };

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    let next = calcSize(ev.clientX, ev.clientY);
    if (opts?.clampSize) next = opts.clampSize(next);
    sizeRef.current = next;
    if (panelEl) {
      applyPanelSize(panelEl, next);
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
    const final = opts?.clampSize ? opts.clampSize(sizeRef.current) : sizeRef.current;
    sizeRef.current = final;
    if (panelEl) {
      applyPanelSize(panelEl, final);
    }
    setSize({ ...final });
    opts?.onResizeEnd?.();
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}
export function applyPanelWidth(el: HTMLElement, width: number) {
  el.style.width = `${width}px`;
  el.style.height = '';
}
export function startPanelWidthResize(
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
    onResizeEnd?: () => void;
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
      applyPanelWidth(panelEl, nextW);
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
      const maxX = window.innerWidth - margin - nextW;
      if (edgeAffectsWest(edge)) {
        if (x < minX) {
          x = minX;
          nextW = clamp(startPos.x + startW - x);
          x = startPos.x + startW - nextW;
        }
      } else {
        x = Math.max(minX, Math.min(x, maxX));
        const rightBound = window.innerWidth - margin;
        if (x + nextW > rightBound) {
          nextW = clamp(rightBound - x);
        }
      }

      const panelH = panelEl.offsetHeight || 1;
      const minY = margin;
      const maxY = window.innerHeight - margin - panelH;
      if (edgeAffectsNorth(edge) && y < minY) {
        y = minY;
      } else {
        y = Math.max(minY, Math.min(y, maxY));
      }

      widthRef.current = nextW;
      if (panelEl) {
        applyPanelWidth(panelEl, nextW);
      }
      const pos = { x, y };
      opts.posRef.current = pos;
      applyExplorePopupWindowPosition(panelEl, pos);
    } else {
      widthRef.current = nextW;
    }
    opts.onResizeMove?.(nextW);
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
    opts.onResizeEnd?.();
    if (opts.setPos && opts.posRef?.current) {
      opts.setPos({ ...opts.posRef.current });
    }
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}
export function defaultPanelLayout(): PersistedPanelLayout {
  const scale = readUiScale();
  return {
    previewPanelWidth: Math.round(PREVIEW_PANEL_DEFAULT_W * scale),
    urlAside: {
      w: Math.round(URL_ASIDE_PANEL_DEFAULT.w * scale),
      h: Math.round(URL_ASIDE_PANEL_DEFAULT.h * scale),
    },
    main: {
      w: Math.round(MAIN_PANEL_DEFAULT.w * scale),
      h: Math.round(MAIN_PANEL_DEFAULT.h * scale),
    },
  };
}
export function loadPanelLayout(): PersistedPanelLayout {
  const fallback = defaultPanelLayout();
  try {
    const raw = localStorage.getItem(PANEL_LAYOUT_STORAGE_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<PersistedPanelLayout>;
    return {
      previewPanelWidth: clampLayoutNumber(
        parsed.previewPanelWidth,
        PREVIEW_PANEL_MIN_W,
        panelMaxW(),
        fallback.previewPanelWidth,
      ),
      urlAside: clampStoredPanelSize(parsed.urlAside, URL_ASIDE_PANEL_DEFAULT),
      main: clampStoredPanelSize(parsed.main, MAIN_PANEL_DEFAULT),
    };
  } catch {
    return fallback;
  }
}
export function persistPanelLayout(layout: PersistedPanelLayout) {
  try {
    localStorage.setItem(PANEL_LAYOUT_STORAGE_KEY, JSON.stringify(layout));
  } catch {
    /* quota / private mode */
  }
}
export function clampLayoutNumber(value: unknown, min: number, max: number, fallback: number): number {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}
export function clampStoredPanelSize(value: unknown, fallback: PanelSize): PanelSize {
  if (!value || typeof value !== 'object') return fallback;
  const o = value as { w?: unknown; h?: unknown };
  const maxH = typeof window !== 'undefined' ? panelMaxHeight() : fallback.h;
  return {
    w: clampLayoutNumber(o.w, PANEL_MIN.w, panelMaxW(), fallback.w),
    h: clampLayoutNumber(o.h, PANEL_MIN.h, maxH, fallback.h),
  };
}
export const PREVIEW_KEY_SKIP_SEC = 5;
export const PREVIEW_FS_CONTROLS_HIDE_MS = 200;
export const PREVIEW_DEFAULT_VOLUME = 0.1;
export const PREVIEW_PANEL_DEFAULT_W = 640;
export const PREVIEW_PANEL_MIN_W = 280;
export const PREVIEW_PANEL_CHROME_H_EST = 120;
export const PREVIEW_PANEL_PAD_H = 32;
export const PREVIEW_VIDEO_ASPECT_DEFAULT = 16 / 9;
export const URL_ASIDE_PANEL_DEFAULT: PanelSize = { w: 288, h: 414 };
/** Min height when trim UI + action buttons must stay visible. */
export const URL_ASIDE_TRIM_MIN_H = 414;
export const MAIN_PANEL_DEFAULT: PanelSize = { w: 448, h: 448 };
export const PANEL_MIN: PanelSize = { w: 200, h: 180 };
export const VIEWPORT_EDGE_LOCK = 40;
export const EXPLORE_POPUP_Z = 9999;
export const MAX_EXPLORE_POPUPS = 5;
export const LAYOUT_ROW_GAP_TRIPLE = 12;
export const LAYOUT_ROW_GAP_SPLIT = 24;
export const RESIZE_EDGE_CURSORS: Record<ResizeEdge, string> = {
  n: 'ns-resize',
  s: 'ns-resize',
  e: 'ew-resize',
  w: 'ew-resize',
  ne: 'nesw-resize',
  nw: 'nwse-resize',
  se: 'nwse-resize',
  sw: 'nesw-resize',
};
export const PANEL_LAYOUT_STORAGE_KEY = 'vodrip_panel_layout';
