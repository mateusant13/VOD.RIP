/**
 * Pure helpers for the in-preview MINI EDITOR (the compact range picker that
 * lives inside each preview window, next to the transport).
 *
 * This module deliberately owns NO range maths of its own: every clamp, nudge
 * and pin comes from `trimUtils`, and every Twitch-clip bound/validation comes
 * from `twitchClip`. What it adds is only the editor's own default selection,
 * its keyboard step contract, and the download payload — so the preview path
 * cannot drift from the main window's trim rail or from TwitchClipPopup.
 *
 * The download body is the piece that matters for sidecar alignment: it sends
 * `crop_start`/`crop_end` on POST /api/download/clip, and the backend's
 * download_sidecars rebases the SRT by crop_start so a trimmed clip's subtitle
 * starts at 0:00 and stays aligned with its own media file.
 */

import {
  adjustTrimEndpointByDelta,
  clampTrimEndpoints,
  trimButtonDeltaForEndpoint,
} from './trimUtils';
import {
  TWITCH_CLIP_MAX_SEC,
  TWITCH_CLIP_MIN_SEC,
  twitchClipDurationError,
} from './twitchClip';

export interface MiniEditorRange {
  start: number;
  end: number;
}

/** Default cut length (s) selected when the editor opens. */
export const MINI_EDITOR_DEFAULT_LEN_SEC = 30;
/** Seconds per arrow-key press; Shift multiplies this. */
export const MINI_EDITOR_KEY_STEP_SEC = 1;
export const MINI_EDITOR_KEY_STEP_FAST_SEC = 5;

/** Selected length in seconds (never negative). */
export function miniEditorRangeLength(range: MiniEditorRange): number {
  return Math.max(0, (range?.end ?? 0) - (range?.start ?? 0));
}

/**
 * The default selection: MINI_EDITOR_DEFAULT_LEN_SEC starting at the playhead,
 * pulled back inside the media when the playhead sits near the end. Clamped
 * through trimUtils so a <1 s media degrades to the whole thing rather than an
 * inverted range.
 */
export function initialMiniEditorRange(
  playheadSec: number,
  durationSec: number,
  defaultLenSec: number = MINI_EDITOR_DEFAULT_LEN_SEC,
): MiniEditorRange {
  const dur = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  if (dur <= 0) return { start: 0, end: 0 };
  const ph = Math.max(0, Math.min(dur, Number.isFinite(playheadSec) ? playheadSec : 0));
  const want = Math.max(1, Math.min(dur, defaultLenSec));
  const end = Math.min(dur, ph + want);
  const start = Math.max(0, end - want);
  return clampTrimEndpoints(start, end, dur, start, end, { seek: 'in' });
}

/**
 * Nudge one endpoint by `deltaSec` (+ extends the cut that way), pinning the
 * other end. Uses the same two trimUtils calls App.tsx's ±buttons use, in the
 * same order, so a keyboard press and a ±button press cannot disagree.
 */
export function nudgeMiniEditorRange(
  range: MiniEditorRange,
  durationSec: number,
  which: 'in' | 'out',
  deltaSec: number,
): MiniEditorRange {
  const dur = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  if (dur <= 0) return { start: 0, end: 0 };
  const step = adjustTrimEndpointByDelta(
    range.start,
    range.end,
    dur,
    which,
    Number.isFinite(deltaSec) ? deltaSec : 0,
  );
  return clampTrimEndpoints(step.start, step.end, dur, range.start, range.end, {
    move: which,
    fixedStart: range.start,
    fixedEnd: range.end,
  });
}

/** A ±button press: the raw button delta, converted like the main window's. */
export function miniEditorButtonDelta(
  range: MiniEditorRange,
  durationSec: number,
  which: 'in' | 'out',
  buttonDelta: number,
): MiniEditorRange {
  return nudgeMiniEditorRange(
    range,
    durationSec,
    which,
    trimButtonDeltaForEndpoint(which, buttonDelta),
  );
}

/**
 * Set one endpoint from a rail click/drag, pinning the other end. `sec` is the
 * pointer's second on the rail. This is the same pin shape the main preview's
 * needle drag uses (clampTrimEndpoints `move`).
 *
 * The `move` branches read a DIFFERENT raw slot each: 'in' reads rawStart,
 * 'out' reads rawEnd. Feeding the same two numbers to both would silently
 * ignore the pointer on the out-needle, so each side passes `sec` into the
 * slot its branch actually consumes.
 */
export function miniEditorRangeFromRail(
  range: MiniEditorRange,
  durationSec: number,
  which: 'in' | 'out',
  sec: number,
): MiniEditorRange {
  const dur = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  if (dur <= 0) return { start: 0, end: 0 };
  const pin = { fixedStart: range.start, fixedEnd: range.end };
  return which === 'in'
    ? clampTrimEndpoints(sec, range.end, dur, range.start, range.end, { move: 'in', ...pin })
    : clampTrimEndpoints(range.start, sec, dur, range.start, range.end, { move: 'out', ...pin });
}

/** Snap a needle to a boundary with Home/End (0 / duration). */
export function miniEditorRangeToEdge(
  range: MiniEditorRange,
  durationSec: number,
  which: 'in' | 'out',
  edge: 'start' | 'end',
): MiniEditorRange {
  const dur = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  if (dur <= 0) return { start: 0, end: 0 };
  return miniEditorRangeFromRail(range, dur, which, edge === 'start' ? 0 : dur);
}

/**
 * Keyboard step for a needle key. ArrowLeft/ArrowRight move by ±step (which
 * endpoint moves depends on the needle: the 'in' needle grows leftwards, the
 * 'out' needle rightwards, so a single Left key widens the cut). Home/End snap
 * to the media bounds. Returns null for any other key.
 */
export function miniEditorKeyDelta(
  e: { key: string; shiftKey?: boolean },
  which: 'in' | 'out',
): { deltaSec: number } | { edge: 'start' | 'end' } | null {
  const step = e.shiftKey ? MINI_EDITOR_KEY_STEP_FAST_SEC : MINI_EDITOR_KEY_STEP_SEC;
  // 'in' moves against the direction (its +delta extends leftwards).
  const dir = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
  if (dir !== 0) return { deltaSec: which === 'in' ? -dir * step : dir * step };
  if (e.key === 'Home') return { edge: 'start' };
  if (e.key === 'End') return { edge: 'end' };
  return null;
}

/** Why the local cut cannot start yet, else null. */
export function miniEditorDownloadError(range: MiniEditorRange, durationSec: number): string | null {
  const len = miniEditorRangeLength(range);
  if (!(durationSec > 0)) return 'Media duration unknown — cannot cut yet';
  if (len <= 0) return 'Select a cut range first';
  return null;
}

/** Why this range cannot become a Twitch clip, else null (reuses twitchClip). */
export function miniEditorTwitchRangeError(range: MiniEditorRange): string | null {
  return twitchClipDurationError(miniEditorRangeLength(range));
}

export interface MiniEditorTwitchTarget {
  platform: string | null;
  channel?: string | null;
  videoId?: string | null;
}

/** Why Twitch clipping is unavailable for this media, else null. */
export function miniEditorTwitchTargetError(t: MiniEditorTwitchTarget): string | null {
  if (t.platform !== 'twitch') return 'Twitch clips need a Twitch VOD';
  if (!t.channel) return 'Channel login missing — cannot open the Twitch editor';
  if (!t.videoId) return 'Not a Twitch VOD URL';
  return null;
}

export interface MiniEditorDownloadInput {
  url: string;
  range: MiniEditorRange;
  title?: string | null;
  channel?: string | null;
  durationSec?: number | null;
  quality?: string;
  /** Default true: a trimmed cut ships its SRT (rebased by crop_start). */
  includeTranscript?: boolean;
  chatStartSec?: number | null;
  chatEndSec?: number | null;
  includeChat?: boolean;
}

/**
 * Body for POST /api/download/clip.
 *
 * crop_start/crop_end are the trimmed window; the backend slices the media to
 * them AND hands the same pair to download_sidecars, which rebases every SRT
 * cue by crop_start. Dropping crop_start here is what makes a trimmed clip's
 * subtitle track start at the source-VOD time and drift out of sync — so the
 * pair is always sent together, never one without the other.
 */
export function miniEditorDownloadBody(input: MiniEditorDownloadInput): Record<string, unknown> {
  const start = Math.max(0, Math.floor(input.range?.start ?? 0));
  const end = Math.max(start + 1, Math.ceil(input.range?.end ?? start + 1));
  const body: Record<string, unknown> = {
    url: input.url,
    quality: input.quality || 'source',
    crop_start: start,
    crop_end: end,
    include_transcript: input.includeTranscript !== false,
  };
  if (input.title) body.title = input.title;
  if (input.channel) body.channel = input.channel;
  if (Number.isFinite(input.durationSec) && (input.durationSec as number) > 0) {
    body.duration = input.durationSec;
  }
  if (input.includeChat) {
    body.include_chat = true;
    if (input.chatStartSec != null) body.chat_start_sec = input.chatStartSec;
    if (input.chatEndSec != null) body.chat_end_sec = input.chatEndSec;
  }
  return body;
}

/** The editable length window of a selection, for the ±1s/±5s buttons. */
export const MINI_EDITOR_NUDGE_BUTTONS = [-5, -1, 1, 5] as const;

export { TWITCH_CLIP_MIN_SEC, TWITCH_CLIP_MAX_SEC };
