import { describe, expect, it } from 'vitest';
import {
  previewRetryAfterError,
  previewRetryMode,
  type PreviewRetryState,
} from './previewRetry';

describe('previewRetry state machine', () => {
  const ctx: PreviewRetryState = {
    url: 'https://kick.com/video/123',
    stage: 'session',
    attempts: 0,
  };

  it('first retry is stage-only; every later click is full pipeline', () => {
    expect(previewRetryMode({ ...ctx, attempts: 0 })).toBe('stage');
    expect(previewRetryMode({ ...ctx, attempts: 1 })).toBe('full');
    expect(previewRetryMode({ ...ctx, attempts: 2 })).toBe('full');
  });

  it('a fresh failure starts a new per-media retry count', () => {
    expect(previewRetryAfterError(null, ctx.url, 'playback', false)).toEqual({
      url: ctx.url,
      stage: 'playback',
      attempts: 0,
    });
  });

  it('a failed retry escalates attempts for the SAME media only', () => {
    const failed = previewRetryAfterError(ctx, ctx.url, 'session', true);
    expect(failed.attempts).toBe(1);
    expect(previewRetryMode(failed)).toBe('full');
  });

  it('repeated retry failures keep escalating', () => {
    const once = previewRetryAfterError(ctx, ctx.url, 'session', true);
    const twice = previewRetryAfterError(once, ctx.url, 'session', true);
    expect(twice.attempts).toBe(2);
  });

  it('a new media resets the retry count', () => {
    const next = previewRetryAfterError(
      ctx,
      'https://twitch.tv/videos/987654321',
      'session',
      true,
    );
    expect(next.url).toBe('https://twitch.tv/videos/987654321');
    expect(next.attempts).toBe(0);
  });

  it('a manual open (not a retry) resets the count even for the same media', () => {
    const next = previewRetryAfterError({ ...ctx, attempts: 3 }, ctx.url, 'session', false);
    expect(next.attempts).toBe(0);
  });
});
