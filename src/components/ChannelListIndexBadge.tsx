/** ponytail: extracted from App.tsx inline helper. Channel list row badge shown on main preview when opened from Channels. */

import { KICK_COLOR, TWITCH_COLOR, YOUTUBE_COLOR } from '../platformColors';

export default function ChannelListIndexBadge({
  platform,
  index,
  size = 'sm',
}: {
  platform: string;
  index: number;
  size?: 'sm' | 'md';
}) {
  const color = platform === 'Kick'
    ? KICK_COLOR
    : platform === 'YouTube'
      ? YOUTUBE_COLOR
      : TWITCH_COLOR;
  const dim = size === 'md' ? 'w-5 text-[11px] leading-tight pt-0.5' : 'w-4 text-[9px]';
  return (
    <span
      className={`shrink-0 text-center font-mono font-bold tabular-nums ${dim}`}
      style={{ color }}
      title={`${platform} #${index}`}
    >
      {index}
    </span>
  );
}
