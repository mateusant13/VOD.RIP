import { describe, expect, it } from 'vitest';
import {
  TWITCH_CHAT_COLORS,
  YOUTUBE_CHAT_COLORS,
  chatUsernameColor,
  resolveChatColor,
} from './chatColors';

describe('chatUsernameColor', () => {
  it('is deterministic per username', () => {
    expect(chatUsernameColor('Pong.agario', 'twitch')).toBe(
      chatUsernameColor('Pong.agario', 'twitch'),
    );
    expect(chatUsernameColor('Titi', 'youtube')).toBe(chatUsernameColor('titi', 'youtube'));
  });

  it('returns a palette color for every platform', () => {
    for (const platform of ['twitch', 'kick', 'youtube', null, undefined, 'unknown']) {
      const c = chatUsernameColor('some_user', platform);
      expect(c).toMatch(/^#[0-9A-F]{6}$/i);
    }
  });

  it('uses the platform palette', () => {
    expect(TWITCH_CHAT_COLORS).toContain(chatUsernameColor('u', 'twitch'));
    expect(TWITCH_CHAT_COLORS).toContain(chatUsernameColor('u', 'kick'));
    expect(YOUTUBE_CHAT_COLORS).toContain(chatUsernameColor('u', 'youtube'));
    // Same name, different platform palette -> may differ, but both valid.
    expect(chatUsernameColor('u', 'youtube')).toMatch(/^#[0-9A-F]{6}$/i);
  });

  it('spreads names across the palette (not all the same color)', () => {
    const names = Array.from({ length: 40 }, (_, i) => `user_${i}`);
    const colors = new Set(names.map((n) => chatUsernameColor(n, 'twitch')));
    expect(colors.size).toBeGreaterThan(4);
  });
});

describe('resolveChatColor', () => {
  it('prefers the stored platform color', () => {
    expect(resolveChatColor('#FF0033', 'someone', 'youtube')).toBe('#FF0033');
    expect(resolveChatColor('#ff0033', 'someone', 'twitch')).toBe('#ff0033');
  });

  it('falls back to the palette for null/empty/malformed', () => {
    const fallback = chatUsernameColor('x', 'twitch');
    expect(resolveChatColor(null, 'x', 'twitch')).toBe(fallback);
    expect(resolveChatColor(undefined, 'x', 'twitch')).toBe(fallback);
    expect(resolveChatColor('', 'x', 'twitch')).toBe(fallback);
    expect(resolveChatColor('red', 'x', 'twitch')).toBe(fallback);
    expect(resolveChatColor('#12345', 'x', 'twitch')).toBe(fallback);
    expect(resolveChatColor('#GGGGGG', 'x', 'twitch')).toBe(fallback);
  });
});
