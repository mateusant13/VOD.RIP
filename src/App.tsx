import { Fragment, useState, useEffect, useCallback, useMemo, useRef, type Dispatch, type KeyboardEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import {
  Download, Info, Play, Pause, Link2, X, Clock,
  Users, Database, Settings2, Loader2,
  AlertCircle, RefreshCw, Pencil, Plus,
  ExternalLink, Eye, Volume2, VolumeX, Maximize2, Minimize2,
  GripVertical,
} from 'lucide-react';
import ChannelExplorePopup, { type ExplorePopupVod } from './ChannelExplorePopup';
import LocalFilePopup, { type LocalFilePopupItem } from './LocalFilePopup';
import PreviewQualityMenu from './PreviewQualityMenu';
import {
  PREVIEW_CLIP_DEFAULT_HEIGHT,


  attachProgressivePreview,
  bindProgressivePreviewRecovery,
  detachProgressivePreview,
  isClipRelativePreviewDuration,
  initialPreviewPreferHeight,
  resolveInitialHlsPreviewHeight,

  maxQualityLabelFromList,
  measurePlayerHeightCap,

  mergeVariantHeights,
  parseQualityHeights,
  resolveHlsPreviewLevels,
  isClipPreviewUrl,
  resolvePreviewPlayback,
  previewSessionRefreshHandoff,
  previewSeekOptimisticUi,
  resolveProgressivePreviewLevels,
  resolveProgressivePreviewLevelsAsync,
  suggestClipDownloadName,
  suggestVideoDownloadName,
  warmYoutubePreview,
  warmYoutubePreviewBatch,
  warmYoutubePreviewFull,
  cancelWarmYoutubePreviewFull,
  bindYoutubeChannelScrollWarm,
  clampPreviewTimeToVodTrim,
  PREVIEW_SEEK_DEBOUNCE_MS,
  YOUTUBE_PREVIEW_ALLOW_HEIGHTS,
  seekYoutubeWindowHls,
  windowHlsVideoTimeSec,
  isPositionInWindowHlsMux,
  attachPreviewBufferingListeners,
  applyVideoLocalSeek,
  reloadWindowHlsAtPosition,
  shieldPreviewBuffering,
  type PreviewLevelOption,
} from './previewPlayerUtils';
import { PreviewTiming, waitVideoPlayable } from './previewTiming';
import DownloadConfirmDialog from './components/DownloadConfirmDialog';
import EditableHmsTime from './components/EditableHmsTime';
import { formatHmsFull } from './utils';
import { actionBtnHover, platformPreviewCtrlBtn, platformCardShadow, platformVodPanelBtn, platformWatchPreviewBtn, platformBulkDownloadBtn, type PlatformStyleKey } from './platformStyles';
import { fmtDuration, fmtShort, fmtClipDuration, formatClipDurationHuman, fmtDateAndAgo, fmtViews, parseVideoTs, formatBytes, basename, sourceQualityOptionLabel } from './formatters';
import type { VideoInfo, ChannelVideo, ListedChannelVideo, SavedChannel, ChannelPreviewBadge, AppSettings, UpdateInfo, DownloadState, DownloadsResponse, Tab, LayoutPanelBoundsInput, PersistedPanelLayout, PreviewSessionResponse } from './types';
import { detectUrlPlatform, isClipUrl, detectVideoPlatform, bestAvailableQuality, channelVideoDurationSec, videoInfoDurationSec, syncDurationFromPreviewSession, isLikelyClip, mergeVodLists, mergeClipLists, channelClipsMissing, channelVodsMissing, channelStreamsMissing, channelHasCachedContent, mergeClipPlatformsFetched, mergeVodPlatformsFetched, buildVodUrl, parseChannelInput, slugFromVideoUrl, isChannelAlreadySaved, deriveChannelDisplayName, normalizeSavedChannel, loadSavedChannels, persistChannels, isHiddenChannelPlatformError, channelVodSubline, reorderChannelsById, mapApiChannelItem, channelInsertIndex, estimateDownloadBytes, resolveVideoThumbnail, findCachedVideoThumbnail, CHANNEL_INITIAL_VISIBLE, CHANNEL_EXPAND_STEP, CHANNEL_FETCH_LIMIT, CHANNEL_INCREMENTAL_LIMIT, CHANNEL_UI_STORAGE_KEY, MAX_SAVED_CHANNELS, loadStoredChannelUi, channelPlatformVisibleSlice, channelPlatformCanExpand, sortChannelVideosByMode, CHANNEL_RECENT_DAYS, channelLinkDraftFromParsed, channelLinkDraftSlugs, type ChannelLinkDraft } from './channelUtils';
import ChannelLinkCard from './components/ChannelLinkCard';
import { YOUTUBE_COLOR, platformAccentColor, platformStyleKey, platformActiveBorder, vodCheckboxStyle } from './platformColors';
import { clampTrimEndpoints, trimButtonDeltaForEndpoint, adjustTrimEndpointByDelta, type TrimRangeOpts } from './trimUtils';
import { panelMaxW, layoutMaxPanelWidth, layoutMaxPanelWidthAtSiblingMins, layoutMaxPanelHeight, clampPanelSizeForLayout, clampAllLayoutPanels, clampPreviewPanelWidth, resizeLayoutGivingWidthTo, layoutRowEdgeInsets, layoutRowHasMultiplePanels as layoutHasMultiplePanels, applyPanelSize, startPanelResizeDrag, applyPanelWidth, startPanelWidthResize, defaultPanelLayout, loadPanelLayout, persistPanelLayout, clampLayoutNumber, clampStoredPanelSize, PREVIEW_KEY_SKIP_SEC, PREVIEW_FS_CONTROLS_HIDE_MS, PREVIEW_DEFAULT_VOLUME, PREVIEW_PANEL_MIN_W, PREVIEW_PANEL_CHROME_H_EST, PREVIEW_VIDEO_ASPECT_DEFAULT, URL_ASIDE_PANEL_DEFAULT, MAIN_PANEL_DEFAULT, EXPLORE_POPUP_Z, MAX_EXPLORE_POPUPS } from './layoutUtils';
import ChannelListIndexBadge from './components/ChannelListIndexBadge';
import ChannelPlatformLabel from './components/ChannelPlatformLabel';
import PlatformVodIcon from './components/PlatformVodIcon';
import ChannelClipThumb from './components/ChannelClipThumb';
import ClipDurationAdjustButtons from './components/ClipDurationAdjustButtons';
import NeedleGlancePopup, { type NeedleGlanceState } from './components/NeedleGlancePopup';
import QueueTab from './components/QueueTab';
import SettingsTab from './components/SettingsTab';
import { PanelResizeHandles, panelResizeHandleInset, type ResizeEdge } from './explorePopupUtils';
import { shouldIgnorePlayerKeyEvent } from './keyboardUtils';
import { applyDownloadSseEvent, useDownloadStreams } from './hooks/useDownloadStreams';import { apiGet, apiPost, apiDelete } from './hooks/useApiClient';
import { useViewportTier } from './useViewportTier';
import { usePreviewPlayer } from './hooks/usePreviewPlayer';
import { useDirectMSEPlayer } from './hooks/useDirectMSEPlayer';
import { youtubeIframeCommand, youtubeIframeListen } from './youtubeEmbed';

// ─── TYPES (migrated to src/types.ts) ───────────────
const IS_DEV_UI = import.meta.env.DEV;
const USE_MSE_DIRECT = import.meta.env.VITE_PREVIEW_MSE_DIRECT === "true";
// Expose the flag for e2e probes (see e2e/tests/preview-mse-direct.spec.ts).
(window as unknown as { __VITE_PREVIEW_MSE_DIRECT__?: boolean }).__VITE_PREVIEW_MSE_DIRECT__ = USE_MSE_DIRECT;

// ─── HELPERS ─────────────────────────────────────────────────────────────────


/** Let text fields, modifiers (Ctrl+A, etc.), and contenteditable keep native behavior. */

;

;


function startChannelReorderDrag(
  e: ReactPointerEvent<HTMLButtonElement>,
  channelId: string,
  listRef: MutableRefObject<HTMLDivElement | null>,
  setChannels: Dispatch<SetStateAction<SavedChannel[]>>,
  setDragId: Dispatch<SetStateAction<string | null>>,
  setDropInsertIndex: Dispatch<SetStateAction<number | null>>,
) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);
  setDragId(channelId);

  const prevUserSelect = document.body.style.userSelect;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = 'grabbing';

  let frame = 0;
  let pendingY: number | null = null;
  let lastInsert = -1;

  const flush = () => {
    frame = 0;
    if (pendingY === null) return;
    const list = listRef.current;
    if (!list) return;
    const insertAt = channelInsertIndex(list, pendingY);
    if (insertAt === lastInsert) return;
    lastInsert = insertAt;
    setDropInsertIndex(insertAt);
  };

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    pendingY = ev.clientY;
    if (!frame) frame = requestAnimationFrame(flush);
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    if (frame) cancelAnimationFrame(frame);
    const list = listRef.current;
    const insertAt = list && pendingY !== null
      ? channelInsertIndex(list, pendingY)
      : lastInsert;
    if (insertAt >= 0) {
      setChannels((prev) => reorderChannelsById(prev, channelId, insertAt));
    }
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = '';
    setDragId(null);
    setDropInsertIndex(null);
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}


// ─── APP ─────────────────────────────────────────────────────────────────────

export default function App() {
  const viewportTier = useViewportTier();
  const [tab, setTab] = useState<Tab>('url');

  // URL mode
  const [url, setUrl] = useState('');
  const [videoInfo, setVideoInfo] = useState<VideoInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [videoInfoThumbFailed, setVideoInfoThumbFailed] = useState(false);

  // Download options
  const [quality, setQuality] = useState('source');
  const [downloadAsAudio, setDownloadAsAudio] = useState(false);
  const urlPlatform = detectUrlPlatform(url);
  const [trimStartSec, setTrimStartSec] = useState(0);
  const [previewMetaDurationSec, setPreviewMetaDurationSec] = useState(0);
  const [trimEndSec, setTrimEndSec] = useState(3600);
  const [trimPanelHeight, setTrimPanelHeight] = useState(0);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewSessionId, setPreviewSessionId] = useState<string | null>(null);
  const [previewPlayback, setPreviewPlayback] = useState<{
    url: string;
    kind: 'hls' | 'progressive';
    variantHeights?: number[];
    qualityLabels?: string[];
    activeHeight?: number;
  } | null>(null);
  const [previewVideoLoading, setPreviewVideoLoading] = useState(false);
  const [previewBuffering, setPreviewBuffering] = useState(false);
  const [previewVideoReady, setPreviewVideoReady] = useState(false);
  const [previewYoutubeEmbedUrl, setPreviewYoutubeEmbedUrl] = useState<string | null>(null);
  const previewYoutubeIframeRef = useRef<HTMLIFrameElement>(null);
  const previewVideoLoadingRef = useRef(false);
  const previewVideoReadyRef = useRef(false);
  const [previewTimeUi, setPreviewTimeUi] = useState(0);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const [previewMuted, setPreviewMuted] = useState(false);
  const [previewVolume, setPreviewVolume] = useState(PREVIEW_DEFAULT_VOLUME);
  const [previewFullscreen, setPreviewFullscreen] = useState(false);
  const [previewFsControlsVisible, setPreviewFsControlsVisible] = useState(true);
  const [previewQualityMenuOpen, setPreviewQualityMenuOpen] = useState(false);
  const [previewVolumeMenuOpen, setPreviewVolumeMenuOpen] = useState(false);
  const [channelVodPanelOpen, setChannelVodPanelOpen] = useState(false);
  const [previewChannelBadge, setPreviewChannelBadge] = useState<ChannelPreviewBadge | null>(null);
  /** URL tab hidden from bar after picking a VOD from channels; restored only on page refresh. */
  const [urlTabBarHidden, setUrlTabBarHidden] = useState(false);
  const [previewTrimStart, setPreviewTrimStart] = useState(0);
  const [previewTrimEnd, setPreviewTrimEnd] = useState(3600);
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  const previewPlayheadRef = useRef<HTMLDivElement>(null);
  const previewCurrentTimeRef = useRef(0);
  const previewTimeUiRef = useRef(0);
  const vodDurationSecRef = useRef(0);
  const previewContainerRef = useRef<HTMLDivElement>(null);
  const previewControlsRef = useRef<HTMLDivElement>(null);
  const previewHlsRef = useRef<Hls | null>(null);
  const previewVolumeRef = useRef(PREVIEW_DEFAULT_VOLUME);
  const previewFsHideTimerRef = useRef<number | null>(null);
  const previewInitialSeekDoneRef = useRef(false);
  const previewInitialPlayDoneRef = useRef(false);
  const previewSuppressPlayRef = useRef(false);
  /** Monotonic generation counter — increment to cancel in-flight openPreview. */
  const previewGenRef = useRef(0);
  const previewClipRelativeRef = useRef(false);
  /** YouTube window-HLS — HLS timeline is chunk-relative (see previewWindowHlsMuxStartRef). */
  const previewTrimTimelineRef = useRef(false);
  /** YouTube window-HLS chunk offset — local timeline 0 = mux_start on VOD. */
  const previewWindowHlsMuxStartRef = useRef(0);
  const previewWindowHlsMuxEndRef = useRef(0);
  const previewSeekInflightRef = useRef(0);
  const previewSeekLockedRef = useRef(false);
  const previewRecoveryTimerRef = useRef<number | null>(null);
  const previewBufferingClearRef = useRef<(() => void) | null>(null);
  const fetchVideoInfoGenRef = useRef(0);
  /** URL that current videoInfo / trim sliders belong to — gates Watch Preview. */
  const [videoInfoUrl, setVideoInfoUrl] = useState<string | null>(null);
  /** ponytail: in-memory VideoInfo cache. Avoids re-fetching the same URL when
   *  the user pastes/types it again. Bounded to 32 entries (LRU-ish). */
  const videoInfoCacheRef = useRef<Map<string, VideoInfo>>(new Map());
  /** Cancels debounced YouTube metadata prefetch when URL changes. */
  const youtubePrefetchGenRef = useRef(0);
  /** ponytail: debounce URL warm so every keystroke doesn't fire a network call. */
  const urlWarmTimerRef = useRef<number | null>(null);
  const channelsScrollRef = useRef<HTMLDivElement>(null);
  /** True while a preview is active (loaded or loading) — blocks re-clicks. */
  const previewStartedRef = useRef(false);
  /** URL currently loaded in the preview player (may differ from `url` while browsing channel VODs). */
  const previewLoadedUrlRef = useRef<string | null>(null);
  const previewTrimStartRef = useRef(0);
  const previewTrimEndRef = useRef(3600);
  const previewSessionMetaRef = useRef<{
    variantHeights: number[];
    qualityLabels?: string[];
    activeHeight: number;
  } | null>(null);
  /** Menu selection — may exceed on-screen playback height until fullscreen. */
  const previewRequestedHeightRef = useRef(0);
  const previewAppliedHeightRef = useRef(0);
  const previewExtractSourceRef = useRef('');
  const previewSessionIdRef = useRef<string | null>(null);
  /** YouTube Extract Info — session created in parallel so Watch Preview attaches instantly. */
  const previewSessionPrefetchRef = useRef<{
    url: string;
    session: PreviewSessionResponse;
  } | null>(null);
  const previewTimingRef = useRef<PreviewTiming | null>(null);
  const previewSeekDebounceRef = useRef<number | null>(null);
  const previewPlaybackKindRef = useRef<'hls' | 'progressive'>('progressive');
  const previewPendingSeekSecRef = useRef<number | null>(null);
  const previewSeekTargetRef = useRef<number | null>(null);
  const previewCachedProgressiveRef = useRef(false);
  // ── Shared preview hook (quality state machine) ──────────────────────────
  const {
    previewLevels,
    qualityLevel: previewQualityLevel,

    syncPlaybackToViewport: syncPreviewPlaybackToViewport,
    applyQuality: applyPreviewQuality,
    setPreviewLevels,
    setQualityLevel: setPreviewQualityLevel,
    setHlsRef,
    syncHlsLevels: syncPreviewHlsLevels,
    prefetchNextSegments,
  } = usePreviewPlayer({
    videoRef: previewVideoRef,
    playback: previewPlayback,
    sessionId: previewSessionId,
    isClipPreview: isClipUrl(url.trim()),
    isYoutubePreview: urlPlatform === 'youtube',
    containerRef: previewContainerRef,
    trimStart: previewTrimStartRef.current,
    trimTimelineRef: previewTrimTimelineRef,
    onPreviewError: (msg: string) => {
      if (msg) setError(msg);
    },
  });

  // Direct MSE player for YouTube window-HLS (opt-in via VITE_PREVIEW_MSE_DIRECT).
  const msePlayer = useDirectMSEPlayer(previewVideoRef);
  const msePlayerRef = useRef<typeof msePlayer | null>(null);
  msePlayerRef.current = msePlayer;

  useEffect(() => {
    previewPlaybackKindRef.current = previewPlayback?.kind ?? 'progressive';
  }, [previewPlayback?.kind]);

  const previewSessionHandoffRefs = {
    trimTimelineRef: previewTrimTimelineRef,
    windowHlsMuxStartRef: previewWindowHlsMuxStartRef,
    windowHlsMuxEndRef: previewWindowHlsMuxEndRef,
    extractSourceRef: previewExtractSourceRef,
    pendingSeekSecRef: previewPendingSeekSecRef,
    cachedProgressiveRef: previewCachedProgressiveRef,
    sessionMetaRef: previewSessionMetaRef,
  };

  const applyPreviewSessionRefresh = useCallback((res: PreviewSessionResponse) => (
    previewSessionRefreshHandoff(
      previewLoadedUrlRef.current ?? url.trim(),
      res,
      previewSessionHandoffRefs,
      setPreviewPlayback,
      () => previewVideoRef.current?.currentTime ?? 0,
    )
  ), [url]);

  const previewNeedleRailRef = useRef<HTMLDivElement>(null);
  const [needleGlance, setNeedleGlance] = useState<NeedleGlanceState | null>(null);
  const [downloadConfirmOpen, setDownloadConfirmOpen] = useState(false);
  const [downloadFilename, setDownloadFilename] = useState('');
  const trimStartSecRef = useRef(0);
  const trimEndSecRef = useRef(3600);
  const trimDragOriginRef = useRef(0);
  const trimPanelResizeRef = useRef<{ startY: number; startHeight: number } | null>(null);
  const previewOpenRef = useRef(false);
  /** True while dragging URL trim sliders or preview in/out needles. */
  const trimDragActiveRef = useRef(false);
  /** Opposite trim endpoint pinned for the duration of a URL slider drag. */
  const urlTrimDragPinRef = useRef<{
    which: 'in' | 'out';
    fixedStart: number;
    fixedEnd: number;
  } | null>(null);
  const urlTrimPointerRef = useRef({ x: 0, y: 0 });
  const lastUrlTrimEndpointRef = useRef<'in' | 'out'>('in');
  const lastPreviewTrimEndpointRef = useRef<'in' | 'out'>('out');
  const [lastUrlTrimEndpoint, setLastUrlTrimEndpoint] = useState<'in' | 'out'>('in');
  const [lastPreviewTrimEndpoint, setLastPreviewTrimEndpoint] = useState<'in' | 'out'>('out');

  // Channel explore players (up to 5 floating popups)
  const [explorePopups, setExplorePopups] = useState<{ id: string; vod: ExplorePopupVod; layoutIndex: number }[]>([]);
  const [localFilePopups, setLocalFilePopups] = useState<LocalFilePopupItem[]>([]);
  const [exploreZOrder, setExploreZOrder] = useState<Record<string, number>>({});
  const [anyExploreVolumeMenuOpen, setAnyExploreVolumeMenuOpen] = useState(false);
  const [exploreVolumeMenuCloseTick, setExploreVolumeMenuCloseTick] = useState(0);
  const explorePauseMapRef = useRef(new Map<string, () => void>());
  const exploreVolumeMenusRef = useRef(new Set<string>());
  const exploreZCounterRef = useRef(0);
  const [initialPanelLayout] = useState(loadPanelLayout);
  const [previewPanelWidth, setPreviewPanelWidth] = useState(initialPanelLayout.previewPanelWidth);
  const [previewVideoAspect, setPreviewVideoAspect] = useState(PREVIEW_VIDEO_ASPECT_DEFAULT);
  const [urlAsidePanelSize, setUrlAsidePanelSize] = useState(initialPanelLayout.urlAside);
  const [mainPanelSize, setMainPanelSize] = useState(initialPanelLayout.main);
  const previewPanelWidthRef = useRef(initialPanelLayout.previewPanelWidth);
  const previewVideoAspectRef = useRef(PREVIEW_VIDEO_ASPECT_DEFAULT);
  const previewChromeHRef = useRef(PREVIEW_PANEL_CHROME_H_EST);
  const urlAsidePanelSizeRef = useRef(initialPanelLayout.urlAside);
  const mainPanelSizeRef = useRef(initialPanelLayout.main);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const urlAsidePanelRef = useRef<HTMLDivElement>(null);
  const mainPanelRef = useRef<HTMLDivElement>(null);
  const panelLayoutPersistReadyRef = useRef(false);
  const panelLayoutSaveTimerRef = useRef<number | null>(null);

  const restorePanelLayout = useCallback((pl: PersistedPanelLayout) => {
    const clampedUrl = clampStoredPanelSize(pl.urlAside, URL_ASIDE_PANEL_DEFAULT);
    const clampedMain = clampStoredPanelSize(pl.main, MAIN_PANEL_DEFAULT);
    const layout = layoutBoundsInput();
    const clampedPreviewW = clampLayoutNumber(
      pl.previewPanelWidth,
      PREVIEW_PANEL_MIN_W,
      layout.previewOpen
        ? layoutMaxPanelWidthAtSiblingMins('preview', layout)
        : panelMaxW(),
      defaultPanelLayout().previewPanelWidth,
    );
    previewPanelWidthRef.current = clampedPreviewW;
    urlAsidePanelSizeRef.current = clampedUrl;
    mainPanelSizeRef.current = clampedMain;
    setPreviewPanelWidth(clampedPreviewW);
    setUrlAsidePanelSize(clampedUrl);
    setMainPanelSize(clampedMain);
    persistPanelLayout({
      previewPanelWidth: clampedPreviewW,
      urlAside: clampedUrl,
      main: clampedMain,
    });
  }, []);

  const readCurrentPanelLayout = useCallback((): PersistedPanelLayout => ({
    previewPanelWidth: previewPanelWidthRef.current,
    urlAside: { ...urlAsidePanelSizeRef.current },
    main: { ...mainPanelSizeRef.current },
  }), []);

  const flushPanelLayoutToBackend = useCallback(() => {
    if (!panelLayoutPersistReadyRef.current) return;
    const layout = readCurrentPanelLayout();
    persistPanelLayout(layout);
    if (panelLayoutSaveTimerRef.current) {
      window.clearTimeout(panelLayoutSaveTimerRef.current);
      panelLayoutSaveTimerRef.current = null;
    }
    const body = JSON.stringify({ panel_layout: layout });
    try {
      void fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: true,
      });
    } catch {
      apiPost('/api/settings', { panel_layout: layout }).catch(() => {});
    }
  }, [readCurrentPanelLayout]);

  useEffect(() => {
    const win = window as Window & {
      __vodripFlushPanelLayout?: () => void;
      __vodripReadPanelLayout?: () => PersistedPanelLayout;
    };
    win.__vodripFlushPanelLayout = flushPanelLayoutToBackend;
    win.__vodripReadPanelLayout = readCurrentPanelLayout;
    const onPageHide = () => flushPanelLayoutToBackend();
    window.addEventListener('pagehide', onPageHide);
    return () => {
      delete win.__vodripFlushPanelLayout;
      delete win.__vodripReadPanelLayout;
      window.removeEventListener('pagehide', onPageHide);
    };
  }, [flushPanelLayoutToBackend, readCurrentPanelLayout]);

  // Persist main layout panels (preview / URL aside / main card) — not channel explore popups.
  useEffect(() => {
    const layout = {
      previewPanelWidth,
      urlAside: urlAsidePanelSize,
      main: mainPanelSize,
    };
    persistPanelLayout(layout);
    if (!panelLayoutPersistReadyRef.current) return;
    if (panelLayoutSaveTimerRef.current) {
      window.clearTimeout(panelLayoutSaveTimerRef.current);
    }
    panelLayoutSaveTimerRef.current = window.setTimeout(() => {
      apiPost('/api/settings', { panel_layout: layout }).catch(() => {});
    }, 400);
    return () => {
      if (panelLayoutSaveTimerRef.current) {
        window.clearTimeout(panelLayoutSaveTimerRef.current);
      }
    };
  }, [previewPanelWidth, urlAsidePanelSize, mainPanelSize]);

  // Queue
  const [queueDownloads, setQueueDownloads] = useState<DownloadState[]>([]);
  const [recentDownloads, setRecentDownloads] = useState<DownloadState[]>([]);
  const [historyDownloads, setHistoryDownloads] = useState<DownloadState[]>([]);
  const [selectedQueueIds, setSelectedQueueIds] = useState<Set<string>>(new Set());
  const [selectedHistoryIds, setSelectedHistoryIds] = useState<Set<string>>(new Set());
  const [selectedRecentIds, setSelectedRecentIds] = useState<Set<string>>(new Set());
  const pendingRemovalIdsRef = useRef<Set<string>>(new Set());
  const [selectedChannelVodUrls, setSelectedChannelVodUrls] = useState<Set<string>>(new Set());
  // Channels — persisted in localStorage (survives server restarts).
  const [savedChannels, setSavedChannels] = useState<SavedChannel[]>(() => loadSavedChannels());
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);
  const [addChannelInput, setAddChannelInput] = useState('');
  const [pendingAddChannel, setPendingAddChannel] = useState<ChannelLinkDraft | null>(null);
  const [editingChannelId, setEditingChannelId] = useState<string | null>(null);
  const [editingChannelName, setEditingChannelName] = useState('');
  const [editingSlug, setEditingSlug] = useState<{ channelId: string; platform: 'Kick' | 'Twitch' | 'YouTube' } | null>(null);
  const [editingSlugValue, setEditingSlugValue] = useState('');
  const [addChannelNotice, setAddChannelNotice] = useState<string | null>(null);
  const [channelDragId, setChannelDragId] = useState<string | null>(null);
  const [channelDropInsertIndex, setChannelDropInsertIndex] = useState<number | null>(null);
  const channelListRef = useRef<HTMLDivElement>(null);
  const channelsPersistReadyRef = useRef(false);
  const channelsSaveTimerRef = useRef<number | null>(null);
  const channelUiSaveTimerRef = useRef<number | null>(null);
  /** True after saved channels were hydrated once (localStorage wins over API). */
  const channelsHydratedRef = useRef(false);
  const channelUiPersistReadyRef = useRef(false);
  const [pickingFolder, setPickingFolder] = useState(false);
  const initialChannelUi = useMemo(() => loadStoredChannelUi(), []);
  // Platform filter for channel browsing — persisted in settings + localStorage.
  const [kickEnabled, setKickEnabled] = useState(initialChannelUi.kick);
  const [twitchEnabled, setTwitchEnabled] = useState(initialChannelUi.twitch);
  const [youtubeEnabled, setYoutubeEnabled] = useState(initialChannelUi.youtube);
  // How many cached VODs to show per platform (expand is client-side only).
  const [kickVisibleLimit, setKickVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);
  const [twitchVisibleLimit, setTwitchVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);
  const [youtubeVisibleLimit, setYoutubeVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);
  const [channelBeyondRecent, setChannelBeyondRecent] = useState<
    Partial<Record<'Kick' | 'Twitch' | 'YouTube', boolean>>
  >({});
  const [channelContentFilter, setChannelContentFilter] = useState<'vods' | 'clips' | 'streams'>(
    initialChannelUi.content,
  );

  const youtubePlatformOnly = youtubeEnabled && !kickEnabled && !twitchEnabled;

  const selectedChannel = useMemo(
    () => savedChannels.find((c) => c.id === selectedChannelId) ?? null,
    [savedChannels, selectedChannelId],
  );

  const allChannelVideos = useMemo(() => {
    if (!selectedChannel) return [];
    if (channelContentFilter === 'clips') return selectedChannel.clipVideos ?? [];
    if (channelContentFilter === 'streams') {
      return (selectedChannel.vodVideos ?? []).filter((v) => v.content_kind === 'stream');
    }
    return (selectedChannel.vodVideos ?? []).filter(
      (v) => v.content_kind !== 'stream' && v.content_kind !== 'clip',
    );
  }, [selectedChannel, channelContentFilter]);

  const kickChannelVideos = useMemo(
    () => allChannelVideos.filter((v) => v.platform === 'Kick'),
    [allChannelVideos],
  );
  const twitchChannelVideos = useMemo(
    () => allChannelVideos.filter((v) => v.platform === 'Twitch'),
    [allChannelVideos],
  );
  const youtubeChannelVideos = useMemo(
    () => allChannelVideos.filter((v) => v.platform === 'YouTube'),
    [allChannelVideos],
  );

  const channelsLoading = selectedChannel?.loading ?? false;

  const channelHasKick = Boolean(selectedChannel?.kickSlug?.trim());
  const channelHasTwitch = Boolean(selectedChannel?.twitchSlug?.trim());
  const channelHasYoutube = Boolean(selectedChannel?.youtubeSlug?.trim());

  const visibleChannelVideos = useMemo(() => {
    const clips = channelContentFilter === 'clips';
    const items: ChannelVideo[] = [];
    if (kickEnabled && channelHasKick) {
      items.push(...channelPlatformVisibleSlice(
        kickChannelVideos,
        kickVisibleLimit,
        channelBeyondRecent.Kick ?? false,
        clips,
      ));
    }
    if (twitchEnabled && channelHasTwitch) {
      items.push(...channelPlatformVisibleSlice(
        twitchChannelVideos,
        twitchVisibleLimit,
        channelBeyondRecent.Twitch ?? false,
        clips,
      ));
    }
    if (youtubeEnabled && channelHasYoutube) {
      items.push(...channelPlatformVisibleSlice(
        youtubeChannelVideos,
        youtubeVisibleLimit,
        channelBeyondRecent.YouTube ?? false,
        clips,
      ));
    }
    const sorted = [...items].sort((a, b) => {
      const ta = parseVideoTs(a.created_at);
      const tb = parseVideoTs(b.created_at);
      // Null/empty dates sort to end
      if (ta === 0 && tb === 0) return 0;
      if (ta === 0) return 1;
      if (tb === 0) return -1;
      return tb - ta; // newest first
    });
    let kickN = 0;
    let twitchN = 0;
    let youtubeN = 0;
    return sorted.map((v): ListedChannelVideo => ({
      ...v,
      platformListIndex: v.platform === 'Kick'
        ? ++kickN
        : v.platform === 'Twitch'
          ? ++twitchN
          : ++youtubeN,
    }));
  }, [
    kickChannelVideos,
    twitchChannelVideos,
    youtubeChannelVideos,
    kickEnabled,
    twitchEnabled,
    youtubeEnabled,
    kickVisibleLimit,
    twitchVisibleLimit,
    youtubeVisibleLimit,
    channelContentFilter,
    channelHasKick,
    channelHasTwitch,
    channelHasYoutube,
    channelBeyondRecent,
  ]);

  const bulkDownloadPlatforms = useMemo(() => {
    const platforms = new Set<PlatformStyleKey>();
    for (const v of visibleChannelVideos) {
      if (!selectedChannelVodUrls.has(buildVodUrl(v))) continue;
      const key = platformStyleKey(v.platform);
      if (key) platforms.add(key);
    }
    return platforms;
  }, [visibleChannelVideos, selectedChannelVodUrls]);

  const bulkDownloadPlatform = useMemo((): PlatformStyleKey => {
    if (bulkDownloadPlatforms.size === 1) return [...bulkDownloadPlatforms][0]!;
    return null;
  }, [bulkDownloadPlatforms]);

  const clipsMode = channelContentFilter === 'clips';
  const canExpandKick = kickEnabled && channelHasKick && channelPlatformCanExpand(
    kickChannelVideos, kickVisibleLimit, channelBeyondRecent.Kick ?? false, clipsMode,
  );
  const canExpandTwitch = twitchEnabled && channelHasTwitch && channelPlatformCanExpand(
    twitchChannelVideos, twitchVisibleLimit, channelBeyondRecent.Twitch ?? false, clipsMode,
  );
  const canExpandYoutube = youtubeEnabled && channelHasYoutube && channelPlatformCanExpand(
    youtubeChannelVideos, youtubeVisibleLimit, channelBeyondRecent.YouTube ?? false, clipsMode,
  );
  const canExpandChannelList = canExpandKick || canExpandTwitch || canExpandYoutube;

  const resetChannelListPaging = useCallback(() => {
    setKickVisibleLimit(CHANNEL_INITIAL_VISIBLE);
    setTwitchVisibleLimit(CHANNEL_INITIAL_VISIBLE);
    setYoutubeVisibleLimit(CHANNEL_INITIAL_VISIBLE);
    setChannelBeyondRecent({});
  }, []);

  useEffect(() => {
    resetChannelListPaging();
  }, [selectedChannelId, channelContentFilter, resetChannelListPaging]);

  // Settings
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [settingsSaved, setSettingsSaved] = useState(false);
  const [appVersion, setAppVersion] = useState<string | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [updateChecking, setUpdateChecking] = useState(false);
  const [updateApplying, setUpdateApplying] = useState(false);
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);

  const syncPreviewTimeUi = useCallback((t: number, force = false) => {
    previewCurrentTimeRef.current = t;
    const dur = vodDurationSecRef.current;
    if (previewPlayheadRef.current && dur > 0) {
      previewPlayheadRef.current.style.left = `${(t / dur) * 100}%`;
    }
    const quant = Math.round(t * 4) / 4;
    if (force || quant !== previewTimeUiRef.current) {
      previewTimeUiRef.current = quant;
      setPreviewTimeUi(quant);
    }
  }, []);

  const vodDurationSec = useMemo(() => {
    if (previewMetaDurationSec > 0) return previewMetaDurationSec;
    return videoInfoDurationSec(videoInfo);
  }, [videoInfo, previewMetaDurationSec]);

  useEffect(() => {
    vodDurationSecRef.current = vodDurationSec;
  }, [vodDurationSec]);

  // Keep previewOpenRef in sync
  useEffect(() => {
    previewOpenRef.current = previewOpen;
  }, [previewOpen]);

  useEffect(() => {
    previewVideoLoadingRef.current = previewVideoLoading;
  }, [previewVideoLoading]);

  useEffect(() => {
    previewVideoReadyRef.current = previewVideoReady;
  }, [previewVideoReady]);

  /** Selected clip length in preview (not full VOD duration). */
  const previewClipLengthSec = useMemo(() => {
    if (needleGlance?.dragging) {
      return Math.max(0, needleGlance.rangeEnd - needleGlance.rangeStart);
    }
    return Math.max(0, previewTrimEnd - previewTrimStart);
  }, [previewTrimStart, previewTrimEnd, needleGlance]);

  const postYoutubePreviewCommand = useCallback((func: string, args: unknown[] = []) => {
    youtubeIframeCommand(previewYoutubeIframeRef.current, func, args);
  }, []);

  const destroyPreviewPlayer = useCallback(() => {
    setPreviewYoutubeEmbedUrl(null);
    if (previewSeekDebounceRef.current != null) {
      window.clearTimeout(previewSeekDebounceRef.current);
      previewSeekDebounceRef.current = null;
    }
    if (previewRecoveryTimerRef.current != null) {
      window.clearTimeout(previewRecoveryTimerRef.current);
      previewRecoveryTimerRef.current = null;
    }
    // Invalidate any in-flight seek so its async callbacks become no-ops.
    previewSeekInflightRef.current += 1;
    previewSeekTargetRef.current = null;
    previewSeekLockedRef.current = false;
    previewPendingSeekSecRef.current = null;
    previewBufferingClearRef.current = null;
    const hls = previewHlsRef.current;
    if (hls) {
      try {
        hls.stopLoad();
        hls.detachMedia();
        hls.destroy();
      } catch {
        /* ignore */
      }
      previewHlsRef.current = null;
      setHlsRef(null);
    }
    // Tear down direct-MSE player if it was used for this session.
    msePlayerRef.current?.destroy();
    const video = previewVideoRef.current;
    if (video) {
      detachProgressivePreview(video);
    }
    previewClipRelativeRef.current = false;
    previewTrimTimelineRef.current = false;
    previewWindowHlsMuxStartRef.current = 0;
    previewWindowHlsMuxEndRef.current = 0;
    setPreviewMetaDurationSec(0);
  }, [setHlsRef]);

  const resetPreview = useCallback(async () => {
    previewGenRef.current += 1; // cancel any in-flight openPreview
    previewStartedRef.current = false;
    previewLoadedUrlRef.current = null;
    const sid = previewSessionId;
    destroyPreviewPlayer();
    setPreviewOpen(false);
    setPreviewSessionId(null);
    setPreviewPlayback(null);
    setPreviewYoutubeEmbedUrl(null);
    setPreviewVideoLoading(false);
    setPreviewVideoReady(false);
    previewCurrentTimeRef.current = 0;
    previewTimeUiRef.current = 0;
    setPreviewTimeUi(0);
    setPreviewPlaying(false);
    setPreviewFullscreen(false);
    setTrimPanelHeight(0);
    setPreviewLevels([]);
    setPreviewQualityLevel(0);
    setPreviewQualityMenuOpen(false);
    setPreviewVolumeMenuOpen(false);
    previewSessionMetaRef.current = null;
    previewSessionIdRef.current = null;
    previewRequestedHeightRef.current = 0;
    previewAppliedHeightRef.current = 0;
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    if (sid) {
      try { await apiDelete(`/api/preview/session/${sid}`); } catch { /* ignore */ }
    }
  }, [previewSessionId, destroyPreviewPlayer]);

  const seekPreviewVideoImmediate = useCallback((sec: number, force = false) => {
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const t = Math.max(start, Math.min(sec, end));
    if (previewYoutubeEmbedUrl) {
      if (!previewVideoReady) return;
      // Keep this target until YouTube reports the new position. The iframe
      // emits its old time briefly after seekTo; accepting it makes the
      // controlled scrubber jump backwards.
      previewSeekTargetRef.current = t;
      previewTimingRef.current?.markSeekStart(t);
      syncPreviewTimeUi(t, true);
      postYoutubePreviewCommand('seekTo', [t, true]);
      return;
    }
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    previewSeekTargetRef.current = t;
    previewTimingRef.current?.markSeekStart(t);
    const pageUrl = previewLoadedUrlRef.current ?? url.trim();
    const youtube = detectUrlPlatform(pageUrl) === 'youtube';
    const optimistic = previewSeekOptimisticUi(
      youtube,
      previewTrimTimelineRef.current,
      previewPlaybackKindRef.current,
    );
    const finishSeek = () => {
      previewSeekTargetRef.current = null;
      syncPreviewTimeUi(t, true);
    };
    const applyLocalTime = (videoTime: number) => {
      if (force || Math.abs(video.currentTime - videoTime) > 0.05) {
        video.currentTime = videoTime;
      }
      if (optimistic) syncPreviewTimeUi(t, true);
    };

    const sid = previewSessionIdRef.current;
    if (
      previewTrimTimelineRef.current
      && sid
      && youtube
    ) {
      // Invalidate any previous seek before starting the next one so callbacks
      // for the old one become no-ops and cannot leak the timeline lock.
      const seekId = ++previewSeekInflightRef.current;
      const clearLockIfCurrent = () => {
        if (seekId === previewSeekInflightRef.current) {
          previewSeekLockedRef.current = false;
          setPreviewBuffering(false);
        }
      };
      const muxStart = previewWindowHlsMuxStartRef.current;
      const muxEnd = previewWindowHlsMuxEndRef.current;
      const resumePlay = !video.paused;
      if (isPositionInWindowHlsMux(t, muxStart, muxEnd)) {
        previewSeekLockedRef.current = true;
        // The slider already jumped optimistically in seekPreviewVideo.
        // applyVideoLocalSeek pauses during the seek so the decoder does not
        // play forward from the previous keyframe to the target.
        void applyVideoLocalSeek(video, windowHlsVideoTimeSec(t, muxStart))
          .then(() => {
            if (seekId !== previewSeekInflightRef.current) return;
            previewSeekLockedRef.current = false;
            finishSeek();
            previewBufferingClearRef.current?.();
            setPreviewBuffering(false);
            if (resumePlay) void video.play().then(() => setPreviewPlaying(true)).catch(() => {});
          })
          .catch(() => {
            if (seekId !== previewSeekInflightRef.current) return;
            previewSeekTargetRef.current = null;
            clearLockIfCurrent();
          });
        return;
      }
      // MSE-direct: out-of-window seek → tell the MSE player to remux+seek.
      if (USE_MSE_DIRECT && msePlayerRef.current) {
        previewSeekLockedRef.current = true;
        video.pause();
        setPreviewPlaying(false);
        setPreviewBuffering(true);
        void msePlayerRef.current.seek(t).then(() => {
          if (seekId !== previewSeekInflightRef.current) return;
          previewSeekLockedRef.current = false;
          finishSeek();
          previewBufferingClearRef.current?.();
          waitVideoPlayable(
            video,
            previewTimingRef.current ?? new PreviewTiming("youtube", "main"),
          );
          if (resumePlay)
            void video.play().then(() => setPreviewPlaying(true)).catch(() => {});
        }).catch(() => {
          if (seekId === previewSeekInflightRef.current) {
            setError("MSE seek failed");
            previewSeekTargetRef.current = null;
          }
        });
        return;
      }
      // Out-of-window seek: keep the slider at the target (already set
      // optimistically) and wait for the backend remux. Do not touch
      // video.currentTime until the new chunk is ready — the old window does
      // not contain the target, so any local seek would snap to the wrong frame.
      previewSeekLockedRef.current = true;
      video.pause();
      setPreviewPlaying(false);
      shieldPreviewBuffering(120_000);
      // Show loading immediately so the user knows the requested frame is being
      // prepared while the backend remuxes.
      setPreviewBuffering(true);
      let slowSpinner: number | undefined;
      void (async () => {
        try {
          slowSpinner = window.setTimeout(() => setPreviewBuffering(true), 800);
          const { muxStart: newStart, muxEnd: newEnd, remuxed } = await seekYoutubeWindowHls(sid, t, apiPost, apiGet, 12_000);
          if (seekId !== previewSeekInflightRef.current) return;
          previewWindowHlsMuxStartRef.current = newStart;
          previewWindowHlsMuxEndRef.current = newEnd;
          const videoTime = windowHlsVideoTimeSec(t, newStart);
          if (remuxed && previewHlsRef.current) {
            await reloadWindowHlsAtPosition(
              previewHlsRef.current,
              sid,
              video,
              videoTime,
            );
          } else {
            await applyVideoLocalSeek(video, videoTime);
          }
          if (seekId !== previewSeekInflightRef.current) return;
          previewSeekLockedRef.current = false;
          finishSeek();
          previewBufferingClearRef.current?.();
          waitVideoPlayable(video, previewTimingRef.current ?? new PreviewTiming('youtube', 'main'));
          if (resumePlay) void video.play().then(() => setPreviewPlaying(true)).catch(() => {});
        } catch (err: unknown) {
          if (seekId === previewSeekInflightRef.current) {
            setError(err instanceof Error ? err.message : 'Seek failed');
            previewSeekTargetRef.current = null;
          }
        } finally {
          if (slowSpinner !== undefined) window.clearTimeout(slowSpinner);
          clearLockIfCurrent();
        }
      })();
      return;
    }

    if (
      youtube
      && !previewTrimTimelineRef.current
      && previewPlaybackKindRef.current === 'progressive'
      && !previewCachedProgressiveRef.current
      && sid
      && t > start + 60
    ) {
      const clipRel = previewClipRelativeRef.current;
      const videoTime = clipRel ? Math.max(0, Math.min(t - start, end - start)) : t;
      // Show a teaser frame at the target immediately while /refresh resolves
      // the full-window progressive URL in the background.
      applyLocalTime(videoTime);
      previewPendingSeekSecRef.current = t;
      setPreviewBuffering(true);
      void apiPost<PreviewSessionResponse>(`/api/preview/session/${sid}/refresh`, {})
        .then((res) => {
          if (applyPreviewSessionRefresh(res)) {
            setPreviewBuffering(false);
            return;
          }
          const clipRel = previewClipRelativeRef.current;
          const videoTime = clipRel ? Math.max(0, Math.min(t - start, end - start)) : t;
          applyLocalTime(videoTime);
          waitVideoPlayable(
            video,
            previewTimingRef.current ?? new PreviewTiming(youtube ? 'youtube' : 'unknown', 'main'),
          );
          finishSeek();
        })
        .catch(() => {
          const clipRel = previewClipRelativeRef.current;
          const videoTime = clipRel ? Math.max(0, Math.min(t - start, end - start)) : t;
          applyLocalTime(videoTime);
          waitVideoPlayable(
            video,
            previewTimingRef.current ?? new PreviewTiming(youtube ? 'youtube' : 'unknown', 'main'),
          );
          finishSeek();
        })
        .finally(() => setPreviewBuffering(false));
      return;
    }

    const clipRel = previewClipRelativeRef.current;
    const videoTime = clipRel ? Math.max(0, Math.min(t - start, end - start)) : t;
    applyLocalTime(videoTime);
    const plat = detectUrlPlatform(pageUrl) ?? 'unknown';
    waitVideoPlayable(
      video,
      previewTimingRef.current ?? new PreviewTiming(plat, 'main'),
    );
    finishSeek();
  }, [previewYoutubeEmbedUrl, previewVideoReady, syncPreviewTimeUi, url, applyPreviewSessionRefresh, postYoutubePreviewCommand]);

  const seekPreviewVideo = useCallback((sec: number, force = false) => {
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const clamped = Math.max(start, Math.min(sec, end));
    previewSeekTargetRef.current = clamped;
    const pageUrl = previewLoadedUrlRef.current ?? url.trim();
    if (previewSeekOptimisticUi(
      detectUrlPlatform(pageUrl) === 'youtube',
      previewTrimTimelineRef.current,
      previewPlaybackKindRef.current,
    )) {
      syncPreviewTimeUi(clamped, true);
    }
    if (force) {
      if (previewSeekDebounceRef.current != null) {
        window.clearTimeout(previewSeekDebounceRef.current);
        previewSeekDebounceRef.current = null;
      }
      seekPreviewVideoImmediate(sec, true);
      return;
    }
    if (previewSeekDebounceRef.current != null) {
      window.clearTimeout(previewSeekDebounceRef.current);
    }
    previewSeekDebounceRef.current = window.setTimeout(() => {
      previewSeekDebounceRef.current = null;
      seekPreviewVideoImmediate(sec, false);
    }, PREVIEW_SEEK_DEBOUNCE_MS);
  }, [seekPreviewVideoImmediate]);

  const openPreview = useCallback(async () => {
    if (!url.trim()) return;
    if (trimEndSec <= trimStartSec) return;
    const trimmedUrl = url.trim();
    // Already showing this URL — no-op unless playback failed and user is retrying
    if (
      previewStartedRef.current
      && previewLoadedUrlRef.current === trimmedUrl
      && previewOpenRef.current
      && (previewVideoReadyRef.current || previewVideoLoadingRef.current)
    ) return;
    previewStartedRef.current = true;
    youtubePrefetchGenRef.current += 1;
    const pagePlatform = detectUrlPlatform(trimmedUrl) ?? 'unknown';
    const timing = new PreviewTiming(pagePlatform, 'main');
    previewTimingRef.current = timing;
    timing.markOpen(trimmedUrl.slice(0, 80));

    // Cancel any previously in-flight openPreview
    const gen = ++previewGenRef.current;
    const bailIfSuperseded = () => {
      if (gen !== previewGenRef.current) {
        if (!previewOpenRef.current) setPreviewVideoLoading(false);
        return true;
      }
      return false;
    };
    let start = trimStartSecRef.current;
    let end = trimEndSecRef.current;
    const clipPreview = isClipUrl(trimmedUrl);
    const youtubePreview = detectUrlPlatform(trimmedUrl) === 'youtube';
    if (youtubePreview && videoInfoDurationSec(videoInfo) <= 0) {
      void apiGet<VideoInfo>(`/api/info/video?id=${encodeURIComponent(trimmedUrl)}`)
        .then((info) => {
          if (gen !== previewGenRef.current) return;
          const dur = videoInfoDurationSec(info);
          if (dur > 0) {
            setVideoInfo(info);
            setPreviewMetaDurationSec(dur);
            if (trimEndSecRef.current === 3600) {
              trimStartSecRef.current = 0;
              trimEndSecRef.current = dur;
              previewTrimStartRef.current = 0;
              previewTrimEndRef.current = dur;
              setTrimStartSec(0);
              setTrimEndSec(dur);
              setPreviewTrimStart(0);
              setPreviewTrimEnd(dur);
            }
          }
        })
        .catch(() => {});
    }
    // Preview window follows trim range (full VOD when sliders span entire duration).
    previewTrimStartRef.current = start;
    previewTrimEndRef.current = end;
    setPreviewTrimStart(start);
    setPreviewTrimEnd(end);
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    previewVolumeRef.current = PREVIEW_DEFAULT_VOLUME;
    setPreviewVolume(PREVIEW_DEFAULT_VOLUME);
    setPreviewMuted(false);
    const initialAspect = clipPreview ? 9 / 16 : PREVIEW_VIDEO_ASPECT_DEFAULT;
    previewVideoAspectRef.current = initialAspect;
    setPreviewVideoAspect(initialAspect);
    const clampedPreviewW = clampPreviewPanelWidth(
      previewPanelWidthRef.current,
      previewChromeHRef.current,
      initialAspect,
      {
        previewOpen: true,
        urlPanelAside: true,
        preview: { w: previewPanelWidthRef.current, h: 0 },
        urlAside: urlAsidePanelSizeRef.current,
        main: mainPanelSizeRef.current,
      },
    );
    previewPanelWidthRef.current = clampedPreviewW;
    setPreviewPanelWidth(clampedPreviewW);
    setPreviewOpen(true);
    setPreviewPlayback(null);
    setPreviewVideoLoading(true);
    setPreviewVideoReady(false);
    syncPreviewTimeUi(start, true);
    setError(null);
    // YouTube deliberately uses the app's proxied <video>/HLS pipeline too.
    // An iframe can display native YouTube overlays regardless of controls=0 and
    // has unreliable desktop fullscreen, so it must never be the preview surface.
    try {
      const oldSid = previewSessionId;
      destroyPreviewPlayer();
      if (oldSid) {
        try { await apiDelete(`/api/preview/session/${oldSid}`); } catch { /* ignore */ }
      }
      const playerCap = measurePlayerHeightCap(
        previewContainerRef.current ?? previewPanelRef.current,
        previewVideoAspectRef.current,
      );
      const previewPreferHeight = initialPreviewPreferHeight(clipPreview, playerCap, {
        youtube: youtubePreview,
        variantHeights: videoInfo?.qualities
          ? parseQualityHeights(videoInfo.qualities)
          : undefined,
      });
      let qualityLabels = videoInfo?.qualities;
      const prefetched =
        previewSessionPrefetchRef.current?.url === trimmedUrl
          ? previewSessionPrefetchRef.current.session
          : null;
      if (prefetched) previewSessionPrefetchRef.current = null;
      const res = prefetched ?? await apiPost<PreviewSessionResponse>('/api/preview/session', {
        url: trimmedUrl,
        crop_start: start,
        crop_end: end,
        prefer_height: previewPreferHeight,
      });
      if (bailIfSuperseded()) return;
      timing.setSessionId(res.session_id);
      timing.mark('session_ready', `kind=${res.kind} trim=${res.trim_timeline === true}`);
      previewExtractSourceRef.current = res.extract_source ?? '';
      if (previewExtractSourceRef.current) {
        console.info('[VOD.RIP preview] extract_source=', previewExtractSourceRef.current);
      }
      const clipInfo = clipPreview && !qualityLabels?.length
        ? await apiGet<VideoInfo>(`/api/info/clip?id=${encodeURIComponent(trimmedUrl)}`).catch(() => null)
        : null;
      if (bailIfSuperseded()) return;
      if (clipInfo?.qualities?.length) {
        qualityLabels = clipInfo.qualities;
      }
      const mergedQualityLabels = qualityLabels?.length
        ? qualityLabels
        : (res.quality_labels?.length ? res.quality_labels : undefined);
      const activeHeight = res.active_height ?? previewPreferHeight;
      previewSessionMetaRef.current = {
        variantHeights: res.variant_heights ?? [],
        qualityLabels: mergedQualityLabels,
        activeHeight,
      };
      previewSessionIdRef.current = res.session_id;
      setPreviewSessionId(res.session_id);
      previewTrimTimelineRef.current = res.trim_timeline === true;
      previewWindowHlsMuxStartRef.current = res.window_hls_mux_start ?? 0;
      previewWindowHlsMuxEndRef.current = res.window_hls_mux_end ?? 0;
      previewCachedProgressiveRef.current = res.cached_progressive === true;
      const synced = syncDurationFromPreviewSession(res.duration_sec, start, end);
      if (synced) {
        start = synced.start;
        end = synced.end;
        previewTrimStartRef.current = start;
        previewTrimEndRef.current = end;
        setPreviewTrimStart(start);
        setPreviewTrimEnd(end);
        trimStartSecRef.current = start;
        trimEndSecRef.current = end;
        setTrimStartSec(start);
        setTrimEndSec(end);
        setPreviewMetaDurationSec(synced.duration);
      }
      const playback = resolvePreviewPlayback(url.trim(), res);
      if (youtubePreview && (res.trim_timeline || !res.segment_buffer_ready)) {
        timing.mark('attach_before_segments');
      }
      setPreviewPlayback({
        ...playback,
        variantHeights: res.variant_heights ?? [],
        qualityLabels: mergedQualityLabels,
        activeHeight,
      });
      previewLoadedUrlRef.current = trimmedUrl;
    } catch (err: any) {
      previewStartedRef.current = false;
      previewLoadedUrlRef.current = null;
      setError(err.message || 'Preview failed');
      setPreviewOpen(false);
      setPreviewVideoLoading(false);
    }
  }, [url, trimEndSec, trimStartSec, vodDurationSec, previewSessionId, destroyPreviewPlayer, videoInfo, videoInfo?.qualities, videoInfo?.title]);

  // Warm YouTube extract cache while user reads the page (no UI update until Extract Info).
  useEffect(() => {
    const trimmed = url.trim();
    if (!trimmed || videoInfo?.title || loading) return;
    if (detectUrlPlatform(trimmed) !== 'youtube') return;
    const gen = ++youtubePrefetchGenRef.current;
    const timer = window.setTimeout(() => {
      if (gen !== youtubePrefetchGenRef.current) return;
      warmYoutubePreview(trimmed, 0);
    }, 450);
    return () => window.clearTimeout(timer);
  }, [url, videoInfo?.title, loading]);

  // Channel list: warm first YouTube rows + IntersectionObserver on scroll.
  useEffect(() => {
    if (tab !== 'channels' || !selectedChannelId || !youtubeEnabled) return;
    // ponytail: defer batch warm while user is on a Twitch URL — frees INFO_EXECUTOR for Twitch info/preview.
    if (detectUrlPlatform(url.trim()) === 'twitch') return;
    const root = channelsScrollRef.current;
    if (!root) return;

    const youtubeUrls = visibleChannelVideos
      .filter((v) => v.platform === 'youtube')
      .map((v) => buildVodUrl(v));
    warmYoutubePreviewBatch(youtubeUrls, 3, 120);

    let cleanup: (() => void) | undefined;
    const raf = requestAnimationFrame(() => {
      const rows = Array.from(root.querySelectorAll<HTMLElement>('[data-youtube-warm]'));
      cleanup = bindYoutubeChannelScrollWarm(root, rows);
    });
    return () => {
      cancelAnimationFrame(raf);
      cleanup?.();
    };
  }, [tab, selectedChannelId, youtubeEnabled, visibleChannelVideos, url]);

  useEffect(() => {
    if (!previewOpen || !previewPlayback?.url) return;
    const previewPageUrl = previewLoadedUrlRef.current ?? url.trim();
    const youtubePreview = detectUrlPlatform(previewPageUrl) === 'youtube';
    let cancelled = false;
    let cleanup: (() => void) | undefined;
    let detachBuffering: (() => void) | undefined;

    const setup = () => {
      if (cancelled) return;
      const video = previewVideoRef.current;
      if (!video) {
        requestAnimationFrame(setup);
        return;
      }
      const bufferingHandle = attachPreviewBufferingListeners(video, (stalling) => {
        if (!cancelled) setPreviewBuffering(stalling);
      });
      previewBufferingClearRef.current = bufferingHandle.clearStall;
      detachBuffering = bufferingHandle.detach;
      const { url: playbackUrl, kind: playbackKind } = previewPlayback;

    setPreviewVideoLoading(true);
    setPreviewBuffering(false);
    setPreviewVideoReady(false);

    const performInitialSeek = () => {
      if (previewInitialSeekDoneRef.current) return;
      previewInitialSeekDoneRef.current = true;
      const start = previewTrimStartRef.current;
      const end = previewTrimEndRef.current;
      const clipRel = previewClipRelativeRef.current;
      const dashSegTimeline = previewTrimTimelineRef.current;
      let target = clipRel ? 0 : start;
      if (dashSegTimeline) {
        target = windowHlsVideoTimeSec(start, previewWindowHlsMuxStartRef.current);
      }
      if (Number.isFinite(target) && Math.abs(video.currentTime - target) > 0.25) {
        video.currentTime = target;
      }
      const vodT = dashSegTimeline
        ? previewWindowHlsMuxStartRef.current + video.currentTime
        : clipRel
          ? start + video.currentTime
          : Math.max(start, Math.min(video.currentTime, end));
      syncPreviewTimeUi(vodT, true);
    };

    const onCanPlay = () => {
      setPreviewVideoReady(true);
      setPreviewBuffering(false);
      setPreviewVideoLoading(false);
      previewTimingRef.current?.mark('canplay');
      video.volume = PREVIEW_DEFAULT_VOLUME;
      previewVolumeRef.current = PREVIEW_DEFAULT_VOLUME;
      setPreviewVolume(PREVIEW_DEFAULT_VOLUME);
      video.muted = false;
      setPreviewMuted(false);
      performInitialSeek();
      if (!previewInitialPlayDoneRef.current && video.paused) {
        previewInitialPlayDoneRef.current = true;
        void video.play().catch(() => {
          video.muted = true;
          setPreviewMuted(true);
          void video.play().catch(() => {});
        });
      }
      if (video.readyState >= 3 && !video.paused && video.currentTime > 0.02) {
        previewTimingRef.current?.markFirstPlayable('canplay_already_playing');
      }
    };

    const clearStallUi = () => {
      if (cancelled) return;
      setPreviewVideoLoading(false);
      setPreviewBuffering(false);
    };
    video.addEventListener('playing', clearStallUi);
    const onFirstPlaying = () => {
      previewTimingRef.current?.markFirstPlayable();
    };
    video.addEventListener('playing', onFirstPlaying, { once: true });

    if (playbackKind === 'progressive' || isClipPreviewUrl(previewPageUrl)) {
      const meta = previewSessionMetaRef.current;
      const activeH = meta?.activeHeight
        ?? previewPlayback.activeHeight
        ?? PREVIEW_CLIP_DEFAULT_HEIGHT;
      const syncProgressiveLevels = (
        mapped: PreviewLevelOption[],
        defaultIndex: number,
      ) => {
        if (cancelled) return;
        setPreviewLevels(mapped);
        setPreviewQualityLevel(defaultIndex);
        const picked = mapped[defaultIndex];
        if (picked?.height) previewRequestedHeightRef.current = picked.height;
      };
      const levelOpts = {
        variantHeights: meta?.variantHeights ?? previewPlayback.variantHeights,
        qualityLabels: meta?.qualityLabels
          ?? previewPlayback.qualityLabels
          ?? videoInfo?.qualities,
        initialHeight: activeH,
        allowHeights: detectUrlPlatform(previewPageUrl) === 'youtube' ? YOUTUBE_PREVIEW_ALLOW_HEIGHTS : undefined,
      };
      const immediate = resolveProgressivePreviewLevels(levelOpts);
      syncProgressiveLevels(immediate.mapped, immediate.defaultIndex);
      void resolveProgressivePreviewLevelsAsync(
        previewPageUrl,
        levelOpts,
        async (clipUrl) => {
          const clipInfo = await apiGet<VideoInfo>(
            `/api/info/clip?id=${encodeURIComponent(clipUrl)}`,
          );
          return clipInfo.qualities;
        },
      ).then(({ mapped, defaultIndex, qualityLabels: resolvedLabels }) => {
        if (resolvedLabels?.length && meta) {
          previewSessionMetaRef.current = {
            ...meta,
            qualityLabels: resolvedLabels,
          };
        }
        if (mapped.length !== immediate.mapped.length) {
          syncProgressiveLevels(mapped, defaultIndex);
        }
      }).catch(() => { /* keep immediate levels */ });
      previewAppliedHeightRef.current = activeH;
      const syncClipRelative = () => {
        const start = previewTrimStartRef.current;
        const end = previewTrimEndRef.current;
        previewClipRelativeRef.current = isClipRelativePreviewDuration(
          video.duration,
          vodDurationSecRef.current,
          end - start,
        );
      };
      const onLoadedMeta = () => {
        syncClipRelative();
        handlePreviewLoadedMetadata();
      };
      attachProgressivePreview(video, playbackUrl);
      const cleanupRecovery = bindProgressivePreviewRecovery({
        video,
        playbackUrl,
        getSessionId: () => previewSessionIdRef.current,
        youtube: youtubePreview,
        extractSource: previewExtractSourceRef.current,
        getResumeSec: () => previewSeekTargetRef.current ?? video.currentTime,
        apiPost,
        onRefreshing: () => setPreviewBuffering(true),
        onFatal: () => {
          setError('Preview interrupted — try again');
          setPreviewVideoLoading(false);
        },
        onSessionRefresh: (res) => {
          previewPendingSeekSecRef.current = previewSeekTargetRef.current ?? video.currentTime;
          const ok = applyPreviewSessionRefresh(res as PreviewSessionResponse);
          if (ok) setPreviewBuffering(false);
          return ok;
        },
      });
      video.addEventListener('loadedmetadata', onLoadedMeta, { once: true });
      video.addEventListener('canplay', () => {
        syncClipRelative();
        onCanPlay();
      }, { once: true });
      cleanup = () => {
        video.removeEventListener('loadedmetadata', onLoadedMeta);
        video.removeEventListener('playing', clearStallUi);
        cleanupRecovery();
        detachProgressivePreview(video);
      };
      return;
    }

    // ── Direct MSE path (opt-in, YouTube window-HLS only) ──────────────
    let attachViaHls: () => void;
    if (
      USE_MSE_DIRECT &&
      youtubePreview &&
      previewTrimTimelineRef.current &&
      Hls.isSupported()
    ) {
      const sid = previewSessionIdRef.current;
      if (!sid) {
        setError("Preview session missing");
        setPreviewVideoLoading(false);
        return;
      }
      const mse = msePlayerRef.current;
      if (!mse) {
        setError("MSE player unavailable");
        setPreviewVideoLoading(false);
        return;
      }
      mse.attach(sid)
        .then(() => {
          if (cancelled) return;
          // onCanPlay-equivalent: MSE ready → video fires canplay once buffered.
          video.addEventListener(
            "canplay",
            () => {
              if (cancelled) return;
              onCanPlay();
            },
            { once: true },
          );
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          console.error("[MSE] attach failed, falling back to hls.js:", err);
          // Fall back to hls.js for this session.
          attachViaHls();
        });
      // Expose seek override for window-HLS remux via MSE.
      cleanup = () => {
        mse.destroy();
      };
      return;
    }

    if (Hls.isSupported()) {
      attachViaHls = () => {
      const dashSegTimeline = previewTrimTimelineRef.current;
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 12,
        // Play-first: start playback once ~6 s are buffered instead of waiting
        // for 20 s. Window-HLS keeps a larger buffer because the chunk is muxed.
        maxBufferLength: dashSegTimeline ? 60 : 6,
        maxMaxBufferLength: dashSegTimeline ? 180 : 12,
        startFragPrefetch: true,
        capLevelToPlayerSize: !youtubePreview,
        fragLoadingTimeOut: dashSegTimeline ? 90000 : 20000,
        manifestLoadingTimeOut: 10000,
        testBandwidth: false,
        startPosition: dashSegTimeline
          ? 0
          : (previewPendingSeekSecRef.current ?? previewTrimStartRef.current),
      });
      previewHlsRef.current = hls;
      setHlsRef(hls);
      hls.attachMedia(video);
      let networkRetries = 0;
      let urlRefreshTried = false;
      const sid = previewSessionId;
      const loadPlayback = () => {
        if (cancelled) return;
        hls.loadSource(playbackUrl);
      };
      requestAnimationFrame(() => requestAnimationFrame(loadPlayback));
      let levelsInitialized = false;
      let maxMenuHeight = 0;
      const playerCap = measurePlayerHeightCap(
        previewContainerRef.current ?? previewPanelRef.current,
        previewVideoAspectRef.current,
      );
      const fallbackHeights = mergeVariantHeights(
        previewPlayback.variantHeights,
        parseQualityHeights(videoInfo?.qualities ?? []),
      );
      const meta = previewSessionMetaRef.current;
      const initialHlsHeight = resolveInitialHlsPreviewHeight(
        isClipUrl(previewPageUrl),
        playerCap,
        {
          youtube: youtubePreview,
          variantHeights: fallbackHeights,
          activeHeight: meta?.activeHeight ?? previewPlayback.activeHeight,
        },
      );
      const syncPreviewLevels = (levels = hls.levels, applyDefault = false) => {
        const { mapped, defaultIndex } = resolveHlsPreviewLevels(levels, {
          initialHeight: initialHlsHeight,
          fallbackHeights,
        });
        if (!mapped.length) return;
        const maxH = Math.max(0, ...mapped.map((m) => m.height));
        const grew = maxH > maxMenuHeight;
        if (grew) maxMenuHeight = maxH;
        setPreviewLevels(mapped);
        if (!levelsInitialized || applyDefault || grew) {
          levelsInitialized = true;
          const hlsIndex = mapped[defaultIndex]?.index ?? defaultIndex;
          if (hls.levels.length > 0 && hlsIndex >= 0 && hlsIndex < hls.levels.length) {
            hls.loadLevel = hlsIndex;
          }
          syncPreviewHlsLevels(mapped, defaultIndex);
          const picked = mapped[defaultIndex];
          if (picked?.height) previewAppliedHeightRef.current = picked.height;
        }
      };

      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        syncPreviewLevels(data.levels ?? hls.levels, true);
        const pending = previewPendingSeekSecRef.current;
        if (pending != null && pending > 0 && !previewTrimTimelineRef.current) {
          previewPendingSeekSecRef.current = null;
          previewSeekTargetRef.current = null;
          syncPreviewTimeUi(pending, true);
          hls.startLoad(pending);
        }
        if (previewTrimTimelineRef.current) {
          const start = previewTrimStartRef.current;
          const end = previewTrimEndRef.current;
          previewClipRelativeRef.current = isClipRelativePreviewDuration(
            vodDurationSecRef.current,
            vodDurationSecRef.current,
            end - start,
          );
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
            if (networkRetries < 2) {
              networkRetries += 1;
              window.setTimeout(() => {
                if (!cancelled) hls.startLoad();
              }, networkRetries * 500);
              break;
            }
            if (youtubePreview && sid && !urlRefreshTried) {
              urlRefreshTried = true;
              networkRetries = 0;
              void apiPost(`/api/preview/session/${sid}/refresh`, {})
                .then(() => {
                  if (cancelled) return;
                  hls.loadSource(playbackUrl);
                  hls.startLoad();
                })
                .catch(() => {
                  setError('Preview playback failed — try again');
                  setPreviewVideoLoading(false);
                  previewStartedRef.current = false;
                  hls.destroy();
                  previewHlsRef.current = null;
                });
              break;
            }
            setError('Preview playback failed — try again');
            setPreviewVideoLoading(false);
            previewStartedRef.current = false;
            hls.destroy();
            previewHlsRef.current = null;
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError();
            break;
          default:
            setError('Preview playback failed — try again');
            setPreviewVideoLoading(false);
            previewStartedRef.current = false;
            hls.destroy();
            previewHlsRef.current = null;
            break;
        }
      });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeEventListener('playing', clearStallUi);
        try {
          hls.stopLoad();
          hls.detachMedia();
          hls.destroy();
        } catch {
          /* ignore */
        }
        previewHlsRef.current = null;
      };
      return;
      };
      attachViaHls();
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
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
    setPreviewVideoLoading(false);
    };

    setup();
    return () => {
      cancelled = true;
      previewBufferingClearRef.current = null;
      detachBuffering?.();
      cleanup?.();
    };
  }, [previewOpen, previewPlayback, previewSessionId]);

  useEffect(() => {
    if (!previewYoutubeEmbedUrl) return;
    const onMessage = (event: MessageEvent) => {
      // YouTube's IFrame API delivers `infoDelivery` events with `currentTime`
      // via postMessage. The safe way to filter them is by comparing
      // `event.source` against the iframe's contentWindow — checking origin
      // alone is unreliable because YouTube occasionally proxies through
      // related origins.
      const iframe = previewYoutubeIframeRef.current;
      if (!iframe || event.source !== iframe.contentWindow) {
        // Fall back: also accept messages whose origin is the embed origin.
        if (event.origin !== 'https://www.youtube.com') return;
      }
      let data: any;
      try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; } catch { return; }
      if (!data || data.event !== 'infoDelivery') return;
      const state = Number(data?.info?.playerState);
      if (state === 1) setPreviewPlaying(true);
      else if (state === 2 || state === 0) setPreviewPlaying(false);
      const t = Number(data?.info?.currentTime);
      if (!Number.isFinite(t)) return;
      const target = previewSeekTargetRef.current;
      if (target != null) {
        if (Math.abs(t - target) > 1.5) return;
        previewSeekTargetRef.current = null;
      }
      const start = previewTrimStartRef.current;
      const end = previewTrimEndRef.current;
      if (t < start - 0.5) {
        postYoutubePreviewCommand('seekTo', [start, true]);
        syncPreviewTimeUi(start, true);
        return;
      }
      if (t >= end - 0.05) {
        postYoutubePreviewCommand('pauseVideo');
        syncPreviewTimeUi(end, true);
        setPreviewPlaying(false);
      } else {
        syncPreviewTimeUi(Math.max(start, t));
      }
    };
    youtubeIframeListen(previewYoutubeIframeRef.current);
    const poll = window.setInterval(() => {
      youtubeIframeListen(previewYoutubeIframeRef.current);
      postYoutubePreviewCommand('getCurrentTime');
      postYoutubePreviewCommand('getPlayerState');
    }, 250);
    window.addEventListener('message', onMessage);
    return () => {
      window.clearInterval(poll);
      window.removeEventListener('message', onMessage);
    };
  }, [previewYoutubeEmbedUrl, postYoutubePreviewCommand, syncPreviewTimeUi]);

    const handlePreviewTimeUpdate = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video) return;
    // During an out-of-chunk remux the HLS loader briefly reports positions
    // near the new chunk's mux start while we wait for FRAG_BUFFERED to land
    // the explicit seek. Ignore those reports so the slider doesn't bounce.
    if (previewSeekLockedRef.current) return;
    // While a user seek is in flight (optimistic UI already shows the target),
    // ignore timeupdate reports at the old position. Otherwise the controlled
    // slider snaps back before the debounced seek fires on the first drag.
    if (previewSeekTargetRef.current != null) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    if (previewTrimTimelineRef.current) {
      const vodTime = previewWindowHlsMuxStartRef.current + video.currentTime;
      if (vodTime > end - 0.05) {
        video.pause();
        syncPreviewTimeUi(end, true);
        setPreviewPlaying(false);
        return;
      }
      syncPreviewTimeUi(Math.max(start, vodTime));
      // Predictive prefetch: fetch upcoming window-HLS segments in the background.
      prefetchNextSegments(video.currentTime);
      return;
    }
    const clipRel = previewClipRelativeRef.current;
    const { paused, vodTime } = clampPreviewTimeToVodTrim(video, start, end, clipRel);
    syncPreviewTimeUi(vodTime);
    if (paused) {
      syncPreviewTimeUi(end, true);
      setPreviewPlaying(false);
    }
  }, [syncPreviewTimeUi]);

  const togglePreviewPlay = useCallback(() => {
    if (previewYoutubeEmbedUrl) {
      if (!previewVideoReady) return;
      const start = previewTrimStartRef.current;
      const outOfTrim = previewTimeUiRef.current >= previewTrimEndRef.current - 0.1 || previewTimeUiRef.current < start - 0.1;
      if (!previewPlaying) {
        if (outOfTrim) {
          postYoutubePreviewCommand('seekTo', [start, true]);
          syncPreviewTimeUi(start, true);
        }
        postYoutubePreviewCommand('setVolume', [Math.round(previewVolumeRef.current * 100)]);
        postYoutubePreviewCommand(previewMuted ? 'mute' : 'unMute');
        postYoutubePreviewCommand('playVideo');
        setPreviewPlaying(true);
      } else {
        postYoutubePreviewCommand('pauseVideo');
        setPreviewPlaying(false);
      }
      return;
    }
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    if (video.paused) {
      const start = previewTrimStartRef.current;
      const end = previewTrimEndRef.current;
      const clipRel = previewClipRelativeRef.current;
      const clipLen = Math.max(0, end - start);
      if (clipRel) {
        if (video.currentTime >= clipLen - 0.1) {
          video.currentTime = 0;
          syncPreviewTimeUi(start, true);
        }
      } else if (video.currentTime >= end - 0.1 || video.currentTime < start) {
        video.currentTime = start;
        syncPreviewTimeUi(start, true);
      }
      void video.play();
      setPreviewPlaying(true);
    } else {
      video.pause();
      setPreviewPlaying(false);
    }
  }, [previewYoutubeEmbedUrl, previewVideoReady, previewPlaying, postYoutubePreviewCommand, syncPreviewTimeUi]);
;


  const skipPreview = useCallback((deltaSec: number) => {
    if (!previewVideoReady) return;
    const video = previewVideoRef.current;
    if (!video && !previewYoutubeEmbedUrl) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const base = previewYoutubeEmbedUrl
      ? previewTimeUiRef.current
      : (previewTrimTimelineRef.current
        ? previewTimeUiRef.current
        : video!.currentTime);
    const t = Math.max(start, Math.min(end, base + deltaSec));
    seekPreviewVideo(t, true);
  }, [previewVideoReady, previewYoutubeEmbedUrl, seekPreviewVideo]);

  const seekPreviewPercent = useCallback((fraction: number) => {
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const t = start + (end - start) * Math.max(0, Math.min(1, fraction));
    seekPreviewVideo(t);
  }, [seekPreviewVideo]);

  const commitUrlTrimRange = useCallback((
    rawStart: number,
    rawEnd: number,
    opts?: TrimRangeOpts,
  ) => {
    const dur = Math.max(1, vodDurationSec);
    const { start, end } = clampTrimEndpoints(
      rawStart,
      rawEnd,
      dur,
      trimStartSecRef.current,
      trimEndSecRef.current,
      opts,
    );
    trimStartSecRef.current = start;
    trimEndSecRef.current = end;
    setTrimStartSec(start);
    setTrimEndSec(end);
    // Sync preview trim when preview is open
    if (previewOpenRef.current) {
      previewTrimStartRef.current = start;
      previewTrimEndRef.current = end;
      setPreviewTrimStart(start);
      setPreviewTrimEnd(end);
    }
    // Pin endpoints are frozen at pointerdown — updating them during drag shifts the
    // other slider's min/max and makes its thumb appear to move the wrong way.
    return { start, end };
  }, [vodDurationSec]);

  const clampPreviewPlaybackToTrim = useCallback(() => {
    if (!previewVideoReady) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    if (previewYoutubeEmbedUrl) {
      const t = Math.max(start, Math.min(previewTimeUiRef.current, end));
      if (Math.abs(previewTimeUiRef.current - t) > 0.05) {
        postYoutubePreviewCommand('seekTo', [t, true]);
        syncPreviewTimeUi(t, true);
      }
      return;
    }
    const video = previewVideoRef.current;
    if (!video) return;
    let t = video.currentTime;
    if (t < start) t = start;
    else if (t > end) t = end;
    if (Math.abs(video.currentTime - t) > 0.05) {
      video.currentTime = t;
      syncPreviewTimeUi(t, true);
    }
  }, [previewVideoReady, previewYoutubeEmbedUrl, postYoutubePreviewCommand, syncPreviewTimeUi]);

  const commitPreviewTrimRange = useCallback((
    rawStart: number,
    rawEnd: number,
    opts?: TrimRangeOpts,
  ) => {
    const dur = Math.max(1, vodDurationSec);
    const { start, end } = clampTrimEndpoints(
      rawStart,
      rawEnd,
      dur,
      previewTrimStartRef.current,
      previewTrimEndRef.current,
      opts,
    );
    previewTrimStartRef.current = start;
    previewTrimEndRef.current = end;
    setPreviewTrimStart(start);
    setPreviewTrimEnd(end);
    trimStartSecRef.current = start;
    trimEndSecRef.current = end;
    setTrimStartSec(start);
    setTrimEndSec(end);
    if (opts?.seek === 'in') seekPreviewVideo(start, true);
    else if (opts?.seek === 'out') seekPreviewVideo(end, true);
    else clampPreviewPlaybackToTrim();
    return { start, end };
  }, [vodDurationSec, seekPreviewVideo, clampPreviewPlaybackToTrim]);

  const markUrlTrimEndpoint = useCallback((which: 'in' | 'out') => {
    lastUrlTrimEndpointRef.current = which;
    setLastUrlTrimEndpoint(which);
  }, []);

  const markPreviewTrimEndpoint = useCallback((which: 'in' | 'out') => {
    lastPreviewTrimEndpointRef.current = which;
    setLastPreviewTrimEndpoint(which);
  }, []);

  const adjustUrlClipDuration = useCallback((buttonDelta: number) => {
    const dur = Math.max(1, vodDurationSec);
    const which = lastUrlTrimEndpointRef.current;
    const adjusted = adjustTrimEndpointByDelta(
      trimStartSecRef.current,
      trimEndSecRef.current,
      dur,
      which,
      trimButtonDeltaForEndpoint(which, buttonDelta),
    );
    commitUrlTrimRange(adjusted.start, adjusted.end);
  }, [vodDurationSec, commitUrlTrimRange]);

  const adjustPreviewClipDuration = useCallback((buttonDelta: number) => {
    const dur = Math.max(1, vodDurationSec);
    const which = lastPreviewTrimEndpointRef.current;
    const adjusted = adjustTrimEndpointByDelta(
      previewTrimStartRef.current,
      previewTrimEndRef.current,
      dur,
      which,
      trimButtonDeltaForEndpoint(which, buttonDelta),
    );
    commitPreviewTrimRange(adjusted.start, adjusted.end);
  }, [vodDurationSec, commitPreviewTrimRange]);

  const updateNeedleGlance = useCallback((
    which: 'in' | 'out',
    ev: PointerEvent,
    rangeStart: number,
    rangeEnd: number,
    activeSec: number,
    deltaSec: number,
  ) => {
    setNeedleGlance({
      which,
      x: ev.clientX,
      y: ev.clientY,
      sec: activeSec,
      rangeStart,
      rangeEnd,
      deltaSec,
      dragging: true,
    });
  }, []);

  const beginPreviewNeedleDrag = useCallback((
    e: ReactPointerEvent<HTMLElement>,
    which: 'in' | 'out',
  ) => {
    markPreviewTrimEndpoint(which);
    e.preventDefault();
    e.stopPropagation();
    const rail = previewNeedleRailRef.current;
    if (!rail || vodDurationSec <= 0) return;

    const handle = e.currentTarget;
    const pointerId = e.pointerId;
    handle.setPointerCapture(pointerId);
    trimDragActiveRef.current = true;
    if (previewFsHideTimerRef.current) {
      window.clearTimeout(previewFsHideTimerRef.current);
    }
    setPreviewFsControlsVisible(true);

    const fixedStart = previewTrimStartRef.current;
    const fixedEnd = previewTrimEndRef.current;
    const dragOrigin = which === 'in' ? fixedStart : fixedEnd;
    trimDragOriginRef.current = dragOrigin;

    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';

    const xToSec = (clientX: number) => {
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return 0;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return Math.round(frac * vodDurationSec);
    };

    let ended = false;
    const endDrag = () => {
      if (ended) return;
      ended = true;
      trimDragActiveRef.current = false;
      setNeedleGlance(null);
      document.body.style.userSelect = prevUserSelect;
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', onUp);
      handle.removeEventListener('pointercancel', onUp);
      handle.removeEventListener('lostpointercapture', onLostCapture);
      try { handle.releasePointerCapture(pointerId); } catch { /* ignore */ }
    };

    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      const sec = xToSec(ev.clientX);
      const applied = which === 'in'
        ? commitPreviewTrimRange(sec, fixedEnd, { move: 'in', fixedEnd })
        : commitPreviewTrimRange(fixedStart, sec, { move: 'out', fixedStart });
      const activeSec = which === 'in' ? applied.start : applied.end;
      updateNeedleGlance(
        which,
        ev,
        applied.start,
        applied.end,
        activeSec,
        activeSec - dragOrigin,
      );
    };

    const onUp = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      endDrag();
    };

    const onLostCapture = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      endDrag();
    };

    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', onUp);
    handle.addEventListener('pointercancel', onUp);
    handle.addEventListener('lostpointercapture', onLostCapture);
    onMove(e.nativeEvent);
  }, [vodDurationSec, commitPreviewTrimRange, updateNeedleGlance, markPreviewTrimEndpoint]);

  const finishUrlTrimDrag = useCallback(() => {
    urlTrimDragPinRef.current = null;
    trimDragActiveRef.current = false;
    setNeedleGlance(null);
  }, []);

  const handleUrlTrimSlider = useCallback((
    which: 'in' | 'out',
    value: number,
    pointer?: { x: number; y: number },
  ) => {
    markUrlTrimEndpoint(which);
    const pin = urlTrimDragPinRef.current;
    // Allow either slider to use the pin values regardless of which was dragged

    const dragOrigin = trimDragOriginRef.current;
    const applied = which === 'in'
      ? commitUrlTrimRange(value, pin?.fixedEnd ?? trimEndSecRef.current, {
        move: 'in',
        fixedEnd: pin?.fixedEnd ?? trimEndSecRef.current,
      })
      : commitUrlTrimRange(pin?.fixedStart ?? trimStartSecRef.current, value, {
        move: 'out',
        fixedStart: pin?.fixedStart ?? trimStartSecRef.current,
      });
    if (pointer) {
      const activeSec = which === 'in' ? applied.start : applied.end;
      setNeedleGlance({
        which,
        x: pointer.x,
        y: pointer.y,
        sec: activeSec,
        rangeStart: applied.start,
        rangeEnd: applied.end,
        deltaSec: activeSec - dragOrigin,
        dragging: true,
      });
    }
  }, [commitUrlTrimRange, markUrlTrimEndpoint]);

  const setPreviewVolumeLevel = useCallback((level: number) => {
    const v = Math.max(0, Math.min(1, level));
    if (previewYoutubeEmbedUrl) {
      postYoutubePreviewCommand('setVolume', [Math.round(v * 100)]);
      previewVolumeRef.current = v;
      setPreviewVolume(v);
      postYoutubePreviewCommand(v <= 0 ? 'mute' : 'unMute');
      setPreviewMuted(v <= 0);
      return;
    }
    const video = previewVideoRef.current;
    if (!video) return;
    video.volume = v;
    previewVolumeRef.current = v;
    setPreviewVolume(v);
    if (v <= 0) {
      video.muted = true;
      setPreviewMuted(true);
    } else {
      video.muted = false;
      setPreviewMuted(false);
    }
  }, [previewYoutubeEmbedUrl, postYoutubePreviewCommand]);

  const bumpPreviewFsControls = useCallback(() => {
    setPreviewFsControlsVisible(true);
    if (previewFsHideTimerRef.current) {
      window.clearTimeout(previewFsHideTimerRef.current);
    }
    if (previewFullscreen && !trimDragActiveRef.current) {
      previewFsHideTimerRef.current = window.setTimeout(() => {
        if (!trimDragActiveRef.current) {
          setPreviewFsControlsVisible(false);
        }
      }, PREVIEW_FS_CONTROLS_HIDE_MS);
    }
  }, [previewFullscreen]);


  const focusPreviewPlayer = useCallback(() => {
    previewContainerRef.current?.focus();
  }, []);

  const togglePreviewFullscreen = useCallback(async () => {
    const container = previewContainerRef.current;
    if (!container || !previewVideoReady) return;
    try {
      if (!document.fullscreenElement) {
        setTrimPanelHeight(0);
        await container.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      /* fullscreen denied or unsupported */
    }
  }, [previewVideoReady]);

  const handlePreviewContainerKeyDown = useCallback((e: KeyboardEvent) => {
    if (!previewVideoReady) return;
    if (shouldIgnorePlayerKeyEvent(e)) return;

    const { key } = e;
    const transportKeys = [' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End',
      '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'f', 'F'];
    if (!transportKeys.includes(key)) return;

    e.preventDefault();
    e.stopPropagation();

    if (key === ' ') {
      togglePreviewPlay();
      return;
    }
    if (key === 'ArrowLeft') {
      skipPreview(-PREVIEW_KEY_SKIP_SEC);
      return;
    }
    if (key === 'ArrowRight') {
      skipPreview(PREVIEW_KEY_SKIP_SEC);
      return;
    }
    if (key === 'ArrowUp') {
      setPreviewVolumeLevel(previewVolumeRef.current + 0.1);
      return;
    }
    if (key === 'ArrowDown') {
      setPreviewVolumeLevel(previewVolumeRef.current - 0.1);
      return;
    }
    if (key.toLowerCase() === 'f') {
      void togglePreviewFullscreen();
      return;
    }
    if (key === 'Home' || key === '0') {
      seekPreviewPercent(0);
      return;
    }
    if (key === 'End') {
      seekPreviewPercent(1);
      return;
    }
    if (key >= '1' && key <= '9') {
      seekPreviewPercent(parseInt(key, 10) * 0.1);
    }
  }, [
    previewVideoReady,
    togglePreviewPlay,
    skipPreview,
    setPreviewVolumeLevel,
    seekPreviewPercent,
    togglePreviewFullscreen,
  ]);

  useEffect(() => {
    const onFullscreenChange = () => {
      // Always derive state from the browser. The old YouTube-only "fake"
      // fullscreen left the controls locked after Escape.
      const fs = document.fullscreenElement === previewContainerRef.current;
      setPreviewFullscreen(fs);
      setPreviewFsControlsVisible(!fs);
      requestAnimationFrame(() => {
        void syncPreviewPlaybackToViewport(fs);
      });
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, [syncPreviewPlaybackToViewport, previewYoutubeEmbedUrl]);

  useEffect(() => {
    if (!previewOpen || !previewVideoReady || previewFullscreen) return;
    if (previewPlayback?.kind === 'progressive') return;
    void syncPreviewPlaybackToViewport();
  }, [
    previewOpen,
    previewVideoReady,
    previewFullscreen,
    previewPanelWidth,
    previewVideoAspect,
    previewPlayback?.kind,
    syncPreviewPlaybackToViewport,
  ]);

  const anyPlayerMenuOpen = previewQualityMenuOpen || previewVolumeMenuOpen || anyExploreVolumeMenuOpen;

  useEffect(() => {
    if (!anyPlayerMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest('[data-player-menu]')) return;
      setPreviewQualityMenuOpen(false);
      setPreviewVolumeMenuOpen(false);
      setExploreVolumeMenuCloseTick((t) => t + 1);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [anyPlayerMenuOpen]);

  useEffect(() => {
    if (!previewOpen || !previewVideoReady) return;
    const video = previewVideoRef.current;
    if (video) {
      video.volume = previewVolumeRef.current;
    }
    const t = window.setTimeout(() => focusPreviewPlayer(), 0);
    return () => window.clearTimeout(t);
  }, [previewOpen, previewVideoReady, focusPreviewPlayer]);

  // ── Channel explore players ──

  const pauseAllExplorePopups = useCallback(() => {
    explorePauseMapRef.current.forEach((pause) => pause());
  }, []);

  const registerExplorePause = useCallback((id: string, pause: () => void) => {
    explorePauseMapRef.current.set(id, pause);
  }, []);

  const unregisterExplorePause = useCallback((id: string) => {
    explorePauseMapRef.current.delete(id);
  }, []);

  const handleExploreVolumeMenuOpen = useCallback((id: string, open: boolean) => {
    if (open) exploreVolumeMenusRef.current.add(id);
    else exploreVolumeMenusRef.current.delete(id);
    setAnyExploreVolumeMenuOpen(exploreVolumeMenusRef.current.size > 0);
  }, []);

  const assignExplorePopupZ = useCallback((id: string) => {
    exploreZCounterRef.current += 1;
    const rank = exploreZCounterRef.current;
    setExploreZOrder((prev) => ({ ...prev, [id]: rank }));
  }, []);

  const bringExplorePopupToFront = useCallback((id: string) => {
    assignExplorePopupZ(id);
  }, [assignExplorePopupZ]);

  const closeExplorePopup = useCallback((id: string) => {
    explorePauseMapRef.current.delete(id);
    exploreVolumeMenusRef.current.delete(id);
    setAnyExploreVolumeMenuOpen(exploreVolumeMenusRef.current.size > 0);
    setExploreZOrder((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setExplorePopups((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const openExplorePlayer = useCallback((v: ListedChannelVideo) => {
    pauseAllExplorePopups();
    const vodUrl = buildVodUrl(v);
    if (v.platform === 'youtube') {
      warmYoutubePreview(vodUrl);
      warmYoutubePreviewFull(vodUrl, 500, 720);
    }
    const isClipItem = v.content_kind === 'clip' || channelContentFilter === 'clips' || isLikelyClip(v);
    const vod: ExplorePopupVod = {
      url: buildVodUrl(v),
      title: v.title || 'Untitled',
      platform: v.platform,
      durationSec: channelVideoDurationSec(v) ?? 0,
      platformListIndex: v.platformListIndex,
      isClip: isClipItem,
      thumbnailUrl: resolveVideoThumbnail(v.thumbnail_url ?? null, 640, 360),
      created_at: v.created_at ?? null,
      views: v.views ?? null,
      duration_string: v.duration_string ?? null,
    };
    const id = crypto.randomUUID();
    assignExplorePopupZ(id);
    setExplorePopups((prev) => {
      const next = [...prev, { id, vod, layoutIndex: prev.length }];
      if (next.length > MAX_EXPLORE_POPUPS) {
        const dropped = next.slice(0, next.length - MAX_EXPLORE_POPUPS);
        dropped.forEach((entry) => {
          explorePauseMapRef.current.delete(entry.id);
          exploreVolumeMenusRef.current.delete(entry.id);
        });
        setExploreZOrder((zPrev) => {
          const zNext = { ...zPrev };
          for (const entry of dropped) delete zNext[entry.id];
          return zNext;
        });
        return next.slice(-MAX_EXPLORE_POPUPS);
      }
      return next;
    });
  }, [pauseAllExplorePopups, assignExplorePopupZ, channelContentFilter]);

  const layoutBoundsInput = useCallback((): LayoutPanelBoundsInput => {
    const aside = previewOpen || channelVodPanelOpen;
    return {
      previewOpen,
      urlPanelAside: aside,
      preview: { w: previewPanelWidthRef.current, h: 0 },
      urlAside: urlAsidePanelSizeRef.current,
      main: mainPanelSizeRef.current,
    };
  }, [previewOpen, channelVodPanelOpen]);

  const handlePreviewLoadedMetadata = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video?.videoWidth || !video?.videoHeight) return;
    const aspect = video.videoWidth / video.videoHeight;
    previewVideoAspectRef.current = aspect;
    setPreviewVideoAspect(aspect);
    const layout = layoutBoundsInput();
    const clampedW = clampPreviewPanelWidth(
      previewPanelWidthRef.current,
      previewChromeHRef.current,
      aspect,
      { ...layout, previewOpen: true },
    );
    if (document.fullscreenElement !== previewContainerRef.current) {
      previewPanelWidthRef.current = clampedW;
      setPreviewPanelWidth(clampedW);
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, clampedW);
    }
  }, [layoutBoundsInput]);

  const applyLayoutPanelClamps = useCallback(() => {
    const layout = layoutBoundsInput();
    const clamped = clampAllLayoutPanels(layout);
    const nextLayout: LayoutPanelBoundsInput = {
      ...layout,
      preview: clamped.preview,
      urlAside: clamped.urlAside,
      main: clamped.main,
    };
    if (layout.previewOpen) {
      const w = clampPreviewPanelWidth(
        clamped.preview.w,
        previewChromeHRef.current,
        previewVideoAspectRef.current,
        nextLayout,
      );
      previewPanelWidthRef.current = w;
      setPreviewPanelWidth(w);
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, w);
    }
    if (layout.urlPanelAside) {
      urlAsidePanelSizeRef.current = clamped.urlAside;
      setUrlAsidePanelSize(clamped.urlAside);
      if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, clamped.urlAside);
    }
    mainPanelSizeRef.current = clamped.main;
    setMainPanelSize(clamped.main);
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, clamped.main);
  }, [layoutBoundsInput, videoInfo]);

  useEffect(() => {
    applyLayoutPanelClamps();
    const onResize = () => applyLayoutPanelClamps();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [applyLayoutPanelClamps, previewOpen, channelVodPanelOpen, videoInfo]);

  const layoutRowHasMultiplePanels = useCallback(() => {
    return layoutHasMultiplePanels(layoutBoundsInput());
  }, [layoutBoundsInput]);

  const syncRowHeightsToPreview = useCallback(() => {
    const layout = layoutBoundsInput();
    if (!layout.previewOpen || !previewPanelRef.current) return;
    const maxH = layoutMaxPanelHeight();
    const previewH = Math.min(maxH, previewPanelRef.current.offsetHeight);
    if (previewH <= 0) return;
    const nextUrlH = Math.min(maxH, Math.max(urlAsidePanelSizeRef.current.h, previewH));
    const nextMainH = Math.min(maxH, Math.max(mainPanelSizeRef.current.h, previewH));
    if (nextUrlH === urlAsidePanelSizeRef.current.h && nextMainH === mainPanelSizeRef.current.h) return;
    urlAsidePanelSizeRef.current = { ...urlAsidePanelSizeRef.current, h: nextUrlH };
    mainPanelSizeRef.current = { ...mainPanelSizeRef.current, h: nextMainH };
    setUrlAsidePanelSize((prev) => ({ ...prev, h: nextUrlH }));
    setMainPanelSize((prev) => ({ ...prev, h: nextMainH }));
    if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, urlAsidePanelSizeRef.current);
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, mainPanelSizeRef.current);
  }, [layoutBoundsInput]);

  const applyLayoutRowSizes = useCallback((fitted: {
    preview: { w: number; h: number };
    urlAside: { w: number; h: number };
    main: { w: number; h: number };
  }) => {
    const layout = layoutBoundsInput();
    if (layout.previewOpen) {
      previewPanelWidthRef.current = fitted.preview.w;
      setPreviewPanelWidth(fitted.preview.w);
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, fitted.preview.w);
    }
    urlAsidePanelSizeRef.current = fitted.urlAside;
    setUrlAsidePanelSize(fitted.urlAside);
    if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, fitted.urlAside);
    mainPanelSizeRef.current = fitted.main;
    setMainPanelSize(fitted.main);
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, fitted.main);
    syncRowHeightsToPreview();
  }, [layoutBoundsInput, syncRowHeightsToPreview]);

  const onPreviewPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const chromeH = previewChromeHRef.current;
    const aspect = previewVideoAspectRef.current;
    const coupled = layoutRowHasMultiplePanels();

    startPanelWidthResize(e, edge, previewPanelWidthRef, setPreviewPanelWidth, {
      panelEl: previewPanelRef.current,
      aspect,
      clampWidth: (w) => {
        const layout = layoutBoundsInput();
        if (coupled) {
          return resizeLayoutGivingWidthTo(layout, 'preview', w).preview.w;
        }
        return clampPreviewPanelWidth(w, chromeH, aspect, layout);
      },
      onResizeMove: coupled
        ? (w) => {
            const fitted = resizeLayoutGivingWidthTo(layoutBoundsInput(), 'preview', w);
            applyLayoutRowSizes(fitted);
          }
        : undefined,
      onResizeEnd: () => {
        applyLayoutPanelClamps();
      },
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps, layoutRowHasMultiplePanels, applyLayoutRowSizes]);

  const onUrlAsidePanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const coupled = layoutRowHasMultiplePanels();
    startPanelResizeDrag(e, edge, urlAsidePanelSizeRef, setUrlAsidePanelSize, {
      panelEl: urlAsidePanelRef.current,
      maxW: layoutMaxPanelWidth('urlAside', layoutBoundsInput()),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => {
        const layout = layoutBoundsInput();
        const base = clampPanelSizeForLayout('urlAside', s, layout);
        if (!coupled) return base;
        const fitted = resizeLayoutGivingWidthTo(layout, 'urlAside', base.w);
        return { w: fitted.urlAside.w, h: base.h };
      },
      onResizeMove: coupled
        ? (next) => {
            const fitted = resizeLayoutGivingWidthTo(layoutBoundsInput(), 'urlAside', next.w);
            applyLayoutRowSizes({ ...fitted, urlAside: { ...fitted.urlAside, h: next.h } });
          }
        : undefined,
      onResizeEnd: () => applyLayoutPanelClamps(),
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps, layoutRowHasMultiplePanels, applyLayoutRowSizes]);

  const onMainPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const coupled = layoutRowHasMultiplePanels();
    startPanelResizeDrag(e, edge, mainPanelSizeRef, setMainPanelSize, {
      panelEl: mainPanelRef.current,
      maxW: layoutMaxPanelWidth('main', layoutBoundsInput()),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => {
        const layout = layoutBoundsInput();
        const base = clampPanelSizeForLayout('main', s, layout);
        if (!coupled) return base;
        const fitted = resizeLayoutGivingWidthTo(layout, 'main', base.w);
        return { w: fitted.main.w, h: base.h };
      },
      onResizeMove: coupled
        ? (next) => {
            const fitted = resizeLayoutGivingWidthTo(layoutBoundsInput(), 'main', next.w);
            applyLayoutRowSizes({ ...fitted, main: { ...fitted.main, h: next.h } });
          }
        : undefined,
      onResizeEnd: () => applyLayoutPanelClamps(),
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps, layoutRowHasMultiplePanels, applyLayoutRowSizes]);

  useEffect(() => {
    if (!previewOpen || previewFullscreen || !previewPanelRef.current || !previewContainerRef.current) return;
    const chromeH = previewPanelRef.current.offsetHeight - previewContainerRef.current.offsetHeight;
    if (chromeH > 0) {
      previewChromeHRef.current = chromeH;
    }
  }, [previewOpen, previewFullscreen, previewPanelWidth, previewVideoAspect, previewVideoReady]);

  // ── Fetch video info ──

  type FetchVideoInfoHint = {
    durationSec?: number;
    title?: string;
    thumbnailUrl?: string | null;
    createdAt?: string | null;
    views?: number | null;
    /** Skip the /api/info/video round-trip when the caller already has enough metadata
     *  (e.g. from the channel list). The VOD · Trim panel renders immediately from the
     *  hint; explicit Extract Info can still refresh later. */
    skipNetwork?: boolean;
  };

  const applyVideoInfoTrim = useCallback((trimmed: string, end: number) => {
    trimStartSecRef.current = 0;
    trimEndSecRef.current = end;
    setTrimStartSec(0);
    setTrimEndSec(end);
    previewTrimStartRef.current = 0;
    previewTrimEndRef.current = end;
    setPreviewTrimStart(0);
    setPreviewTrimEnd(end);
    setVideoInfoUrl(trimmed);
  }, []);

  const fetchVideoInfo = useCallback(async (videoUrl: string, hint?: FetchVideoInfoHint) => {
    const trimmed = videoUrl.trim();
    if (!trimmed) return;
    const gen = ++fetchVideoInfoGenRef.current;
    if (previewSessionPrefetchRef.current?.url !== trimmed) {
      previewSessionPrefetchRef.current = null;
    }
    setLoading(true);
    setError(null);
    setPendingAddChannel(null);

    // ponytail: cache hit — user has pasted/typed this URL before. Apply it
    // immediately so the UI populates while we skip the network call.
    const cached = videoInfoCacheRef.current.get(trimmed);
    if (cached && gen === fetchVideoInfoGenRef.current) {
      setUrl(trimmed);
      setVideoInfo(cached);
      setQuality(bestAvailableQuality(cached));
      const end = Math.max(1, videoInfoDurationSec(cached));
      if (end > 0) applyVideoInfoTrim(trimmed, end);
      setLoading(false);
      return;
    }
    const hintDuration = hint?.durationSec;
    const hintTitle = hint?.title;
    // ponytail: when the caller passes channel-list metadata (skipNetwork), render it
    // immediately WITHOUT hitting /api/info/video. We do this even when duration is
    // unknown (e.g. YouTube RSS rows have no duration) so the user sees the
    // title/date/views they already fetched from the channel list instead of a
    // redundant, slow re-extraction.
    if (hint?.skipNetwork && hintTitle) {
      const end = hintDuration && hintDuration > 0 ? Math.max(1, Math.floor(hintDuration)) : 0;
      if (end > 0) applyVideoInfoTrim(trimmed, end);
      const platform = detectUrlPlatform(trimmed);
      const synthetic: VideoInfo = {
        id: trimmed,
        title: hintTitle,
        duration: end,
        duration_string: end > 0 ? fmtDuration(end) : null,
        created_at: hint.createdAt ?? null,
        views: hint.views ?? null,
        uploader: null,
        thumbnail: hint.thumbnailUrl || findCachedVideoThumbnail(trimmed, savedChannels),
        webpage_url: trimmed,
        extractor: platform,
        is_live: null,
        qualities: ['source'],
        platform: platform === 'youtube' ? 'YouTube' : platform === 'twitch' ? 'Twitch' : platform === 'kick' ? 'Kick' : null,
      };
      setUrl(trimmed);
      setVideoInfo(synthetic);
      setQuality(bestAvailableQuality(synthetic));
      if (!previewOpen) {
        void resetPreview();
      }
      setLoading(false);
      return;
    }
    if (hintDuration && hintDuration > 0 && hintTitle) {
      const end = Math.max(1, Math.floor(hintDuration));
      applyVideoInfoTrim(trimmed, end);
      const platform = detectUrlPlatform(trimmed);
      const synthetic: VideoInfo = {
        id: trimmed,
        title: hintTitle,
        duration: end,
        duration_string: fmtDuration(end),
        uploader: null,
        thumbnail: hint.thumbnailUrl || findCachedVideoThumbnail(trimmed, savedChannels),
        webpage_url: trimmed,
        extractor: platform,
        is_live: null,
        qualities: ['source'],
        platform: platform === 'youtube' ? 'YouTube' : platform === 'twitch' ? 'Twitch' : platform === 'kick' ? 'Kick' : null,
      };
      setUrl(trimmed);
      setVideoInfo(synthetic);
      setQuality(bestAvailableQuality(synthetic));
      if (hint.skipNetwork) {
        if (!previewOpen) {
          void resetPreview();
        }
        setLoading(false);
        return;
      }
    } else if (trimmed !== videoInfoUrl) {
      setVideoInfoUrl(null);
    }
    const infoPath = isClipUrl(trimmed) ? '/api/info/clip' : '/api/info/video';
    const encoded = encodeURIComponent(trimmed);
    let lastErr: Error | null = null;
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const info = await apiGet<VideoInfo>(`${infoPath}?id=${encoded}`);
          if (gen !== fetchVideoInfoGenRef.current) return;
          setUrl(trimmed);
          // ponytail: store in cache so re-pasting the same URL is instant.
          const cache = videoInfoCacheRef.current;
          cache.set(trimmed, info);
          if (cache.size > 32) {
            const firstKey = cache.keys().next().value;
            if (firstKey !== undefined) cache.delete(firstKey);
          }
          setVideoInfo(info);
          setQuality(bestAvailableQuality(info));
          const end = Math.max(1, videoInfoDurationSec(info));
          if (end <= 0) {
            setError('Could not determine video length');
            return;
          }
          applyVideoInfoTrim(trimmed, end);
          if (!previewOpen) {
            void resetPreview();
          }
          const isMediaUrl = isClipUrl(trimmed) || /\/videos\//i.test(trimmed) || /^\d+$/.test(trimmed)
            || detectUrlPlatform(trimmed) === 'youtube';
          if (isMediaUrl && savedChannels.length < MAX_SAVED_CHANNELS) {
            const platform = detectUrlPlatform(trimmed) ?? detectVideoPlatform(info, trimmed);
            const { kickSlug, twitchSlug, youtubeSlug } = slugFromVideoUrl(
              trimmed,
              platform === 'kick' || platform === 'twitch' ? platform : null,
              info.uploader,
              info.channel ?? info.uploader,
            );
            if (
              (kickSlug || twitchSlug || youtubeSlug)
              && !isChannelAlreadySaved(kickSlug, twitchSlug, savedChannels, youtubeSlug)
            ) {
              setPendingAddChannel(channelLinkDraftFromParsed({
                displayName: info.channel ?? info.uploader ?? '',
                kickSlug,
                twitchSlug,
                youtubeSlug,
              }, trimmed));
            }
          }
          return;
        } catch (err: unknown) {
          lastErr = err instanceof Error ? err : new Error(String(err));
          if (attempt + 1 < 3) {
            await new Promise((r) => window.setTimeout(r, 350 * (attempt + 1)));
          }
        }
      }
      if (lastErr) setError(lastErr.message);
    } finally {
      setLoading(false);
    }
  }, [previewOpen, resetPreview, savedChannels, applyVideoInfoTrim, videoInfoUrl]);

  const handleGetInfo = useCallback(() => {
    previewStartedRef.current = false;
    fetchVideoInfo(url);
  }, [url, fetchVideoInfo]);

  const pickDownloadFolder = useCallback(async (): Promise<string | null> => {
    setPickingFolder(true);
    setError(null);
    try {
      const res = await apiPost<{ path: string | null; error?: string | null }>('/api/pick-folder', {});
      if (res.error && !res.path) {
        setError(res.error);
        return null;
      }
      if (res.path) {
        try {
          const s = await apiGet<AppSettings>('/api/settings');
          setSettings(s);
        } catch {
          setSettings((prev) => (prev ? { ...prev, download_folder: res.path! } : prev));
        }
      }
      return res.path;
    } catch (err: any) {
      setError(err.message || 'Could not open folder picker');
      return null;
    } finally {
      setPickingFolder(false);
    }
  }, []);

  const ensureDownloadFolder = useCallback(async (): Promise<boolean> => {
    let confirmed = settings?.download_folder_confirmed;
    let folder = settings?.download_folder?.trim();
    if (confirmed === undefined || !folder) {
      try {
        const s = await apiGet<AppSettings>('/api/settings');
        folder = s.download_folder?.trim();
        confirmed = s.download_folder_confirmed;
        setSettings(s);
      } catch {
        /* ignore */
      }
    }
    if (confirmed && folder) return true;
    const picked = await pickDownloadFolder();
    return Boolean(picked);
  }, [settings?.download_folder, settings?.download_folder_confirmed, pickDownloadFolder]);

  const openFolder = useCallback((filePath: string) => {
    if (!filePath) return;
    void apiPost('/api/open-folder', { path: filePath }).catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : 'Could not open folder';
      setError(msg);
    });
  }, []);

  const openLocalFilePreview = useCallback((dl: DownloadState) => {
    if (!dl.output_file || !/\.(mp4|mkv|webm|mov|m4v)$/i.test(dl.output_file)) return;
    const id = `local_${Date.now().toString(36)}`;
    setLocalFilePopups((prev) => [
      ...prev,
      {
        id,
        filePath: dl.output_file,
        title: dl.title || dl.url,
        platform: dl.platform,
      },
    ]);
  }, []);

  const closeLocalFilePopup = useCallback((id: string) => {
    setLocalFilePopups((prev) => prev.filter((p) => p.id !== id));
  }, []);

  // ── Start download ──

  const effectiveDownloadTrim = useCallback(() => ({
    start: previewOpen ? previewTrimStartRef.current : trimStartSecRef.current,
    end: previewOpen ? previewTrimEndRef.current : trimEndSecRef.current,
  }), [previewOpen, trimStartSec, trimEndSec, previewTrimStart, previewTrimEnd]);

  const promptStartDownload = useCallback(() => {
    if (!videoInfo) return;
    const { start: effectiveStart, end: effectiveEnd } = effectiveDownloadTrim();
    if (effectiveEnd <= effectiveStart) {
      setError('Set a valid trim range before downloading.');
      return;
    }
    setDownloadConfirmOpen(true);
  }, [videoInfo, effectiveDownloadTrim]);

  // ── Refresh downloads ──

  const refreshDownloads = useCallback(async () => {
    try {
      const data = await apiGet<DownloadsResponse>('/api/downloads');
      const pending = pendingRemovalIdsRef.current;
      const withoutPending = (list: DownloadState[] | undefined) =>
        (list || []).filter((d) => !pending.has(d.download_id));
      setQueueDownloads(withoutPending(data.queue));
      setRecentDownloads(withoutPending(data.recent));
      setHistoryDownloads(withoutPending(data.history));
    } catch {}
  }, []);

  const hideDownloadOptimistic = useCallback((id: string) => {
    pendingRemovalIdsRef.current.add(id);
    setHistoryDownloads((prev) => prev.filter((d) => d.download_id !== id));
    setRecentDownloads((prev) => prev.filter((d) => d.download_id !== id));
    setQueueDownloads((prev) => prev.filter((d) => d.download_id !== id));
    setSelectedHistoryIds((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    setSelectedRecentIds((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    setSelectedQueueIds((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const finishDownloadRemoval = useCallback((id: string, ok: boolean) => {
    pendingRemovalIdsRef.current.delete(id);
    if (!ok) void refreshDownloads();
  }, [refreshDownloads]);

  const requestDownloadRemoval = useCallback((id: string) => {
    hideDownloadOptimistic(id);
    void apiPost(`/api/download/${id}/remove`, {})
      .then(() => finishDownloadRemoval(id, true))
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : 'Failed to remove download';
        setError(msg);
        finishDownloadRemoval(id, false);
      });
  }, [hideDownloadOptimistic, finishDownloadRemoval]);

  const activeDownloadIds = useMemo(
    () => queueDownloads
      .filter((d) => !d.download_id.startsWith('pending_'))
      .filter((d) => !['Paused', 'Failed', 'Cancelled', 'Interrupted'].includes(d.status))
      .map((d) => d.download_id),
    [queueDownloads],
  );

  const handleDownloadSseEvent = useCallback((id: string, event: { type: string; data: unknown }) => {
    setQueueDownloads((prev) =>
      prev.map((dl) => (dl.download_id === id ? applyDownloadSseEvent(dl, event) : dl)),
    );
  }, []);

  const handleDownloadTerminal = useCallback(() => {
    void refreshDownloads();
  }, [refreshDownloads]);

  useDownloadStreams(activeDownloadIds, handleDownloadSseEvent, handleDownloadTerminal);

  useEffect(() => {
    if (activeDownloadIds.length === 0) return;
    const timer = window.setInterval(() => {
      void refreshDownloads();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [activeDownloadIds.length, refreshDownloads]);

  const executeStartDownload = useCallback(async () => {
    setDownloadConfirmOpen(false);
    if (!videoInfo) return;
    setError(null);
    if (!(await ensureDownloadFolder())) {
      setError('Choose a download folder to continue.');
      return;
    }
    const clipDownload = isClipUrl(url.trim());
    const { start: cropStart, end: cropEnd } = effectiveDownloadTrim();
    if (cropEnd <= cropStart) {
      setError('Set a valid trim range before downloading.');
      return;
    }
    const platform = (videoInfo as VideoInfo & { platform?: string }).platform
      || detectVideoPlatform(videoInfo, url.trim())
      || 'Unknown';
    const pendingId = `pending_${Date.now().toString(36)}`;
    const optimistic: DownloadState = {
      download_id: pendingId,
      url: url.trim(),
      type: clipDownload ? 'clip' : (downloadAsAudio ? 'audio' : 'video'),
      platform: String(platform),
      status: 'Starting...',
      progress: 0,
      output_file: '',
      error: null,
      started_at: new Date().toISOString(),
      title: videoInfo.title ?? null,
      channel: videoInfo.channel ?? videoInfo.uploader ?? null,
      thumbnail: videoInfo.thumbnail ?? null,
    };
    setQueueDownloads((prev) => [...prev, optimistic]);
    setTab('queue');
    try {
      const endpoint = clipDownload ? '/api/download/clip' : '/api/download/video';
      const clipDuration = clipDownload
        ? (videoInfo?.duration ?? Math.max(1, cropEnd - cropStart))
        : (videoInfo?.duration ?? null);
      const defaultName = clipDownload
        ? suggestClipDownloadName(
            videoInfo.title,
            videoInfo.uploader,
            url.trim(),
            {
              duration: clipDuration,
              cropStart,
              cropEnd,
              platform: String(platform),
            },
          )
        : suggestVideoDownloadName(
            videoInfo.title,
            platform,
            null,
            { duration: clipDuration, cropStart, cropEnd },
          );
      const clipName = downloadFilename.trim() || defaultName;
      const trimBody = { crop_start: cropStart, crop_end: cropEnd };
      const metaBody = {
        title: videoInfo.title ?? undefined,
        channel: videoInfo.channel ?? videoInfo.uploader ?? undefined,
        thumbnail: videoInfo.thumbnail ?? undefined,
        duration: videoInfo.duration ?? undefined,
      };
      const body = clipDownload
        ? {
            url: url.trim(),
            quality: quality || undefined,
            output_file: clipName,
            ...trimBody,
            ...metaBody,
          }
        : {
            url: url.trim(),
            quality: quality || undefined,
            ...trimBody,
            ...metaBody,
            ...(downloadAsAudio && !clipDownload ? { audio_only: true } : {}),
          };
      await apiPost<{ download_id: string; status: string }>(endpoint, body);
      void refreshDownloads();
    } catch (err: unknown) {
      setQueueDownloads((prev) => prev.filter((d) => d.download_id !== pendingId));
      setError(err instanceof Error ? err.message : 'Download failed');
    }
  }, [videoInfo, url, quality, effectiveDownloadTrim, ensureDownloadFolder, refreshDownloads, downloadFilename, downloadAsAudio]);

  const downloadConfirmCopy = useMemo(() => {
    const clipDownload = isClipUrl(url.trim());
    const title = videoInfo?.title || 'Untitled';
    const trimStart = previewOpen ? previewTrimStart : trimStartSec;
    const trimEnd = previewOpen ? previewTrimEnd : trimEndSec;
    const trimDur = Math.max(1, trimEnd - trimStart);
    const platform = (videoInfo as any)?.platform || undefined;
    if (clipDownload) {
      const human = formatClipDurationHuman(trimDur);
      const defaultFilename = suggestClipDownloadName(
        videoInfo?.title,
        videoInfo?.uploader,
        url.trim(),
        { duration: videoInfo?.duration, cropStart: trimStart, cropEnd: trimEnd, platform },
      );
      const rangeNote = trimDur < (videoInfo?.duration ?? trimDur)
        ? ` (${formatHmsFull(trimStart)} → ${formatHmsFull(trimEnd)})`
        : '';
      return {
        title: 'Download clip?',
        message: `Save this clip (${human})${rangeNote}. Edit the file name below if you want.`,
        defaultFilename,
      };
    }
    return {
      title: 'Download trim?',
      message: `Download "${title}" from ${formatHmsFull(trimStart)} to ${formatHmsFull(trimEnd)}?`,
      defaultFilename: '',
    };
  }, [url, videoInfo, trimStartSec, trimEndSec, previewOpen, previewTrimStart, previewTrimEnd]);

  useEffect(() => {
    if (!downloadConfirmOpen) return;
    setDownloadFilename('');
  }, [downloadConfirmOpen, downloadConfirmCopy.defaultFilename]);

  // ── Cancel download ──

  const handleCancel = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/cancel`, {});
    } catch (err: any) {
      setError(err.message || 'Failed to cancel download');
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handlePause = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/pause`, {});
    } catch (err: any) {
      setError(err.message || 'Failed to pause download');
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handleResume = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/resume`, {});
    } catch (err: any) {
      setError(err.message || 'Failed to resume download');
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handleDeleteHistory = useCallback((id: string) => {
    if (!window.confirm('Remove this download from history? The file on disk will also be deleted.')) return;
    requestDownloadRemoval(id);
  }, [requestDownloadRemoval]);

  const handleRemoveFromQueue = useCallback(async (id: string) => {
    if (!window.confirm('Remove this download from the queue? Any partial file on disk will also be deleted.')) return;
    requestDownloadRemoval(id);
  }, [requestDownloadRemoval]);

  const toggleQueueSelection = useCallback((id: string) => {
    setSelectedQueueIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleHistorySelection = useCallback((id: string) => {
    setSelectedHistoryIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleRecentSelection = useCallback((id: string) => {
    setSelectedRecentIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const handleBulkDeleteRecent = useCallback(() => {
    if (selectedRecentIds.size === 0) return;
    if (!window.confirm(`Remove ${selectedRecentIds.size} download(s) from recent? Files on disk will also be deleted.`)) return;
    const ids = [...selectedRecentIds];
    setSelectedRecentIds(new Set());
    ids.forEach((id) => requestDownloadRemoval(id));
  }, [selectedRecentIds, requestDownloadRemoval]);

  const handleBulkDeleteQueue = useCallback(() => {
    if (selectedQueueIds.size === 0) return;
    if (!window.confirm(`Remove ${selectedQueueIds.size} download(s) from the queue? Partial files on disk will also be deleted.`)) return;
    const ids = [...selectedQueueIds];
    setSelectedQueueIds(new Set());
    ids.forEach((id) => requestDownloadRemoval(id));
  }, [selectedQueueIds, requestDownloadRemoval]);

  const handleBulkDeleteHistory = useCallback(() => {
    if (selectedHistoryIds.size === 0) return;
    if (!window.confirm(`Remove ${selectedHistoryIds.size} download(s) from history? Files on disk will also be deleted.`)) return;
    const ids = [...selectedHistoryIds];
    setSelectedHistoryIds(new Set());
    ids.forEach((id) => requestDownloadRemoval(id));
  }, [selectedHistoryIds, requestDownloadRemoval]);

  const handleBulkDownloadChannelVods = useCallback(async () => {
    if (selectedChannelVodUrls.size === 0) return;
    const count = selectedChannelVodUrls.size;
    if (!window.confirm(`Download ${count} selected item(s)?\n\nEach will download at source quality with no trim.`)) return;
    if (!(await ensureDownloadFolder())) {
      setError('Choose a download folder to continue.');
      return;
    }
    setError(null);
    const urls = [...selectedChannelVodUrls];
    setSelectedChannelVodUrls(new Set());
    setTab('queue');
    for (const vodUrl of urls) {
      const chVideo = visibleChannelVideos.find((v) => buildVodUrl(v) === vodUrl);
      const platform = chVideo?.platform ?? detectUrlPlatform(vodUrl) ?? 'Unknown';
      const pendingId = `pending_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
      setQueueDownloads((prev) => [...prev, {
        download_id: pendingId,
        url: vodUrl,
        type: isClipUrl(vodUrl) ? 'clip' : 'video',
        platform: String(platform),
        status: 'Starting...',
        progress: 0,
        output_file: '',
        error: null,
        started_at: new Date().toISOString(),
        title: chVideo?.title ?? null,
        channel: chVideo?.channel ?? null,
        thumbnail: chVideo?.thumbnail_url ?? null,
      }]);
      try {
        const dlEndpoint = isClipUrl(vodUrl) ? '/api/download/clip' : '/api/download/video';
        await apiPost<{ download_id: string }>(dlEndpoint, {
          url: vodUrl,
          quality: 'source',
          title: chVideo?.title ?? undefined,
          channel: chVideo?.channel ?? undefined,
          thumbnail: chVideo?.thumbnail_url ?? undefined,
          duration: chVideo?.duration ?? undefined,
        });
      } catch (err: unknown) {
        setQueueDownloads((prev) => prev.filter((d) => d.download_id !== pendingId));
        setError(err instanceof Error ? err.message : 'Failed to start download');
        break;
      }
    }
    void refreshDownloads();
  }, [selectedChannelVodUrls, ensureDownloadFolder, refreshDownloads, visibleChannelVideos]);

  const toggleChannelVodSelection = useCallback((vodUrl: string) => {
    setSelectedChannelVodUrls((prev) => {
      const next = new Set(prev);
      if (next.has(vodUrl)) next.delete(vodUrl); else next.add(vodUrl);
      return next;
    });
  }, []);

  useEffect(() => {
    void refreshDownloads();
  }, [refreshDownloads]);

  // Refresh queue when opening the tab
  useEffect(() => {
    if (tab !== 'queue') return;
    refreshDownloads();
  }, [tab, refreshDownloads]);

  // ── Channel browsing (localStorage) ──

  type ChannelVodsResponse = {
    videos: ChannelVideo[];
    channel: string;
    platforms: string[];
    content?: 'vods';
    days: number;
    per_platform_errors?: Record<string, string>;
  };

  type ChannelClipsResponse = {
    clips: ChannelVideo[];
    channel: string;
    platforms: string[];
    content?: 'clips';
    per_platform_errors?: Record<string, string>;
  };

  useEffect(() => {
    persistChannels(savedChannels);
    if (!channelsPersistReadyRef.current) return;
    const payload = savedChannels.map(({ loading: _loading, ...ch }) => ch);
    if (channelsSaveTimerRef.current) {
      window.clearTimeout(channelsSaveTimerRef.current);
    }
    channelsSaveTimerRef.current = window.setTimeout(() => {
      apiPost('/api/settings', { saved_channels: payload }).catch(() => {});
    }, 2000);
    return () => {
      if (channelsSaveTimerRef.current) {
        window.clearTimeout(channelsSaveTimerRef.current);
      }
    };
  }, [savedChannels]);

  useEffect(() => {
    try {
      localStorage.setItem(
        CHANNEL_UI_STORAGE_KEY,
        JSON.stringify({
          kick: kickEnabled,
          twitch: twitchEnabled,
          youtube: youtubeEnabled,
          content: channelContentFilter,
        }),
      );
    } catch {
      /* ignore */
    }
    if (!channelUiPersistReadyRef.current) return;
    if (channelUiSaveTimerRef.current) {
      window.clearTimeout(channelUiSaveTimerRef.current);
    }
    channelUiSaveTimerRef.current = window.setTimeout(() => {
      apiPost('/api/settings', {
        channel_kick_enabled: kickEnabled,
        channel_twitch_enabled: twitchEnabled,
        channel_youtube_enabled: youtubeEnabled,
        channel_content_filter: channelContentFilter,
      }).catch(() => {});
    }, 800);
    setSettings((prev) =>
      prev
        ? {
            ...prev,
            channel_kick_enabled: kickEnabled,
            channel_twitch_enabled: twitchEnabled,
            channel_youtube_enabled: youtubeEnabled,
            channel_content_filter: channelContentFilter,
          }
        : prev,
    );
    return () => {
      if (channelUiSaveTimerRef.current) {
        window.clearTimeout(channelUiSaveTimerRef.current);
      }
    };
  }, [kickEnabled, twitchEnabled, youtubeEnabled, channelContentFilter]);

  const updateChannel = useCallback((id: string, patch: Partial<SavedChannel>) => {
    setSavedChannels((prev) => prev.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }, []);

  const savedChannelsRef = useRef(savedChannels);
  savedChannelsRef.current = savedChannels;

  const channelRefreshInFlightRef = useRef<Set<string>>(new Set());
  const channelRefreshPromisesRef = useRef<Map<string, Promise<void>>>(new Map());

  const clearChannelRefreshFlight = useCallback((channelId: string, mode?: 'vods' | 'clips' | 'streams') => {
    const modes = mode ? [mode] : (['vods', 'clips', 'streams'] as const);
    for (const m of modes) {
      const key = `${channelId}:${m}`;
      channelRefreshInFlightRef.current.delete(key);
      channelRefreshPromisesRef.current.delete(key);
    }
  }, []);

  const refreshChannel = useCallback(async (
    channelId: string,
    channelOverride?: SavedChannel,
    contentMode?: 'vods' | 'clips' | 'streams',
    opts?: { incremental?: boolean; silent?: boolean; force?: boolean },
  ) => {
    const ch = channelOverride ?? savedChannelsRef.current.find((c) => c.id === channelId);
    if (!ch) return;
    const mode = contentMode ?? channelContentFilter;
    const incremental = opts?.incremental ?? false;
    const silent = opts?.silent ?? false;
    const flightKey = `${channelId}:${mode}`;

    if (opts?.force) {
      clearChannelRefreshFlight(channelId, mode);
    }

    if (!incremental) {
      const pending = channelRefreshPromisesRef.current.get(flightKey);
      if (pending) return pending;
    }

    const task = (async () => {
    if (!incremental) channelRefreshInFlightRef.current.add(flightKey);

    if (!incremental && !silent) {
      updateChannel(channelId, { loading: true });
      resetChannelListPaging();
    }
    const errs: Record<string, string> = {};
    const incoming: ChannelVideo[] = [];
    const attempted: Partial<Record<'Kick' | 'Twitch' | 'YouTube', boolean>> = {};

    // Always fetch both platforms; Kick/Twitch toggles only filter the display.
    const wantKick = true;
    const wantTwitch = true;
    const wantYoutube = true;

    try {
      if (mode === 'clips') {
        const slug = ch.kickSlug?.trim() || ch.twitchSlug?.trim() || ch.youtubeSlug?.trim() || '';
        const clipPlatforms = ['Kick', 'Twitch'];
        if (ch.youtubeSlug?.trim()) clipPlatforms.push('YouTube');
        const params = new URLSearchParams({
          platforms: clipPlatforms.join(','),
          limit: '10',
          days: '0',
          kick_slug: ch.kickSlug,
          twitch_login: ch.twitchSlug,
          youtube_slug: ch.youtubeSlug,
        });
        if (slug) params.set('url', slug);
        try {
          let data: ChannelClipsResponse;
          try {
            data = await apiGet<ChannelClipsResponse>(`/api/channel/clips?${params}&_t=${Date.now()}`);
          } catch (clipErr: unknown) {
            const msg = clipErr instanceof Error ? clipErr.message : '';
            if (!msg.includes('Clips API not on server') && !msg.includes('Clips API unavailable')) {
              throw clipErr;
            }
            params.set('content', 'clips');
            data = await apiGet<ChannelClipsResponse>(`/api/channel/videos?${params}&_t=${Date.now()}`);
          }
          if (data.content && data.content !== 'clips') {
            errs.Kick = IS_DEV_UI
              ? 'Clips API unavailable — restart with npm run dev'
              : 'Clips API unavailable — reopen VOD.RIP';
            errs.Twitch = errs.Kick;
          } else {
            incoming.push(...(data.clips ?? (data as unknown as ChannelVodsResponse).videos ?? []).map(mapApiChannelItem));
            for (const [platform, pe] of Object.entries(data.per_platform_errors ?? {})) {
              if (pe && !isHiddenChannelPlatformError(pe)) errs[platform] = pe;
            }
          }
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : 'Failed to fetch clips';
          errs.Kick = msg;
          errs.Twitch = msg;
          if (ch.kickSlug?.trim()) attempted.Kick = true;
          if (ch.twitchSlug?.trim()) attempted.Twitch = true;
          if (ch.youtubeSlug?.trim()) attempted.YouTube = true;
        }
        if (ch.kickSlug?.trim() && !attempted.Kick) attempted.Kick = true;
        if (ch.twitchSlug?.trim() && !attempted.Twitch) attempted.Twitch = true;
        if (ch.youtubeSlug?.trim() && !attempted.YouTube) attempted.YouTube = true;
        const latest = savedChannelsRef.current.find((c) => c.id === channelId) ?? ch;
        const clipVideos = mergeClipLists(latest.clipVideos ?? [], incoming);
        if (incremental) {
          updateChannel(channelId, {
            clipVideos,
            updatedAt: new Date().toISOString(),
          });
        } else {
          const prevClipErrors = latest.clipErrors ?? {};
          const clipPlatformsFetched = mergeClipPlatformsFetched(
            latest.clipPlatformsFetched ?? {},
            ch,
            incoming,
            errs,
            attempted,
          );
          updateChannel(channelId, {
            clipVideos,
            clipErrors: { ...prevClipErrors, ...errs },
            clipsFetched: Object.values(clipPlatformsFetched).some(Boolean),
            clipPlatformsFetched,
            loading: false,
            updatedAt: new Date().toISOString(),
          });
        }
      } else if (mode === 'streams') {
        const limit = incremental ? CHANNEL_INCREMENTAL_LIMIT : CHANNEL_FETCH_LIMIT;
        if (ch.youtubeSlug?.trim()) {
          const params = new URLSearchParams({
            platforms: 'YouTube',
            content: 'streams',
            youtube_slug: ch.youtubeSlug,
            url: ch.youtubeSlug,
            limit: String(limit),
            days: '0',
            kick_slug: ch.kickSlug,
            twitch_login: ch.twitchSlug,
          });
          try {
            const data = await apiGet<ChannelVodsResponse>(`/api/channel/videos?${params}&_t=${Date.now()}`);
            attempted.YouTube = true;
            incoming.push(...(data.videos ?? []).map(mapApiChannelItem));
            delete errs.YouTube;
            const pe = data.per_platform_errors?.YouTube;
            if (pe && !isHiddenChannelPlatformError(pe)) errs.YouTube = pe;
          } catch (err: unknown) {
            attempted.YouTube = true;
            errs.YouTube = err instanceof Error ? err.message : 'Failed to fetch YouTube stream VODs';
          }
        } else if (wantYoutube) {
          errs.YouTube = 'YouTube channel is required';
        }
        const latest = savedChannelsRef.current.find((c) => c.id === channelId) ?? ch;
        const vodVideos = mergeVodLists(latest.vodVideos ?? [], incoming);
        if (incremental) {
          updateChannel(channelId, {
            vodVideos,
            updatedAt: new Date().toISOString(),
          });
        } else {
          updateChannel(channelId, {
            vodVideos,
            vodErrors: { ...(latest.vodErrors ?? {}), ...errs },
            streamsFetched: !ch.youtubeSlug?.trim()
              || incoming.some((v) => v.content_kind === 'stream')
              || Boolean(errs.YouTube),
            loading: false,
            updatedAt: new Date().toISOString(),
          });
        }
      } else {
        const limit = incremental ? CHANNEL_INCREMENTAL_LIMIT : CHANNEL_FETCH_LIMIT;
        const fetchVods = async (platform: 'Kick' | 'Twitch' | 'YouTube', slug: string) => {
          if (!slug?.trim()) return;
          const params = new URLSearchParams({
            url: slug,
            limit: String(limit),
            days: '0',
            platforms: platform,
            content: 'vods',
            kick_slug: ch.kickSlug,
            twitch_login: ch.twitchSlug,
            youtube_slug: ch.youtubeSlug,
          });
          try {
            const data = await apiGet<ChannelVodsResponse>(`/api/channel/videos?${params}&_t=${Date.now()}`);
            attempted[platform] = true;
            incoming.push(...(data.videos ?? []).map(mapApiChannelItem));
            delete errs[platform];
            const pe = data.per_platform_errors?.[platform];
            if (pe && !isHiddenChannelPlatformError(pe)) errs[platform] = pe;
          } catch (err: unknown) {
            attempted[platform] = true;
            errs[platform] = err instanceof Error ? err.message : `Failed to fetch ${platform} VODs`;
          }
        };
        const vodTasks: Promise<void>[] = [];
        if (wantKick) vodTasks.push(fetchVods('Kick', ch.kickSlug));
        if (wantTwitch) vodTasks.push(fetchVods('Twitch', ch.twitchSlug));
        if (wantYoutube) vodTasks.push(fetchVods('YouTube', ch.youtubeSlug));
        if (!wantKick) delete errs.Kick;
        if (!wantTwitch) delete errs.Twitch;
        if (!wantYoutube) delete errs.YouTube;
        await Promise.all(vodTasks);
        const latest = savedChannelsRef.current.find((c) => c.id === channelId) ?? ch;
        const vodVideos = mergeVodLists(latest.vodVideos ?? [], incoming);
        if (incremental) {
          updateChannel(channelId, {
            vodVideos,
            updatedAt: new Date().toISOString(),
          });
        } else {
          const vodPlatformsFetched = mergeVodPlatformsFetched(
            latest.vodPlatformsFetched ?? {},
            ch,
            incoming,
            errs,
            attempted,
          );
          updateChannel(channelId, {
            vodVideos,
            vodErrors: errs,
            vodPlatformsFetched,
            loading: false,
            updatedAt: new Date().toISOString(),
          });
        }
      }

    } finally {
      if (!incremental) {
        channelRefreshInFlightRef.current.delete(flightKey);
        channelRefreshPromisesRef.current.delete(flightKey);
        if (!silent) {
          updateChannel(channelId, { loading: false });
        }
      }
    }
    })();

    if (!incremental) {
      channelRefreshPromisesRef.current.set(flightKey, task);
    }
    return task;
  }, [updateChannel, channelContentFilter, resetChannelListPaging, clearChannelRefreshFlight]);

  const refreshChannelRef = useRef(refreshChannel);
  refreshChannelRef.current = refreshChannel;

  const channelFiltersRef = useRef({
    channelContentFilter,
    kickEnabled,
    twitchEnabled,
    youtubeEnabled,
  });

  // Whenever the user clicks any of the four filter surfaces (channel
  // selection, VODs/Clips toggle, Kick toggle, Twitch toggle) we
  // re-check the displayed data is populated for the current filter
  // combination and re-fetch anything that is missing.
  //
  // Why this matters: the old code had three separate useEffects, two
  // of which were gated by Sets that persisted for the life of the
  // page. As soon as a fetch had been kicked off once, toggling Kick /
  // Twitch / VODs / Clips no longer triggered a re-fetch — so the user
  // would see "No VODs" or "No clips" after clicking around the filters
  // even when the underlying channels had plenty of content.
  //
  // The only guard we still need is the in-flight ref, so we don't
  // re-fire a fetch that's already running. In-flight is keyed by
  // `channelId:mode` so concurrent fetches for the OTHER mode (clips
  // vs VODs) are unaffected.
  useEffect(() => {
    if (!channelUiPersistReadyRef.current || !selectedChannelId) return;

    // Persist the latest filter choices to localStorage regardless of
    // whether a fetch happens (matches the prior behaviour).
    channelFiltersRef.current = { channelContentFilter, kickEnabled, twitchEnabled, youtubeEnabled };

    const ch = savedChannelsRef.current.find((c) => c.id === selectedChannelId);
    if (!ch) return;
    const mode = channelContentFilter;

    const needsFetch =
      mode === 'clips'
        ? channelClipsMissing(ch, kickEnabled, twitchEnabled, youtubeEnabled)
        : mode === 'streams'
          ? channelStreamsMissing(ch, youtubeEnabled)
          : channelVodsMissing(ch, kickEnabled, twitchEnabled, youtubeEnabled);
    if (!needsFetch) return;

    const hasCache = channelHasCachedContent(ch, mode, kickEnabled, twitchEnabled, youtubeEnabled);
    void refreshChannelRef.current(selectedChannelId, undefined, mode, {
      silent: hasCache,
      force: !hasCache,
    });
  }, [channelContentFilter, kickEnabled, twitchEnabled, youtubeEnabled, selectedChannelId]);

  // ponytail: prefetch YouTube stream-tab VODs while user is on Videos/Shorts
  useEffect(() => {
    if (!selectedChannelId) return;
    const ch = savedChannelsRef.current.find((c) => c.id === selectedChannelId);
    if (!ch?.youtubeSlug?.trim()) return;
    if (!channelStreamsMissing(ch, true)) return;
    void refreshChannelRef.current(selectedChannelId, undefined, 'streams', { silent: true });
  }, [selectedChannelId]);

  useEffect(() => {
    if (channelContentFilter === 'streams' && !youtubePlatformOnly) {
      setChannelContentFilter('vods');
    }
  }, [channelContentFilter, youtubePlatformOnly]);

  // On page load: cheap incremental VOD sync for every saved channel
  // (merge new ids only — the full refresh is triggered by the filter
  // useEffect above when a user actually selects a channel). This
  // used to live in a separate useEffect gated by a ref so it would
  // fire exactly once per page load; we keep the same shape so the
  // behaviour survives the consolidation.
  // On page load: show cached channel data immediately, then silently
  // refetch every channel in the background (both VODs and clips).
  // The merge functions (mergeVodLists/mergeClipLists) do incoming-wins
  // merge, so the cached data stays visible until fresh data arrives.
  const incrementalSyncDoneRef = useRef(false);
  useEffect(() => {
    if (incrementalSyncDoneRef.current) return;
    incrementalSyncDoneRef.current = true;
    const channels = loadSavedChannels();
    channels.forEach((c) => {
      void refreshChannelRef.current(c.id, c, 'vods', { silent: true, incremental: true });
      void refreshChannelRef.current(c.id, c, 'clips', { silent: true, incremental: true });
    });
  }, []);

  // Start-up: warm YouTube preview cache for cached videos (2 per channel first).
  // Collects YouTube URLs from saved channels' vodVideos/clipVideos (loaded from localStorage on mount).
  // Sends in batches of 2 per channel to minimize latency for the first click.
  useEffect(() => {
    const channels = savedChannelsRef.current;
    if (!channels.length) return;

    // Collect YouTube URLs from each channel's cached videos
    const perChannel: string[][] = [];
    for (const ch of channels) {
      const urls: string[] = [];
      for (const v of (ch.vodVideos ?? []).concat(ch.clipVideos ?? [])) {
        if ((v.platform === 'YouTube' || v.platform === 'youtube') && v.url) {
          urls.push(v.url);
        }
      }
      if (urls.length) perChannel.push(urls);
    }
    if (!perChannel.length) return;

    let idx = 0;
    const BATCH = 2; // 2 per channel per batch

    const sendNextBatch = () => {
      const batch: string[] = [];
      let anyLeft = false;
      for (const chUrls of perChannel) {
        const remaining = chUrls.slice(idx);
        if (remaining.length) {
          anyLeft = true;
          batch.push(...remaining.slice(0, BATCH));
        }
      }
      if (!batch.length || !anyLeft) return;

      idx += BATCH;

      fetch('/api/preview/warm/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: batch, prefer_height: 360 }),
      }).catch(() => {});

      // Schedule next batch after a delay (let each warm complete before next)
      setTimeout(sendNextBatch, 3000);
    };

    // Start warming after a short delay so page render isn't affected
    setTimeout(sendNextBatch, 500);
  }, []);

  const addChannelFromSlugs = useCallback(async (
    kickSlug: string,
    twitchSlug: string,
    youtubeSlug: string,
  ) => {
    const kick = kickSlug.trim();
    const twitch = twitchSlug.trim();
    const youtube = youtubeSlug.trim();
    if (!kick && !twitch && !youtube) return;
    if (savedChannels.length >= MAX_SAVED_CHANNELS) {
      setAddChannelNotice(`Max ${MAX_SAVED_CHANNELS} channels.`);
      return;
    }
    setAddChannelNotice(null);
    const id = `ch_${Date.now().toString(36)}`;
    const entry: SavedChannel = {
      id,
      displayName: deriveChannelDisplayName(kick, twitch, youtube),
      kickSlug: kick,
      twitchSlug: twitch,
      youtubeSlug: youtube,
      vodVideos: [],
      clipVideos: [],
      vodErrors: {},
      clipErrors: {},
      updatedAt: '',
      loading: true,
    };
    setSavedChannels((prev) => [...prev, entry]);
    setSelectedChannelId(id);
    channelRefreshInFlightRef.current.delete(`${id}:vods`);
    channelRefreshInFlightRef.current.delete(`${id}:clips`);
    channelRefreshInFlightRef.current.delete(`${id}:streams`);
    channelRefreshPromisesRef.current.delete(`${id}:vods`);
    channelRefreshPromisesRef.current.delete(`${id}:clips`);
    channelRefreshPromisesRef.current.delete(`${id}:streams`);
    await refreshChannel(id, entry, 'vods', { force: true });
    if (channelContentFilter === 'clips' && (kick || twitch || youtube)) {
      await refreshChannel(id, entry, 'clips');
    }
    if (channelContentFilter === 'streams' && youtube) {
      await refreshChannel(id, entry, 'streams');
    }
  }, [savedChannels.length, refreshChannel, channelContentFilter]);

  const channelLinkDuplicate = useMemo(() => {
    if (!pendingAddChannel) return null;
    const { kick, twitch, youtube } = channelLinkDraftSlugs(pendingAddChannel);
    if (!kick && !twitch && !youtube) return null;
    if (isChannelAlreadySaved(kick, twitch, savedChannels, youtube)) {
      return 'This channel is already linked.';
    }
    return null;
  }, [pendingAddChannel, savedChannels]);

  const commitChannelLink = useCallback(async () => {
    if (!pendingAddChannel) return;
    const { kick, twitch, youtube } = channelLinkDraftSlugs(pendingAddChannel);
    if (!kick && !twitch && !youtube) return;
    setPendingAddChannel(null);
    setAddChannelInput('');
    await addChannelFromSlugs(kick, twitch, youtube);
  }, [pendingAddChannel, addChannelFromSlugs]);

  const handleAddChannel = useCallback(() => {
    const raw = addChannelInput.trim();
    if (!raw) return;
    const parsed = parseChannelInput(raw);
    if (!parsed.kickSlug && !parsed.twitchSlug && !parsed.youtubeSlug && !parsed.displayName) return;
    setPendingAddChannel(channelLinkDraftFromParsed(parsed, raw));
    setAddChannelInput('');
  }, [addChannelInput]);

  const toggleChannelSelection = useCallback((channelId: string) => {
    setSelectedChannelId((prev) => {
      if (prev === channelId) return null;
      return channelId;
    });
    setEditingChannelId(null);
    setEditingSlug(null);
  }, []);

  const startRenameChannel = useCallback((channelId: string) => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    setEditingChannelId(channelId);
    setEditingChannelName(ch.displayName);
  }, [savedChannels]);

  const commitRenameChannel = useCallback(async () => {
    if (!editingChannelId) return;
    const nextRaw = editingChannelName.trim();
    const channelId = editingChannelId;
    setEditingChannelId(null);
    setEditingChannelName('');
    if (!nextRaw) return;
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    // Re-derive slugs from the new name — pasting a Kick/Twitch URL or
    // channel handle should rebind this saved channel to that target and
    // re-fetch its VODs/clips, mirroring `handleAddChannel`. Without this
    // step "rename" only changed the label and left the cached videos
    // pointing at the old (now unrelated) channel.
    const parsed = parseChannelInput(nextRaw);
    const nextKick = parsed.kickSlug || ch.kickSlug;
    const nextTwitch = parsed.twitchSlug || ch.twitchSlug;
    const nextDisplay = parsed.displayName || nextRaw;
    const slugChanged =
      nextKick.toLowerCase() !== (ch.kickSlug || '').toLowerCase() ||
      nextTwitch.toLowerCase() !== (ch.twitchSlug || '').toLowerCase();
    if (!slugChanged) {
      if (nextDisplay !== ch.displayName) {
        updateChannel(channelId, { displayName: nextDisplay });
      }
      return;
    }
    const cleared = {
      vodVideos: [] as ChannelVideo[],
      clipVideos: [] as ChannelVideo[],
      vodErrors: {} as Record<string, string>,
      clipErrors: {} as Record<string, string>,
      clipsFetched: false,
      streamsFetched: false,
      vodPlatformsFetched: {},
      clipPlatformsFetched: {},
    };
    const updated: SavedChannel = {
      ...ch,
      displayName: deriveChannelDisplayName(nextKick, nextTwitch),
      kickSlug: nextKick,
      twitchSlug: nextTwitch,
      ...cleared,
    };
    channelRefreshInFlightRef.current.delete(`${channelId}:vods`);
    channelRefreshInFlightRef.current.delete(`${channelId}:clips`);
    channelRefreshInFlightRef.current.delete(`${channelId}:streams`);
    updateChannel(channelId, updated);
    await refreshChannel(channelId, updated);
  }, [editingChannelId, editingChannelName, savedChannels, updateChannel, refreshChannel]);

  const startEditPlatformSlug = useCallback((channelId: string, platform: 'Kick' | 'Twitch' | 'YouTube') => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    setEditingSlug({ channelId, platform });
    setEditingSlugValue(
      platform === 'Kick' ? ch.kickSlug : platform === 'Twitch' ? ch.twitchSlug : ch.youtubeSlug,
    );
  }, [savedChannels]);

  const commitEditPlatformSlug = useCallback(async () => {
    if (!editingSlug) return;
    const slug = editingSlugValue.trim();
    if (!slug) return;
    const ch = savedChannels.find((c) => c.id === editingSlug.channelId);
    if (!ch) return;

    const prevSlug = editingSlug.platform === 'Kick'
      ? ch.kickSlug
      : editingSlug.platform === 'Twitch'
        ? ch.twitchSlug
        : ch.youtubeSlug;
    const channelId = editingSlug.channelId;

    setEditingSlug(null);
    setEditingSlugValue('');

    if (slug === prevSlug) return;

    const cleared = {
      vodVideos: [] as ChannelVideo[],
      clipVideos: [] as ChannelVideo[],
      vodErrors: {} as Record<string, string>,
      clipErrors: {} as Record<string, string>,
      clipsFetched: false,
      streamsFetched: false,
      vodPlatformsFetched: {},
      clipPlatformsFetched: {},
    };
    const slugPatch = editingSlug.platform === 'Kick'
      ? { kickSlug: slug }
      : editingSlug.platform === 'Twitch'
        ? { twitchSlug: slug }
        : { youtubeSlug: slug };
    const updated: SavedChannel = { ...ch, ...slugPatch, ...cleared };

    channelRefreshInFlightRef.current.delete(`${channelId}:vods`);
    channelRefreshInFlightRef.current.delete(`${channelId}:clips`);
    channelRefreshInFlightRef.current.delete(`${channelId}:streams`);
    updateChannel(channelId, { ...slugPatch, ...cleared });
    await refreshChannel(channelId, updated);
  }, [editingSlug, editingSlugValue, savedChannels, updateChannel, refreshChannel]);

  const handleExpandChannelList = useCallback(() => {
    const markBeyond = (videos: ChannelVideo[], nextLimit: number, platform: 'Kick' | 'Twitch' | 'YouTube') => {
      const sorted = sortChannelVideosByMode(videos, clipsMode);
      const cutoff = Date.now() - CHANNEL_RECENT_DAYS * 86_400_000;
      const recent = sorted.filter((v) => {
        const ts = parseVideoTs(v.created_at);
        return ts === 0 || ts >= cutoff;
      });
      const recentPool = recent.length > 0 ? recent : sorted;
      if (nextLimit > recentPool.length && sorted.length > recentPool.length) {
        setChannelBeyondRecent((prev) => ({ ...prev, [platform]: true }));
      }
    };
    if (kickEnabled && channelHasKick) {
      setKickVisibleLimit((n) => {
        const next = n + CHANNEL_EXPAND_STEP;
        markBeyond(kickChannelVideos, next, 'Kick');
        return next;
      });
    }
    if (twitchEnabled && channelHasTwitch) {
      setTwitchVisibleLimit((n) => {
        const next = n + CHANNEL_EXPAND_STEP;
        markBeyond(twitchChannelVideos, next, 'Twitch');
        return next;
      });
    }
    if (youtubeEnabled && channelHasYoutube) {
      setYoutubeVisibleLimit((n) => {
        const next = n + CHANNEL_EXPAND_STEP;
        markBeyond(youtubeChannelVideos, next, 'YouTube');
        return next;
      });
    }
  }, [
    clipsMode,
    kickEnabled,
    twitchEnabled,
    youtubeEnabled,
    channelHasKick,
    channelHasTwitch,
    channelHasYoutube,
    kickChannelVideos,
    twitchChannelVideos,
    youtubeChannelVideos,
  ]);
  const removeChannel = useCallback((channelId: string) => {
    setSavedChannels((prev) => {
      const next = prev.filter((c) => c.id !== channelId);
      if (selectedChannelId === channelId) {
        setSelectedChannelId(next[0]?.id ?? null);
      }
      return next;
    });
  }, [selectedChannelId]);

  const removePlatformFromChannel = useCallback((channelId: string, platform: 'Kick' | 'Twitch' | 'YouTube') => {
    setSavedChannels((prev) => {
      const ch = prev.find((c) => c.id === channelId);
      if (!ch) return prev;
      const nextKick = platform === 'Kick' ? '' : ch.kickSlug;
      const nextTwitch = platform === 'Twitch' ? '' : ch.twitchSlug;
      const nextYoutube = platform === 'YouTube' ? '' : ch.youtubeSlug;
      if (!nextKick.trim() && !nextTwitch.trim() && !nextYoutube.trim()) {
        const next = prev.filter((c) => c.id !== channelId);
        if (selectedChannelId === channelId) {
          setSelectedChannelId(next[0]?.id ?? null);
        }
        return next;
      }
      const stripPlatform = (v: ChannelVideo) => v.platform !== platform;
      const updated: SavedChannel = {
        ...ch,
        kickSlug: nextKick,
        twitchSlug: nextTwitch,
        youtubeSlug: nextYoutube,
        displayName: deriveChannelDisplayName(nextKick, nextTwitch, nextYoutube),
        vodVideos: (ch.vodVideos ?? []).filter(stripPlatform),
        clipVideos: (ch.clipVideos ?? []).filter(stripPlatform),
        vodErrors: Object.fromEntries(
          Object.entries(ch.vodErrors ?? {}).filter(([k]) => k !== platform),
        ),
        clipErrors: Object.fromEntries(
          Object.entries(ch.clipErrors ?? {}).filter(([k]) => k !== platform),
        ),
      };
      channelRefreshInFlightRef.current.delete(`${channelId}:vods`);
      channelRefreshInFlightRef.current.delete(`${channelId}:clips`);
      return prev.map((c) => (c.id === channelId ? updated : c));
    });
  }, [selectedChannelId]);

  // ── Load settings ──

  const hydrateSavedChannelsOnce = useCallback((apiChannels?: SavedChannel[] | null) => {
    if (channelsHydratedRef.current) return;
    channelsHydratedRef.current = true;
    const local = loadSavedChannels();
    if (local.length === 0 && apiChannels && apiChannels.length > 0) {
      const restored = apiChannels.map((ch) => normalizeSavedChannel(ch));
      setSavedChannels(restored);
      persistChannels(restored);
    }
    channelsPersistReadyRef.current = true;
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const s = await apiGet<AppSettings>('/api/settings');
      setSettings(s);
      if (typeof s.channel_kick_enabled === 'boolean') {
        setKickEnabled(s.channel_kick_enabled);
      }
      if (typeof s.channel_twitch_enabled === 'boolean') {
        setTwitchEnabled(s.channel_twitch_enabled);
      }
      if (typeof s.channel_youtube_enabled === 'boolean') {
        setYoutubeEnabled(s.channel_youtube_enabled);
      }
      if (s.channel_content_filter === 'clips' || s.channel_content_filter === 'vods' || s.channel_content_filter === 'streams') {
        setChannelContentFilter(s.channel_content_filter);
      }
      hydrateSavedChannelsOnce(
        Array.isArray(s.saved_channels)
          ? s.saved_channels.map((ch) => normalizeSavedChannel(ch as SavedChannel))
          : null,
      );
      if (s.panel_layout) {
        const pl = s.panel_layout as PersistedPanelLayout;
        if (pl.previewPanelWidth && pl.urlAside && pl.main) {
          restorePanelLayout(pl);
        }
      }
    } catch {
      hydrateSavedChannelsOnce(null);
    } finally {
      channelUiPersistReadyRef.current = true;
      panelLayoutPersistReadyRef.current = true;
    }
  }, [restorePanelLayout, hydrateSavedChannelsOnce]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    if (tab === 'settings') loadSettings();
  }, [tab, loadSettings]);

  const loadUpdateStatus = useCallback(async (force = false) => {
    try {
      const [ver, check] = await Promise.all([
        apiGet<{ version: string }>('/api/app/version'),
        apiGet<{ current: string; update: UpdateInfo | null }>(
          `/api/update/check${force ? '?force=true' : ''}`,
        ),
      ]);
      setAppVersion(ver.version);
      setUpdateInfo(check.update);
      if (!check.update && force) {
        setUpdateMessage(`You're on the latest version (v${ver.version}).`);
      }
    } catch {
      /* packaged-only endpoints may be unavailable in dev */
    }
  }, []);

  useEffect(() => {
    if (tab === 'settings') void loadUpdateStatus();
  }, [tab, loadUpdateStatus]);

  const handleCheckUpdate = useCallback(async () => {
    setUpdateChecking(true);
    setUpdateMessage(null);
    try {
      await loadUpdateStatus(true);
    } catch (err: any) {
      setUpdateMessage(err.message || 'Update check failed');
    } finally {
      setUpdateChecking(false);
    }
  }, [loadUpdateStatus]);

  const handleApplyUpdate = useCallback(async () => {
    if (!updateInfo) return;
    const isSetup = (updateInfo.asset_name || '').toLowerCase().includes('setup');
    const prompt = isSetup
      ? `Install VOD.RIP v${updateInfo.version}? The installer will open and this app will close.`
      : `Download VOD.RIP v${updateInfo.version}? The verified zip will open in Explorer — extract it over your install folder, or use Setup.exe from GitHub.`;
    if (!window.confirm(prompt)) return;
    setUpdateApplying(true);
    setUpdateMessage(null);
    try {
      const res = await apiPost<{ ok: boolean; message?: string }>('/api/update/apply', {});
      setUpdateMessage(res.message || 'Update started');
      if (!isSetup) setUpdateApplying(false);
    } catch (err: any) {
      setUpdateMessage(err.message || 'Update failed');
      setUpdateApplying(false);
    }
  }, [updateInfo]);

  const handleSaveSettings = useCallback(async () => {
    if (!settings) return;
    try {
      const payload: AppSettings = {
        ...settings,
        panel_layout: {
          previewPanelWidth,
          urlAside: urlAsidePanelSize,
          main: mainPanelSize,
        },
        saved_channels: savedChannels.map(({ loading: _loading, ...ch }) => ch),
        channel_kick_enabled: kickEnabled,
        channel_twitch_enabled: twitchEnabled,
        channel_youtube_enabled: youtubeEnabled,
        channel_content_filter: channelContentFilter,
      };
      await apiPost('/api/settings', payload);
      setSettingsSaved(true);
      setError(null);
      setTimeout(() => setSettingsSaved(false), 2000);
    } catch (err: any) {
      setError(err.message || 'Failed to save settings');
    }
  }, [
    settings,
    previewPanelWidth,
    urlAsidePanelSize,
    mainPanelSize,
    savedChannels,
    kickEnabled,
    twitchEnabled,
    youtubeEnabled,
    channelContentFilter,
  ]);

  // ── Fill VOD from channel ──
  const selectVod = useCallback((
    vodUrl: string,
    badge?: ChannelPreviewBadge,
    hint?: FetchVideoInfoHint,
  ) => {
    setUrl(vodUrl);
    setChannelVodPanelOpen(true);
    setUrlTabBarHidden(true);
    setPreviewChannelBadge(badge ?? null);
    void fetchVideoInfo(vodUrl, hint);
  }, [fetchVideoInfo]);

  const carryExploreToUrl = useCallback((vod: ExplorePopupVod) => {
    selectVod(vod.url, {
      platform: vod.platform,
      platformListIndex: vod.platformListIndex,
      isClip: vod.isClip,
    }, {
      durationSec: vod.durationSec > 0 ? vod.durationSec : undefined,
      title: vod.title,
      thumbnailUrl: vod.thumbnailUrl ?? undefined,
      createdAt: vod.created_at ?? null,
      views: vod.views ?? null,
      skipNetwork: true,
    });
  }, [selectVod]);

  const currentIsClip = isClipUrl(url);

  const urlTrimStartMax = Math.max(0, trimEndSec - 1);
  const urlTrimEndMin = Math.min(vodDurationSec, trimStartSec + 1);

  // ── Size estimate ──
  const estTrimStart = previewOpen ? previewTrimStart : trimStartSec;
  const estTrimEnd = previewOpen ? previewTrimEnd : trimEndSec;
  const effectiveTrimSec = Math.max(0, estTrimEnd - estTrimStart);
  const fullDur = videoInfo?.duration ?? 0;
  const trimActive = fullDur > 0 && (estTrimStart > 0 || estTrimEnd < fullDur);
  const clipSec = trimActive
    ? effectiveTrimSec
    : currentIsClip && fullDur > 0
      ? Math.max(1, Math.floor(fullDur))
      : effectiveTrimSec;

  const activePlatform = detectVideoPlatform(videoInfo, url);
  const layoutPlatform = useMemo(() => {
    const fromBadge = platformStyleKey(previewChannelBadge?.platform);
    if (fromBadge) return fromBadge;
    return urlPlatform || platformStyleKey(activePlatform ?? '') || null;
  }, [previewChannelBadge, urlPlatform, activePlatform]);
  const urlActionPlatform = layoutPlatform;

  const estBytes = estimateDownloadBytes(
    videoInfo,
    quality,
    clipSec,
    fullDur || clipSec,
    downloadAsAudio && Boolean(activePlatform),
  );

  const sourceQualityLabel = useMemo(
    () => sourceQualityOptionLabel(maxQualityLabelFromList(videoInfo?.qualities ?? [])),
    [videoInfo?.qualities],
  );

  const channelsSplitActive = channelVodPanelOpen && !previewOpen;
  const showUrlInSidebar = channelsSplitActive;
  const showUrlInPreviewMiddle = previewOpen;
  const urlPanelAside = showUrlInSidebar || showUrlInPreviewMiddle;
  const splitLayout = urlPanelAside;
  const triplePanelLayout = previewOpen && urlPanelAside;
  const showUrlInMainCard = tab === 'url' && !urlPanelAside && !urlTabBarHidden;
  const urlMainCompact = showUrlInMainCard && Boolean(videoInfo);
  const mainCardHeaderCompact = triplePanelLayout || urlMainCompact;
  const visibleTabs: Tab[] = urlPanelAside || urlTabBarHidden
    ? ['channels', 'queue', 'settings']
    : ['url', 'channels', 'queue', 'settings'];

  useEffect(() => {
    if ((urlPanelAside || urlTabBarHidden) && tab === 'url') {
      setTab('channels');
    }
  }, [urlPanelAside, urlTabBarHidden, tab]);

  const urlFetched = Boolean(videoInfo);
  const extractBtnHoverClass = urlPanelAside && !previewOpen
    ? actionBtnHover(null)
    : actionBtnHover(urlActionPlatform);
  const urlInputClass = urlFetched
    ? 'w-full bg-zinc-950 border border-zinc-800 text-zinc-400 font-mono placeholder:text-zinc-600 pl-7 pr-7 py-1 focus:outline-none focus:border-zinc-500 transition-colors text-[10px] truncate'
    : 'w-full bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 pl-10 pr-10 py-3 focus:outline-none focus:border-white transition-colors uppercase text-sm';

  const videoInfoThumbSrc = useMemo(() => {
    const fromInfo = resolveVideoThumbnail(videoInfo?.thumbnail, 48, 36);
    if (fromInfo) return fromInfo;
    const cached = findCachedVideoThumbnail(url, savedChannels);
    return resolveVideoThumbnail(cached, 48, 36);
  }, [videoInfo?.thumbnail, url, savedChannels]);

  const previewPosterSrc = useMemo(() => {
    const fromInfo = resolveVideoThumbnail(videoInfo?.thumbnail, 640, 360);
    if (fromInfo) return fromInfo;
    const cached = findCachedVideoThumbnail(url, savedChannels);
    return resolveVideoThumbnail(cached, 640, 360);
  }, [videoInfo?.thumbnail, url, savedChannels]);

  useEffect(() => {
    setVideoInfoThumbFailed(false);
  }, [videoInfoThumbSrc]);

  const urlTabContent = (
    <div className="flex flex-col gap-2 min-h-0 h-full">
      <div className="flex flex-col gap-1 shrink-0">
        <div className="relative group">
          <div className={`absolute inset-y-0 left-0 flex items-center pointer-events-none text-white/40 ${urlFetched ? 'pl-2' : 'pl-3'}`}>
            <Link2 size={urlFetched ? 12 : 18} strokeWidth={urlFetched ? 2 : 3} />
          </div>
          <input
            type="text"
            value={url}
            onChange={(e) => {
              const v = e.target.value;
              setUrl(v);
              setPreviewChannelBadge(null);
              const trimmed = v.trim();
              // ponytail: debounce warm calls so a paste/typing doesn't fire on every keystroke.
              if (urlWarmTimerRef.current != null) {
                window.clearTimeout(urlWarmTimerRef.current);
                urlWarmTimerRef.current = null;
              }
              if (detectUrlPlatform(trimmed) === 'youtube' && !isClipUrl(trimmed) && trimmed.length >= 12) {
                urlWarmTimerRef.current = window.setTimeout(() => {
                  urlWarmTimerRef.current = null;
                  warmYoutubePreview(trimmed);
                  // ponytail: also queue a full-VOD mux so first preview open is instant.
                  warmYoutubePreviewFull(trimmed, 1500, 720);
                }, 300);
              }
            }}
            placeholder={urlFetched ? 'VOD or clip link' : 'PASTE VOD OR CLIP LINK...'}
            onKeyDown={(e) => e.key === 'Enter' && handleGetInfo()}
            className={urlInputClass}
          />
          {url && (
            <button type="button" onClick={() => setUrl('')}
              className={`absolute inset-y-0 right-0 flex items-center text-zinc-500 hover:text-white ${urlFetched ? 'pr-2' : 'pr-3'}`}>
              <X size={urlFetched ? 12 : 18} strokeWidth={urlFetched ? 2 : 3} />
            </button>
          )}
        </div>

        {!videoInfo && (
          <button
            onClick={handleGetInfo}
            disabled={!url || loading}
            className={`w-full bg-zinc-800 text-white font-black uppercase py-3 flex items-center justify-center gap-2 transition-all duration-300 disabled:opacity-50 disabled:cursor-default border-2 border-zinc-700 ${extractBtnHoverClass}`}
          >
            {loading ? (
              <><Loader2 size={16} className="animate-spin" /> Loading...</>
            ) : (
              <><Info size={16} strokeWidth={3} /> Extract Info</>
            )}
          </button>
        )}

        {videoInfo && loading && (
          <div className="flex items-center justify-center gap-2 py-1 text-[10px] font-mono text-zinc-500">
            <Loader2 size={12} className="animate-spin" />
            Updating…
          </div>
        )}
      </div>

      {videoInfo && (
        <div className="flex flex-col gap-2 shrink-0">
          <div className="border border-zinc-800 p-2 flex gap-2 bg-zinc-900/80 relative overflow-hidden shrink-0">
            <div className={`absolute top-0 right-0 w-10 h-10 opacity-15 blur-xl ${
              videoInfo.platform?.toLowerCase() === 'kick'
                ? 'bg-[#53fc18]'
                : videoInfo.platform?.toLowerCase() === 'youtube'
                  ? 'bg-[#F03030]'
                  : 'bg-[#9146FF]'
            }`} />
            <div className="w-12 h-9 bg-zinc-800 border border-zinc-700 flex items-center justify-center shrink-0 overflow-hidden">
              {videoInfoThumbSrc && !videoInfoThumbFailed ? (
                <img
                  src={videoInfoThumbSrc}
                  alt=""
                  className="w-full h-full object-cover"
                  onError={() => setVideoInfoThumbFailed(true)}
                />
              ) : (
                <Play size={12} className="text-zinc-500" />
              )}
            </div>
            <div className="flex flex-col justify-center overflow-hidden w-full min-w-0 gap-0.5">
              <h3 className="font-bold truncate uppercase text-[10px] leading-tight">
                {videoInfo.title || 'Untitled'}
              </h3>
              <p className="text-[9px] text-zinc-500 font-mono truncate">
                {videoInfo.uploader || 'Unknown'}
                {videoInfo.created_at ? ` · ${fmtDateAndAgo(videoInfo.created_at)}` : ''}
              </p>
              <div className="flex justify-between items-center gap-1 text-[9px] font-mono text-zinc-500">
                <span className="flex items-center gap-1 truncate">
                  <Clock size={9} /> {videoInfo.duration_string || fmtDuration(videoInfo.duration || 0)}
                  {videoInfo.views != null && Number(videoInfo.views) > 0 ? (
                    <span className="flex items-center gap-0.5 text-zinc-400">
                      <Eye size={9} /> {fmtViews(Number(videoInfo.views))}
                    </span>
                  ) : null}
                </span>
                <span className="flex items-center gap-0.5 shrink-0 text-zinc-300">
                  <Database size={9} className={
                    videoInfo.platform?.toLowerCase() === 'kick'
                      ? 'text-[#53fc18]'
                      : videoInfo.platform?.toLowerCase() === 'youtube'
                        ? 'text-[#F03030]'
                        : 'text-[#9146FF]'
                  } /> {formatBytes(estBytes)}
                </span>
              </div>
            </div>
          </div>

          {pendingAddChannel && (
            <ChannelLinkCard
              draft={pendingAddChannel}
              onChange={setPendingAddChannel}
              onConfirm={() => void commitChannelLink()}
              onCancel={() => setPendingAddChannel(null)}
              duplicateMessage={channelLinkDuplicate}
              className="shrink-0"
            />
          )}

          <div className="grid grid-cols-2 gap-2 shrink-0">
            <div className="flex flex-col gap-0.5">
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">Quality</span>
                <select value={quality} onChange={(e) => setQuality(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono py-1 px-1.5 focus:outline-none focus:border-white text-[10px] cursor-pointer">
                {/* Always offer quality tiers so users can pick higher resolutions.
                    Backend fetches the requested height on demand (yt-dlp format filter).
                    When the API returned specific tiers we list those; otherwise the
                    standard ladder is shown as a fallback. */}
                <option value="source">{sourceQualityLabel}</option>
                {['1080p', '720p', '480p', '360p'].map((q) => {
                  const lower = q.toLowerCase();
                  const haveIt = (videoInfo.qualities || []).some((x) => x.toLowerCase() === lower);
                  if (haveIt) return null; // already listed below from API
                  return <option key={q} value={lower}>{q}</option>;
                })}
                {(videoInfo.qualities || []).map((q) => (
                  <option key={q} value={q.toLowerCase()}>{q}</option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">Est. size</span>
              <div className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono py-1 px-1.5 text-[10px] flex items-center justify-center">
                {formatBytes(estBytes)}
              </div>
            </div>
          </div>

          {!currentIsClip && activePlatform && (
            <label className="flex items-center gap-2 text-[10px] font-mono text-zinc-400 shrink-0 cursor-pointer hover:text-zinc-200">
              <input
                type="checkbox"
                checked={downloadAsAudio}
                onChange={(e) => setDownloadAsAudio(e.target.checked)}
                className="shrink-0"
                style={vodCheckboxStyle(platformAccentColor(activePlatform))}
              />
              Audio only (MP3)
            </label>
          )}

          <div className="flex flex-col gap-2.5 shrink-0 py-0.5">
            <div className="flex justify-between items-center gap-2">
              <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500 shrink-0">Trim</span>
              <ClipDurationAdjustButtons
                onAdjust={adjustUrlClipDuration}
                activeEndpoint={lastUrlTrimEndpoint}
                disabled={vodDurationSec <= 0 || trimEndSec <= trimStartSec}
              />
              <span className="text-xs font-mono text-zinc-400 shrink-0">{formatHmsFull(trimEndSec - trimStartSec)}</span>
            </div>
            <div className="flex justify-between text-xs font-mono text-white px-0.5">
              <EditableHmsTime
                valueSec={trimStartSec}
                minSec={0}
                maxSec={Math.max(0, Math.min(urlTrimStartMax, vodDurationSec - 1))}
                onChange={(sec) => handleUrlTrimSlider('in', sec)}
              />
              <EditableHmsTime
                valueSec={trimEndSec}
                minSec={urlTrimEndMin}
                maxSec={vodDurationSec}
                onChange={(sec) => handleUrlTrimSlider('out', sec)}
                className="text-zinc-500"
              />
            </div>
            <input type="range" min={0} max={vodDurationSec} step={1} value={trimStartSec}
              onPointerDown={(e) => {
                markUrlTrimEndpoint('in');
                e.currentTarget.setPointerCapture(e.pointerId);
                trimDragActiveRef.current = true;
                urlTrimDragPinRef.current = {
                  which: 'in',
                  fixedStart: trimStartSecRef.current,
                  fixedEnd: trimEndSecRef.current,
                };
                trimDragOriginRef.current = trimStartSecRef.current;
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
                if (previewFsHideTimerRef.current) window.clearTimeout(previewFsHideTimerRef.current);
                setPreviewFsControlsVisible(true);
              }}
              onPointerMove={(e) => {
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
              }}
              onInput={(e) => {
                handleUrlTrimSlider(
                  'in',
                  Number((e.target as HTMLInputElement).value),
                  urlTrimPointerRef.current,
                );
              }}
              onPointerUp={(e) => {
                try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
                finishUrlTrimDrag();
              }}
              onPointerCancel={(e) => {
                try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
                finishUrlTrimDrag();
              }}
              className="url-trim-range w-full accent-zinc-400" />
            <input type="range" min={0} max={vodDurationSec} step={1} value={trimEndSec}
              onPointerDown={(e) => {
                markUrlTrimEndpoint('out');
                e.currentTarget.setPointerCapture(e.pointerId);
                trimDragActiveRef.current = true;
                urlTrimDragPinRef.current = {
                  which: 'out',
                  fixedStart: trimStartSecRef.current,
                  fixedEnd: trimEndSecRef.current,
                };
                trimDragOriginRef.current = trimEndSecRef.current;
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
                if (previewFsHideTimerRef.current) window.clearTimeout(previewFsHideTimerRef.current);
                setPreviewFsControlsVisible(true);
              }}
              onPointerMove={(e) => {
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
              }}
              onInput={(e) => {
                handleUrlTrimSlider(
                  'out',
                  Number((e.target as HTMLInputElement).value),
                  urlTrimPointerRef.current,
                );
              }}
              onPointerUp={(e) => {
                try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
                finishUrlTrimDrag();
              }}
              onPointerCancel={(e) => {
                try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
                finishUrlTrimDrag();
              }}
              className="url-trim-range w-full accent-zinc-400" />
          </div>
        </div>
      )}
    </div>
  );

  const urlAsideActionBar = videoInfo ? (
    <div className={`${urlPanelAside ? 'flex-1' : 'shrink-0'} min-h-[6.5rem] flex flex-col gap-2 shrink-0 pt-2 border-t border-zinc-800 overflow-hidden`}>
      <button
        type="button"
        onClick={() => {
          const trimmed = url.trim();
          if (trimmed) window.open(trimmed, '_blank', 'noopener,noreferrer');
        }}
        disabled={!url.trim()}
        className={`flex-1 min-h-0 ${platformWatchPreviewBtn(urlActionPlatform, false)} disabled:opacity-40`}
      >
        <ExternalLink size={12} className="shrink-0" />
        Open URL
      </button>
      <button
        type="button"
        onMouseEnter={() => {
          const trimmed = url.trim();
          if (detectUrlPlatform(trimmed) === 'youtube' && !isClipUrl(trimmed)) {
            warmYoutubePreview(trimmed);
          }
        }}
        onClick={openPreview}
        disabled={
          previewVideoLoading
          || loading
          || vodDurationSec <= 0
          || trimEndSec <= trimStartSec
          || (url.trim() !== '' && videoInfoUrl !== url.trim())
        }
        className={`flex-1 min-h-0 ${platformWatchPreviewBtn(urlActionPlatform, previewOpen)} disabled:opacity-40`}
      >
        {previewVideoLoading ? (
          <Loader2 size={12} className="animate-spin shrink-0" />
        ) : (
          <Play size={12} fill="currentColor" className="shrink-0" />
        )}
        Watch preview
      </button>
      <button
        onClick={promptStartDownload}
        disabled={loading || !videoInfo}
        className={`flex-1 min-h-0 ${platformVodPanelBtn(urlActionPlatform)}`}
      >
        <Download size={16} strokeWidth={3} />
        <span className="inline-flex items-center">
          <span className="tracking-widest">{currentIsClip ? 'Clip rip it' : 'VOD rip it'}</span>
          <span className="rip-btn-bang" aria-hidden="true">!</span>
        </span>
      </button>
    </div>
  ) : null;

  const previewCtrlBtn = (fsOverlay: boolean, large = false) =>
    platformPreviewCtrlBtn(layoutPlatform, fsOverlay, large);

  const renderVolumeControl = (opts: {
    volume: number;
    muted: boolean;
    menuOpen: boolean;
    setMenuOpen: Dispatch<SetStateAction<boolean>>;
    onVolumeChange: (level: number) => void;
    disabled: boolean;
    buttonClassName: string;
    popoverFs?: boolean;
    onMenuOpen?: () => void;
  }) => {
    const displayVol = opts.muted ? 0 : opts.volume;
    const popoverClass = opts.popoverFs
      ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
      : 'border-2 border-zinc-600 bg-zinc-950';
    return (
      <div className="relative" data-player-menu>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            opts.onMenuOpen?.();
            opts.setMenuOpen((o) => !o);
          }}
          disabled={opts.disabled}
          className={opts.buttonClassName}
          title="Volume"
        >
          {opts.muted || opts.volume <= 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
        </button>
        {opts.menuOpen && (
          <div
            className={`absolute bottom-full left-0 mb-1.5 z-30 flex items-center gap-2 px-2.5 py-2 shadow-lg ${popoverClass}`}
            onClick={(e) => e.stopPropagation()}
          >
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={displayVol}
              disabled={opts.disabled}
              onChange={(e) => opts.onVolumeChange(parseFloat(e.target.value))}
              className={`w-24 accent-white ${opts.popoverFs ? 'h-1' : 'h-1.5'}`}
            />
          </div>
        )}
      </div>
    );
  };

  const previewClipPct = vodDurationSec > 0
    ? {
        start: (previewTrimStart / vodDurationSec) * 100,
        end: (previewTrimEnd / vodDurationSec) * 100,
        play: (previewTimeUi / vodDurationSec) * 100,
      }
    : { start: 0, end: 100, play: 0 };

  const previewTimelineUi = (
    <div className="flex flex-col gap-0.5 w-full"
      style={trimPanelHeight > 0 ? { height: trimPanelHeight + 'px' } : undefined}>
      {vodDurationSec > 0 && (
        <div className="flex items-stretch gap-2 flex-1 min-h-0">
          <span className={`text-[8px] font-mono uppercase w-11 shrink-0 tracking-wider self-center ${
            previewFullscreen ? 'text-zinc-400' : 'text-zinc-600'
          }`}>
            Clip
          </span>
          <div
            ref={previewNeedleRailRef}
            className={`preview-needle-rail relative flex-1 ${
              previewFullscreen ? 'bg-white/10' : 'bg-zinc-800/80'
            }`}
            title="Drag needles to set preview clip range"
            onClick={(e) => {
              if (e.target !== e.currentTarget) return;
              const rail = previewNeedleRailRef.current;
              if (!rail || vodDurationSec <= 0) return;
              const rect = rail.getBoundingClientRect();
              const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
              seekPreviewVideo(frac * vodDurationSec);
            }}
          >
            <div
              className="preview-needle-region absolute top-1/2 -translate-y-1/2 h-1 pointer-events-none"
              style={{
                left: `${previewClipPct.start}%`,
                width: `${Math.max(0, previewClipPct.end - previewClipPct.start)}%`,
              }}
            />
            <div
              ref={previewPlayheadRef}
              className="preview-needle-playhead absolute top-0 bottom-0 w-px bg-white/50 -translate-x-1/2 pointer-events-none z-[1]"
              style={{ left: `${previewClipPct.play}%` }}
            />
            <div
              role="slider"
              aria-label="Clip in"
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={previewTrimStart}
              className="preview-needle preview-needle-in absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${previewClipPct.start}%` }}
              onPointerDown={(e) => beginPreviewNeedleDrag(e, 'in')}
            />
            <div
              role="slider"
              aria-label="Clip out"
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={previewTrimEnd}
              className="preview-needle preview-needle-out absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${previewClipPct.end}%` }}
              onPointerDown={(e) => beginPreviewNeedleDrag(e, 'out')}
            />
        </div>
          <ClipDurationAdjustButtons
            compact
            onAdjust={adjustPreviewClipDuration}
            activeEndpoint={lastPreviewTrimEndpoint}
            disabled={vodDurationSec <= 0 || previewTrimEnd <= previewTrimStart}
          />
          <span
            className={`text-[8px] font-mono w-11 shrink-0 text-right ${
              previewFullscreen ? 'text-zinc-300/90' : 'text-zinc-500'
            }`}
            title="Selected clip length"
          >
            {formatHmsFull(previewClipLengthSec)}
          </span>
        </div>
      )}
      {vodDurationSec > 0 && (
        <div
          className="h-2 cursor-ns-resize flex items-center justify-center gap-1 select-none shrink-0 hover:bg-zinc-800/50 rounded"
          onMouseMove={previewFullscreen ? bumpPreviewFsControls : undefined}
          onPointerDown={(e) => {
            e.preventDefault();
            e.currentTarget.setPointerCapture(e.pointerId);
            trimPanelResizeRef.current = { startY: e.clientY, startHeight: trimPanelHeight };
            trimDragActiveRef.current = true;
            bumpPreviewFsControls();
          }}
          onPointerMove={(e) => {
            if (!trimPanelResizeRef.current) return;
            const startY = trimPanelResizeRef.current.startY;
            const startH = trimPanelResizeRef.current.startHeight;
            const delta = e.clientY - startY;
            const minH = previewFullscreen ? 60 : 40;
            const maxH = previewFullscreen ? Math.floor(window.innerHeight * 0.5) : Infinity;
            const h = Math.min(maxH, Math.max(minH, startH - delta));
            setTrimPanelHeight(h);
          }}
          onPointerUp={(e) => {
            trimPanelResizeRef.current = null;
            trimDragActiveRef.current = false;
            try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
          }}
          onPointerCancel={(e) => {
            trimPanelResizeRef.current = null;
            trimDragActiveRef.current = false;
            try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
          }}
        >
          <span className="w-8 h-0.5 rounded-full bg-zinc-600" />
        </div>
      )}
      <div className="flex items-center gap-2">
        <span className={`text-[9px] font-mono w-11 shrink-0 ${previewFullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}>
          {formatHmsFull(Math.max(0, previewTimeUi - previewTrimStart))}
        </span>
        <input
          type="range"
          min={previewTrimStart}
          max={previewTrimEnd}
          step={0.25}
          value={Math.min(Math.max(previewTimeUi, previewTrimStart), previewTrimEnd)}
          disabled={!previewVideoReady || previewClipLengthSec <= 0}
          onChange={(e) => seekPreviewVideo(parseFloat(e.target.value))}
          className="flex-1 accent-white disabled:opacity-40"
        />
        <span
          className={`text-[9px] font-mono w-11 shrink-0 text-right ${previewFullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}
          title="Selected clip length"
        >
          {formatHmsFull(previewClipLengthSec)}
        </span>
      </div>
    </div>
  );

  const previewTransportUi = (opts: { fsCornerExit?: boolean }) => (
    <div className="flex items-center gap-2 justify-between">
      <div className="flex items-center gap-1.5">
        <button type="button" onClick={togglePreviewPlay}
          disabled={!previewVideoReady}
          className={previewCtrlBtn(previewFullscreen, true)}>
          {previewPlaying ? <Pause size={18} /> : <Play size={18} />}
        </button>
        {renderVolumeControl({
          volume: previewVolume,
          muted: previewMuted,
          menuOpen: previewVolumeMenuOpen,
          setMenuOpen: setPreviewVolumeMenuOpen,
          onVolumeChange: setPreviewVolumeLevel,
          disabled: !previewVideoReady,
          buttonClassName: previewCtrlBtn(previewFullscreen, true),
          popoverFs: previewFullscreen,
          onMenuOpen: () => setPreviewQualityMenuOpen(false),
        })}
      </div>
      <div className="flex items-center gap-1.5 ml-auto relative z-20 overflow-visible">
        <PreviewQualityMenu
          levels={previewLevels}
          currentLevel={previewQualityLevel}
          menuOpen={previewQualityMenuOpen}
          setMenuOpen={setPreviewQualityMenuOpen}
          onSelect={applyPreviewQuality}
          disabled={!previewVideoReady}
          buttonClassName={previewCtrlBtn(previewFullscreen)}
          onMenuOpen={() => setPreviewVolumeMenuOpen(false)}
          popoverPlacement="up"
          popoverClassName={previewFullscreen
            ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
            : 'border-2 border-zinc-600 bg-zinc-950'}
        />
        {opts.fsCornerExit ? (
          <button type="button" onClick={() => void togglePreviewFullscreen()}
            disabled={!previewVideoReady}
            className={previewCtrlBtn(false, true)}
            title="Exit fullscreen">
            <Minimize2 size={18} />
          </button>
        ) : (
          <button type="button" onClick={() => void togglePreviewFullscreen()}
            disabled={!previewVideoReady}
            className={previewCtrlBtn(false, true)}
            title="Fullscreen">
            <Maximize2 size={18} />
          </button>
        )}
      </div>
    </div>
  );

  const edgePinnedRow = triplePanelLayout;
  const rowEdgeInsets = edgePinnedRow ? layoutRowEdgeInsets() : null;

  return (
    <div
      className="vod-app-shell h-screen max-h-screen min-h-0 flex justify-center items-center overflow-hidden p-4 selection:bg-white selection:text-black bg-[#09090b]"
      style={{
        backgroundImage: 'radial-gradient(#27272a 1px, transparent 1px)',
        backgroundSize: 'calc(24px * var(--ui-scale)) calc(24px * var(--ui-scale))',
      }}
    >
      <div
        className={`vod-layout-row flex items-start max-w-full min-w-0 w-full justify-center ${
        triplePanelLayout || splitLayout
          ? viewportTier === 'narrow'
            ? 'gap-2'
            : triplePanelLayout
              ? 'gap-3'
              : 'gap-6'
          : viewportTier === 'wide'
            ? 'max-w-lg gap-6'
            : 'max-w-md gap-6'
      }`}
        style={rowEdgeInsets ? { width: rowEdgeInsets.usableWidth, maxWidth: rowEdgeInsets.usableWidth } : undefined}
      >
      {previewOpen && (
        <div
          ref={previewPanelRef}
          className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white p-4 flex flex-col gap-3 min-h-0 min-w-0 ${platformCardShadow(layoutPlatform, true)}`}
          style={{ width: previewPanelWidth }}
        >
          <div className="flex items-start justify-between gap-2 shrink-0">
            {previewChannelBadge ? (
              <div className="flex items-start gap-1.5 min-w-0">
                <ChannelListIndexBadge
                  platform={previewChannelBadge.platform}
                  index={previewChannelBadge.platformListIndex}
                  size="md"
                />
                <div className="min-w-0">
                  <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
                    {previewChannelBadge.isClip ? 'Channel clip preview' : 'Channel VOD preview'}
                  </span>
                  {videoInfo?.title && (
                    <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
                      {videoInfo.title}
                    </p>
                  )}
                </div>
              </div>
            ) : (
              <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 pt-0.5">
                Preview
              </span>
            )}
            <button type="button" onClick={() => void resetPreview()} className="text-zinc-500 hover:text-white p-1 shrink-0">
              <X size={18} />
            </button>
          </div>
          <div className="flex flex-col gap-2 w-full" data-preview-panel>
            <div
              ref={previewContainerRef}
              tabIndex={0}
              role="application"
              aria-label="Trim preview player"
              onKeyDown={handlePreviewContainerKeyDown}
              onMouseMove={previewFullscreen ? bumpPreviewFsControls : undefined}
              onMouseLeave={previewFullscreen ? () => {
                if (trimDragActiveRef.current) return;
                if (previewFsHideTimerRef.current) window.clearTimeout(previewFsHideTimerRef.current);
                previewFsHideTimerRef.current = window.setTimeout(() => {
                  if (!trimDragActiveRef.current) {
                    setPreviewFsControlsVisible(false);
                  }
                }, PREVIEW_FS_CONTROLS_HIDE_MS);
              } : undefined}
              onFocus={focusPreviewPlayer}
              className={`preview-fs-host outline-none focus:ring-2 focus:ring-white/30 bg-black overflow-hidden flex flex-col ${
                previewFullscreen
                  ? 'relative border-0'
                  : 'relative w-full shrink-0 border-2 border-zinc-700'
              }`}
              style={!previewFullscreen ? { aspectRatio: previewVideoAspect, maxHeight: previewVideoAspect < 1 ? '80vh' : undefined, transition: 'max-height 0.3s ease' } : undefined}
            >
              <div
                className="relative bg-black overflow-hidden cursor-pointer absolute inset-0 z-0"
                onClick={() => {
                  focusPreviewPlayer();
                  togglePreviewPlay();
                }}
              >
                {previewYoutubeEmbedUrl ? (
                  <>
                    <iframe
                      ref={previewYoutubeIframeRef}
                      className="youtube-embed-frame pointer-events-none"
                      src={previewYoutubeEmbedUrl}
                      title="YouTube trim preview"
                      allow="autoplay; encrypted-media; picture-in-picture"
                      tabIndex={-1}
                      onLoad={() => {
                        setPreviewVideoReady(true);
                        setPreviewVideoLoading(false);
                        youtubeIframeListen(previewYoutubeIframeRef.current);
                        postYoutubePreviewCommand('setVolume', [Math.round(previewVolumeRef.current * 100)]);
                      }}
                    />
                    <div className="absolute inset-0 z-[1]" aria-hidden="true" />
                  </>
                ) : (
                  <video
                    ref={previewVideoRef}
                    className="w-full h-full object-contain pointer-events-none"
                    muted={previewMuted}
                    playsInline
                    poster={previewPosterSrc || videoInfoThumbSrc || undefined}
                    onLoadedMetadata={handlePreviewLoadedMetadata}
                    onTimeUpdate={handlePreviewTimeUpdate}
                    onPlay={() => {
                      if (previewSuppressPlayRef.current) {
                        previewVideoRef.current?.pause();
                        return;
                      }
                      setPreviewPlaying(true);
                    }}
                    onPause={() => setPreviewPlaying(false)}
                  />
                )}
                {previewVideoLoading && !previewVideoReady && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/60 z-20 pointer-events-none">
                    <Loader2 size={40} className="animate-spin text-zinc-300" />
                    {!previewPlayback && (
                      <span className="text-zinc-300 text-xs font-mono">
                        {urlPlatform === 'youtube' ? 'Starting YouTube preview…' : 'Preparing preview…'}
                      </span>
                    )}
                  </div>
                )}
                {previewBuffering && previewVideoReady && !previewVideoLoading && (
                  <div className="absolute inset-0 flex items-center justify-center bg-black/35 z-20 pointer-events-none">
                    <Loader2 size={32} className="animate-spin text-zinc-200/90" />
                  </div>
                )}
              </div>
              <div
                ref={previewControlsRef}
                data-player-controls
                data-preview-fs-ui={previewFullscreen ? '' : undefined}

                className={
                  previewFullscreen
                    ? `absolute bottom-0 left-0 right-0 z-10 flex flex-col gap-1 px-2 pb-2 pt-2 max-h-[50vh] overflow-x-hidden overflow-y-visible bg-gradient-to-t from-black/90 to-black/75 transition-opacity duration-150 ${
                      previewFsControlsVisible ? 'opacity-100' : 'opacity-0 pointer-events-none'
                    }`
                    : 'absolute bottom-0 left-0 right-0 z-10 flex flex-col gap-1.5 px-2 pb-2 pt-2 bg-gradient-to-t from-black/80 to-black/50'
                }
                onClick={previewFullscreen ? (e) => e.stopPropagation() : undefined}
                onPointerDown={previewFullscreen ? (e) => e.stopPropagation() : undefined}
                onPointerUp={previewFullscreen ? (e) => e.stopPropagation() : undefined}
                onMouseMove={previewFullscreen ? bumpPreviewFsControls : undefined}
              >
                {previewTimelineUi}
                {previewTransportUi({ fsCornerExit: previewFullscreen })}
              </div>
              {previewFullscreen && (
                <div
                  className="absolute bottom-0 right-0 z-30 w-10 h-10 cursor-pointer"
                  title="Exit fullscreen"
                  onClick={() => void togglePreviewFullscreen()}
                />
              )}
            </div>
          </div>
          {!previewFullscreen && (
            <PanelResizeHandles onPointerDown={onPreviewPanelResize} insetPx={panelResizeHandleInset(true)} />
          )}
        </div>
      )}
      {(showUrlInSidebar || showUrlInPreviewMiddle) && (
        <div
          ref={urlAsidePanelRef}
          className={`group relative shrink-0 overflow-hidden bg-zinc-950 border-2 border-white p-4 flex flex-col gap-2 min-h-0 ${platformCardShadow(layoutPlatform, true)}`}
          style={{ width: urlAsidePanelSize.w, height: urlAsidePanelSize.h }}
        >
          {showUrlInSidebar && (
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">Selected VOD</span>
              <button
                type="button"
                onClick={() => {
                  setChannelVodPanelOpen(false);
                  setVideoInfo(null);
                  setUrl('');
                  setPreviewChannelBadge(null);
                }}
                className="text-zinc-500 hover:text-white p-1"
                title="Clear selection"
              >
                <X size={14} />
              </button>
            </div>
          )}
          {showUrlInPreviewMiddle && (
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">VOD · Trim</span>
            </div>
          )}
          <div className="flex-[2] min-h-0 overflow-hidden flex flex-col">
            {urlTabContent}
          </div>
          {urlAsideActionBar}
          <PanelResizeHandles onPointerDown={onUrlAsidePanelResize} insetPx={panelResizeHandleInset(true)} />
        </div>
      )}
      <div
        ref={mainPanelRef}
        className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white flex flex-col min-h-0 ${
          triplePanelLayout ? 'p-4 gap-3' : urlMainCompact ? 'p-4 gap-2' : 'p-6 gap-4'
        } ${platformCardShadow(layoutPlatform)}`}
        style={{ width: mainPanelSize.w, height: mainPanelSize.h }}
      >

        {/* ── HEADER ── */}
        <div className="flex justify-between items-start shrink-0 min-w-0 gap-2">
          <div className="flex flex-col min-w-0">
            <h1 className={`font-black uppercase tracking-tighter truncate ${
              mainCardHeaderCompact ? 'text-2xl' : 'text-4xl md:text-5xl'
            }`}>
              VOD<span className="text-[#9146FF]">.</span>RIP
            </h1>
            {!mainCardHeaderCompact && (
              <p className="text-zinc-400 text-[10px] font-mono tracking-widest uppercase mt-1">
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> {'//'} <span className="text-[#F03030]">YouTube</span> Downloader
              </p>
            )}
            {triplePanelLayout && !urlMainCompact && (
              <p className="text-zinc-500 text-[9px] font-mono tracking-widest uppercase mt-0.5 truncate">
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> {'//'} <span className="text-[#F03030]">YouTube</span>
              </p>
            )}
          </div>
          <div className={`flex gap-1 shrink-0 ${mainCardHeaderCompact ? 'mt-1' : 'mt-2'}`}>
            <div className="w-2 h-2 bg-[#53fc18] rounded-full animate-pulse" />
            <div className="w-2 h-2 bg-[#9146FF] rounded-full animate-pulse" style={{ animationDelay: '0.5s' }} />
            <div className="w-2 h-2 bg-[#F03030] rounded-full animate-pulse" style={{ animationDelay: '1s' }} />
          </div>
        </div>

        {/* ── TABS ── */}
        <div className="flex w-full border-2 border-zinc-800 font-mono text-[10px] uppercase font-bold tracking-widest shrink-0">
          {visibleTabs.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 text-center transition-all flex items-center justify-center gap-2 ${
                mainCardHeaderCompact ? 'py-2' : 'py-3'
              } ${
                tab === t ? 'bg-white text-black' : 'bg-transparent text-zinc-500 hover:text-white'
              }`}
            >
              {t === 'url' && <Link2 size={14} />}
              {t === 'channels' && <Users size={14} />}
              {t === 'queue' && <Download size={14} />}
              {t === 'settings' && <Settings2 size={14} />}
              {t === 'url' ? 'URL' : t === 'channels' ? 'CHANNELS' : t === 'queue' ? 'QUEUE' : 'SETTINGS'}
            </button>
          ))}
        </div>

        {/* ── ERROR ── */}
        {error && (
          <div className="border-2 border-red-500/75 bg-red-500/15 p-3 text-red-300 text-xs font-mono flex items-center gap-2 shrink-0">
            <AlertCircle size={14} />
            {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400/60 hover:text-red-400">
              <X size={14} />
            </button>
          </div>
        )}

        <div
          ref={channelsScrollRef}
          className={`flex-1 min-h-0 ${
          showUrlInMainCard
            ? 'overflow-hidden flex flex-col'
            : 'overflow-y-auto overflow-x-hidden custom-scrollbar pr-1 pb-2 overscroll-y-contain'
        }`}>
        {/* ════════════════════════════ URL TAB ════════════════════════════ */}
        {showUrlInMainCard && (
          <>
            {urlTabContent}
            {urlAsideActionBar}
          </>
        )}

        {/* ════════════════════════════ CHANNELS TAB ════════════════════════════ */}
          {tab === 'channels' && (
          <div className="flex flex-col gap-3 min-w-0">
            <div className="flex gap-2">
              <input type="text" value={addChannelInput}
                onChange={(e) => setAddChannelInput(e.target.value)}
                placeholder="KICK / TWITCH / YOUTUBE NAME OR URL..."
                onKeyDown={(e) => e.key === 'Enter' && handleAddChannel()}
                className="flex-1 bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 px-2 py-1.5 focus:outline-none focus:border-white uppercase text-[10px] min-h-0" />
              <button type="button" onClick={handleAddChannel}
                disabled={channelsLoading || !addChannelInput.trim()}
                className="bg-white text-black font-black uppercase px-3 text-xs border-2 border-white disabled:opacity-50">
                <Plus size={14} />
              </button>
            </div>
            {pendingAddChannel && tab === 'channels' && (
              <ChannelLinkCard
                draft={pendingAddChannel}
                onChange={setPendingAddChannel}
                onConfirm={() => void commitChannelLink()}
                onCancel={() => setPendingAddChannel(null)}
                duplicateMessage={channelLinkDuplicate}
              />
            )}
            {addChannelNotice && (
              <p className="text-amber-400 text-[10px] font-mono">{addChannelNotice}</p>
            )}

            {savedChannels.length > 0 && (
              <div ref={channelListRef} className="flex flex-col gap-1">
                {savedChannels.map((ch, index) => {
                  const dropAbove = channelDragId != null
                    && channelDropInsertIndex === index;
                  const dropBelow = channelDragId != null
                    && channelDropInsertIndex === savedChannels.length
                    && index === savedChannels.length - 1;
                  return (
                  <Fragment key={ch.id}>
                  <div
                    data-channel-row
                    data-channel-id={ch.id}
                    className={`relative flex items-center gap-1 border px-2 py-1 overflow-visible ${
                      ch.id === selectedChannelId ? 'border-white bg-zinc-900' : 'border-zinc-800'
                    } ${ch.id === channelDragId ? 'opacity-45' : ''} ${
                      dropAbove ? 'shadow-[inset_0_2px_0_0_rgba(255,255,255,0.95)]' : ''
                    } ${dropBelow ? 'shadow-[inset_0_-2px_0_0_rgba(255,255,255,0.95)]' : ''}`}
                  >
                    <button
                      type="button"
                      title="Drag to reorder"
                      aria-label={`Reorder ${ch.displayName}`}
                      disabled={editingChannelId === ch.id}
                      onPointerDown={(e) => {
                        if (editingChannelId === ch.id) return;
                        setChannelDropInsertIndex(index);
                        startChannelReorderDrag(
                          e,
                          ch.id,
                          channelListRef,
                          setSavedChannels,
                          setChannelDragId,
                          setChannelDropInsertIndex,
                        );
                      }}
                      className="shrink-0 text-zinc-600 hover:text-zinc-300 p-0.5 cursor-grab active:cursor-grabbing touch-none disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      <GripVertical size={12} />
                    </button>
                    {editingChannelId === ch.id ? (
                      <input type="text" value={editingChannelName}
                        onChange={(e) => setEditingChannelName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') commitRenameChannel();
                          if (e.key === 'Escape') setEditingChannelId(null);
                        }}
                        onBlur={commitRenameChannel}
                        autoFocus
                        className="flex-1 min-w-0 bg-zinc-950 text-white font-mono text-xs px-1 py-0.5 focus:outline-none" />
                    ) : (
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => toggleChannelSelection(ch.id)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            toggleChannelSelection(ch.id);
                          }
                        }}
                        className="flex-1 min-w-0 overflow-visible text-left text-xs font-mono text-zinc-200 hover:text-white select-none cursor-pointer"
                      >
                        <ChannelPlatformLabel
                          kickSlug={ch.kickSlug}
                          twitchSlug={ch.twitchSlug}
                          youtubeSlug={ch.youtubeSlug}
                          onRemoveKick={() => removePlatformFromChannel(ch.id, 'Kick')}
                          onRemoveTwitch={() => removePlatformFromChannel(ch.id, 'Twitch')}
                          onRemoveYoutube={() => removePlatformFromChannel(ch.id, 'YouTube')}
                        />
                      </div>
                    )}
                    {editingChannelId !== ch.id && (
                      <button type="button" title="Rename"
                        onClick={(e) => { e.stopPropagation(); startRenameChannel(ch.id); }}
                        className="text-zinc-600 hover:text-white p-0.5">
                        <Pencil size={11} />
                      </button>
                    )}
                    <button type="button" title="Refresh"
                      onClick={(e) => {
                        e.stopPropagation();
                        clearChannelRefreshFlight(ch.id);
                        void refreshChannel(ch.id, undefined, channelContentFilter, { force: true });
                      }}
                      disabled={ch.loading}
                      className="text-zinc-600 hover:text-white p-0.5 disabled:opacity-40">
                      {ch.loading ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
                    </button>
                    <button type="button" title="Remove"
                      onClick={(e) => { e.stopPropagation(); removeChannel(ch.id); }}
                      className="text-zinc-600 hover:text-red-400 p-0.5">
                      <X size={11} />
                    </button>
                  </div>
                  {selectedChannelId === ch.id && (
                    <div className="flex flex-col gap-2 ml-1 pl-2 border-l-2 border-zinc-700 py-1 min-w-0">
                      {(() => {
                        const platformFiltersOn = Number(kickEnabled) + Number(twitchEnabled) + Number(youtubeEnabled);
                        return (
                      <div className="flex flex-col gap-1.5 min-w-0 w-full">
                      <div className="flex flex-wrap items-center gap-1.5 min-w-0">
                        {(['Kick', 'Twitch', 'YouTube'] as const).map((platform) => {
                          const slug = platform === 'Kick'
                            ? ch.kickSlug
                            : platform === 'Twitch'
                              ? ch.twitchSlug
                              : ch.youtubeSlug;
                          if (!slug?.trim()) return null;
                          const enabled = platform === 'Kick'
                            ? kickEnabled
                            : platform === 'Twitch'
                              ? twitchEnabled
                              : youtubeEnabled;
                          const color = platform === 'Kick'
                            ? '#53fc18'
                            : platform === 'Twitch'
                              ? '#9146FF'
                              : YOUTUBE_COLOR;
                          const setEnabled = platform === 'Kick'
                            ? setKickEnabled
                            : platform === 'Twitch'
                              ? setTwitchEnabled
                              : setYoutubeEnabled;
                          const loading = channelsLoading;
                          const editing = editingSlug?.channelId === ch.id && editingSlug.platform === platform;
                          return (
                            <div key={platform} className="group relative flex items-center shrink-0">
                              {editing ? (
                                <input type="text" value={editingSlugValue}
                                  onChange={(e) => setEditingSlugValue(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') commitEditPlatformSlug();
                                    if (e.key === 'Escape') setEditingSlug(null);
                                  }}
                                  onBlur={commitEditPlatformSlug}
                                  autoFocus
                                  className="w-28 bg-zinc-950 border text-white font-mono text-[10px] px-1.5 py-0.5 focus:outline-none"
                                  style={{ borderColor: color }} />
                              ) : (
                                <div
                                  role="button"
                                  tabIndex={0}
                                  onClick={() => {
                                    if (enabled && platformFiltersOn <= 1) return;
                                    setEnabled((v) => !v);
                                  }}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                      e.preventDefault();
                                      if (enabled && platformFiltersOn <= 1) return;
                                      setEnabled((v) => !v);
                                    }
                                  }}
                                  title={enabled && platformFiltersOn <= 1 ? 'At least one platform filter must stay on' : undefined}
                                  className={`flex items-center gap-1 px-2 py-0.5 border font-mono text-[10px] uppercase font-bold cursor-pointer select-none ${
                                    enabled ? '' : 'opacity-40'
                                  }`}
                                  style={enabled ? { borderColor: color, color } : { borderColor: '#3f3f46' }}
                                >
                                  <input type="checkbox" checked={enabled} readOnly tabIndex={-1}
                                    className="vod-cb-sm pointer-events-none" style={vodCheckboxStyle(color)} />
                                  <span>{platform}</span>
                                  <span className="text-zinc-500 normal-case font-normal">{slug}</span>
                                  <span className="inline-flex w-3 h-3 shrink-0 items-center justify-center">
                                    {loading ? <Loader2 size={9} className="animate-spin" /> : null}
                                  </span>
                                </div>
                              )}
                              {!editing && (
                                <button type="button" title={`Edit ${platform} name`}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    startEditPlatformSlug(ch.id, platform);
                                  }}
                                  className="absolute -top-1 -right-1 opacity-0 group-hover:opacity-100 p-0.5 bg-zinc-900 border rounded-sm"
                                  style={{ borderColor: color, color }}>
                                  <Pencil size={9} />
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[10px] uppercase w-full min-w-0 pt-0.5">
                        <span className="text-zinc-500 shrink-0">Show:</span>
                        <button
                          type="button"
                          onClick={() => setChannelContentFilter('vods')}
                          className={`px-2 py-0.5 border font-bold ${
                            channelContentFilter === 'vods'
                              ? 'border-white text-white bg-zinc-900'
                              : 'border-zinc-700 text-zinc-500 hover:text-white'
                          }`}
                        >
                          {youtubePlatformOnly ? 'Videos' : 'VODs'}
                        </button>
                        <button
                          type="button"
                          onClick={() => setChannelContentFilter('clips')}
                          className={`px-2 py-0.5 border font-bold ${
                            channelContentFilter === 'clips'
                              ? 'border-white text-white bg-zinc-900'
                              : 'border-zinc-700 text-zinc-500 hover:text-white'
                          }`}
                        >
                          {youtubePlatformOnly ? 'Shorts' : 'Clips'}
                        </button>
                        {youtubePlatformOnly && (
                          <button
                            type="button"
                            onClick={() => setChannelContentFilter('streams')}
                            className={`px-2 py-0.5 border font-bold ${
                              channelContentFilter === 'streams'
                                ? 'border-white text-white bg-zinc-900'
                                : 'border-zinc-700 text-zinc-500 hover:text-white'
                            }`}
                          >
                            VODs
                          </button>
                        )}
                      </div>
                      </div>
                        );
                      })()}
                      {channelsLoading && visibleChannelVideos.length === 0 ? (
                        <div className="flex justify-center py-4 text-zinc-500">
                          <Loader2 size={18} className="animate-spin" />
                        </div>
                      ) : visibleChannelVideos.length === 0 ? (
                        <p className="text-center text-zinc-600 font-mono text-[10px] py-3">
                          {channelContentFilter === 'clips'
                            ? (youtubePlatformOnly ? 'No shorts' : 'No clips')
                            : channelContentFilter === 'streams'
                              ? 'No VODs'
                              : (youtubePlatformOnly ? 'No videos' : 'No VODs')}
                        </p>
                      ) : (
                        <div className="flex flex-col gap-1">
                          {selectedChannelVodUrls.size > 0 && (
                            <div className="flex items-center justify-between mb-1">
                              <label className="flex items-center gap-1.5 text-[9px] font-mono text-zinc-500 cursor-pointer hover:text-zinc-300">
                                <input
                                  type="checkbox"
                                  checked={selectedChannelVodUrls.size === visibleChannelVideos.length}
                                  onChange={() => {
                                    if (selectedChannelVodUrls.size === visibleChannelVideos.length) {
                                      setSelectedChannelVodUrls(new Set());
                                    } else {
                                      setSelectedChannelVodUrls(new Set(visibleChannelVideos.map(v => buildVodUrl(v))));
                                    }
                                  }}
                                  className="shrink-0"
                                  style={vodCheckboxStyle('#a1a1aa')}
                                />
                                Select all
                              </label>
                              <button
                                type="button"
                                onClick={handleBulkDownloadChannelVods}
                                className={platformBulkDownloadBtn(bulkDownloadPlatform, bulkDownloadPlatforms.size > 1)}
                              >
                                <Download size={10} /> Download {selectedChannelVodUrls.size}
                              </button>
                            </div>
                          )}
                          <div className={`flex flex-col gap-1 transition-opacity duration-150 ${channelsLoading ? 'opacity-60' : ''}`}>
                          {visibleChannelVideos.map((v, i) => {
                            const fullUrl = buildVodUrl(v);
                            const subline = channelVodSubline(v);
                            const durSec = channelVideoDurationSec(v);
                            const isClipItem = v.content_kind === 'clip' || channelContentFilter === 'clips';
                            const isActiveVod = url.trim() === fullUrl.trim();
                            const rowAccent = platformAccentColor(v.platform);
                            const rowBorder = platformActiveBorder(v.platform);
                            return (
                              <div
                                key={`${v.platform}-${v.id}-${i}`}
                                role="button"
                                tabIndex={0}
                                data-youtube-warm={v.platform === 'youtube' ? fullUrl : undefined}
                                onClick={() => selectVod(fullUrl, {
                                  platform: v.platform,
                                  platformListIndex: v.platformListIndex,
                                  isClip: isClipItem,
                                }, {
                                  durationSec: durSec ?? undefined,
                                  title: v.title || undefined,
                                  thumbnailUrl: v.thumbnail_url ?? undefined,
                                  createdAt: v.created_at ?? null,
                                  views: v.views ?? null,
                                  skipNetwork: true,
                                })}
                                onMouseEnter={() => {
                                  if (v.platform === 'youtube') {
                                    warmYoutubePreview(fullUrl);
                                    // ponytail: longer-delay full-VOD mux on hover.
                                    // Fires after ~1s of mouse rest so it only runs
                                    // when the user is genuinely browsing rather than
                                    // sweeping the list. Cache hit makes the next
                                    // click ~instant from local MP4.
                                    warmYoutubePreviewFull(fullUrl, 1000, 720);
                                  }
                                }}
                                onMouseLeave={() => {
                                  if (v.platform === 'youtube') cancelWarmYoutubePreviewFull(fullUrl);
                                }}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter' || e.key === ' ') {
                                    e.preventDefault();
                                    selectVod(fullUrl, {
                                      platform: v.platform,
                                      platformListIndex: v.platformListIndex,
                                      isClip: isClipItem,
                                    }, {
                                      durationSec: durSec ?? undefined,
                                      title: v.title || undefined,
                                      thumbnailUrl: v.thumbnail_url ?? undefined,
                                      createdAt: v.created_at ?? null,
                                      views: v.views ?? null,
                                      skipNetwork: true,
                                    });
                                  }
                                }}
                                className={`flex items-center gap-1.5 border bg-zinc-950 px-2 py-1.5 hover:border-zinc-600 hover:text-white cursor-pointer group ${
                                  isActiveVod ? `${rowBorder} bg-zinc-900` : 'border-zinc-800'
                                }`}
                              >
                                <label
                                  className="flex items-center self-stretch pl-2 -ml-2 pr-1 cursor-pointer"
                                  onClick={(e) => {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    toggleChannelVodSelection(fullUrl);
                                  }}
                                >
                                  <input
                                    type="checkbox"
                                    checked={selectedChannelVodUrls.has(fullUrl)}
                                    readOnly
                                    tabIndex={-1}
                                    className="shrink-0 pointer-events-none"
                                    style={vodCheckboxStyle(rowAccent)}
                                  />
                                </label>
                                <ChannelClipThumb video={v} />
                                <ChannelListIndexBadge platform={v.platform} index={v.platformListIndex} />
                                <div className="flex-1 min-w-0 text-left text-[11px] font-mono text-zinc-300 group-hover:text-white">
                                  <span className="truncate flex items-center gap-1">
                                    <PlatformVodIcon platform={v.platform} />
                                    <span className="truncate">
                                      {v.title || 'Untitled'}
                                      {durSec != null ? (
                                        <span className="text-zinc-500 ml-1">
                                          {isClipItem ? fmtClipDuration(durSec) : fmtShort(durSec)}
                                        </span>
                                      ) : null}
                                    </span>
                                  </span>
                                  {subline && (
                                    <span className="text-[11px] text-zinc-300 block truncate font-medium">
                                      {subline}
                                    </span>
                                  )}
                                </div>
                                <button
                                  type="button"
                                  title={isClipItem ? 'Preview clip' : 'Preview VOD'}
                                  onMouseEnter={() => {
                                    if (v.platform === 'youtube') {
                                      warmYoutubePreview(fullUrl);
                                      warmYoutubePreviewFull(fullUrl, 1000, 720);
                                    }
                                  }}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    void openExplorePlayer(v);
                                  }}
                                  className="shrink-0 border border-zinc-700 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-zinc-400 hover:border-white hover:text-white flex items-center gap-0.5"
                                >
                                  <Eye size={10} />
                                  Preview
                                </button>
                                <button
                                  type="button"
                                  title="Open in browser"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    window.open(fullUrl, '_blank', 'noopener,noreferrer');
                                  }}
                                  className="text-zinc-600 hover:text-white p-1 shrink-0"
                                >
                                  <ExternalLink size={11} />
                                </button>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                      )}
                      {canExpandChannelList && (
                        <button type="button" onClick={handleExpandChannelList}
                          className="text-[10px] font-mono text-zinc-500 hover:text-white uppercase">
                          +{CHANNEL_EXPAND_STEP} more
                        </button>
                      )}
                    </div>
                  )}
                  </Fragment>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {tab === 'queue' && (
          <QueueTab
            queueDownloads={queueDownloads}
            recentDownloads={recentDownloads}
            historyDownloads={historyDownloads}
            onPause={handlePause}
            onResume={handleResume}
            onCancel={handleCancel}
            onDelete={handleRemoveFromQueue}
            onDeleteHistory={handleDeleteHistory}
            onOpenFolder={openFolder}
            onRefresh={refreshDownloads}
            basename={basename}
            selectedQueueIds={selectedQueueIds}
            selectedHistoryIds={selectedHistoryIds}
            onToggleQueueSelection={toggleQueueSelection}
            onToggleHistorySelection={toggleHistorySelection}
            onBulkDeleteQueue={handleBulkDeleteQueue}
            onBulkDeleteHistory={handleBulkDeleteHistory}
            selectedRecentIds={selectedRecentIds}
            onToggleRecentSelection={toggleRecentSelection}
            onBulkDeleteRecent={handleBulkDeleteRecent}
            onWatchLocal={openLocalFilePreview}
          />
        )}

        {tab === 'settings' && settings && (
          <SettingsTab
            settings={settings}
            setSettings={setSettings}
            appVersion={appVersion}
            updateInfo={updateInfo}
            updateChecking={updateChecking}
            updateApplying={updateApplying}
            updateMessage={updateMessage}
            pickingFolder={pickingFolder}
            settingsSaved={settingsSaved}
            onPickFolder={pickDownloadFolder}
            onSave={handleSaveSettings}
            onCheckUpdate={handleCheckUpdate}
            onApplyUpdate={handleApplyUpdate}
            onFlushPanelLayout={flushPanelLayoutToBackend}
          />
        )}

        <PanelResizeHandles onPointerDown={onMainPanelResize} insetPx={panelResizeHandleInset(false)} />
      </div>
      </div>
      </div>

      {/* Background */}
      <div className="fixed top-10 left-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        KICK
      </div>
      {explorePopups.length > 0 && createPortal(
        <>
          {explorePopups.map((entry) => (
            <ChannelExplorePopup
              key={entry.id}
              id={entry.id}
              vod={entry.vod}
              zIndex={EXPLORE_POPUP_Z + (exploreZOrder[entry.id] ?? 0)}
              stackIndex={entry.layoutIndex}
              volumeMenuCloseTick={exploreVolumeMenuCloseTick}
              onClose={() => closeExplorePopup(entry.id)}
              onCarryToUrl={carryExploreToUrl}
              onRegisterPause={registerExplorePause}
              onUnregisterPause={unregisterExplorePause}
              onVolumeMenuOpen={handleExploreVolumeMenuOpen}
              onBringToFront={() => bringExplorePopupToFront(entry.id)}
            />
          ))}
        </>,
        document.getElementById('explore-portal') ?? document.body,
      )}
      {localFilePopups.length > 0 && createPortal(
        <>
          {localFilePopups.map((entry, i) => (
            <LocalFilePopup
              key={entry.id}
              item={entry}
              zIndex={EXPLORE_POPUP_Z + 100 + i}
              stackIndex={i}
              onClose={() => closeLocalFilePopup(entry.id)}
              onBringToFront={() => {}}
            />
          ))}
        </>,
        document.getElementById('explore-portal') ?? document.body,
      )}
      <div className="fixed bottom-10 right-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        TWITCH
      </div>

      <NeedleGlancePopup glance={needleGlance} vodDurationSec={vodDurationSec} />
      <DownloadConfirmDialog
        open={downloadConfirmOpen}
        title={downloadConfirmCopy.title}
        message={downloadConfirmCopy.message}
        accentColor={platformAccentColor(urlActionPlatform || activePlatform || 'kick')}
        filenamePlaceholder={
          downloadConfirmCopy.defaultFilename
            ? downloadConfirmCopy.defaultFilename
            : undefined
        }
        filename={downloadFilename}
        onFilenameChange={setDownloadFilename}
        onConfirm={() => void executeStartDownload()}
        onCancel={() => setDownloadConfirmOpen(false)}
      />
    </div>
  );
}
