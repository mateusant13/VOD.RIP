/** Shared platform accent colors — YouTube red toned down vs pure #FF0000. */
import type { CSSProperties } from 'react';

export const KICK_COLOR = '#53fc18';
export const TWITCH_COLOR = '#9146FF';
export const YOUTUBE_COLOR = '#F03030';

export type PlatformStyleKey = 'kick' | 'twitch' | 'youtube' | null;

export function platformStyleKey(platform: string | null | undefined): PlatformStyleKey {
  const p = (platform || '').toLowerCase();
  if (p === 'kick') return 'kick';
  if (p === 'youtube') return 'youtube';
  if (p === 'twitch') return 'twitch';
  return null;
}

export function platformAccentColor(platform: string): string {
  const key = platformStyleKey(platform);
  if (key === 'kick') return KICK_COLOR;
  if (key === 'youtube') return YOUTUBE_COLOR;
  return TWITCH_COLOR;
}

/** Inline style for custom checkboxes — pass platform accent or neutral zinc. */
export function vodCheckboxStyle(accent: string): CSSProperties {
  return { '--vod-cb-accent': accent } as CSSProperties;
}

export function platformActiveBorder(platform: string | null | undefined): string {
  const key = platformStyleKey(platform);
  if (key === 'kick') return 'border-[#53fc18]';
  if (key === 'twitch') return 'border-[#9146FF]';
  if (key === 'youtube') return 'border-[#F03030]';
  return 'border-white';
}
