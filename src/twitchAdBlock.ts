/**
 * Twitch ad blocking for the in-app hls.js players, ported from the vaft
 * userscript (pixeltris/TwitchAdSolutions, v37 — see
 * TwitchAdSolutions (vaft)-37.0.0.txt in the repo root).
 *
 * vaft runs inside Twitch's player page and does three things:
 *   1. detects ad ('stitched') segments in the media playlist,
 *   2. rotates to backup player types to obtain an ad-free stream,
 *   3. strips the ad segments / cleans the playlist served to the player.
 *
 * The app plays Twitch streams with raw hls.js (no Twitch player page), so
 * this module provides (2)+(3) — a custom pLoader that rewrites every
 * playlist response before hls.js parses it: when stitched segments keep
 * showing up mid-playback it asks the site to rotate the session to the
 * next player type via POST /api/preview/live/rotate/{session_id} (the
 * backend swaps the session's usher master in place and the site reloads
 * the same proxied URL). Non-Twitch playlists never contain 'stitched'
 * segments, so both paths are inert there — applied by default to all
 * hls.js players.
 */

/** vaft's ad signifier: Twitch ad segments carry this in their URL. */
const AD_SIGNIFIER = 'stitched';
/** Neutral replacement for Twitch ad tracking URLs (vaft uses twitch.tv). */
const AD_SAFE_URL = 'https://twitch.tv';

/**
 * Optional midroll-rotation wiring for ``twitchAdBlockHlsConfig``.
 *
 * vaft's core value is rotating to a different ``player_type`` mid-stream to
 * obtain an ad-free backup. The strip path detects the same 'stitched'
 * segments; when they keep reappearing the loader fires ``onAdRotation`` and
 * the site swaps the session's master (backend) + reloads hls.js. Stripping
 * is never disabled — rotation is an enhancement, fallback is always strip.
 */
export interface TwitchAdRotationOptions {
  /** Called once ads are stripped on consecutive playlist refreshes. */
  onAdRotation?: (info: { url: string }) => void;
  /** Strips on this many consecutive ad-tainted refreshes before rotating (default 2). */
  rotationThreshold?: number;
  /** Min ms between rotations (default 60_000 — one per ad break). */
  rotationCooldownMs?: number;
  /** Hard cap on rotations per player session (default 4). */
  maxRotations?: number;
  /**
   * Rotation state, lazily attached by the pLoader and shared by every
   * loader instance of one player. hls.js constructs a fresh pLoader for
   * each playlist load, so tracker state cannot live on the loader — it is
   * parked on this per-player config object instead.
   * @internal
   */
  tracker?: TwitchAdRotationTracker | null;
  /**
   * Last media playlist containing playable live segments — held during full
   * ad takeovers. Same lifetime requirement as ``tracker`` (per-player).
   * @internal
   */
  lastClean?: string | null;
}

/**
 * Rewrite an m3u8 playlist: neutralize Twitch ad tracking URLs, drop
 * low-latency prefetch lines, and remove ad segments.
 *
 * Ad segments are identified two ways:
 *   1. the classic vaft signifier — the segment URI contains 'stitched'
 *      (raw upstream playlists), and
 *   2. the app's proxied/rewritten form: the backend rewrites every segment
 *      URI to an opaque ``/api/preview/hls/{sid}/resource?id=...`` URL, so
 *      the ONLY remaining ad marker is the ``#EXT-X-DATERANGE`` tag with
 *      ``CLASS="twitch-stitched-ad"`` (the loader treats any playlist
 *      containing 'stitched' as ad-tainted and drives rotation from it).
 *
 * Segment lines are ONLY removed in the direct form (1). The proxied form
 * must not be deleted: hls.js stalls when fragments vanish from a live
 * window mid-playback (empty playlist = fatal levelEmptyError), so live ads
 * are dodged by rotating to an ad-free player type instead of by rewriting
 * the playlist — this matches vaft, which also never deletes segment lines.
 *
 * Returns the input unchanged when there is nothing to strip, and when EVERY
 * segment would be removed (full ad takeover) — stripping everything would
 * break the player (original vaft guard).
 */
export function stripTwitchAdSegments(text: string): string {
  if (!text.includes('#EXT')) return text;
  const hadAds = text.includes(AD_SIGNIFIER);
  const lines = text.replace(/\r/g, '').split('\n');

  // Neutralize ad tracking URLs which appear in the overlay UI (vaft).
  for (let i = 0; i < lines.length; i++) {
    lines[i] = lines[i]
      .replace(/(X-TV-TWITCH-AD-URL=")(?:[^"]*)(")/g, `$1${AD_SAFE_URL}$2`)
      .replace(/(X-TV-TWITCH-AD-CLICK-TRACKING-URL=")(?:[^"]*)(")/g, `$1${AD_SAFE_URL}$2`);
  }
  if (!hadAds) return lines.join('\n'); // no ad segments — only URL cleanup

  // Remove EXTINF + URI pairs whose segment URL is an ad (direct/raw
  // playlists). The proxied form must NOT be deleted — see the doc comment.
  let adPairs = 0;
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('#EXTINF') && i + 1 < lines.length) {
      const uri = lines[i + 1];
      if (uri.includes(AD_SIGNIFIER)) {
        adPairs++; // drop the EXTINF + URI pair
        i++;
        continue;
      }
      out.push(line, uri);
      i++;
      continue;
    }
    out.push(line);
  }
  // No low latency during ads — prefetching would fetch ad segments (vaft).
  // Blank whenever ads are detected (not only when pairs were removed): the
  // proxied form has no stitched URIs to remove but the prefetch lines carry
  // RAW upstream URLs that would leak ad segments via LL-HLS prefetch.
  if (hadAds) {
    for (let i = 0; i < out.length; i++) {
      if (out[i].startsWith('#EXT-X-TWITCH-PREFETCH:')) out[i] = '';
    }
  }
  // Full ad takeover: removing everything would stall the player — let the
  // (short) ad play rather than break playback (original vaft guard).
  if (adPairs === 0) return out.filter((l) => l !== '').join('\n');
  const kept = out.filter((l) => l !== '');
  if (!kept.some((l) => l.startsWith('#EXTINF'))) return text;
  return kept.join('\n');
}

export function bumpAdStripCounter(): void {
  if (typeof window === 'undefined') return;
  const win = window as Window & { __vodripAdSegmentsStripped?: number };
  win.__vodripAdSegmentsStripped = (win.__vodripAdSegmentsStripped ?? 0) + 1;
}

/**
 * Pure rotation-decision state machine (testable without a browser).
 *
 * vaft rotates once per ad break: we count consecutive ad-tainted playlist
 * refreshes and rotate at the threshold, but a clean playlist resets the
 * count, a cooldown window prevents re-rotating inside the same break, and
 * a hard cap keeps a pathological channel from cycling forever.
 */
export class TwitchAdRotationTracker {
  private strippedCount = 0;
  private rotationsUsed = 0;
  private lastRotationAt = 0;

  constructor(private readonly opts: {
    rotationThreshold: number;
    rotationCooldownMs: number;
    maxRotations: number;
  }) {}

  /** Feed an ad-strip event; returns true when a rotation should be requested. */
  onAds(now = Date.now()): boolean {
    if (this.rotationsUsed >= this.opts.maxRotations) return false;
    if (now - this.lastRotationAt < this.opts.rotationCooldownMs) return false;
    this.strippedCount += 1;
    if (this.strippedCount < this.opts.rotationThreshold) return false;
    this.strippedCount = 0;
    this.lastRotationAt = now;
    this.rotationsUsed += 1;
    return true;
  }

  /** Feed a clean playlist — the ad break is over, restart the count. */
  onCleanPlaylist(): void {
    this.strippedCount = 0;
  }
}

/**
 * Per-site rotation glue: builds the ``onAdRotation`` callback for one hls.js
 * player. Reads the session id / hls / video from refs at fire time (not at
 * config time) so the callback survives re-renders, and never throws — a
 * failed rotation falls back to the pLoader's stripping, never a black screen.
 */
export function createTwitchAdRotationHandler(rot: {
  getSessionId: () => string | null;
  getHls: () => { loadSource(url: string): void } | null;
  getVideo: () => { paused: boolean; play(): Promise<void> } | null;
  requestRotation: (sessionId: string) => Promise<{ ok?: boolean; master_url?: string } | null | undefined>;
}): () => void {
  return () => {
    const sid = rot.getSessionId();
    const hls = rot.getHls();
    const video = rot.getVideo();
    if (!sid || !hls || !video) return;
    const wasPlaying = !video.paused;
    void (async () => {
      try {
        const res = await rot.requestRotation(sid);
        if (!res?.ok || !res.master_url) return; // keep stripping
        // Backend swapped the session's upstream master in place — reloading
        // the same proxied URL serves the rotated stream (hls.js refetches
        // the manifest on loadSource; the proxy responds Cache-Control: no-cache).
        hls.loadSource(res.master_url);
        if (wasPlaying) video.play().catch(() => {});
      } catch {
        // rotation failed — stripping continues, playback never stalls
      }
    })();
  };
}

type HlsLoaderCallbacks = {
  onSuccess: (response: { url: string; data: string }, stats: Record<string, unknown>, context: unknown, networkDetails: unknown) => void;
  onError: (error: Record<string, unknown>, context: unknown, networkDetails: unknown) => void;
  onTimeout?: (stats: Record<string, unknown>, context: unknown, networkDetails: unknown) => void;
};

/**
 * hls.js pLoader: fetches playlists via fetch(), strips Twitch ad segments,
 * and hands the cleaned text to hls.js. Self-contained (no hls.js import) so
 * the pure strip function stays testable in node. Playlist loading runs on
 * the main thread even with enableWorker, so a pLoader override suffices.
 */
export class TwitchAdBlockLoader {
  private controller: AbortController | null = null;
  private readonly tracker: TwitchAdRotationTracker | null = null;
  private readonly onAdRotation: ((info: { url: string }) => void) | null = null;
  private readonly opts: TwitchAdRotationOptions | null;

  /** hls.js constructs the pLoader with the full Hls config — read the custom key. */
  constructor(config?: Record<string, unknown>) {
    this.opts = ((config as { twitchAdRotation?: TwitchAdRotationOptions | null } | undefined)
      ?.twitchAdRotation) ?? null;
    const opts = this.opts;
    if (opts?.onAdRotation) {
      this.onAdRotation = opts.onAdRotation;
      // hls.js instantiates a fresh pLoader per playlist load (the loader is
      // reset after every successful load), so consecutive ad-tainted
      // refreshes must accumulate on the per-player config — not here.
      opts.tracker = opts.tracker ?? new TwitchAdRotationTracker({
        rotationThreshold: Math.max(1, opts.rotationThreshold ?? 2),
        rotationCooldownMs: Math.max(0, opts.rotationCooldownMs ?? 60_000),
        maxRotations: Math.max(1, opts.maxRotations ?? 4),
      });
      this.tracker = opts.tracker ?? null;
    }
    if (opts && opts.lastClean === undefined) opts.lastClean = null;
  }

  load(context: { url: string; type?: string }, _config: Record<string, unknown>, callbacks: HlsLoaderCallbacks): void {
    const url = context.url;
    this.controller = new AbortController();
    const timeoutMs = 10_000;
    const timer = window.setTimeout(() => {
      this.controller?.abort();
      callbacks.onTimeout?.({ trequest: performance.now(), tfirst: 0, tload: 0, loaded: 0, total: 0 }, context, null);
    }, timeoutMs);
    fetch(url, { signal: this.controller.signal, credentials: 'same-origin' })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.text();
      })
      .then((data) => {
        window.clearTimeout(timer);
        const hadAds = data.includes(AD_SIGNIFIER);
        let served = stripTwitchAdSegments(data);
        // Full ad takeover: the window contains ONLY ad segments (no ',live'
        // EXTINF survives). Serving it plays the ad; deleting it stalls hls.js
        // (levelEmptyError / bufferStalledError — empirically disproven). Hold
        // the last known-good playlist instead: hls.js keeps playing real
        // stream segments a few seconds behind live and snaps forward when the
        // takeover ends and the next fetch is clean again. First fetch has
        // nothing to hold — play the short ad rather than a black screen.
        const fullTakeover = hadAds && !/#EXTINF:[^\n]*,\s*live/.test(served);
        if (fullTakeover) {
          // Held content lives on the per-player config: this loader instance
          // is discarded after every load, so per-instance state would never
          // accumulate a known-good playlist.
          if (this.opts?.lastClean) served = this.opts.lastClean;
        } else if (/#EXTINF/.test(served) && this.opts) {
          this.opts.lastClean = served;
        }
        const changed = served !== data;
        if (changed) bumpAdStripCounter();
        if (hadAds && this.tracker?.onAds() && this.onAdRotation) {
          this.onAdRotation({ url: context.url });
        } else if (!hadAds) {
          this.tracker?.onCleanPlaylist();
        }
        // hls.js >=1.0 LoaderStats shape — playlist-loader writes stats.parsing
        // and TTFB math reads stats.loading, so the legacy v0.x trequest/tfirst
        // shape breaks the success path ('Cannot set properties of undefined').
        const now = performance.now();
        const stats = {
          aborted: false,
          loaded: served.length,
          retry: 0,
          total: served.length,
          chunkCount: 0,
          bwEstimate: 0,
          loading: { start: now, first: now, end: now },
          parsing: { start: 0, end: 0 },
          buffering: { start: 0, first: 0, end: 0 },
        };
        callbacks.onSuccess({ url, data: served }, stats, context, null);
      })
      .catch((err: unknown) => {
        window.clearTimeout(timer);
        if (err instanceof DOMException && err.name === 'AbortError') return;
        callbacks.onError(
          {
            type: 'networkError',
            details: 'manifestLoadError',
            fatal: context.type === 'manifest',
            url,
            loader: this,
            error: err,
            networkDetails: null,
          },
          context,
          null,
        );
      });
  }

  abort(): void {
    this.controller?.abort();
  }

  destroy(): void {
    this.controller?.abort();
  }
}

export interface TwitchAdBlockHlsOptions extends TwitchAdRotationOptions {
  /** Live playback knobs (live popup / in-progress VOD previews). */
  live?: boolean;
}

/**
 * hls.js config that strips Twitch ad segments from every playlist response
 * and (optionally) rotates to the next vaft player type after repeated ads.
 *
 * Rotation knobs (``opts.onAdRotation`` receives ``{ url }`` — the playlist
 * URL that was being loaded when the threshold was hit) are wired to the
 * loader via the ``twitchAdRotation`` config key; the site supplies its own
 * rotation glue (``createTwitchAdRotationHandler``) with its session id +
 * hls instance. With ``{ live: true }`` also adds the LL-HLS knobs used by
 * the live popup and in-progress VOD previews (lowLatencyMode, short
 * liveSyncDuration, deep back buffer, capped max buffer) — kept in one
 * function so latency behaviour never drifts between players.
 *
 * No-arg keeps the strip-only shape ``{ pLoader: TwitchAdBlockLoader,
 * twitchAdRotation: null }``. Unknown config keys are ignored by hls.js;
 * ``...twitchAdBlockHlsConfig()`` stays compatible with all existing call
 * sites.
 */
export function twitchAdBlockHlsConfig(opts: TwitchAdBlockHlsOptions = {}): Record<string, unknown> {
  const config: Record<string, unknown> = { pLoader: TwitchAdBlockLoader };
  if (opts.live) {
    Object.assign(config, {
      lowLatencyMode: true,
      liveSyncDuration: 3,
      liveMaxLatencyDuration: 10,
      liveDurationInfinity: true,
      maxBufferLength: 20,
      maxMaxBufferLength: 30,
      backBufferLength: 30,
      maxLiveSyncPlaybackRate: 1.25,
    });
  }
  // vaft midroll rotation wiring — null when no rotation knobs were supplied,
  // so the no-arg shape stays { pLoader, twitchAdRotation: null }.
  const { live: _live, ...rotationOpts } = opts;
  config.twitchAdRotation = Object.keys(rotationOpts).length > 0 ? rotationOpts : null;
  return config;
}
