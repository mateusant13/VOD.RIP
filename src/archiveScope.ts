/**
 * Pure helpers for per-video archive search scoping.
 * No DOM, no network — unit-testable.
 */

/**
 * Native archive video id for a video URL, or null when there is none or the
 * URL is ambiguous. Mirrors the archive DB's video_id formats:
 *   - YouTube  → the v= / youtu.be / shorts id
 *   - Twitch   → numeric VOD id (no 'v' prefix)
 *   - Kick     → the /videos/ uuid segment
 * Clips, channel pages, live pages and bare pastes → null (not archive-able
 * or ambiguous), so callers hide the SEARCH THIS VIDEO button.
 */
export function archiveVideoIdFromUrl(rawUrl: string): string | null {
  const trimmed = (rawUrl || '').trim();
  if (!trimmed) return null;
  let u: URL;
  try {
    u = new URL(trimmed);
  } catch {
    return null;
  }
  const host = u.hostname.toLowerCase();
  if (host === 'youtu.be') {
    const id = u.pathname.split('/').filter(Boolean)[0] ?? '';
    return /^[\w-]{6,}$/.test(id) ? id : null;
  }
  if (host === 'youtube.com' || host.endsWith('.youtube.com')) {
    const v = u.searchParams.get('v');
    if (v && /^[\w-]{6,}$/.test(v)) return v;
    const m = u.pathname.match(/^\/(?:shorts|embed)\/([\w-]{6,})/);
    return m ? m[1] : null;
  }
  if (host === 'twitch.tv' || host.endsWith('.twitch.tv')) {
    const m = u.pathname.match(/^\/videos\/(\d+)/);
    return m ? m[1] : null;
  }
  if (host === 'kick.com' || host.endsWith('.kick.com')) {
    const m = u.pathname.match(/^\/[^/]+\/videos\/([^/]+)/);
    return m ? m[1] : null;
  }
  return null;
}

/**
 * True when an /api/info id is a native video id (not the synthetic
 * skipNetwork fallback, which stores the full URL).
 */
export function isNativeArchiveVideoId(id: string | null | undefined): id is string {
  if (!id) return false;
  return !/^(https?:)?\/\//i.test(id) && !id.includes('/');
}
