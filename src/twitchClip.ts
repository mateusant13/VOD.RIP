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

export const TWITCH_CLIP_EDITOR_HOST = 'https://clips.twitch.tv/create';

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
