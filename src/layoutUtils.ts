/**
 * Layout/panel utility functions extracted from App.tsx.
 */
import { panelMaxWidthCap, readUiScale } from './uiScale';
import { panelResizeHandleInset, makeRafMoveLoop, suspendPanelTransitions, restorePanelTransitions, type ResizeEdge } from './explorePopupUtils';
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
export function layoutRowHasMultiplePanels(layout: LayoutPanelBoundsInput): boolean {
  let count = 1;
  if (layout.previewOpen) count += 1;
  if (layout.urlPanelAside) count += 1;
  return count > 1;
}
/** Max width for one panel when every sibling is at its minimum width. */
export function layoutMaxPanelWidthAtSiblingMins(
  target: LayoutPanelKey,
  layout: LayoutPanelBoundsInput,
): number {
  const budget = layoutRowWidthBudget(layout);
  let minOthers = 0;
  if (layout.previewOpen && target !== 'preview' && target !== 'live') {
    minOthers += layout.liveOpen ? LIVE_PANEL_MIN_W : PREVIEW_PANEL_MIN_W;
  }
  if (layout.urlPanelAside && target !== 'urlAside') minOthers += PANEL_MIN.w;
  if (target !== 'main') minOthers += PANEL_MIN.w;
  const minTarget = target === 'preview' ? PREVIEW_PANEL_MIN_W : target === 'live' ? LIVE_PANEL_MIN_W : PANEL_MIN.w;
  return Math.max(minTarget, budget - minOthers);
}
export function layoutMaxPanelWidth(target: LayoutPanelKey, layout: LayoutPanelBoundsInput): number {
  const { maxW } = viewportContentBox();
  const count = (layout.previewOpen ? 1 : 0) + (layout.urlPanelAside ? 1 : 0) + 1;
  const gapTotal = Math.max(0, count - 1) * layoutRowGap(layout.previewOpen, layout.urlPanelAside);

  let othersW = 0;
  const slotIsLive = !!layout.liveOpen;
  if (layout.previewOpen && target !== 'preview' && target !== 'live') {
    othersW += slotIsLive ? (layout.live?.w ?? 0) : layout.preview.w;
  }
  if (layout.urlPanelAside && target !== 'urlAside') othersW += layout.urlAside.w;
  if (target !== 'main') othersW += layout.main.w;

  return Math.max(
    PANEL_MIN.w,
    Math.min(layoutMaxPanelWidthAtSiblingMins(target, layout), maxW - othersW - gapTotal),
  );
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
  const minW = target === 'live' ? LIVE_PANEL_MIN_W : PANEL_MIN.w;
  const minH = target === 'urlAside' ? URL_ASIDE_TRIM_MIN_H : PANEL_MIN.h;
  return {
    w: Math.min(maxW, Math.max(minW, size.w)),
    h: Math.min(maxH, Math.max(minH, size.h)),
  };
}
export function layoutRowWidthBudget(layout: LayoutPanelBoundsInput): number {
  const { usableWidth } = layoutRowEdgeInsets();
  const count = (layout.previewOpen ? 1 : 0) + (layout.urlPanelAside ? 1 : 0) + 1;
  const gapTotal = Math.max(0, count - 1) * layoutRowGap(layout.previewOpen, layout.urlPanelAside);
  return usableWidth - gapTotal;
}

/** Shrink siblings when one panel grows so the row stays within the viewport budget. */
export function resizeLayoutGivingWidthTo(
  layout: LayoutPanelBoundsInput,
  target: LayoutPanelKey,
  desiredW: number,
  preferred?: Partial<Record<LayoutPanelKey, number>>,
): { preview: PanelSize; urlAside: PanelSize; main: PanelSize; live?: PanelSize } {
  let preview = { ...layout.preview };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };
  // Live replaces the preview slot; both stay mirrored while live is open.
  let live = { ...(layout.live ?? layout.preview) };

  const slotMinW = () => (layout.liveOpen ? LIVE_PANEL_MIN_W : PREVIEW_PANEL_MIN_W);
  const minWFor = (key: LayoutPanelKey) =>
    key === 'preview' || key === 'live' ? slotMinW() : PANEL_MIN.w;

  const getW = (key: LayoutPanelKey) => {
    if (key === 'preview' || key === 'live') return layout.liveOpen ? live.w : preview.w;
    if (key === 'urlAside') return urlAside.w;
    return main.w;
  };

  const setW = (key: LayoutPanelKey, w: number) => {
    if (key === 'preview' || key === 'live') {
      if (layout.liveOpen) {
        live = { ...live, w };
        preview = { ...preview, w };
      } else {
        preview = { ...preview, w };
      }
    } else if (key === 'urlAside') urlAside = { ...urlAside, w };
    else main = { ...main, w };
  };

  const maxAtMins = layoutMaxPanelWidthAtSiblingMins(target, layout);
  setW(target, Math.min(maxAtMins, Math.max(minWFor(target), desiredW)));

  type Slot = { key: LayoutPanelKey; get: () => number; set: (w: number) => void; minW: number };
  const siblingSlots: Slot[] = [];
  if (layout.previewOpen && target !== 'preview' && target !== 'live') {
    siblingSlots.push({
      key: 'preview',
      get: () => (layout.liveOpen ? live.w : preview.w),
      set: (w) => { if (layout.liveOpen) { live = { ...live, w }; preview = { ...preview, w }; } else preview = { ...preview, w }; },
      minW: slotMinW(),
    });
  }
  if (layout.urlPanelAside && target !== 'urlAside') {
    siblingSlots.push({
      key: 'urlAside',
      get: () => urlAside.w,
      set: (w) => { urlAside = { ...urlAside, w }; },
      minW: PANEL_MIN.w,
    });
  }
  if (target !== 'main') {
    siblingSlots.push({
      key: 'main',
      get: () => main.w,
      set: (w) => { main = { ...main, w }; },
      minW: PANEL_MIN.w,
    });
  }

  const budget = layoutRowWidthBudget(layout);

  // ponytail: `preferred` = sibling widths captured at drag start. Makes the
  // drag reversible — shrinking the target back grows siblings toward their
  // pre-drag widths instead of leaving the one-way ratchet in place.
  if (preferred) {
    const prefW = (slot: Slot) => {
      const p = preferred[slot.key];
      return typeof p === 'number' && Number.isFinite(p)
        ? Math.max(slot.minW, p)
        : slot.get();
    };
    const remaining = budget - getW(target);
    const minTotal = siblingSlots.reduce((sum, slot) => sum + slot.minW, 0);
    if (remaining <= minTotal) {
      for (const slot of siblingSlots) slot.set(slot.minW);
    } else {
      const prefTotal = siblingSlots.reduce((sum, slot) => sum + prefW(slot), 0);
      if (prefTotal <= remaining) {
        for (const slot of siblingSlots) slot.set(prefW(slot));
      } else {
        const scale = remaining / prefTotal;
        for (const slot of siblingSlots) {
          slot.set(Math.max(slot.minW, Math.floor(prefW(slot) * scale)));
        }
        // Min-clamping a sibling past its proportional share can overshoot
        // `remaining`; shave the overflow off siblings with headroom so the
        // dragged panel keeps the pointer's width (the fit pass below would
        // otherwise shrink the target too).
        let sibTotal = siblingSlots.reduce((sum, slot) => sum + slot.get(), 0);
        let overflow = sibTotal - remaining;
        if (overflow > 0) {
          const flexTotal = siblingSlots.reduce((sum, slot) => sum + (slot.get() - slot.minW), 0);
          if (flexTotal > 0) {
            for (const slot of siblingSlots) {
              const excess = slot.get() - slot.minW;
              const shave = Math.min(excess, Math.ceil(overflow * (excess / flexTotal)));
              slot.set(slot.get() - shave);
            }
          }
        }
      }
      // Slack-grow: leftover row budget grows the center panels back toward
      // their aspect-consistent widths (capped at defaults) — a panel squeezed
      // to its minimum by a neighbor's growth returns to its shape once the
      // neighbor releases the space. The dragged target is never grown: its
      // width is the pointer's explicit choice.
      const sibTotal = siblingSlots.reduce((sum, slot) => sum + slot.get(), 0);
      const slack = remaining - sibTotal;
      if (slack > 0) {
        const grown = growCenterPanelsFromSlack(urlAside, main, slack, target);
        urlAside = grown.urlAside;
        main = grown.main;
      }
    }
    return shrinkLayoutPanelsToFit({ ...layout, preview, urlAside, main });
  }

  let total = getW(target) + siblingSlots.reduce((sum, slot) => sum + slot.get(), 0);
  if (total <= budget) {
    return layout.liveOpen ? { preview, urlAside, main, live } : { preview, urlAside, main };
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

  return shrinkLayoutPanelsToFit({ ...layout, preview, urlAside, main, live });
}

/** Shrink visible panel widths proportionally when the row exceeds the viewport. */
export function shrinkLayoutPanelsToFit(layout: LayoutPanelBoundsInput): {
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
  live?: PanelSize;
} {
  let preview = { ...layout.preview };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };
  let live = { ...(layout.live ?? layout.preview) };

  type Slot = {
    get: () => number;
    set: (w: number) => void;
    minW: number;
  };
  const slots: Slot[] = [];
  if (layout.previewOpen) {
    slots.push({
      get: () => (layout.liveOpen ? live.w : preview.w),
      set: (w) => { if (layout.liveOpen) { live = { ...live, w }; preview = { ...preview, w }; } else preview = { ...preview, w }; },
      minW: layout.liveOpen ? LIVE_PANEL_MIN_W : PREVIEW_PANEL_MIN_W,
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
  if (total <= available) {
    return layout.liveOpen ? { preview, urlAside, main, live } : { preview, urlAside, main };
  }

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

  const result = layout.liveOpen ? { preview, urlAside, main, live } : { preview, urlAside, main };
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

export interface EffectivePanelLayout {
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
}

/**
 * Derive runtime (effective) panel widths from the user's preferred (dragged) widths.
 * Preferred is NEVER mutated. The runtime layer may clamp to viewport bounds; the
 * persisted preferred widths survive across refetches, reloads, and viewport changes.
 */
export function effectiveLayoutFromPreferred(
  preferred: PersistedPanelLayout,
  viewport: { previewOpen: boolean; urlPanelAside: boolean; chromeH?: number; aspect?: number },
): EffectivePanelLayout {
  const preferredSnapshot = {
    previewPanelWidth: preferred.previewPanelWidth,
    urlAside: { ...preferred.urlAside },
    main: { ...preferred.main },
  };
  const input: LayoutPanelBoundsInput = {
    previewOpen: viewport.previewOpen,
    urlPanelAside: viewport.urlPanelAside,
    preview: { w: preferredSnapshot.previewPanelWidth, h: 0 },
    urlAside: preferredSnapshot.urlAside,
    main: preferredSnapshot.main,
  };
  const clamped = clampAllLayoutPanels(input);
  const effective: EffectivePanelLayout = {
    preview: clamped.preview,
    urlAside: clamped.urlAside,
    main: clamped.main,
  };
  if (viewport.previewOpen) {
    // Cap against the CLAMPED layout (fitted siblings), matching the final
    // pass inside clampAllLayoutPanels and the DOM paint in App.tsx — the
    // video-fit bound must not be computed against stale desired widths.
    effective.preview = {
      w: clampPreviewPanelWidth(
        clamped.preview.w,
        viewport.chromeH ?? PREVIEW_PANEL_CHROME_H_EST,
        viewport.aspect ?? PREVIEW_VIDEO_ASPECT_DEFAULT,
        { ...input, preview: clamped.preview, urlAside: clamped.urlAside, main: clamped.main },
      ),
      h: clamped.preview.h,
    };
  }
  // ponytail: assert-based self-check — preferred must never be read or written here.
  console.assert(
    preferredSnapshot.previewPanelWidth === preferred.previewPanelWidth,
    'effectiveLayoutFromPreferred must not mutate preferred.previewPanelWidth',
  );
  console.assert(
    preferredSnapshot.urlAside.w === preferred.urlAside.w &&
      preferredSnapshot.urlAside.h === preferred.urlAside.h,
    'effectiveLayoutFromPreferred must not mutate preferred.urlAside',
  );
  console.assert(
    preferredSnapshot.main.w === preferred.main.w && preferredSnapshot.main.h === preferred.main.h,
    'effectiveLayoutFromPreferred must not mutate preferred.main',
  );
  return effective;
}

/**
 * Derive the runtime panel sizes for one render: height clamps first, then a
 * PROPORTIONAL width fit, then a final min-floor/video-cap safety pass.
 *
 * The old order clamped widths greedily per-panel (each against "budget minus
 * everyone else's minimum") BEFORE the fit: the preview absorbed the overflow
 * first and the last-clamped siblings floored at PANEL_MIN.w — the collapse
 * on enter (preview 765 + urlAside 240 + main 240 instead of ~611/302/355).
 */
export function clampAllLayoutPanels(layout: LayoutPanelBoundsInput): {
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
  live?: PanelSize;
} {
  const maxH = layoutMaxPanelHeight();
  let preview = { ...layout.preview };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };
  let live = { ...(layout.live ?? layout.preview) };

  // 1. Height clamps only. Widths keep their desired values so a row overflow
  //    is distributed proportionally below instead of being absorbed greedily
  //    by the first-clamped panel.
  if (layout.liveOpen) {
    live = { ...live, h: Math.min(maxH, Math.max(PANEL_MIN.h, live.h)) };
    if (layout.previewOpen) preview = { ...preview, w: live.w };
  }
  if (layout.urlPanelAside) {
    urlAside = { ...urlAside, h: Math.min(maxH, Math.max(URL_ASIDE_TRIM_MIN_H, urlAside.h)) };
  }
  main = { ...main, h: Math.min(maxH, Math.max(PANEL_MIN.h, main.h)) };

  // 2. Proportional fit on the desired widths: every visible panel shares the
  //    overflow (floored at its own minimum), so siblings never collapse to a
  //    PANEL_MIN.w strip while the preview keeps most of the row.
  const fitted = shrinkLayoutPanelsToFit({ ...layout, preview, urlAside, main, live });

  // 3. Final safety pass: per-panel min floors + the preview video-fit cap.
  //    After the proportional fit these only bite in degenerate cases (tiny
  //    viewports, desired widths below min) — they cannot re-collapse siblings.
  const urlAsideFinal = layout.urlPanelAside
    ? { ...fitted.urlAside, w: Math.max(PANEL_MIN.w, fitted.urlAside.w) }
    : fitted.urlAside;
  const mainFinal = { ...fitted.main, w: Math.max(PANEL_MIN.w, fitted.main.w) };
  let previewFinal = fitted.preview;
  let liveFinal = fitted.live ?? fitted.preview;
  if (layout.previewOpen && !layout.liveOpen) {
    previewFinal = {
      ...fitted.preview,
      w: clampPreviewPanelWidth(
        fitted.preview.w,
        PREVIEW_PANEL_CHROME_H_EST,
        PREVIEW_VIDEO_ASPECT_DEFAULT,
        { ...layout, preview: fitted.preview, urlAside: urlAsideFinal, main: mainFinal },
      ),
    };
  } else if (layout.liveOpen) {
    liveFinal = { ...liveFinal, w: Math.max(LIVE_PANEL_MIN_W, liveFinal.w) };
    if (layout.previewOpen) previewFinal = { ...previewFinal, w: liveFinal.w };
  }
  return layout.liveOpen
    ? { preview: previewFinal, urlAside: urlAsideFinal, main: mainFinal, live: liveFinal }
    : { preview: previewFinal, urlAside: urlAsideFinal, main: mainFinal };
}
export function maxPreviewPanelWidth(
  chromeH: number,
  aspect: number,
  layout: LayoutPanelBoundsInput,
): number {
  const rowMax = layoutMaxPanelWidthAtSiblingMins('preview', layout);
  if (layoutRowHasMultiplePanels(layout)) return rowMax;
  const shadowPad = panelResizeHandleInset(true);
  const { maxH } = viewportContentBox(shadowPad);
  const capW = layoutMaxPanelWidth('preview', layout);
  const videoMaxW = capW - PREVIEW_PANEL_PAD_H;
  const videoMaxH = Math.max(100, maxH - chromeH - PREVIEW_PANEL_PAD_H);
  const videoMaxWFromH = videoMaxH * aspect;
  return Math.floor(Math.min(rowMax, Math.min(videoMaxW, videoMaxWFromH) + PREVIEW_PANEL_PAD_H));
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
/**
 * Preview player container height for the current panel width and media
 * aspect. The frozen (user-picked) height survives refetches and sibling
 * squeezes, but a LANDSCAPE panel must never become taller than wide — the
 * container is capped at width minus chrome so the whole card stays
 * landscape (a 1920x1080 video in a portrait panel is the reported bug).
 * Portrait media (shorts 9:16) keeps its tall panel: height > width is
 * correct there. `chromeH` is the non-video card chrome (header + padding +
 * gap); App.tsx passes the measured value, the estimate is the fallback.
 */
export function previewContainerHeight(
  frozenH: number,
  panelW: number,
  aspect: number,
  chromeH = PREVIEW_PANEL_CHROME_H_EST,
): number {
  const naturalH = Math.round(panelW / Math.max(0.01, aspect));
  if (aspect < 1) {
    // Portrait media (shorts): preserve the tall panel the user picked;
    // never collapse below the video-fit height.
    return Math.max(frozenH, naturalH);
  }
  // Landscape: the user's picked height wins only while it fits the panel
  // width minus chrome; it can shrink but never grow past that cap, so the
  // panel cannot flip to portrait. Nothing picked yet → video-fit height.
  const cap = Math.max(naturalH, panelW - chromeH);
  return frozenH > 0 ? Math.min(frozenH, cap) : naturalH;
}

/** Fixed horizontal chrome of the preview card around the player row:
 *  card p-4 (16px per side) + border-2 (2px per side) + the row's gap-2 (8).
 *  Keep in sync with the card's p-4/border-2 classes and the row's gap-2. */
export const PREVIEW_PLAYER_ROW_FIXED_W = 36 + 8;
/**
 * Actual width available to the preview PLAYER container inside the card.
 * The container height must be derived from THIS width, not the card width:
 * the attached chat panel eats into the row, and a height computed from the
 * card width makes the container portrait (taller than wide) the moment the
 * chat opens — the reported "divs entering one another" bug. `chatW` is the
 * chat panel's rendered footprint (open width, collapsed strip, or 0 when
 * space-forced); pass the value reported via PreviewChatPanel's
 * onLayoutChange, or PANEL_STRIP_W's equivalent when it is not wired.
 */
export function previewPlayerColumnWidth(cardW: number, chatW: number): number {
  return Math.max(0, cardW - PREVIEW_PLAYER_ROW_FIXED_W - chatW);
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
// New position for a floating popup after resizing to `nextSize` from `startSize`
// at `startPos`: west/north edges keep their outer edge fixed, then the result is
// clamped to stay `margin` px inside `viewport`. Used by the live player popup.
export function panelPosAfterResize(
  edge: ResizeEdge,
  startPos: PanelPos,
  startSize: PanelSize,
  nextSize: PanelSize,
  viewport: { w: number; h: number },
  margin = 8,
): PanelPos {
  let x = startPos.x;
  let y = startPos.y;
  if (edgeAffectsWest(edge)) x = startPos.x + (startSize.w - nextSize.w);
  if (edgeAffectsNorth(edge)) y = startPos.y + (startSize.h - nextSize.h);
  return {
    x: Math.max(margin, Math.min(viewport.w - nextSize.w - margin, x)),
    y: Math.max(margin, Math.min(viewport.h - nextSize.h - margin, y)),
  };
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
    onResizeMove?: (size: PanelSize) => void;
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
  const prevTransition = suspendPanelTransitions(panelEl);

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

  const applySize = (size: PanelSize) => {
    sizeRef.current = size;
    if (panelEl) {
      applyPanelSize(panelEl, size);
    }
    opts?.onResizeMove?.(size);
  };

  const loop = makeRafMoveLoop((x, y) => {
    let next = calcSize(x, y);
    if (opts?.clampSize) next = opts.clampSize(next);
    applySize(next);
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
    const final = opts?.clampSize ? opts.clampSize(sizeRef.current) : sizeRef.current;
    applySize(final);
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
  const prevTransition = suspendPanelTransitions(panelEl);
  // Geometry reads hoisted to drag start (read-after-write would force a
  // synchronous layout per pointermove); viewport dims cannot change mid-drag.
  const startPanelH = panelEl ? panelEl.offsetHeight || 1 : 1;
  const viewport = { w: window.innerWidth, h: window.innerHeight };

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
  const urlAside = {
    w: Math.round(URL_ASIDE_PANEL_DEFAULT.w * scale),
    h: Math.round(URL_ASIDE_PANEL_DEFAULT.h * scale),
  };
  const main = {
    w: Math.round(MAIN_PANEL_DEFAULT.w * scale),
    h: Math.round(MAIN_PANEL_DEFAULT.h * scale),
  };
  const previewPanelWidth = Math.round(PREVIEW_PANEL_DEFAULT_W * scale);
  return {
    previewPanelWidth,
    urlAside,
    main,
    livePanelWidth: Math.round(LIVE_PANEL_DEFAULT_W * scale),
    owned: { preview: previewPanelWidth, urlAside: urlAside.w, main: main.w },
  };
}
/**
 * User-owned (restore-target) widths from a persisted layout. Falls back to the
 * stored visual widths for layouts saved before the `owned` field existed.
 */
export function userOwnedWidthsFrom(layout: PersistedPanelLayout): {
  preview: number;
  urlAside: number;
  main: number;
} {
  const fallback = {
    preview: layout.previewPanelWidth,
    urlAside: layout.urlAside.w,
    main: layout.main.w,
  };
  if (!layout.owned || typeof layout.owned !== 'object') return fallback;
  const o = layout.owned as { preview?: unknown; urlAside?: unknown; main?: unknown };
  const maxW = panelMaxW();
  return {
    preview: clampLayoutNumber(o.preview, PREVIEW_PANEL_MIN_W, maxW, fallback.preview),
    urlAside: clampLayoutNumber(o.urlAside, PANEL_MIN.w, maxW, fallback.urlAside),
    main: clampLayoutNumber(o.main, PANEL_MIN.w, maxW, fallback.main),
  };
}
/**
 * Migration heal for layouts persisted by the pre-owned resize bug: legacy
 * data has no `owned` field, so a min-parked panel's restore target falls back
 * to its own (thin) visual width — the one-way-ratchet that never restored
 * squares. For those layouts, reset the min-parked owned width to the default
 * shape (so a reverse drag grows it back) and grow the visual width now while
 * the row has slack. Layouts WITH `owned` are untouched: owned is the user's
 * restore target, and a deliberate drag to the minimum writes owned == min.
 * ponytail: a stronger heuristic (e.g. resetting any sub-default width from
 * legacy data) could restore legacy proportional leftovers, but would also
 * fight deliberate small sizes; exact-min only, and only for owned-less data.
 */
export function healSqueezedPanelLayout(
  layout: PersistedPanelLayout,
): PersistedPanelLayout & { owned: { preview: number; urlAside: number; main: number } } {
  const owned = userOwnedWidthsFrom(layout);
  const legacy = !layout.owned || typeof layout.owned !== 'object';
  if (!legacy) {
    return { ...layout, owned };
  }

  const defaults = defaultPanelLayout();

  const healedOwned = {
    preview: owned.preview <= PREVIEW_PANEL_MIN_W + 1 ? defaults.previewPanelWidth : owned.preview,
    urlAside: owned.urlAside <= PANEL_MIN.w + 1 ? defaults.urlAside.w : owned.urlAside,
    main: owned.main <= PANEL_MIN.w + 1 ? defaults.main.w : owned.main,
  };

  let previewW = layout.previewPanelWidth;
  let urlAsideW = layout.urlAside.w;
  let mainW = layout.main.w;
  // All-three-visible budget is the conservative (tightest) row budget, so
  // healing never overflows the row no matter which panels actually render.
  const budget =
    typeof window === 'undefined'
      ? 0
      : layoutRowWidthBudget({
          previewOpen: true,
          urlPanelAside: true,
          preview: { w: previewW, h: 0 },
          urlAside: layout.urlAside,
          main: layout.main,
        });
  let slack = Math.max(0, budget - previewW - urlAsideW - mainW);
  if (slack > 0) {
    const grow = (atMin: boolean, current: number, target: number) => {
      if (!atMin) return current;
      const next = Math.min(target, current + slack);
      slack -= next - current;
      return next;
    };
    urlAsideW = grow(urlAsideW <= PANEL_MIN.w + 1, urlAsideW, healedOwned.urlAside);
    mainW = grow(mainW <= PANEL_MIN.w + 1, mainW, healedOwned.main);
    previewW = grow(previewW <= PREVIEW_PANEL_MIN_W + 1, previewW, healedOwned.preview);
  }

  return {
    ...layout,
    previewPanelWidth: previewW,
    urlAside: { ...layout.urlAside, w: urlAsideW },
    main: { ...layout.main, w: mainW },
    owned: healedOwned,
  };
}
/**
 * Repair for layouts persisted with squeezed/clamped runtime widths (the
 * pre-fix persist bug): the persist effect saved the RUNTIME row — including
 * preview-open sibling squeezes and small-viewport clamps — as both the
 * visual and `owned` widths. healSqueezedPanelLayout only heals legacy
 * owned-less data, so modern layouts loaded squeezed forever. This detects
 * VERY inconsistent rows — all three owned widths parked at their minimums,
 * or the owned sum below 60% of the all-three-visible row budget — and
 * resets them to the default shape. Guard: only repair when the current
 * viewport actually fits the default row, so a genuinely small window is
 * never fought (no repair, no overwrite). The live popup is not part of this
 * row, so its width survives a repair.
 */
export function repairInconsistentPanelLayout(
  layout: PersistedPanelLayout,
): PersistedPanelLayout {
  const owned = userOwnedWidthsFrom(layout);
  const allMin =
    owned.preview <= PREVIEW_PANEL_MIN_W + 1 &&
    owned.urlAside <= PANEL_MIN.w + 1 &&
    owned.main <= PANEL_MIN.w + 1;
  // All-three-visible budget is the conservative (tightest) row budget, so a
  // repair never overflows the row no matter which panels actually render.
  const budget =
    typeof window === 'undefined'
      ? 0
      : layoutRowWidthBudget({
          previewOpen: true,
          urlPanelAside: true,
          preview: { w: layout.previewPanelWidth, h: 0 },
          urlAside: layout.urlAside,
          main: layout.main,
        });
  const ownedSum = owned.preview + owned.urlAside + owned.main;
  const sumBelowBudget = budget > 0 && ownedSum < 0.6 * budget;
  if (!allMin && !sumBelowBudget) return layout;

  const defaults = defaultPanelLayout();
  // Guard: the default row (three panels; the budget already accounts for the
  // inter-panel gaps) must fit the current viewport — a small window must
  // keep its squeezed-but-fitting layout untouched.
  if (defaults.previewPanelWidth + defaults.urlAside.w + defaults.main.w > budget) {
    return layout;
  }
  return {
    ...defaults,
    // The live popup is not part of this row — keep its stored width when
    // present and sane, else the default.
    livePanelWidth: clampLayoutNumber(
      layout.livePanelWidth,
      LIVE_PANEL_MIN_W,
      panelMaxW(),
      defaults.livePanelWidth ?? LIVE_PANEL_DEFAULT_W,
    ),
  };
}
export function loadPanelLayout(): PersistedPanelLayout {
  const fallback = defaultPanelLayout();
  try {
    const raw = localStorage.getItem(PANEL_LAYOUT_STORAGE_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<PersistedPanelLayout>;
    const urlAside = sanitizeStoredPanelSize(parsed.urlAside, URL_ASIDE_PANEL_DEFAULT);
    const main = sanitizeStoredPanelSize(parsed.main, MAIN_PANEL_DEFAULT);
    const previewPanelWidth = clampLayoutNumber(
      parsed.previewPanelWidth,
      PREVIEW_PANEL_MIN_W,
      panelMaxW(),
      fallback.previewPanelWidth,
    );
    const base: PersistedPanelLayout = {
      previewPanelWidth,
      urlAside,
      main,
      livePanelWidth: parsed.livePanelWidth !== undefined
        ? clampLayoutNumber(
            parsed.livePanelWidth,
            LIVE_PANEL_MIN_W,
            panelMaxW(),
            fallback.livePanelWidth ?? LIVE_PANEL_DEFAULT_W,
          )
        : fallback.livePanelWidth,
    };
    return repairInconsistentPanelLayout(healSqueezedPanelLayout({ ...base, owned: parsed.owned }));
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
  const maxW = typeof window !== 'undefined' ? viewportContentBox().maxW : fallback.w;
  return {
    w: clampLayoutNumber(o.w, PANEL_MIN.w, maxW, fallback.w),
    h: clampLayoutNumber(o.h, PANEL_MIN.h, maxH, fallback.h),
  };
}
/**
 * Viewport-INDEPENDENT prefer-preserving sanitize for stored panel sizes.
 * Clamps to hard caps that survive small viewports; the runtime layer
 * (`effectiveLayoutFromPreferred`) handles per-render viewport clamping.
 * ponytail: a UI-driven "max panel size" upgrade could replace PANEL_MAX_H_HARD
 * with a scaled max without changing the public contract.
 */
export function sanitizeStoredPanelSize(value: unknown, fallback: PanelSize): PanelSize {
  if (!value || typeof value !== 'object') return { ...fallback };
  const o = value as { w?: unknown; h?: unknown };
  const maxW = panelMaxW();
  return {
    w: clampLayoutNumber(o.w, PANEL_MIN.w, maxW, fallback.w),
    h: clampLayoutNumber(o.h, PANEL_MIN.h, PANEL_MAX_H_HARD, fallback.h),
  };
}

export const PREVIEW_KEY_SKIP_SEC = 5;
export const PREVIEW_FS_CONTROLS_HIDE_MS = 200;
export const PREVIEW_DEFAULT_VOLUME = 0.3;
export const PREVIEW_PANEL_DEFAULT_W = 640;
export const PREVIEW_PANEL_MIN_W = 280;
export const PREVIEW_PANEL_CHROME_H_EST = 120;
export const PREVIEW_PANEL_PAD_H = 32;
export const LIVE_PANEL_DEFAULT_W = 480;
export const LIVE_PANEL_MIN_W = 320;
/** Live player's own min height — smaller than this the transport row
 *  collides with the header (mirror of URL_ASIDE_TRIM_MIN_H idea). */
export const LIVE_PANEL_MIN_H = 200;
/** Live player's own hard size cap — unlike the layout-coupled preview
 *  panel, the floating live player caps at these fixed bounds (also
 *  clamped to the viewport, see LivePlayerPopup handleResize). */
export const LIVE_PANEL_MAX_W = 1280;
export const LIVE_PANEL_MAX_H = 800;
export const PREVIEW_VIDEO_ASPECT_DEFAULT = 16 / 9;
export const URL_ASIDE_PANEL_DEFAULT: PanelSize = { w: 288, h: 480 };
/** Min height when trim UI + action buttons must stay visible. */
export const URL_ASIDE_TRIM_MIN_H = 480;
export const MAIN_PANEL_DEFAULT: PanelSize = { w: 448, h: 448 };
/**
 * Minimum width for urlAside/main panels. 240 keeps every panel's text on one
 * line (channel rows, VOD rows, trim UI, action bars all fit with truncation);
 * below it the fixed-content flex rows collapse their text children to 0.
 */
export const PANEL_MIN: PanelSize = { w: 240, h: 180 };
/** Hard upper bound for stored panel height (viewport-independent). */
export const PANEL_MAX_H_HARD = 3000;
export const VIEWPORT_EDGE_LOCK = 40;
export const EXPLORE_POPUP_Z = 9999;
/** Floating archive-search panels — own base of the SAME ladder players
 *  use (SEARCH_POPUP_Z + popupZOrder rank, rank from the shared counter),
 *  so a search popup clicked or opened via 'Search this video' lands ABOVE
 *  any player and the two search instances stack against each other. A
 *  player clicked afterwards comes back above the search (standard window
 *  stacking). */
export const SEARCH_POPUP_Z = EXPLORE_POPUP_Z;
export const MAX_EXPLORE_POPUPS = 5;
/** Z for floating popups without an explicit ladder rank (needle glance; a
 *  LivePlayerPopup rendered without a zIndex prop). Sits ABOVE the whole
 *  shared ladder (EXPLORE_POPUP_Z + popupZOrder rank): the needle glance is
 *  the active trim-drag feedback and must stay visible even with a search
 *  popup brought to front (batch-1 contract: preview popups float above the
 *  archive search while active). ponytail: the monotonic rank counter can
 *  grow past this constant in theory; in practice ranks are assigned only to
 *  the handful of concurrently-open popups, and the glance is transient —
 *  the upgrade path is a rank-aware glance z derived from the active player's
 *  rank, not a constant. */
export const LIVE_POPUP_ACTIVE_Z = EXPLORE_POPUP_Z + MAX_EXPLORE_POPUPS + 1;
/** Z for app-level modal overlays (download confirm, cookie-wait). Sits above
 *  the whole shared ladder (EXPLORE_POPUP_Z + popupZOrder rank) and the needle
 *  glance, so a modal spawned while popups are open always covers them.
 *  ponytail: same unbounded-counter ceiling as LIVE_POPUP_ACTIVE_Z — the
 *  upgrade path is a rank-aware modal z derived from the active max rank. */
export const MODAL_Z = EXPLORE_POPUP_Z + MAX_EXPLORE_POPUPS + 2;
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

/**
 * Aspect-consistent restore WIDTH for a center panel height: the width the
 * panel tends back to when the row frees space, capped at its default
 * (urlAside keeps its 288:414 default ratio; main keeps its 448 square).
 * Floored at PANEL_MIN.w so a short panel never grows past its square-ish
 * share (a deliberately small panel stays small).
 */
export function aspectWidthForHeight(key: 'urlAside' | 'main', h: number): number {
  if (key === 'main') {
    return Math.max(PANEL_MIN.w, Math.min(MAIN_PANEL_DEFAULT.w, Math.round(h)));
  }
  const hh = Math.max(h, URL_ASIDE_TRIM_MIN_H);
  return Math.max(
    PANEL_MIN.w,
    Math.min(
      URL_ASIDE_PANEL_DEFAULT.w,
      Math.round((hh * URL_ASIDE_PANEL_DEFAULT.w) / URL_ASIDE_PANEL_DEFAULT.h),
    ),
  );
}

/** Aspect-consistent restore HEIGHT for a center panel width: the height the
 *  panel falls back to when the preview stops forcing it taller (inverse of
 *  aspectWidthForHeight), capped at the panel's default height. */
export function aspectHeightForWidth(key: 'urlAside' | 'main', w: number): number {
  if (key === 'main') {
    return Math.max(PANEL_MIN.h, Math.min(MAIN_PANEL_DEFAULT.h, Math.round(w)));
  }
  return Math.max(
    URL_ASIDE_TRIM_MIN_H,
    Math.min(
      Math.max(URL_ASIDE_PANEL_DEFAULT.h, URL_ASIDE_TRIM_MIN_H),
      Math.round((w * URL_ASIDE_PANEL_DEFAULT.h) / URL_ASIDE_PANEL_DEFAULT.w),
    ),
  );
}

/**
 * Owned-height seed for a center panel: the stored height, capped at the
 * aspect-consistent height so heights ratcheted by the old one-way preview
 * sync heal back toward the default shape on load. Deliberate in-session
 * S-edge drags overwrite the seed; ponytail: this means a deliberately tall
 * panel loses its tallness across a reload (indistinguishable from ratchet
 * pollution from the stored value alone).
 */
export function ownedPanelHeightSeed(key: 'urlAside' | 'main', w: number, storedH: number): number {
  const floor = key === 'urlAside' ? URL_ASIDE_TRIM_MIN_H : PANEL_MIN.h;
  return Math.max(floor, Math.min(Math.max(storedH, floor), aspectHeightForWidth(key, w)));
}

/** Two-way preview-row height target for a side panel: row-aligned with the
 *  preview while it is tall, falling back to the panel's own restore height
 *  when the preview shrinks — never ratchets at the tall value. */
export function rowPanelHeightFromPreview(
  restoreH: number,
  previewH: number,
  maxH: number,
  minH: number,
): number {
  return Math.min(maxH, Math.max(minH, Math.max(restoreH, previewH)));
}

/**
 * Leftover-slack growth for the center panels (urlAside, then main): grows
 * each toward its aspect-consistent width, capped at its default, using only
 * slack that the rest of the row left unused. `skip` is the panel being
 * dragged — its width is the pointer's explicit choice and must not be grown.
 */
function growCenterPanelsFromSlack(
  urlAside: PanelSize,
  main: PanelSize,
  slack: number,
  skip: LayoutPanelKey,
): { urlAside: PanelSize; main: PanelSize } {
  if (slack <= 0) return { urlAside, main };
  const grow = (panel: PanelSize, targetW: number): PanelSize => {
    if (targetW <= panel.w) return panel;
    const amount = Math.min(targetW - panel.w, slack);
    if (amount <= 0) return panel;
    slack -= amount;
    return { ...panel, w: panel.w + amount };
  };
  return {
    urlAside: skip === 'urlAside' ? urlAside : grow(urlAside, aspectWidthForHeight('urlAside', urlAside.h)),
    main: skip === 'main' ? main : grow(main, aspectWidthForHeight('main', main.h)),
  };
}
