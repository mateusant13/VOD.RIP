/** Client half of the archive queue's USER FOCUS signal.
 *
 * The backend already implements the pause rule end to end:
 *   POST /api/archive/focus  ->  user_focus row (routers/archive.py)
 *   queue_policy.active_focus()  ->  reads it, TTL FOCUS_TTL_S, lazy prune
 *   archive_transcribe._claim_next_job()  ->  while a focus is live only the
 *                                           FOCUSED VOD's transcribe job is
 *                                           claimable; chat/events keep draining
 * (That is the owner's "pause the others when the user interacts with one".)
 *
 * Nothing outside the test suite ever WROTE that row, so in the real app
 * active_focus() was always None and the queue was never paused. This module
 * is the missing production writer: it follows the player popup the user is
 * actually watching.
 *
 * The pair MUST be the archive's native ids (archive DB videos.platform /
 * videos.video_id). A URL-derived or synthetic id would focus a pair that no
 * transcribe job carries — and since the claim gate matches on
 * (platform, video_id), that pauses EVERYTHING instead of the intended VOD.
 * So every stamp is guarded by the same native-id test the SEARCH THIS VIDEO
 * button uses (archiveScope.isNativeArchiveVideoId) and dropped silently when
 * it cannot be trusted. Sending a bad pair is worse than sending nothing: the
 * endpoint treats an unusable pair as "release".
 */
import { useEffect, useRef } from 'react';
import { apiPost } from './hooks/useApiClient';
import { isNativeArchiveVideoId } from './archiveScope';

/** Mirrors queue_policy.FOCUS_TTL_S — the backend ignores a focus record older
 *  than this, so a crashed renderer cannot wedge the queue. */
const FOCUS_TTL_S = 300;
/** Refresh well inside the TTL so a long watch outlives it: 2 stamps fit in
 *  every window, so one dropped/failed call cannot expire a live focus. */
const REFRESH_MS = Math.floor(FOCUS_TTL_S * 1000 * 0.4);

const FOCUS_PATH = '/api/archive/focus';

/** Archive platforms the backend accepts (archive_db.PLATFORMS). */
export type ArchiveFocusPlatform = 'youtube' | 'twitch' | 'kick';

const FOCUS_PLATFORMS: readonly string[] = ['youtube', 'twitch', 'kick'];

/** Lowercase the popup's display platform ('Twitch', 'YouTube', 'Kick') into
 *  the backend's wire form, or null when it is not an archive platform. */
export function normalizeArchivePlatform(raw: string | null | undefined): ArchiveFocusPlatform | null {
  const p = (raw || '').trim().toLowerCase();
  return (FOCUS_PLATFORMS as readonly string[]).includes(p) ? (p as ArchiveFocusPlatform) : null;
}

/** The stampable focus key for one open item, or null when it is not an
 *  archive VOD we can safely focus (local file, live, synthetic, unknown). */
export function archiveFocusKey(
  platform: string | null | undefined,
  videoId: string | null | undefined,
): string | null {
  const plat = normalizeArchivePlatform(platform);
  if (!plat || !isNativeArchiveVideoId(videoId)) return null;
  return `${plat}:${normalizeArchiveVideoId(plat, videoId)}`;
}

/** Twitch VOD ids arrive 'v'-prefixed from the API while the archive stores the
 *  bare digits (same normalization the popup vod does at App.tsx openExplorePlayer).
 *  Normalizing here too means a caller that forgets cannot focus a pair no job
 *  carries — which would pause the ENTIRE queue instead of one VOD. */
function normalizeArchiveVideoId(platform: ArchiveFocusPlatform, videoId: string): string {
  if (platform === 'twitch' && /^v\d/i.test(videoId)) return videoId.slice(1);
  return videoId;
}

/** Fire-and-forget by design: a focus stamp must never surface an error into
 *  playback. The queue simply does not pause if the call does not land. */
async function post(body: Record<string, unknown>): Promise<void> {
  try {
    await apiPost(FOCUS_PATH, body);
  } catch {
    /* ignore — pausing the queue is best-effort */
  }
}

export function stampArchiveFocus(key: string): void {
  const sep = key.indexOf(':');
  void post({ platform: key.slice(0, sep), video_id: key.slice(sep + 1) });
}

/** Empty body = "the user navigated away" (routers/archive.py). */
export function releaseArchiveFocus(): void {
  void post({});
}

/** Minimal shape this module needs from an open player — keeps it independent
 *  of the popup component so it can be reasoned about (and tested) alone. */
export type ArchiveFocusCandidate = {
  id: string;
  platform?: string | null;
  videoId?: string | null;
};

/**
 * Keep the backend's focus record pointed at whichever popup is frontmost,
 * for as long as the user is watching it.
 *
 * `zOrder` is App.tsx's popup ladder (rank per popup id, re-assigned on
 * pointer-down), so "frontmost" tracks the window the user actually clicked up
 * — not merely the last one opened.
 *
 * Three effects, deliberately: the stamp/release effect must NOT clean up on
 * every key change (a release-then-stamp race would briefly unpause the whole
 * queue between two popups), so release lives in its own effect keyed on the
 * now-null case plus unmount, and the heartbeat is separate again.
 */
export function useArchiveFocusSignal(
  candidates: readonly ArchiveFocusCandidate[],
  zOrder: Record<string, number>,
): void {
  let frontKey: string | null = null;
  let frontRank = -Infinity;
  for (const c of candidates) {
    const rank = zOrder[c.id] ?? -Infinity;
    if (rank > frontRank) {
      frontRank = rank;
      frontKey = archiveFocusKey(c.platform, c.videoId);
    }
  }
  const key = frontRank === -Infinity ? null : frontKey;

  const liveKeyRef = useRef<string | null>(null);

  // 1. Point the record at the new target (no cleanup — see docstring).
  useEffect(() => {
    if (liveKeyRef.current === key) return;
    liveKeyRef.current = key;
    if (key) stampArchiveFocus(key);
    else releaseArchiveFocus();
  }, [key]);

  // 2. A long watch outlives the TTL; refresh so the pause outlives it too.
  useEffect(() => {
    if (!key) return;
    const timer = window.setInterval(() => stampArchiveFocus(key), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [key]);

  // 3. Leaving the app releases immediately rather than parking the queue
  //    until the TTL expires.
  useEffect(
    () => () => {
      liveKeyRef.current = null;
      releaseArchiveFocus();
    },
    [],
  );
}
