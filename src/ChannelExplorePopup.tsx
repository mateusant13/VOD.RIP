import {
  useState, useEffect, useLayoutEffect, useCallback, useRef,
  type KeyboardEvent, type PointerEvent as ReactPointerEvent,
} from 'react';
import Hls from 'hls.js';
import { Play, Pause, X, Volume2, VolumeX, Maximize2, Minimize2, ArrowRightToLine, Loader2 } from 'lucide-react';
import { apiGet, apiPost, apiDelete } from './hooks/useApiClient';
import PreviewQualityMenu from './PreviewQualityMenu';
import { usePreviewPlayer } from './hooks/usePreviewPlayer';
import {
  PREVIEW_CLIP_DEFAULT_HEIGHT,
  attachProgressivePreview,
  detachProgressivePreview,
  inferLevelHeight,
  initialPreviewPreferHeight,
  measurePlayerHeightCap,
  mergeVariantHeights,
  resolveHlsPreviewLevels,
  isClipPreviewUrl,
  resolvePreviewPlayback,
  resolveProgressivePreviewLevels,
  resolveProgressivePreviewLevelsAsync,
  type PreviewLevelOption,
  isValidPreviewUrl,
} from './previewPlayerUtils';
import {
  EXPLORE_PANEL_DEFAULT_W,
  EXPLORE_PANEL_CHROME_H_EST,
  EXPLORE_VIDEO_ASPECT_DEFAULT,
  VIEWPORT_EDGE_LOCK,
  PanelResizeHandles,
  clampExplorePanelWidth,
  layoutExplorePopupWindow,
  applyExplorePopupWindowPosition,
  applyExplorePopupFullscreenPosition,
  panelResizeHandleInset,
  startExplorePanelWidthResize,
  startFloatingPanelDrag,
  type PanelPos,
  type ResizeEdge,
} from './explorePopupUtils';

const PREVIEW_KEY_SKIP_SEC = 5;
const PREVIEW_FS_CONTROLS_HIDE_MS = 200;
const PREVIEW_DEFAULT_VOLUME = 0.3;

export interface ExplorePopupVod {
  url: string;
  title: string;
  platform: string;
  durationSec: number;
  platformListIndex: number;
  isClip: boolean;
}

interface ChannelExplorePopupProps {
  id: string;
  vod: ExplorePopupVod;
  zIndex: number;
  stackIndex: number;
  volumeMenuCloseTick: number;
  onClose: () => void;
  onCarryToUrl: (vod: ExplorePopupVod) => void;
  onRegisterPause: (id: string, pause: () => void) => void;
  onUnregisterPause: (id: string) => void;
  onVolumeMenuOpen: (id: string, open: boolean) => void;
  onBringToFront: () => void;
}

function formatHmsFull(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

function shouldIgnorePlayerKeyEvent(e: KeyboardEvent): boolean {
  if (e.ctrlKey || e.metaKey || e.altKey) return true;
  const el = e.target as HTMLElement;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el as HTMLInputElement).type;
    return type !== 'range' && type !== 'checkbox' && type !== 'radio';
  }
  return false;
}

function platformCardShadow(platform: 'kick' | 'twitch' | null): string {
  if (platform === 'kick') return 'shadow-[4px_4px_0px_0px_#53fc18]';
  if (platform === 'twitch') return 'shadow-[4px_4px_0px_0px_#9146FF]';
  return 'shadow-[4px_4px_0px_0px_#53fc18]';
}

export default function ChannelExplorePopup({
  id,
  vod,
  zIndex,
  stackIndex,
  volumeMenuCloseTick,
  onClose,
  onCarryToUrl,
  onRegisterPause,
  onUnregisterPause,
  onVolumeMenuOpen,
  onBringToFront,
}: ChannelExplorePopupProps) {
  const [playback, setPlayback] = useState<{
    url: string;
    kind: 'hls' | 'progressive';
    variantHeights?: number[];
    qualityLabels?: string[];
    activeHeight?: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(PREVIEW_DEFAULT_VOLUME);
  const [volumeMenuOpen, setVolumeMenuOpen] = useState(false);
  const [qualityMenuOpen, setQualityMenuOpen] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [panelWidth, setPanelWidth] = useState(EXPLORE_PANEL_DEFAULT_W);
  const [videoAspect, setVideoAspect] = useState(EXPLORE_VIDEO_ASPECT_DEFAULT);
  const [pos, setPos] = useState<PanelPos | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [fsControlsVisible, setFsControlsVisible] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  // Reset on VOD change — hook manages the runtime copies, but the
  // setup effect needs to clear these before initialising a new preview.
  // ponytail: keep in sync with hook's internal requestedHeightRef + appliedHeightRef
  const requestedHeightRef = useRef(0);
  const appliedHeightRef = useRef(0);
  const sessionIdRef = useRef<string | null>(null);
  const sessionMetaRef = useRef<{
    variantHeights: number[];
    qualityLabels?: string[];
    activeHeight: number;
  } | null>(null);
  const fsHideTimerRef = useRef<number | null>(null);
  const initialPlayDoneRef = useRef(false);
  const panelWidthRef = useRef(EXPLORE_PANEL_DEFAULT_W);
  const videoAspectRef = useRef(EXPLORE_VIDEO_ASPECT_DEFAULT);
  const posRef = useRef<PanelPos | null>(null);
  const chromeHRef = useRef(EXPLORE_PANEL_CHROME_H_EST);
  const videoWrapRef = useRef<HTMLDivElement>(null);
  const volumeRef = useRef(PREVIEW_DEFAULT_VOLUME);
  const suppressPlayRef = useRef(false);
  const {
    previewLevels,
    qualityLevel,
    syncPlaybackToViewport,
    applyQuality,
    setPreviewLevels,
    setQualityLevel,
    setHlsRef,
    syncHlsLevels,
  } = usePreviewPlayer({
    videoRef,
    playback,
    sessionId: sessionIdRef.current,
    isClipPreview: vod.isClip,
    containerRef,
    onPreviewError: (msg) => setError(msg),
  });


  const platform = vod.platform === 'Twitch' ? 'twitch' : 'kick';

  useEffect(() => {
    const pause = () => {
      videoRef.current?.pause();
      setPlaying(false);
    };
    onRegisterPause(id, pause);
    return () => onUnregisterPause(id);
  }, [id, onRegisterPause, onUnregisterPause]);

  useEffect(() => {
    onVolumeMenuOpen(id, volumeMenuOpen || qualityMenuOpen);
  }, [id, volumeMenuOpen, qualityMenuOpen, onVolumeMenuOpen]);

  useEffect(() => {
    setVolumeMenuOpen(false);
    setQualityMenuOpen(false);
  }, [volumeMenuCloseTick]);

  useEffect(() => {
    let cancelled = false;
    initialPlayDoneRef.current = false;
    setPlayback(null);
    setLoading(true);
    setReady(false);
    setError(null);
    setPreviewLevels([]);
    setQualityLevel(0);
    setQualityMenuOpen(false);
    requestedHeightRef.current = 0;
    appliedHeightRef.current = 0;

    (async () => {
      try {
        const playerCap = measurePlayerHeightCap(videoWrapRef.current, videoAspectRef.current);
        const preferHeight = initialPreviewPreferHeight(vod.isClip, playerCap);
        const clipInfoPromise = vod.isClip
          ? apiGet<{ qualities?: string[] }>(
            `/api/info/clip?id=${encodeURIComponent(vod.url)}`,
          ).catch(() => null)
          : Promise.resolve(null);
        const sessionPromise = apiPost<{
          session_id: string;
          master_url: string;
          playback_url?: string;
          kind?: string;
          variant_heights?: number[];
          quality_labels?: string[];
          active_height?: number;
        }>('/api/preview/session', {
          url: vod.url,
          crop_start: 0,
          crop_end: vod.durationSec,
          prefer_height: preferHeight,
        });
        const [clipInfo, res] = await Promise.all([clipInfoPromise, sessionPromise]);
        if (cancelled) {
          try { await apiDelete(`/api/preview/session/${res.session_id}`); } catch { /* ignore */ }
          return;
        }
        const mergedQualityLabels = clipInfo?.qualities?.length
          ? clipInfo.qualities
          : (res.quality_labels?.length ? res.quality_labels : undefined);
        const activeHeight = res.active_height ?? preferHeight;
        sessionMetaRef.current = {
          variantHeights: res.variant_heights ?? [],
          qualityLabels: mergedQualityLabels,
          activeHeight,
        };
        sessionIdRef.current = res.session_id;
        const resolved = resolvePreviewPlayback(vod.url, res);
        setPlayback({
          ...resolved,
          variantHeights: res.variant_heights ?? [],
          qualityLabels: mergedQualityLabels,
          activeHeight,
        });
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not start player');
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
      const video = videoRef.current;
      if (video) {
        detachProgressivePreview(video);
      }
      if (document.fullscreenElement === containerRef.current) {
        void document.exitFullscreen().catch(() => {});
      }
      const sid = sessionIdRef.current;
      sessionIdRef.current = null;
      sessionMetaRef.current = null;
      if (sid) {
        void apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      }
    };
  }, [vod.url, vod.durationSec]);









  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video || !ready) return;
    if (video.paused) {
      void video.play().catch(() => {});
      setPlaying(true);
    } else {
      video.pause();
      setPlaying(false);
    }
  }, [ready]);

  const setVolumeLevel = useCallback((level: number) => {
    const video = videoRef.current;
    if (!video) return;
    const v = Math.max(0, Math.min(1, level));
    video.volume = v;
    if (v > 0) volumeRef.current = v;
    setVolume(v);
    if (v <= 0) {
      video.muted = true;
      setMuted(true);
    } else {
      video.muted = false;
      setMuted(false);
    }
  }, []);

  const seekVideo = useCallback((sec: number) => {
    const video = videoRef.current;
    if (!video || !ready) return;
    const t = Math.max(0, Math.min(sec, vod.durationSec));
    if (Math.abs(video.currentTime - t) > 0.2) {
      video.currentTime = t;
    }
    setCurrentTime(t);
  }, [ready, vod.durationSec]);

  const skip = useCallback((deltaSec: number) => {
    const video = videoRef.current;
    if (!video || !ready) return;
    seekVideo(video.currentTime + deltaSec);
  }, [ready, seekVideo]);

  const focusPlayer = useCallback(() => {
    containerRef.current?.focus();
  }, []);

  const onPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    startExplorePanelWidthResize(e, edge, panelWidthRef, setPanelWidth, {
      panelEl: containerRef.current,
      aspect: videoAspectRef.current,
      posRef,
      setPos,
      clampWidth: (w) => clampExplorePanelWidth(w, chromeHRef.current, videoAspectRef.current),
    });
  }, []);

  const onPopupDrag = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (fullscreen) return;
    const t = e.target as HTMLElement;
    if (t.tagName === 'VIDEO') return;
    if (t.closest('button, input, select, textarea, a, [role="slider"], [data-player-menu]')) return;
    const el = containerRef.current;
    if (!el) return;
    if (!posRef.current) {
      posRef.current = layoutExplorePopupWindow(el, panelWidthRef.current, posRef, stackIndex);
      setPos(posRef.current);
    }
    startFloatingPanelDrag(e, posRef, setPos, el);
  }, [fullscreen, stackIndex]);

  const toggleFullscreen = useCallback(async () => {
    const container = containerRef.current;
    if (!container || !ready) return;
    try {
      if (!document.fullscreenElement) {
        await container.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      /* fullscreen denied */
    }
  }, [ready]);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (!ready) return;
    if (shouldIgnorePlayerKeyEvent(e)) return;
    const { key } = e;
    if (
      ![' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(key)
      && key.toLowerCase() !== 'f'
    ) {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    if (key === ' ') { togglePlay(); return; }
    if (key === 'ArrowLeft') { skip(-PREVIEW_KEY_SKIP_SEC); return; }
    if (key === 'ArrowRight') { skip(PREVIEW_KEY_SKIP_SEC); return; }
    if (key === 'ArrowUp') { setVolumeLevel(volumeRef.current + 0.1); return; }
    if (key === 'ArrowDown') { setVolumeLevel(volumeRef.current - 0.1); return; }
    if (key.toLowerCase() === 'f') { void toggleFullscreen(); }
  }, [ready, togglePlay, skip, setVolumeLevel, toggleFullscreen]);

  const bumpFsControls = useCallback(() => {
    setFsControlsVisible(true);
    if (fsHideTimerRef.current) window.clearTimeout(fsHideTimerRef.current);
    if (fullscreen) {
      fsHideTimerRef.current = window.setTimeout(() => {
        setFsControlsVisible(false);
      }, PREVIEW_FS_CONTROLS_HIDE_MS);
    }
  }, [fullscreen]);

  useEffect(() => {
    const onFullscreenChange = () => {
      const fs = document.fullscreenElement === containerRef.current;
      setFullscreen(fs);
      setFsControlsVisible(!fs);
      const el = containerRef.current;
      if (!el) return;
      if (fs) {
        applyExplorePopupFullscreenPosition(el);
      } else if (posRef.current) {
        applyExplorePopupWindowPosition(el, posRef.current);
      } else {
        const p = layoutExplorePopupWindow(el, panelWidthRef.current, posRef, stackIndex);
        setPos(p);
      }
      void syncPlaybackToViewport(fs);
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, [stackIndex, syncPlaybackToViewport]);

  useEffect(() => {
    if (!ready || fullscreen) return;
    void syncPlaybackToViewport();
  }, [ready, fullscreen, panelWidth, videoAspect, syncPlaybackToViewport]);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    if (fullscreen) {
      applyExplorePopupFullscreenPosition(el);
      return;
    }
    const p = layoutExplorePopupWindow(el, panelWidth, posRef, stackIndex);
    setPos((prev) => (prev?.x === p.x && prev?.y === p.y ? prev : p));
  }, [fullscreen, panelWidth, videoAspect, stackIndex]);

  useEffect(() => {
    if (fullscreen) return;
    const fit = () => {
      const clampedW = clampExplorePanelWidth(
        panelWidthRef.current,
        chromeHRef.current,
        videoAspectRef.current,
      );
      panelWidthRef.current = clampedW;
      setPanelWidth(clampedW);
      const el = containerRef.current;
      if (!el) return;
      const p = layoutExplorePopupWindow(el, clampedW, posRef, stackIndex);
      setPos(p);
    };
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, [fullscreen, stackIndex]);

  useEffect(() => {
    if (fullscreen || !containerRef.current || !videoWrapRef.current) return;
    const chromeH = containerRef.current.offsetHeight - videoWrapRef.current.offsetHeight;
    if (chromeH > 0) chromeHRef.current = chromeH;
  }, [fullscreen, panelWidth, videoAspect, ready]);

  useEffect(() => {
    if (!playback?.url) return;
    let cancelled = false;
    let cleanup: (() => void) | undefined;

    const setup = () => {
      if (cancelled) return;
      const video = videoRef.current;
      if (!video) {
        requestAnimationFrame(setup);
        return;
      }
      const { url: playbackUrl, kind: playbackKind } = playback;

    setLoading(true);
    setReady(false);

    const onCanPlay = () => {
      setReady(true);
      setLoading(false);
      video.volume = PREVIEW_DEFAULT_VOLUME;
      volumeRef.current = PREVIEW_DEFAULT_VOLUME;
      setVolume(PREVIEW_DEFAULT_VOLUME);
      video.muted = false;
      setMuted(false);
      if (!initialPlayDoneRef.current && video.paused) {
        initialPlayDoneRef.current = true;
        void video.play().catch(() => {
          video.muted = true;
          setMuted(true);
          void video.play().catch(() => {});
        });
      }
    };

    if (playbackKind === 'progressive' || isClipPreviewUrl(vod.url)) {
      const meta = sessionMetaRef.current;
      const activeH = meta?.activeHeight
        ?? playback.activeHeight
        ?? PREVIEW_CLIP_DEFAULT_HEIGHT;
      const syncProgressiveLevels = (
        mapped: PreviewLevelOption[],
        defaultIndex: number,
      ) => {
        if (cancelled) return;
        setPreviewLevels(mapped);
        setQualityLevel(defaultIndex);
        const picked = mapped[defaultIndex];
        if (picked?.height) requestedHeightRef.current = picked.height;
      };
      const levelOpts = {
        variantHeights: meta?.variantHeights ?? playback.variantHeights,
        qualityLabels: meta?.qualityLabels ?? playback.qualityLabels,
        initialHeight: activeH,
      };
      const immediate = resolveProgressivePreviewLevels(levelOpts);
      syncProgressiveLevels(immediate.mapped, immediate.defaultIndex);
      void resolveProgressivePreviewLevelsAsync(
        vod.url,
        levelOpts,
        async (clipUrl) => {
          const info = await apiGet<{ qualities?: string[] }>(
            `/api/info/clip?id=${encodeURIComponent(clipUrl)}`,
          );
          return info.qualities;
        },
      ).then(({ mapped, defaultIndex, qualityLabels: resolvedLabels }) => {
        if (resolvedLabels?.length && meta) {
          sessionMetaRef.current = { ...meta, qualityLabels: resolvedLabels };
        }
        if (mapped.length !== immediate.mapped.length) {
          syncProgressiveLevels(mapped, defaultIndex);
        }
      }).catch(() => { /* keep immediate levels */ });
      const onVideoError = () => {
        setError('Clip preview failed — try again');
        setLoading(false);
      };
      appliedHeightRef.current = activeH;
      attachProgressivePreview(video, playbackUrl);
      video.addEventListener('canplay', onCanPlay, { once: true });
      video.addEventListener('error', onVideoError, { once: true });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeEventListener('error', onVideoError);
        detachProgressivePreview(video);
      };
      return;
    }

    if (Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        maxBufferLength: 20,
        maxMaxBufferLength: 40,
        startFragPrefetch: true,
        capLevelToPlayerSize: true,
        fragLoadingTimeOut: 20000,
        manifestLoadingTimeOut: 10000,
        testBandwidth: false,
        startPosition: 0,
      });
      hlsRef.current = hls;
      setHlsRef(hls);
      hls.loadSource(playbackUrl);
      hls.attachMedia(video);
      let levelsInitialized = false;
      const playerCap = measurePlayerHeightCap(videoWrapRef.current, videoAspectRef.current);
      const preferHeight = initialPreviewPreferHeight(vod.isClip, playerCap);
      const fallbackHeights = mergeVariantHeights(playback.variantHeights);
      const syncPreviewLevels = (levels = hls.levels, applyDefault = false) => {
        const cappedDefault = Math.min(preferHeight, playerCap);
        const { mapped, defaultIndex } = resolveHlsPreviewLevels(levels, {
          initialHeight: cappedDefault,
          fallbackHeights,
        });
        if (!mapped.length) return;
        if (!levelsInitialized || applyDefault) {
          levelsInitialized = true;
          const hlsIndex = mapped[defaultIndex]?.index ?? defaultIndex;
          if (hls.levels.length > 0 && hlsIndex >= 0 && hlsIndex < hls.levels.length) {
            const levelHeight = inferLevelHeight(hls.levels[hlsIndex]);
            if (levelHeight > 0) {
              hls.loadLevel = hlsIndex;
            }
          }
          syncHlsLevels(mapped, defaultIndex);
        } else {
          setPreviewLevels(mapped);
        }
      };

      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        syncPreviewLevels(data.levels ?? hls.levels, true);
      });
      hls.on(Hls.Events.LEVELS_UPDATED, () => {
        syncPreviewLevels(hls.levels);
      });
      video.addEventListener('canplay', onCanPlay, { once: true });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            hls.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError();
            break;
          default:
            setError('Playback failed — try again');
            setLoading(false);
            hls.destroy();
            hlsRef.current = null;
            break;
        }
      });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        hls.destroy();
        hlsRef.current = null;
        setHlsRef(null);
      };
      return;
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      if (!isValidPreviewUrl(playbackUrl)) {
        setError('Invalid playback URL');
        setLoading(false);
        return;
      }
      video.src = playbackUrl;
      video.addEventListener('canplay', onCanPlay, { once: true });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeAttribute('src');
        video.load();
      };
      return;
    }

    setError('HLS playback is not supported in this browser');
    setLoading(false);
    };

    setup();
    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, [playback, vod.isClip]);

  useEffect(() => {
    if (!ready) return;
    const t = window.setTimeout(() => focusPlayer(), 0);
    return () => window.clearTimeout(t);
  }, [ready, focusPlayer]);

  const ctrlBtn = (fs: boolean) => fs
    ? 'border border-white/20 bg-black/25 text-zinc-100 p-2 disabled:opacity-30 backdrop-blur-[1px]'
    : 'border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40';

  const fsCtrlBtn = 'border border-white/20 bg-black/25 text-zinc-100 p-2 disabled:opacity-30 backdrop-blur-[1px]';

  const timelineUi = (
    <div className="flex items-center gap-1.5 w-full shrink-0">
      <span className={`text-[9px] font-mono w-10 shrink-0 ${fullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}>
        {formatHmsFull(currentTime)}
      </span>
      <input
        type="range"
        min={0}
        max={vod.durationSec}
        step={0.25}
        value={Math.min(currentTime, vod.durationSec)}
        disabled={!ready}
        onChange={(e) => seekVideo(parseFloat(e.target.value))}
        className="flex-1 accent-white disabled:opacity-40 h-1"
      />
      <span className={`text-[9px] font-mono w-10 shrink-0 text-right ${fullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}>
        {formatHmsFull(vod.durationSec)}
      </span>
    </div>
  );

  const volumeUi = (fs: boolean) => (
    <div className="relative" data-player-menu>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setQualityMenuOpen(false);
          setVolumeMenuOpen((o) => !o);
        }}
        disabled={!ready}
        className={fs ? fsCtrlBtn : ctrlBtn(false)}
        title="Volume"
      >
        {muted || volume <= 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
      </button>
      {volumeMenuOpen && (
        <div
          className={`absolute bottom-full left-0 mb-1.5 z-30 flex items-center gap-2 px-2.5 py-2 shadow-lg ${
            fs ? 'border border-white/20 bg-black/85 backdrop-blur-sm' : 'border-2 border-zinc-600 bg-zinc-950'
          }`}
          onClick={(e) => e.stopPropagation()}
        >
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={muted ? 0 : volume}
            disabled={!ready}
            onChange={(e) => setVolumeLevel(parseFloat(e.target.value))}
            className={`w-24 accent-white ${fs ? 'h-1' : 'h-1.5'}`}
          />
        </div>
      )}
    </div>
  );

  const qualityUi = (fs: boolean) => (
    <PreviewQualityMenu
      levels={previewLevels}
      currentLevel={qualityLevel}
      menuOpen={qualityMenuOpen}
      setMenuOpen={setQualityMenuOpen}
      onSelect={(idx: number) => { void applyQuality(idx); setQualityMenuOpen(false); }}
      disabled={!ready}
      buttonClassName={fs ? fsCtrlBtn : ctrlBtn(false)}
      onMenuOpen={() => setVolumeMenuOpen(false)}
      popoverClassName={fs
        ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
        : 'border-2 border-zinc-600 bg-zinc-950'}
    />
  );

  return (
    <div
      ref={containerRef}
      tabIndex={0}
      role="application"
      aria-label={vod.isClip ? 'Channel clip explore player' : 'Channel VOD explore player'}
      onKeyDown={handleKeyDown}
      onPointerDownCapture={onBringToFront}
      onClick={focusPlayer}
      className={`group outline-none focus:ring-2 focus:ring-white/25 flex flex-col overflow-visible bg-zinc-950 ${
        fullscreen
          ? 'explore-fs-host min-h-0 p-0 gap-0 border-0 shadow-none'
          : `p-3 gap-2 border-2 border-white ${platformCardShadow(platform)}`
      }`}
      style={fullscreen ? {
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        zIndex,
        width: '100vw',
        height: '100vh',
      } : {
        position: 'fixed',
        zIndex,
        width: panelWidth,
        ...(pos
          ? { top: pos.y, left: pos.x, right: 'auto', bottom: 'auto' }
          : {
            top: 'auto',
            left: 'auto',
            bottom: VIEWPORT_EDGE_LOCK - stackIndex * 28,
            right: VIEWPORT_EDGE_LOCK - stackIndex * 28,
          }),
      }}
      onMouseMove={fullscreen ? bumpFsControls : undefined}
    >
      <div
        className={`flex flex-col ${fullscreen ? 'relative h-full min-h-0 w-full gap-0' : 'gap-2 relative cursor-grab active:cursor-grabbing select-none'}`}
        onPointerDown={fullscreen ? undefined : onPopupDrag}
      >
        {!fullscreen && (
          <div className="flex items-start justify-between gap-2 shrink-0">
            <div className="min-w-0 flex items-start gap-1.5">
              <span
                className={`shrink-0 w-5 text-center text-[11px] font-mono font-bold tabular-nums leading-tight pt-0.5 ${
                  platform === 'kick' ? 'text-[#53fc18]' : 'text-[#9146FF]'
                }`}
                title={`${vod.platform} #${vod.platformListIndex}`}
              >
                {vod.platformListIndex}
              </span>
              <div className="min-w-0">
                <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
                  {vod.isClip ? 'Channel clip explore' : 'Channel VOD explore'}
                </span>
                <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
                  {vod.title}
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => onClose()}
              className="text-zinc-500 hover:text-white p-0.5 shrink-0"
              title="Close player"
            >
              <X size={14} />
            </button>
          </div>
        )}
        <div
          ref={videoWrapRef}
          className={`relative bg-black overflow-hidden w-full cursor-pointer ${
            fullscreen ? 'absolute inset-0 z-0 border-0' : 'border-2 border-zinc-700 shrink-0'
          }`}
          style={fullscreen ? undefined : { aspectRatio: videoAspect }}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => {
            if (ready) togglePlay();
          }}
        >
          <video
            ref={videoRef}
            className="w-full h-full object-contain pointer-events-none"
            muted={muted}
            playsInline
            onLoadedMetadata={() => {
              const video = videoRef.current;
              if (!video?.videoWidth || !video?.videoHeight) return;
              const aspect = video.videoWidth / video.videoHeight;
              videoAspectRef.current = aspect;
              setVideoAspect(aspect);
              const clampedW = clampExplorePanelWidth(
                panelWidthRef.current,
                chromeHRef.current,
                aspect,
              );
              panelWidthRef.current = clampedW;
              setPanelWidth(clampedW);
            }}
            onTimeUpdate={() => {
              const video = videoRef.current;
              if (video) setCurrentTime(video.currentTime);
            }}
            onPlay={() => {
              if (suppressPlayRef.current) {
                videoRef.current?.pause();
                return;
              }
              setPlaying(true);
            }}
            onPause={() => setPlaying(false)}
          />
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/60 z-20">
              <Loader2 size={28} className="animate-spin text-zinc-300" />
            </div>
          )}
          {error && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/80 z-20 p-3">
              <p className="text-red-400 text-[10px] font-mono text-center">{error}</p>
            </div>
          )}
          {fullscreen && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onClose(); }}
              className="absolute top-3 right-3 z-20 text-zinc-400 hover:text-white p-2 pointer-events-auto"
              title="Close player"
            >
              <X size={20} />
            </button>
          )}
        </div>
        {fullscreen && (
          <>
            <div
              data-player-controls
              className={`absolute bottom-0 left-0 right-0 z-10 flex flex-col gap-1.5 px-3 pb-3 pt-2 bg-gradient-to-t from-black/90 to-black/75 transition-opacity duration-150 ${
                fsControlsVisible ? 'opacity-100' : 'opacity-0 pointer-events-none'
              }`}
              onClick={(e) => e.stopPropagation()}
              onPointerDown={(e) => e.stopPropagation()}
              onPointerUp={(e) => e.stopPropagation()}
              onMouseMove={bumpFsControls}
            >
              {timelineUi}
              <div className="flex items-center gap-2">
                <button type="button" onClick={togglePlay} disabled={!ready} className={fsCtrlBtn}>
                  {playing ? <Pause size={18} /> : <Play size={18} />}
                </button>
                {volumeUi(true)}
                {qualityUi(true)}
                <button
                  type="button"
                  onClick={() => onCarryToUrl(vod)}
                  className="border border-white/20 bg-black/25 text-zinc-100 px-2 py-2 backdrop-blur-[1px] flex items-center gap-1 text-[8px] font-bold uppercase tracking-wider"
                  title="Send to URL panel for rip"
                >
                  <ArrowRightToLine size={14} />
                  URL
                </button>
                <button
                  type="button"
                  onClick={() => void toggleFullscreen()}
                  disabled={!ready}
                  className="ml-auto border-2 border-white bg-black text-white hover:bg-white hover:text-black p-2 disabled:opacity-40 shadow-[2px_2px_0px_0px_#53fc18]"
                  title="Exit fullscreen"
                >
                  <Minimize2 size={18} />
                </button>
              </div>
            </div>
            <div
              className="absolute bottom-0 right-0 z-30 w-10 h-10 cursor-pointer"
              title="Exit fullscreen"
              onClick={() => void toggleFullscreen()}
            />
          </>
        )}
        {!fullscreen && (
          <>
            {timelineUi}
            <p className="text-[8px] font-mono text-zinc-600 uppercase tracking-wider text-center shrink-0">
              Fullscreen to explore
            </p>
            <div className="flex items-center justify-between gap-2 shrink-0">
              <div className="flex items-center gap-1.5">
                <button type="button" onClick={togglePlay} disabled={!ready} className={ctrlBtn(false)}>
                  {playing ? <Pause size={18} /> : <Play size={18} />}
                </button>
                {volumeUi(false)}
                {qualityUi(false)}
                <button
                  type="button"
                  onClick={() => onCarryToUrl(vod)}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white px-2 py-2 disabled:opacity-40 flex items-center gap-1 text-[8px] font-bold uppercase tracking-wider"
                  title="Send to URL panel for rip"
                >
                  <ArrowRightToLine size={14} />
                  URL
                </button>
              </div>
              <button
                type="button"
                onClick={() => void toggleFullscreen()}
                disabled={!ready}
                className="border-2 border-white bg-black text-white hover:bg-white hover:text-black p-2 disabled:opacity-40 shadow-[2px_2px_0px_0px_#53fc18]"
                title="Fullscreen"
              >
                <Maximize2 size={18} />
              </button>
            </div>
          </>
        )}
      </div>
      {!fullscreen && (
        <PanelResizeHandles onPointerDown={onPanelResize} insetPx={panelResizeHandleInset(true)} />
      )}
    </div>
  );
}
