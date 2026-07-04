/**
 * Shared utility functions extracted from App.tsx and other components.
 *
 * ponytail: these were duplicated across App.tsx, EditableHmsTime.tsx,
 * NeedleGlancePopup.tsx, and ChannelExplorePopup.tsx. Centralised here
 * to prevent divergence. If a function doesn't exist here but should,
 * add it instead of redefining inline.
 */

/** Convert seconds to HH:MM:SS string. */
export function formatHmsFull(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

/** Parse an HH:MM:SS string to seconds. */
export function parseHms(t: string): number {
  const parts = t.split(':').map((s) => parseInt(s, 10) || 0);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

/** Clamp a number between min and max. */
export function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}
