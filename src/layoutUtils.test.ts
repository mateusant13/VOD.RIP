import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  layoutRowWidthBudget,
  layoutMaxPanelWidthAtSiblingMins,
  resizeLayoutGivingWidthTo,
  shrinkLayoutPanelsToFit,
  PREVIEW_PANEL_MIN_W,
  PANEL_MIN,
} from './layoutUtils';
import type { LayoutPanelBoundsInput } from './types';

const tripleLayout = (): LayoutPanelBoundsInput => ({
  previewOpen: true,
  urlPanelAside: true,
  preview: { w: 640, h: 0 },
  urlAside: { w: 288, h: 414 },
  main: { w: 448, h: 448 },
});

describe('layoutUtils resize budget', () => {
  const innerWidth = 1600;

  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: innerWidth });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
  });

  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 768 });
  });

  it('lets preview grow to viewport budget by shrinking siblings', () => {
    const layout = tripleLayout();
    const budget = layoutRowWidthBudget(layout);
    const previewMax = layoutMaxPanelWidthAtSiblingMins('preview', layout);
    expect(previewMax).toBe(budget - PANEL_MIN.w - PANEL_MIN.w);

    const fitted = resizeLayoutGivingWidthTo(layout, 'preview', previewMax);
    const total = fitted.preview.w + fitted.urlAside.w + fitted.main.w;
    expect(total).toBeLessThanOrEqual(budget);
    expect(fitted.preview.w).toBe(previewMax);
    expect(fitted.urlAside.w).toBe(PANEL_MIN.w);
    expect(fitted.main.w).toBe(PANEL_MIN.w);
  });

  it('keeps the row within budget after proportional shrink', () => {
    const layout = tripleLayout();
    const budget = layoutRowWidthBudget(layout);
    const blown = {
      ...layout,
      preview: { w: 1200, h: 0 },
      urlAside: { w: 500, h: 414 },
      main: { w: 500, h: 448 },
    };
    const fitted = shrinkLayoutPanelsToFit(blown);
    const total = fitted.preview.w + fitted.urlAside.w + fitted.main.w;
    expect(total).toBeLessThanOrEqual(budget);
    expect(fitted.preview.w).toBeGreaterThanOrEqual(PREVIEW_PANEL_MIN_W);
  });

  it('accounts for triple-panel gaps in the budget', () => {
    const layout = tripleLayout();
    const budget = layoutRowWidthBudget(layout);
    const previewMax = layoutMaxPanelWidthAtSiblingMins('preview', layout);
    expect(previewMax + PANEL_MIN.w + PANEL_MIN.w).toBe(budget);
  });
});
