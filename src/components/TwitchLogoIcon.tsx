/**
 * Official simple-icons Twitch glyph (https://simpleicons.org) rendered with
 * `fill="currentColor"` so it inherits the button's text color. lucide-react
 * dropped brand icons, so the clip buttons use this instead of Clapperboard.
 */

export const TWITCH_GLYPH_PATH =
  'M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z';

export default function TwitchLogoIcon({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path d={TWITCH_GLYPH_PATH} />
    </svg>
  );
}
