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
 * the applicable part is (3): a custom pLoader that rewrites every playlist
 * response before hls.js parses it. Non-Twitch playlists never contain
 * 'stitched' segments, so the transform is inert there — applied by default
 * to all hls.js players.
 *
 * ponytail: vaft's backup-stream rotation (fresh access token + player_type
 * variants) is not ported — it needs the usher URL + channel name, which the
 * app's proxied sessions don't expose. If Twitch ever stops inserting
 * stitched segments, upgrade to the full rotation using entry.channelName.
 */

/** vaft's ad signifier: Twitch ad segments carry this in their URL. */
const AD_SIGNIFIER = 'stitched';
/** Neutral replacement for Twitch ad tracking URLs (vaft uses twitch.tv). */
const AD_SAFE_URL = 'https://twitch.tv';

/**
 * Rewrite an m3u8 playlist: neutralize Twitch ad tracking URLs, drop
 * low-latency prefetch lines, and remove ad segments (EXTINF + URI pairs
 * whose segment URL contains the ad signifier). Returns the input unchanged
 * when there is nothing to strip, and when EVERY segment would be removed
 * (full ad takeover) — stripping everything would break the player.
 */
export function stripTwitchAdSegments(text: string): string {
  if (!text.includes('#EXT')) return text;
  const hadAds = text.includes(AD_SIGNIFIER);
  const lines = text.replace(/\r/g, '').split('\n');
  const segCount = () => lines.filter((l) => l.startsWith('#EXTINF')).length;

  // Neutralize ad tracking URLs which appear in the overlay UI (vaft).
  for (let i = 0; i < lines.length; i++) {
    lines[i] = lines[i]
      .replace(/(X-TV-TWITCH-AD-URL=")(?:[^"]*)(")/g, `$1${AD_SAFE_URL}$2`)
      .replace(/(X-TV-TWITCH-AD-CLICK-TRACKING-URL=")(?:[^"]*)(")/g, `$1${AD_SAFE_URL}$2`);
  }
  if (!hadAds) return lines.join('\n'); // no ad segments — only URL cleanup

  // Remove EXTINF + URI pairs whose segment URL is an ad.
  const totalSegs = segCount();
  let adPairs = 0;
  for (let i = 0; i < lines.length - 1; i++) {
    if (lines[i].startsWith('#EXTINF') && lines[i + 1].includes(AD_SIGNIFIER)) {
      lines[i] = '';
      lines[i + 1] = '';
      adPairs++;
      i++; // skip the URI line
    }
  }
  // No low latency during ads — prefetching would fetch ad segments (vaft).
  if (adPairs > 0) {
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith('#EXT-X-TWITCH-PREFETCH:')) lines[i] = '';
    }
  }
  // Full ad takeover: removing everything would stall the player — let the
  // (short) ad play rather than break playback.
  if (adPairs === 0 || (totalSegs > 0 && segCount() === 0)) return text;
  return lines.filter((l) => l !== '').join('\n');
}

export function bumpAdStripCounter(): void {
  if (typeof window === 'undefined') return;
  const win = window as Window & { __vodripAdSegmentsStripped?: number };
  win.__vodripAdSegmentsStripped = (win.__vodripAdSegmentsStripped ?? 0) + 1;
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
        const stripped = stripTwitchAdSegments(data);
        if (stripped !== data) bumpAdStripCounter();
        // hls.js >=1.0 LoaderStats shape — playlist-loader writes stats.parsing
        // and TTFB math reads stats.loading, so the legacy v0.x trequest/tfirst
        // shape breaks the success path ('Cannot set properties of undefined').
        const now = performance.now();
        const stats = {
          aborted: false,
          loaded: stripped.length,
          retry: 0,
          total: stripped.length,
          chunkCount: 0,
          bwEstimate: 0,
          loading: { start: now, first: now, end: now },
          parsing: { start: 0, end: 0 },
          buffering: { start: 0, first: 0, end: 0 },
        };
        callbacks.onSuccess({ url, data: stripped }, stats, context, null);
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

export interface TwitchAdBlockHlsOptions {
  /** Live playback knobs (live popup / in-progress VOD previews). */
  live?: boolean;
  // vaft backup-stream rotation knobs (adblock-merge agent) — declared here
  // so the merge only reconciles the interface name, never the function body.
  onAdRotation?: (reason: string) => void;
  rotationThreshold?: number;
  rotationCooldownMs?: number;
  maxRotations?: number;
}

/**
 * hls.js config that strips Twitch ad segments from every playlist response.
 *
 * With `{ live: true }` adds the live knobs used by the live popup and
 * in-progress VOD previews. liveSyncDuration is seconds (the single knob — it
 * overrides liveSyncDurationCount) and matches the main player's live config
 * (App.tsx) so the live popup latency behaviour never drifts from the main
 * preview. lowLatencyMode defaults to true in hls.js 1.x; set explicitly for
 * LL-HLS masters (Twitch low_latency=true usher param).
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
  return config;
}
