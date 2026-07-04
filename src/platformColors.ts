/** Shared platform accent colors — YouTube red toned down vs pure #FF0000. */
export const KICK_COLOR = '#53fc18';
export const TWITCH_COLOR = '#9146FF';
export const YOUTUBE_COLOR = '#EB2828';

export function platformAccentColor(platform: string): string {
  const p = platform.toLowerCase();
  if (p === 'kick') return KICK_COLOR;
  if (p === 'youtube') return YOUTUBE_COLOR;
  return TWITCH_COLOR;
}
