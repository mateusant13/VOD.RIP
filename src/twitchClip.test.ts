import { describe, expect, it } from 'vitest';
import {
  TWITCH_CLIP_MAX_SEC,
  buildLiveTwitchClipEditorUrl,
  buildTwitchClipEditorUrl,
  twitchClipDurationError,
} from './twitchClip';

describe('buildTwitchClipEditorUrl', () => {
  it('positions the editor on the VOD at the clip end', () => {
    const url = buildTwitchClipEditorUrl({
      vodId: '2536167775',
      broadcasterLogin: 'surtepi',
      offsetSeconds: 434.7,
    });
    expect(url).toBe(
      'https://clips.twitch.tv/create?vodID=2536167775&broadcasterLogin=surtepi&offsetSeconds=434',
    );
  });
});

describe('buildLiveTwitchClipEditorUrl', () => {
  it('targets the live channel without a VOD id', () => {
    expect(buildLiveTwitchClipEditorUrl('surtepi')).toBe(
      'https://clips.twitch.tv/create?broadcasterLogin=surtepi',
    );
  });
});

describe('twitchClipDurationError', () => {
  it('accepts ranges inside the 1..60s window', () => {
    expect(twitchClipDurationError(30)).toBeNull();
    expect(twitchClipDurationError(TWITCH_CLIP_MAX_SEC)).toBeNull();
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
