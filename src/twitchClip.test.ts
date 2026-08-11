import { describe, expect, it, vi } from 'vitest';
import {
  TWITCH_CLIP_MAX_SEC,
  TWITCH_CLIP_MIN_SEC,
  clampClipSelection,
  initialClipSelection,
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
  it('centres a 90s (1:30) window on the playhead', () => {
    expect(twitchClipWindow(3600, 7200)).toEqual({ start: 3555, end: 3645 });
  });

  it('clamps the start at the VOD start edge', () => {
    expect(twitchClipWindow(10, 7200)).toEqual({ start: 0, end: 90 });
  });

  it('clamps the end at the VOD end edge', () => {
    expect(twitchClipWindow(7190, 7200)).toEqual({ start: 7145, end: 7200 });
  });

  it('shortens the window for VODs under 90s', () => {
    expect(twitchClipWindow(30, 100)).toEqual({ start: 0, end: 90 });
    expect(twitchClipWindow(50, 60)).toEqual({ start: 5, end: 60 });
  });

  it('keeps the upper edge unclamped when the duration is unknown', () => {
    expect(twitchClipWindow(100, 0)).toEqual({ start: 55, end: 145 });
  });

  it('centres on the anchor midpoint when a valid anchor is given', () => {
    // anchor 300..360 → midpoint 330 → 90s window 285..375
    expect(twitchClipWindow(0, 7200, { start: 300, end: 360 })).toEqual({ start: 285, end: 375 });
  });

  it('ignores an invalid anchor and centres on the playhead', () => {
    expect(twitchClipWindow(3600, 7200, { start: 5000, end: 4000 })).toEqual({ start: 3555, end: 3645 });
    expect(twitchClipWindow(3600, 7200, { start: 0, end: 0 })).toEqual({ start: 3555, end: 3645 });
  });

  it('clamps an anchored window at the VOD start edge', () => {
    expect(twitchClipWindow(3600, 7200, { start: 10, end: 50 })).toEqual({ start: 0, end: 90 });
  });
});

describe('initialClipSelection', () => {
  it('defaults to the last 60s of the window without an anchor', () => {
    expect(initialClipSelection({ start: 0, end: 120 })).toEqual({ start: 60, end: 120 });
  });

  it('defaults to the whole window when shorter than 60s', () => {
    expect(initialClipSelection({ start: 0, end: 30 })).toEqual({ start: 0, end: 30 });
  });

  it('returns the anchor as-is when it fits the window', () => {
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 300, end: 360 }))
      .toEqual({ start: 300, end: 360 });
  });

  it('keeps the END anchored when the anchor is longer than 60s', () => {
    expect(initialClipSelection({ start: 0, end: 7200 }, { start: 1000, end: 1100 }))
      .toEqual({ start: 1040, end: 1100 });
  });

  it('grows an under-5s anchor to the minimum from its start', () => {
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 300, end: 302 }))
      .toEqual({ start: 300, end: 305 });
    // at the window end the start is pulled back instead
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 388, end: 389 }))
      .toEqual({ start: 385, end: 390 });
  });

  it('clamps an anchor that extends past the window edge', () => {
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 300, end: 500 }))
      .toEqual({ start: 330, end: 390 });
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 100, end: 300 }))
      .toEqual({ start: 270, end: 300 });
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

describe('openTwitchClipEditorInBrowser', () => {
  it('opens the legacy editor URL with vodrip_* params (offset = clip END)', () => {
    const opened: string[] = [];
    vi.spyOn(window, 'open').mockImplementation((url?: string | URL) => {
      opened.push(String(url ?? ''));
      return null;
    });
    try {
      openTwitchClipEditorInBrowser('2832716983', 'titiltei', 458, 520, 'Teste VOD.RIP', 3600);
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
      expect(u.searchParams.get('vodrip_dur')).toBe('3600'); // VOD length for the editor-edge nudge
      expect(u.searchParams.get('vodrip_title')).toBe('Teste VOD.RIP');
      // Browser path is the user's explicit choice — the Twitch tab stays
      // open after the flow (the extension's closeAfterFlow honors this).
      expect(u.searchParams.get('vodrip_close')).toBe('0');
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
      expect(u.searchParams.has('vodrip_dur')).toBe(false);
      expect(u.searchParams.get('offsetSeconds')).toBe('520');
    } finally {
      vi.restoreAllMocks();
    }
  });
});
