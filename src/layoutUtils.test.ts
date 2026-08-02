import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  layoutRowWidthBudget,
  layoutMaxPanelWidthAtSiblingMins,
  resizeLayoutGivingWidthTo,
  shrinkLayoutPanelsToFit,
  clampPanelSizeForLayout,
  effectiveLayoutFromPreferred,
  panelPosAfterResize,
  healSqueezedPanelLayout,
  PREVIEW_PANEL_MIN_W,
  LIVE_PANEL_MIN_W,
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

  it('restores sibling widths when the drag reverses (preferred widths)', () => {
    const layout = tripleLayout();
    const budget = layoutRowWidthBudget(layout);
    const preferred = { urlAside: layout.urlAside.w, main: layout.main.w };
    const startW = layout.preview.w;

    // Drag preview wider: siblings shrink below their start widths.
    const grown = resizeLayoutGivingWidthTo(layout, 'preview', startW + 400, preferred);
    expect(grown.preview.w).toBeGreaterThan(startW);
    expect(grown.urlAside.w + grown.main.w).toBeLessThan(layout.urlAside.w + layout.main.w);
    expect(grown.preview.w + grown.urlAside.w + grown.main.w).toBeLessThanOrEqual(budget);

    // Drag back to the start width: siblings return to their exact start widths.
    // Chained from `grown` — a restore computed from the fresh fixture would
    // pass even without the `preferred` logic (old code: no-op early return).
    // resizeLayoutGivingWidthTo drops the visibility flags in its return, so
    // re-add them (production handlers pass the drag-start snapshot instead).
    const chained = { ...grown, previewOpen: true, urlPanelAside: true };
    const restored = resizeLayoutGivingWidthTo(chained, 'preview', startW, preferred);
    expect(restored.preview.w).toBe(startW);
    expect(restored.urlAside.w).toBe(layout.urlAside.w);
    expect(restored.main.w).toBe(layout.main.w);

    // Drag narrower than start: siblings stay at preferred (never exceed start).
    const shrunk = resizeLayoutGivingWidthTo(chained, 'preview', startW - 120, preferred);
    expect(shrunk.urlAside.w).toBe(layout.urlAside.w);
    expect(shrunk.main.w).toBe(layout.main.w);
  });

  it('still clamps siblings to mins when the target takes the whole budget', () => {
    const layout = tripleLayout();
    const budget = layoutRowWidthBudget(layout);
    const preferred = { urlAside: layout.urlAside.w, main: layout.main.w };
    const previewMax = layoutMaxPanelWidthAtSiblingMins('preview', layout);
    const fitted = resizeLayoutGivingWidthTo(layout, 'preview', previewMax + 500, preferred);
    // Exact values: the target clamps to max-at-mins and both siblings hit min.
    // Inequality assertions pass on the old ratchet code too ({996, 214, 269}).
    expect(fitted.preview.w).toBe(previewMax);
    expect(fitted.urlAside.w).toBe(PANEL_MIN.w);
    expect(fitted.main.w).toBe(PANEL_MIN.w);
    expect(fitted.preview.w + fitted.urlAside.w + fitted.main.w).toBeLessThanOrEqual(budget);
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

  it('heals min-parked panels from the pre-owned resize bug (visual + owned)', () => {
    // Legacy polluted layout: urlAside parked at min while the row has slack.
    const polluted = {
      previewPanelWidth: 680,
      urlAside: { w: PANEL_MIN.w, h: 620 },
      main: { w: 468, h: 620 },
    };
    const healed = healSqueezedPanelLayout(polluted);
    // Owned resets to the default shape so a reverse drag restores it.
    expect(healed.owned.urlAside).toBeGreaterThan(PANEL_MIN.w);
    // Visual grows back now because the row has slack.
    expect(healed.urlAside.w).toBeGreaterThan(PANEL_MIN.w);
    // Total still fits the budget.
    const budget = layoutRowWidthBudget({
      previewOpen: true,
      urlPanelAside: true,
      preview: { w: healed.previewPanelWidth, h: 0 },
      urlAside: healed.urlAside,
      main: healed.main,
    });
    expect(healed.previewPanelWidth + healed.urlAside.w + healed.main.w).toBeLessThanOrEqual(budget);
  });

  it('does not heal deliberate narrow panels above min', () => {
    const layout = {
      previewPanelWidth: 640,
      urlAside: { w: 250, h: 414 }, // narrow but above min — user's own choice
      main: { w: 448, h: 448 },
      owned: { preview: 640, urlAside: 250, main: 448 },
    };
    const healed = healSqueezedPanelLayout(layout);
    expect(healed.owned.urlAside).toBe(250);
    expect(healed.urlAside.w).toBe(250);
  });

  it('keeps a genuinely full row squeezed visually but resets the owned restore target', () => {
    // Preview maxed at sibling mins: row has zero slack — the squeeze is real.
    const layout = {
      previewPanelWidth: 1000, // budget(1480) - 240 - 240 at the new mins
      urlAside: { w: PANEL_MIN.w, h: 620 },
      main: { w: PANEL_MIN.w, h: 620 },
    };
    const healed = healSqueezedPanelLayout(layout);
    // Visual stays squeezed (no slack), owned resets so a reverse drag restores.
    expect(healed.urlAside.w).toBe(PANEL_MIN.w);
    expect(healed.main.w).toBe(PANEL_MIN.w);
    expect(healed.owned.urlAside).toBeGreaterThan(PANEL_MIN.w);
    expect(healed.owned.main).toBeGreaterThan(PANEL_MIN.w);
  });

  it('preserves deliberate exact-min drags when owned exists', () => {
    // New-code data: the user dragged the preview to its minimum on purpose —
    // drag-end wrote owned == min. The heal must NOT reset it on reload.
    const layout = {
      previewPanelWidth: PREVIEW_PANEL_MIN_W,
      urlAside: { w: PANEL_MIN.w, h: 437 },
      main: { w: PANEL_MIN.w, h: 437 },
      owned: { preview: PREVIEW_PANEL_MIN_W, urlAside: PANEL_MIN.w, main: PANEL_MIN.w },
    };
    const healed = healSqueezedPanelLayout(layout);
    expect(healed.previewPanelWidth).toBe(PREVIEW_PANEL_MIN_W);
    expect(healed.urlAside.w).toBe(PANEL_MIN.w);
    expect(healed.owned.preview).toBe(PREVIEW_PANEL_MIN_W);
    expect(healed.owned.main).toBe(PANEL_MIN.w);
  });

  it('enforces raised minimum widths on every clamp path', () => {
    // The whole point of the panel-min feature: urlAside/main never below 240,
    // preview never below 280, live never below 320. These exact values are
    // mirrored by e2e/repro-resize-full.mjs (main agent updates it on merge).
    expect(PANEL_MIN.w).toBe(240);
    expect(PREVIEW_PANEL_MIN_W).toBe(280);
    expect(LIVE_PANEL_MIN_W).toBe(320);
    // Triple layout at 1600x900: 280 + 240 + 240 fits the row budget easily.
    expect(PREVIEW_PANEL_MIN_W + PANEL_MIN.w + PANEL_MIN.w).toBeLessThanOrEqual(layoutRowWidthBudget(tripleLayout()));
  });

  it('never lets urlAside/main drop below the min during a drag', () => {
    const layout = tripleLayout();
    expect(resizeLayoutGivingWidthTo(layout, 'main', 1).main.w).toBe(PANEL_MIN.w);
    expect(resizeLayoutGivingWidthTo(layout, 'main', 1).urlAside.w).toBeGreaterThanOrEqual(PANEL_MIN.w);
    expect(resizeLayoutGivingWidthTo(layout, 'urlAside', 1).urlAside.w).toBe(PANEL_MIN.w);
    expect(resizeLayoutGivingWidthTo(layout, 'preview', 1).preview.w).toBe(PREVIEW_PANEL_MIN_W);
    // Drag the target past the pointer (desiredW huge) still floors siblings at min.
    const maxed = resizeLayoutGivingWidthTo(layout, 'main', 10_000);
    expect(maxed.main.w).toBe(layoutMaxPanelWidthAtSiblingMins('main', layout));
    expect(maxed.urlAside.w).toBe(PANEL_MIN.w);
    expect(maxed.preview.w).toBe(PREVIEW_PANEL_MIN_W);
  });

  it('shrinkLayoutPanelsToFit never renders a panel below its min', () => {
    // A row blown way past the budget floors every panel at its own min.
    const blown = {
      ...tripleLayout(),
      preview: { w: 4000, h: 0 },
      urlAside: { w: 4000, h: 414 },
      main: { w: 4000, h: 448 },
    };
    const fitted = shrinkLayoutPanelsToFit(blown);
    expect(fitted.preview.w).toBeGreaterThanOrEqual(PREVIEW_PANEL_MIN_W);
    expect(fitted.urlAside.w).toBeGreaterThanOrEqual(PANEL_MIN.w);
    expect(fitted.main.w).toBeGreaterThanOrEqual(PANEL_MIN.w);
    expect(fitted.preview.w + fitted.urlAside.w + fitted.main.w).toBeLessThanOrEqual(layoutRowWidthBudget(blown));
  });

  it('clampPanelSizeForLayout floors width at the panel min', () => {
    const layout = tripleLayout();
    const main = clampPanelSizeForLayout('main', { w: 30, h: 40 }, layout);
    expect(main.w).toBe(PANEL_MIN.w);
    expect(main.h).toBe(PANEL_MIN.h);
    const urlAside = clampPanelSizeForLayout('urlAside', { w: 0, h: 0 }, layout);
    expect(urlAside.w).toBe(PANEL_MIN.w);
    expect(urlAside.h).toBe(PANEL_MIN.h);
  });

  it('effective layout never renders a panel below its min width', () => {
    const preferred = {
      previewPanelWidth: 100,
      urlAside: { w: 50, h: 100 },
      main: { w: 60, h: 100 },
      livePanelWidth: 480,
    };
    const effective = effectiveLayoutFromPreferred(
      preferred,
      { previewOpen: true, urlPanelAside: true },
    );
    expect(effective.preview.w).toBeGreaterThanOrEqual(PREVIEW_PANEL_MIN_W);
    expect(effective.urlAside.w).toBeGreaterThanOrEqual(PANEL_MIN.w);
    expect(effective.main.w).toBeGreaterThanOrEqual(PANEL_MIN.w);
    // Preferred inputs stay untouched (persistence contract).
    expect(preferred.urlAside.w).toBe(50);
  });
});

describe('panelPosAfterResize (live popup west/north edges)', () => {
  const viewport = { w: 1920, h: 1080 };

  it('keeps the west edge fixed when growing to the east', () => {
    const p = panelPosAfterResize('e', { x: 100, y: 50 }, { w: 480, h: 320 }, { w: 640, h: 320 }, viewport);
    expect(p).toEqual({ x: 100, y: 50 });
  });

  it('moves the popup left when growing from the west edge (west edge fixed)', () => {
    const p = panelPosAfterResize('w', { x: 400, y: 50 }, { w: 480, h: 320 }, { w: 640, h: 320 }, viewport);
    expect(p).toEqual({ x: 240, y: 50 }); // 400 + (480-640): right edge stays at 880
  });

  it('moves the popup up when growing from the north edge (north edge fixed)', () => {
    const p = panelPosAfterResize('n', { x: 100, y: 300 }, { w: 480, h: 320 }, { w: 480, h: 420 }, viewport);
    expect(p).toEqual({ x: 100, y: 200 }); // 300 + (320-420): bottom edge stays at 620
  });

  it('moves the popup right when shrinking from the west edge (right edge fixed)', () => {
    const p = panelPosAfterResize('w', { x: 8, y: 50 }, { w: 480, h: 320 }, { w: 320, h: 320 }, viewport);
    expect(p.x).toBe(168); // 8 + (480-320): right edge stays at 488
  });

  it('clamps when growing from the west edge past the on-screen margin', () => {
    const p = panelPosAfterResize('w', { x: 8, y: 50 }, { w: 480, h: 320 }, { w: 640, h: 320 }, viewport);
    expect(p.x).toBe(8); // -60 would leave the screen; stays at the margin
  });

  it('clamps x when the viewport is too small to fit the size at the start pos', () => {
    const p = panelPosAfterResize('e', { x: 1900, y: 50 }, { w: 480, h: 320 }, { w: 480, h: 320 }, viewport);
    expect(p.x).toBe(1920 - 480 - 8);
  });
});
