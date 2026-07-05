/**
 * Shared preview-player hook — extracted from App.tsx & ChannelExplorePopup.tsx
 * (ponytail: duplicates merged, ~100 lines saved per consumer).
 *
 * Manages:
 *  - Quality level resolution & switching (HLS + progressive)
 *  - Viewport-synced playback height
 *  - Session quality sync with the backend
 *
 * Consumers (App.tsx, ChannelExplorePopup.tsx) retain their own UI state
 * (playing, muted, fullscreen, currentTime, error) because their layout and
 * control surfaces differ. Only the *quality state machine* lives here.
 */

import { useCallback, useRef, useState } from 'react';
import Hls from 'hls.js';
import {
  PREVIEW_CLIP_DEFAULT_HEIGHT,
  applyHlsQualityLevel,
  attachProgressivePreview,
  inferLevelHeight,
  resolveInitialHlsPreviewHeight,
  levelIndexForHeight,
  playbackHeightFromRequest,
  mergeVariantHeights,
  resolveHlsPreviewLevels,
  resolveProgressivePreviewLevels,
  resolveProgressivePreviewLevelsAsync,
  previewUrlWithPreferHeight,
  type PreviewLevelOption,
} from '../previewPlayerUtils';

export interface PlaybackInfo {
  url: string;
  kind: 'hls' | 'progressive';
  variantHeights?: number[];
  qualityLabels?: string[];
  activeHeight?: number;
}

export interface PreviewPlayerState {
  previewLevels: PreviewLevelOption[];
  qualityLevel: number;
}

export interface PreviewPlayerActions {
  applyPlaybackHeight: (
    playbackHeight: number,
    opts?: boolean | { userInitiated?: boolean },
  ) => Promise<void>;
  syncPlaybackToViewport: (fullscreenOverride?: boolean) => Promise<void>;
  applyQuality: (levelIndex: number) => Promise<void>;
  setPreviewLevels: (levels: PreviewLevelOption[]) => void;
  setQualityLevel: (level: number) => void;
}

interface PreviewPlayerOptions {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  playback: PlaybackInfo | null;
  sessionId: string | null;
  isClipPreview: boolean;
  isYoutubePreview?: boolean;
  trimStart?: number;
  /**
   * Container element used to measure the player viewport cap.
   * Pass a ref to the outermost wrapper of the player so height
   * calculations reflect the actual available space.
   */
  containerRef: React.RefObject<HTMLElement | null>;
  /**
   * Called when applyPlaybackHeight encounters an error.
   * If not provided, errors are silently swallowed.
   */
  onPreviewError?: (message: string) => void;
}

/**
 * Shared hook for preview player quality management.
 * Callers provide a video ref, playback info, and session id.
 * Returns levels state + action callbacks.
 */
export function usePreviewPlayer({
  videoRef,
  playback,
  sessionId,
  isClipPreview,
  isYoutubePreview = false,
  trimStart = 0,
  containerRef,
  onPreviewError,
}: PreviewPlayerOptions): PreviewPlayerState & PreviewPlayerActions & {
  setHlsRef: (hls: Hls | null) => void;
  syncProgressiveLevels: (mapped: PreviewLevelOption[], defaultIndex: number) => void;
  syncHlsLevels: (mapped: PreviewLevelOption[], defaultIndex: number) => void;
  resolveAndSyncProgressive: typeof resolveAndSyncProgressive;
  resolveAndSyncHls: typeof resolveAndSyncHls;
} {
  const [previewLevels, setPreviewLevels] = useState<PreviewLevelOption[]>([]);
  const [qualityLevel, setQualityLevel] = useState(0);

  const hlsRef = useRef<Hls | null>(null);
  const requestedHeightRef = useRef(0);
  const appliedHeightRef = useRef(0);
  const previewLevelsRef = useRef<PreviewLevelOption[]>([]);

  // Keep a ref in sync for closures
  const setLevels = useCallback((levels: PreviewLevelOption[]) => {
    previewLevelsRef.current = levels;
    setPreviewLevels(levels);
  }, []);

  /** Registered by the consumer after creating an Hls instance. */
  const setHlsRef = useCallback((hls: Hls | null) => {
    hlsRef.current = hls;
  }, []);

  const apiPost = useCallback(async <T,>(path: string, body: unknown): Promise<T> => {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }, []);

  const measurePlayerCap = useCallback(() => {
    if (!containerRef.current) return PREVIEW_CLIP_DEFAULT_HEIGHT;
    const r = containerRef.current.getBoundingClientRect();
    let h = r.height;
    if (h < 48 && r.width > 0) h = r.width / (16 / 9);
    const steps = [240, 360, 480, 720, 1080] as const;
    for (const step of steps) {
      if (h <= step + 48) return step;
    }
    return 1080;
  }, []);

  const applyPlaybackHeight = useCallback(async (
    playbackHeight: number,
    opts?: boolean | { userInitiated?: boolean },
  ) => {
    if (!playbackHeight) return;
    if (playbackHeight === appliedHeightRef.current && !opts) return;

    const userInitiated = typeof opts === 'boolean' ? opts : (opts?.userInitiated ?? false);
    const levels = previewLevelsRef.current;
    const menuIndex = levelIndexForHeight(levels, playbackHeight);
    const level = levels[menuIndex];
    if (!level) return;

    const video = videoRef.current;
    const wasPaused = video?.paused ?? true;
    const savedTime = video?.currentTime ?? trimStart;
    const sid = sessionId;

    const syncSessionQuality = () => apiPost(
      `/api/preview/session/${sid}/quality`,
      { prefer_height: playbackHeight },
    );

    // ── Progressive (direct MP4) path ──
    if (playback?.kind === 'progressive' && sid) {
      if (!video || !playback.url) return;
      appliedHeightRef.current = playbackHeight;
      try {
        const targetUrl = userInitiated
          ? previewUrlWithPreferHeight(playback.url, playbackHeight)
          : `${playback.url}${playback.url.includes('?') ? '&' : '?'}t=${Date.now()}`;
        attachProgressivePreview(video, targetUrl, savedTime);
        if (!wasPaused) void video.play().catch(() => {});
        if (userInitiated) {
          void syncSessionQuality().catch(() => {});
        } else {
          await syncSessionQuality();
        }
      } catch (err: unknown) {
        appliedHeightRef.current = 0;
        onPreviewError?.(err instanceof Error ? err.message : 'Could not change preview quality');
      }
      return;
    }

    // ── HLS path ──
    const hls = hlsRef.current;
    if (!hls) return;

    const hlsIndex = level.index;
    const hlsLevel = hls.levels[hlsIndex] as { height?: number } | undefined;
    const hlsHeight = hlsLevel ? inferLevelHeight(hlsLevel) : 0;
    const needsApiSwitch = !hlsHeight || hlsHeight !== playbackHeight;
    const playbackUrl = playback?.url ?? '';

    if (userInitiated && hlsIndex >= 0 && hlsIndex < hls.levels.length && !needsApiSwitch) {
      appliedHeightRef.current = playbackHeight;
      applyHlsQualityLevel(hls, hlsIndex, true);
      if (!wasPaused) void video?.play().catch(() => {});
      return;
    }

    if (needsApiSwitch && sid && playbackUrl) {
      appliedHeightRef.current = playbackHeight;
      try {
        const targetUrl = previewUrlWithPreferHeight(playbackUrl, playbackHeight);
        const onManifest = () => {
          hls.off?.(Hls.Events.MANIFEST_PARSED, onManifest);
          if (hlsIndex >= 0 && hlsIndex < hls.levels.length) {
            applyHlsQualityLevel(hls, hlsIndex, true);
          }
          hls.startLoad?.(Math.max(0, savedTime));
          if (video) {
            video.currentTime = savedTime;
            if (!wasPaused) void video.play().catch(() => {});
          }
        };
        hls.stopLoad?.();
        hls.on?.(Hls.Events.MANIFEST_PARSED, onManifest);
        hls.loadSource?.(targetUrl);
        void syncSessionQuality().catch(() => {});
      } catch (err: unknown) {
        appliedHeightRef.current = 0;
        onPreviewError?.(err instanceof Error ? err.message : 'Could not change preview quality');
      }
    } else if (hlsIndex >= 0 && hlsIndex < hls.levels.length) {
      appliedHeightRef.current = playbackHeight;
      applyHlsQualityLevel(hls, hlsIndex, userInitiated);
      if (userInitiated && !wasPaused) void video?.play().catch(() => {});
    }
  }, [apiPost, playback, sessionId, trimStart, videoRef, onPreviewError]);

  const syncPlaybackToViewport = useCallback(async (fullscreenOverride?: boolean) => {
    const levels = previewLevelsRef.current;
    if (!levels.length) return;
    const fullscreen = fullscreenOverride ?? false;
    const requested = requestedHeightRef.current || levels[qualityLevel]?.height || PREVIEW_CLIP_DEFAULT_HEIGHT;
    const cap = measurePlayerCap();
    const available = levels.map((l) => l.height);
    const playbackHeight = playbackHeightFromRequest(requested, available, cap, fullscreen);
    await applyPlaybackHeight(playbackHeight);
  }, [applyPlaybackHeight, measurePlayerCap, qualityLevel]);

  const applyQuality = useCallback(async (levelIndex: number) => {
    const level = previewLevelsRef.current[levelIndex];
    if (!level) return;
    requestedHeightRef.current = level.height;
    setQualityLevel(levelIndex);
    await applyPlaybackHeight(level.height, { userInitiated: true });
  }, [applyPlaybackHeight]);

  const syncProgressiveLevels = useCallback((
    mapped: PreviewLevelOption[],
    defaultIndex: number,
  ) => {
    setLevels(mapped);
    setQualityLevel(defaultIndex);
    const picked = mapped[defaultIndex];
    if (picked?.height) requestedHeightRef.current = picked.height;
  }, [setLevels]);

  /**
   * HLS variant of syncProgressiveLevels.
   * Sets both requestedHeightRef and appliedHeightRef so the first
   * quality switch doesn't re-apply the same height.
   * ponytail: extracted to fix the ref sync gap — was previously
   * handled inline with local refs that never synced back to the hook.
   */
  const syncHlsLevels = useCallback((
    mapped: PreviewLevelOption[],
    defaultIndex: number,
  ) => {
    setLevels(mapped);
    setQualityLevel(defaultIndex);
    const picked = mapped[defaultIndex];
    if (picked?.height) {
      requestedHeightRef.current = picked.height;
      appliedHeightRef.current = picked.height;
    }
  }, [setLevels]);

  const resolveAndSyncProgressive = useCallback(async (
    pageUrl: string,
    meta: {
      variantHeights?: number[];
      qualityLabels?: string[];
      initialHeight: number;
    },
    fetchClipQualities?: (url: string) => Promise<string[] | undefined>,
  ) => {
    const immediate = resolveProgressivePreviewLevels(meta);
    syncProgressiveLevels(immediate.mapped, immediate.defaultIndex);

    if (fetchClipQualities) {
      void resolveProgressivePreviewLevelsAsync(pageUrl, meta, fetchClipQualities)
        .then(({ mapped, defaultIndex }) => {
          if (mapped.length !== immediate.mapped.length) {
            syncProgressiveLevels(mapped, defaultIndex);
          }
        })
        .catch(() => {});
    }

    return immediate;
  }, [syncProgressiveLevels]);

  const resolveAndSyncHls = useCallback((
    hls: Hls,
    playerCap: number,
    fallbackHeights?: number[],
  ) => {
    return resolveHlsPreviewLevels(hls.levels, {
      initialHeight: resolveInitialHlsPreviewHeight(isClipPreview, playerCap, {
        youtube: isYoutubePreview,
      }),
      fallbackHeights: mergeVariantHeights(fallbackHeights),
    });
  }, [isClipPreview, isYoutubePreview]);

  return {
    previewLevels,
    qualityLevel,
    applyPlaybackHeight,
    syncPlaybackToViewport,
    applyQuality,
    setPreviewLevels: setLevels,
    setQualityLevel,
    // Hls instance ref — consumer sets this after creating the player
    setHlsRef,
    // Internal helpers for consumer setup
    syncProgressiveLevels,
    syncHlsLevels,
    resolveAndSyncProgressive,
    resolveAndSyncHls,
  } as PreviewPlayerState & PreviewPlayerActions & {
    setHlsRef: (hls: Hls | null) => void;
    syncProgressiveLevels: (mapped: PreviewLevelOption[], defaultIndex: number) => void;
    syncHlsLevels: (mapped: PreviewLevelOption[], defaultIndex: number) => void;
    resolveAndSyncProgressive: typeof resolveAndSyncProgressive;
    resolveAndSyncHls: typeof resolveAndSyncHls;
  };
}
