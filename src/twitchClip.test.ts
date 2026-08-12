import { describe, expect, it, vi } from 'vitest';
import {
  TWITCH_CLIP_MAX_SEC,
  TWITCH_CLIP_MIN_SEC,
  TWITCH_CLIP_WINDOW_SEC,
  clampClipSelection,
  clipRailDragTarget,
  initialClipSelection,
  openTwitchClipEditorInBrowser,
  twitchClipDurationError,
  twitchClipDownloadRequest,
  twitchClipWindow,
  vodRangeFromEditorWindow,
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
  it('is 120s (60s each side of the click)', () => {
    expect(TWITCH_CLIP_WINDOW_SEC).toBe(120);
    expect(twitchClipWindow(3600, 7200)).toEqual({ start: 3540, end: 3660 });
  });

  it('fills from the right when the click is near VOD start', () => {
    expect(twitchClipWindow(10, 7200)).toEqual({ start: 0, end: 120 });
  });

  it('fills from the left when the click is near VOD end', () => {
    expect(twitchClipWindow(7190, 7200)).toEqual({ start: 7080, end: 7200 });
  });

  it('shortens the window for VODs under 120s', () => {
    expect(twitchClipWindow(30, 100)).toEqual({ start: 0, end: 100 });
    expect(twitchClipWindow(50, 60)).toEqual({ start: 0, end: 60 });
  });

  it('keeps the upper edge unclamped when the duration is unknown', () => {
    expect(twitchClipWindow(100, 0)).toEqual({ start: 40, end: 160 });
  });

  it('ignores an anchor and stays on the click', () => {
    expect(twitchClipWindow(3600, 7200, { start: 300, end: 360 })).toEqual({ start: 3540, end: 3660 });
    expect(twitchClipWindow(3600, 7200, { start: 10, end: 50 })).toEqual({ start: 3540, end: 3660 });
  });
});

describe('initialClipSelection', () => {
  it('defaults to the first 60s of the window without an anchor', () => {
    expect(initialClipSelection({ start: 0, end: 120 })).toEqual({ start: 0, end: 60 });
  });

  it('defaults to the whole window when shorter than 60s', () => {
    expect(initialClipSelection({ start: 0, end: 30 })).toEqual({ start: 0, end: 30 });
  });

  it('starts at the click and extends forward up to 60s', () => {
    expect(initialClipSelection({ start: 3540, end: 3660 }, undefined, 3600))
      .toEqual({ start: 3600, end: 3660 });
  });

  it('grows backward when there is not enough room after the click', () => {
    expect(initialClipSelection({ start: 3540, end: 3660 }, undefined, 3658))
      .toEqual({ start: 3655, end: 3660 });
  });

  it('returns a short in-window anchor as-is', () => {
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 300, end: 360 }))
      .toEqual({ start: 300, end: 360 });
  });

  it('ignores a full-VOD / over-long anchor and uses the playhead', () => {
    expect(initialClipSelection({ start: 3540, end: 3660 }, { start: 0, end: 3600 }, 3600))
      .toEqual({ start: 3600, end: 3660 });
    expect(initialClipSelection({ start: 0, end: 7200 }, { start: 1000, end: 1100 }, 3600))
      .toEqual({ start: 3600, end: 3660 });
  });

  it('clamps a short anchor that sits before the window', () => {
    expect(initialClipSelection({ start: 270, end: 390 }, { start: 100, end: 160 }))
      .toEqual({ start: 270, end: 275 });
  });
});

describe('clipRailDragTarget', () => {
  it('picks the playhead when the pointer is near it', () => {
    expect(clipRailDragTarget(50, 100, 50, 40, 80)).toBe('playhead');
    expect(clipRailDragTarget(50 + 12, 100, 50, 40, 80)).toBe('playhead');
  });

  it('picks the range inside the slider away from the playhead', () => {
    expect(clipRailDragTarget(70, 100, 50, 40, 80)).toBe('range');
  });

  it('seeks on empty rail', () => {
    expect(clipRailDragTarget(10, 100, 50, 40, 80)).toBe('seek');
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


describe('vodRangeFromEditorWindow', () => {
  it('maps a relative 0-90 editor window back through the VOD end anchor', () => {
    expect(vodRangeFromEditorWindow({ start: 871, end: 890 }, { start: 71, end: 90 }))
      .toEqual({ start: 871, end: 890 });
    expect(vodRangeFromEditorWindow({ start: 1054, end: 1066 }, { start: 78, end: 90 }))
      .toEqual({ start: 1054, end: 1066 });
  });
  it('keeps an already-absolute editor window', () => {
    expect(vodRangeFromEditorWindow({ start: 871, end: 890 }, { start: 871, end: 890 }))
      .toEqual({ start: 871, end: 890 });
  });
  it('does not treat a short early-VOD range as relative', () => {
    expect(vodRangeFromEditorWindow({ start: 10, end: 29 }, { start: 10, end: 29 }))
      .toEqual({ start: 10, end: 29 });
  });
});

describe('twitchClipDownloadRequest', () => {
  const base = {
    id: '1',
    created_at: '2026-08-12T00:00:00Z',
    channel: 'relentless',
    title: 'clip',
    url: 'https://clips.twitch.tv/example',
    status: 'ready',
    vod_id: '2844207886',
    offset_sec: 890,
    duration_sec: 19,
  };
  it('crops the original VOD using offset_sec as the clip END', () => {
    expect(twitchClipDownloadRequest(base)).toEqual({
      url: 'https://www.twitch.tv/videos/2844207886',
      quality: 'source',
      title: 'clip',
      channel: 'relentless',
      duration: 19,
      crop_start: 871,
      crop_end: 890,
    });
  });
  it('falls back to the clip URL when the VOD range is missing', () => {
    expect(twitchClipDownloadRequest({ ...base, vod_id: null, offset_sec: null })).toEqual({
      url: 'https://clips.twitch.tv/example',
      quality: 'source',
      title: 'clip',
      channel: 'relentless',
      duration: 19,
    });
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
      openTwitchClipEditorInBrowser('2832716983', 'titiltei', 458, 520, 'jantando o guiven parte 1', 3600);
      expect(opened).toHaveLength(1);
      const u = new URL(opened[0]);
      expect(u.host).toBe('clips.twitch.tv');
      expect(u.pathname).toBe('/create');
      expect(u.searchParams.get('vodID')).toBe('2832716983');
      expect(u.searchParams.get('broadcasterLogin')).toBe('titiltei');
      expect(u.searchParams.get('offsetSeconds')).toBe('520'); // clip END, not start
      expect(u.searchParams.get('vodrip_clip')).toBe('1');
      expect(u.searchParams.get('vodrip_start')).toBe('460'); // 62s range clamped to 60s, END kept
      expect(u.searchParams.get('vodrip_end')).toBe('520');
      expect(u.searchParams.get('vodrip_dur')).toBe('3600'); // VOD length for the editor-edge nudge
      expect(u.searchParams.get('vodrip_title')).toBe('jantando o guiven parte 1');
      // Browser path is the user's explicit choice — the Twitch tab stays
      // open after the flow (the extension's closeAfterFlow honors this).
      expect(u.searchParams.get('vodrip_close')).toBe('0');
    } finally {
      vi.restoreAllMocks();
    }
  });

  it('rejects a missing original VOD title instead of inventing a custom one', () => {
    const opened: string[] = [];
    vi.spyOn(window, 'open').mockImplementation((url?: string | URL) => {
      opened.push(String(url ?? ''));
      return null;
    });
    try {
      expect(() => openTwitchClipEditorInBrowser('2832716983', 'titiltei', 458, 520, '   ')).toThrow(
        'Original VOD title is required',
      );
      expect(opened).toHaveLength(0);
    } finally {
      vi.restoreAllMocks();
    }
  });
});
