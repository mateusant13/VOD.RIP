import { describe, expect, it, vi } from 'vitest';
import {
  TWITCH_CLIP_MAX_SEC,
  TWITCH_CLIP_MIN_SEC,
  clampClipSelection,
  clipEditorOffsetAndDuration,
  openTwitchClipEditorInBrowser,
  twitchClipDurationError,
  twitchClipWindow,
} from './twitchClip';

describe('twitchClipDurationError', () => {
  it('accepts ranges inside the 5..60s window', () => {
    expect(twitchClipDurationError(TWITCH_CLIP_MIN_SEC)).toBeNull();
    expect(twitchClipDurationError(30)).toBeNull();
    expect(twitchClipDurationError(TWITCH_CLIP_MAX_SEC)).toBeNull();
  });
  it('rejects selections shorter than 5s', () => {
    expect(twitchClipDurationError(4)).toContain('at least 5s');
    expect(twitchClipDurationError(1)).toContain('at least 5s');
  });
  it('rejects selections longer than 60s', () => {
    expect(twitchClipDurationError(61)).toContain('60s or less');
    expect(twitchClipDurationError(120)).toContain('60s or less');
  });
  it('rejects an empty/invalid range', () => {
    expect(twitchClipDurationError(0)).toMatch(/select a clip range/i);
    expect(twitchClipDurationError(-5)).toMatch(/select a clip range/i);
    expect(twitchClipDurationError(Number.NaN)).toMatch(/select a clip range/i);
  });
});

describe('twitchClipWindow', () => {
  it('centres a ±60s window on the playhead', () => {
    expect(twitchClipWindow(3600, 7200)).toEqual({ start: 3540, end: 3660 });
  });

  it('clamps the start at the VOD start edge', () => {
    expect(twitchClipWindow(10, 7200)).toEqual({ start: 0, end: 70 });
  });

  it('clamps the end at the VOD end edge', () => {
    expect(twitchClipWindow(7190, 7200)).toEqual({ start: 7130, end: 7200 });
  });

  it('shortens the window for VODs under 120s', () => {
    expect(twitchClipWindow(30, 100)).toEqual({ start: 0, end: 90 });
    expect(twitchClipWindow(50, 60)).toEqual({ start: 0, end: 60 });
  });

  it('keeps the upper edge unclamped when the duration is unknown', () => {
    expect(twitchClipWindow(100, 0)).toEqual({ start: 40, end: 160 });
  });
});

describe('clampClipSelection', () => {
  it('keeps an in-range free-form selection', () => {
    expect(clampClipSelection(10, 40, 0, 120)).toEqual({ start: 10, end: 40 });
  });

  it('enforces the 5s minimum by extending the end', () => {
    expect(clampClipSelection(10, 12, 0, 120)).toEqual({ start: 10, end: 15 });
  });

  it('keeps an over-long free-form range (only the 5s floor applies here)', () => {
    // The mini popup initialises with the full window (up to 120s); the 60s
    // limit is enforced by the move branches and the Create action instead.
    expect(clampClipSelection(10, 120, 0, 120)).toEqual({ start: 10, end: 120 });
    expect(clampClipSelection(0, 120, 0, 120)).toEqual({ start: 0, end: 120 });
  });

  it('clamps into the window bounds', () => {
    expect(clampClipSelection(-50, 200, 0, 120)).toEqual({ start: 0, end: 120 });
    expect(clampClipSelection(-50, 10, 0, 120)).toEqual({ start: 0, end: 10 });
  });

  it('pins the end when dragging the in handle (move=in)', () => {
    const res = clampClipSelection(55, 80, 0, 120, { move: 'in', fixedEnd: 80 });
    expect(res.end).toBe(80);
    expect(res.start).toBe(55);
    // cannot drag past the 60s max
    expect(clampClipSelection(0, 80, 0, 120, { move: 'in', fixedEnd: 80 }).start).toBe(20);
    // cannot drag past the pinned end minus the 5s min
    expect(clampClipSelection(90, 80, 0, 120, { move: 'in', fixedEnd: 80 }).start).toBe(75);
  });

  it('pins the start when dragging the out handle (move=out)', () => {
    const res = clampClipSelection(20, 55, 0, 120, { move: 'out', fixedStart: 20 });
    expect(res.start).toBe(20);
    expect(res.end).toBe(55);
    expect(clampClipSelection(20, 200, 0, 120, { move: 'out', fixedStart: 20 }).end).toBe(80);
    expect(clampClipSelection(20, 21, 0, 120, { move: 'out', fixedStart: 20 }).end).toBe(25);
  });

  it('collapses to the whole window when the window is under 5s', () => {
    expect(clampClipSelection(0, 4, 0, 4)).toEqual({ start: 0, end: 4 });
    expect(clampClipSelection(1, 3, 0, 4, { move: 'in', fixedEnd: 4 })).toEqual({ start: 0, end: 4 });
  });

  it('caps the max selection at the window length for short windows', () => {
    // 30s window: max 30, not 60
    expect(clampClipSelection(0, 30, 0, 30)).toEqual({ start: 0, end: 30 });
    expect(clampClipSelection(-10, 50, 0, 30)).toEqual({ start: 0, end: 30 });
  });
});

describe('clipEditorOffsetAndDuration', () => {
  it('maps selection end → offset (END reference) and length → duration', () => {
    expect(clipEditorOffsetAndDuration(100, 160)).toEqual({ offsetSec: 160, durationSec: 60 });
  });

  it('floors the offset and rounds the duration', () => {
    expect(clipEditorOffsetAndDuration(99.4, 160.7)).toEqual({ offsetSec: 160, durationSec: 61 });
  });

  it('never produces a negative offset', () => {
    expect(clipEditorOffsetAndDuration(0, 0)).toEqual({ offsetSec: 0, durationSec: 0 });
  });
});

describe('openTwitchClipEditorInBrowser', () => {
  it('opens the legacy editor URL with vodrip_* params (offset = clip END)', () => {
    const opened: string[] = [];
    vi.spyOn(window, 'open').mockImplementation((url?: string | URL) => {
      opened.push(String(url ?? ''));
      return null;
    });
    try {
      openTwitchClipEditorInBrowser('2832716983', 'titiltei', 458, 520, 'Teste VOD.RIP');
      expect(opened).toHaveLength(1);
      const u = new URL(opened[0]);
      expect(u.host).toBe('clips.twitch.tv');
      expect(u.pathname).toBe('/create');
      expect(u.searchParams.get('vodID')).toBe('2832716983');
      expect(u.searchParams.get('broadcasterLogin')).toBe('titiltei');
      expect(u.searchParams.get('offsetSeconds')).toBe('520'); // clip END, not start
      expect(u.searchParams.get('vodrip_clip')).toBe('1');
      expect(u.searchParams.get('vodrip_start')).toBe('458');
      expect(u.searchParams.get('vodrip_end')).toBe('520');
      expect(u.searchParams.get('vodrip_title')).toBe('Teste VOD.RIP');
    } finally {
      vi.restoreAllMocks();
    }
  });

  it('omits vodrip_title when no title is given', () => {
    const opened: string[] = [];
    vi.spyOn(window, 'open').mockImplementation((url?: string | URL) => {
      opened.push(String(url ?? ''));
      return null;
    });
    try {
      openTwitchClipEditorInBrowser('2832716983', 'titiltei', 458, 520);
      const u = new URL(opened[0]);
      expect(u.searchParams.has('vodrip_title')).toBe(false);
      expect(u.searchParams.get('offsetSeconds')).toBe('520');
    } finally {
      vi.restoreAllMocks();
    }
  });
});
