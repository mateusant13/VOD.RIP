/**
 * Chat username colors — platform-provided color when stored, deterministic
 * palette fallback otherwise.
 *
 * Sources:
 *  - YouTube live chat renderers carry `authorNameTextColor` (#RRGGBB) and
 *    the ingest path stores it on messages.color (archive_ytdlp).
 *  - Twitch GQL VOD comments carry NO color (verified), so every twitch row
 *    falls back to the classic Twitch palette below — the same behavior
 *    Twitch's own client uses for users without a set color.
 *  - Kick chat is not archived today; rows would follow the twitch-style
 *    fallback when/if it lands (messages.color stays NULL).
 *
 * The fallback hash is deterministic per (username, platform): the same
 * nickname always renders the same color across tabs and sessions.
 * ponytail: palette is a static list (Twitch's 16-color set + a
 * YouTube-style set); upgrade path = server-assigned per-user colors from
 * platform APIs that provide them (already wired via messages.color).
 */

export const TWITCH_CHAT_COLORS = [
  '#FF0000', '#0000FF', '#008000', '#B22222',
  '#FF7F50', '#9ACD32', '#FF4500', '#2E8B57',
  '#DAA520', '#D2691E', '#5F9EA0', '#1E90FF',
  '#FF69B4', '#00CED1', '#FF1493', '#00FA9A',
] as const;

/** YouTube-style bright palette (readable on the dark chat background). */
export const YOUTUBE_CHAT_COLORS = [
  '#FF0033', '#00D24D', '#0057E7', '#F65314', '#9B59B6', '#1ABC9C',
] as const;

/** FNV-1a 32-bit — stable, cheap, no collisions for realistic name counts. */
function fnv1a32(input: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

/**
 * Deterministic fallback color for a username on a platform.
 * `platform` is 'youtube' | 'twitch' | 'kick' (case-insensitive); unknown
 * platforms use the twitch palette.
 */
export function chatUsernameColor(username: string, platform: string | null | undefined): string {
  const key = `${platform ?? ''}:${username.toLowerCase()}`;
  const palette =
    platform?.toLowerCase() === 'youtube' ? YOUTUBE_CHAT_COLORS : TWITCH_CHAT_COLORS;
  return palette[fnv1a32(key) % palette.length];
}

/**
 * Final username color for a chat row: the platform-provided stored color
 * wins; NULL falls back to the deterministic palette.
 */
export function resolveChatColor(
  storedColor: string | null | undefined,
  username: string,
  platform: string | null | undefined,
): string {
  if (storedColor && /^#[0-9a-fA-F]{6}$/.test(storedColor)) {
    return storedColor;
  }
  return chatUsernameColor(username, platform);
}
