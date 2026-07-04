import { Fragment, useState, useEffect, useCallback, useMemo, useRef, type CSSProperties, type Dispatch, type KeyboardEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import {
  Download, Scissors, Info, Play, Pause, Link2, X, Clock,
  Users, Database, Settings2, Loader2,
  AlertCircle, RefreshCw, Pencil, Plus,
  ExternalLink, Eye, Volume2, VolumeX, Maximize2, Minimize2,
  GripVertical,
} from 'lucide-react';
import ChannelExplorePopup, { type ExplorePopupVod } from './ChannelExplorePopup';
import PreviewQualityMenu from './PreviewQualityMenu';
import {
  PREVIEW_CLIP_DEFAULT_HEIGHT,


  attachProgressivePreview,

  detachProgressivePreview,
  initialPreviewPreferHeight,

  maxQualityLabelFromList,
  measurePlayerHeightCap,

  mergeVariantHeights,
  parseQualityHeights,
  resolveHlsPreviewLevels,
  isClipPreviewUrl,
  resolvePreviewPlayback,
  resolveProgressivePreviewLevels,
  resolveProgressivePreviewLevelsAsync,
  inferLevelHeight,
  suggestClipDownloadName,
  suggestVideoDownloadName,
  type PreviewLevelOption,
} from './previewPlayerUtils';
import DownloadConfirmDialog from './components/DownloadConfirmDialog';
import EditableHmsTime from './components/EditableHmsTime';
import { formatHmsFull } from './utils';
import { actionBtnHover, platformCardShadow } from './platformStyles';
import { fmtDuration, fmtShort, fmtClipDuration, formatClipDurationHuman, fmtDateAndAgo, parseVideoTs, formatBytes, basename, sourceQualityOptionLabel } from './formatters';
import type { VideoInfo, ChannelVideo, ListedChannelVideo, SavedChannel, ChannelPreviewBadge, AppSettings, UpdateInfo, DownloadState, DownloadsResponse, Tab, LayoutPanelBoundsInput, PersistedPanelLayout } from './types';
import { detectUrlPlatform, isClipUrl, detectVideoPlatform, bestAvailableQuality, channelVideoDurationSec, videoInfoDurationSec, isLikelyClip, mergeVodLists, mergeClipLists, channelClipsMissing, channelVodsMissing, buildVodUrl, parseChannelInput, slugFromVideoUrl, isChannelAlreadySaved, normalizeSavedChannel, loadSavedChannels, persistChannels, formatChannelErrorMessage, channelVodSubline, reorderChannelsById, mapApiChannelItem, channelInsertIndex, estimateDownloadBytes, CHANNEL_INITIAL_VISIBLE, CHANNEL_EXPAND_STEP, CHANNEL_FETCH_LIMIT, CHANNEL_INCREMENTAL_LIMIT, CHANNEL_UI_STORAGE_KEY, MAX_SAVED_CHANNELS , loadStoredChannelUi } from './channelUtils';
import { clampTrimEndpoints, trimButtonDeltaForEndpoint, adjustTrimEndpointByDelta, type TrimRangeOpts } from './trimUtils';
import { panelMaxW, layoutMaxPanelWidth, layoutMaxPanelHeight, clampPanelSizeForLayout, clampAllLayoutPanels, clampPreviewPanelWidth, applyPanelSize, startPanelResizeDrag, applyPanelWidth, startPanelWidthResize, defaultPanelLayout, loadPanelLayout, persistPanelLayout, clampLayoutNumber, clampStoredPanelSize, PREVIEW_KEY_SKIP_SEC, PREVIEW_FS_CONTROLS_HIDE_MS, PREVIEW_DEFAULT_VOLUME, PREVIEW_PANEL_MIN_W, PREVIEW_PANEL_CHROME_H_EST, PREVIEW_VIDEO_ASPECT_DEFAULT, URL_ASIDE_PANEL_DEFAULT, MAIN_PANEL_DEFAULT, EXPLORE_POPUP_Z, MAX_EXPLORE_POPUPS } from './layoutUtils';
import ChannelListIndexBadge from './components/ChannelListIndexBadge';
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

// ─── TYPES (migrated to src/types.ts) ───────────────
const IS_DEV_UI = import.meta.env.DEV;

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

  // Download options
  const [quality, setQuality] = useState('source');
  const urlPlatform = detectUrlPlatform(url);
  const [trimStartSec, setTrimStartSec] = useState(0);
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
  const [previewVideoReady, setPreviewVideoReady] = useState(false);
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
  // ── Shared preview hook (quality state machine) ──────────────────────────
  const {
    previewLevels,
    qualityLevel: previewQualityLevel,

    syncPlaybackToViewport: syncPreviewPlaybackToViewport,
    applyQuality: applyPreviewQuality,
    setPreviewLevels,
    setQualityLevel: setPreviewQualityLevel,
    setHlsRef,
  } = usePreviewPlayer({
    videoRef: previewVideoRef,
    playback: previewPlayback,
    sessionId: previewSessionId,
    isClipPreview: isClipUrl(url.trim()),
    containerRef: previewContainerRef,
    trimStart: previewTrimStartRef.current,
    onPreviewError: (msg: string) => {
      if (msg) setError(msg);
    },
  });

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
    const clampedPreviewW = clampLayoutNumber(
      pl.previewPanelWidth,
      PREVIEW_PANEL_MIN_W,
      panelMaxW(),
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
  const [selectedChannelVodUrls, setSelectedChannelVodUrls] = useState<Set<string>>(new Set());
  // Channels — persisted in localStorage (survives server restarts).
  const [savedChannels, setSavedChannels] = useState<SavedChannel[]>(() => loadSavedChannels());
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);
  const [addChannelInput, setAddChannelInput] = useState('');
  const [editingChannelId, setEditingChannelId] = useState<string | null>(null);
  const [editingChannelName, setEditingChannelName] = useState('');
  const [editingSlug, setEditingSlug] = useState<{ channelId: string; platform: 'Kick' | 'Twitch' } | null>(null);
  const [editingSlugValue, setEditingSlugValue] = useState('');
  const [addChannelNotice, setAddChannelNotice] = useState<string | null>(null);
  const [pendingAddChannel, setPendingAddChannel] = useState<{
    displayName: string;
    kickSlug: string;
    twitchSlug: string;
  } | null>(null);
  const [channelDragId, setChannelDragId] = useState<string | null>(null);
  const [channelDropInsertIndex, setChannelDropInsertIndex] = useState<number | null>(null);
  const channelListRef = useRef<HTMLDivElement>(null);
  const channelsPersistReadyRef = useRef(false);
  /** True after saved channels were hydrated once (localStorage wins over API). */
  const channelsHydratedRef = useRef(false);
  const channelUiPersistReadyRef = useRef(false);
  const [pickingFolder, setPickingFolder] = useState(false);
  const initialChannelUi = useMemo(() => loadStoredChannelUi(), []);
  // Platform filter for channel browsing — persisted in settings + localStorage.
  const [kickEnabled, setKickEnabled] = useState(initialChannelUi.kick);
  const [twitchEnabled, setTwitchEnabled] = useState(initialChannelUi.twitch);
  // How many cached VODs to show per platform (expand is client-side only).
  const [kickVisibleLimit, setKickVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);
  const [twitchVisibleLimit, setTwitchVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);
  const [channelContentFilter, setChannelContentFilter] = useState<'vods' | 'clips'>(
    initialChannelUi.content,
  );

  const selectedChannel = useMemo(
    () => savedChannels.find((c) => c.id === selectedChannelId) ?? null,
    [savedChannels, selectedChannelId],
  );

  const allChannelVideos = useMemo(() => {
    if (!selectedChannel) return [];
    return channelContentFilter === 'clips'
      ? (selectedChannel.clipVideos ?? [])
      : (selectedChannel.vodVideos ?? []);
  }, [selectedChannel, channelContentFilter]);

  const kickChannelVideos = useMemo(
    () => allChannelVideos.filter((v) => v.platform === 'Kick'),
    [allChannelVideos],
  );
  const twitchChannelVideos = useMemo(
    () => allChannelVideos.filter((v) => v.platform === 'Twitch'),
    [allChannelVideos],
  );

  const kickBrowseLoading = selectedChannel?.loading ?? false;
  const twitchBrowseLoading = selectedChannel?.loading ?? false;
  const channelsLoading = kickBrowseLoading || twitchBrowseLoading;

  const visibleChannelVideos = useMemo(() => {
    const items: ChannelVideo[] = [];
    if (kickEnabled) items.push(...kickChannelVideos.slice(0, kickVisibleLimit));
    if (twitchEnabled) items.push(...twitchChannelVideos.slice(0, twitchVisibleLimit));
    const sorted = channelContentFilter === 'clips'
      ? items.sort((a, b) => (Number(b.views) || 0) - (Number(a.views) || 0))
      : items.sort((a, b) => parseVideoTs(b.created_at) - parseVideoTs(a.created_at));
    let kickN = 0;
    let twitchN = 0;
    return sorted.map((v): ListedChannelVideo => ({
      ...v,
      platformListIndex: v.platform === 'Kick' ? ++kickN : ++twitchN,
    }));
  }, [
    kickChannelVideos,
    twitchChannelVideos,
    kickEnabled,
    twitchEnabled,
    kickVisibleLimit,
    twitchVisibleLimit,
    channelContentFilter,
  ]);

  const canExpandKick = kickEnabled && kickVisibleLimit < kickChannelVideos.length;
  const canExpandTwitch = twitchEnabled && twitchVisibleLimit < twitchChannelVideos.length;
  const canExpandChannelList = canExpandKick || canExpandTwitch;

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

  const vodDurationSec = useMemo(
    () => Math.max(1, videoInfoDurationSec(videoInfo)),
    [videoInfo],
  );

  useEffect(() => {
    vodDurationSecRef.current = vodDurationSec;
  }, [vodDurationSec]);

  // Keep previewOpenRef in sync
  useEffect(() => {
    previewOpenRef.current = previewOpen;
  }, [previewOpen]);

  const previewDurationSec = useMemo(
    () => Math.max(1, previewTrimEnd - previewTrimStart),
    [previewTrimStart, previewTrimEnd],
  );

  const destroyPreviewPlayer = useCallback(() => {
    if (previewHlsRef.current) {
      previewHlsRef.current.destroy();
      previewHlsRef.current = null;
        setHlsRef(null);
    }
    const video = previewVideoRef.current;
    if (video) {
      detachProgressivePreview(video);
    }
  }, []);

  const resetPreview = useCallback(async () => {
    previewGenRef.current += 1; // cancel any in-flight openPreview
    previewStartedRef.current = false;
    previewLoadedUrlRef.current = null;
    const sid = previewSessionId;
    destroyPreviewPlayer();
    setPreviewOpen(false);
    setPreviewSessionId(null);
    setPreviewPlayback(null);
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
    previewRequestedHeightRef.current = 0;
    previewAppliedHeightRef.current = 0;
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    if (sid) {
      try { await apiDelete(`/api/preview/session/${sid}`); } catch { /* ignore */ }
    }
  }, [previewSessionId, destroyPreviewPlayer]);

  const seekPreviewVideo = useCallback((sec: number, force = false) => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const t = Math.max(start, Math.min(sec, end));
    if (force || Math.abs(video.currentTime - t) > 0.05) {
      video.currentTime = t;
      syncPreviewTimeUi(t, true);
    }
  }, [previewVideoReady, syncPreviewTimeUi]);

  const openPreview = useCallback(async () => {
    if (!url.trim()) return;
    if (trimEndSec <= trimStartSec) return;
    const trimmedUrl = url.trim();
    // Already showing this URL — no-op on re-click (uses refs to avoid closure staleness)
    if (previewStartedRef.current && previewLoadedUrlRef.current === trimmedUrl) return;
    previewStartedRef.current = true;

    // Cancel any previously in-flight openPreview
    const gen = ++previewGenRef.current;
    const start = trimStartSec;
    const end = trimEndSec;
    previewTrimStartRef.current = start;
    previewTrimEndRef.current = end;
    setPreviewTrimStart(start);
    setPreviewTrimEnd(end);
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    previewVolumeRef.current = PREVIEW_DEFAULT_VOLUME;
    setPreviewVolume(PREVIEW_DEFAULT_VOLUME);
    setPreviewMuted(false);
    previewVideoAspectRef.current = PREVIEW_VIDEO_ASPECT_DEFAULT;
    setPreviewVideoAspect(PREVIEW_VIDEO_ASPECT_DEFAULT);
    const clampedPreviewW = clampPreviewPanelWidth(
      previewPanelWidthRef.current,
      previewChromeHRef.current,
      PREVIEW_VIDEO_ASPECT_DEFAULT,
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
    try {
      if (previewSessionId) {
        try { await apiDelete(`/api/preview/session/${previewSessionId}`); } catch { /* ignore */ }
      }
      destroyPreviewPlayer();
      const clipPreview = isClipUrl(url.trim());
      const playerCap = measurePlayerHeightCap(
        previewContainerRef.current ?? previewPanelRef.current,
        previewVideoAspectRef.current,
      );
      const previewPreferHeight = initialPreviewPreferHeight(clipPreview, playerCap);
      let qualityLabels = videoInfo?.qualities;
      const sessionPromise = apiPost<{
        session_id: string;
        master_url: string;
        playback_url?: string;
        kind?: string;
        variant_heights?: number[];
        quality_labels?: string[];
        active_height?: number;
      }>('/api/preview/session', {
        url: url.trim(),
        crop_start: start,
        crop_end: end,
        prefer_height: previewPreferHeight,
      });
      const clipInfoPromise = clipPreview && !qualityLabels?.length
        ? apiGet<VideoInfo>(`/api/info/clip?id=${encodeURIComponent(url.trim())}`).catch(() => null)
        : Promise.resolve(null);
      const [res, clipInfo] = await Promise.all([sessionPromise, clipInfoPromise]);
      if (gen !== previewGenRef.current) return;
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
      setPreviewSessionId(res.session_id);
      const playback = resolvePreviewPlayback(url.trim(), res);
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
  }, [url, trimEndSec, trimStartSec, vodDurationSec, previewSessionId, destroyPreviewPlayer, videoInfo?.qualities]);

  useEffect(() => {
    if (!previewOpen || !previewPlayback?.url) return;
    const previewPageUrl = previewLoadedUrlRef.current ?? url.trim();
    let cancelled = false;
    let cleanup: (() => void) | undefined;

    const setup = () => {
      if (cancelled) return;
      const video = previewVideoRef.current;
      if (!video) {
        requestAnimationFrame(setup);
        return;
      }
      const { url: playbackUrl, kind: playbackKind } = previewPlayback;

    setPreviewVideoLoading(true);
    setPreviewVideoReady(false);

    const performInitialSeek = () => {
      if (previewInitialSeekDoneRef.current) return;
      previewInitialSeekDoneRef.current = true;
      const start = previewTrimStartRef.current;
      const end = previewTrimEndRef.current;
      if (Number.isFinite(start) && Math.abs(video.currentTime - start) > 0.25) {
        video.currentTime = start;
      }
      const t = Math.max(start, Math.min(video.currentTime, end));
      if (Math.abs(video.currentTime - t) > 0.05) video.currentTime = t;
      syncPreviewTimeUi(t, true);
    };

    const onCanPlay = () => {
      setPreviewVideoReady(true);
      setPreviewVideoLoading(false);
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
    };

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
      const onVideoError = () => {
        setError('Clip preview failed — try again');
        setPreviewVideoLoading(false);
      };
      previewAppliedHeightRef.current = activeH;
      attachProgressivePreview(video, playbackUrl, previewTrimStartRef.current);
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
        startPosition: previewTrimStartRef.current,
      });
      previewHlsRef.current = hls;
      setHlsRef(hls);
      hls.loadSource(playbackUrl);
      hls.attachMedia(video);
      let levelsInitialized = false;
      const playerCap = measurePlayerHeightCap(
        previewContainerRef.current ?? previewPanelRef.current,
        previewVideoAspectRef.current,
      );
      const previewPreferHeight = initialPreviewPreferHeight(isClipUrl(previewPageUrl), playerCap);
      const fallbackHeights = mergeVariantHeights(
        previewPlayback.variantHeights,
        parseQualityHeights(videoInfo?.qualities ?? []),
      );
      const syncPreviewLevels = (levels = hls.levels, applyDefault = false) => {
        const cappedDefault = Math.min(previewPreferHeight, playerCap);
        const { mapped, defaultIndex } = resolveHlsPreviewLevels(levels, {
          initialHeight: cappedDefault,
          fallbackHeights,
        });
        if (!mapped.length) return;
        setPreviewLevels(mapped);
        if (!levelsInitialized || applyDefault) {
          levelsInitialized = true;
          const hlsIndex = mapped[defaultIndex]?.index ?? defaultIndex;
          if (hls.levels.length > 0 && hlsIndex >= 0 && hlsIndex < hls.levels.length) {
            const levelHeight = inferLevelHeight(hls.levels[hlsIndex]);
            if (levelHeight > 0) {
              hls.loadLevel = hlsIndex;
              previewAppliedHeightRef.current = levelHeight;
            }
          }
          setPreviewQualityLevel(defaultIndex);
          const picked = mapped[defaultIndex];
          if (picked?.height) previewRequestedHeightRef.current = picked.height;
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
            setError('Preview playback failed — try again');
            setPreviewVideoLoading(false);
            hls.destroy();
            previewHlsRef.current = null;
            break;
        }
      });
      cleanup = () => {
        video.removeEventListener('canplay', onCanPlay);
        hls.destroy();
        previewHlsRef.current = null;
      };
      return;
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
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
    setPreviewVideoLoading(false);
    };

    setup();
    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, [previewOpen, previewPlayback]);

  const handlePreviewTimeUpdate = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    let t = video.currentTime;
    if (t < start - 0.05) {
      video.currentTime = start;
      t = start;
    }
    syncPreviewTimeUi(t);
    if (t >= end - 0.05) {
      video.pause();
      if (Math.abs(video.currentTime - end) > 0.05) {
        video.currentTime = end;
      }
      syncPreviewTimeUi(end, true);
      setPreviewPlaying(false);
    }
  }, [syncPreviewTimeUi]);

  const togglePreviewPlay = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    if (video.paused) {
      const start = previewTrimStartRef.current;
      const end = previewTrimEndRef.current;
      if (video.currentTime >= end - 0.1 || video.currentTime < start) {
        video.currentTime = start;
        syncPreviewTimeUi(start, true);
      }
      void video.play();
      setPreviewPlaying(true);
    } else {
      video.pause();
      setPreviewPlaying(false);
    }
  }, [previewVideoReady]);
;


  const skipPreview = useCallback((deltaSec: number) => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const t = Math.max(start, Math.min(end, video.currentTime + deltaSec));
    seekPreviewVideo(t);
  }, [previewVideoReady, seekPreviewVideo]);

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
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    let t = video.currentTime;
    if (t < start) t = start;
    else if (t > end) t = end;
    if (Math.abs(video.currentTime - t) > 0.05) {
      video.currentTime = t;
      syncPreviewTimeUi(t, true);
    }
  }, [previewVideoReady, syncPreviewTimeUi]);

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
    const video = previewVideoRef.current;
    if (!video) return;
    const v = Math.max(0, Math.min(1, level));
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
  }, []);

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
      const fs = document.fullscreenElement === previewContainerRef.current;
      setPreviewFullscreen(fs);
      setPreviewFsControlsVisible(!fs);
      requestAnimationFrame(() => {
        void syncPreviewPlaybackToViewport(fs);
      });
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, [syncPreviewPlaybackToViewport]);

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
    const isClipItem = v.content_kind === 'clip' || channelContentFilter === 'clips' || isLikelyClip(v);
    const vod: ExplorePopupVod = {
      url: buildVodUrl(v),
      title: v.title || 'Untitled',
      platform: v.platform,
      durationSec: v.duration ? Math.max(2, Math.floor(v.duration)) : 7200,
      platformListIndex: v.platformListIndex,
      isClip: isClipItem,
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
  }, [layoutBoundsInput]);

  useEffect(() => {
    applyLayoutPanelClamps();
    const onResize = () => applyLayoutPanelClamps();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [applyLayoutPanelClamps, previewOpen, channelVodPanelOpen]);

  const onPreviewPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    // ponytail: pass empty layout so preview can grow freely - other panels shrink on drag end
    const emptyLayout = { previewOpen: false, urlPanelAside: false, preview: { w: 0, h: 0 }, urlAside: { w: 0, h: 0 }, main: { w: 0, h: 0 } };
    startPanelWidthResize(e, edge, previewPanelWidthRef, setPreviewPanelWidth, {
      panelEl: previewPanelRef.current,
      aspect: previewVideoAspectRef.current,
      clampWidth: (w) => clampPreviewPanelWidth(
        w,
        previewChromeHRef.current,
        previewVideoAspectRef.current,
        emptyLayout,
      ),
      onResizeEnd: () => {
        applyLayoutPanelClamps();
      },
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps]);

  const onUrlAsidePanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const layout = layoutBoundsInput();
    startPanelResizeDrag(e, edge, urlAsidePanelSizeRef, setUrlAsidePanelSize, {
      panelEl: urlAsidePanelRef.current,
      maxW: layoutMaxPanelWidth('urlAside', layout),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => clampPanelSizeForLayout('urlAside', s, layoutBoundsInput()),
      onResizeEnd: () => applyLayoutPanelClamps(),
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps]);

  const onMainPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const layout = layoutBoundsInput();
    startPanelResizeDrag(e, edge, mainPanelSizeRef, setMainPanelSize, {
      panelEl: mainPanelRef.current,
      maxW: layoutMaxPanelWidth('main', layout),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => clampPanelSizeForLayout('main', s, layoutBoundsInput()),
      onResizeEnd: () => applyLayoutPanelClamps(),
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps]);

  useEffect(() => {
    if (!previewOpen || previewFullscreen || !previewPanelRef.current || !previewContainerRef.current) return;
    const chromeH = previewPanelRef.current.offsetHeight - previewContainerRef.current.offsetHeight;
    if (chromeH > 0) {
      previewChromeHRef.current = chromeH;
    }
  }, [previewOpen, previewFullscreen, previewPanelWidth, previewVideoAspect, previewVideoReady]);

  // ── Fetch video info ──

  const fetchVideoInfo = useCallback(async (videoUrl: string) => {
    const trimmed = videoUrl.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    setPendingAddChannel(null);
    try {
      const infoPath = isClipUrl(trimmed) ? '/api/info/clip' : '/api/info/video';
      const info = await apiGet<VideoInfo>(`${infoPath}?id=${encodeURIComponent(trimmed)}`);
      setUrl(trimmed);
      setVideoInfo(info);
      setQuality(bestAvailableQuality(info));
      const end = Math.max(1, videoInfoDurationSec(info));
      trimStartSecRef.current = 0;
      trimEndSecRef.current = end;
      setTrimStartSec(0);
      setTrimEndSec(end);
      // Keep the current preview playing until the user hits Preview on the new VOD.
      if (!previewOpen) {
        previewTrimStartRef.current = 0;
        previewTrimEndRef.current = end;
        setPreviewTrimStart(0);
        setPreviewTrimEnd(end);
        void resetPreview();
      }
      const isMediaUrl = isClipUrl(trimmed) || /\/videos\//i.test(trimmed) || /^\d+$/.test(trimmed);
      if (isMediaUrl && savedChannels.length < MAX_SAVED_CHANNELS) {
        const platform = detectUrlPlatform(trimmed) ?? detectVideoPlatform(info, trimmed);
        const { kickSlug, twitchSlug } = slugFromVideoUrl(
          trimmed,
          platform,
          info.uploader,
          info.channel ?? info.uploader,
        );
        if (
          kickSlug
          && !isChannelAlreadySaved(kickSlug, twitchSlug, savedChannels)
        ) {
          setPendingAddChannel({
            displayName: info.uploader?.trim() || kickSlug,
            kickSlug,
            twitchSlug,
          });
        }
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [previewOpen, resetPreview, savedChannels]);

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
      setQueueDownloads(data.queue || []);
      setRecentDownloads(data.recent || []);
      setHistoryDownloads(data.history || []);
    } catch {}
  }, []);

  const activeDownloadIds = useMemo(
    () => queueDownloads
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
    try {
      const endpoint = clipDownload ? '/api/download/clip' : '/api/download/video';
      const platform = (videoInfo as any)?.platform || undefined;
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
              platform,
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
      const body = clipDownload
        ? {
            url: url.trim(),
            quality: quality || undefined,
            output_file: clipName,
            ...trimBody,
          }
        : {
            url: url.trim(),
            quality: quality || undefined,
            ...trimBody,
          };
      await apiPost<{ download_id: string; status: string }>(endpoint, body);
      setTab('queue');
      refreshDownloads();
    } catch (err: any) {
      setError(err.message);
    }
  }, [videoInfo, url, quality, effectiveDownloadTrim, ensureDownloadFolder, refreshDownloads, downloadFilename]);

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

  const handleDeleteHistory = useCallback(async (id: string) => {
    if (!window.confirm('Remove this download from history?')) return;
    setHistoryDownloads((prev) => prev.filter((d) => d.download_id !== id));
    try {
      await apiPost(`/api/download/${id}/remove`, {});
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to remove from history';
      setError(msg);
      refreshDownloads();
    }
  }, [refreshDownloads]);

  const handleRemoveFromQueue = useCallback(async (id: string) => {
    if (!window.confirm('Remove this download from the queue?')) return;
    setQueueDownloads((prev) => prev.filter((d) => d.download_id !== id));
    try {
      await apiPost(`/api/download/${id}/remove`, {});
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to remove from queue';
      setError(msg);
      refreshDownloads();
    }
  }, [refreshDownloads]);

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

  const handleBulkDeleteRecent = useCallback(async () => {
    if (selectedRecentIds.size === 0) return;
    if (!window.confirm(`Remove ${selectedRecentIds.size} download(s) from recent?`)) return;
    const ids = [...selectedRecentIds];
    setSelectedRecentIds(new Set());
    await Promise.allSettled(ids.map((id) =>
      apiPost(`/api/download/${id}/remove`, {}).catch(() => {}),
    ));
    refreshDownloads();
  }, [selectedRecentIds, refreshDownloads]);

  const handleBulkDeleteQueue = useCallback(async () => {
    if (selectedQueueIds.size === 0) return;
    if (!window.confirm(`Remove ${selectedQueueIds.size} download(s) from the queue?`)) return;
    const ids = [...selectedQueueIds];
    setSelectedQueueIds(new Set());
    await Promise.allSettled(ids.map((id) =>
      apiPost(`/api/download/${id}/remove`, {}).catch(() => {}),
    ));
    refreshDownloads();
  }, [selectedQueueIds, refreshDownloads]);

  const handleBulkDeleteHistory = useCallback(async () => {
    if (selectedHistoryIds.size === 0) return;
    if (!window.confirm(`Remove ${selectedHistoryIds.size} download(s) from history?`)) return;
    const ids = [...selectedHistoryIds];
    setSelectedHistoryIds(new Set());
    await Promise.allSettled(ids.map((id) =>
      apiPost(`/api/download/${id}/remove`, {}).catch(() => {}),
    ));
    refreshDownloads();
  }, [selectedHistoryIds, refreshDownloads]);

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
    for (const vodUrl of urls) {
      try {
        const dlEndpoint = isClipUrl(vodUrl) ? '/api/download/clip' : '/api/download/video';
        await apiPost<{ download_id: string }>(dlEndpoint, {
          url: vodUrl,
          quality: 'source',
        });
      } catch (err: any) {
        setError(err.message || 'Failed to start download');
        break;
      }
    }
    setTab('queue');
    refreshDownloads();
  }, [selectedChannelVodUrls, ensureDownloadFolder, refreshDownloads]);

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
    apiPost('/api/settings', { saved_channels: payload }).catch(() => {});
  }, [savedChannels]);

  useEffect(() => {
    try {
      localStorage.setItem(
        CHANNEL_UI_STORAGE_KEY,
        JSON.stringify({
          kick: kickEnabled,
          twitch: twitchEnabled,
          content: channelContentFilter,
        }),
      );
    } catch {
      /* ignore */
    }
    if (!channelUiPersistReadyRef.current) return;
    apiPost('/api/settings', {
      channel_kick_enabled: kickEnabled,
      channel_twitch_enabled: twitchEnabled,
      channel_content_filter: channelContentFilter,
    }).catch(() => {});
    setSettings((prev) =>
      prev
        ? {
            ...prev,
            channel_kick_enabled: kickEnabled,
            channel_twitch_enabled: twitchEnabled,
            channel_content_filter: channelContentFilter,
          }
        : prev,
    );
  }, [kickEnabled, twitchEnabled, channelContentFilter]);

  const updateChannel = useCallback((id: string, patch: Partial<SavedChannel>) => {
    setSavedChannels((prev) => prev.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }, []);

  const savedChannelsRef = useRef(savedChannels);
  savedChannelsRef.current = savedChannels;

  const channelRefreshInFlightRef = useRef<Set<string>>(new Set());

  const refreshChannel = useCallback(async (
    channelId: string,
    channelOverride?: SavedChannel,
    contentMode?: 'vods' | 'clips',
    opts?: { incremental?: boolean; silent?: boolean },
  ) => {
    const ch = channelOverride ?? savedChannelsRef.current.find((c) => c.id === channelId);
    if (!ch) return;
    const mode = contentMode ?? channelContentFilter;
    const incremental = opts?.incremental ?? false;
    const silent = opts?.silent ?? false;
    const flightKey = `${channelId}:${mode}`;
    if (!incremental && channelRefreshInFlightRef.current.has(flightKey)) return;
    if (!incremental) channelRefreshInFlightRef.current.add(flightKey);

    if (!incremental && !silent) {
      updateChannel(channelId, { loading: true });
      setKickVisibleLimit(CHANNEL_INITIAL_VISIBLE);
      setTwitchVisibleLimit(CHANNEL_INITIAL_VISIBLE);
    }
    const errs: Record<string, string> = {};
    const incoming: ChannelVideo[] = [];

    // Always fetch both platforms; Kick/Twitch toggles only filter the display.
    const wantKick = true;
    const wantTwitch = true;

    try {
      if (mode === 'clips') {
        const slug = ch.kickSlug?.trim() || ch.twitchSlug?.trim() || '';
        const params = new URLSearchParams({
          platforms: 'Kick,Twitch',
          limit: '10',
          kick_slug: ch.kickSlug,
          twitch_login: ch.twitchSlug,
        });
        if (slug) params.set('url', slug);
        try {
          let data: ChannelClipsResponse;
          try {
            data = await apiGet<ChannelClipsResponse>(`/api/channel/clips?${params}`);
          } catch (clipErr: unknown) {
            const msg = clipErr instanceof Error ? clipErr.message : '';
            if (!msg.includes('Clips API not on server') && !msg.includes('Clips API unavailable')) {
              throw clipErr;
            }
            params.set('content', 'clips');
            data = await apiGet<ChannelClipsResponse>(`/api/channel/videos?${params}`);
          }
          if (data.content && data.content !== 'clips') {
            errs.Kick = IS_DEV_UI
              ? 'Clips API unavailable — restart with npm run dev'
              : 'Clips API unavailable — reopen VOD.RIP';
            errs.Twitch = errs.Kick;
          } else {
            incoming.push(...(data.clips ?? (data as unknown as ChannelVodsResponse).videos ?? []).map(mapApiChannelItem));
            for (const [platform, pe] of Object.entries(data.per_platform_errors ?? {})) {
              if (pe) errs[platform] = pe;
            }
          }
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : 'Failed to fetch clips';
          errs.Kick = msg;
          errs.Twitch = msg;
        }
        const latest = savedChannelsRef.current.find((c) => c.id === channelId) ?? ch;
        const clipVideos = mergeClipLists(latest.clipVideos ?? [], incoming);
        const prevClipErrors = latest.clipErrors ?? {};
        updateChannel(channelId, {
          clipVideos,
          clipErrors: { ...prevClipErrors, ...errs },
          clipsFetched: clipVideos.length > 0 || Object.keys(errs).length === 0,
          loading: false,
          updatedAt: new Date().toISOString(),
        });
      } else {
        const limit = incremental ? CHANNEL_INCREMENTAL_LIMIT : CHANNEL_FETCH_LIMIT;
        const fetchVods = async (platform: 'Kick' | 'Twitch', slug: string) => {
          if (!slug?.trim()) {
            errs[platform] = `${platform} slug is not set`;
            return;
          }
          const params = new URLSearchParams({
            url: slug,
            limit: String(limit),
            days: '14',
            platforms: platform,
            kick_slug: ch.kickSlug,
            twitch_login: ch.twitchSlug,
          });
          try {
            const data = await apiGet<ChannelVodsResponse>(`/api/channel/videos?${params}`);
            incoming.push(...(data.videos ?? []).map(mapApiChannelItem));
            delete errs[platform];
            const pe = data.per_platform_errors?.[platform];
            if (pe) errs[platform] = pe;
          } catch (err: unknown) {
            errs[platform] = err instanceof Error ? err.message : `Failed to fetch ${platform} VODs`;
          }
        };
        const vodTasks: Promise<void>[] = [];
        if (wantKick) vodTasks.push(fetchVods('Kick', ch.kickSlug));
        if (wantTwitch) vodTasks.push(fetchVods('Twitch', ch.twitchSlug));
        if (!wantKick) delete errs.Kick;
        if (!wantTwitch) delete errs.Twitch;
        await Promise.all(vodTasks);
        const latest = savedChannelsRef.current.find((c) => c.id === channelId) ?? ch;
        const vodVideos = mergeVodLists(latest.vodVideos ?? [], incoming);
        updateChannel(channelId, {
          vodVideos,
          vodErrors: errs,
          loading: false,
          updatedAt: new Date().toISOString(),
        });
      }

    } finally {
      if (!incremental) {
        channelRefreshInFlightRef.current.delete(flightKey);
        if (!silent) {
          updateChannel(channelId, { loading: false });
        }
      }
    }
  }, [updateChannel, channelContentFilter]);

  const refreshChannelRef = useRef(refreshChannel);
  refreshChannelRef.current = refreshChannel;

  const channelFiltersRef = useRef({
    channelContentFilter,
    kickEnabled,
    twitchEnabled,
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
    channelFiltersRef.current = { channelContentFilter, kickEnabled, twitchEnabled };

    const ch = savedChannelsRef.current.find((c) => c.id === selectedChannelId);
    if (!ch) return;
    const mode = channelContentFilter;

    const needsFetch =
      mode === 'clips'
        ? channelClipsMissing(ch, kickEnabled, twitchEnabled)
        : channelVodsMissing(ch, kickEnabled, twitchEnabled);
    if (!needsFetch) return;

    // The in-flight Set inside `refreshChannel` is the only guard
    // we keep: if a fetch for the same channel+mode is already
    // running, it will pick up the latest filter state when it
    // completes (the toggle only affects display filtering, and
    // the underlying fetch always pulls both platforms). Letting
    // an in-flight fetch finish is preferable to aborting and
    // restarting it, because the response carries both Kick and
    // Twitch entries — the next render will see the populated
    // cache and skip the re-fetch.
    void refreshChannelRef.current(selectedChannelId, undefined, mode, { silent: true });
  }, [channelContentFilter, kickEnabled, twitchEnabled, selectedChannelId]);

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
      // silent: true -> no loading spinner, cached data stays visible
      // undefined contentMode -> uses current filter (VODs or clips)
      void refreshChannelRef.current(c.id, c, undefined, { silent: true });
    });
  }, []);

  const addChannelFromSlugs = useCallback(async (
    displayName: string,
    kickSlug: string,
    twitchSlug: string,
  ) => {
    if (!kickSlug) return;
    if (savedChannels.length >= MAX_SAVED_CHANNELS) {
      setAddChannelNotice(`Max ${MAX_SAVED_CHANNELS} channels.`);
      return;
    }
    setAddChannelNotice(null);
    const id = `ch_${Date.now().toString(36)}`;
    const entry: SavedChannel = {
      id,
      displayName: displayName.trim() || kickSlug,
      kickSlug,
      twitchSlug,
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
    await refreshChannel(id, entry, 'vods');
    if (channelContentFilter === 'clips') {
      await refreshChannel(id, entry, 'clips');
    }
  }, [savedChannels.length, refreshChannel, channelContentFilter]);

  const handleAddChannel = useCallback(async () => {
    const raw = addChannelInput.trim();
    if (!raw) return;
    const { displayName, kickSlug, twitchSlug } = parseChannelInput(raw);
    await addChannelFromSlugs(displayName, kickSlug, twitchSlug);
    setAddChannelInput('');
  }, [addChannelInput, addChannelFromSlugs]);

  const toggleChannelSelection = useCallback((channelId: string) => {
    setSelectedChannelId((prev) => {
      if (prev === channelId) return null;
      setKickVisibleLimit(CHANNEL_INITIAL_VISIBLE);
      setTwitchVisibleLimit(CHANNEL_INITIAL_VISIBLE);
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
    };
    const updated: SavedChannel = {
      ...ch,
      displayName: nextDisplay,
      kickSlug: nextKick,
      twitchSlug: nextTwitch,
      ...cleared,
    };
    channelRefreshInFlightRef.current.delete(`${channelId}:vods`);
    channelRefreshInFlightRef.current.delete(`${channelId}:clips`);
    updateChannel(channelId, updated);
    await refreshChannel(channelId, updated);
  }, [editingChannelId, editingChannelName, savedChannels, updateChannel, refreshChannel]);

  const startEditPlatformSlug = useCallback((channelId: string, platform: 'Kick' | 'Twitch') => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    setEditingSlug({ channelId, platform });
    setEditingSlugValue(platform === 'Kick' ? ch.kickSlug : ch.twitchSlug);
  }, [savedChannels]);

  const commitEditPlatformSlug = useCallback(async () => {
    if (!editingSlug) return;
    const slug = editingSlugValue.trim();
    if (!slug) return;
    const ch = savedChannels.find((c) => c.id === editingSlug.channelId);
    if (!ch) return;

    const prevSlug = editingSlug.platform === 'Kick' ? ch.kickSlug : ch.twitchSlug;
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
    };
    const updated: SavedChannel = editingSlug.platform === 'Kick'
      ? { ...ch, kickSlug: slug, ...cleared }
      : { ...ch, twitchSlug: slug, ...cleared };

    channelRefreshInFlightRef.current.delete(`${channelId}:vods`);
    channelRefreshInFlightRef.current.delete(`${channelId}:clips`);
    updateChannel(channelId, editingSlug.platform === 'Kick'
      ? { kickSlug: slug, ...cleared }
      : { twitchSlug: slug, ...cleared });
    await refreshChannel(channelId, updated);
  }, [editingSlug, editingSlugValue, savedChannels, updateChannel, refreshChannel]);

  const handleExpandChannelList = useCallback(() => {
    if (kickEnabled) setKickVisibleLimit((n) => n + CHANNEL_EXPAND_STEP);
    if (twitchEnabled) setTwitchVisibleLimit((n) => n + CHANNEL_EXPAND_STEP);
  }, [kickEnabled, twitchEnabled]);
  const removeChannel = useCallback((channelId: string) => {
    setSavedChannels((prev) => {
      const next = prev.filter((c) => c.id !== channelId);
      if (selectedChannelId === channelId) {
        setSelectedChannelId(next[0]?.id ?? null);
      }
      return next;
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
      if (s.channel_content_filter === 'clips' || s.channel_content_filter === 'vods') {
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
    channelContentFilter,
  ]);

  // ── Fill VOD from channel ──
  const selectVod = useCallback((vodUrl: string, badge?: ChannelPreviewBadge) => {
    setUrl(vodUrl);
    setChannelVodPanelOpen(true);
    setUrlTabBarHidden(true);
    setPreviewChannelBadge(badge ?? null);
    void fetchVideoInfo(vodUrl);
  }, [fetchVideoInfo]);

  const carryExploreToUrl = useCallback((vod: ExplorePopupVod) => {
    selectVod(vod.url, {
      platform: vod.platform,
      platformListIndex: vod.platformListIndex,
      isClip: vod.isClip,
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

  const estBytes = estimateDownloadBytes(
    videoInfo,
    quality,
    clipSec,
    fullDur || clipSec,
  );

  const sourceQualityLabel = useMemo(
    () => sourceQualityOptionLabel(maxQualityLabelFromList(videoInfo?.qualities ?? [])),
    [videoInfo?.qualities],
  );

  const activePlatform = detectVideoPlatform(videoInfo, url);
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
    : actionBtnHover(urlPlatform);
  const urlInputClass = urlFetched
    ? 'w-full bg-zinc-950 border border-zinc-800 text-zinc-400 font-mono placeholder:text-zinc-600 pl-7 pr-7 py-1 focus:outline-none focus:border-zinc-500 transition-colors text-[10px] truncate'
    : 'w-full bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 pl-10 pr-10 py-3 focus:outline-none focus:border-white transition-colors uppercase text-sm';

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
              setUrl(e.target.value);
              setPreviewChannelBadge(null);
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
        <div className="flex flex-col gap-2 min-h-0 flex-1">
          <div className="border border-zinc-800 p-2 flex gap-2 bg-zinc-900/80 relative overflow-hidden shrink-0">
            <div className={`absolute top-0 right-0 w-10 h-10 opacity-15 blur-xl ${
              videoInfo.platform?.toLowerCase() === 'kick' ? 'bg-[#53fc18]' : 'bg-[#9146FF]'
            }`} />
            <div className="w-12 h-9 bg-zinc-800 border border-zinc-700 flex items-center justify-center shrink-0 overflow-hidden">
              {videoInfo.thumbnail ? (
                <img src={videoInfo.thumbnail} alt="" className="w-full h-full object-cover" />
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
                <span className="flex items-center gap-0.5 truncate">
                  <Clock size={9} /> {videoInfo.duration_string || fmtDuration(videoInfo.duration || 0)}
                </span>
                <span className="flex items-center gap-0.5 shrink-0 text-zinc-300">
                  <Database size={9} className={
                    videoInfo.platform?.toLowerCase() === 'kick' ? 'text-[#53fc18]' : 'text-[#9146FF]'
                  } /> {formatBytes(estBytes)}
                </span>
              </div>
            </div>
          </div>

          {pendingAddChannel && (
            <div className="border border-zinc-700 bg-zinc-900/90 p-2 flex flex-col gap-2 shrink-0">
              <p className="text-[10px] font-mono text-zinc-400">
                Add {pendingAddChannel.displayName} to channels?
              </p>
              <input
                type="text"
                value={pendingAddChannel.displayName}
                onChange={(e) => setPendingAddChannel((prev) => (
                  prev ? { ...prev, displayName: e.target.value } : prev
                ))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    void addChannelFromSlugs(
                      pendingAddChannel.displayName,
                      pendingAddChannel.kickSlug,
                      pendingAddChannel.twitchSlug,
                    );
                    setPendingAddChannel(null);
                  }
                }}
                className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono px-2 py-1 focus:outline-none focus:border-white text-[10px]"
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    void addChannelFromSlugs(
                      pendingAddChannel.displayName,
                      pendingAddChannel.kickSlug,
                      pendingAddChannel.twitchSlug,
                    );
                    setPendingAddChannel(null);
                  }}
                  className="flex-1 bg-white text-black font-black uppercase py-1 text-[10px] border-2 border-white"
                >
                  Add
                </button>
                <button
                  type="button"
                  onClick={() => setPendingAddChannel(null)}
                  className="flex-1 bg-zinc-800 text-zinc-300 font-black uppercase py-1 text-[10px] border-2 border-zinc-700"
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-2 shrink-0">
            <div className="flex flex-col gap-0.5">
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">Quality</span>
              <select value={quality} onChange={(e) => setQuality(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono py-1 px-1.5 focus:outline-none focus:border-white text-[10px] cursor-pointer">
                {videoInfo.qualities.length > 0 ? (
                  videoInfo.qualities.map((q) => <option key={q} value={q.toLowerCase()}>{q}</option>)
                ) : (
                  <>
                    <option value="source">{sourceQualityLabel}</option>
                    <option value="1080p">1080p</option>
                    <option value="720p">720p</option>
                    <option value="480p">480p</option>
                    <option value="360p">360p</option>
                  </>
                )}
              </select>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">Est. size</span>
              <div className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono py-1 px-1.5 text-[10px] flex items-center justify-center">
                {formatBytes(estBytes)}
              </div>
            </div>
          </div>

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
            <button type="button" onClick={openPreview}
              disabled={previewVideoLoading || vodDurationSec <= 0 || trimEndSec <= trimStartSec}
              className="w-full border border-zinc-700 text-zinc-400 hover:border-white hover:text-white font-mono text-[9px] uppercase font-bold py-1 flex items-center justify-center gap-1 disabled:opacity-40">
              {previewVideoLoading ? <Loader2 size={11} className="animate-spin" /> : <Eye size={11} />}
              Preview
            </button>

          </div>

          <button
            onClick={promptStartDownload}
            disabled={loading || !videoInfo}
            className={`w-full mt-auto shrink-0 border-2 border-white bg-black py-2 flex items-center justify-center gap-2 text-xs font-black uppercase transition-[transform,box-shadow,background-color,color] duration-150 hover:bg-white hover:text-black disabled:opacity-40 disabled:cursor-not-allowed ${
              urlPlatform === 'kick'
                ? 'shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18] hover:translate-x-0.5 hover:translate-y-0.5'
                : urlPlatform === 'twitch'
                  ? 'shadow-[3px_3px_0px_0px_#9146FF] hover:shadow-[2px_2px_0px_0px_#9146FF] hover:translate-x-0.5 hover:translate-y-0.5'
                  : 'shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18] hover:translate-x-0.5 hover:translate-y-0.5'
            }`}
          >
            <Download size={16} strokeWidth={3} />
            <span className="inline-flex items-center">
              <span className="tracking-widest">{currentIsClip ? 'Clip rip it' : 'VOD rip it'}</span>
              <span className="rip-btn-bang" aria-hidden="true">!</span>
            </span>
          </button>
        </div>
      )}
    </div>
  );

  const previewCtrlBtn = (fsOverlay: boolean, large = false) => {
    const pad = large ? 'p-2' : 'p-1.5';
    return fsOverlay
      ? `border border-white/20 bg-black/20 text-zinc-100/90 hover:bg-black/35 hover:border-white/50 ${pad} disabled:opacity-30 backdrop-blur-[1px]`
      : `border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white ${pad} disabled:opacity-40`;
  };

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
            className="self-center"
          />
          <span className={`text-[8px] font-mono w-11 shrink-0 text-right ${
            previewFullscreen ? 'text-zinc-300/90' : 'text-zinc-500'
          }`}>
            {formatHmsFull(previewTrimEnd - previewTrimStart)}
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
          disabled={!previewVideoReady || previewDurationSec <= 0}
          onChange={(e) => seekPreviewVideo(parseFloat(e.target.value))}
          className="flex-1 accent-white disabled:opacity-40"
        />
        <span className={`text-[9px] font-mono w-11 shrink-0 text-right ${previewFullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}>
          {formatHmsFull(previewDurationSec)}
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
        <PreviewQualityMenu
          levels={previewLevels}
          currentLevel={previewQualityLevel}
          menuOpen={previewQualityMenuOpen}
          setMenuOpen={setPreviewQualityMenuOpen}
          onSelect={applyPreviewQuality}
          disabled={!previewVideoReady}
          buttonClassName={previewCtrlBtn(previewFullscreen)}
          onMenuOpen={() => setPreviewVolumeMenuOpen(false)}
          popoverClassName={previewFullscreen
            ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
            : 'border-2 border-zinc-600 bg-zinc-950'}
        />
      </div>
      <div className="flex items-center gap-1.5 ml-auto">
        <button
          type="button"
          onClick={promptStartDownload}
          disabled={loading || !previewVideoReady || !videoInfo || (!currentIsClip && (previewOpen ? previewTrimEndRef.current <= previewTrimStartRef.current : trimEndSec <= trimStartSec))}
          className={previewCtrlBtn(previewFullscreen, true)}
          title={currentIsClip ? 'Download clip' : 'Download selected trim'}
        >
          <Scissors size={18} />
        </button>
        {opts.fsCornerExit ? (
          <button type="button" onClick={() => void togglePreviewFullscreen()}
            disabled={!previewVideoReady}
            className={previewCtrlBtn(previewFullscreen, true)}
            title="Exit fullscreen">
            <Minimize2 size={18} />
          </button>
        ) : (
          <button type="button" onClick={() => void togglePreviewFullscreen()}
            disabled={!previewVideoReady}
            className={previewCtrlBtn(previewFullscreen, true)}
            title="Fullscreen">
            <Maximize2 size={18} />
          </button>
        )}
      </div>
    </div>
  );

  return (
    <div
      className="vod-app-shell h-screen max-h-screen min-h-0 flex justify-center items-center overflow-hidden p-4 selection:bg-white selection:text-black bg-[#09090b]"
      style={{
        backgroundImage: 'radial-gradient(#27272a 1px, transparent 1px)',
        backgroundSize: 'calc(24px * var(--ui-scale)) calc(24px * var(--ui-scale))',
      }}
    >
      <div className={`vod-layout-row flex items-start max-w-full min-w-0 ${
        triplePanelLayout
          ? `w-full ${viewportTier === 'narrow' ? 'gap-2' : 'gap-3'} justify-center`
          : splitLayout
            ? `w-full ${viewportTier === 'narrow' ? 'gap-3' : 'gap-6'} justify-center`
            : `w-full ${viewportTier === 'wide' ? 'max-w-lg' : 'max-w-md'} justify-center gap-6`
      }`}>
      {previewOpen && (
        <div
          ref={previewPanelRef}
          className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white p-4 flex flex-col gap-3 min-h-0 min-w-0 ${platformCardShadow(activePlatform, true)}`}
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
              style={!previewFullscreen ? { aspectRatio: previewVideoAspect } : undefined}
            >
              <div
                className={`relative bg-black overflow-hidden cursor-pointer ${
                  'absolute inset-0 z-0'
                }`}

                onClick={() => {
                  focusPreviewPlayer();
                  togglePreviewPlay();
                }}
              >
                <video
                  ref={previewVideoRef}
                  className="w-full h-full object-contain pointer-events-none"
                  muted={previewMuted}
                  playsInline
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
                {previewVideoLoading && (
                  <div className="absolute inset-0 flex items-center justify-center bg-black/60 z-20 pointer-events-none">
                    <Loader2 size={40} className="animate-spin text-zinc-300" />
                  </div>
                )}
              </div>
              <div
                ref={previewControlsRef}
                data-player-controls
                data-preview-fs-ui={previewFullscreen ? '' : undefined}

                className={
                  previewFullscreen
                    ? `absolute bottom-0 left-0 right-0 z-10 flex flex-col gap-1 px-2 pb-2 pt-2 max-h-[50vh] overflow-hidden bg-gradient-to-t from-black/90 to-black/75 transition-opacity duration-150 ${
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
          className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white p-4 flex flex-col gap-2 min-h-0 ${platformCardShadow(activePlatform, true)}`}
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
          <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
            {urlTabContent}
          </div>
          <PanelResizeHandles onPointerDown={onUrlAsidePanelResize} insetPx={panelResizeHandleInset(true)} />
        </div>
      )}
      <div
        ref={mainPanelRef}
        className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white flex flex-col min-h-0 ${
          triplePanelLayout ? 'p-4 gap-3' : urlMainCompact ? 'p-4 gap-2' : 'p-6 gap-4'
        } ${platformCardShadow(activePlatform)}`}
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
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> Downloader
              </p>
            )}
            {triplePanelLayout && !urlMainCompact && (
              <p className="text-zinc-500 text-[9px] font-mono tracking-widest uppercase mt-0.5 truncate">
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span>
              </p>
            )}
          </div>
          <div className={`flex gap-1 shrink-0 ${mainCardHeaderCompact ? 'mt-1' : 'mt-2'}`}>
            <div className="w-2 h-2 bg-[#53fc18] rounded-full animate-pulse" />
            <div className="w-2 h-2 bg-[#9146FF] rounded-full animate-pulse" style={{ animationDelay: '0.5s' }} />
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
          <div className="border-2 border-red-500/50 bg-red-500/10 p-3 text-red-400 text-xs font-mono flex items-center gap-2 shrink-0">
            <AlertCircle size={14} />
            {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400/60 hover:text-red-400">
              <X size={14} />
            </button>
          </div>
        )}

        <div className={`flex-1 min-h-0 ${
          showUrlInMainCard
            ? 'overflow-hidden flex flex-col'
            : 'overflow-y-auto overflow-x-hidden custom-scrollbar pr-1 pb-2 overscroll-y-contain'
        }`}>
        {/* ════════════════════════════ URL TAB ════════════════════════════ */}
        {showUrlInMainCard && urlTabContent}

        {/* ════════════════════════════ CHANNELS TAB ════════════════════════════ */}
        {tab === 'channels' && (
          <div className="flex flex-col gap-3">
            <div className="flex gap-2">
              <input type="text" value={addChannelInput}
                onChange={(e) => setAddChannelInput(e.target.value)}
                placeholder="CHANNEL NAME OR URL..."
                onKeyDown={(e) => e.key === 'Enter' && handleAddChannel()}
                className="flex-1 bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 px-2 py-1.5 focus:outline-none focus:border-white uppercase text-[10px] min-h-0" />
              <button type="button" onClick={handleAddChannel}
                disabled={channelsLoading || !addChannelInput.trim()}
                className="bg-white text-black font-black uppercase px-3 text-xs border-2 border-white disabled:opacity-50">
                <Plus size={14} />
              </button>
            </div>
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
                    className={`relative flex items-center gap-1 border px-2 py-1 ${
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
                      <button type="button" onClick={() => toggleChannelSelection(ch.id)}
                        className="flex-1 text-left text-xs font-mono text-zinc-200 truncate hover:text-white select-none">
                        {ch.displayName}
                      </button>
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
                        channelRefreshInFlightRef.current.delete(`${ch.id}:vods`);
                        channelRefreshInFlightRef.current.delete(`${ch.id}:clips`);
                        void refreshChannel(ch.id, undefined, channelContentFilter);
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
                    <div className="flex flex-col gap-2 ml-1 pl-2 border-l-2 border-zinc-700 py-1">
                      <div className="flex items-center gap-2 flex-nowrap min-h-[22px] shrink-0">
                        {(['Kick', 'Twitch'] as const).map((platform) => {
                          const isKick = platform === 'Kick';
                          const enabled = isKick ? kickEnabled : twitchEnabled;
                          const slug = isKick ? ch.kickSlug : ch.twitchSlug;
                          const color = isKick ? '#53fc18' : '#9146FF';
                          const loading = isKick ? kickBrowseLoading : twitchBrowseLoading;
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
                                  onClick={() => (isKick ? setKickEnabled : setTwitchEnabled)((v) => !v)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                      e.preventDefault();
                                      (isKick ? setKickEnabled : setTwitchEnabled)((v) => !v);
                                    }
                                  }}
                                  className={`flex items-center gap-1 px-2 py-0.5 border font-mono text-[10px] uppercase font-bold cursor-pointer select-none ${
                                    enabled ? '' : 'opacity-40'
                                  }`}
                                  style={enabled ? { borderColor: color, color } : { borderColor: '#3f3f46' }}
                                >
                                  <input type="checkbox" checked={enabled} readOnly tabIndex={-1}
                                    className="w-3 h-3 pointer-events-none" style={{ accentColor: color }} />
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
                        <div className="flex items-center gap-2 font-mono text-[10px] uppercase shrink-0">
                          <span className="text-zinc-500">Show:</span>
                          <button
                            type="button"
                            onClick={() => {
                              if (channelContentFilter !== 'vods') {
                                setChannelContentFilter('vods');
                              }
                            }}
                            className={`px-2 py-0.5 border font-bold ${
                              channelContentFilter === 'vods'
                                ? 'border-white text-white bg-zinc-900'
                                : 'border-zinc-700 text-zinc-500 hover:text-white'
                            }`}
                          >
                            VODs
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              if (channelContentFilter !== 'clips') {
                                setChannelContentFilter('clips');
                              }
                            }}
                            className={`px-2 py-0.5 border font-bold ${
                              channelContentFilter === 'clips'
                                ? 'border-white text-white bg-zinc-900'
                                : 'border-zinc-700 text-zinc-500 hover:text-white'
                            }`}
                          >
                            Clips
                          </button>
                        </div>
                      </div>
                      {(() => {
                        const msg = formatChannelErrorMessage(
                          ch,
                          channelContentFilter,
                          kickEnabled,
                          twitchEnabled,
                        );
                        return msg ? (
                          <p className="text-red-400 text-[10px] font-mono">{msg}</p>
                        ) : null;
                      })()}
                      {channelsLoading && visibleChannelVideos.length === 0 ? (
                        <div className="flex justify-center py-4 text-zinc-500">
                          <Loader2 size={18} className="animate-spin" />
                        </div>
                      ) : visibleChannelVideos.length === 0 ? (
                        <p className="text-center text-zinc-600 font-mono text-[10px] py-3">
                          {channelContentFilter === 'clips' ? 'No clips' : 'No VODs'}
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
                                  className="accent-[#53fc18]"
                                />
                                Select all
                              </label>
                              <button
                                type="button"
                                onClick={handleBulkDownloadChannelVods}
                                className="border border-[#53fc18] bg-[#53fc18]/10 text-[#53fc18] hover:bg-[#53fc18]/20 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider flex items-center gap-1"
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
                            return (
                              <div
                                key={`${v.platform}-${v.id}-${i}`}
                                role="button"
                                tabIndex={0}
                                onClick={() => selectVod(fullUrl, {
                                  platform: v.platform,
                                  platformListIndex: v.platformListIndex,
                                  isClip: isClipItem,
                                })}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter' || e.key === ' ') {
                                    e.preventDefault();
                                    selectVod(fullUrl, {
                                      platform: v.platform,
                                      platformListIndex: v.platformListIndex,
                                      isClip: isClipItem,
                                    });
                                  }
                                }}
                                className="flex items-center gap-1.5 border border-zinc-800 bg-zinc-950 px-2 py-1.5 hover:border-zinc-600 hover:text-white cursor-pointer group"
                              >
                                <input
                                  type="checkbox"
                                  checked={selectedChannelVodUrls.has(fullUrl)}
                                  onChange={() => toggleChannelVodSelection(fullUrl)}
                                  onClick={(e) => e.stopPropagation()}
                                  className="accent-[#53fc18] shrink-0"
                                />
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
                                    <span className="text-[9px] text-zinc-400 block truncate">
                                      {subline}
                                    </span>
                                  )}
                                </div>
                                <button
                                  type="button"
                                  title={isClipItem ? 'Preview clip' : 'Preview VOD'}
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
      <div className="fixed bottom-10 right-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        TWITCH
      </div>

      <NeedleGlancePopup glance={needleGlance} vodDurationSec={vodDurationSec} />
      <DownloadConfirmDialog
        open={downloadConfirmOpen}
        title={downloadConfirmCopy.title}
        message={downloadConfirmCopy.message}
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
