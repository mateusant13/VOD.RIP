import { describe, expect, it } from 'vitest';
import {
  EXPLORE_PANEL_BOX_MIN_H,
  EXPLORE_PANEL_BOX_MIN_W,
  clampExplorePanelBox,
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
