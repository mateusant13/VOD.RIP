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
/** Mini-preview pad on each side of the click (60s left + 60s right). */
export const TWITCH_CLIP_PAD_SEC = 60;
/** Mini-preview window length: 120s around the click. Twitch's native
 * editor is still ~90s — that constraint lives in the cookie extension. */
export const TWITCH_CLIP_WINDOW_SEC = TWITCH_CLIP_PAD_SEC * 2;
/** Pixel hit-radius around the playhead on the mini-preview rail. */
export const CLIP_PLAYHEAD_HIT_PX = 12;

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
 * Mini-preview window for the "Twitch clip" button: 120s around the click
 * (60s left + 60s right). At VOD edges the missing side is filled from the
 * other so the window stays 120s when the VOD is long enough. `anchor` is
 * ignored — the window is always the click moment, not the main trim.
 */
export function twitchClipWindow(
  playheadSec: number,
  vodDurationSec: number,
  _anchor?: { start: number; end: number },
): { start: number; end: number } {
  const dur = Number.isFinite(vodDurationSec) && vodDurationSec > 0
    ? vodDurationSec
    : Number.POSITIVE_INFINITY;
  const click = Number.isFinite(playheadSec) ? playheadSec : 0;
  let start = click - TWITCH_CLIP_PAD_SEC;
  let end = click + TWITCH_CLIP_PAD_SEC;
  if (start < 0) {
    end = Math.min(dur, end - start);
    start = 0;
  }
  if (end > dur) {
    start = Math.max(0, start - (end - dur));
    end = dur;
  }
  return {
    start: Math.max(0, Math.round(start)),
    end: Math.round(end),
  };
}

/**
 * Initial clip selection for the mini-preview. The click/playhead is the
 * START of the clip, extending forward up to 60s (so the moment the user
 * heard is at the left of the purple range, with 60s of past still on the
 * rail to the left). A short in-window `anchor` (5-60s) still wins when
 * given; a full-VOD trim is ignored.
 */
export function initialClipSelection(
  win: { start: number; end: number },
  anchor?: { start: number; end: number },
  playheadSec?: number,
): { start: number; end: number } {
  const anchorLen = anchor && anchor.end > anchor.start ? anchor.end - anchor.start : 0;
  if (anchor && anchorLen >= TWITCH_CLIP_MIN_SEC && anchorLen <= TWITCH_CLIP_MAX_SEC) {
    let start = Math.max(win.start, Math.min(anchor.start, win.end));
    let end = Math.max(win.start, Math.min(anchor.end, win.end));
    if (end - start < TWITCH_CLIP_MIN_SEC) {
      end = Math.min(win.end, Math.max(win.start, start + TWITCH_CLIP_MIN_SEC));
      if (end - start < TWITCH_CLIP_MIN_SEC) {
        start = Math.max(win.start, end - TWITCH_CLIP_MIN_SEC);
      }
    }
    return { start, end };
  }
  if (Number.isFinite(playheadSec)) {
    const ph = Math.max(win.start, Math.min(win.end, Math.floor(playheadSec as number)));
    let start = ph;
    let end = Math.min(win.end, start + TWITCH_CLIP_MAX_SEC);
    if (end - start < TWITCH_CLIP_MIN_SEC) {
      start = Math.max(win.start, end - TWITCH_CLIP_MIN_SEC);
    }
    return end - start >= TWITCH_CLIP_MIN_SEC
      ? { start, end }
      : clampClipSelection(win.start, win.end, win.start, win.end);
  }
  if (win.end - win.start > TWITCH_CLIP_MAX_SEC) {
    return { start: win.start, end: win.start + TWITCH_CLIP_MAX_SEC };
  }
  return clampClipSelection(win.start, win.end, win.start, win.end);
}

/** Which rail drag to start from a pointer X on the mini-preview slider. */
export function clipRailDragTarget(
  x: number,
  railWidth: number,
  playFracPct: number,
  selStartFracPct: number,
  selEndFracPct: number,
): 'playhead' | 'range' | 'seek' {
  if (!(railWidth > 0) || !Number.isFinite(x)) return 'seek';
  const playX = (playFracPct / 100) * railWidth;
  if (Math.abs(x - playX) <= CLIP_PLAYHEAD_HIT_PX) return 'playhead';
  const left = (selStartFracPct / 100) * railWidth;
  const right = (selEndFracPct / 100) * railWidth;
  if (x >= Math.min(left, right) && x <= Math.max(left, right)) return 'range';
  return 'seek';
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

/** Canonical integer range shared by the app, editor URL, extension, and history. */
export function canonicalTwitchClipRange(startSec: number, endSec: number): { start: number; end: number } {
  const start = Math.max(0, Math.floor(Number.isFinite(startSec) ? startSec : 0));
  const end = Math.max(start, Math.ceil(Number.isFinite(endSec) ? endSec : start));
  return {
    start: end - start > TWITCH_CLIP_MAX_SEC ? end - TWITCH_CLIP_MAX_SEC : start,
    end,
  };
}

/**
 * Map the clip editor's confirmed window back to VOD time.
 *
 * Twitch's slider is relative to the ~90s raw-media chunk (e.g. 71-90).
 * offsetSeconds / vodrip_end is the VOD time at that chunk's end, so
 * origin = vodEnd - editorEnd. A window already in VOD seconds is kept.
 */
export function vodRangeFromEditorWindow(
  requested: { start: number; end: number },
  editor?: { start: number; end: number } | null,
): { start: number; end: number } {
  const reqStart = Number.isFinite(requested.start) ? requested.start : 0;
  const reqEnd = Number.isFinite(requested.end) ? requested.end : 0;
  if (!editor || !Number.isFinite(editor.start) || !Number.isFinite(editor.end)) {
    return { start: reqStart, end: reqEnd };
  }
  if (editor.end <= 93 && reqEnd > editor.end + 2) {
    const origin = Math.max(0, reqEnd - editor.end);
    return { start: origin + editor.start, end: origin + editor.end };
  }
  return { start: editor.start, end: editor.end };
}

/**
 * Ensure the cookie extension is active without blocking the browser tab.
 * Endpoint failures are non-fatal: the editor can still open and report its
 * own state, while a completed installer asks the caller to reload the tab.
 */
export async function ensureTwitchClipExtension(): Promise<{ ok: boolean; installed: boolean }> {
  try {
    const status = await apiGet<{
      paired: boolean;
      platforms?: { twitch?: { lastGrabAt?: string | null } };
    }>('/api/session/cookies/status');
    if (status.paired) return { ok: true, installed: false };
    const lastGrab = status.platforms?.twitch?.lastGrabAt;
    if (lastGrab && Date.now() - new Date(lastGrab).getTime() < 11 * 60_000) {
      return { ok: true, installed: false };
    }
    const inst = await apiPost<{ ok: boolean; started?: boolean; alreadyInstalled?: boolean }>(
      '/api/session/cookies/auto-install',
      {},
    );
    if (!inst.ok && !inst.started && !inst.alreadyInstalled) return { ok: false, installed: false };
    const deadline = Date.now() + 700_000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      try {
        const next = await apiGet<{ paired: boolean; auto_install?: { state?: string } }>(
          '/api/session/cookies/status',
        );
        if (next.paired && (!next.auto_install || next.auto_install.state !== 'running')) {
          return { ok: true, installed: true };
        }
      } catch {
        // Installer restarts the backend briefly; keep polling.
      }
    }
    return { ok: false, installed: true };
  } catch {
    return { ok: true, installed: false };
  }
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
  thumbnail_url?: string | null;
}
/**
 * Build the download payload for a history clip.
 *
 * Browser-created Twitch clips can remain in Twitch's processing queue with no
 * media variants. When the original VOD range is stored, download that exact
 * range instead of asking yt-dlp to resolve an empty clip page.
 */
export function twitchClipDownloadRequest(clip: TwitchClipRecord): {
  url: string;
  quality: 'source';
  title?: string;
  channel: string;
  thumbnail?: string;
  duration?: number;
  crop_start?: number;
  crop_end?: number;
} {
  const title = clip.title?.trim() || undefined;
  const thumbnail = clip.thumbnail_url?.trim() || undefined;
  const offset = clip.offset_sec;
  const duration = clip.duration_sec;
  if (
    clip.vod_id
    && Number.isFinite(offset)
    && Number.isFinite(duration)
    && (offset ?? 0) >= 0
    && (duration ?? 0) > 0
  ) {
    const end = Math.floor(offset as number);
    const length = Math.max(1, Math.round(duration as number));
    return {
      url: `https://www.twitch.tv/videos/${encodeURIComponent(clip.vod_id)}`,
      quality: 'source',
      title,
      channel: clip.channel,
      thumbnail,
      duration: length,
      crop_start: Math.max(0, end - length),
      crop_end: end,
    };
  }
  return {
    url: clip.url,
    quality: 'source',
    title,
    channel: clip.channel,
    thumbnail,
    duration: clip.duration_sec ?? undefined,
  };
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
  title: string,
  vodDurationSec?: number,
  targetWindow?: Window | null,
): Window | null {
  const clipTitle = title.trim();
  if (!clipTitle) throw new Error('Original VOD title is required to create a Twitch clip');
  const range = canonicalTwitchClipRange(startSec, endSec);
  const p = new URLSearchParams({
    vodrip_clip: '1',
    vodrip_start: String(range.start),
    vodrip_end: String(range.end),
    vodrip_close: '0',
    vodrip_title: clipTitle,
  });
  if (vodDurationSec && Number.isFinite(vodDurationSec) && vodDurationSec > 0) {
    p.set('vodrip_dur', String(Math.round(vodDurationSec)));
  }
  const url =
    `https://clips.twitch.tv/create?broadcasterLogin=${encodeURIComponent(broadcasterLogin)}&offsetSeconds=${range.end}&vodID=${encodeURIComponent(vodId)}&${p.toString()}`;
  reportClipEvent('browser_open', {
    url,
    startSec: range.start,
    endSec: range.end,
    title: clipTitle,
  });
  if (targetWindow) {
    targetWindow.location.href = url;
    return targetWindow;
  }
  return window.open(url, '_blank');
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

/** One archived chat row inside a clip's window (preview-panel projection). */
export interface TwitchClipChatMessage {
  platform: string;
  video_id: string;
  offset_sec: number;
  username: string;
  text: string;
  spam_count: number;
  color: string | null;
}

/** GET /api/twitch/clips/{slug}/chat — the source VOD's chat windowed to the
 *  clip media ([offset_sec - duration_sec, offset_sec]); an empty window
 *  arrives as `messages: []` with total 0. */
export interface TwitchClipChat {
  messages: TwitchClipChatMessage[];
  truncated: boolean;
  total: number;
}

/** Fetch the archived chat of a recorded clip's source VOD (windowed). */
export async function fetchTwitchClipChat(slug: string): Promise<TwitchClipChat> {
  return apiGet<TwitchClipChat>(`/api/twitch/clips/${encodeURIComponent(slug)}/chat`);
}

/** Twitch clip slug from a clip URL (clips.twitch.tv/Slug or
 *  twitch.tv/<channel>/clip/Slug); null when the URL is not a clip URL. */
export function clipSlugFromUrl(url: string): string | null {
  const m = (url || '').trim().match(
    /(?:clips\.twitch\.tv\/|twitch\.tv\/[^/]+\/clip\/)([A-Za-z0-9_-]+)/i,
  );
  return m ? m[1] : null;
}
