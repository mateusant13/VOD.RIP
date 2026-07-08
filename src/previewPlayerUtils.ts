import { apiPost } from './hooks/useApiClient';
import { detectUrlPlatform, isClipUrl } from './channelUtils';
import type { PreviewSessionStatusResponse } from './types';

const _warmInflight = new Set<string>();
const _warmTimers = new Map<string, number>();

/** YouTube watch URL → 11-char video id (explore iframe fast path). */
export function youtubeVideoIdFromUrl(url: string): string | null {
  const m = url.trim().match(/(?:[?&]v=|youtu\.be\/|\/shorts\/|\/live\/|\/embed\/)([a-zA-Z0-9_-]{11})/);
  return m?.[1] ?? null;
}

/** ponytail: channel explore YouTube — iframe beats DASH segment mux for startup SLA */
export function youtubeEmbedSrc(videoId: string, startSec = 0): string {
  const q = new URLSearchParams({
    autoplay: '1',
    mute: '1',
    start: String(Math.max(0, Math.floor(startSec))),
    rel: '0',
    modestbranding: '1',
    playsinline: '1',
  });
  return `https://www.youtube-nocookie.com/embed/${videoId}?${q}`;
}

/** Debounced fire-and-forget InnerTube cache warm (hover, URL paste, preview intent). */
export function warmYoutubePreview(url: string, delayMs = 0): void {
  const trimmed = url.trim();
  if (!trimmed || detectUrlPlatform(trimmed) !== 'youtube' || isClipUrl(trimmed)) return;
  if (_warmInflight.has(trimmed)) return;

  const existing = _warmTimers.get(trimmed);
  if (existing != null) window.clearTimeout(existing);

  const run = () => {
    _warmTimers.delete(trimmed);
    if (_warmInflight.has(trimmed)) return;
    _warmInflight.add(trimmed);
    void apiPost<{ warmed?: boolean }>('/api/preview/warm', { url: trimmed })
      .catch(() => {})
      .finally(() => { _warmInflight.delete(trimmed); });
  };

  if (delayMs <= 0) run();
  else _warmTimers.set(trimmed, window.setTimeout(run, delayMs));
}

/** Stagger warm for first N YouTube URLs (channel list load / filter change). */
export function warmYoutubePreviewBatch(urls: string[], max = 6, staggerMs = 90): void {
  let n = 0;
  for (const raw of urls) {
    if (n >= max) break;
    const trimmed = raw.trim();
    if (!trimmed || detectUrlPlatform(trimmed) !== 'youtube' || isClipUrl(trimmed)) continue;
    warmYoutubePreview(trimmed, n * staggerMs);
    n += 1;
  }
}

/** Warm YouTube rows as they scroll into the channel list viewport. */
export function bindYoutubeChannelScrollWarm(
  scrollRoot: Element | null,
  rowNodes: HTMLElement[],
): () => void {
  if (!scrollRoot || typeof IntersectionObserver === 'undefined') return () => {};
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const url = (entry.target as HTMLElement).dataset.youtubeWarm;
        if (url) warmYoutubePreview(url);
      }
    },
    { root: scrollRoot, rootMargin: '240px 0px', threshold: 0.01 },
  );
  for (const el of rowNodes) observer.observe(el);
  return () => observer.disconnect();
}

export const PREVIEW_MAIN_DEFAULT_HEIGHT = 720;
export const PREVIEW_EXPLORE_DEFAULT_HEIGHT = 720;
export const PREVIEW_CLIP_DEFAULT_HEIGHT = 360;
/** Default YouTube preview tier — user can raise in preview quality menu. */
export const PREVIEW_YOUTUBE_DEFAULT_HEIGHT = 720;
/** Max prefer_height when variant list unknown. */
export const PREVIEW_YOUTUBE_PREFER_HEIGHT = 1080;
/** Debounce resize-driven POST /api/preview/session/.../quality (viewport cap changes). */
export const VIEWPORT_PREVIEW_QUALITY_DEBOUNCE_MS = 450;
export const VIEWPORT_PREVIEW_FULLSCREEN_DEBOUNCE_MS = 120;

export function isYouTubePreviewPlatform(platform: string | null | undefined): boolean {
  return (platform || '').toLowerCase() === 'youtube';
}

/** Validate URL protocol — only https, http, blob:, and relative proxy paths are allowed. */
export function isValidPreviewUrl(u: string): boolean {
  if (u.startsWith('/')) return true;
  try {
    const parsed = new URL(u);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' || parsed.protocol === 'blob:';
  } catch { return false; }
}

/** Extract hostname from a URL, falling back to the raw string on parse failure. */
export function hostnameFromUrl(u: string): string {
  try { return new URL(u).hostname; } catch { return u; }
}

export function isClipPreviewUrl(u: string): boolean {
  const host = hostnameFromUrl(u);
  const lower = u.toLowerCase();
  if (host === 'clips.twitch.tv') return true;
  if ((host === 'www.twitch.tv' || host === 'twitch.tv' || host.endsWith('.twitch.tv')) && lower.includes('/clip/')) return true;
  if ((host === 'kick.com' || host === 'www.kick.com') && lower.includes('/clips/')) return true;
  return false;
}

/** Normalize preview session response — Twitch clips are progressive MP4, not HLS. */
export function resolvePreviewPlayback(
  pageUrl: string,
  session: { playback_url?: string; master_url: string; kind?: string },
): { url: string; kind: 'hls' | 'progressive' } {
  const progressive = isClipPreviewUrl(pageUrl) || session.kind === 'progressive';
  const url = session.playback_url || session.master_url;
  return { url, kind: progressive ? 'progressive' : 'hls' };
}

export type PreviewMuxPollSignal = { gen: number; current: number };

/** Scale mux poll timeout with trim length (full-file mux is proportional to duration). */
export function previewMuxPollMaxMs(cropStart: number, cropEnd: number): number {
  const sec = Math.max(1, cropEnd - cropStart);
  return Math.min(15 * 60 * 1000, Math.max(45_000, sec * 2000));
}

/** Window-HLS: playlist becomes safe to attach within a few seconds — cap wait short. */
export function previewPlaylistPollMaxMs(): number {
  return 15_000;
}

/** ponytail: legacy dash-segment mux poll cap — backend no longer muxes per segment. */
export function previewDashMuxPollMaxMs(): number {
  return 0;
}

/** Debounce trim-slider / scrub seeks so HLS fragment requests are not stamped on every mousemove. */
export const PREVIEW_SEEK_DEBOUNCE_MS = 200;

/**
 * ponytail: deprecated — backend no longer muxes per-segment DASH segments.
 * Kept as a no-op so legacy callers compile but no network call is dispatched.
 * Use absolute VOD seek + client-side clamp on window-HLS instead.
 */
export function prewarmPreviewDashSeek(
  _sessionId: string,
  _positionSec: number,
  _apiPost: <T>(path: string, body?: unknown) => Promise<T>,
): void {
  /* no-op: backend removed per-segment DASH mux */
}

/** Stagger explore-popup YouTube sessions so ffmpeg mux does not stampede. */
export const YOUTUBE_EXPLORE_SESSION_STAGGER_MS = 0;

export function youtubeExploreSessionStaggerMs(stackIndex: number): number {
  return Math.max(0, stackIndex) * YOUTUBE_EXPLORE_SESSION_STAGGER_MS;
}

/** Debounced waiting/playing hooks — avoids spinner flicker on sub-200ms gaps. */
export function attachPreviewBufferingListeners(
  video: HTMLVideoElement,
  onBuffering: (buffering: boolean) => void,
): () => void {
  let stallTimer: number | undefined;
  const clearStallTimer = () => {
    if (stallTimer !== undefined) {
      window.clearTimeout(stallTimer);
      stallTimer = undefined;
    }
  };
  const onWaiting = () => {
    clearStallTimer();
    stallTimer = window.setTimeout(() => onBuffering(true), 180);
  };
  const onResume = () => {
    clearStallTimer();
    onBuffering(false);
  };
  video.addEventListener('waiting', onWaiting);
  video.addEventListener('playing', onResume);
  video.addEventListener('canplay', onResume);
  return () => {
    clearStallTimer();
    video.removeEventListener('waiting', onWaiting);
    video.removeEventListener('playing', onResume);
    video.removeEventListener('canplay', onResume);
  };
}

/**
 * Poll the preview session until playlist/seg0 is ready (window-HLS path) or
 * the full file is muxed (progressive / very-long-window fallback).
 * Backend removed per-segment DASH mux — we never wait on individual segments.
 */
export async function waitForPreviewMuxReady(
  sessionId: string,
  apiGet: <T>(path: string) => Promise<T>,
  signal?: PreviewMuxPollSignal,
  maxMs?: number,
): Promise<boolean> {
  const deadline = Date.now() + (maxMs ?? previewPlaylistPollMaxMs());
  const pollMs = 150;
  while (Date.now() < deadline) {
    if (signal && signal.gen !== signal.current) return false;
    try {
      const st = await apiGet<PreviewSessionStatusResponse>(
        `/api/preview/session/${sessionId}/status`,
      );
      // Short-circuit on playlist / first-segment cache — never wait for full mux.
      if (st.playlist_ready === true || st.segment_buffer_ready === true) return true;
      if (st.mux_ready) return true;
    } catch {
      /* keep polling until timeout */
    }
    await new Promise<void>((resolve) => { window.setTimeout(resolve, pollMs); });
  }
  return false;
}

/**
 * Decide whether to block session create on a readiness poll.
 *
 * - Already attachable (playlist_ready / segment_buffer_ready / mux_ready) → no wait.
 * - Window-HLS (trim_timeline=true): playlist attaches immediately; poll seg0 only.
 * - Muxed CDN HLS (trim_timeline=false): poll playlist/seg0, not full-window mux.
 * - Progressive: wait for the underlying full-file mux.
 *
 * Backend removed per-segment DASH mux, so the old "wait for full mux on HLS"
 * path is intentionally gone — we only wait for playlist/seg0.
 */
export function shouldWaitForPreviewMux(
  res: {
    playlist_ready?: boolean;
    segment_buffer_ready?: boolean;
    trim_timeline?: boolean;
    mux_ready?: boolean;
  },
  playbackKind: 'hls' | 'progressive',
): boolean {
  if (
    res.playlist_ready === true
    || res.segment_buffer_ready === true
    || res.mux_ready === true
  ) {
    return false;
  }
  if (res.trim_timeline) return false;
  // Window-HLS: still wait for playlist/seg0 (caller polls via waitForPreviewMuxReady).
  // Progressive: wait for full-file mux readiness.
  return playbackKind === 'hls' || playbackKind === 'progressive';
}

/** Append prefer_height (and cache-bust) so the proxy switches tier on the same request. */
export function previewUrlWithPreferHeight(baseUrl: string, height: number): string {
  try {
    const u = new URL(baseUrl, window.location.origin);
    u.searchParams.set('prefer_height', String(height));
    u.searchParams.set('t', String(Date.now()));
    return `${u.pathname}${u.search}`;
  } catch {
    const sep = baseUrl.includes('?') ? '&' : '?';
    return `${baseUrl}${sep}prefer_height=${height}&t=${Date.now()}`;
  }
}

/** Strip cache-bust only — keep prefer_height so tier changes still reload. */
export function progressivePlaybackUrlKey(playbackUrl: string): string {
  try {
    const u = new URL(playbackUrl, 'http://vod-rip.local');
    u.searchParams.delete('t');
    return `${u.pathname}${u.search}`;
  } catch {
    return playbackUrl.replace(/([?&])t=\d+(&?)/, (_m, lead, tail) => (tail ? lead : ''));
  }
}

/** Unmute + play; falls back to muted autoplay when the browser blocks sound. */
export async function playPreviewWithAudio(
  video: HTMLVideoElement,
  setMuted: (muted: boolean) => void,
  volume = 0.1,
): Promise<void> {
  video.muted = false;
  video.volume = volume;
  setMuted(false);
  try {
    await video.play();
    if (video.muted) setMuted(true);
  } catch {
    video.muted = true;
    setMuted(true);
    await video.play().catch(() => {});
  }
}

/** Call on click/key — unlocks audio after muted autoplay (explore popup loads async). */
export function unlockPreviewAudioFromGesture(
  video: HTMLVideoElement,
  setMuted: (muted: boolean) => void,
  volume = 0.1,
): void {
  video.muted = false;
  video.volume = Math.max(volume, 0.05);
  setMuted(false);
}

/** Load proxied MP4 into a <video> — use <source type="video/mp4"> so .m3u8 paths still play. */
export function attachProgressivePreview(
  video: HTMLVideoElement,
  playbackUrl: string,
  startTime?: number,
  cacheBust = false,
): void {
  if (!isValidPreviewUrl(playbackUrl)) {
    throw new Error(`Blocked playback URL with disallowed protocol: ${playbackUrl.slice(0, 80)}`);
  }
  const src = cacheBust
    ? `${playbackUrl}${playbackUrl.includes('?') ? '&' : '?'}_=${Date.now()}`
    : playbackUrl;
  const existingSource = video.querySelector('source');
  const existingUrl = existingSource?.getAttribute('src') ?? video.currentSrc;
  if (
    !cacheBust
    && existingUrl
    && progressivePlaybackUrlKey(existingUrl) === progressivePlaybackUrlKey(playbackUrl)
    && video.readyState >= HTMLMediaElement.HAVE_METADATA
  ) {
    if (startTime != null && Number.isFinite(startTime) && Math.abs(video.currentTime - startTime) > 0.25) {
      video.currentTime = startTime;
    }
    return;
  }
  video.innerHTML = '';
  video.removeAttribute('src');
  const source = document.createElement('source');
  source.src = src;
  source.type = 'video/mp4';
  video.appendChild(source);
  video.load();
  if (startTime != null && Number.isFinite(startTime)) {
    const seek = () => {
      video.currentTime = startTime;
    };
    video.addEventListener('loadedmetadata', seek, { once: true });
    requestAnimationFrame(() => {
      if (video.readyState >= HTMLMediaElement.HAVE_METADATA) seek();
    });
  }
}

export function detachProgressivePreview(video: HTMLVideoElement): void {
  video.pause();
  video.innerHTML = '';
  video.removeAttribute('src');
  video.load();
}

/** Muxed trim-window MP4 uses a 0-based timeline; full VOD progressive URLs do not. */
export function isClipRelativePreviewDuration(
  videoDurationSec: number,
  vodDurationSec: number,
  clipLengthSec: number,
): boolean {
  if (!Number.isFinite(videoDurationSec) || videoDurationSec <= 0) return false;
  if (!Number.isFinite(vodDurationSec) || vodDurationSec <= 0) return false;
  if (videoDurationSec >= vodDurationSec * 0.98) return false;
  if (!Number.isFinite(clipLengthSec) || clipLengthSec <= 0) return false;
  return Math.abs(videoDurationSec - clipLengthSec) <= Math.max(3, clipLengthSec * 0.08);
}

/** Prefer <video> duration when API metadata is off (common on YouTube progressive). */
export function resolvePreviewDurationSec(
  mediaDurationSec: number,
  vodDurationSec: number,
): number {
  if (mediaDurationSec > 0 && vodDurationSec > 0) {
    if (Math.abs(mediaDurationSec - vodDurationSec) > 3) return mediaDurationSec;
  }
  return mediaDurationSec > 0 ? mediaDurationSec : vodDurationSec;
}

/**
 * Client-side trim clamp for the window-HLS path (trim_timeline=false).
 *
 * Window-HLS exposes the full VOD timeline — trim is enforced by seeking into
 * [start, end] and clamping the player back if it drifts. This is a no-op when
 * the HLS path uses clip-relative timing (trim_timeline=true / muxed clip MP4),
 * where the timeline itself is already 0-based from crop_start.
 */
export function clampPreviewTimeToVodTrim(
  video: HTMLVideoElement,
  trimStartSec: number,
  trimEndSec: number,
  clipRelative: boolean,
): { applied: boolean; paused: boolean; vodTime: number } {
  if (!Number.isFinite(trimStartSec) || !Number.isFinite(trimEndSec)) {
    return { applied: false, paused: false, vodTime: video.currentTime };
  }
  if (trimEndSec <= trimStartSec) {
    return { applied: false, paused: false, vodTime: video.currentTime };
  }
  const cur = Number.isFinite(video.currentTime) ? video.currentTime : 0;
  if (clipRelative) {
    const clipLen = Math.max(0, trimEndSec - trimStartSec);
    let t = cur;
    if (t < 0) t = 0;
    else if (t > clipLen) t = clipLen;
    const vodTime = trimStartSec + t;
    const applied = Math.abs(video.currentTime - t) > 0.05;
    if (applied) video.currentTime = t;
    let paused = false;
    if (t >= clipLen - 0.05) {
      video.pause();
      paused = true;
    }
    return { applied, paused, vodTime };
  }
  // Window-HLS: currentTime is absolute VOD time.
  let t = cur;
  if (t < trimStartSec) t = trimStartSec;
  else if (t > trimEndSec) t = trimEndSec;
  const applied = Math.abs(video.currentTime - t) > 0.05;
  if (applied) video.currentTime = t;
  let paused = false;
  if (t >= trimEndSec - 0.05) {
    video.pause();
    paused = true;
  }
  return { applied, paused, vodTime: t };
}

export type ProgressivePreviewRecoveryOpts = {
  video: HTMLVideoElement;
  playbackUrl: string;
  /** ponytail: ref getter — React state is stale when the preview effect first runs. */
  getSessionId: () => string | null;
  youtube: boolean;
  extractSource?: string;
  getResumeSec: () => number;
  apiPost: <T>(path: string, body: unknown) => Promise<T>;
  onRefreshing?: () => void;
  onFatal?: () => void;
  maxRetries?: number;
};

/** Spurious errors while swapping <source> or calling video.load() — not user-visible failures. */
export function isIgnorableProgressivePreviewError(video: HTMLVideoElement): boolean {
  const code = video.error?.code;
  if (code === MediaError.MEDIA_ERR_ABORTED) return true;
  if (!video.currentSrc && video.readyState === HTMLMediaElement.HAVE_NOTHING) return true;
  if (video.networkState === HTMLMediaElement.NETWORK_EMPTY) return true;
  return false;
}

/** Retry progressive preview on CDN 416/expiry — logs extract_source for debugging. */
export function bindProgressivePreviewRecovery(
  opts: ProgressivePreviewRecoveryOpts,
): () => void {
  let retries = 0;
  const max = opts.maxRetries ?? 4;

  const onError = () => {
    if (isIgnorableProgressivePreviewError(opts.video)) return;
    const code = opts.video.error?.code;
    const sessionId = opts.getSessionId();
    console.warn('[VOD.RIP preview] progressive error', {
      code,
      extractSource: opts.extractSource ?? 'unknown',
      retries,
      sessionId,
    });
    if (opts.youtube && sessionId && retries < max) {
      retries += 1;
      opts.onRefreshing?.();
      const resume = opts.getResumeSec();
      void opts.apiPost<{ extract_source?: string }>(
        `/api/preview/session/${sessionId}/refresh`,
        {},
      ).then((res) => {
        const src = res?.extract_source ?? opts.extractSource;
        if (src) console.info('[VOD.RIP preview] refresh extract_source=', src);
        attachProgressivePreview(opts.video, opts.playbackUrl, resume, true);
        void opts.video.play().catch(() => {});
      }).catch((err: unknown) => {
        console.warn('[VOD.RIP preview] refresh failed', err);
        if (retries < max) {
          const resume = opts.getResumeSec();
          attachProgressivePreview(opts.video, opts.playbackUrl, resume, true);
          void opts.video.play().catch(() => {});
        } else {
          opts.onFatal?.();
        }
      });
      return;
    }
    opts.onFatal?.();
  };

  opts.video.addEventListener('error', onError);
  return () => opts.video.removeEventListener('error', onError);
}

export function channelSlugFromMediaUrl(u: string): string | null {
  const host = hostnameFromUrl(u);
  if (host === 'kick.com' || host === 'www.kick.com') {
    const kick = u.match(/kick\.com\/([^/?#]+)/i);
    if (kick && !['videos', 'clips'].includes(kick[1].toLowerCase())) return kick[1];
  }
  if (host === 'www.twitch.tv' || host === 'twitch.tv' || host.endsWith('.twitch.tv')) {
    const tw = u.match(/twitch\.tv\/([^/?#]+)/i);
    if (tw && !['videos', 'clip', 'directory', 'clips'].includes(tw[1].toLowerCase())) return tw[1];
  }
  return null;
}

const PREVIEW_HEIGHT_STEPS = [240, 360, 480, 720, 1080] as const;

/** Snap CSS player height to a stream tier — avoids fetching 1080p for a tiny panel. */
export function snapPreviewHeight(cssPx: number): number {
  if (!Number.isFinite(cssPx) || cssPx <= 0) return PREVIEW_CLIP_DEFAULT_HEIGHT;
  for (const step of PREVIEW_HEIGHT_STEPS) {
    if (cssPx <= step + 48) return step;
  }
  return 1080;
}

/** Max stream height that matches the on-screen player box (logical CSS pixels). */
export function measurePlayerHeightCap(element: HTMLElement | null, aspect = 16 / 9): number {
  if (!element) return PREVIEW_MAIN_DEFAULT_HEIGHT;
  const r = element.getBoundingClientRect();
  let h = r.height;
  if (h < 48 && r.width > 0) h = r.width / aspect;
  return snapPreviewHeight(h);
}

export type PreviewPreferHeightOpts = {
  youtube?: boolean;
  variantHeights?: number[];
  qualityLabels?: string[];
  activeHeight?: number;
};

/** Highest tier from API hints, else 1080p request for the preview session. */
export function maxAvailablePreviewHeight(
  variantHeights?: number[] | null,
  qualityLabels?: string[] | null,
): number {
  const heights = mergeVariantHeights(
    variantHeights ?? undefined,
    parseQualityHeights(qualityLabels ?? []),
  );
  return heights.length ? heights[heights.length - 1] : PREVIEW_YOUTUBE_PREFER_HEIGHT;
}

/** First preview load: YouTube → 720p default (raise in player); Kick/Twitch → capped to player. */
export function initialPreviewPreferHeight(
  isClip: boolean,
  playerCap: number,
  opts?: PreviewPreferHeightOpts,
): number {
  if (opts?.youtube) {
    if (opts.activeHeight && opts.activeHeight > 0) return opts.activeHeight;
    const max = maxAvailablePreviewHeight(opts.variantHeights, opts.qualityLabels);
    return Math.min(PREVIEW_YOUTUBE_DEFAULT_HEIGHT, max || PREVIEW_YOUTUBE_DEFAULT_HEIGHT);
  }
  const desired = isClip ? PREVIEW_CLIP_DEFAULT_HEIGHT : PREVIEW_MAIN_DEFAULT_HEIGHT;
  return Math.min(desired, playerCap);
}

/** HLS manifest default tier — YouTube 720p default; others cap to viewport. */
export function resolveInitialHlsPreviewHeight(
  isClip: boolean,
  playerCap: number,
  opts?: PreviewPreferHeightOpts,
): number {
  if (opts?.youtube) {
    if (opts.activeHeight && opts.activeHeight > 0) return opts.activeHeight;
    const max = maxAvailablePreviewHeight(opts.variantHeights, opts.qualityLabels);
    return Math.min(PREVIEW_YOUTUBE_DEFAULT_HEIGHT, max || PREVIEW_YOUTUBE_DEFAULT_HEIGHT);
  }
  return Math.min(initialPreviewPreferHeight(isClip, playerCap), playerCap);
}

/** Stream height to fetch: full request in fullscreen, capped to player size otherwise. */
export function effectivePreviewHeight(
  requestedHeight: number,
  playerCap: number,
  fullscreen: boolean,
): number {
  if (!Number.isFinite(requestedHeight) || requestedHeight <= 0) {
    return fullscreen ? 1080 : playerCap;
  }
  if (fullscreen) return requestedHeight;
  return Math.min(requestedHeight, playerCap);
}

/** Pick the highest available tier that does not exceed the target height. */
export function snapHeightToTier(heights: number[], target: number): number {
  const tiers = [...new Set(heights.filter((h) => h > 0))].sort((a, b) => a - b);
  if (!tiers.length) return target;
  const atOrBelow = tiers.filter((h) => h <= target);
  return atOrBelow.length ? atOrBelow[atOrBelow.length - 1] : tiers[0];
}

export function playbackHeightFromRequest(
  requestedHeight: number,
  availableHeights: number[],
  playerCap: number,
  fullscreen: boolean,
): number {
  const effective = effectivePreviewHeight(requestedHeight, playerCap, fullscreen);
  return snapHeightToTier(availableHeights, effective);
}

export function parseQualityHeights(qualities: string[]): number[] {
  const heights = qualities
    .map((q) => {
      const m = q.match(/(\d+)/);
      return m ? parseInt(m[1], 10) : 0;
    })
    .filter((h) => h > 0);
  return [...new Set(heights)].sort((a, b) => a - b);
}

/** Quality menu entries; highest available height is labelled source/… */
export function mapHeightsToPreviewLevels(heights: number[]): PreviewLevelOption[] {
  if (!heights.length) return [];
  const maxH = Math.max(...heights);
  return heights.map((height, index) => ({
    index,
    height,
    label: previewLevelLabel(height, undefined, height === maxH),
  }));
}

/** Highest quality label for download / URL info — never tied to preview playback. */
export function maxQualityLabelFromList(qualities: string[]): string {
  const heights = parseQualityHeights(qualities);
  if (heights.length) return `${heights[heights.length - 1]}p`;
  return '1080p';
}

function _fmtTrimSec(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m > 0) return `${m.toString().padStart(2, '0')}m${r.toString().padStart(2, '0')}s`;
  return `${r.toString().padStart(2, '0')}s`;
}

function _trimRangeTag(cropStart: number | null | undefined, cropEnd: number | null | undefined): string {
  if (cropStart == null && cropEnd == null) return '';
  const start = _fmtTrimSec(cropStart ?? 0);
  const end = _fmtTrimSec(cropEnd ?? (cropStart ?? 0) + 1);
  return `${start}-${end}`;
}

function _durationTag(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return 'clip';
  const sec = Math.max(1, Math.round(seconds));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m > 0) return `clip_${m}m${s}s`;
  return `clip_${s}s`;
}

export function suggestClipDownloadName(
  title: string | null | undefined,
  uploader: string | null | undefined,
  mediaUrl: string,
  options?: {
    duration?: number | null;
    cropStart?: number | null;
    cropEnd?: number | null;
    platform?: 'Kick' | 'Twitch' | string | null;
  },
): string {
  const clipper = uploader?.trim() || channelSlugFromMediaUrl(mediaUrl) || 'channel';
  const clipTitle = title?.trim() || 'Untitled';
  const dur = _durationTag(options?.duration);
  const platform = options?.platform ? options.platform.toLowerCase() : '';
  const parts: string[] = [clipper, clipTitle, dur];
  if (platform) parts.push(platform);
  const trim = _trimRangeTag(options?.cropStart ?? null, options?.cropEnd ?? null);
  if (trim) parts.push(`[${trim}]`);
  return parts.join(' - ');
}

export function suggestVideoDownloadName(
  title: string | null | undefined,
  platform: 'Kick' | 'Twitch' | string | null | undefined,
  vodId: string | null | undefined,
  options?: {
    duration?: number | null;
    cropStart?: number | null;
    cropEnd?: number | null;
  },
): string {
  const cleanTitle = title?.trim() || platform?.toLowerCase() || 'video';
  const dur = options?.duration ? _durationTag(options.duration) : '';
  const platformPart = platform ? platform.toLowerCase() : '';
  const parts: string[] = [cleanTitle];
  if (dur) parts.push(dur);
  if (platformPart) parts.push(platformPart);
  if (vodId) parts.push(vodId);
  const stem = parts.join(' - ');
  const trim = _trimRangeTag(options?.cropStart ?? null, options?.cropEnd ?? null);
  return trim ? `${stem} [${trim}]` : stem;
}

export interface PreviewLevelOption {
  index: number;
  height: number;
  label: string;
}

type HlsLevelLike = {
  height?: number;
  width?: number;
  bitrate?: number;
  url?: string | string[];
  name?: string;
  attrs?: { RESOLUTION?: string };
};

export function previewLevelLabel(height: number, _bitrate?: number, isSourceLevel = false): string {
  if (!height) return 'Auto';
  const res = `${height}p`;
  if (isSourceLevel) return `source/${res}`;
  return res;
}

export function levelIndexForHeight(levels: PreviewLevelOption[], target: number): number {
  if (!levels.length) return 0;
  const matches = levels.filter((l) => l.height === target);
  if (matches.length) return matches[0].index;
  const below = levels.filter((l) => l.height > 0 && l.height < target);
  if (below.length) return below[below.length - 1].index;
  const above = levels.filter((l) => l.height > target);
  if (above.length) return above[0].index;
  return levels[0].index;
}

export function inferLevelHeight(level: HlsLevelLike): number {
  if (level.height && level.height > 0) return level.height;
  const res = level.attrs?.RESOLUTION;
  if (res) {
    const m = res.match(/x(\d+)/i);
    if (m) return parseInt(m[1], 10);
  }
  const urlRaw = level.url;
  const url = (Array.isArray(urlRaw) ? urlRaw[0] : urlRaw) || level.name || '';
  const urlM = url.match(/\/(\d{3,4})p\d*\//i) || url.match(/(\d{3,4})p/i);
  if (urlM) return parseInt(urlM[1], 10);
  return 0;
}

export function mergeVariantHeights(
  ...sources: Array<number[] | undefined | null>
): number[] {
  const set = new Set<number>();
  for (const src of sources) {
    for (const h of src ?? []) {
      if (h > 0) set.add(h);
    }
  }
  return [...set].sort((a, b) => a - b);
}

function mapInferredHlsLevels(
  raw: Array<{ index: number; height: number; bitrate?: number }>,
  initialHeight: number,
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const withHeight = raw.filter((l) => l.height > 0);
  if (!withHeight.length) {
    return { mapped: [], defaultIndex: 0 };
  }
  const maxH = Math.max(...withHeight.map((l) => l.height));
  const mapped = withHeight
    .map((l) => ({
      index: l.index,
      height: l.height,
      label: previewLevelLabel(l.height, l.bitrate, l.height === maxH),
    }))
    .sort((a, b) => a.height - b.height);
  return {
    mapped,
    defaultIndex: levelIndexForHeight(mapped, initialHeight),
  };
}

/**
 * Build the preview quality menu for HLS. Menu tiers come from the manifest and/or
 * API variant_heights — never from the player-size cap alone (that only picks initial playback).
 */
export function resolveHlsPreviewLevels(
  hlsLevels: HlsLevelLike[],
  opts: { initialHeight: number; fallbackHeights?: number[] },
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const fallback = mergeVariantHeights(opts.fallbackHeights);
  const initialHeight = opts.initialHeight;

  if (hlsLevels.length > 0) {
    const raw = hlsLevels.map((l, i) => ({
      index: i,
      height: inferLevelHeight(l),
      bitrate: l.bitrate,
    }));

    if (raw.every((l) => !l.height)) {
      if (fallback.length === hlsLevels.length) {
        raw.forEach((l, i) => { l.height = fallback[i]; });
      } else if (hlsLevels.length === 1 && fallback.length > 1) {
        const mapped = mapHeightsToPreviewLevels(fallback);
        mapped.forEach((m) => { m.index = 0; });
        return {
          mapped,
          defaultIndex: levelIndexForHeight(mapped, initialHeight),
        };
      } else if (fallback.length > 0) {
        const mapped = mapHeightsToPreviewLevels(fallback);
        if (mapped.length === hlsLevels.length) {
          mapped.forEach((m, i) => { m.index = i; });
        }
        return {
          mapped,
          defaultIndex: levelIndexForHeight(mapped, initialHeight),
        };
      }
    }

    const fromHls = mapInferredHlsLevels(raw, initialHeight);
    if (fromHls.mapped.length) return fromHls;
  }

  if (fallback.length > 0) {
    const mapped = mapHeightsToPreviewLevels(fallback);
    return {
      mapped,
      defaultIndex: levelIndexForHeight(mapped, initialHeight),
    };
  }

  if (hlsLevels.length > 0) {
    const mapped = hlsLevels.map((_l, i) => ({
      index: i,
      height: 0,
      label: `Level ${i + 1}`,
    }));
    return { mapped, defaultIndex: 0 };
  }

  return { mapped: [], defaultIndex: 0 };
}

/** Progressive / API-only quality menu (never collapse to active playback height only). */
export function resolveProgressivePreviewLevels(
  opts: {
    variantHeights?: number[];
    qualityLabels?: string[];
    initialHeight: number;
  },
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const heights = mergeVariantHeights(
    opts.variantHeights,
    parseQualityHeights(opts.qualityLabels ?? []),
  );
  const mapped = mapHeightsToPreviewLevels(heights);
  if (!mapped.length) {
    return { mapped: [], defaultIndex: 0 };
  }
  return {
    mapped,
    defaultIndex: levelIndexForHeight(mapped, opts.initialHeight),
  };
}

export type ProgressivePreviewMeta = {
  variantHeights?: number[];
  qualityLabels?: string[];
  initialHeight: number;
};

/** Merge session + optional clip-info qualities; fetch clip info only when still empty. */
export async function resolveProgressivePreviewLevelsAsync(
  pageUrl: string,
  meta: ProgressivePreviewMeta,
  fetchClipQualities?: (url: string) => Promise<string[] | undefined>,
): Promise<{ mapped: PreviewLevelOption[]; defaultIndex: number; qualityLabels?: string[] }> {
  let qualityLabels = meta.qualityLabels;
  let result = resolveProgressivePreviewLevels({
    variantHeights: meta.variantHeights,
    qualityLabels,
    initialHeight: meta.initialHeight,
  });
  if (result.mapped.length || !isClipPreviewUrl(pageUrl) || !fetchClipQualities) {
    return result;
  }
  try {
    const fetched = await fetchClipQualities(pageUrl);
    if (fetched?.length) {
      qualityLabels = fetched;
      result = resolveProgressivePreviewLevels({
        variantHeights: meta.variantHeights,
        qualityLabels,
        initialHeight: meta.initialHeight,
      });
    }
  } catch {
    /* keep empty */
  }
  return { ...result, qualityLabels };
}

export interface HlsLevelController {
  levels: unknown[];
  currentLevel: number;
  nextLevel: number;
  loadLevel: number;
  autoLevelCapping?: number;
  config?: { capLevelToPlayerSize?: boolean };
  media?: HTMLMediaElement | null;
  stopLoad?: () => void;
  loadSource?: (url: string) => void;
  startLoad?: (startPosition?: number) => void;
}

/** Clamp menu level index to manifest level count (YouTube DASH mux exposes one HLS level). */
export function effectiveHlsLevelIndex(hlsIndex: number, levelCount: number): number {
  if (levelCount <= 0) return -1;
  return Math.min(Math.max(0, hlsIndex), levelCount - 1);
}

/**
 * Whether POST /quality + manifest reload is needed.
 * YouTube DASH segment playlists expose one HLS level with height=0 — compare applied tier instead.
 */
export function hlsNeedsApiQualitySwitch(
  playbackHeight: number,
  appliedHeight: number,
  hlsLevelHeight: number,
  trimTimeline: boolean,
): boolean {
  if (trimTimeline) return playbackHeight !== appliedHeight;
  return !hlsLevelHeight || hlsLevelHeight !== playbackHeight;
}

/** Re-seek on the next frame so MSE fetches the current fragment at the new tier. */
export function resumePreviewAtTime(
  video: HTMLVideoElement,
  time: number,
  wasPaused: boolean,
): void {
  const t = Math.max(0, time);
  const apply = () => {
    const end = video.seekable.length > 0
      ? video.seekable.end(video.seekable.length - 1)
      : Number.POSITIVE_INFINITY;
    const target = Number.isFinite(end) ? Math.min(t, Math.max(0, end - 0.05)) : t;
    if (Math.abs(video.currentTime - target) > 0.02) {
      video.currentTime = target;
    }
    if (!wasPaused) void video.play().catch(() => {});
  };
  requestAnimationFrame(() => requestAnimationFrame(apply));
}

/** Switch HLS quality — reloads the current fragment at the chosen level. */
export function applyHlsQualityLevel(
  hls: HlsLevelController,
  levelIndex: number,
  immediate = false,
): void {
  const idx = effectiveHlsLevelIndex(levelIndex, hls.levels.length);
  if (idx < 0) return;
  if (hls.config) hls.config.capLevelToPlayerSize = false;
  if (typeof hls.autoLevelCapping === 'number') hls.autoLevelCapping = -1;
  if (immediate) {
    hls.currentLevel = idx;
    hls.nextLevel = idx;
    hls.loadLevel = idx;
    const media = hls.media;
    if (media && hls.startLoad) {
      hls.startLoad(Math.max(0, media.currentTime || 0));
    }
    return;
  }
  hls.nextLevel = idx;
}

void (() => {
  const stub = document.createElement('video');
  Object.defineProperty(stub, 'error', { value: { code: MediaError.MEDIA_ERR_ABORTED } });
  console.assert(isIgnorableProgressivePreviewError(stub), 'abort during load is ignorable');
  console.assert(
    warmYoutubePreviewBatch(['https://www.youtube.com/watch?v=dQw4w9WgXcQ'], 1) === undefined,
    'batch warm is fire-and-forget',
  );
  const cap = 360;
  console.assert(
    initialPreviewPreferHeight(false, cap, { youtube: true, variantHeights: [720, 1080] }) === 720,
    'YouTube preview should default to 720p',
  );
  console.assert(
    initialPreviewPreferHeight(false, cap) === cap,
    'Kick/Twitch preview should cap to player',
  );
  console.assert(previewMuxPollMaxMs(0, 900) === 15 * 60 * 1000, 'mux poll capped at 15min');
  console.assert(previewDashMuxPollMaxMs() === 0, 'dash segment mux poll removed');
  console.assert(previewPlaylistPollMaxMs() === 15_000, 'window-HLS playlist poll capped');
  console.assert(PREVIEW_SEEK_DEBOUNCE_MS === 200, 'seek debounce ms');
  console.assert(
    shouldWaitForPreviewMux({ playlist_ready: true, trim_timeline: true }, 'hls') === false,
    'playlist_ready skips mux wait',
  );
  console.assert(
    shouldWaitForPreviewMux({ segment_buffer_ready: true }, 'hls') === false,
    'segment_buffer_ready (seg0) skips mux wait',
  );
  console.assert(
    shouldWaitForPreviewMux({ playlist_ready: false, trim_timeline: true }, 'hls') === false,
    'trim_timeline skips mux wait (segments built on demand)',
  );
  console.assert(
    shouldWaitForPreviewMux({ playlist_ready: false, trim_timeline: false }, 'hls') === true,
    'window-HLS still waits for playlist/seg0',
  );
  console.assert(
    shouldWaitForPreviewMux({ playlist_ready: false, trim_timeline: false }, 'progressive') === true,
    'full-file mux still waits for progressive',
  );
  // prewarmPreviewDashSeek is a no-op now (backend removed per-segment DASH mux).
  prewarmPreviewDashSeek('sid', 12.3, async () => ({ ok: true }));
  {
    const v = document.createElement('video');
    Object.defineProperty(v, 'currentTime', { writable: true, value: 5 });
    const r = clampPreviewTimeToVodTrim(v, 10, 20, false);
    console.assert(r.vodTime === 10 && r.applied, 'window-HLS clamps below trim start');
    Object.defineProperty(v, 'currentTime', { writable: true, value: 25 });
    const r2 = clampPreviewTimeToVodTrim(v, 10, 20, false);
    console.assert(r2.paused && r2.vodTime === 20, 'window-HLS clamps and pauses at trim end');
  }
  console.assert(youtubeExploreSessionStaggerMs(2) === 0, 'explore stagger disabled');
  console.assert(youtubeVideoIdFromUrl('https://youtu.be/dQw4w9WgXcQ') === 'dQw4w9WgXcQ');
  console.assert(youtubeEmbedSrc('abc123def45', 30).includes('start=30'));
  console.assert(typeof waitForPreviewMuxReady === 'function', 'mux poll helper exported');
  console.assert(VIEWPORT_PREVIEW_QUALITY_DEBOUNCE_MS >= VIEWPORT_PREVIEW_FULLSCREEN_DEBOUNCE_MS);
  console.assert(effectiveHlsLevelIndex(3, 1) === 0, 'single-level manifest clamps menu index');
  console.assert(
    hlsNeedsApiQualitySwitch(720, 720, 0, true) === false,
    'trim timeline: same tier skips API reload',
  );
  console.assert(
    hlsNeedsApiQualitySwitch(720, 0, 0, false) === true,
    'normal HLS: unknown level height needs API',
  );
})();
