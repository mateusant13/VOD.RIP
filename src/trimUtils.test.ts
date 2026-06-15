/**
 * Unit tests for trimUtils.ts — trim/range helpers.
 */
import { describe, it, expect } from 'vitest';
import {
  clampTrimEndpoints,
  trimButtonDeltaForEndpoint,
  adjustTrimEndpointByDelta,
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
