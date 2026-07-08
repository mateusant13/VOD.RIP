/** YouTube IFrame Player API — chromeless embed for channel explore (our UI drives playback). */

export interface YTPlayer {
  playVideo(): void;
  pauseVideo(): void;
  seekTo(seconds: number, allowSeekAhead: boolean): void;
  getCurrentTime(): number;
  getDuration(): number;
  getAvailableQualityLevels(): string[];
  getPlaybackQuality(): string;
  setPlaybackQuality(quality: string): void;
  mute(): void;
  unMute(): void;
  isMuted(): boolean;
  setVolume(volume: number): void;
  getVolume(): number;
  destroy(): void;
}

export type YoutubeEmbedQuality = {
  ytQuality: string;
  height: number;
  label: string;
};

export const YT_PLAYER_STATE = {
  UNSTARTED: -1,
  ENDED: 0,
  PLAYING: 1,
  PAUSED: 2,
  BUFFERING: 3,
  CUED: 5,
} as const;

/** ponytail: cross-origin blocks .ytp-* DOM hacks — crop/scale hides edge chrome only */
export const YOUTUBE_EMBED_SCALE = 1.12;
export const YOUTUBE_EMBED_CLIP = 'inset(12% 0 14% 0)';

export const YOUTUBE_EMBED_PLAYER_VARS = {
  autoplay: 1,
  mute: 1,
  controls: 0,
  disablekb: 1,
  fs: 0,
  rel: 0,
  iv_load_policy: 3,
  cc_load_policy: 0,
  playsinline: 1,
  enablejsapi: 1,
  color: 'white',
} as const;

const YT_QUALITY_HEIGHT: Record<string, number> = {
  highres: 1440,
  hd1440: 1440,
  hd1080: 1080,
  hd720: 720,
  large: 480,
  medium: 360,
  small: 240,
  tiny: 144,
};

let apiReady: Promise<void> | null = null;

export function loadYoutubeIframeApi(): Promise<void> {
  if (typeof window !== 'undefined' && (window as Window & { YT?: { Player: unknown } }).YT?.Player) {
    return Promise.resolve();
  }
  if (!apiReady) {
    apiReady = new Promise((resolve) => {
      const w = window as Window & {
        onYouTubeIframeAPIReady?: () => void;
        YT?: { Player: new (...args: unknown[]) => YTPlayer };
      };
      const finish = () => resolve();
      const prev = w.onYouTubeIframeAPIReady;
      w.onYouTubeIframeAPIReady = () => {
        prev?.();
        finish();
      };
      if (!document.querySelector('script[src*="youtube.com/iframe_api"]')) {
        const s = document.createElement('script');
        s.src = 'https://www.youtube.com/iframe_api';
        document.head.appendChild(s);
      }
    });
  }
  return apiReady;
}

export function mapYoutubePlaybackQualities(raw: string[]): YoutubeEmbedQuality[] {
  const out: YoutubeEmbedQuality[] = [];
  const seen = new Set<number>();
  for (const q of raw) {
    const h = YT_QUALITY_HEIGHT[q];
    if (!h || seen.has(h)) continue;
    seen.add(h);
    out.push({ ytQuality: q, height: h, label: `${h}p` });
  }
  out.sort((a, b) => b.height - a.height);
  return out;
}

export function createYoutubeEmbedPlayer(
  host: HTMLElement,
  videoId: string,
  handlers: {
    onReady?: (player: YTPlayer) => void;
    onStateChange?: (state: number) => void;
    onError?: (code: number) => void;
  },
): YTPlayer {
  const w = window as Window & { YT?: { Player: new (...args: unknown[]) => YTPlayer } };
  if (!w.YT?.Player) throw new Error('YouTube IFrame API not loaded');
  return new w.YT.Player(host, {
    videoId,
    width: '100%',
    height: '100%',
    playerVars: { ...YOUTUBE_EMBED_PLAYER_VARS, origin: window.location.origin },
    events: {
      onReady: (e: { target: YTPlayer }) => handlers.onReady?.(e.target),
      onStateChange: (e: { data: number }) => handlers.onStateChange?.(e.data),
      onError: (e: { data: number }) => handlers.onError?.(e.data),
    },
  });
}

console.assert(mapYoutubePlaybackQualities(['hd720', 'medium', 'hd720']).length === 2);
console.assert(mapYoutubePlaybackQualities(['hd1080'])[0]?.height === 1080);
