/** ponytail: extracted from App.tsx inline helper. Platform icon for Kick/Twitch VOD rows. */

import kickIcon from '@/assets/platforms/kick.ico';
import twitchIcon from '@/assets/platforms/twitch.png';

export default function PlatformVodIcon({ platform, className = 'w-3.5 h-3.5' }: { platform: string; className?: string }) {
  const isTw = platform === 'Twitch';
  return (
    <img
      src={isTw ? twitchIcon : kickIcon}
      alt={isTw ? 'Twitch' : 'Kick'}
      className={`shrink-0 object-contain ${className}`}
      draggable={false}
    />
  );
}
