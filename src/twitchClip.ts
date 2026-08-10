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
 * Debugging event sequence for the clip flow: every step of a clip attempt
 * (app UI, Helix API, browser cookie extension) appends a timestamped JSON
 * line to the backend's clip-events log so the attempt can be replayed end
 * to end — see GET /api/debug/clip-events. Fire-and-forget: logging must
 * never affect the flow.
 */
export function reportClipEvent(event: string, data: Record<string, unknown> = {}): void {
  void apiPost('/api/debug/clip-events', { src: 'app', event, data }).catch(() => {});
}

/**
 * Mini-preview window for the "Twitch clip" button: ±60s around the click
 * moment, clamped to the VOD edges (shorter at the edges is fine). Unknown
 * duration (<=0) leaves the upper edge unclamped — the backend clamps the
 * session crop to the real extracted length anyway.
 *
 * When `anchor` is a valid range (end > start) — the user's typed trim from
 * the opening preview — the window centres on the anchor's midpoint instead
 * of the playhead, so the trim lands inside the mini-preview.
 */
export function twitchClipWindow(
  playheadSec: number,
  vodDurationSec: number,
  anchor?: { start: number; end: number },
): { start: number; end: number } {
  const dur = Number.isFinite(vodDurationSec) && vodDurationSec > 0
    ? vodDurationSec
    : Number.POSITIVE_INFINITY;
  const anchorValid = !!anchor && anchor.end > anchor.start;
  const center = anchorValid ? (anchor.start + anchor.end) / 2 : playheadSec;
  return {
    start: Math.max(0, center - TWITCH_CLIP_MAX_SEC),
    end: Math.min(dur, center + TWITCH_CLIP_MAX_SEC),
  };
}

/**
 * Initial clip selection for the mini-preview. With a valid `anchor` (the
 * user's typed trim range) the selection IS the anchor, clamped into the
 * window, with its length forced into [TWITCH_CLIP_MIN_SEC, TWITCH_CLIP_MAX_SEC]
 * — an over-long anchor keeps its END pinned (start = end − 60, matching the
 * END-reference semantics of Helix vod_offset), an under-long one grows from
 * its start. Without an anchor, replicate the default: the last 60s of the
 * window, or the whole window when it's shorter.
 */
export function initialClipSelection(
  win: { start: number; end: number },
  anchor?: { start: number; end: number },
): { start: number; end: number } {
  if (anchor && anchor.end > anchor.start) {
    let start = Math.max(win.start, Math.min(anchor.start, win.end));
    let end = Math.max(win.start, Math.min(anchor.end, win.end));
    if (end - start > TWITCH_CLIP_MAX_SEC) {
      // Keep the END anchored (clip END = Helix vod_offset).
      start = end - TWITCH_CLIP_MAX_SEC;
    } else if (end - start < TWITCH_CLIP_MIN_SEC) {
      // Grow from the start first; at the window edge, pull the start back.
      end = Math.min(win.end, Math.max(win.start, start + TWITCH_CLIP_MIN_SEC));
      if (end - start < TWITCH_CLIP_MIN_SEC) {
        start = Math.max(win.start, end - TWITCH_CLIP_MIN_SEC);
      }
    }
    return { start, end };
  }
  if (win.end - win.start > TWITCH_CLIP_MAX_SEC) {
    return { start: win.end - TWITCH_CLIP_MAX_SEC, end: win.end };
  }
  return clampClipSelection(win.start, win.end, win.start, win.end);
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

/**
 * Official VOD.RIP OAuth app Client ID, embedded at build time so end users
 * never register their own app (same model as Chatterino's client_login).
 * The registered redirect URI (localhost:7897) is identical on every machine,
 * so one app serves all users. Overridable per-user via Settings → Official
 * APIs for power users. Empty until the official app is created; the token
 * button then falls back to the registration page.
 */
export const DEFAULT_TWITCH_CLIENT_ID = 'lvhunanwtrdeo3luw5hq2p94ygzgjp';

/**
 * Open Twitch's clip editor at a VOD timestamp in the OS default browser.
 * Uses the legacy editor URL (clips.twitch.tv/create?vodID=...&offsetSeconds=...)
 * which mounts the clip editor DIRECTLY when logged in — offsetSeconds is the
 * clip END (Twitch's editor anchor), the same reference the Helix path uses.
 * The VOD.RIP cookie extension's content script (clip_assist.mjs, matches
 * clips.twitch.tv/create) then fills the title and clicks Save Clip using
 * Twitch's own session cookie — no Helix clip scopes needed.
 */
export function openTwitchClipEditorInBrowser(
  vodId: string,
  broadcasterLogin: string,
  startSec: number,
  endSec: number,
  title?: string,
): void {
  const p = new URLSearchParams({
    vodrip_clip: '1',
    vodrip_start: String(Math.max(0, Math.floor(startSec))),
    vodrip_end: String(Math.max(0, Math.ceil(endSec))),
    // The browser path is the user's explicit choice ("Open in browser") —
    // keep the Twitch tab open after the flow so the editor + published clip
    // stay visible. Default (absent) is close, per the window rule.
    vodrip_close: '0',
  });
  if (title) p.set('vodrip_title', title);
  const url =
    `https://clips.twitch.tv/create?broadcasterLogin=${encodeURIComponent(broadcasterLogin)}&offsetSeconds=${Math.max(0, Math.floor(endSec))}&vodID=${encodeURIComponent(vodId)}&${p.toString()}`;
  reportClipEvent('browser_open', { url, startSec, endSec, title: title ?? null });
  openExternal(url);
}

/**
 * Ask the backend to create a Twitch clip via the official Helix API. The
 * clip is created server-side — no browser window is opened; the caller
 * surfaces the clip URL (see clipPublicUrl) in the app.
 */
export async function openTwitchClipEditor(
  args: OpenTwitchClipArgs,
): Promise<TwitchClipOpenResult> {
  const body = {
    broadcaster_login: args.broadcasterLogin,
    vod_id: args.vodId ?? null,
    offset_sec: args.offsetSec ?? null,
    duration_sec: args.durationSec ?? null,
    title: args.title ?? null,
  };
  reportClipEvent('api_request', body);
  const res = await apiPost<TwitchClipOpenResult>('/api/twitch/clip', body);
  if (res.ok) {
    reportClipEvent('api_success', { id: res.id, edit_url: res.edit_url });
  } else {
    reportClipEvent('api_error', { code: res.error.code, message: res.error.message });
  }
  return res;
}

/** Public clip URL for a Helix edit_url (drops a trailing /edit, if any). */
export function clipPublicUrl(editUrl: string): string {
  return editUrl ? editUrl.replace(/\/edit$/, '') : editUrl;
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
