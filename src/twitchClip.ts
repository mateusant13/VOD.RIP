/**
 * Twitch clip creation — shared helpers for the preview "CLIP" buttons.
 *
 * Clips are created in Twitch's own clip editor (clips.twitch.tv/create),
 * opened by the frontend in the OS default browser with vodrip_* params; the
 * VOD.RIP cookie extension's content script (clip_assist.mjs) fills the title
 * and clicks Save using Twitch's own session cookie — no API token, scopes or
 * editor role needed. The extension posts the published clip URL to
 * /api/twitch/clips/record so the clip lands in the app history with a
 * download button.
 */

import { apiDelete, apiGet, apiPost } from './hooks/useApiClient';

export const TWITCH_CLIP_MAX_SEC = 60;
/** Twitch's own editor rejects selections shorter than this. */
export const TWITCH_CLIP_MIN_SEC = 5;
/** Twitch editor clip title length limit. */
export const TWITCH_CLIP_TITLE_MAX = 140;
/** Twitch's native editor window length: 1:30 (90s). */
export const TWITCH_CLIP_WINDOW_SEC = 90;

/**
 * Debugging event sequence for the clip flow: every step of a clip attempt
 * (app UI, browser cookie extension, backend) appends a timestamped JSON
 * line to the backend's clip-events log so the attempt can be replayed end
 * to end — see GET /api/debug/clip-events. Fire-and-forget: logging must
 * never affect the flow.
 */
export function reportClipEvent(event: string, data: Record<string, unknown> = {}): void {
  void apiPost('/api/debug/clip-events', { src: 'app', event, data }).catch(() => {});
}

/**
 * Mini-preview window for the "Twitch clip" button: 90s (1:30) around the
 * click moment — the user trims there and creates a 5–60s clip. Live: open
 * the editor directly — no VOD timeline to select from.
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
  // Round the start, then add the full native 90s window.
  const start = Math.max(0, Math.round(center - TWITCH_CLIP_WINDOW_SEC / 2));
  return {
    start,
    end: Math.min(dur, start + TWITCH_CLIP_WINDOW_SEC),
  };
}

/**
 * Initial clip selection for the mini-preview. With a valid `anchor` (the
 * user's typed trim range) the selection IS the anchor, clamped into the
 * window, with its length forced into [TWITCH_CLIP_MIN_SEC, TWITCH_CLIP_MAX_SEC]
 * — an over-long anchor keeps its END pinned (start = end − 60, matching the
 * END-reference semantics of the Twitch editor's offsetSeconds), an under-long
 * one grows from its start. Without an anchor, replicate the default: the
 * last 60s of the window, or the whole window when it's shorter.
 */
export function initialClipSelection(
  win: { start: number; end: number },
  anchor?: { start: number; end: number },
): { start: number; end: number } {
  if (anchor && anchor.end > anchor.start) {
    let start = Math.max(win.start, Math.min(anchor.start, win.end));
    let end = Math.max(win.start, Math.min(anchor.end, win.end));
    if (end - start > TWITCH_CLIP_MAX_SEC) {
      // Keep the END anchored (clip END = editor offsetSeconds).
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

/**
 * Open Twitch's clip editor at a VOD timestamp in the OS default browser.
 * Uses the legacy editor URL (clips.twitch.tv/create?vodID=...&offsetSeconds=...)
 * which mounts the clip editor DIRECTLY when logged in — offsetSeconds is the
 * clip END (Twitch's editor anchor). The VOD.RIP cookie extension's content
 * script (clip_assist.mjs, matches clips.twitch.tv/create) then fills the
 * title and clicks Save Clip using Twitch's own session cookie.
 */
export function openTwitchClipEditorInBrowser(
  vodId: string,
  broadcasterLogin: string,
  startSec: number,
  endSec: number,
  title?: string,
  vodDurationSec?: number,
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
  // VOD total length — the editor clamps the clip window at the VOD's last
  // frame, so the extension needs it to nudge the window off the edge
  // instead of failing the confirmation (see background.js edge retry).
  if (vodDurationSec && Number.isFinite(vodDurationSec) && vodDurationSec > 0) {
    p.set('vodrip_dur', String(Math.round(vodDurationSec)));
  }
  const url =
    `https://clips.twitch.tv/create?broadcasterLogin=${encodeURIComponent(broadcasterLogin)}&offsetSeconds=${Math.max(0, Math.floor(endSec))}&vodID=${encodeURIComponent(vodId)}&${p.toString()}`;
  reportClipEvent('browser_open', { url, startSec, endSec, title: title ?? null });
  window.open(url, '_blank', 'noopener,noreferrer');
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
