import { describe, expect, it } from 'vitest';
import { FRAME_GRID_PADDING, getFrameCellRect } from './frameLayout';

describe('frameLayout', () => {
  it('computes non-overlapping cells for 3-column grid', () => {
    const a = getFrameCellRect(0, 1200, 800);
    const b = getFrameCellRect(1, 1200, 800);
    expect(a.x).toBe(FRAME_GRID_PADDING);
    expect(b.x).toBeGreaterThan(a.x);
    expect(a.w).toBeGreaterThan(100);
  });
});
