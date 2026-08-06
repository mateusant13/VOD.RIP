/**
 * Twitch clip creation — shared helpers for the preview "CLIP" buttons.
 *
 * Creating a clip via the official Helix API needs broadcaster/editor OAuth,
 * so instead we open Twitch's own clip editor pre-positioned on the selected
 * moment (the route Chatterino uses — an internal UI route, not a stable
 * public API) in the user's default browser, where they are already logged
 * in. The backend records every editor-open into a local history file.
 */

import { apiGet, apiPost } from './hooks/useApiClient';

export const TWITCH_CLIP_MAX_SEC = 60;
/** Twitch's own editor rejects selections shorter than this. */
export const TWITCH_CLIP_MIN_SEC = 5;

export const TWITCH_CLIP_EDITOR_HOST = 'https://clips.twitch.tv/create';

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
  // the Create action re-checks the range before opening the editor.
  let start = Math.max(winStart, Math.min(Math.floor(rawStart), winEnd));
  let end = Math.min(winEnd, Math.max(Math.ceil(rawEnd), start + minLen));
  if (end - start < minLen) start = Math.max(winStart, end - minLen);
  return { start, end };
}

/**
 * Map a VOD-coordinate selection to the editor request: the editor's
 * offsetSeconds is a clip-END reference (see buildTwitchClipEditorUrl).
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

/** Editor URL pre-positioned on a VOD moment (offset = clip END reference). */
export function buildTwitchClipEditorUrl(opts: {
  vodId: string;
  broadcasterLogin: string;
  offsetSeconds: number;
}): string {
  const p = new URLSearchParams({
    vodID: opts.vodId,
    broadcasterLogin: opts.broadcasterLogin,
    offsetSeconds: String(Math.floor(opts.offsetSeconds)),
  });
  return `${TWITCH_CLIP_EDITOR_HOST}?${p.toString()}`;
}

/** Editor URL for a live broadcast — the web editor picks the recent window. */
export function buildLiveTwitchClipEditorUrl(broadcasterLogin: string): string {
  const p = new URLSearchParams({ broadcasterLogin });
  return `${TWITCH_CLIP_EDITOR_HOST}?${p.toString()}`;
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
  url: string;
  status: string;
}

export interface OpenTwitchClipArgs {
  broadcasterLogin: string;
  vodId?: string;
  offsetSec?: number;
  durationSec?: number;
}

/** Ask the backend to record + open the Twitch clip editor in the default browser. */
export async function openTwitchClipEditor(
  args: OpenTwitchClipArgs,
): Promise<{ ok: boolean; url: string; id: string }> {
  return apiPost<{ ok: boolean; url: string; id: string }>('/api/twitch/clip', {
    broadcaster_login: args.broadcasterLogin,
    vod_id: args.vodId ?? null,
    offset_sec: args.offsetSec ?? null,
    duration_sec: args.durationSec ?? null,
  });
}

export async function fetchTwitchClipHistory(): Promise<TwitchClipRecord[]> {
  return apiGet<TwitchClipRecord[]>('/api/twitch/clips/history');
}
