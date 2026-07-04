/** ponytail: extracted from App.tsx inline helper. Platform icon for Kick/Twitch/YouTube VOD rows. */

import kickIcon from '@/assets/platforms/kick.ico';
import twitchIcon from '@/assets/platforms/twitch.png';

const YT_PATH =
  'M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31 31 0 0 0 0 12a31 31 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31 31 0 0 0 24 12a31 31 0 0 0-.5-5.8zM9.7 15.5V8.5L15.8 12l-6.1 3.5z';

export default function PlatformVodIcon({ platform, className = 'w-3.5 h-3.5' }: { platform: string; className?: string }) {
  const isTw = platform === 'Twitch';
  const isYt = platform === 'YouTube';
  if (isYt) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 fill-[#E03E3E] ${className}`} aria-label="YouTube">
        <path d={YT_PATH} />
      </svg>
    );
  }
  const sizeClass = isTw ? className : `${className} origin-left scale-90`;
  return (
    <img
      src={isTw ? twitchIcon : kickIcon}
      alt={isTw ? 'Twitch' : 'Kick'}
      className={`shrink-0 object-contain ${sizeClass}`}
      draggable={false}
    />
  );
}
