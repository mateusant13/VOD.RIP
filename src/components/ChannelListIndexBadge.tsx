/** ponytail: extracted from App.tsx inline helper. Channel list row badge shown on main preview when opened from Channels. */

export default function ChannelListIndexBadge({
  platform,
  index,
  size = 'sm',
}: {
  platform: string;
  index: number;
  size?: 'sm' | 'md';
}) {
  const isKick = platform === 'Kick';
  const dim = size === 'md' ? 'w-5 text-[11px] leading-tight pt-0.5' : 'w-4 text-[9px]';
  return (
    <span
      className={`shrink-0 text-center font-mono font-bold tabular-nums ${dim} ${
        isKick ? 'text-[#53fc18]' : 'text-[#9146FF]'
      }`}
      title={`${platform} #${index}`}
    >
      {index}
    </span>
  );
}
