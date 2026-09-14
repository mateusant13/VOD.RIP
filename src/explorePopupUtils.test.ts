import { describe, expect, it } from 'vitest';
import {
  EXPLORE_PANEL_BOX_MIN_H,
  EXPLORE_PANEL_BOX_MIN_W,
  clampExplorePanelBox,
  panelResizeCornerSize,
} from './explorePopupUtils';

const MIN = { w: EXPLORE_PANEL_BOX_MIN_W, h: EXPLORE_PANEL_BOX_MIN_H };

describe('clampExplorePanelBox', () => {
  const viewport = { w: 1280, h: 800 };

  it('passes in-range sizes through unchanged', () => {
    expect(clampExplorePanelBox({ w: 600, h: 500 }, viewport, MIN)).toEqual({ w: 600, h: 500 });
  });

  it('clamps below-min sizes up to the minimum, per dimension', () => {
    expect(clampExplorePanelBox({ w: 100, h: 400 }, viewport, MIN)).toEqual({
      w: EXPLORE_PANEL_BOX_MIN_W,
      h: 400,
    });
    expect(clampExplorePanelBox({ w: 400, h: 40 }, viewport, MIN)).toEqual({
      w: 400,
      h: EXPLORE_PANEL_BOX_MIN_H,
    });
    expect(clampExplorePanelBox({ w: 10, h: 10 }, viewport, MIN)).toEqual(MIN);
  });

  it('clamps above-viewport sizes down to the viewport, per dimension', () => {
    expect(clampExplorePanelBox({ w: 5000, h: 500 }, viewport, MIN)).toEqual({
      w: 1280,
      h: 500,
    });
    expect(clampExplorePanelBox({ w: 500, h: 4000 }, viewport, MIN)).toEqual({
      w: 500,
      h: 800,
    });
    expect(clampExplorePanelBox({ w: 9000, h: 9000 }, viewport, MIN)).toEqual({ w: 1280, h: 800 });
  });

  it('keeps the minimum when the viewport is smaller than the minimum', () => {
    // Degenerate viewport: min wins so the panel stays usable.
    expect(clampExplorePanelBox({ w: 500, h: 400 }, { w: 200, h: 200 }, MIN)).toEqual(MIN);
  });
});

describe('panelResizeCornerSize', () => {
  it('clamps a clipped host corner to its tightest padding edge', () => {
    // The archive search popup is `overflow: hidden` (p-3 = 12px padding). A
    // corner block anchored at the padding-box edge must stay within that 12px
    // gutter: larger, and it overlaps the content box where the footer's
    // Cancel button sits. The pre-fix size for its shadow-2xl band was 52px.
    expect(panelResizeCornerSize(true, 50, 12)).toBe(12);
    expect(panelResizeCornerSize(true, 0, 12)).toBe(12);
    // The clamp is authoritative over the standard size: a hypothetical host
    // with one tight edge (pt-1 = 4px among larger paddings — the measurement
    // takes min of all four paddings, so gutter=4) gets a 4px corner, not 12
    // nor 16 — never swallowing the tighter edge's content beats grip size.
    expect(panelResizeCornerSize(true, 6, 4)).toBe(4);
    // Degenerate zero gutter: 0px is honest (a host with no padding has no
    // safe gutter to paint in); real handle hosts all declare p-3 or more.
    expect(panelResizeCornerSize(true, 6, 0)).toBe(0);
    // Cap still holds for absurd measurements.
    expect(panelResizeCornerSize(true, 0, 999)).toBe(16);
  });

  it('keeps a non-clipped corner covering the real opaque band', () => {
    // Hosts that paint outside straddle border + band: 4/6/8px bands get a 16px
    // block, the 18px multi-platform stack gets 20 — unchanged from before.
    expect(panelResizeCornerSize(false, 4, 16)).toBe(16);
    expect(panelResizeCornerSize(false, 6, 16)).toBe(16);
    expect(panelResizeCornerSize(false, 8, 16)).toBe(16);
    expect(panelResizeCornerSize(false, 18, 16)).toBe(20);
    // The inner gutter is irrelevant for non-clipped hosts (the block sits
    // outside the border, never over content).
    expect(panelResizeCornerSize(false, 0, 0)).toBe(16);
  });
});
