/**
 * Frame snap pin — the ONE place a floating window is placed into a frame
 * cell.
 *
 * This used to live inline in ChannelExplorePopup's layout effect, which
 * meant every other window family (live players) could not snap at all
 * without a second, drifting copy of the same math. The geometry that
 * matters — inner box, centring, clamping, writing the position — is shared
 * here; each window family only supplies `fit()`, how it wants to size
 * itself inside the cell.
 */
import { useLayoutEffect, useRef, type MutableRefObject } from 'react';
import type { FrameRect } from './frameLayout';

/** Breathing room between the cell border and the pinned window. */
export const FRAME_SNAP_INNER_PAD = 6;

export type FrameSnapPos = { x: number; y: number };
export type FrameSnapSize = { w: number; h: number };

/** The drawable area of a cell (cell rect minus the inner padding). */
export function frameSnapInnerBox(rect: FrameRect): FrameRect {
  return {
    x: rect.x + FRAME_SNAP_INNER_PAD,
    y: rect.y + FRAME_SNAP_INNER_PAD,
    w: Math.max(0, rect.w - FRAME_SNAP_INNER_PAD * 2),
    h: Math.max(0, rect.h - FRAME_SNAP_INNER_PAD * 2),
  };
}

/**
 * Width left for a snapped explore panel's VIDEO column: the cell inner width
 * minus the chat column.
 *
 * The policy has to be explicit because a frame cell can be narrower than the
 * chat column: the chat is clamped to what the cell can afford and the video
 * takes the remainder, never negative and never overflowing the cell. A panel
 * that asks for more room than the layout has is the "dropped into a layout and
 * it breaks" failure, and it used to be reachable - the old fit used
 * Math.max(EXPLORE_PANEL_MIN_W, inner.w - chatW), which for a narrow cell asked
 * for more than the whole cell and desynchronised panelWidthRef from the
 * React state derived from it.
 */
export function exploreSnapVideoWidth(innerW: number, chatW: number): number {
  const w = Math.max(0, innerW);
  const chat = Math.max(0, Math.min(chatW, w));
  return Math.max(1, w - chat);
}

/** Top-left that centres `size` inside `rect`. */
export function frameSnapPos(rect: FrameRect, size: FrameSnapSize): FrameSnapPos {
  const inner = frameSnapInnerBox(rect);
  return {
    x: inner.x + Math.max(0, (inner.w - size.w) / 2),
    y: inner.y + Math.max(0, (inner.h - size.h) / 2),
  };
}

/**
 * The DROP GHOST: where the window the user is holding would land in `rect`.
 *
 * While a drag is in flight the user is asking "what happens if I let go
 * here?", and a highlighted cell alone answers it only for a window that
 * exactly fills the cell. This returns the window's own size clamped into the
 * cell's inner box and centred, so the preview shows the REAL result — a
 * window wider than the cell is shrunk to it rather than drawn overflowing,
 * and a window smaller than the cell is centred instead of stretched.
 *
 * Pure and honest about its inputs: a missing size (offsetWidth 0 on an
 * unmeasurable window) falls back to filling the cell rather than drawing a
 * degenerate one.
 */
export function frameDropGhost(rect: FrameRect, size: FrameSnapSize | null | undefined):
  FrameSnapPos & FrameSnapSize {
  const inner = frameSnapInnerBox(rect);
  const w = Math.max(1, Math.min(size && size.w > 0 ? size.w : inner.w, inner.w));
  const h = Math.max(1, Math.min(size && size.h > 0 ? size.h : inner.h, inner.h));
  const pos = frameSnapPos(rect, { w, h });
  return { x: pos.x, y: pos.y, w, h };
}

/**
 * Pin `el` into `rect` whenever `rect` is set (null = not snapped, the
 * caller keeps its own free-floating layout).
 *
 * `repinKey` re-runs the pin when the window's own content changes shape
 * (measured chrome height, stream aspect, chat column) and the pin must be
 * recomputed. It is deliberately NOT a function identity: an inline `fit`
 * or a fresh `rect` object must not re-pin on unrelated renders — that churn
 * is the "snaps, then springs back" failure.
 */
export function useFrameSnapPin(opts: {
  el: HTMLElement | null;
  rect: FrameRect | null;
  posRef: MutableRefObject<FrameSnapPos | null>;
  setPos: (pos: FrameSnapPos) => void;
  fit: (inner: FrameRect) => FrameSnapSize;
  onPin?: (size: FrameSnapSize) => void;
  repinKey?: unknown;
}): boolean {
  const { el, rect, posRef, repinKey } = opts;
  // Latest callbacks in refs: the effect must depend on the CELL, not on the
  // identity of a closure rebuilt every render.
  const live = useRef(opts);
  live.current = opts;

  useLayoutEffect(() => {
    if (!rect || !el) return;
    const { posRef: pr, setPos, fit, onPin } = live.current;
    const inner = frameSnapInnerBox(rect);
    const size = fit(inner);
    const snapped = frameSnapPos(rect, size);
    pr.current = snapped;
    setPos(snapped);
    el.style.position = 'fixed';
    el.style.top = `${snapped.y}px`;
    el.style.left = `${snapped.x}px`;
    el.style.right = 'auto';
    el.style.bottom = 'auto';
    el.style.width = `${size.w}px`;
    el.style.maxWidth = `${inner.w}px`;
    el.style.maxHeight = `${inner.h}px`;
    onPin?.(size);
  }, [el, rect, posRef, repinKey]);

  return rect != null;
}
