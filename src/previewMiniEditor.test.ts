import { describe, expect, it } from 'vitest';
import {
  MINI_EDITOR_DEFAULT_LEN_SEC,
  MINI_EDITOR_KEY_STEP_FAST_SEC,
  initialMiniEditorRange,
  miniEditorButtonDelta,
  miniEditorDownloadBody,
  miniEditorDownloadError,
  miniEditorKeyDelta,
  miniEditorRangeFromRail,
  miniEditorRangeLength,
  miniEditorRangeToEdge,
  miniEditorTwitchRangeError,
  miniEditorTwitchTargetError,
  nudgeMiniEditorRange,
} from './previewMiniEditor';
import { TWITCH_CLIP_MAX_SEC, TWITCH_CLIP_MIN_SEC } from './twitchClip';

describe('initialMiniEditorRange', () => {
  it('selects the default length starting at the playhead', () => {
    expect(initialMiniEditorRange(120, 3600)).toEqual({
      start: 120,
      end: 120 + MINI_EDITOR_DEFAULT_LEN_SEC,
    });
  });

  it('pulls the selection back inside the media near the end', () => {
    const r = initialMiniEditorRange(3590, 3600);
    expect(r.end).toBe(3600);
    expect(r.start).toBe(3600 - MINI_EDITOR_DEFAULT_LEN_SEC);
    expect(r.start).toBeGreaterThanOrEqual(0);
  });

  it('degrades to an empty range when the duration is unknown', () => {
    expect(initialMiniEditorRange(10, 0)).toEqual({ start: 0, end: 0 });
  });

  it('never returns an inverted range on a sub-second media', () => {
    const r = initialMiniEditorRange(0.4, 0.5);
    expect(r.end).toBeGreaterThan(r.start);
  });
});

describe('nudgeMiniEditorRange', () => {
  it('extends the cut at the start when in-point moves earlier (+delta)', () => {
    expect(nudgeMiniEditorRange({ start: 100, end: 130 }, 3600, 'in', 5)).toEqual({
      start: 95,
      end: 130,
    });
  });

  it('pins the opposite endpoint', () => {
    expect(nudgeMiniEditorRange({ start: 100, end: 130 }, 3600, 'out', 5)).toEqual({
      start: 100,
      end: 135,
    });
  });

  it('clamps the start at 0 and the end at the duration', () => {
    expect(nudgeMiniEditorRange({ start: 2, end: 30 }, 3600, 'in', 10).start).toBe(0);
    expect(nudgeMiniEditorRange({ start: 10, end: 3598 }, 3600, 'out', 10).end).toBe(3600);
  });

  it('keeps at least one second of cut when a needle is driven into the other', () => {
    // Driving the IN needle FORWARD (negative delta) is what shrinks the cut;
    // the 1s floor is what stops it collapsing through the out-needle.
    const r = nudgeMiniEditorRange({ start: 100, end: 130 }, 3600, 'in', -1000);
    expect(r.start).toBe(129);
    expect(r.end - r.start).toBe(1);
  });

  it('can only grow when the in-needle is driven backwards', () => {
    const r = nudgeMiniEditorRange({ start: 100, end: 130 }, 3600, 'in', 1000);
    expect(r.start).toBe(0);
    expect(r.end).toBe(130);
  });

  it('matches the main window button contract (button -5 extends the start)', () => {
    // Same conversion App.tsx's ClipDurationAdjustButtons does via
    // trimButtonDeltaForEndpoint — a button press and a key press agree.
    expect(miniEditorButtonDelta({ start: 100, end: 130 }, 3600, 'in', -5)).toEqual({
      start: 95,
      end: 130,
    });
    expect(miniEditorButtonDelta({ start: 100, end: 130 }, 3600, 'out', 5)).toEqual({
      start: 100,
      end: 135,
    });
  });
});

describe('miniEditorRangeFromRail', () => {
  it('moves the dragged needle and pins the other end', () => {
    expect(miniEditorRangeFromRail({ start: 100, end: 200 }, 3600, 'in', 40)).toEqual({
      start: 40,
      end: 200,
    });
    expect(miniEditorRangeFromRail({ start: 100, end: 200 }, 3600, 'out', 300)).toEqual({
      start: 100,
      end: 300,
    });
  });

  it('keeps the range valid when the in-needle is dragged past the out-needle', () => {
    const r = miniEditorRangeFromRail({ start: 100, end: 200 }, 3600, 'in', 900);
    expect(r.end - r.start).toBeGreaterThanOrEqual(1);
  });
});

describe('miniEditorRangeToEdge', () => {
  it('snaps the in-needle to 0 and the out-needle to the duration', () => {
    expect(miniEditorRangeToEdge({ start: 100, end: 200 }, 3600, 'in', 'start').start).toBe(0);
    expect(miniEditorRangeToEdge({ start: 100, end: 200 }, 3600, 'out', 'end').end).toBe(3600);
  });
});

describe('miniEditorKeyDelta', () => {
  it('maps ArrowLeft on the in-needle to a positive delta (widens leftwards)', () => {
    expect(miniEditorKeyDelta({ key: 'ArrowLeft' }, 'in')).toEqual({ deltaSec: 1 });
  });

  it('maps ArrowRight on the out-needle to a positive delta', () => {
    expect(miniEditorKeyDelta({ key: 'ArrowRight' }, 'out')).toEqual({ deltaSec: 1 });
  });

  it('narrows the cut when the in-needle moves right', () => {
    expect(miniEditorKeyDelta({ key: 'ArrowRight' }, 'in')).toEqual({ deltaSec: -1 });
  });

  it('uses the fast step with Shift', () => {
    expect(miniEditorKeyDelta({ key: 'ArrowLeft', shiftKey: true }, 'in')).toEqual({
      deltaSec: MINI_EDITOR_KEY_STEP_FAST_SEC,
    });
  });

  it('maps Home/End to the media bounds and ignores other keys', () => {
    expect(miniEditorKeyDelta({ key: 'Home' }, 'in')).toEqual({ edge: 'start' });
    expect(miniEditorKeyDelta({ key: 'End' }, 'out')).toEqual({ edge: 'end' });
    expect(miniEditorKeyDelta({ key: 'a' }, 'in')).toBeNull();
  });
});

describe('miniEditorRangeLength', () => {
  it('never returns a negative length', () => {
    expect(miniEditorRangeLength({ start: 100, end: 40 })).toBe(0);
  });
});

describe('miniEditorDownloadError', () => {
  it('blocks an empty range', () => {
    expect(miniEditorDownloadError({ start: 0, end: 0 }, 0)).toMatch(/duration unknown/i);
    expect(miniEditorDownloadError({ start: 100, end: 100 }, 3600)).toMatch(/range/i);
  });

  it('allows any positive length (a local cut is not bound to Twitch limits)', () => {
    expect(miniEditorDownloadError({ start: 0, end: 600 }, 3600)).toBeNull();
    expect(miniEditorDownloadError({ start: 0, end: 2 }, 3600)).toBeNull();
  });
});

describe('miniEditorTwitchRangeError', () => {
  it('reuses twitchClip bounds rather than restating them', () => {
    expect(miniEditorTwitchRangeError({ start: 0, end: TWITCH_CLIP_MIN_SEC - 1 })).toMatch(/at least/i);
    expect(miniEditorTwitchRangeError({ start: 0, end: TWITCH_CLIP_MAX_SEC + 1 })).toMatch(/or less/i);
    expect(miniEditorTwitchRangeError({ start: 0, end: TWITCH_CLIP_MIN_SEC })).toBeNull();
    expect(miniEditorTwitchRangeError({ start: 0, end: TWITCH_CLIP_MAX_SEC })).toBeNull();
  });
});

describe('miniEditorTwitchTargetError', () => {
  it('requires a Twitch VOD, a broadcaster login and a video id', () => {
    expect(
      miniEditorTwitchTargetError({ platform: 'youtube', channel: 'x', videoId: '1' }),
    ).toMatch(/Twitch VOD/);
    expect(
      miniEditorTwitchTargetError({ platform: 'twitch', channel: null, videoId: '1' }),
    ).toMatch(/Channel login/);
    expect(
      miniEditorTwitchTargetError({ platform: 'twitch', channel: 'x', videoId: null }),
    ).toMatch(/Twitch VOD URL/);
    expect(
      miniEditorTwitchTargetError({ platform: 'twitch', channel: 'x', videoId: '1' }),
    ).toBeNull();
  });
});

describe('miniEditorDownloadBody', () => {
  it('always sends crop_start AND crop_end so the SRT can be rebased', () => {
    const body = miniEditorDownloadBody({
      url: 'https://www.twitch.tv/videos/123',
      range: { start: 410.4, end: 470.9 },
    });
    expect(body.crop_start).toBe(410);
    expect(body.crop_end).toBe(471);
    expect(body.include_transcript).toBe(true);
  });

  it('never emits a half-open crop window', () => {
    const body = miniEditorDownloadBody({
      url: 'u',
      range: { start: 100, end: 100 },
    });
    expect(body.crop_end as number).toBeGreaterThan(body.crop_start as number);
  });

  it('clamps a negative start to 0', () => {
    const body = miniEditorDownloadBody({ url: 'u', range: { start: -50, end: 30 } });
    expect(body.crop_start).toBe(0);
    expect(body.crop_end).toBe(30);
  });

  it('omits the chat fields unless chat markers were actually set', () => {
    const plain = miniEditorDownloadBody({ url: 'u', range: { start: 0, end: 10 } });
    expect(plain).not.toHaveProperty('include_chat');
    expect(plain).not.toHaveProperty('chat_start_sec');

    const withChat = miniEditorDownloadBody({
      url: 'u',
      range: { start: 0, end: 10 },
      includeChat: true,
      chatStartSec: 12,
      chatEndSec: null,
    });
    expect(withChat.include_chat).toBe(true);
    expect(withChat.chat_start_sec).toBe(12);
    expect(withChat).not.toHaveProperty('chat_end_sec');
  });

  it('carries title/channel/duration when known', () => {
    const body = miniEditorDownloadBody({
      url: 'u',
      range: { start: 0, end: 30 },
      title: 'T',
      channel: 'c',
      durationSec: 3600,
    });
    expect(body.title).toBe('T');
    expect(body.channel).toBe('c');
    expect(body.duration).toBe(3600);
  });
});
