/** ponytail: extracted from App.tsx inline helper. Platform icon for Kick/Twitch/YouTube VOD rows. */

const YT_PATH =
  'M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31 31 0 0 0 0 12a31 31 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31 31 0 0 0 24 12a31 31 0 0 0-.5-5.8zM9.7 15.5V8.5L15.8 12l-6.1 3.5z';
// Official simple-icons brand marks (https://simpleicons.org). Inline SVG paints
// in the same frame as the row — the old <img> data-URIs needed an async decode.
const TWITCH_PATH =
  'M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z';
const KICK_PATH =
  'M1.333 0h8v5.333H12V2.667h2.667V0h8v8H20v2.667h-2.667v2.666H20V16h2.667v8h-8v-2.667H12v-2.666H9.333V24h-8Z';

export default function PlatformVodIcon({ platform, className = 'w-3.5 h-3.5' }: { platform: string; className?: string }) {
  if (platform === 'YouTube') {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 fill-[#EB2828] ${className}`} aria-label="YouTube">
        <path d={YT_PATH} />
      </svg>
    );
  }
  if (platform === 'Twitch') {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 fill-[#9146FF] ${className}`} aria-label="Twitch">
        <path d={TWITCH_PATH} />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className={`shrink-0 fill-[#18FC53] ${className}`} aria-label="Kick">
      <path d={KICK_PATH} />
    </svg>
  );
}
