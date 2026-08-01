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

  it('clamps live panel width to min 320 / default 480 at sibling mins', () => {
    const layout: LayoutPanelBoundsInput = {
      ...tripleLayout(),
      previewOpen: true,
      liveOpen: true,
      preview: { w: 480, h: 0 },
      live: { w: 480, h: 0 },
    };
    const budget = layoutRowWidthBudget(layout);

    const liveMax = layoutMaxPanelWidthAtSiblingMins('live', layout);
    expect(liveMax).toBe(budget - PANEL_MIN.w - PANEL_MIN.w);

    // Too narrow → clamped up to 320.
    const tiny = resizeLayoutGivingWidthTo(layout, 'live', 100);
    expect(tiny.live?.w).toBe(320);
    // Too wide → clamped down to max at sibling mins, row stays in budget.
    const wide = resizeLayoutGivingWidthTo(layout, 'live', 10_000);
    const total = wide.live!.w + wide.urlAside.w + wide.main.w;
    expect(total).toBeLessThanOrEqual(budget);
    expect(wide.live?.w).toBe(liveMax);
    expect(wide.urlAside.w).toBe(PANEL_MIN.w);
    expect(wide.main.w).toBe(PANEL_MIN.w);
  });

  it('restores live width after a drag without letting preview mirror drift', () => {
    const layout: LayoutPanelBoundsInput = {
      ...tripleLayout(),
      previewOpen: true,
      liveOpen: true,
      preview: { w: 480, h: 0 },
      live: { w: 480, h: 0 },
    };
    const budget = layoutRowWidthBudget(layout);

    // Drag the live panel toward the urlAside edge.
    const fitted = resizeLayoutGivingWidthTo(layout, 'live', 560);
    expect(fitted.live?.w).toBe(560);
    expect(fitted.preview.w).toBe(560); // preview mirrors the live slot
    const total = fitted.live!.w + fitted.urlAside.w + fitted.main.w;
    expect(total).toBeLessThanOrEqual(budget);

    // Shrink the main panel afterwards still fits the row.
    const afterMain = resizeLayoutGivingWidthTo(
      { ...layout, preview: { w: fitted.live!.w, h: 0 }, live: { w: fitted.live!.w, h: 0 } },
      'main',
      700,
    );
    const total2 = afterMain.live!.w + afterMain.urlAside.w + afterMain.main.w;
    expect(total2).toBeLessThanOrEqual(budget);
  });
});
