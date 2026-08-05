/**
 * Unit tests for trimUtils.ts — trim/range helpers + rail zoom window math.
 */
import { describe, it, expect } from 'vitest';
import {
  clampTrimEndpoints,
  trimButtonDeltaForEndpoint,
  adjustTrimEndpointByDelta,
  zoomWindowFromView,
  fracToSec,
  secToFrac,
  zoomTrimViewAround,
  clampTrimZoom,
  TRIM_ZOOM_MIN,
  TRIM_ZOOM_MAX,
} from './trimUtils';

describe('clampTrimEndpoints', () => {
  it('clamps start < end normally', () => {
    const result = clampTrimEndpoints(10, 100, 200, 0, 3600);
    expect(result.start).toBe(10);
    expect(result.end).toBe(100);
  });

  it('ensures start < end by adjusting start backward when equal', () => {
    // start === end with no opts — else branch sets start = Math.max(0, end - 1)
    const result = clampTrimEndpoints(50, 50, 200, 0, 3600);
    expect(result.start).toBe(49);
    expect(result.end).toBe(50);
  });

  it('clamps to duration bounds', () => {
    const result = clampTrimEndpoints(-10, 500, 100, 0, 3600);
    expect(result.start).toBe(0);
    expect(result.end).toBe(100);
  });

  it('uses opts.move=in to pin end', () => {
    const result = clampTrimEndpoints(
      5, 50, 200, 10, 100,
      { move: 'in', fixedEnd: 80 },
    );
    expect(result.end).toBe(80);
    expect(result.start).toBe(5);
  });

  it('uses opts.move=out to pin start', () => {
    const result = clampTrimEndpoints(
      10, 150, 200, 10, 100,
      { move: 'out', fixedStart: 20 },
    );
    expect(result.start).toBe(20);
    expect(result.end).toBe(150);
  });
});

describe('trimButtonDeltaForEndpoint', () => {
  it('negates delta for "in" endpoint', () => {
    expect(trimButtonDeltaForEndpoint('in', 5)).toBe(-5);
    expect(trimButtonDeltaForEndpoint('in', -3)).toBe(3);
  });

  it('passes through delta for "out" endpoint', () => {
    expect(trimButtonDeltaForEndpoint('out', 5)).toBe(5);
    expect(trimButtonDeltaForEndpoint('out', -3)).toBe(-3);
  });
});

describe('adjustTrimEndpointByDelta', () => {
  it('adjusts "in" endpoint backward (extending clip earlier)', () => {
    const result = adjustTrimEndpointByDelta(30, 60, 200, 'in', 10);
    expect(result.start).toBe(20);
    expect(result.end).toBe(60);
  });

  it('adjusts "out" endpoint forward (extending clip later)', () => {
    const result = adjustTrimEndpointByDelta(30, 60, 200, 'out', 10);
    expect(result.start).toBe(30);
    expect(result.end).toBe(70);
  });

  it('ensures minimum 1s length', () => {
    // delta=100 moves start backward to 0 (clamped), end stays at 31
    const result = adjustTrimEndpointByDelta(30, 31, 200, 'in', 100);
    expect(result.start).toBe(0);
    expect(result.end).toBe(31);
    expect(result.start).toBeLessThan(result.end);
  });

  it('clamps to duration', () => {
    const result = adjustTrimEndpointByDelta(190, 195, 200, 'out', 20);
    expect(result.end).toBe(200);
  });

  it('clamps start to 0', () => {
    const result = adjustTrimEndpointByDelta(5, 30, 200, 'in', 10);
    expect(result.start).toBe(0);
  });
});

describe('clampTrimZoom', () => {
  it('keeps in-range zoom unchanged', () => {
    expect(clampTrimZoom(8)).toBe(8);
    expect(clampTrimZoom(1)).toBe(1);
    expect(clampTrimZoom(64)).toBe(64);
  });

  it('clamps to the supported range', () => {
    expect(clampTrimZoom(0.1)).toBe(TRIM_ZOOM_MIN);
    expect(clampTrimZoom(500)).toBe(TRIM_ZOOM_MAX);
    expect(clampTrimZoom(Number.NaN)).toBe(TRIM_ZOOM_MIN);
    expect(clampTrimZoom(Number.POSITIVE_INFINITY)).toBe(TRIM_ZOOM_MAX);
  });
});

describe('zoomWindowFromView', () => {
  it('zoom=1 shows the full duration (pixel-identical default)', () => {
    expect(zoomWindowFromView(1, 0.5, 7200)).toEqual({ start: 0, end: 7200 });
    expect(zoomWindowFromView(1, 0, 7200)).toEqual({ start: 0, end: 7200 });
    expect(zoomWindowFromView(1, 1, 7200)).toEqual({ start: 0, end: 7200 });
  });

  it('centres the window on the anchor fraction', () => {
    expect(zoomWindowFromView(4, 0.5, 7200)).toEqual({ start: 2700, end: 4500 });
    // anchor 0.25 → window centre at 1800s: [0, 3600]
    expect(zoomWindowFromView(2, 0.25, 7200)).toEqual({ start: 0, end: 3600 });
    expect(zoomWindowFromView(4, 0.75, 7200)).toEqual({ start: 4500, end: 6300 });
  });

  it('clamps the window to the duration bounds', () => {
    expect(zoomWindowFromView(4, 0, 7200)).toEqual({ start: 0, end: 1800 });
    expect(zoomWindowFromView(4, 1, 7200)).toEqual({ start: 5400, end: 7200 });
  });

  it('clamps zoom into [1, 64]', () => {
    expect(zoomWindowFromView(0.1, 0.5, 7200)).toEqual({ start: 0, end: 7200 });
    const w = zoomWindowFromView(999, 0.5, 7200);
    expect(w.end - w.start).toBeCloseTo(7200 / 64, 6);
  });

  it('handles zero duration', () => {
    expect(zoomWindowFromView(8, 0.5, 0)).toEqual({ start: 0, end: 0 });
  });
});

describe('fracToSec / secToFrac', () => {
  const view = { start: 2700, end: 4500 };

  it('maps rail fractions into the zoomed window', () => {
    expect(fracToSec(0, view)).toBe(2700);
    expect(fracToSec(0.5, view)).toBe(3600);
    expect(fracToSec(1, view)).toBe(4500);
  });

  it('maps seconds to clamped rail fractions', () => {
    expect(secToFrac(2700, view)).toBe(0);
    expect(secToFrac(3600, view)).toBe(0.5);
    expect(secToFrac(4500, view)).toBe(1);
    // Outside the window clamps to the rail edges.
    expect(secToFrac(0, view)).toBe(0);
    expect(secToFrac(9999, view)).toBe(1);
  });

  it('round-trips within the window', () => {
    for (const sec of [2700, 3600, 4500]) {
      expect(fracToSec(secToFrac(sec, view), view)).toBeCloseTo(sec, 6);
    }
  });

  it('guards a zero-width window', () => {
    expect(secToFrac(100, { start: 50, end: 50 })).toBe(0);
  });
});

describe('zoomTrimViewAround', () => {
  it('zoom in around the cursor keeps the cursor second under the cursor', () => {
    const view = { start: 0, end: 1000 };
    const { zoom, anchorFrac } = zoomTrimViewAround(view, 0.75, 1.25, 1000);
    expect(zoom).toBeCloseTo(1.25, 6);
    const next = zoomWindowFromView(zoom, anchorFrac, 1000);
    expect(fracToSec(0.75, next)).toBeCloseTo(750, 6);
  });

  it('zoom in at the rail centre keeps the centre second', () => {
    const { zoom, anchorFrac } = zoomTrimViewAround({ start: 0, end: 1000 }, 0.5, 1.25, 1000);
    expect(zoom).toBeCloseTo(1.25, 6);
    expect(anchorFrac).toBeCloseTo(0.5, 6);
    const next = zoomWindowFromView(zoom, anchorFrac, 1000);
    expect(next.start).toBeCloseTo(100, 6);
    expect(next.end).toBeCloseTo(900, 6);
  });

  it('zoom out from the full view stays at zoom 1', () => {
    const { zoom } = zoomTrimViewAround({ start: 0, end: 1000 }, 0.5, 0.8, 1000);
    expect(zoom).toBe(1);
  });

  it('zooming in at the left edge pins the window start to 0', () => {
    const { zoom, anchorFrac } = zoomTrimViewAround({ start: 0, end: 1000 }, 0, 4, 1000);
    const next = zoomWindowFromView(zoom, anchorFrac, 1000);
    expect(next.start).toBe(0);
  });

  it('zooming in at the right edge pins the window end to the duration', () => {
    const { zoom, anchorFrac } = zoomTrimViewAround({ start: 0, end: 1000 }, 1, 4, 1000);
    const next = zoomWindowFromView(zoom, anchorFrac, 1000);
    expect(next.end).toBe(1000);
  });

  it('keeps zooming at an already-zoomed window anchored at the cursor', () => {
    // First zoom around 0.75 → window [150, 950]; zoom again around 0.75.
    const first = zoomTrimViewAround({ start: 0, end: 1000 }, 0.75, 1.25, 1000);
    const v1 = zoomWindowFromView(first.zoom, first.anchorFrac, 1000);
    const second = zoomTrimViewAround(v1, 0.75, 1.25, 1000);
    const v2 = zoomWindowFromView(second.zoom, second.anchorFrac, 1000);
    expect(second.zoom).toBeCloseTo(1.25 * 1.25, 6);
    expect(fracToSec(0.75, v2)).toBeCloseTo(750, 6);
  });

  it('handles zero duration', () => {
    expect(zoomTrimViewAround({ start: 0, end: 0 }, 0.5, 1.25, 0)).toEqual({
      zoom: TRIM_ZOOM_MIN,
      anchorFrac: 0.5,
    });
  });
});
