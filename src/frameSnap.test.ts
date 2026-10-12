import { describe, expect, it } from 'vitest';
import { exploreSnapVideoWidth, frameSnapInnerBox } from './frameSnap';

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
