import { describe, expect, it } from 'vitest';
import {
  exploreSnapVideoWidth,
  frameDropGhost,
  frameSnapInnerBox,
} from './frameSnap';

describe('exploreSnapVideoWidth - the width a snapped panel may ask for', () => {
  it('gives the video the whole inner width when no chat is open', () => {
    expect(exploreSnapVideoWidth(400, 0)).toBe(400);
  });

  it('subtracts the chat column from the inner width', () => {
    expect(exploreSnapVideoWidth(400, 320)).toBe(80);
  });

  it('clamps chat to the cell rather than asking for a negative video', () => {
    // A cell narrower than the chat column must not produce a negative width:
    // that is the desync between panelWidthRef and its React-state consumer.
    expect(exploreSnapVideoWidth(300, 500)).toBe(1);
    expect(exploreSnapVideoWidth(300, 300)).toBe(1);
  });

  it('never returns less than one pixel', () => {
    expect(exploreSnapVideoWidth(0, 0)).toBe(1);
    expect(exploreSnapVideoWidth(-10, -10)).toBe(1);
  });

  it('agrees with the inner box the pin writes (no overflow)', () => {
    const inner = frameSnapInnerBox({ x: 0, y: 0, w: 480, h: 720 });
    const video = exploreSnapVideoWidth(inner.w, 320);
    expect(video + 320).toBeLessThanOrEqual(inner.w);
  });
});


describe('frameDropGhost — where the held window will land', () => {
  // A cell 600x300 with the 6px inner pad => inner 588x288 at (6,6).
  const cell = { x: 100, y: 50, w: 600, h: 300 };

  it('centres a window smaller than the cell, not stretched to fill it', () => {
    const g = frameDropGhost(cell, { w: 300, h: 200 });
    expect(g.w).toBe(300);
    expect(g.h).toBe(200);
    expect(g.x).toBe(100 + 6 + (588 - 300) / 2);
    expect(g.y).toBe(50 + 6 + (288 - 200) / 2);
  });

  it("clamps a window WIDER than the cell to its inner box", () => {
    // The "buttons end up outside the div" failure in miniature: a ghost that
    // drew the window at its own width would promise a result the cell cannot
    // hold.
    const g = frameDropGhost(cell, { w: 4000, h: 9000 });
    expect(g.w).toBe(588);
    expect(g.h).toBe(288);
    expect(g.x).toBe(106);
    expect(g.y).toBe(56);
  });

  it("falls back to filling the cell when the size is unmeasurable", () => {
    // offsetWidth is 0 on a window the browser has not laid out yet. Drawing a
    // degenerate 0x0 ghost is worse than promising the cell.
    for (const bad of [null, undefined, { w: 0, h: 0 }]) {
      const g = frameDropGhost(cell, bad);
      expect(g.w).toBe(588);
      expect(g.h).toBe(288);
    }
  });
});