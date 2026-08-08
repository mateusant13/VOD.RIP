/**
 * Twitch clip creation — shared helpers for the preview "CLIP" buttons.
 *
 * POST /api/twitch/clip creates the clip through Twitch's official Helix API
 * (Chatterino-style) using the stored twitch_helix_token: live clips via
 * POST /helix/clips (clips:edit), VOD clips via POST /helix/videos/clips
 * (editor:manage:clips | channel:manage:clips, vod_offset = clip END).
 * On success the returned edit_url (valid 24h) is opened in the OS default
 * browser; failures come back as {ok: false, error: {code, message}} and are
 * surfaced by the callers.
 */

import { apiDelete, apiGet, apiPost } from './hooks/useApiClient';

export const TWITCH_CLIP_MAX_SEC = 60;
/** Twitch's own editor rejects selections shorter than this. */
export const TWITCH_CLIP_MIN_SEC = 5;
/** Helix clip title length limit. */
export const TWITCH_CLIP_TITLE_MAX = 140;

/**
 * Mini-preview window for the "Twitch clip" button: ±60s around the click
 * moment, clamped to the VOD edges (shorter at the edges is fine). Unknown
 * duration (<=0) leaves the upper edge unclamped — the backend clamps the
 * session crop to the real extracted length anyway.
 */
export function twitchClipWindow(
  playheadSec: number,
  vodDurationSec: number,
): { start: number; end: number } {
  const dur = Number.isFinite(vodDurationSec) && vodDurationSec > 0
    ? vodDurationSec
    : Number.POSITIVE_INFINITY;
  return {
    start: Math.max(0, playheadSec - TWITCH_CLIP_MAX_SEC),
    end: Math.min(dur, playheadSec + TWITCH_CLIP_MAX_SEC),
  };
}

/**
 * Clamp a selection on the mini-preview window timeline to [5, 60]s (or the
 * window length when the window is shorter), mirroring clampTrimEndpoints'
 * move/pin shape: dragging one handle pins the other end.
 */
export function clampClipSelection(
  rawStart: number,
  rawEnd: number,
  winStart: number,
  winEnd: number,
  opts?: { move?: 'in' | 'out'; fixedStart?: number; fixedEnd?: number },
): { start: number; end: number } {
  const winLen = winEnd - winStart;
  // Degenerate window (<5s): selection collapses to the whole window — the
  // create button then reports the too-short error instead of a weird trim.
  if (winLen < TWITCH_CLIP_MIN_SEC) {
    return { start: winStart, end: winEnd };
  }
  const minLen = Math.min(TWITCH_CLIP_MIN_SEC, winLen);
  const maxLen = Math.min(TWITCH_CLIP_MAX_SEC, winLen);

  if (opts?.move === 'in') {
    const pinnedEnd = Math.min(winEnd, Math.max(winStart, Math.floor(opts.fixedEnd ?? winEnd)));
    let start = Math.max(winStart, Math.min(Math.floor(rawStart), pinnedEnd - minLen));
    if (pinnedEnd - start > maxLen) start = Math.max(winStart, pinnedEnd - maxLen);
    return { start, end: pinnedEnd };
  }
  if (opts?.move === 'out') {
    const pinnedStart = Math.max(winStart, Math.min(Math.floor(opts.fixedStart ?? winStart), winEnd));
    let end = Math.min(winEnd, Math.max(Math.ceil(rawEnd), pinnedStart + minLen));
    if (end - pinnedStart > maxLen) end = Math.min(winEnd, pinnedStart + maxLen);
    return { start: pinnedStart, end };
  }
  // Free-form (init / button nudges): clamp into the window and enforce the
  // 5s floor. No 60s cap here — the initial full-window selection may exceed
  // it; every user edit goes through the move branches below (hard 5..60) and
  // the Create action re-checks the range before creating the clip.
  let start = Math.max(winStart, Math.min(Math.floor(rawStart), winEnd));
  let end = Math.min(winEnd, Math.max(Math.ceil(rawEnd), start + minLen));
  if (end - start < minLen) start = Math.max(winStart, end - minLen);
  return { start, end };
}

/**
 * Map a VOD-coordinate selection to the clip request: vod_offset is a clip-END
 * reference (the backend passes it straight to Helix as vod_offset), duration
 * is the selected clip length.
 */
export function clipEditorOffsetAndDuration(
  selectionStartSec: number,
  selectionEndSec: number,
): { offsetSec: number; durationSec: number } {
  return {
    offsetSec: Math.max(0, Math.floor(selectionEndSec)),
    durationSec: Math.round(Math.max(0, selectionEndSec - selectionStartSec)),
  };
}

/** Error message when the selected range can't become a Twitch clip, else null. */
export function twitchClipDurationError(durationSec: number): string | null {
  if (!Number.isFinite(durationSec) || durationSec <= 0) {
    return 'Select a clip range first';
  }
  if (durationSec < TWITCH_CLIP_MIN_SEC) {
    return `Clip must be at least ${TWITCH_CLIP_MIN_SEC}s (selected ${Math.round(durationSec)}s)`;
  }
  if (durationSec > TWITCH_CLIP_MAX_SEC) {
    return `Clip must be ${TWITCH_CLIP_MAX_SEC}s or less (selected ${Math.round(durationSec)}s)`;
  }
  return null;
}

export interface TwitchClipRecord {
  id: string;
  created_at: string;
  channel: string;
  vod_id: string | null;
  offset_sec: number | null;
  duration_sec: number | null;
  title: string | null;
  url: string;
  status: string;
}

export interface TwitchClipError {
  code: string;
  message: string;
}

export type TwitchClipOpenResult =
  | { ok: true; id: string | null; edit_url: string }
  | { ok: false; error: TwitchClipError };

export interface OpenTwitchClipArgs {
  broadcasterLogin: string;
  vodId?: string;
  offsetSec?: number;
  durationSec?: number;
  /** User-chosen clip title (empty -> backend sends the broadcaster login for VOD clips, since Helix requires a title; live clips omit it -> Twitch auto-titles). Becomes the local filename on download. */
  title?: string;
}

/** Open a URL in the OS default browser (external window, not the WebView2). */
function openExternal(url: string): void {
  window.open(url, '_blank', 'noopener,noreferrer');
}

/** Format VOD seconds as Twitch's ?t= syntax (1h2m3s / 2m3s / 3s). */
function formatTwitchT(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h ? `${h}h${pad(m)}m${pad(r)}s` : m ? `${m}m${pad(r)}s` : `${r}s`;
}

/**
 * Open Twitch's in-site clip editor at a VOD timestamp in the OS default
 * browser. Requires the VOD.RIP cookie extension: its content script
 * (vendor/cookie-extension/src/clip_assist.mjs) reads the vodrip_* query
 * params, opens the editor, lands the player on `startSec`, fills the title
 * and clicks Publish — using Twitch's own session cookie, so no Helix
 * clip scopes are needed.
 */
export function openTwitchClipEditorInBrowser(
  vodId: string,
  startSec: number,
  endSec: number,
  title?: string,
): void {
  const p = new URLSearchParams({
    vodrip_clip: '1',
    vodrip_start: String(Math.max(0, Math.floor(startSec))),
    vodrip_end: String(Math.max(0, Math.ceil(endSec))),
  });
  if (title) p.set('vodrip_title', title);
  openExternal(
    `https://www.twitch.tv/videos/${encodeURIComponent(vodId)}?t=${formatTwitchT(startSec)}&${p.toString()}`,
  );
}

/**
 * Ask the backend to create a Twitch clip via the official Helix API; on
 * success the returned edit_url (valid 24h) is opened in the default browser.
 */
export async function openTwitchClipEditor(
  args: OpenTwitchClipArgs,
): Promise<TwitchClipOpenResult> {
  const res = await apiPost<TwitchClipOpenResult>('/api/twitch/clip', {
    broadcaster_login: args.broadcasterLogin,
    vod_id: args.vodId ?? null,
    offset_sec: args.offsetSec ?? null,
    duration_sec: args.durationSec ?? null,
    title: args.title ?? null,
  });
  if (res.ok && res.edit_url) {
    openExternal(res.edit_url);
  }
  return res;
}

export async function fetchTwitchClipHistory(): Promise<TwitchClipRecord[]> {
  return apiGet<TwitchClipRecord[]>('/api/twitch/clips/history');
}

/** Batch-remove clip history entries by id; returns how many were removed. */
export async function deleteTwitchClipHistory(
  ids: string[],
): Promise<{ ok: boolean; removed: number }> {
  return apiDelete<{ ok: boolean; removed: number }>('/api/twitch/clips/history', { ids });
}
