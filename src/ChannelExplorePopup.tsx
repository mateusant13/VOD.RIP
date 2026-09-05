import {
  useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef,
  type KeyboardEvent, type PointerEvent as ReactPointerEvent,
} from 'react';
import Hls from 'hls.js';
import { createTwitchAdRotationHandler, twitchAdBlockHlsConfig } from './twitchAdBlock';
import { Play, Pause, X, Volume2, VolumeX, Maximize2, Minimize2, Download, Loader2, RefreshCw, Search, AlertCircle, MessageSquare, Sparkles, ChevronDown } from 'lucide-react';
import { apiGet, apiPost, apiDelete } from './hooks/useApiClient';
import { archiveVideoIdFromUrl } from './archiveScope';
import TwitchClipPopup from './components/TwitchClipPopup';
import { useI18n } from './i18n';
import TwitchLogoIcon from './components/TwitchLogoIcon';
import ArchiveSearchPopup from './components/ArchiveSearchPopup';
import PreviewChatPanel, { readPreviewChatPanelWidth } from './components/PreviewChatPanel';
import type { ChatMarkers } from './components/ChatRangeMarkers';
import PreviewQualityMenu from './PreviewQualityMenu';
import { usePreviewPlayer } from './hooks/usePreviewPlayer';
import { useInstantPreview } from './hooks/useInstantPreview';
import {
  PREVIEW_CLIP_DEFAULT_HEIGHT,
  attachProgressivePreview,
  bindProgressivePreviewRecovery,
  detachProgressivePreview,
  isClipRelativePreviewDuration,
  resolvePreviewDurationSec,
  inferLevelHeight,
  initialPreviewPreferHeight,
  resolveInitialHlsPreviewHeight,
  measurePlayerHeightCap,
  mergeVariantHeights,
  resolveHlsPreviewLevels,
  isClipPreviewUrl,
  resolvePreviewPlayback,
  previewSessionRefreshHandoff,
  previewSeekOptimisticUi,
  resolveProgressivePreviewLevels,
  resolveProgressivePreviewLevelsAsync,
  seekYoutubeWindowHls,
  windowHlsVideoTimeSec,
  windowHlsVodTimeSec,
  isYoutubeWindowHlsPreview,
  isPositionInWindowHlsMux,
  PREVIEW_SEEK_DEBOUNCE_MS,
  youtubePreviewAllowHeights,
  attachPreviewBufferingListeners,
  applyVideoLocalSeek,
  reloadWindowHlsAtPosition,
  shieldPreviewBuffering,
  playPreviewWithAudio,
  unlockPreviewAudioFromGesture,
  type PreviewLevelOption,
  isValidPreviewUrl,
  createPreviewSessionWithRetry,
  pinHlsToLowestLevel,
} from './previewPlayerUtils';
import { PreviewTiming, waitVideoPlayable } from './previewTiming';
import { PreviewStartTimeout } from './previewStartTimeout';
import { PREVIEW_DEFAULT_VOLUME } from './layoutUtils';
import { pauseOtherPreviews, autoPauseOtherPreviews, noteUserUnpause, registerPreviewPlayback } from './previewPlaybackBus';
import { youtubeIframeCommand, youtubeIframeListen } from './youtubeEmbed';
import {
  EXPLORE_PANEL_DEFAULT_W,
  EXPLORE_PANEL_MIN_W,
  EXPLORE_PANEL_CHROME_H_EST,
  EXPLORE_VIDEO_ASPECT_DEFAULT,
  VIEWPORT_EDGE_LOCK,
  PanelResizeHandles,
  clampExplorePanelWidth,
  layoutExplorePopupWindow,
  applyExplorePopupWindowPosition,
  applyExplorePopupFullscreenPosition,
  startExplorePanelWidthResize,
  startFloatingPanelDrag,
  type PanelPos,
  type ResizeEdge,
} from './explorePopupUtils';
import type { FrameRect } from './frameLayout';
import { encodeFrameDragPopupId } from './frameLayout';
import { formatHmsFull } from './utils';
import { createFullscreenGate, FULLSCREEN_SETTLE_FALLBACK_MS, type FullscreenGate } from './utils/fullscreenGate';
import type { PreviewSessionResponse, AiAskResponse } from './types';
import type { ArchiveSearchHit, ArchiveVideoRow } from './archiveSearchUtils';
import { resolveVideoThumbnail, isSyntheticArchiveId } from './channelUtils';
import { channelVodSubline } from './channelUtils';
import { platformPreviewCtrlBtn, platformCardShadow, type PlatformStyleKey } from './platformStyles';
import { platformAccentColor } from './platformColors';
import { previewRetryAfterError, previewRetryMode, type PreviewRetryStage, type PreviewRetryState } from './previewRetry';

function explorePlatformKey(raw: string): PlatformStyleKey {
  const p = raw.toLowerCase();
  if (p === 'twitch') return 'twitch';
  if (p === 'youtube') return 'youtube';
  return 'kick';
}

const PREVIEW_KEY_SKIP_SEC = 5;
const PREVIEW_FS_CONTROLS_HIDE_MS = 200;

export interface ExplorePopupVod {
  url: string;
  title: string;
  platform: string;
  durationSec: number;
  platformListIndex: number;
  isClip: boolean;
  thumbnailUrl?: string | null;
  /** Channel metadata for the subline (days-ago · length · views). */
  created_at?: string | null;
  views?: number | null;
  duration_string?: string | null;
  /** Open the player already positioned at this VOD time (archive search seek-to-moment). */
  initialTimeSec?: number;
  /** Native archive video id (same value as archive DB videos.video_id) —
   *  enables the header's SEARCH THIS VIDEO button; absent → button hidden. */
  videoId?: string;
  /** Channel slug/login (broadcaster login for Twitch) — enables the CLIP button. */
  channel?: string;
  /** WS-3: detected channel language ('' / absent = unknown). */
  channel_language?: string | null;
}

interface ChannelExplorePopupProps {
  id: string;
  vod: ExplorePopupVod;
  zIndex: number;
  stackIndex: number;
  onClose: () => void;
  /** Hand the mini preview to the app MAIN preview: plays `timeSec` (VOD
   *  absolute). `chat` carries the chat-range markers selected in the mini
   *  preview's chat panel (null when neither start nor end is set). */
  onHandoffToMain: (
    vod: ExplorePopupVod,
    timeSec: number,
    trim?: { start: number; end: number } | null,
    chat?: ChatMarkers | null,
  ) => void;
  onRegisterPause: (id: string, pause: () => void) => void;
  onUnregisterPause: (id: string) => void;
  onBringToFront: () => void;
  /** Open an archive hit in the explore-player flow (App owns the popup stack). */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => void;
  /** When set, popup is snapped into a frame grid cell (frame mode). */
  frameSnapRect?: FrameRect | null;
  frameMode?: boolean;
  /** Release the popup from its frame cell so a drag can move it freely. */
  onUnsnap?: () => void;
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

export default function ChannelExplorePopup({
  id,
  vod,
  zIndex,
  stackIndex,
  onClose,
  onHandoffToMain,
  onRegisterPause,
  onUnregisterPause,
  onBringToFront,
  onOpenHit,
  frameSnapRect = null,
  frameMode = false,
  onUnsnap,
}: ChannelExplorePopupProps) {
  const { t } = useI18n();
  const [playback, setPlayback] = useState<{
    url: string;
    kind: 'hls' | 'progressive';
    variantHeights?: number[];
    qualityLabels?: string[];
    activeHeight?: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [buffering, setBuffering] = useState(false);
  const [ready, setReady] = useState(false);
  const [youtubeEmbed, setYoutubeEmbed] = useState<string | null>(null);
  const youtubeIframeRef = useRef<HTMLIFrameElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(PREVIEW_DEFAULT_VOLUME);
  const [volumeHovered, setVolumeHovered] = useState(false);
  const [qualityMenuOpen, setQualityMenuOpen] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  /** Twitch clip editor open — transient notice in the transport row. */
  const [clipNotice, setClipNotice] = useState<{ kind: 'error' | 'ok'; text: string } | null>(null);
  /** Twitch clip mini-preview — opened at the current playhead. */
  const [clipPopup, setClipPopup] = useState<{
    url: string;
    broadcasterLogin: string;
    vodId: string;
    playheadSec: number;
    vodDurationSec: number;
    reuseSession?: { sessionId: string; trimTimeline: boolean } | null;
  } | null>(null);
  const clipNoticeTimerRef = useRef<number | null>(null);
  const [mediaDurationSec, setMediaDurationSec] = useState(0);
  const [sessionDurationSec, setSessionDurationSec] = useState(0);
  const [windowHlsMuxEndSec, setWindowHlsMuxEndSec] = useState(0);
  const [panelWidth, setPanelWidth] = useState(EXPLORE_PANEL_DEFAULT_W);
  /** Chat-history column alongside the video; the panel reports its footprint.
   *  The container grows to panelWidth + footprint so the video never shrinks
   *  when the chat opens. The explore popup starts collapsed (strip only) so
   *  the mini preview is player-sized by default; the main preview starts
   *  closed too — only its header Chat toggle opens it. */
  const [chatInfo, setChatInfo] = useState<{ open: boolean; width: number }>(() => ({
    open: false,
    width: readPreviewChatPanelWidth(),
  }));
  const chatTotalRef = useRef(0);
  const chatTotal = chatInfo.open ? chatInfo.width + 8 : chatInfo.width; // gap-2 row
  chatTotalRef.current = chatTotal;
  const containerW = panelWidth + chatTotal;
  /** Chat-range markers (start/end) selected in the mini preview's chat
   *  panel — lifted via onMarkersChange so the Download handoff can carry
   *  them to the main preview (the next download writes the chat txt over
   *  [start, end]). */
  const chatMarkersRef = useRef<ChatMarkers>({ start: null, end: null });
  const handleChatMarkersChange = useCallback((markers: ChatMarkers) => {
    chatMarkersRef.current = markers;
  }, []);
  const [videoAspect, setVideoAspect] = useState(EXPLORE_VIDEO_ASPECT_DEFAULT);
  const [pos, setPos] = useState<PanelPos | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [fsControlsVisible, setFsControlsVisible] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** Docked archive-search panel (SEARCH THIS VIDEO) inside this popup. */
  const [archiveSearchOpen, setArchiveSearchOpen] = useState(false);
  /** Chat column open state — host-controlled (button next to Search), so
   *  the panel renders no collapsed side strip. */
  const [chatOpen, setChatOpen] = useState(false);
  /** Experimental AI ask-about-channel. aiEnabled comes from a GET
   *  /api/settings on mount (the feature is off by default); the ask row is
   *  hidden entirely while disabled. */
  const [aiEnabled, setAiEnabled] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  const [askQuestion, setAskQuestion] = useState('');
  const [askScope, setAskScope] = useState<'chat' | 'transcript' | 'all'>('all');
  const [askDays, setAskDays] = useState('');
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const [askResult, setAskResult] = useState<AiAskResponse | null>(null);
  // Per-media preview retry: which single media failed + how many retries
  // already failed, so the overlay's RETRY button escalates stage → full.
  const [previewRetry, setPreviewRetry] = useState<PreviewRetryState | null>(null);
  const previewRetryRef = useRef<PreviewRetryState | null>(null);
  const previewRetryingRef = useRef(false);
  // Bumping these re-runs the session-create / playback-attach effects.
  const [sessionRetryTick, setSessionRetryTick] = useState(0);
  const [attachRetryTick, setAttachRetryTick] = useState(0);
  const setPreviewRetryBoth = useCallback((s: PreviewRetryState | null) => {
    previewRetryRef.current = s;
    setPreviewRetry(s);
  }, []);

  /** Record which stage failed for this popup's media, keeping the per-media
   *  retry count so the NEXT RETRY click escalates stage retry → full pipeline. */
  const markPreviewError = useCallback((stage: PreviewRetryStage) => {
    const wasRetry = previewRetryingRef.current;
    previewRetryingRef.current = false;
    setPreviewRetryBoth(previewRetryAfterError(previewRetryRef.current, vod.url, stage, wasRetry));
  }, [vod.url, setPreviewRetryBoth]);

  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  /** Hard bound for the 'Starting…' phase — see PreviewStartTimeout. */
  const previewStartTimeoutRef = useRef<PreviewStartTimeout | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  // Reset on VOD change — hook manages the runtime copies, but the
  // setup effect needs to clear these before initialising a new preview.
  // ponytail: keep in sync with hook's internal requestedHeightRef + appliedHeightRef
  const requestedHeightRef = useRef(0);
  const appliedHeightRef = useRef(0);
  const sessionIdRef = useRef<string | null>(null);
  const extractSourceRef = useRef('');
  const clipRelativeRef = useRef(false);
  const trimTimelineRef = useRef(false);
  const windowHlsMuxStartRef = useRef(0);
  const windowHlsMuxEndRef = useRef(0);
  const seekInflightRef = useRef(0);
  const seekLockedRef = useRef(false);
  const bufferingClearRef = useRef<(() => void) | null>(null);
  const sessionMetaRef = useRef<{
    variantHeights: number[];
    qualityLabels?: string[];
    activeHeight: number;
    /** Quality policy: YouTube session resolved without user auth — 360p only. */
    anonymous?: boolean;
    /** True for create_live_session sessions — YouTube live tiers allowed. */
    isLive?: boolean;
  } | null>(null);
  const fsHideTimerRef = useRef<number | null>(null);
  const initialPlayDoneRef = useRef(false);
  const panelWidthRef = useRef(EXPLORE_PANEL_DEFAULT_W);
  const videoAspectRef = useRef(EXPLORE_VIDEO_ASPECT_DEFAULT);
  const posRef = useRef<PanelPos | null>(null);
  const chromeHRef = useRef(EXPLORE_PANEL_CHROME_H_EST);
  // Bumped whenever the measured chrome height changes, so the snap-pin
  // layout effect re-runs and keeps the popup centered after late chrome
  // growth (error banner, subtitle row, etc.). chromeHRef alone is invisible
  // to React.
  const [chromeHVersion, setChromeHVersion] = useState(0);
  const videoWrapRef = useRef<HTMLDivElement>(null);
  const videoPointerRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  /** Player column (video + chrome + optional archive-search dock). Its
   *  content height bounds the row, so the self-stretching chat column can
   *  never inflate the popup to the unbounded chat list height (~112k px). */
  const playerColRef = useRef<HTMLDivElement>(null);
  const playerRowRef = useRef<HTMLDivElement>(null);
  const fullscreenRef = useRef(false);
  fullscreenRef.current = fullscreen;
  /** Pinned row height (px); 0 = pre-measure (row auto-sized for one frame). */
  const playerColHRef = useRef(0);
  const volumeRef = useRef(PREVIEW_DEFAULT_VOLUME);
  useEffect(() => {
    const pause = () => {
      const video = videoRef.current;
      if (video && !video.paused) {
        video.pause();
        setPlaying(false);
      }
    };
    return registerPreviewPlayback(pause);
  }, []);
  /** Experimental AI flag — off by default; the ask row renders only when
   *  the user enabled the feature + saved a key in Settings. */
  useEffect(() => {
    let alive = true;
    apiGet<{ experimental_ai_enabled?: boolean }>('/api/settings')
      .then((s) => { if (alive) setAiEnabled(s.experimental_ai_enabled === true); })
      .catch(() => { /* backend unreachable → feature stays hidden */ });
    return () => { alive = false; };
  }, []);
  /** One RAG question: local archive search + one LLM call (backend). */
  const askAi = async () => {
    const question = askQuestion.trim();
    if (!question || askLoading) return;
    setAskLoading(true);
    setAskError(null);
    try {
      const daysRaw = askDays.trim();
      const daysNum = daysRaw === '' ? null : Number(daysRaw);
      const days = daysNum !== null && Number.isFinite(daysNum) && daysNum >= 1
        ? Math.floor(daysNum)
        : null;
      const res = await apiPost<AiAskResponse>('/api/ai/ask', {
        channel: vod.channel ?? '',
        platform: vod.platform,
        question,
        scope: askScope,
        days,
      });
      setAskResult(res);
    } catch (err) {
      setAskError(err instanceof Error ? err.message : String(err));
      setAskResult(null);
    } finally {
      setAskLoading(false);
    }
  };
  const seekDebounceRef = useRef<number | null>(null);
  const playbackKindRef = useRef<'hls' | 'progressive'>('progressive');
  const pendingSeekSecRef = useRef<number | null>(null);
  /** One-shot seek-on-open (archive search): target + whether it was applied. */
  const initialSeekRef = useRef<{ target: number; consumed: boolean } | null>(null);
  const seekTargetRef = useRef<number | null>(null);
  const cachedProgressiveRef = useRef(false);
  const previewTimingRef = useRef<PreviewTiming | null>(null);
  const suppressPlayRef = useRef(false);
  /** Load start of the current preview — any user unpause at/after this
   *  timestamp suppresses the load-complete auto-pause (mini preview open
   *  and loading rule). */
  const loadingSinceRef = useRef(0);
  const platform = explorePlatformKey(vod.platform);
  useEffect(() => {
    playbackKindRef.current = playback?.kind ?? 'progressive';
  }, [playback?.kind]);

  // Height explosion guard: the row's height is content-driven and
  // PreviewChatPanel's `self-stretch` column grows to the unbounded
  // virtualized chat list (~112k px for a long VOD → whole popup becomes an
  // invisible overlay). Pin the row to the player column's content height.
  // `items-start` on the row keeps the video column at its natural content
  // height (never squeezed), while the chat panel's `self-stretch` re-engages
  // and matches the pinned row exactly. Direct style writes (not state) so the
  // position layout effect below reads the pinned height in the same commit.
  useLayoutEffect(() => {
    const row = playerRowRef.current;
    const col = playerColRef.current;
    if (!row || !col) return;
    if (fullscreen) {
      row.style.height = '';
      return;
    }
    const h = col.offsetHeight;
    if (h > 0) {
      playerColHRef.current = h;
      row.style.height = `${h}px`;
    }
  }, [fullscreen]);

  // Keep the pin exact as the column's content height changes (video aspect
  // resolves, chat opens/narrows the video, archive-search dock toggles,
  // viewport resize). Skipped while fullscreen so exiting fullscreen never
  // flashes a viewport-tall row.
  useEffect(() => {
    const col = playerColRef.current;
    if (!col) return;
    const ro = new ResizeObserver(() => {
      if (fullscreenRef.current) return;
      const row = playerRowRef.current;
      const h = col.offsetHeight;
      if (row && h > 0 && h !== playerColHRef.current) {
        playerColHRef.current = h;
        row.style.height = `${h}px`;
      }
    });
    ro.observe(col);
    return () => ro.disconnect();
  }, []);

  // vaft midroll rotation — inert unless the player is a Twitch live session
  // (only Twitch live playlists contain 'stitched' segments). The backend
  // swaps the session's usher master to the next player type in place and the
  // same proxied URL serves the rotated stream; failure keeps stripping.
  const onAdRotation = useMemo(
    () => createTwitchAdRotationHandler({
      getSessionId: () => sessionIdRef.current,
      getHls: () => hlsRef.current,
      getVideo: () => videoRef.current,
      requestRotation: (sid) =>
        apiPost<{ ok?: boolean; master_url?: string }>(`/api/preview/live/rotate/${sid}`, {}),
    }),
    [],
  );

  const sessionHandoffRefs = {
    trimTimelineRef,
    windowHlsMuxStartRef,
    windowHlsMuxEndRef,
    extractSourceRef,
    pendingSeekSecRef,
    cachedProgressiveRef,
    sessionMetaRef,
  };

  const applySessionRefresh = useCallback((res: PreviewSessionResponse) => (
    previewSessionRefreshHandoff(
      vod.url,
      res,
      sessionHandoffRefs,
      setPlayback,
      () => videoRef.current?.currentTime ?? 0,
    )
  ), [vod.url]);

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
    isYoutubePreview: platform === 'youtube',
    containerRef,
    trimTimelineRef: trimTimelineRef,
    onPreviewError: (msg) => {
      if (msg) {
        setError(msg);
        previewStartTimeoutRef.current?.settle();
        markPreviewError('playback');
      }
    },
  });

  // Instant preview: play the local 6s clip while the mini session boots.
  // The mini preview always opens at offset 0, so the clip window always applies.
  const instantPreview = useInstantPreview({
    url: vod.url,
    active: true,
    remoteReady: ready,
    startSec: 0,
  });


  const postYoutubeCommand = useCallback((func: string, args: unknown[] = []) => {
    youtubeIframeCommand(youtubeIframeRef.current, func, args);
  }, []);

  useEffect(() => {
    const pause = () => {
      videoRef.current?.pause();
      setPlaying(false);
    };
    onRegisterPause(id, pause);
    return () => onUnregisterPause(id);
  }, [id, onRegisterPause, onUnregisterPause]);

  // Click-outside: close the quality menu on ANY mousedown outside
  // its own [data-quality-menu] wrapper.
  useEffect(() => {
    if (!qualityMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest('[data-quality-menu]')) return;
      setQualityMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [qualityMenuOpen]);

  useEffect(() => {
    let cancelled = false;
    initialPlayDoneRef.current = false;
    setPlayback(null);
    setYoutubeEmbed(null);
    setLoading(true);
    setBuffering(false);
    setReady(false);
    setError(null);
    // Auto-pause guard: any user unpause at/after this instant suppresses
    // the load-complete pause when the mini preview finishes loading.
    loadingSinceRef.current = Date.now();
    // Synthetic watchdog ids (chat capture without an extracted videoId) have
    // no video — show the honest state instead of booting a 404 preview
    // session (watch?v=<synthetic> does not exist on YouTube).
    const syntheticVidMatch = /[?&]v=([^&/?#]+)/.exec(vod.url);
    if (platform === 'youtube' && syntheticVidMatch
        && isSyntheticArchiveId(decodeURIComponent(syntheticVidMatch[1]))) {
      setLoading(false);
      setError('Live chat capture — no video');
      return;
    }
    // Archive search seek-to-moment: position the fresh session at the hit
    // offset. HLS/progressive consume this via pendingSeekSecRef when the
    // media attaches; window-HLS (YouTube trim timeline) skips that and the
    // ready-fallback below re-seeks through the full remux path.
    initialSeekRef.current = vod.initialTimeSec != null && vod.initialTimeSec > 0
      ? { target: vod.initialTimeSec, consumed: false }
      : null;
    pendingSeekSecRef.current = vod.initialTimeSec ?? null;
    // A manual open (or new media) starts a fresh retry budget; a
    // RETRY-triggered run keeps it so a repeated failure escalates.
    if (!previewRetryingRef.current) {
      setPreviewRetryBoth(null);
    }
    setMediaDurationSec(0);
    setSessionDurationSec(0);
    setWindowHlsMuxEndSec(0);
    if (vod.durationSec > 0) {
      setSessionDurationSec(Math.floor(vod.durationSec));
      setMediaDurationSec(vod.durationSec);
    }
    trimTimelineRef.current = false;
    setPreviewLevels([]);
    setQualityLevel(0);
    setQualityMenuOpen(false);
    requestedHeightRef.current = 0;
    appliedHeightRef.current = 0;
    const timing = new PreviewTiming(platform ?? 'unknown', 'explore');
    previewTimingRef.current = timing;
    timing.markOpen(vod.url.slice(0, 80));

    // Hard bound on the 'Starting…' phase: from here until canplay (or a
    // terminal error). A hung session-create or playback that never starts
    // would otherwise spin the loading overlay forever — surface the RETRY
    // UI instead. Mirrors the main-preview guard in App.tsx.
    const armedGen = sessionRetryTick;
    const guard = new PreviewStartTimeout(vod.url, {
      onTimeout: (_url, stage) => {
        if (armedGen !== sessionRetryTick) return false;
        setError(t('Preview took too long — try again'));
        markPreviewError(stage);
        hlsRef.current?.destroy();
        hlsRef.current = null;
        setLoading(false);
        setBuffering(false);
        return true;
      },
    });
    previewStartTimeoutRef.current = guard;
    guard.start();

    // Never embed youtube.com: controls=0 cannot suppress every native overlay.
    // Use the same proxied media pipeline as every other platform so only the
    // application's controls receive pointer/keyboard input.
    (async () => {
      try {
        const playerCap = measurePlayerHeightCap(videoWrapRef.current, videoAspectRef.current);
        const preferHeight = initialPreviewPreferHeight(vod.isClip, playerCap, {
          youtube: platform === 'youtube',
        });
        const clipInfoPromise = vod.isClip
          ? apiGet<{ qualities?: string[] }>(
            `/api/info/clip?id=${encodeURIComponent(vod.url)}`,
          ).catch(() => null)
          : Promise.resolve(null);
        // ponytail: start the session create immediately with crop_end=0
        // (backend falls back to extract duration) — don't block the click on
        // /api/info/video which can take 30-60s on a cold YouTube URL. Fire
        // the info fetch in parallel; if it returns a real duration, send a
        // seek to clamp the trim window.
        const knownDuration = vod.durationSec;
        const sessionPromise = createPreviewSessionWithRetry({
          url: vod.url,
          crop_start: 0,
          crop_end: knownDuration > 0 ? knownDuration : 0,
          prefer_height: preferHeight,
        }, guard.signal ?? undefined);
        const [clipInfo, res] = await Promise.all([clipInfoPromise, sessionPromise]);
        if (cancelled) {
          // ponytail: StrictMode runs this effect twice. The in-flight dedup
          // in createPreviewSessionWithRetry ensures both runs share the SAME
          // underlying POST and receive the same session_id. The guard below
          // still correctly identifies orphans: the ref is the session_id
          // that the effect applied, and if this run was cancelled its session
          // is the orphan. Remove the guard only if the dedup contract changes.
          const orphanSid = res.session_id;
          setTimeout(() => {
            if (sessionIdRef.current !== orphanSid) {
              void apiDelete(`/api/preview/session/${orphanSid}`).catch(() => {});
            }
          }, 5000);
          return;
        }
        // ponytail: res.duration_sec comes from the extract — prefer it over
        // the channel-list hint (which is 0 for YouTube RSS rows).
        guard.markCreateResolved();
        if (res.duration_sec && res.duration_sec > 0) {
          setSessionDurationSec(Math.floor(res.duration_sec));
        }
        trimTimelineRef.current = res.trim_timeline === true;
        windowHlsMuxStartRef.current = res.window_hls_mux_start ?? 0;
        windowHlsMuxEndRef.current = res.window_hls_mux_end ?? 0;
        cachedProgressiveRef.current = res.cached_progressive === true;
        setWindowHlsMuxEndSec(res.window_hls_mux_end ?? 0);
        const resolved = resolvePreviewPlayback(vod.url, res);
        const mergedQualityLabels = clipInfo?.qualities?.length
          ? clipInfo.qualities
          : (res.quality_labels?.length ? res.quality_labels : undefined);
        const activeHeight = res.active_height ?? preferHeight;
        sessionMetaRef.current = {
          variantHeights: res.variant_heights ?? [],
          qualityLabels: mergedQualityLabels,
          activeHeight,
          anonymous: res.anonymous === true,
          isLive: res.is_live === true,
        };
        sessionIdRef.current = res.session_id;
        timing.setSessionId(res.session_id);
        timing.mark('session_ready', `kind=${res.kind} trim=${res.trim_timeline === true}`);
        extractSourceRef.current = res.extract_source ?? '';
        if (extractSourceRef.current) {
          console.info('[VOD.RIP preview] extract_source=', extractSourceRef.current);
        }
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
          guard.settle();
          markPreviewError('session');
        }
      }
    })();

    return () => {
      cancelled = true;
      // Invalidate any in-flight seek so its async callbacks become no-ops.
      seekInflightRef.current += 1;
      seekTargetRef.current = null;
      seekLockedRef.current = false;
      pendingSeekSecRef.current = null;
      bufferingClearRef.current = null;
      if (seekDebounceRef.current != null) {
        window.clearTimeout(seekDebounceRef.current);
        seekDebounceRef.current = null;
      }
      const hls = hlsRef.current;
      if (hls) {
        try {
          hls.stopLoad();
          hls.detachMedia();
          hls.destroy();
        } catch {
          /* ignore */
        }
        hlsRef.current = null;
      }
      const video = videoRef.current;
      if (video) {
        detachProgressivePreview(video);
      }
      if (document.fullscreenElement === containerRef.current) {
        void document.exitFullscreen().catch(() => {});
      }
      setYoutubeEmbed(null);
      const sid = sessionIdRef.current;
      sessionIdRef.current = null;
      sessionMetaRef.current = null;
      if (sid) {
        void apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      }
      previewStartTimeoutRef.current?.settle();
    };
  }, [vod.url, vod.durationSec, sessionRetryTick]);

  /**
   * RETRY button for THIS popup's media only. First click re-runs just the
   * failed stage; after a failed retry the next click runs the full pipeline
   * end-to-end for this media (drop stale session + force fresh backend
   * extract — never touches other popups or channel rows).
   */
  const retryPreview = useCallback(() => {
    const ctx = previewRetryRef.current;
    if (!ctx) return;
    setError(null);
    previewRetryingRef.current = true;
    const mode = previewRetryMode(ctx);
    void (async () => {
      try {
        if (mode === 'full') {
          const sid = sessionIdRef.current;
          if (sid) {
            try { await apiDelete(`/api/preview/session/${sid}`); } catch { /* ignore */ }
          }
          // Clear the backend's per-URL caches (fatal/negative extract caches
          // live 30-300s) so the fresh POST re-extracts instead of re-raising.
          try { await apiPost('/api/preview/invalidate', { url: ctx.url }); } catch { /* ignore */ }
        }
        if (mode === 'stage' && ctx.stage === 'playback') {
          // Stage retry: re-attach playback to the SAME session — the attach
          // effect reports success (canplay clears the context) or failure
          // (markPreviewError escalates attempts). No new session.
          setAttachRetryTick((t) => t + 1);
          return;
        }
        // Session-stage retry and full retries both re-run create + attach.
        setSessionRetryTick((t) => t + 1);
      } catch { /* no-op */ }
    })();
  }, []);










  const togglePlay = useCallback(() => {
    if (youtubeEmbed) {
      if (!ready) return;
      if (playing) {
        postYoutubeCommand('pauseVideo');
        setPlaying(false);
      } else {
        postYoutubeCommand('setVolume', [Math.round(volumeRef.current * 100)]);
        postYoutubeCommand(muted ? 'mute' : 'unMute');
        postYoutubeCommand('playVideo');
        setPlaying(true);
        noteUserUnpause();
      }
      return;
    }
    const video = videoRef.current;
    if (!video || !ready) return;
    if (video.paused) {
      unlockPreviewAudioFromGesture(video, setMuted, volumeRef.current);
      noteUserUnpause();
      void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
        setPlaying(!video.paused);
      });
    } else {
      video.pause();
      setPlaying(false);
    }
  }, [ready, playing, youtubeEmbed, postYoutubeCommand]);

  const setVolumeLevel = useCallback((level: number) => {
    const v = Math.max(0, Math.min(1, level));
    if (youtubeEmbed) {
      postYoutubeCommand('setVolume', [Math.round(v * 100)]);
      if (v > 0) volumeRef.current = v;
      setVolume(v);
      postYoutubeCommand(v <= 0 ? 'mute' : 'unMute');
      setMuted(v <= 0);
      return;
    }
    const video = videoRef.current;
    if (!video) return;
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
  }, [youtubeEmbed, postYoutubeCommand]);

  useEffect(() => {
    if (!youtubeEmbed) return;
    const onMessage = (event: MessageEvent) => {
      // YouTube's IFrame API delivers `infoDelivery` events with `currentTime`
      // via postMessage. Match by `event.source` against the iframe's
      // contentWindow — origin alone is unreliable for YT embeds.
      const iframe = youtubeIframeRef.current;
      if (!iframe || event.source !== iframe.contentWindow) {
        if (event.origin !== 'https://www.youtube.com') return;
      }
      let data: any;
      try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; } catch { return; }
      if (!data || data.event !== 'infoDelivery') return;
      const state = Number(data?.info?.playerState);
      if (state === 1) setPlaying(true);
      else if (state === 2 || state === 0) setPlaying(false);
      const t = Number(data?.info?.currentTime);
      if (Number.isFinite(t)) {
        const target = seekTargetRef.current;
        if (target != null) {
          if (Math.abs(t - target) > 1.5) return;
          seekTargetRef.current = null;
        }
        setCurrentTime(t);
      }
    };
    youtubeIframeListen(youtubeIframeRef.current);
    const poll = window.setInterval(() => {
      youtubeIframeListen(youtubeIframeRef.current);
      postYoutubeCommand('getCurrentTime');
    }, 250);
    window.addEventListener('message', onMessage);
    return () => {
      window.clearInterval(poll);
      window.removeEventListener('message', onMessage);
    };
  }, [youtubeEmbed, postYoutubeCommand]);

  const effectiveDurationSec = resolvePreviewDurationSec(
    mediaDurationSec,
    sessionDurationSec > 0 ? sessionDurationSec : vod.durationSec,
    isYoutubeWindowHlsPreview(
      platform === 'youtube',
      playback?.kind ?? 'progressive',
      windowHlsMuxEndSec,
    ),
  );

  const windowHlsTimeline = trimTimelineRef.current
    || isYoutubeWindowHlsPreview(
      platform === 'youtube',
      playback?.kind ?? 'progressive',
      windowHlsMuxEndSec,
    );

  const seekVideo = useCallback((sec: number) => {
    const t = Math.max(0, Math.min(sec, effectiveDurationSec));
    if (youtubeEmbed) {
      if (!ready) return;
      // Ignore delayed pre-seek infoDelivery events until the iframe reaches
      // this position; otherwise the controlled timeline visibly rewinds.
      seekTargetRef.current = t;
      setCurrentTime(t);
      previewTimingRef.current?.markSeekStart(t);
      postYoutubeCommand('seekTo', [t, true]);
      return;
    }
    const video = videoRef.current;
    if (!video || !ready) return;
    pinHlsToLowestLevel(hlsRef.current);
    const windowHlsSeek = trimTimelineRef.current
      || isYoutubeWindowHlsPreview(
        platform === 'youtube',
        playbackKindRef.current,
        windowHlsMuxEndRef.current,
      );
    seekTargetRef.current = t;
    const optimistic = previewSeekOptimisticUi(
      platform === 'youtube',
      trimTimelineRef.current,
      playbackKindRef.current,
    );
    if (optimistic) setCurrentTime(t);
    previewTimingRef.current?.markSeekStart(t);

    const applyLocal = (videoTime: number) => {
      if (Math.abs(video.currentTime - videoTime) > 0.2) {
        video.currentTime = videoTime;
      }
    };

    const finishSeek = () => {
      seekTargetRef.current = null;
      setCurrentTime(t);
    };

    const sid = sessionIdRef.current;
    if (windowHlsSeek && sid && platform === 'youtube') {
      // Invalidate any previous seek before starting the next one so callbacks
      // for the old one become no-ops and cannot leak the timeline lock.
      const seekId = ++seekInflightRef.current;
      const clearLockIfCurrent = () => {
        if (seekId === seekInflightRef.current) {
          seekLockedRef.current = false;
          setBuffering(false);
        }
      };
      const muxStart = windowHlsMuxStartRef.current;
      const muxEnd = windowHlsMuxEndRef.current;
      const resumePlay = !video.paused;
      if (isPositionInWindowHlsMux(t, muxStart, muxEnd)) {
        seekLockedRef.current = true;
        // The slider already jumped optimistically in seekVideoDebounced.
        // applyVideoLocalSeek pauses during the seek so the decoder does not
        // play forward from the previous keyframe to the target.
        void applyVideoLocalSeek(video, windowHlsVideoTimeSec(t, muxStart))
          .then(() => {
            if (seekId !== seekInflightRef.current) return;
            seekLockedRef.current = false;
            finishSeek();
            bufferingClearRef.current?.();
            setBuffering(false);
            if (resumePlay) void video.play().then(() => setPlaying(true)).catch(() => {});
          })
          .catch(() => {
            if (seekId !== seekInflightRef.current) return;
            seekTargetRef.current = null;
            clearLockIfCurrent();
          });
        return;
      }
      // Out-of-window seek: keep the slider at the target (already set
      // optimistically) and wait for the backend remux. Do not touch
      // video.currentTime until the new chunk is ready — the old window does
      // not contain the target, so any local seek would snap to the wrong frame.
      seekLockedRef.current = true;
      video.pause();
      setPlaying(false);
      shieldPreviewBuffering(120_000);
      // Show loading immediately so the user knows the requested frame is being
      // prepared while the backend remuxes.
      setBuffering(true);
      let slowSpinner: number | undefined;
      void (async () => {
        try {
          slowSpinner = window.setTimeout(() => setBuffering(true), 800);
          const { muxStart: newStart, muxEnd: newEnd, remuxed } = await seekYoutubeWindowHls(sid, t, apiPost, apiGet, 12_000);
          if (seekId !== seekInflightRef.current) return;
          windowHlsMuxStartRef.current = newStart;
          windowHlsMuxEndRef.current = newEnd;
          setWindowHlsMuxEndSec(newEnd);
          const videoTime = windowHlsVideoTimeSec(t, newStart);
          if (remuxed && hlsRef.current) {
            await reloadWindowHlsAtPosition(
              hlsRef.current,
              sid,
              video,
              videoTime,
            );
          } else {
            await applyVideoLocalSeek(video, videoTime);
          }
          if (seekId !== seekInflightRef.current) return;
          seekLockedRef.current = false;
          finishSeek();
          bufferingClearRef.current?.();
          waitVideoPlayable(video, previewTimingRef.current ?? new PreviewTiming(platform, 'explore'));
          if (resumePlay) void video.play().then(() => setPlaying(true)).catch(() => {});
        } catch (err: unknown) {
          if (seekId === seekInflightRef.current) {
            setError(err instanceof Error ? err.message : 'Seek failed');
            seekTargetRef.current = null;
          }
        } finally {
          if (slowSpinner !== undefined) window.clearTimeout(slowSpinner);
          clearLockIfCurrent();
        }
      })();
      return;
    }

    if (
      platform === 'youtube'
      && !trimTimelineRef.current
      && playbackKindRef.current === 'progressive'
      && !cachedProgressiveRef.current
      && sid
      && t > 60
    ) {
      // Show a teaser frame at the target immediately while /refresh resolves
      // the full-window progressive URL in the background.
      applyLocal(t);
      pendingSeekSecRef.current = t;
      setBuffering(true);
      void apiPost<PreviewSessionResponse>(`/api/preview/session/${sid}/refresh`, {})
        .then((res) => {
          // The element already played ~0.5s past t while the refresh was in
          // flight. Resume the handoff from the LIVE position — re-seeking to
          // the original t would visibly replay the same half second.
          pendingSeekSecRef.current = video.currentTime;
          if (applySessionRefresh(res)) {
            finishSeek();
            setBuffering(false);
            return;
          }
          // No handoff: same progressive stream, element is already at/past
          // the target — do NOT re-seek (that was the seek-repeat glitch).
          waitVideoPlayable(
            video,
            previewTimingRef.current ?? new PreviewTiming(platform ?? 'unknown', 'explore'),
          );
          finishSeek();
        })
        .catch(() => {
          waitVideoPlayable(
            video,
            previewTimingRef.current ?? new PreviewTiming(platform ?? 'unknown', 'explore'),
          );
          finishSeek();
        })
        .finally(() => setBuffering(false));
      return;
    }

    applyLocal(t);
    waitVideoPlayable(
      video,
      previewTimingRef.current ?? new PreviewTiming(platform ?? 'unknown', 'explore'),
    );
    finishSeek();
  }, [ready, effectiveDurationSec, platform, applySessionRefresh, youtubeEmbed, postYoutubeCommand]);

  const seekVideoDebounced = useCallback((sec: number) => {
    const clamped = Math.max(0, Math.min(sec, effectiveDurationSec));
    seekTargetRef.current = clamped;
    if (previewSeekOptimisticUi(
      platform === 'youtube',
      trimTimelineRef.current,
      playbackKindRef.current,
    )) {
      setCurrentTime(clamped);
    }
    if (seekDebounceRef.current != null) {
      window.clearTimeout(seekDebounceRef.current);
    }
    seekDebounceRef.current = window.setTimeout(() => {
      seekDebounceRef.current = null;
      seekVideo(sec);
    }, PREVIEW_SEEK_DEBOUNCE_MS);
  }, [effectiveDurationSec, seekVideo]);

  const skip = useCallback((deltaSec: number) => {
    if (!ready) return;
    const video = videoRef.current;
    if (!video) return;
    const windowHlsSeek = trimTimelineRef.current
      || isYoutubeWindowHlsPreview(
        platform === 'youtube',
        playbackKindRef.current,
        windowHlsMuxEndRef.current,
      );
    const base = windowHlsSeek
      ? windowHlsMuxStartRef.current + video.currentTime
      : video.currentTime;
    seekVideo(base + deltaSec);
  }, [ready, seekVideo]);

  const focusPlayer = useCallback(() => {
    containerRef.current?.focus();
  }, []);

  // Spawned windows take focus (shared raise-to-front contract) — the popup
  // is the active surface the moment it opens.
  useEffect(() => {
    containerRef.current?.focus({ preventScroll: true });
  }, []);

  const onPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    if (frameSnapRect) return;
    startExplorePanelWidthResize(e, edge, panelWidthRef, setPanelWidth, {
      panelEl: containerRef.current,
      aspect: videoAspectRef.current,
      posRef,
      setPos,
      // The drag resizes the video; the container is video + chat footprint.
      // Floor at the panel minimum (60 was below EXPLORE_PANEL_MIN_W and let
      // the player column collapse to a few px — buttons wrapped into the
      // chat panel).
      clampWidth: (w) =>
        Math.max(EXPLORE_PANEL_MIN_W, clampExplorePanelWidth(w + chatTotal, chromeHRef.current, videoAspectRef.current) - chatTotal),
    });
  }, [chatTotal, frameSnapRect]);

  const onPopupDrag = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (fullscreen) return;
    // Dragging the body of a popup snapped into a frame cell releases it from
    // the cell so the same gesture can move it freely (a snapped popup would
    // otherwise be re-pinned to the cell by the layout effect).
    if (frameSnapRect) onUnsnap?.();
    const t = e.target as HTMLElement;
    if (t.closest('button, input, select, textarea, a, [role="slider"], [data-player-menu], [data-preview-chat-panel]')) return;
    const el = containerRef.current;
    if (!el) return;
    if (!posRef.current) {
      posRef.current = layoutExplorePopupWindow(el, panelWidthRef.current + chatTotal, posRef, stackIndex);
      setPos(posRef.current);
    }
    // The natural gesture to snap into a frame cell is grabbing the popup body/video.
    // That is a pointer drag (below), which never fires HTML5 dragstart — so in frame
    // mode we arm the snap grid explicitly. FrameOverlay computes the hovered cell by
    // geometry and snaps on pointerup.
    if (frameMode) {
      document.dispatchEvent(new CustomEvent('explore-frame-arm', { detail: { id } }));
    }
    startFloatingPanelDrag(e, posRef, setPos, el);
  }, [fullscreen, stackIndex, frameSnapRect, onUnsnap, frameMode, id]);

  const fsGateRef = useRef<FullscreenGate | null>(null);
  if (fsGateRef.current === null) {
    fsGateRef.current = createFullscreenGate();
  }

  const toggleFullscreen = useCallback(() => {
    const container = containerRef.current;
    if (!container || !ready) return;
    // Gate: single in-flight transition, direction from the current fullscreen element.
    const dir = fsGateRef.current?.toggle(container);
    if (dir === 'enter') {
      // Optimistic state: unmount the chat column and apply the fullscreen
      // layout BEFORE the browser transition, so the chat can never be
      // captured inside the fullscreening container during the
      // fullscreenchange state-lag window (the 'chat swaps the whole screen'
      // flash). A recovery effect below reverts if the request is denied.
      setFullscreen(true);
      setFsControlsVisible(true);
      applyExplorePopupFullscreenPosition(container);
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
      fsGateRef.current?.sync();
      const fs = document.fullscreenElement === containerRef.current;
      setFullscreen(fs);
      // Show the transport on enter and on exit; on enter, arm the auto-hide
      // so idle fullscreen still clears the overlay (mirrors App.tsx main
      // preview — the old `!fs` hid controls the instant fullscreen began,
      // leaving a fullscreen player with no exit, which felt like the app
      // "locked you out of doing anything").
      setFsControlsVisible(true);
      if (fsHideTimerRef.current) {
        window.clearTimeout(fsHideTimerRef.current);
        fsHideTimerRef.current = null;
      }
      if (fs) {
        fsHideTimerRef.current = window.setTimeout(() => {
          setFsControlsVisible(false);
        }, PREVIEW_FS_CONTROLS_HIDE_MS);
      }
      const el = containerRef.current;
      if (!el) return;
      if (fs) {
        applyExplorePopupFullscreenPosition(el);
      } else if (posRef.current) {
        applyExplorePopupWindowPosition(el, posRef.current);
      } else {
        const p = layoutExplorePopupWindow(el, panelWidthRef.current + chatTotal, posRef, stackIndex);
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
  }, [ready, fullscreen, panelWidth, videoAspect, chatTotal, syncPlaybackToViewport]);

  // Recovery for the optimistic fullscreen enter: if the browser denied the
  // request (no fullscreenchange fires), revert to the windowed layout
  // instead of leaving the popup stuck as a fullscreen overlay.
  useEffect(() => {
    if (!fullscreen) return;
    const t = window.setTimeout(() => {
      if (document.fullscreenElement !== containerRef.current) {
        setFullscreen(false);
        setFsControlsVisible(true);
      }
    }, FULLSCREEN_SETTLE_FALLBACK_MS + 100);
    return () => window.clearTimeout(t);
  }, [fullscreen]);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    if (fullscreen) {
      applyExplorePopupFullscreenPosition(el);
      return;
    }
    if (frameSnapRect) {
      const innerPad = 6;
      const maxInnerW = Math.max(0, frameSnapRect.w - innerPad * 2);
      const maxInnerH = Math.max(0, frameSnapRect.h - innerPad * 2);
      const chatW = chatTotal;
      let videoW = Math.max(
        EXPLORE_PANEL_MIN_W,
        Math.min(maxInnerW - chatW, maxInnerW * 0.92 - chatW),
      );
      let videoH = videoW / videoAspectRef.current;
      const chrome = chromeHRef.current;
      let totalH = chrome + videoH;
      if (totalH > maxInnerH) {
        videoH = Math.max(72, maxInnerH - chrome);
        videoW = Math.min(maxInnerW - chatW, videoH * videoAspectRef.current);
        totalH = chrome + videoW / videoAspectRef.current;
      }
      panelWidthRef.current = videoW;
      setPanelWidth(videoW);
      const totalW = videoW + chatW;
      const px = frameSnapRect.x + innerPad + Math.max(0, (maxInnerW - totalW) / 2);
      const py = frameSnapRect.y + innerPad + Math.max(0, (maxInnerH - totalH) / 2);
      const snapped: PanelPos = { x: px, y: py };
      posRef.current = snapped;
      setPos(snapped);
      applyExplorePopupWindowPosition(el, snapped);
      el.style.width = `${totalW}px`;
      el.style.maxWidth = `${maxInnerW}px`;
      el.style.maxHeight = `${maxInnerH}px`;
      return;
    }
    const p = layoutExplorePopupWindow(el, containerW, posRef, stackIndex);
    setPos((prev) => (prev?.x === p.x && prev?.y === p.y ? prev : p));
  }, [fullscreen, containerW, videoAspect, stackIndex, frameSnapRect, chatTotal, chromeHVersion]);

  useEffect(() => {
    if (fullscreen || frameSnapRect) return;
    const fit = () => {
      // Clamp the total (video + chat) to the viewport budget; the video
      // keeps the remainder (60px floor while the chat is open).
      const totalW = clampExplorePanelWidth(
        panelWidthRef.current + chatTotalRef.current,
        chromeHRef.current,
        videoAspectRef.current,
      );
      const clampedW = Math.max(EXPLORE_PANEL_MIN_W, totalW - chatTotalRef.current);
      panelWidthRef.current = clampedW;
      setPanelWidth(clampedW);
      const el = containerRef.current;
      if (!el) return;
      const p = layoutExplorePopupWindow(el, totalW, posRef, stackIndex);
      setPos(p);
    };
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, [fullscreen, stackIndex, frameSnapRect]);

  useEffect(() => {
    if (fullscreen || !containerRef.current || !videoWrapRef.current) return;
    const chromeH = containerRef.current.offsetHeight - videoWrapRef.current.offsetHeight;
    if (chromeH > 0 && Math.abs(chromeH - chromeHRef.current) >= 1) {
      chromeHRef.current = chromeH;
      setChromeHVersion((v) => v + 1);
    }
  }, [fullscreen, panelWidth, videoAspect, ready, error]);

  useEffect(() => {
    if (!playback?.url) return;
    let cancelled = false;
    let cleanup: (() => void) | undefined;
    let detachBuffering: (() => void) | undefined;

    const setup = () => {
      if (cancelled) return;
      const video = videoRef.current;
      if (!video) {
        requestAnimationFrame(setup);
        return;
      }
      const bufferingHandle = attachPreviewBufferingListeners(video, (stalling) => {
        if (!cancelled) setBuffering(stalling);
      });
      bufferingClearRef.current = bufferingHandle.clearStall;
      detachBuffering = bufferingHandle.detach;
      const { url: playbackUrl, kind: playbackKind } = playback;

    setLoading(true);
    setBuffering(false);
    setReady(false);

    const onCanPlay = () => {
      // Playback genuinely started — any in-flight retry succeeded.
      previewRetryingRef.current = false;
      setPreviewRetryBoth(null);
      setReady(true);
      setBuffering(false);
      setLoading(false);
      previewStartTimeoutRef.current?.markReady();
      previewTimingRef.current?.mark('canplay');
      video.volume = PREVIEW_DEFAULT_VOLUME;
      volumeRef.current = PREVIEW_DEFAULT_VOLUME;
      setVolume(PREVIEW_DEFAULT_VOLUME);
      if (!initialPlayDoneRef.current && video.paused) {
        initialPlayDoneRef.current = true;
        autoPauseOtherPreviews(loadingSinceRef.current);
        void playPreviewWithAudio(video, setMuted, PREVIEW_DEFAULT_VOLUME).then(() => {
          setPlaying(!video.paused);
          if (video.readyState >= 3 && !video.paused && video.currentTime > 0.02) {
            previewTimingRef.current?.markFirstPlayable('canplay_already_playing');
          }
        });
      }
    };

    const clearStallUi = () => {
      if (cancelled) return;
      setLoading(false);
      setBuffering(false);
    };
    video.addEventListener('playing', clearStallUi);
    video.addEventListener('playing', () => {
      previewTimingRef.current?.markFirstPlayable();
    }, { once: true });

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
        allowHeights: vod.platform === 'youtube'
          ? youtubePreviewAllowHeights({
            isLive: meta?.isLive ?? false,
            anonymous: meta?.anonymous ?? false,
          })
          : undefined,
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
        setError('Preview interrupted — try again');
        setLoading(false);
        previewStartTimeoutRef.current?.settle();
        markPreviewError('playback');
      };
      appliedHeightRef.current = activeH;
      const onLoadedMeta = () => {
        if (Number.isFinite(video.duration) && video.duration > 0) {
          setMediaDurationSec(Math.round(video.duration));
          clipRelativeRef.current = isClipRelativePreviewDuration(
            video.duration,
            vod.durationSec,
            vod.durationSec,
          );
        }
        // Archive seek-to-moment: progressive streams have no startPosition,
        // so land the initial offset on first metadata instead.
        const pending = pendingSeekSecRef.current;
        if (pending != null && pending > 0) {
          pendingSeekSecRef.current = null;
          seekTargetRef.current = pending;
          setCurrentTime(pending);
          video.currentTime = pending;
          const init = initialSeekRef.current;
          if (init) init.consumed = true;
        }
      };
      attachProgressivePreview(video, playbackUrl);
      const cleanupRecovery = bindProgressivePreviewRecovery({
        video,
        playbackUrl,
        getSessionId: () => sessionIdRef.current,
        youtube: platform === 'youtube',
        extractSource: extractSourceRef.current,
        getResumeSec: () => seekTargetRef.current ?? video.currentTime,
        apiPost,
        onRefreshing: () => setBuffering(true),
        onFatal: onVideoError,
        onSessionRefresh: (res) => {
          pendingSeekSecRef.current = seekTargetRef.current ?? video.currentTime;
          const ok = applySessionRefresh(res as PreviewSessionResponse);
          if (ok) setBuffering(false);
          return ok;
        },
      });
      video.addEventListener('loadedmetadata', onLoadedMeta, { once: true });
      video.addEventListener('canplay', onCanPlay, { once: true });
      cleanup = () => {
        video.removeEventListener('loadedmetadata', onLoadedMeta);
        video.removeEventListener('canplay', onCanPlay);
        video.removeEventListener('playing', clearStallUi);
        cleanupRecovery();
        detachProgressivePreview(video);
      };
      return;
    }

    if (Hls.isSupported()) {
      const dashSegTimeline = trimTimelineRef.current;
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 12,
        // Play-first: start playback once ~3 s are buffered instead of waiting
        // for 20 s (6 s delayed first frame on slow Twitch CDN fetches).
        // Window-HLS keeps a larger buffer because the chunk is muxed.
        maxBufferLength: dashSegTimeline ? 60 : 3,
        maxMaxBufferLength: dashSegTimeline ? 180 : 6,
        startFragPrefetch: true,
        capLevelToPlayerSize: platform !== 'youtube',
        fragLoadingTimeOut: dashSegTimeline ? 90000 : 20000,
        manifestLoadingTimeOut: 10000,
        testBandwidth: false,
        ...twitchAdBlockHlsConfig({ onAdRotation }),
        startPosition: trimTimelineRef.current ? 0 : (pendingSeekSecRef.current ?? 0),
      });
      hlsRef.current = hls;
      setHlsRef(hls);
      hls.attachMedia(video);
      const loadPlayback = () => {
        if (cancelled) return;
        hls.loadSource(playbackUrl);
      };
      requestAnimationFrame(() => requestAnimationFrame(loadPlayback));
      let levelsInitialized = false;
      let maxMenuHeight = 0;
      const playerCap = measurePlayerHeightCap(videoWrapRef.current, videoAspectRef.current);
      const meta = sessionMetaRef.current;
      const fallbackHeights = mergeVariantHeights(playback.variantHeights);
      const initialHlsHeight = resolveInitialHlsPreviewHeight(vod.isClip, playerCap, {
        youtube: platform === 'youtube',
        variantHeights: fallbackHeights,
        activeHeight: meta?.activeHeight ?? playback.activeHeight,
      });
      // Quality policy: YouTube VOD/anonymous previews cap the menu at 360p;
      // live sessions with user cookies may raise to 1080p.
      const allowHeights = platform === 'youtube'
        ? youtubePreviewAllowHeights({
          isLive: meta?.isLive ?? false,
          anonymous: meta?.anonymous ?? false,
        })
        : undefined;
      const syncPreviewLevels = (levels = hls.levels, applyDefault = false) => {
        const { mapped, defaultIndex } = resolveHlsPreviewLevels(levels, {
          initialHeight: initialHlsHeight,
          fallbackHeights,
          allowHeights,
        });
        if (!mapped.length) return;
        const maxH = Math.max(0, ...mapped.map((m) => m.height));
        const grew = maxH > maxMenuHeight;
        if (grew) maxMenuHeight = maxH;
        if (!levelsInitialized || applyDefault || grew) {
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
        const pending = pendingSeekSecRef.current;
        if (pending != null && pending > 0 && !trimTimelineRef.current) {
          pendingSeekSecRef.current = null;
          seekTargetRef.current = null;
          setCurrentTime(pending);
          hls.startLoad(pending);
          const init = initialSeekRef.current;
          if (init) init.consumed = true;
        }
        if (!trimTimelineRef.current && Number.isFinite(video.duration) && video.duration > 0) {
          setMediaDurationSec(Math.round(video.duration));
        }
      });
      hls.on(Hls.Events.LEVELS_UPDATED, () => {
        syncPreviewLevels(hls.levels);
      });
      video.addEventListener('canplay', onCanPlay, { once: true });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            // startLoad() alone is a no-op after a fatal manifest error (levels
            // empty) — re-loadSource forces a fresh manifest fetch.
            hls.loadSource(playbackUrl);
            hls.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError();
            break;
          default:
            setError('Playback failed — try again');
            setLoading(false);
            markPreviewError('playback');
            hls.destroy();
            hlsRef.current = null;
            break;
        }
      });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeEventListener('playing', clearStallUi);
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
        previewStartTimeoutRef.current?.settle();
        markPreviewError('playback');
        return;
      }
      video.src = playbackUrl;
      video.addEventListener('canplay', onCanPlay, { once: true });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeEventListener('playing', clearStallUi);
        video.removeAttribute('src');
        video.load();
      };
      return;
    }

    setError('HLS playback is not supported in this browser');
    setLoading(false);
    previewStartTimeoutRef.current?.settle();
    markPreviewError('playback');
    };

    setup();
    return () => {
      cancelled = true;
      bufferingClearRef.current = null;
      detachBuffering?.();
      cleanup?.();
    };
  }, [playback, vod.isClip, attachRetryTick]);

  useEffect(() => {
    if (!ready) return;
    // Archive seek-to-moment fallback: window-HLS (YouTube trim timeline)
    // skips the pending-start fast path, so re-seek through the full
    // machinery once playback is genuinely ready (remuxes when the hit is
    // outside the current mux window). Guarded so the fast path is not
    // re-applied on top of itself.
    const init = initialSeekRef.current;
    if (init && !init.consumed) {
      init.consumed = true;
      seekVideo(init.target);
    }
    const t = window.setTimeout(() => focusPlayer(), 0);
    return () => window.clearTimeout(t);
  }, [ready, focusPlayer, seekVideo]);

  const ctrlBtn = (fs: boolean) => platformPreviewCtrlBtn(platform, fs);

  const fsCtrlBtn = platformPreviewCtrlBtn(platform, true);

  const timelineUi = (
    <>
    <div className="flex items-center gap-1.5 w-full shrink-0 flex-wrap">
      <span className={`text-[9px] font-mono w-10 shrink-0 ${fullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}>
        {formatHmsFull(currentTime)}
      </span>
      <input
        type="range"
        min={0}
        max={effectiveDurationSec}
        step={0.25}
        value={Math.min(currentTime, effectiveDurationSec)}
        disabled={!ready}
        onChange={(e) => seekVideoDebounced(parseFloat(e.target.value))}
        className="flex-1 accent-white disabled:opacity-40 h-1 min-w-0"
      />
      <span className={`text-[9px] font-mono w-10 shrink-0 text-right ${fullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}>
        {formatHmsFull(effectiveDurationSec)}
      </span>
    </div>
    </>
  );

  const volumeUi = (fs: boolean) => (
    <div
      className="relative"
      data-volume-menu
      onMouseEnter={() => setVolumeHovered(true)}
      onMouseLeave={() => setVolumeHovered(false)}
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          if (muted) {
            setVolumeLevel(volumeRef.current || PREVIEW_DEFAULT_VOLUME);
          } else {
            setVolumeLevel(0);
          }
        }}
        className={fs ? fsCtrlBtn : ctrlBtn(false)}
        title={muted ? t('Unmute') : t('Mute')}
      >
        {muted || volume <= 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
      </button>
      {volumeHovered && (
        <div
          className={`absolute left-full bottom-0 ml-1.5 z-30 flex items-center gap-2 px-2.5 py-2 shadow-lg ${
            fs ? 'border border-white/20 bg-black/85 backdrop-blur-sm' : 'border-2 border-zinc-600 bg-zinc-950'
          }`}
          onClick={(e) => e.stopPropagation()}
          onMouseEnter={() => setVolumeHovered(true)}
          onMouseLeave={() => setVolumeHovered(false)}
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
      onSelect={(idx: number) => {
        void applyQuality(idx);
        setQualityMenuOpen(false);
      }}
      disabled={!ready}
      buttonClassName={fs ? fsCtrlBtn : ctrlBtn(false)}
      iconSize={18}
      onMenuOpen={() => {}}
      popoverClassName={fs
        ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
        : 'border-2 border-zinc-600 bg-zinc-950'}
      popoverPlacement="up"
    />
  );

  const showClipNotice = useCallback((kind: 'error' | 'ok', text: string) => {
    if (clipNoticeTimerRef.current) window.clearTimeout(clipNoticeTimerRef.current);
    setClipNotice({ kind, text });
    clipNoticeTimerRef.current = window.setTimeout(() => setClipNotice(null), 4000);
  }, []);

  /**
   * Open the Twitch clip mini-preview at the current playhead (120s window,
   * user trims there and creates the clip). Fixes the old direct-open call,
   * which sent offsetSec without durationSec — the backend 422'd every VOD.
   */
  const openExploreTwitchClip = useCallback(() => {
    const login = (vod.channel || '').trim();
    const vodId = vod.videoId ?? archiveVideoIdFromUrl(vod.url) ?? undefined;
    if (!login) {
      showClipNotice('error', 'Channel login missing — cannot open the Twitch editor');
      return;
    }
    if (!vodId) {
      showClipNotice('error', t('Not a Twitch VOD URL'));
      return;
    }
    try { videoRef.current?.pause(); } catch { /* ignore */ }
    pauseOtherPreviews();
    setClipPopup({
      url: vod.url,
      broadcasterLogin: login,
      vodId,
      playheadSec: currentTime,
      vodDurationSec: vod.durationSec,
      reuseSession: sessionIdRef.current
        ? { sessionId: sessionIdRef.current, trimTimeline: trimTimelineRef.current }
        : null,
    });
  }, [vod.channel, vod.videoId, vod.url, vod.durationSec, currentTime, showClipNotice]);

  const exploreClipBtn = (fs: boolean) => {
    const clipLabel = !vod.channel?.trim()
      ? t('Channel login missing — cannot open the Twitch editor')
      : t('Open the Twitch clip mini-preview at the playhead');
    return (
      <button
        type="button"
        onClick={() => void openExploreTwitchClip()}
        disabled={!vod.channel?.trim()}
        className={fs ? fsCtrlBtn : ctrlBtn(false)}
        title={clipLabel}
        aria-label={clipLabel}
      >
        <TwitchLogoIcon size={16} className="shrink-0" />
      </button>
    );
  };

  const clipNoticeUi = (
    <div className={`flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-wider px-1 ${
      clipNotice?.kind === 'error' ? 'text-red-400' : 'text-[#53fc18]'
    }`}>
      <AlertCircle size={11} className="shrink-0" />
      <span className="truncate">{clipNotice?.text}</span>
    </div>
  );

  const showClipButton = platform === 'twitch' && !vod.isClip;

  return (
    <div
      ref={containerRef}
      tabIndex={0}
      role="application"
      aria-label={vod.isClip ? t('Channel clip explore player') : t('Channel VOD explore player')}
      onKeyDown={handleKeyDown}
      onPointerDownCapture={(e) => {
        onBringToFront();
        if (!fullscreen && frameMode && videoWrapRef.current?.contains(e.target as Node)) {
          videoPointerRef.current = { x: e.clientX, y: e.clientY, moved: false };
        } else {
          videoPointerRef.current = null;
        }
      }}
      onClick={(e) => {
        const videoPointer = videoPointerRef.current;
        videoPointerRef.current = null;
        if (
          frameMode
          && !fullscreen
          && videoPointer
          && !videoPointer.moved
          && videoWrapRef.current?.contains(e.target as Node)
        ) {
          if (ready) {
            const video = videoRef.current;
            if (video) unlockPreviewAudioFromGesture(video, setMuted, volumeRef.current);
            togglePlay();
          }
        }
        focusPlayer();
      }}
      onPointerMove={frameMode && !fullscreen ? (e) => {
        const videoPointer = videoPointerRef.current;
        if (videoPointer && Math.hypot(e.clientX - videoPointer.x, e.clientY - videoPointer.y) > 4) {
          videoPointer.moved = true;
        }
      } : undefined}
      className={`explore-frame-popup group outline-none focus:ring-2 focus:ring-white/25 flex flex-col overflow-visible bg-zinc-950 ${
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
        width: containerW,
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
        style={fullscreen ? undefined : { transition: 'none' }}
        onPointerDown={fullscreen ? undefined : onPopupDrag}
      >
        {!fullscreen && (
          <div
            className="flex items-start justify-between gap-2 shrink-0"
            draggable={frameMode && !fullscreen}
            onPointerDown={frameMode && !fullscreen ? (e) => e.stopPropagation() : undefined}
            onDragStart={frameMode && !fullscreen ? (e) => {
              e.dataTransfer.setData('text/plain', encodeFrameDragPopupId(id));
              e.dataTransfer.effectAllowed = 'move';
            } : undefined}
          >
            <div className="min-w-0 flex items-start gap-1.5">
              <span
                className="shrink-0 w-5 text-center text-[11px] font-mono font-bold tabular-nums leading-tight pt-0.5"
                style={{ color: platformAccentColor(platform ?? 'kick') }}
                title={`${vod.platform} #${vod.platformListIndex}`}
              >
                {vod.platformListIndex}
              </span>
              <div className="min-w-0">
                <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
                  {vod.isClip ? t('Channel clip explore') : t('Channel VOD explore')}
                </span>
                <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
                  {vod.title}
                </p>
                <p className="text-[9px] font-mono text-zinc-500 truncate leading-tight mt-0.5">
                  {channelVodSubline(vod)}
                  {vod.channel_language ? (
                    <span
                      className="ml-1 border border-zinc-700 px-1 py-px text-[7px] font-bold uppercase tracking-wider text-zinc-400"
                      title={t('Channel language: {lang}', { lang: vod.channel_language })}
                    >
                      {vod.channel_language}
                    </span>
                  ) : null}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {vod.videoId && (
                <button
                  type="button"
                  onClick={() => setArchiveSearchOpen((o) => !o)}
                  aria-pressed={archiveSearchOpen}
                  title={t('Search the local archive for this video only')}
                  className={`flex items-center gap-1 border px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                    archiveSearchOpen
                      ? 'bg-white text-black border-white'
                      : 'border-zinc-700 text-zinc-400 hover:text-white hover:border-white'
                  }`}
                >
                  <Search size={10} className="shrink-0" />
                  {t('Search this video')}
                </button>
              )}
              <button
                type="button"
                onClick={() => setChatOpen((o) => !o)}
                aria-pressed={chatOpen}
                title={chatOpen ? t('Close preview chat panel') : t('Open preview chat panel')}
                className={`flex items-center gap-1 border px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                  chatOpen
                    ? 'bg-white text-black border-white'
                    : 'border-zinc-700 text-zinc-400 hover:text-white hover:border-white'
                }`}
              >
                <MessageSquare size={10} className="shrink-0" />
                {chatOpen ? t('Close chat') : t('Chat')}
              </button>
              <button
                type="button"
                onClick={() => onClose()}
                className="text-zinc-500 hover:text-white p-0.5 shrink-0"
                title={t('Close player')}
              >
                <X size={14} />
              </button>
            </div>
          </div>
        )}
        {!fullscreen && aiEnabled && vod.channel ? (
          <div className="shrink-0 border-2 border-zinc-800 bg-zinc-950/60">
            <button
              type="button"
              onClick={() => setAskOpen((o) => !o)}
              aria-expanded={askOpen}
              className="w-full flex items-center gap-1.5 px-2 py-1 text-left"
            >
              <Sparkles size={10} className="text-amber-300 shrink-0" />
              <span className="text-[8px] font-mono uppercase tracking-widest font-bold text-zinc-400 truncate">
                {t('Ask about this channel')}
              </span>
              <ChevronDown
                size={10}
                className={`ml-auto shrink-0 transition-transform text-zinc-500 ${askOpen ? 'rotate-180' : ''}`}
              />
            </button>
            {askOpen ? (
              <div className="p-2 flex flex-col gap-2 border-t-2 border-zinc-800">
                <input
                  type="text"
                  value={askQuestion}
                  onChange={(e) => setAskQuestion(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void askAi(); }}
                  maxLength={500}
                  placeholder={t('Ask about this channel…')}
                  aria-label="ai ask question"
                  className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1.5 px-2 text-[11px] focus:outline-none focus:border-white"
                />
                <div className="flex items-center gap-1.5 flex-wrap">
                  <select
                    value={askScope}
                    onChange={(e) => setAskScope(e.target.value as 'chat' | 'transcript' | 'all')}
                    aria-label="ai ask scope"
                    className="bg-zinc-950 border-2 border-zinc-800 text-zinc-200 font-mono py-1 px-1.5 text-[10px] focus:outline-none focus:border-white"
                  >
                    <option value="all">{t('All')}</option>
                    <option value="chat">{t('Chat')}</option>
                    <option value="transcript">{t('Transcript')}</option>
                  </select>
                  <input
                    type="number"
                    min={1}
                    value={askDays}
                    onChange={(e) => setAskDays(e.target.value)}
                    placeholder={t('Days')}
                    title={t('Days (blank = entire history)')}
                    aria-label="ai ask days"
                    className="w-16 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1 px-1.5 text-[10px] focus:outline-none focus:border-white"
                  />
                  <button
                    type="button"
                    onClick={() => void askAi()}
                    disabled={askLoading || !askQuestion.trim()}
                    className="flex items-center gap-1 border px-2 py-1 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors border-zinc-700 text-zinc-300 hover:text-white hover:border-white disabled:opacity-40"
                  >
                    {askLoading ? <Loader2 size={10} className="animate-spin" /> : <Sparkles size={10} />}
                    {askLoading ? t('Asking…') : t('Ask')}
                  </button>
                </div>
                {askError ? (
                  <p className="text-[10px] font-mono text-red-400" role="alert">{askError}</p>
                ) : null}
                {askResult ? (
                  <div className="flex flex-col gap-1.5">
                    <p className="text-[11px] text-zinc-200 whitespace-pre-wrap leading-relaxed">
                      {askResult.answer}
                    </p>
                    {askResult.sources.length > 0 ? (
                      <div className="flex flex-col gap-1">
                        <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 font-bold">
                          {t('Sources')}
                        </span>
                        {askResult.sources.map((s, i) => (
                          <span
                            key={i}
                            className="text-[9px] font-mono text-zinc-500 truncate"
                            title={s.matched_text}
                          >
                            {s.video_title}
                            {s.created_at ? ` · ${s.created_at.slice(0, 10)}` : ''}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
        <div
          ref={playerRowRef}
          // Height is pinned by the height-explosion guard via direct style
          // write (see useLayoutEffect above) so the chat column self-stretches
          // to exactly the player column height.
          className={fullscreen ? 'relative flex flex-col gap-0 min-w-0 flex-1 min-h-0 w-full' : 'flex flex-row gap-2 min-w-0 min-h-0 items-start'}
        >
          <div
            ref={playerColRef}
            className={`relative flex flex-col ${fullscreen ? 'h-full min-h-0 gap-0 w-full' : 'gap-2 min-w-0 flex-1'}`}
          >
        <div
          ref={videoWrapRef}
          className={`relative bg-black overflow-hidden w-full cursor-pointer ${
            fullscreen ? 'absolute inset-0 z-0 border-0' : 'border-2 border-zinc-700 shrink-0'
          }`}
          style={fullscreen ? undefined : { aspectRatio: videoAspect, maxHeight: videoAspect < 1 ? '80vh' : undefined, transition: 'max-height 0.3s ease' }}
          onPointerDown={frameMode && !fullscreen ? undefined : (e) => e.stopPropagation()}
          onClick={frameMode && !fullscreen ? undefined : () => {
            if (!ready) return;
            const video = videoRef.current;
            if (video) unlockPreviewAudioFromGesture(video, setMuted, volumeRef.current);
            togglePlay();
          }}
        >
          {youtubeEmbed ? (
            <>
              <iframe
                ref={youtubeIframeRef}
                className="youtube-embed-frame pointer-events-none"
                src={youtubeEmbed}
                title={t('YouTube mini preview')}
                allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
                allowFullScreen
                onLoad={() => {
                  setReady(true);
                  setLoading(false);
                  previewStartTimeoutRef.current?.markReady();
                  youtubeIframeListen(youtubeIframeRef.current);
                  postYoutubeCommand('setVolume', [Math.round(volumeRef.current * 100)]);
                }}
              />
              <div className="absolute inset-0 z-[1]" aria-hidden="true" />
            </>
          ) : (
          <video
            ref={videoRef}
            className="w-full h-full object-contain pointer-events-none"
            muted={muted}
            playsInline
            poster={resolveVideoThumbnail(vod.thumbnailUrl ?? null, 640, 360) || undefined}
            onLoadedMetadata={() => {
              const video = videoRef.current;
              if (!video?.videoWidth || !video?.videoHeight) return;
              const aspect = video.videoWidth / video.videoHeight;
              if (
                !trimTimelineRef.current
                && Number.isFinite(video.duration)
                && video.duration > 0
              ) {
                setMediaDurationSec(Math.round(video.duration));
              }
              videoAspectRef.current = aspect;
              setVideoAspect(aspect);
              const totalW = clampExplorePanelWidth(
                panelWidthRef.current + chatTotalRef.current,
                chromeHRef.current,
                aspect,
              );
              const clampedW = Math.max(EXPLORE_PANEL_MIN_W, totalW - chatTotalRef.current);
              panelWidthRef.current = clampedW;
              setPanelWidth(clampedW);
            }}
            onTimeUpdate={() => {
              const video = videoRef.current;
              if (!video) return;
              // During an out-of-chunk remux the HLS loader briefly reports
              // positions near the new chunk's mux start while we wait for
              // FRAG_BUFFERED to land the explicit seek. Ignore those reports
              // so the slider doesn't bounce.
              if (seekLockedRef.current) return;
              // While a user seek is in flight (optimistic UI already shows the
              // target), ignore timeupdate reports at the old position so the
              // controlled slider doesn't snap back before the seek lands.
              if (seekTargetRef.current != null) return;
              if (windowHlsTimeline) {
                setCurrentTime(windowHlsVodTimeSec(video.currentTime, windowHlsMuxStartRef.current));
              } else {
                setCurrentTime(video.currentTime);
              }
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
          )}
          {!youtubeEmbed && instantPreview.show && instantPreview.matched && (
            <video
              ref={instantPreview.videoRef}
              className="absolute inset-0 w-full h-full object-contain pointer-events-none z-30"
              src={instantPreview.matched.media_url}
              autoPlay
              muted
              playsInline
              preload="auto"
              onEnded={instantPreview.onOverlayEnded}
              onError={instantPreview.onOverlayError}
            />
          )}
          {loading && !ready && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/40 z-20">
              <Loader2 size={28} className="animate-spin text-zinc-300" />
              <span className="text-zinc-300 text-[10px] font-mono">
                {platform === 'youtube' ? 'Starting YouTube preview…' : 'Preparing preview…'}
              </span>
            </div>
          )}
          {buffering && ready && !loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/35 z-20 pointer-events-none">
              <Loader2 size={24} className="animate-spin text-zinc-200/90" />
            </div>
          )}
          {error && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/80 z-20 p-3">
              <p className="text-red-400 text-[10px] font-mono text-center">{error}</p>
              {previewRetry && (
                <button
                  type="button"
                  onClick={retryPreview}
                  title={t('Retry this preview only')}
                  className="flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-red-300"
                >
                  <RefreshCw size={12} />
                  Retry
                </button>
              )}
            </div>
          )}
          {fullscreen && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onClose(); }}
              className="absolute top-3 right-3 z-20 text-zinc-400 hover:text-white p-2 pointer-events-auto"
              title={t('Close player')}
            >
              <X size={20} />
            </button>
          )}
        </div>
        {!fullscreen && archiveSearchOpen && vod.videoId && (
          <ArchiveSearchPopup
            zIndex={zIndex + 50}
            onClose={() => setArchiveSearchOpen(false)}
            onOpenHit={onOpenHit}
            onSeekHit={(hit) => seekVideo(hit.offset_sec)}
            onSeekOffset={(sec) => seekVideo(sec)}
            scope={{ videoId: vod.videoId, title: vod.title }}
          />
        )}
        {!fullscreen && (
          <>
            {timelineUi}
            <p className="text-[8px] font-mono text-zinc-600 uppercase tracking-wider text-center shrink-0">
              Fullscreen to explore
            </p>
            <div className="flex items-center justify-between gap-2 shrink-0 flex-wrap">
              <div className="flex items-center gap-1.5">
                <button type="button" onClick={togglePlay} disabled={!ready} className={ctrlBtn(false)}>
                  {playing ? <Pause size={18} /> : <Play size={18} />}
                </button>
                {volumeUi(false)}
                {showClipButton && exploreClipBtn(false)}
                <button
                  type="button"
                  onClick={() => onHandoffToMain(
                    vod,
                    currentTime,
                    null,
                    chatMarkersRef.current.start != null || chatMarkersRef.current.end != null
                      ? chatMarkersRef.current
                      : null,
                  )}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white px-2 py-2 disabled:opacity-40"
                  title={t('Open in main preview to download')}
                  aria-label={t('Open in main preview to download')}
                >
                  <Download size={14} />
                </button>
              </div>
              <div className="flex items-center gap-1.5 ml-auto">
                {qualityUi(false)}
                <button
                  type="button"
                  onClick={() => void toggleFullscreen()}
                  disabled={!ready}
                  className={platformPreviewCtrlBtn(platform, false)}
                  title={t('Fullscreen')}
                >
                  <Maximize2 size={18} />
                </button>
              </div>
            </div>
            {clipNotice && clipNoticeUi}
          </>
        )}
          </div>
          {!fullscreen && (
            <PreviewChatPanel
              platform={platform}
              videoId={vod.videoId ?? null}
              currentTime={currentTime}
              // Channel-scoped custom emotes (BTTV/FFZ/7TV) for twitch rows.
              channel={vod.channel}
              // Host-controlled: opens from the Chat button next to Search
              // this video (no collapsed side strip).
              open={chatOpen}
              onOpenChange={setChatOpen}
              // Click-to-seek: rows/caption seek this popup's own player
              // (seekVideo clamps to the effective duration and no-ops
              // until ready).
              onSeek={seekVideo}
              onLayoutChange={setChatInfo}
              onMarkersChange={handleChatMarkersChange}
            />
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
              <div className="flex items-center gap-2 justify-between">
                <div className="flex items-center gap-2">
                  <button type="button" onClick={togglePlay} disabled={!ready} className={fsCtrlBtn}>
                    {playing ? <Pause size={18} /> : <Play size={18} />}
                  </button>
                  {volumeUi(true)}
                  {showClipButton && exploreClipBtn(true)}
                  <button
                    type="button"
                    onClick={() => onHandoffToMain(
                    vod,
                    currentTime,
                    null,
                    chatMarkersRef.current.start != null || chatMarkersRef.current.end != null
                      ? chatMarkersRef.current
                      : null,
                  )}
                    className="border border-white/20 bg-black/25 text-zinc-100 px-2 py-2 backdrop-blur-[1px]"
                    title={t('Open in main preview to download')}
                    aria-label={t('Open in main preview to download')}
                  >
                    <Download size={14} />
                  </button>
                </div>
                <div className="flex items-center gap-1.5 ml-auto">
                  {qualityUi(true)}
                  <button
                    type="button"
                    onClick={() => void toggleFullscreen()}
                    disabled={!ready}
                    className={platformPreviewCtrlBtn(platform, false)}
                    title={t('Exit fullscreen')}
                  >
                    <Minimize2 size={18} />
                  </button>
                </div>
              </div>
              {clipNotice && clipNoticeUi}
            </div>
            <div
              className="absolute bottom-0 right-0 z-30 w-10 h-10 cursor-pointer"
              title={t('Exit fullscreen')}
              onClick={() => void toggleFullscreen()}
            />
          </>
        )}
      </div>
      {!fullscreen && (
        <PanelResizeHandles onPointerDown={onPanelResize} />
      )}
      {clipPopup && (
        <TwitchClipPopup
          url={clipPopup.url}
          broadcasterLogin={clipPopup.broadcasterLogin}
          vodId={clipPopup.vodId}
          playheadSec={clipPopup.playheadSec}
          vodDurationSec={clipPopup.vodDurationSec}
          reuseSession={clipPopup.reuseSession}
          // The clip title defaults to the VOD's title (user-mandated:
          // the clip keeps the ORIGINAL title) — sent as vodrip_title so
          // the extension fills the editor's required field.
          vodTitle={vod.title}
          // Ladder-derived: parent's shared-ladder rank + 50 headroom — at
          // spawn this is above every other window (search panel included).
          // ponytail: 50+ new ranks while this popup is open could overtake
          // it; upgrade path is a ladder rank of its own via App.
          zIndex={zIndex + 50}
          initialVolume={PREVIEW_DEFAULT_VOLUME}
          onClose={() => setClipPopup(null)}
        />
      )}
    </div>
  );
}
