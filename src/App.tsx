import { memo, useState, useEffect, useCallback, useLayoutEffect, useMemo, useRef, type Dispatch, type KeyboardEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import { createTwitchAdRotationHandler, twitchAdBlockHlsConfig } from './twitchAdBlock';
import { detectSystemLanguage, langFamily, setLanguage, useI18n, type Lang } from './i18n';
import {
  Download, Info, Play, Pause, Link2, X, Clock,
  Users, Database, Settings2, Loader2, Search,
  AlertCircle, RefreshCw, Pencil, Plus,
  ExternalLink, Eye, Volume2, VolumeX, Maximize2, Minimize2,
  GripVertical,
} from 'lucide-react';
import { openTwitchClipEditor } from './twitchClip';
import TwitchClipPopup from './components/TwitchClipPopup';
import TwitchLogoIcon from './components/TwitchLogoIcon';
import ChannelExplorePopup, { type ExplorePopupVod } from './ChannelExplorePopup';
import ArchiveSearchPopup from './components/ArchiveSearchPopup';
import { buildArchiveVodUrl, type ArchiveSearchHit, type ArchiveVideoRow } from './archiveSearchUtils';
import { archiveVideoIdFromUrl, isNativeArchiveVideoId } from './archiveScope';
import LocalFilePopup, { type LocalFilePopupItem } from './LocalFilePopup';
import PreviewQualityMenu from './PreviewQualityMenu';
import { LivePlayerPopup } from './components/LivePlayerPopup';
import { LiveBadge } from './components/LiveBadge';
import { liveArchiveContext, appendLivePopup } from './livePlayerLevels';
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
  youtubePreviewAllowHeights,
  seekYoutubeWindowHls,
  windowHlsVideoTimeSec,
  isPositionInWindowHlsMux,
  attachPreviewBufferingListeners,
  applyVideoLocalSeek,
  reloadWindowHlsAtPosition,
  shieldPreviewBuffering,
  createPreviewSessionWithRetry,
  type PreviewLevelOption,
} from './previewPlayerUtils';
import { PreviewTiming, waitVideoPlayable, notePreviewGesture } from './previewTiming';
import DownloadConfirmDialog from './components/DownloadConfirmDialog';
import EditableHmsTime from './components/EditableHmsTime';
import { formatHmsFull } from './utils';
import { createFullscreenGate, type FullscreenGate } from './utils/fullscreenGate';
import { actionBtnHover, platformPreviewCtrlBtn, platformCardShadow, platformVodPanelBtn, platformWatchPreviewBtn, platformBulkDownloadBtn, type PlatformStyleKey } from './platformStyles';
import { fmtDuration, fmtShort, fmtClipDuration, formatClipDurationHuman, fmtDateAndAgo, fmtViews, parseVideoTs, formatBytes, basename, sourceQualityOptionLabel } from './formatters';
import type { VideoInfo, ChannelVideo, ListedChannelVideo, SavedChannel, ChannelPreviewBadge, AppSettings, UpdateInfo, DownloadState, DownloadsResponse, Tab, LayoutPanelBoundsInput, PersistedPanelLayout, PreviewSessionResponse, PanelPos } from './types';
import { detectUrlPlatform, isClipUrl, detectVideoPlatform, bestAvailableQuality, channelVideoDurationSec, videoInfoDurationSec, syncDurationFromPreviewSession, isLikelyClip, isMembersOnlyVideo, isPublicVideo, mergeVodLists, mergeClipLists, channelClipsMissing, channelVodsMissing, channelStreamsMissing, channelHasCachedContent, effectivePlatformFlags, mergeClipPlatformsFetched, mergeVodPlatformsFetched, buildVodUrl, parseChannelInput, slugFromVideoUrl, isChannelAlreadySaved, deriveChannelDisplayName, normalizeSavedChannel, displayTitle, loadSavedChannels, persistChannels, isHiddenChannelPlatformError, channelVodSubline, reorderChannelsById, mapApiChannelItem, channelInsertIndex, estimateDownloadBytes, resolveVideoThumbnail, findCachedVideoThumbnail, isSyntheticArchiveId, CHANNEL_INITIAL_VISIBLE, CHANNEL_EXPAND_STEP, CHANNEL_FETCH_LIMIT, CHANNEL_INCREMENTAL_LIMIT, CHANNEL_UI_STORAGE_KEY, loadStoredChannelUi, channelPlatformVisibleSlice, channelPlatformCanExpand, sortChannelVideosByMode, CHANNEL_RECENT_DAYS, channelLinkDraftFromParsed, channelLinkDraftSlugs, type ChannelLinkDraft, loadStoredChannelLiveStatuses, persistChannelLiveStatuses, type StoredChannelLiveStatus } from './channelUtils';
import ChannelLinkCard from './components/ChannelLinkCard';
import { YOUTUBE_COLOR, platformAccentColor, platformStyleKey, platformActiveBorder, vodCheckboxStyle } from './platformColors';
import { clampTrimEndpoints, trimButtonDeltaForEndpoint, adjustTrimEndpointByDelta, zoomWindowFromView, fracToSec, secToFrac, zoomTrimViewAround, resolveTimestampSeek, TRIM_ZOOM_STEP, type TrimRangeOpts, type TrimViewWindow } from './trimUtils';
import { panelMaxW, layoutMaxPanelHeight, layoutMaxPanelWidthAtSiblingMins, clampPanelSizeForLayout, clampAllLayoutPanels, clampPreviewPanelWidth, resizeLayoutGivingWidthTo, layoutRowEdgeInsets, layoutRowHasMultiplePanels as layoutHasMultiplePanels, applyPanelSize, startPanelResizeDrag, applyPanelWidth, startPanelWidthResize, defaultPanelLayout, loadPanelLayout, persistPanelLayout, clampLayoutNumber, sanitizeStoredPanelSize, effectiveLayoutFromPreferred, userOwnedWidthsFrom, healSqueezedPanelLayout, rowPanelHeightFromPreview, ownedPanelHeightSeed, type EffectivePanelLayout, PREVIEW_KEY_SKIP_SEC, PREVIEW_FS_CONTROLS_HIDE_MS, PREVIEW_DEFAULT_VOLUME, PREVIEW_PANEL_MIN_W, PREVIEW_PANEL_CHROME_H_EST, PREVIEW_VIDEO_ASPECT_DEFAULT, PANEL_MIN, EXPLORE_POPUP_Z, SEARCH_POPUP_Z, MAX_EXPLORE_POPUPS } from './layoutUtils';
import ChannelListIndexBadge from './components/ChannelListIndexBadge';
import ChannelPlatformLabel from './components/ChannelPlatformLabel';
import PlatformVodIcon from './components/PlatformVodIcon';
import ChannelClipThumb from './components/ChannelClipThumb';
import ClipDurationAdjustButtons from './components/ClipDurationAdjustButtons';
import NeedleGlancePopup, { type NeedleGlanceState } from './components/NeedleGlancePopup';
import QueueTab from './components/QueueTab';
import SettingsTab from './components/SettingsTab';
import PreviewChatPanel from './components/PreviewChatPanel';
import { PanelResizeHandles, type ResizeEdge } from './explorePopupUtils';
import { shouldIgnorePlayerKeyEvent } from './keyboardUtils';
import { applyDownloadSseEvent, useDownloadStreams } from './hooks/useDownloadStreams';import { apiGet, apiPost, apiDelete } from './hooks/useApiClient';
import { useViewportTier } from './useViewportTier';
import { usePreviewPlayer } from './hooks/usePreviewPlayer';
import { useDirectMSEPlayer } from './hooks/useDirectMSEPlayer';
import { youtubeIframeCommand, youtubeIframeListen } from './youtubeEmbed';
import { previewRetryAfterError, previewRetryMode, type PreviewRetryStage, type PreviewRetryState } from './previewRetry';

// ─── TYPES (migrated to src/types.ts) ───────────────

interface ChannelLiveStatus {
  channel_id: string;
  live: Array<{
    platform: string;
    is_live: boolean;
    title: string;
    url: string;
    headers: Record<string, string>;
    type: string;
    thumbnail_url?: string;
    viewer_count?: number;
    started_at?: string;
  }>;
}

const IS_DEV_UI = import.meta.env.DEV;
/** Concurrent live players allowed at once (user requirement). */
const MAX_LIVE_POPUPS = 5;

interface LivePopupItem {
  id: number;
  entry: ChannelLiveStatus['live'][number];
  /** All of the channel's live entries — the popup auto-advances through
   *  them when an entry's session fails/stalls (one attempt each). */
  entries: ChannelLiveStatus['live'];
  channelName: string;
  channel: SavedChannel | null;
}
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

// ─── CHANNEL ROW ────────────────────────────────────────────────────────────

interface ChannelRowProps {
  ch: SavedChannel;
  index: number;
  selected: boolean;
  isEditing: boolean;
  editingChannelName: string;
  dragId: string | null;
  dropInsertIndex: number | null;
  isLast: boolean;
  savedChannelsLength: number;
  liveStatus: ChannelLiveStatus | undefined;
  openLivePreview: (entry: ChannelLiveStatus['live'][number], entries: ChannelLiveStatus['live'], channelName?: string, channel?: SavedChannel | null) => Promise<void> | void;
  onOpenChannelSearch: (ch: SavedChannel) => void;
  channelListRef: MutableRefObject<HTMLDivElement | null>;
  toggleChannelSelection: (id: string) => void;
  removeChannel: (id: string) => void;
  refreshChannel: (channelId: string, channelOverride?: SavedChannel, contentMode?: 'vods' | 'clips' | 'streams', opts?: { incremental?: boolean; silent?: boolean; force?: boolean }) => Promise<unknown>;
  clearChannelRefreshFlight: (channelId: string, mode?: 'vods' | 'clips' | 'streams') => void;
  startRenameChannel: (id: string) => void;
  commitRenameChannel: () => void;
  setEditingChannelId: Dispatch<SetStateAction<string | null>>;
  setEditingChannelName: Dispatch<SetStateAction<string>>;
  removePlatformFromChannel: (channelId: string, platform: 'Kick' | 'Twitch' | 'YouTube') => void;
  channelContentFilter: 'vods' | 'clips' | 'streams';
  setSavedChannels: Dispatch<SetStateAction<SavedChannel[]>>;
  setChannelDragId: Dispatch<SetStateAction<string | null>>;
  setChannelDropInsertIndex: Dispatch<SetStateAction<number | null>>;
}

const ChannelRow = memo(function ChannelRow({
  ch, index, selected, isEditing, editingChannelName,
  dragId, dropInsertIndex, isLast, savedChannelsLength, liveStatus,
  openLivePreview, onOpenChannelSearch,
  channelListRef,
  toggleChannelSelection, removeChannel, refreshChannel, clearChannelRefreshFlight,
  startRenameChannel, commitRenameChannel,
  setEditingChannelId, setEditingChannelName,
  removePlatformFromChannel, channelContentFilter,
  setSavedChannels, setChannelDragId, setChannelDropInsertIndex,
}: ChannelRowProps) {
  const { t } = useI18n();
  const dropAbove = dragId != null && dropInsertIndex === index;
  const dropBelow = dragId != null && dropInsertIndex === savedChannelsLength && isLast;
  const liveEntries = liveStatus?.live.filter((e) => e.is_live && e.url) ?? [];
  return (
    <div
      data-channel-row
      data-channel-id={ch.id}
      className={`relative flex items-center gap-1 border px-2 py-1 overflow-visible ${
        selected ? 'border-white bg-zinc-900' : 'border-zinc-800'
      } ${ch.id === dragId ? 'opacity-45' : ''} ${
        dropAbove ? 'shadow-[inset_0_2px_0_0_rgba(255,255,255,0.95)]' : ''
      } ${dropBelow ? 'shadow-[inset_0_-2px_0_0_rgba(255,255,255,0.95)]' : ''}`}
    >
      <button
        type="button"
        title={t('Drag to reorder')}
        aria-label={t('Reorder {name}', { name: ch.displayName })}
        disabled={isEditing}
        onPointerDown={(e) => {
          if (isEditing) return;
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
      {isEditing ? (
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
          className="flex-1 min-w-0 whitespace-nowrap overflow-visible text-left text-xs font-mono text-zinc-200 hover:text-white select-none cursor-pointer"
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
      <LiveBadge
        entries={liveEntries}
        invisible={liveEntries.length === 0}
        onClick={liveEntries.length ? (e) => {
          e.stopPropagation();
          void openLivePreview(liveEntries[0], liveEntries, ch.displayName, ch);
        } : undefined}
        ariaLabel={t('Live {name}', { name: ch.displayName })}
      />
      <button
        type="button"
        title={t('Search channel')}
        onClick={(e) => { e.stopPropagation(); onOpenChannelSearch(ch); }}
        className="text-zinc-600 hover:text-white p-0.5"
      >
        <Search size={11} />
      </button>
      <button type="button" title={t('Edit')}
        onClick={(e) => { e.stopPropagation(); startRenameChannel(ch.id); }}
        className="text-zinc-600 hover:text-white p-0.5">
        <Pencil size={11} />
      </button>
      <button type="button" title={t('Reload')}
        onClick={(e) => {
          e.stopPropagation();
          clearChannelRefreshFlight(ch.id);
          void refreshChannel(ch.id, undefined, channelContentFilter, { force: true, incremental: true });
        }}
        disabled={ch.loading}
        className="text-zinc-600 hover:text-white p-0.5 disabled:opacity-40">
        {ch.loading ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
      </button>
      <button type="button" title={t('Delete')}
        onClick={(e) => { e.stopPropagation(); removeChannel(ch.id); }}
        className="text-zinc-600 hover:text-red-400 p-0.5">
        <X size={11} />
      </button>
    </div>
  );
});


// ─── APP ─────────────────────────────────────────────────────────────────────

// Defaults mirror backend models/schemas.py AppSettings so the Settings tab
// renders real content before the first GET /api/settings resolves.
const DEFAULT_SETTINGS: AppSettings = {
  download_folder: '',
  download_threads: 8,
  max_cache_mb: 512,
  throttle_kib: -1,
  ffmpeg_path: '',
  temp_folder: '',
  cache_dir: '',
  oauth: '',
  quality: '1080p',
  channel_kick_enabled: true,
  channel_twitch_enabled: true,
  channel_youtube_enabled: true,
  channel_content_filter: 'vods',
  twitch_helix_token: '',
  youtube_data_api_key: '',
};

export default function App() {
  const viewportTier = useViewportTier();
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>('url');

  // URL mode
  const [url, setUrl] = useState('');
  const [videoInfo, setVideoInfo] = useState<VideoInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [videoInfoThumbFailed, setVideoInfoThumbFailed] = useState(false);

  // Per-media preview retry: which single media failed + how many retries
  // already failed, so the error banner's RETRY button escalates stage → full.
  const [previewRetry, setPreviewRetry] = useState<PreviewRetryState | null>(null);
  const previewRetryRef = useRef<PreviewRetryState | null>(null);
  const previewRetryingRef = useRef(false);
  // Bumping this re-runs the playback-attach effect (stage retry for the same session).
  const [previewRetryTick, setPreviewRetryTick] = useState(0);
  const setPreviewRetryBoth = useCallback((s: PreviewRetryState | null) => {
    previewRetryRef.current = s;
    setPreviewRetry(s);
  }, []);

  /** Record which single media failed and at which stage. Keeps the per-media
   *  retry count so the NEXT RETRY click escalates stage retry → full pipeline. */
  const markPreviewError = useCallback((mediaUrl: string, stage: PreviewRetryStage) => {
    const wasRetry = previewRetryingRef.current;
    previewRetryingRef.current = false;
    setPreviewRetryBoth(previewRetryAfterError(previewRetryRef.current, mediaUrl, stage, wasRetry));
  }, [setPreviewRetryBoth]);

  // Download options
  const [quality, setQuality] = useState('source');
  const [downloadAsAudio, setDownloadAsAudio] = useState(false);
  const urlPlatform = detectUrlPlatform(url);
  const [trimStartSec, setTrimStartSec] = useState(0);
  const [previewMetaDurationSec, setPreviewMetaDurationSec] = useState(0);
  const [trimEndSec, setTrimEndSec] = useState(3600);
  const [trimPanelHeight, setTrimPanelHeight] = useState(0);
  /** Wheel zoom for the preview trim rail — 1 = full duration visible. */
  const [previewTrimZoom, setPreviewTrimZoom] = useState(1);
  /** Window centre as a fraction (0..1) of the full duration. */
  const [previewTrimAnchorFrac, setPreviewTrimAnchorFrac] = useState(0.5);
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
  /** Twitch clip editor open — transient notice shown in the transport row. */
  const [clipOpenNotice, setClipOpenNotice] = useState<{ kind: 'error' | 'ok'; text: string } | null>(null);
  const [clipOpening, setClipOpening] = useState(false);
  /** Twitch clip mini-preview (VOD path) — opened at the current playhead. */
  const [twitchClipPopup, setTwitchClipPopup] = useState<{
    url: string;
    broadcasterLogin: string;
    vodId: string;
    playheadSec: number;
    vodDurationSec: number;
  } | null>(null);
  const clipOpenNoticeTimerRef = useRef<number | null>(null);
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  const previewPlayheadRef = useRef<HTMLDivElement>(null);
  const previewCurrentTimeRef = useRef(0);
  const previewTimeUiRef = useRef(0);
  const vodDurationSecRef = useRef(0);
  const previewContainerRef = useRef<HTMLDivElement>(null);
  const previewControlsRef = useRef<HTMLDivElement>(null);
  const previewHlsRef = useRef<Hls | null>(null);
  const previewIsLiveRef = useRef(false);
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
    /** Quality policy: YouTube session resolved without user auth — 360p only. */
    anonymous?: boolean;
    /** True for create_live_session sessions (live popup) — YouTube live tiers allowed. */
    isLive?: boolean;
    /** WS-3: detected channel language from the session response ('' = unknown). */
    channelLanguage?: string;
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
  /** URL of the last VOD clicked in a list — auto-opens the main preview once its info loads. */
  const autoOpenPreviewPendingRef = useRef<string | null>(null);
  /** Bumped on every list-VOD click so re-clicking the already-active VOD re-fires the auto-open effect. */
  const [autoOpenPreviewTick, setAutoOpenPreviewTick] = useState(0);
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
      if (msg) {
        setError(msg);
        markPreviewError(url.trim(), 'playback');
      }
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
  /** URL for which the user manually picked a download quality. When a
   *  re-extract/refresh runs for the SAME url, the auto-set to the best
   *  available tier must NOT clobber that pick (fixes downloads silently
   *  switching quality after the user chose it in the VOD trim panel). */
  const qualityUserTouchedUrlRef = useRef<string | null>(null);
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
  const [archiveSearchOpen, setArchiveSearchOpen] = useState(false);
  /** Per-video scope for the archive search popup (SEARCH THIS VIDEO). */
  const [archiveSearchScope, setArchiveSearchScope] = useState<{ videoId: string; title: string } | null>(null);
  /** Channel scope (comma-joined slugs) for the archive search popup — set
   *  by the per-channel Search action in the channel list row. */
  const [archiveSearchChannel, setArchiveSearchChannel] = useState<string | null>(null);
  const openChannelSearch = useCallback((ch: SavedChannel) => {
    const slugs = [ch.twitchSlug, ch.kickSlug, ch.youtubeSlug]
      .map((s) => (s || '').trim())
      .filter(Boolean);
    setArchiveSearchChannel(slugs.join(',') || null);
    setArchiveSearchOpen(true);
  }, []);
  /** Floating archive-search popup anchored to the main preview panel
   *  (SEARCH THIS VIDEO / SEARCH ARCHIVE). Floating — never part of the
   *  panel row — so opening it cannot move or resize the panels. */
  const [previewSearchOpen, setPreviewSearchOpen] = useState(false);
  /** Seed position computed from the preview panel rect at open time. */
  const previewSearchAnchorRef = useRef<PanelPos | null>(null);
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
  // ponytail: lock the preview container height across refetch/aspect changes.
  // The CSS `aspect-ratio` would re-derive the height from the new video aspect
  // on every metadata load, collapsing a 16:9 panel to a square when the next
  // video is 1:1. Storing the last rendered height in a ref and using it as
  // explicit `height` keeps the panel size the user picked. Upgrade: persist
  // this to loadPanelLayout so the height survives reloads.
  const previewPanelHeightRef = useRef(0);
  const urlAsidePanelSizeRef = useRef(initialPanelLayout.urlAside);
  const mainPanelSizeRef = useRef(initialPanelLayout.main);
  /**
   * User-owned widths: the width each panel last had when the user dragged IT.
   * Sibling squeezes never write here, so the next drag restores the row to the
   * user's shapes instead of latching the squeezed (thin) widths forever.
   * Persisted via `owned` so the restore target survives reloads.
   */
  const preferredDragRef = useRef<{ preview: number; urlAside: number; main: number }>(
    userOwnedWidthsFrom(initialPanelLayout),
  );
  /**
   * User-owned heights: the height each center panel last had when the user
   * dragged ITS south edge. Preview-driven row sync (syncRowHeightsToPreview)
   * falls back to these when the preview shrinks instead of ratcheting at the
   * tall value forever. Seeded from the stored height capped at the panel's
   * aspect-consistent height, so heights ratcheted by the pre-fix one-way sync
   * heal toward the default shape on load; a deliberate in-session S-edge drag
   * overwrites the seed.
   */
  const ownedPanelHeightRef = useRef<{ urlAside: number; main: number }>({
    urlAside: ownedPanelHeightSeed('urlAside', initialPanelLayout.urlAside.w, initialPanelLayout.urlAside.h),
    main: ownedPanelHeightSeed('main', initialPanelLayout.main.w, initialPanelLayout.main.h),
  });
  /** Previous previewOpen, for the open→closed height-restore transition. */
  const prevPreviewOpenRef = useRef(previewOpen);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const previewRowRef = useRef<HTMLDivElement>(null);
  const previewColHRef = useRef(0);
  const urlAsidePanelRef = useRef<HTMLDivElement>(null);
  const mainPanelRef = useRef<HTMLDivElement>(null);
  const panelLayoutPersistReadyRef = useRef(false);
  const panelLayoutSaveTimerRef = useRef<number | null>(null);

  const restorePanelLayout = useCallback((pl: PersistedPanelLayout) => {
    const fallback = defaultPanelLayout();
    const clampedUrl = sanitizeStoredPanelSize(pl.urlAside, fallback.urlAside);
    const clampedMain = sanitizeStoredPanelSize(pl.main, fallback.main);
    const clampedPreviewW = clampLayoutNumber(
      pl.previewPanelWidth,
      PREVIEW_PANEL_MIN_W,
      panelMaxW(),
      fallback.previewPanelWidth,
    );
    // Heal layouts corrupted by the pre-owned resize bug (panels parked at
    // their minimum are squeeze artifacts): owned widths reset to the default
    // shape, and min-parked visual widths grow back when the row has slack.
    const healed = healSqueezedPanelLayout({
      previewPanelWidth: clampedPreviewW,
      urlAside: clampedUrl,
      main: clampedMain,
      livePanelWidth: pl.livePanelWidth ?? fallback.livePanelWidth,
      owned: pl.owned,
    });
    // Preferred (dragged) widths are restored from storage as-is, within hard caps.
    // Runtime viewport clamps happen via effectiveLayoutFromPreferred each render.
    // No localStorage write here — the user's stored preferred widths must survive.
    previewPanelWidthRef.current = healed.previewPanelWidth;
    urlAsidePanelSizeRef.current = healed.urlAside;
    mainPanelSizeRef.current = healed.main;
    setPreviewPanelWidth(healed.previewPanelWidth);
    setUrlAsidePanelSize(healed.urlAside);
    setMainPanelSize(healed.main);
    preferredDragRef.current = { ...healed.owned };
    ownedPanelHeightRef.current = {
      urlAside: ownedPanelHeightSeed('urlAside', healed.urlAside.w, healed.urlAside.h),
      main: ownedPanelHeightSeed('main', healed.main.w, healed.main.h),
    };
  }, []);

  const readCurrentPanelLayout = useCallback((): PersistedPanelLayout => ({
    previewPanelWidth: previewPanelWidthRef.current,
    urlAside: { ...urlAsidePanelSizeRef.current },
    main: { ...mainPanelSizeRef.current },
    owned: { ...preferredDragRef.current },
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
  // localStorage write is gated by panelLayoutPersistReadyRef so the mount/restore path
  // (which setState'd from loadPanelLayout) does NOT immediately overwrite the stored
  // preferred widths.
  useEffect(() => {
    if (!panelLayoutPersistReadyRef.current) return;
    persistPanelLayout({
      previewPanelWidth,
      urlAside: urlAsidePanelSize,
      main: mainPanelSize,
      owned: { ...preferredDragRef.current },
    });
    if (panelLayoutSaveTimerRef.current) {
      window.clearTimeout(panelLayoutSaveTimerRef.current);
    }
    // Debounced reconciler for state-driven changes; serializes the latest
    // refs (incl. `owned`) via the keepalive flush so an in-flight POST
    // survives navigation. Drag-end calls flushPanelLayoutToBackend() directly.
    panelLayoutSaveTimerRef.current = window.setTimeout(() => {
      flushPanelLayoutToBackend();
    }, 400);
    return () => {
      if (panelLayoutSaveTimerRef.current) {
        window.clearTimeout(panelLayoutSaveTimerRef.current);
      }
    };
  }, [previewPanelWidth, urlAsidePanelSize, mainPanelSize, flushPanelLayoutToBackend]);

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
  const [channelLiveStatuses, setChannelLiveStatuses] = useState<Record<string, ChannelLiveStatus>>(() => loadStoredChannelLiveStatuses() as unknown as Record<string, ChannelLiveStatus>);
  const [channelDragId, setChannelDragId] = useState<string | null>(null);
  const [channelDropInsertIndex, setChannelDropInsertIndex] = useState<number | null>(null);
  const [isLive, setIsLive] = useState(false);
  /** Concurrent live players — capped at MAX_LIVE_POPUPS (5). */
  const [livePopups, setLivePopups] = useState<LivePopupItem[]>([]);
  const livePopupsRef = useRef<LivePopupItem[]>([]);
  const livePopupIdRef = useRef(0);
  const [livePopupNotice, setLivePopupNotice] = useState<string | null>(null);
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
  /** Clip listing: time range (server filter) + sort key. Defaults to Today. */
  const [clipRangeDays, setClipRangeDays] = useState<number>(1);
  const [clipSort, setClipSort] = useState<'date' | 'views'>('date');

  // Era window: each Range option covers (previous step, selected step] —
  // e.g. 1mo shows clips 14–30 days old, NOT "up to 1 month old".
  const clipRangeMinDays = useMemo(() => {
    const steps = [1, 7, 14, 30, 180, 365];
    const idx = steps.indexOf(clipRangeDays);
    return idx > 0 ? steps[idx - 1] : 0;
  }, [clipRangeDays]);

  const selectedChannel = useMemo(
    () => savedChannels.find((c) => c.id === selectedChannelId) ?? null,
    [savedChannels, selectedChannelId],
  );

  // Effective per-channel platform flags: a channel with exactly one platform
  // keeps it ON regardless of the global toggle, so its content can never be
  // hidden (e.g. a Twitch-only channel with the global Twitch filter off).
  // The persisted global flags are untouched — this only drives display/fetch.
  const effectiveFlags = effectivePlatformFlags(selectedChannel, {
    kick: kickEnabled,
    twitch: twitchEnabled,
    youtube: youtubeEnabled,
  });
  const effectiveKickEnabled = effectiveFlags.kick;
  const effectiveTwitchEnabled = effectiveFlags.twitch;
  const effectiveYoutubeEnabled = effectiveFlags.youtube;
  const youtubePlatformOnly = effectiveYoutubeEnabled && !effectiveKickEnabled && !effectiveTwitchEnabled;

  const selectedChannelFirstLiveEntry = useMemo(() => {
    if (!selectedChannelId) return null;
    const liveStatus = channelLiveStatuses[selectedChannelId];
    if (!liveStatus) return null;
    const live = liveStatus.live.filter((e) => e.is_live === true);
    return live[0] ?? null;
  }, [channelLiveStatuses, selectedChannelId]);

  /** Full live-entry list for the selected channel (fallback chain source). */
  const selectedChannelLiveEntries = useMemo(() => {
    if (!selectedChannelId) return [];
    const liveStatus = channelLiveStatuses[selectedChannelId];
    if (!liveStatus) return [];
    return liveStatus.live.filter((e) => e.is_live === true && e.url);
  }, [channelLiveStatuses, selectedChannelId]);

  const allChannelVideos = useMemo(() => {
    if (!selectedChannel) return [];
    // Members-only rows can linger in cached lists from before the backend
    // filtered them; they can never be previewed or downloaded, so drop them
    // at the source for every tab.
    const visible = (list: ChannelVideo[] | undefined) =>
      (list ?? []).filter((v) => isPublicVideo(v));
    if (channelContentFilter === 'clips') return visible(selectedChannel.clipVideos);
    if (channelContentFilter === 'streams') {
      return visible(selectedChannel.vodVideos).filter((v) => v.content_kind === 'stream');
    }
    // Multi-platform UI: recorded YouTube broadcasts (kind 'stream') belong
    // in the channel's VOD list — the /streams tab content is now merged
    // into the vods fetch. YouTube-only mode keeps them out of "Videos"
    // because its dedicated "VODs" tab shows them.
    return visible(selectedChannel.vodVideos).filter((v) =>
      youtubePlatformOnly ? v.content_kind !== 'stream' && v.content_kind !== 'clip' : v.content_kind !== 'clip',
    );
  }, [selectedChannel, channelContentFilter, youtubePlatformOnly]);

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
    // Era-window guard: clipVideos is MERGED across range changes, so clips
    // from a previously-selected era linger in state. Re-apply the current
    // window client-side so the list only ever shows the selected era.
    const clipWindow = clips && clipRangeDays > 0
      ? { minMs: clipRangeMinDays * 86_400_000, maxMs: clipRangeDays * 86_400_000 }
      : null;
    const items: ChannelVideo[] = [];
    if (effectiveKickEnabled && channelHasKick) {
      items.push(...channelPlatformVisibleSlice(
        kickChannelVideos,
        kickVisibleLimit,
        channelBeyondRecent.Kick ?? false,
        clips,
      ));
    }
    if (effectiveTwitchEnabled && channelHasTwitch) {
      items.push(...channelPlatformVisibleSlice(
        twitchChannelVideos,
        twitchVisibleLimit,
        channelBeyondRecent.Twitch ?? false,
        clips,
      ));
    }
    if (effectiveYoutubeEnabled && channelHasYoutube) {
      items.push(...channelPlatformVisibleSlice(
        youtubeChannelVideos,
        youtubeVisibleLimit,
        channelBeyondRecent.YouTube ?? false,
        clips,
      ));
    }
    const windowed = clipWindow
      ? items.filter((v) => {
          const ts = parseVideoTs(v.created_at);
          if (!ts) return true; // keep undated — hiding makes sparse platforms look empty
          const age = Date.now() - ts;
          return age >= clipWindow.minMs && age <= clipWindow.maxMs;
        })
      : items;
    const sorted = [...windowed].sort((a, b) => {
      // Clips "Most Views": keep the server's views ordering — re-sorting by
      // date here silently undoes the sort=views fetch.
      if (clips && clipSort === 'views') return (b.views ?? 0) - (a.views ?? 0);
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
    effectiveKickEnabled,
    effectiveTwitchEnabled,
    effectiveYoutubeEnabled,
    kickVisibleLimit,
    twitchVisibleLimit,
    youtubeVisibleLimit,
    channelContentFilter,
    clipRangeDays,
    clipRangeMinDays,
    clipSort,
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
  const canExpandKick = effectiveKickEnabled && channelHasKick && channelPlatformCanExpand(
    kickChannelVideos, kickVisibleLimit, channelBeyondRecent.Kick ?? false, clipsMode,
  );
  const canExpandTwitch = effectiveTwitchEnabled && channelHasTwitch && channelPlatformCanExpand(
    twitchChannelVideos, twitchVisibleLimit, channelBeyondRecent.Twitch ?? false, clipsMode,
  );
  const canExpandYoutube = effectiveYoutubeEnabled && channelHasYoutube && channelPlatformCanExpand(
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

  // Settings — initialized with defaults so the Settings tab renders immediately;
  // the mount loadSettings() below replaces them with the server values.
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);
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
    previewRetryingRef.current = false;
    setPreviewRetryBoth(null);
    const sid = previewSessionId;
    destroyPreviewPlayer();
    setPreviewOpen(false);
    setPreviewSessionId(null);
    setIsLive(false);
    previewIsLiveRef.current = false;
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
    const target = Math.max(start, Math.min(sec, end));
    if (previewYoutubeEmbedUrl) {
      // ponytail: if the iframe API hasn'target bound yet, the seekTo command is
      // dropped silently and the user thinks the seek is broken. Queue the
      // target — the effect below replays it the moment previewVideoReady
      // flips, and the slider stays in sync (optimistic UI was already set).
      if (!previewVideoReady) {
        previewPendingSeekSecRef.current = target;
        return;
      }
      // Keep this target until YouTube reports the new position. The iframe
      // emits its old time briefly after seekTo; accepting it makes the
      // controlled scrubber jump backwards.
      previewSeekTargetRef.current = target;
      previewTimingRef.current?.markSeekStart(target);
      syncPreviewTimeUi(target, true);
      postYoutubePreviewCommand('seekTo', [target, true]);
      return;
    }
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    previewSeekTargetRef.current = target;
    previewTimingRef.current?.markSeekStart(target);
    const pageUrl = previewLoadedUrlRef.current ?? url.trim();
    const youtube = detectUrlPlatform(pageUrl) === 'youtube';
    const optimistic = previewSeekOptimisticUi(
      youtube,
      previewTrimTimelineRef.current,
      previewPlaybackKindRef.current,
    );
    const finishSeek = () => {
      previewSeekTargetRef.current = null;
      syncPreviewTimeUi(target, true);
    };
    const applyLocalTime = (videoTime: number) => {
      if (force || Math.abs(video.currentTime - videoTime) > 0.05) {
        video.currentTime = videoTime;
      }
      if (optimistic) syncPreviewTimeUi(target, true);
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
      if (isPositionInWindowHlsMux(target, muxStart, muxEnd)) {
        previewSeekLockedRef.current = true;
        // The slider already jumped optimistically in seekPreviewVideo.
        // applyVideoLocalSeek pauses during the seek so the decoder does not
        // play forward from the previous keyframe to the target.
        void applyVideoLocalSeek(video, windowHlsVideoTimeSec(target, muxStart))
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
        void msePlayerRef.current.seek(target).then(() => {
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
          const { muxStart: newStart, muxEnd: newEnd, remuxed } = await seekYoutubeWindowHls(sid, target, apiPost, apiGet, 12_000);
          if (seekId !== previewSeekInflightRef.current) return;
          previewWindowHlsMuxStartRef.current = newStart;
          previewWindowHlsMuxEndRef.current = newEnd;
          const videoTime = windowHlsVideoTimeSec(target, newStart);
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
            setError(err instanceof Error ? err.message : t('Seek failed'));
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
      && target > start + 60
    ) {
      const clipRel = previewClipRelativeRef.current;
      const videoTime = clipRel ? Math.max(0, Math.min(target - start, end - start)) : target;
      // Show a teaser frame at the target immediately while /refresh resolves
      // the full-window progressive URL in the background.
      applyLocalTime(videoTime);
      previewPendingSeekSecRef.current = target;
      setPreviewBuffering(true);
      void apiPost<PreviewSessionResponse>(`/api/preview/session/${sid}/refresh`, {})
        .then((res) => {
          // The element already played ~0.5s past target while the refresh was in
          // flight. Resume the handoff from the LIVE position — re-seeking to
          // the original target would visibly replay the same half second.
          const clipRelNow = previewClipRelativeRef.current;
          previewPendingSeekSecRef.current = clipRelNow ? start + video.currentTime : video.currentTime;
          if (applyPreviewSessionRefresh(res)) {
            finishSeek();
            setPreviewBuffering(false);
            return;
          }
          // No handoff: same progressive stream, element is already at/past
          // the target — do NOT re-seek (that was the seek-repeat glitch).
          waitVideoPlayable(
            video,
            previewTimingRef.current ?? new PreviewTiming(youtube ? 'youtube' : 'unknown', 'main'),
          );
          finishSeek();
        })
        .catch(() => {
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
    const videoTime = clipRel ? Math.max(0, Math.min(target - start, end - start)) : target;
    applyLocalTime(videoTime);
    const plat = detectUrlPlatform(pageUrl) ?? 'unknown';
    waitVideoPlayable(
      video,
      previewTimingRef.current ?? new PreviewTiming(plat, 'main'),
    );
    finishSeek();
  }, [previewYoutubeEmbedUrl, previewVideoReady, syncPreviewTimeUi, url, applyPreviewSessionRefresh, postYoutubePreviewCommand]);

  // Replay a queued iframe seek when the YouTube embed API finally binds.
  useEffect(() => {
    if (!previewVideoReady || !previewYoutubeEmbedUrl) return;
    const pending = previewPendingSeekSecRef.current;
    if (pending == null) return;
    previewPendingSeekSecRef.current = null;
    previewSeekTargetRef.current = pending;
    previewTimingRef.current?.markSeekStart(pending);
    syncPreviewTimeUi(pending, true);
    postYoutubePreviewCommand('seekTo', [pending, true]);
  }, [previewVideoReady, previewYoutubeEmbedUrl, syncPreviewTimeUi, postYoutubePreviewCommand]);

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

  const openPreview = useCallback(async (): Promise<boolean> => {
    if (!url.trim()) return false;
    // Unknown duration (in-progress VOD): both ends are 0 — open anyway so the
    // growing archive can play; only bail on a known inverted range.
    if ((trimEndSec !== 0 || trimStartSec !== 0) && trimEndSec <= trimStartSec) return false;
    const trimmedUrl = url.trim();
    // Already showing this URL — no-op unless playback failed and user is retrying
    if (
      previewStartedRef.current
      && previewLoadedUrlRef.current === trimmedUrl
      && previewOpenRef.current
      && (previewVideoReadyRef.current || previewVideoLoadingRef.current)
    ) return true;
    // A manual open starts a fresh retry budget; a RETRY-triggered open keeps it
    // so a repeated failure escalates to the full pipeline.
    if (!previewRetryingRef.current) {
      setPreviewRetryBoth(null);
    }
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
    // Preferred width is unchanged; the render-time effective layout honors the aspect.
    const _clampedPreviewW = clampPreviewPanelWidth(
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
    void _clampedPreviewW;
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
      const res = prefetched ?? await createPreviewSessionWithRetry({
        url: trimmedUrl,
        crop_start: start,
        crop_end: end,
        prefer_height: previewPreferHeight,
      });
      if (bailIfSuperseded()) return false;
      timing.setSessionId(res.session_id);
      timing.mark('session_ready', `kind=${res.kind} trim=${res.trim_timeline === true}`);
      previewExtractSourceRef.current = res.extract_source ?? '';
      if (previewExtractSourceRef.current) {
        console.info('[VOD.RIP preview] extract_source=', previewExtractSourceRef.current);
      }
      const clipInfo = clipPreview && !qualityLabels?.length
        ? await apiGet<VideoInfo>(`/api/info/clip?id=${encodeURIComponent(trimmedUrl)}`).catch(() => null)
        : null;
      if (bailIfSuperseded()) return false;
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
        anonymous: res.anonymous === true,
        isLive: res.is_live === true,
        channelLanguage: res.channel_language ?? '',
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
      // In-progress VOD (growing archive) is a live HLS stream — enable the live
      // knobs (liveSyncDuration/liveDurationInfinity) or playback stalls at the
      // playlist edge because duration keeps growing.
      if (res.growing_vod || res.is_live) previewIsLiveRef.current = true;
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
      return true;
    } catch (err: any) {
      previewStartedRef.current = false;
      previewLoadedUrlRef.current = null;
      setError(err.message || t('Preview failed'));
      markPreviewError(trimmedUrl, 'session');
      setPreviewOpen(false);
      setPreviewVideoLoading(false);
      return false;
    }
  }, [url, trimEndSec, trimStartSec, vodDurationSec, previewSessionId, destroyPreviewPlayer, videoInfo, videoInfo?.qualities, videoInfo?.title]);

  // List-VOD click → main preview. selectVod records the clicked URL; once the
  // info fetch for it lands (and the video gate passes — the same conditions
  // that enable the WATCH PREVIEW button), open the preview. The pending flag
  // drops only when the user moves on to a different URL — NOT when the info
  // for the clicked URL is missing: a superseded fetch's finally{} clears
  // `loading` before the newest fetch lands, so dropping on videoInfoUrl
  // mismatch there would strand the newest click.
  useEffect(() => {
    const pendingUrl = autoOpenPreviewPendingRef.current;
    if (!pendingUrl) return;
    if (url.trim() !== pendingUrl) {
      autoOpenPreviewPendingRef.current = null;
      return;
    }
    if (loading) return; // info fetch still in flight
    if (videoInfoUrl !== pendingUrl) return; // fetch failed or still running — wait
    autoOpenPreviewPendingRef.current = null;
    if (vodDurationSec <= 0 || trimEndSec <= trimStartSec) return;
    void openPreview();
  }, [loading, videoInfoUrl, url, vodDurationSec, trimEndSec, trimStartSec, openPreview, autoOpenPreviewTick]);

  /**
   * RETRY button for the failing media only. First click re-runs just the
   * failed stage; after a failed retry the next click runs the full pipeline
   * end-to-end for that media (drop stale session + force fresh backend
   * extract — never touches other media/channels).
   */
  const retryPreview = useCallback(async () => {
    const ctx = previewRetryRef.current;
    if (!ctx) return;
    setError(null);
    previewRetryingRef.current = true;
    try {
      if (previewRetryMode(ctx) === 'full') {
        // Escalation: end-to-end for THIS media only. Delete any stale session
        // and clear the backend's per-URL caches (fatal/negative extract caches
        // live 30-300s) so the fresh POST re-extracts instead of re-raising.
        const sid = previewSessionIdRef.current;
        if (sid) {
          try { await apiDelete(`/api/preview/session/${sid}`); } catch { /* ignore */ }
        }
        try { await apiPost('/api/preview/invalidate', { url: ctx.url }); } catch { /* ignore */ }
      }
      if (ctx.stage === 'playback' && previewRetryMode(ctx) === 'stage') {
        // Stage retry: re-attach playback to the SAME session. The attach
        // effect reports success (canplay clears the context) or failure
        // (markPreviewError escalates attempts). No new session, no refresh.
        setPreviewRetryTick((t) => t + 1);
        return;
      }
      // Session-stage retry and full retries both re-run create + attach.
      const ok = await openPreview();
      if (ok) {
        previewRetryingRef.current = false;
        setPreviewRetryBoth(null);
      }
    } catch (err: unknown) {
      previewRetryingRef.current = false;
      setError(err instanceof Error ? err.message : t('Preview failed'));
    }
  }, [openPreview, setPreviewRetryBoth]);

  // ─── YouTube preview warm — per-channel VOD limit ────────────────
  // mergeVodLists sorts vodVideos by created_at desc (newest first; see channelUtils.ts:157-160),
  // so slice(0, limit) reliably selects the most recent entries.
  const YOUTUBE_WARM_VOD_LIMIT = 5;

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
      .filter((v) => v.platform === 'youtube' && isPublicVideo(v)
        && v.content_kind !== 'clip' && !(v.url || '').includes('/shorts/')
        && channelContentFilter !== 'clips')
      .filter((v) => !warmedUrlsRef.current.has(buildVodUrl(v)))
      .slice(0, YOUTUBE_WARM_VOD_LIMIT)
      .map((v) => buildVodUrl(v));
    warmYoutubePreviewBatch(youtubeUrls);

    let cleanup: (() => void) | undefined;
    const raf = requestAnimationFrame(() => {
      const rows = Array.from(root.querySelectorAll<HTMLElement>('[data-youtube-warm]'));
      cleanup = bindYoutubeChannelScrollWarm(root, rows);
    });
    return () => {
      cancelAnimationFrame(raf);
      cleanup?.();
    };
  }, [tab, selectedChannelId, youtubeEnabled, visibleChannelVideos, url, channelContentFilter]);

  // Poll live status for every saved channel while the Channels tab is open.
  // The first poll fires immediately (no setTimeout) so the LIVE badge paints
  // on the same render the Channels tab opens. Server-side startup warm
  // (routers.live.warm_all_saved_channel_live_status) ensures the cache
  // hits in O(1) — first poll typically completes in <50ms.
  useEffect(() => {
    // ponytail: hold the live-status polls while the preview player is open —
    // a full channel grid's poll wave (14+ parallel requests) saturates the
    // browser's per-host connection pool and starves the preview's manifest
    // fetch past hls.js's manifestLoadingTimeOut (10s), killing playback.
    if (tab !== 'channels' || !savedChannels.length || previewOpen) return;
    let cancelled = false;
    let timeout: number | null = null;
    const FAST_POLL_MS = 3_000;
    const SLOW_POLL_MS = 30_000;
    const FAST_POLLS = 6;
    // Backend cache TTL (routers/live.py _LIVE_STATUS_TTL_SEC = 60s): a poll
    // for a channel we fetched less than this ago is a guaranteed cache hit,
    // so skip the round trip entirely — badge freshness is bounded by the
    // backend TTL anyway, and the backend refresh only kicks on request.
    const BACKEND_TTL_MS = 60_000;
    let pollCount = 0;
    const fetchedAt: Record<string, number> = {};
    const inFlight = new Set<string>();
    // Channels that 404 (removed from backend settings, e.g. stale ids left
    // in localStorage) are dropped from the poll list so they stop 404ing
    // every cycle. Reset on effect re-run (tab switch / channel-list change),
    // so a recovered channel gets one retry per visit.
    // ponytail: no periodic revalidation; re-adding the channel mints a new
    // id anyway, and backend settings hydrate replaces the list wholesale.
    const droppedIds = new Set<string>();
    // Preload the hls.js chunk while the Channels tab renders — the popup's
    // dynamic import then resolves from the module cache instead of pulling
    // the ~900KB chunk on the live-click critical path.
    void import('hls.js').catch(() => {});
    const fetchOne = async (ch: SavedChannel) => {
      if (inFlight.has(ch.id)) return;
      inFlight.add(ch.id);
      try {
        const status = await apiGet<ChannelLiveStatus>(`/api/channels/${ch.id}/live`);
        if (!cancelled) {
          fetchedAt[ch.id] = Date.now();
          setChannelLiveStatuses((prev) => {
            const next = { ...prev, [ch.id]: status };
            // ponytail: write last-known live status to localStorage so the next
            // app open paints the LIVE badge immediately (before the first poll
            // round-trip). Server-side warm already keeps the API hot, but
            // local cache means we never paint an empty list.
            persistChannelLiveStatuses(
              Object.fromEntries(
                Object.entries(next).map(([cid, s]) => [cid, { ...s, fetched_at: Date.now() }]),
              ) as unknown as Record<string, StoredChannelLiveStatus>,
            );
            return next;
          });
        }
      } catch (err) {
        // The live endpoint 404s with detail "Channel not found" when the
        // channel no longer exists in backend settings — drop it from the
        // poll list so a stale id stops polluting every tick.
        if (err instanceof Error && /not found/i.test(err.message)) {
          droppedIds.add(ch.id);
        }
      } finally {
        inFlight.delete(ch.id);
      }
    };
    const tick = async () => {
      // Schedule the next tick BEFORE awaiting the batch — one slow/cold
      // channel must not stretch every badge's refresh cadence.
      pollCount++;
      const ms = pollCount <= FAST_POLLS ? FAST_POLL_MS : SLOW_POLL_MS;
      timeout = window.setTimeout(tick, ms);
      const now = Date.now();
      const due = savedChannelsRef.current.filter(
        (ch) =>
          !droppedIds.has(ch.id) &&
          (fetchedAt[ch.id] ?? 0) + BACKEND_TTL_MS <= now,
      );
      await Promise.all(due.map(fetchOne));
    };
    tick();
    return () => {
      cancelled = true;
      if (timeout) window.clearTimeout(timeout);
    };
  }, [tab, savedChannels.length, previewOpen]);

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
      // Playback genuinely started — any in-flight retry succeeded.
      previewRetryingRef.current = false;
      setPreviewRetryBoth(null);
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
        allowHeights: detectUrlPlatform(previewPageUrl) === 'youtube'
          ? youtubePreviewAllowHeights({
            isLive: meta?.isLive ?? previewIsLiveRef.current,
            anonymous: meta?.anonymous ?? false,
          })
          : undefined,
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
          setError(t('Preview interrupted — try again'));
          setPreviewVideoLoading(false);
          markPreviewError(previewPageUrl, 'playback');
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
        markPreviewError(previewPageUrl, 'session');
        return;
      }
      const mse = msePlayerRef.current;
      if (!mse) {
        setError("MSE player unavailable");
        setPreviewVideoLoading(false);
        markPreviewError(previewPageUrl, 'playback');
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
      let staleSessionFired = false;
      const markSessionGone = (reason: string) => {
        if (staleSessionFired) return;
        staleSessionFired = true;
        const msg = t('Preview expired — refresh and try again');
        console.warn('[VOD.RIP preview] session gone:', reason);
        setError(msg);
        setPreviewVideoLoading(false);
        previewStartedRef.current = false;
        previewLoadedUrlRef.current = null;
        markPreviewError(previewPageUrl, 'session');
        try { hls.stopLoad(); hls.destroy(); } catch { /* ignore */ }
        previewHlsRef.current = null;
        setHlsRef(null);
      };
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
        ...twitchAdBlockHlsConfig({
          // vaft midroll rotation: on repeated ad-tainted refreshes the
          // backend swaps the live session's usher master in place (404 for
          // non-Twitch / non-usher sessions — the pLoader keeps stripping).
          onAdRotation: createTwitchAdRotationHandler({
            getSessionId: () => previewSessionIdRef.current,
            getHls: () => previewHlsRef.current,
            getVideo: () => previewVideoRef.current,
            requestRotation: (sid) =>
              apiPost<{ ok?: boolean; master_url?: string }>(`/api/preview/live/rotate/${sid}`, {}),
          }),
        }),
        ...(previewIsLiveRef.current ? {
          liveSyncDuration: 3,
          liveMaxLatencyDuration: 10,
          liveDurationInfinity: true,
          maxLiveSyncPlaybackRate: 1.5,
        } : {}),
        startPosition: previewIsLiveRef.current
          ? -1
          : (previewPendingSeekSecRef.current ?? previewTrimStartRef.current),
        // ponytail: peel stale-session 404s out of the HLS retry loop. Without
        // this, a player that comes back after a backend restart retries the
        // same dead manifest URLs forever and the user sees only INFO 404 lines.
        xhrSetup: (xhr) => {
          xhr.addEventListener('readystatechange', () => {
            if (xhr.readyState === 4 && (xhr.status === 404 || xhr.status === 410)) {
              try {
                const body = xhr.responseText || '';
                if (body.includes('not found or expired')
                  || body.includes('Unknown preview resource')) {
                  markSessionGone(`xhr ${xhr.status}`);
                }
              } catch { /* ignore */ }
            }
          });
        },
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
      // Quality policy: YouTube VOD/anonymous previews cap the menu at 360p;
      // live sessions with user cookies may raise to 1080p.
      const allowHeights = youtubePreview
        ? youtubePreviewAllowHeights({
          isLive: meta?.isLive ?? previewIsLiveRef.current,
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
                if (!cancelled) {
                  // startLoad() alone is a no-op after a fatal manifest error
                  // (levels empty → StreamController returns immediately);
                  // re-loadSource forces a fresh manifest fetch.
                  hls.loadSource(playbackUrl);
                  hls.startLoad();
                }
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
                  setError(t('Preview playback failed — try again'));
                  setPreviewVideoLoading(false);
                  previewStartedRef.current = false;
                  markPreviewError(previewPageUrl, 'playback');
                  hls.destroy();
                  previewHlsRef.current = null;
                });
              break;
            }
            setError(t('Preview playback failed — try again'));
            setPreviewVideoLoading(false);
            previewStartedRef.current = false;
            markPreviewError(previewPageUrl, 'playback');
            hls.destroy();
            previewHlsRef.current = null;
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError();
            break;
          default:
            setError(t('Preview playback failed — try again'));
            setPreviewVideoLoading(false);
            previewStartedRef.current = false;
            markPreviewError(previewPageUrl, 'playback');
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
      return; // hls.js owns this session — do not fall through to native/unsupported

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

    setError(t('HLS playback is not supported in this browser'));
    setPreviewVideoLoading(false);
    markPreviewError(previewPageUrl, 'playback');
    };

    setup();
    return () => {
      cancelled = true;
      previewBufferingClearRef.current = null;
      detachBuffering?.();
      cleanup?.();
    };
  }, [previewOpen, previewPlayback, previewSessionId, previewRetryTick]);

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

  // Chat/transcript/subtitle timestamp clicks ALWAYS seek the archive-absolute
  // time — even with a trim active. seekPreviewVideo clamps to the trim window
  // and the timeupdate clamp drags the playhead back to the boundary, so widen
  // the trim to include the clicked offset first (resolveTimestampSeek).
  const handlePreviewChatSeek = useCallback((offsetSec: number) => {
    const { target, start, end } = resolveTimestampSeek(
      offsetSec,
      previewTrimStartRef.current,
      previewTrimEndRef.current,
      vodDurationSec,
    );
    if (start !== previewTrimStartRef.current || end !== previewTrimEndRef.current) {
      commitPreviewTrimRange(start, end);
    }
    seekPreviewVideo(target);
  }, [vodDurationSec, commitPreviewTrimRange, seekPreviewVideo]);

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
    // Pin the zoomed window at drag start so the mapping cannot change mid-drag.
    const dragView = zoomWindowFromView(previewTrimZoom, previewTrimAnchorFrac, vodDurationSec);

    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';

    const xToSec = (clientX: number) => {
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return 0;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return Math.round(fracToSec(frac, dragView));
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
      // seek: the needle is the playhead while dragging — scrub in BOTH
      // directions (clamp-only used to move the video only when the playhead
      // fell outside the new range, i.e. one direction).
      const applied = which === 'in'
        ? commitPreviewTrimRange(sec, fixedEnd, { move: 'in', fixedEnd, seek: 'in' })
        : commitPreviewTrimRange(fixedStart, sec, { move: 'out', fixedStart, seek: 'out' });
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
  }, [vodDurationSec, previewTrimZoom, previewTrimAnchorFrac, commitPreviewTrimRange, updateNeedleGlance, markPreviewTrimEndpoint]);

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


  const togglePreviewSearch = useCallback(() => {
    const next = !previewSearchOpen;
    if (next) {
      const rect = previewPanelRef.current?.getBoundingClientRect();
      if (rect) {
        // Anchor just right of the preview panel, top-aligned, clamped to the viewport.
        const popupW = 460;
        previewSearchAnchorRef.current = {
          x: Math.max(8, Math.min(rect.right + 8, window.innerWidth - popupW - 8)),
          y: Math.max(8, rect.top),
        };
      } else {
        previewSearchAnchorRef.current = null;
      }
    }
    setPreviewSearchOpen(next);
  }, [previewSearchOpen]);

  const focusPreviewPlayer = useCallback(() => {
    previewContainerRef.current?.focus();
  }, []);

  const previewFsGateRef = useRef<FullscreenGate | null>(null);
  if (previewFsGateRef.current === null) {
    previewFsGateRef.current = createFullscreenGate();
  }
  /** Whether the last fullscreenchange event had the preview as the active element. */
  const previewFsActiveRef = useRef(false);

  const togglePreviewFullscreen = useCallback(() => {
    const container = previewContainerRef.current;
    if (!container || !previewVideoReady) return;
    // The gate ignores calls while a transition is in flight and decides
    // direction from the CURRENT fullscreen element — no sync element check.
    if (previewFsGateRef.current?.toggle(container) === 'enter') {
      setTrimPanelHeight(0);
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
      previewFsGateRef.current?.sync();
      // Always derive state from the browser. The old YouTube-only "fake"
      // fullscreen left the controls locked after Escape.
      const fs = document.fullscreenElement === previewContainerRef.current;
      const wasFs = previewFsActiveRef.current;
      previewFsActiveRef.current = fs;
      setPreviewFullscreen(fs);
      setPreviewFsControlsVisible(!fs);
      // Leaving fullscreen must not keep the in-fullscreen resize — reset to
      // the default panel height (entering may keep the user's resize). The
      // wasFs guard keeps other surfaces' fullscreenchange events out of it.
      if (wasFs && !fs) setTrimPanelHeight(0);
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
    previewVideoAspect,
    previewPlayback?.kind,
    syncPreviewPlaybackToViewport,
  ]);

  // Height explosion guard: PreviewChatPanel's self-stretching column grows
  // to the unbounded virtualized list (topPad/bottomPad spacers ~112k px for
  // a long VOD), and in an auto-height flex row the chat's content height
  // wins over the player column's explicit height — the whole preview card
  // balloons and the chat fills the screen. Pin the row to the player
  // column's content height (same fix as ChannelExplorePopup, 3a2b9d4); the
  // chat's internal `flex-1 min-h-0 overflow-y-auto` then scrolls inside it.
  useLayoutEffect(() => {
    const row = previewRowRef.current;
    const col = previewContainerRef.current;
    if (!row || !col) return;
    if (previewFullscreen) {
      row.style.height = '';
      return;
    }
    const h = col.offsetHeight;
    if (h > 0) {
      previewColHRef.current = h;
      row.style.height = `${h}px`;
    }
  }, [previewFullscreen, previewOpen]);

  // Keep the pin exact as the player column's content height changes (video
  // aspect resolves, panel resize, viewport clamp). Re-attaches when the
  // preview row mounts (App stays mounted; the row is conditional). Skipped
  // while fullscreen so exiting fullscreen never flashes a viewport-tall row.
  useEffect(() => {
    const col = previewContainerRef.current;
    if (!col || !previewOpen) return;
    const ro = new ResizeObserver(() => {
      if (previewFsActiveRef.current) return;
      const row = previewRowRef.current;
      const h = col.offsetHeight;
      if (row && h > 0 && h !== previewColHRef.current) {
        previewColHRef.current = h;
        row.style.height = `${h}px`;
      }
    });
    ro.observe(col);
    return () => ro.disconnect();
  }, [previewOpen]);

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
    // Synthetic watchdog ids have no real video — nothing to preview.
    // (platform arrives capitalized as 'YouTube'; compare case-insensitively.)
    if ((v.platform || '').toLowerCase() === 'youtube' && isSyntheticArchiveId(v.id)) return;
    notePreviewGesture();
    pauseAllExplorePopups();
    const vodUrl = buildVodUrl(v);
    if (v.platform === 'youtube') {
      warmYoutubePreview(vodUrl);
      warmYoutubePreviewFull(vodUrl, 500);
    }
    const isClipItem = v.content_kind === 'clip' || channelContentFilter === 'clips' || isLikelyClip(v);
    const vod: ExplorePopupVod = {
      url: buildVodUrl(v),
      title: displayTitle(v),
      platform: v.platform,
      durationSec: channelVideoDurationSec(v) ?? 0,
      platformListIndex: v.platformListIndex,
      isClip: isClipItem,
      thumbnailUrl: resolveVideoThumbnail(v.thumbnail_url ?? null, 640, 360),
      created_at: v.created_at ?? null,
      views: v.views ?? null,
      duration_string: v.duration_string ?? null,
      channel_language: v.channel_language ?? null,
      channel: v.channel,
      // Native id, same format as the archive DB videos.video_id (Twitch ids
      // come 'v'-prefixed from the API; the archive stores the bare digits).
      videoId: v.platform === 'Twitch' && v.id.startsWith('v') ? v.id.slice(1) : v.id,
    };
    setExplorePopups((prev) => {
      // Dedupe: bring existing popup to front instead of opening a duplicate
      const existing = prev.find((p) => p.vod.url === vod.url);
      if (existing) {
        bringExplorePopupToFront(existing.id);
        return prev;
      }
      const id = crypto.randomUUID();
      assignExplorePopupZ(id);
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
  }, [pauseAllExplorePopups, assignExplorePopupZ, bringExplorePopupToFront, channelContentFilter]);

  /**
   * Archive search → open the hit in the explore-player flow at its offset.
   * A re-click on the same video drops the old popup and opens a fresh
   * session so the new initialTimeSec actually lands (dedupe would only
   * bring the stale one to front).
   */
  const openArchiveHit = useCallback((hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => {
    notePreviewGesture();
    pauseAllExplorePopups();
    const vodUrl = buildArchiveVodUrl(hit.platform, hit.video_id, video?.channel);
    if (hit.platform === 'youtube') {
      warmYoutubePreview(vodUrl);
    }
    const vod: ExplorePopupVod = {
      url: vodUrl,
      title: displayTitle({ title: video?.title, originalTitle: video?.originalTitle }) || hit.video_id,
      platform: hit.platform,
      durationSec: video?.duration_sec ?? 0,
      platformListIndex: 0,
      isClip: false,
      initialTimeSec: hit.offset_sec,
      videoId: hit.video_id,
      channel: video?.channel ?? undefined,
    };
    setExplorePopups((prev) => {
      const next = prev.filter((p) => p.vod.url !== vodUrl);
      const id = crypto.randomUUID();
      assignExplorePopupZ(id);
      const after = [...next, { id, vod, layoutIndex: next.length }];
      if (after.length > MAX_EXPLORE_POPUPS) {
        const dropped = after.slice(0, after.length - MAX_EXPLORE_POPUPS);
        dropped.forEach((entry) => {
          explorePauseMapRef.current.delete(entry.id);
          exploreVolumeMenusRef.current.delete(entry.id);
        });
        setExploreZOrder((zPrev) => {
          const zNext = { ...zPrev };
          for (const entry of dropped) delete zNext[entry.id];
          return zNext;
        });
        return after.slice(-MAX_EXPLORE_POPUPS);
      }
      return after;
    });
  }, [pauseAllExplorePopups, assignExplorePopupZ]);

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
  // Render-time effective layout: enforces viewport bounds on the preferred widths.
  // This is the SOLE source of truth for panel widths in JSX. State holds preferred;
  // the effective layout is re-derived each render from preferred + viewport.
  // innerWidth/innerHeight in the deps: the viewport is part of the derived
  // value (layoutRowWidthBudget etc. read it), so a window resize must
  // recompute — otherwise derived props (e.g. the chat panel's maxWidth) go
  // stale while the DOM width is kept live by applyLayoutPanelClamps.
  const effectiveLayout = useMemo<EffectivePanelLayout>(
    () =>
      effectiveLayoutFromPreferred(
        {
          previewPanelWidth,
          urlAside: urlAsidePanelSize,
          main: mainPanelSize,
        },
        {
          previewOpen,
          urlPanelAside: previewOpen || channelVodPanelOpen,
          chromeH: previewChromeHRef.current,
          aspect: previewVideoAspect,
        },
      ),
    [
      previewPanelWidth,
      urlAsidePanelSize,
      mainPanelSize,
      previewOpen,
      channelVodPanelOpen,
      previewVideoAspect,
      window.innerWidth,
      window.innerHeight,
    ],
  );
  const effectivePreviewPanelWidth = effectiveLayout.preview.w;


  const handlePreviewLoadedMetadata = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video?.videoWidth || !video?.videoHeight) return;
    const aspect = video.videoWidth / video.videoHeight;
    previewVideoAspectRef.current = aspect;
    setPreviewVideoAspect(aspect);
    // Aspect change re-derives the effective layout via the render-time useMemo;
    // we apply the DOM here so the user sees the (possibly clamped) width immediately.
    if (document.fullscreenElement !== previewContainerRef.current) {
      const effective = effectiveLayoutFromPreferred(
        {
          previewPanelWidth: previewPanelWidthRef.current,
          urlAside: urlAsidePanelSizeRef.current,
          main: mainPanelSizeRef.current,
        },
        {
          previewOpen: true,
          urlPanelAside: previewOpen || channelVodPanelOpen,
          chromeH: previewChromeHRef.current,
          aspect,
        },
      );
      const w = effective.preview.w;
      previewPanelWidthRef.current = w;
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, w);
    }
  }, [layoutBoundsInput, previewOpen, channelVodPanelOpen]);

  /**
   * Sync DOM panel sizes from the runtime-clamped effective layout.
   * NEVER writes to React state — preferred widths (state) must not be mutated by
   * info refetch, viewport resize, or any non-user action. Render-time clamping
   * produces effective widths; this function only paints the DOM.
   */
  const applyLayoutPanelClamps = useCallback(() => {
    const layout = layoutBoundsInput();
    const clamped = clampAllLayoutPanels(layout);
    if (layout.previewOpen) {
      const w = clampPreviewPanelWidth(
        clamped.preview.w,
        previewChromeHRef.current,
        previewVideoAspectRef.current,
        { ...layout, preview: clamped.preview, urlAside: clamped.urlAside, main: clamped.main },
      );
      previewPanelWidthRef.current = w;
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, w);
    }
    if (layout.urlPanelAside) {
      urlAsidePanelSizeRef.current = clamped.urlAside;
      if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, clamped.urlAside);
    }
    mainPanelSizeRef.current = clamped.main;
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, clamped.main);
  }, [layoutBoundsInput]);

  useEffect(() => {
    // Preview close: side panels fall back to their owned heights instead of
    // keeping the tall height the preview's row sync forced on them (the
    // one-way ratchet). Guarded on the open→closed transition so a deliberate
    // closed-preview S-edge drag is never clobbered by this effect.
    if (prevPreviewOpenRef.current && !previewOpen) {
      const maxH = layoutMaxPanelHeight();
      const targetUrlH = Math.min(maxH, Math.max(PANEL_MIN.h, ownedPanelHeightRef.current.urlAside));
      if (urlAsidePanelSizeRef.current.h !== targetUrlH) {
        const nextUrl = { ...urlAsidePanelSizeRef.current, h: targetUrlH };
        urlAsidePanelSizeRef.current = nextUrl;
        setUrlAsidePanelSize(nextUrl);
        if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, nextUrl);
      }
      const targetMainH = Math.min(maxH, Math.max(PANEL_MIN.h, ownedPanelHeightRef.current.main));
      if (mainPanelSizeRef.current.h !== targetMainH) {
        const nextMain = { ...mainPanelSizeRef.current, h: targetMainH };
        mainPanelSizeRef.current = nextMain;
        setMainPanelSize(nextMain);
        if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, nextMain);
      }
    }
    prevPreviewOpenRef.current = previewOpen;
    applyLayoutPanelClamps();
    const onResize = () => applyLayoutPanelClamps();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [applyLayoutPanelClamps, previewOpen, channelVodPanelOpen]);

  const layoutRowHasMultiplePanels = useCallback(() => {
    return layoutHasMultiplePanels(layoutBoundsInput());
  }, [layoutBoundsInput]);

  const syncRowHeightsToPreview = useCallback((commitState = true) => {
    const layout = layoutBoundsInput();
    if (!layout.previewOpen || !previewPanelRef.current) return;
    const maxH = layoutMaxPanelHeight();
    const previewH = Math.min(maxH, previewPanelRef.current.offsetHeight);
    if (previewH <= 0) return;
    // Two-way row sync: side panels follow the preview while it is tall, but
    // fall back to their OWNED heights when the preview shrinks — the old
    // max(current.h, previewH) ratchet never released a tall panel.
    const nextUrlH = rowPanelHeightFromPreview(
      ownedPanelHeightRef.current.urlAside,
      previewH,
      maxH,
      PANEL_MIN.h,
    );
    const nextMainH = rowPanelHeightFromPreview(
      ownedPanelHeightRef.current.main,
      previewH,
      maxH,
      PANEL_MIN.h,
    );
    if (nextUrlH === urlAsidePanelSizeRef.current.h && nextMainH === mainPanelSizeRef.current.h) return;
    urlAsidePanelSizeRef.current = { ...urlAsidePanelSizeRef.current, h: nextUrlH };
    mainPanelSizeRef.current = { ...mainPanelSizeRef.current, h: nextMainH };
    if (commitState) {
      setUrlAsidePanelSize((prev) => ({ ...prev, h: nextUrlH }));
      setMainPanelSize((prev) => ({ ...prev, h: nextMainH }));
    }
    if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, urlAsidePanelSizeRef.current);
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, mainPanelSizeRef.current);
  }, [layoutBoundsInput]);

  const applyLayoutRowSizes = useCallback((fitted: {
    preview: { w: number; h: number };
    urlAside: { w: number; h: number };
    main: { w: number; h: number };
  }, commitState = true) => {
    // Commit the FULL fitted row (dragged panel + siblings) to refs and DOM on
    // every move. State is what the render derives effective widths from;
    // committing the siblings too keeps that derivation identical to the
    // live drag — no proportional re-split, no release-time yank, no flicker.
    // WS-9: during a drag, `commitState` is false — refs + DOM update per
    // frame, React state commits once on pointerup. React's fiber holds the
    // pre-drag style props, so its virtual DOM never changes mid-drag and it
    // cannot clobber the direct DOM writes (verified: no style re-write on
    // unrelated re-renders). Deferring the setStates removes the full-App
    // re-render per frame that the coupled layout previously paid.
    const layout = layoutBoundsInput();
    previewPanelWidthRef.current = fitted.preview.w;
    urlAsidePanelSizeRef.current = fitted.urlAside;
    mainPanelSizeRef.current = fitted.main;
    if (layout.previewOpen) {
      if (commitState) setPreviewPanelWidth(fitted.preview.w);
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, fitted.preview.w);
    }
    if (layout.urlPanelAside) {
      if (commitState) setUrlAsidePanelSize(fitted.urlAside);
      if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, fitted.urlAside);
    }
    if (commitState) setMainPanelSize(fitted.main);
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, fitted.main);
    syncRowHeightsToPreview(commitState);
  }, [layoutBoundsInput, syncRowHeightsToPreview]);

  const onPreviewPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const chromeH = previewChromeHRef.current;
    const aspect = previewVideoAspectRef.current;
    const coupled = layoutRowHasMultiplePanels();
    // Snapshot the row at drag start; sibling restore targets come from the
    // user-owned widths (survive squeezes across drags, unlike a live snapshot).
    const dragLayout = layoutBoundsInput();
    const preferred = preferredDragRef.current;
    const dragStartW = previewPanelWidthRef.current;

    // The container height is frozen (previewPanelHeightRef) so a refetch's
    // aspect change can't collapse the panel; a user resize must re-derive it
    // from the new width or the panel stays locked on the vertical axis.
    const setPreviewHeightFromWidth = (w: number) => {
      const h = Math.round(w / Math.max(0.01, aspect));
      previewPanelHeightRef.current = h;
      const c = previewContainerRef.current;
      if (c) c.style.height = `${h}px`;
    };

    startPanelWidthResize(e, edge, previewPanelWidthRef, setPreviewPanelWidth, {
      panelEl: previewPanelRef.current,
      aspect,
      clampWidth: (w) => {
        if (coupled) {
          return resizeLayoutGivingWidthTo(dragLayout, 'preview', w, preferred).preview.w;
        }
        return clampPreviewPanelWidth(w, chromeH, aspect, dragLayout);
      },
      onResizeMove: (w) => {
        setPreviewHeightFromWidth(w);
        if (coupled) {
          // WS-9: refs + DOM per frame, React state commits once on pointerup
          // (see onResizeEnd) — removes the per-frame full-App re-render.
          applyLayoutRowSizes(resizeLayoutGivingWidthTo(dragLayout, 'preview', w, preferred), false);
        }
      },
      onResizeEnd: () => {
        preferredDragRef.current.preview = previewPanelWidthRef.current;
        // Commit the row state once, matching the live DOM exactly (no yank).
        if (coupled) {
          applyLayoutRowSizes(
            resizeLayoutGivingWidthTo(dragLayout, 'preview', previewPanelWidthRef.current, preferred),
            true,
          );
        }
        applyLayoutPanelClamps();
        // Only re-derive the height when the width actually moved — a plain
        // click on a handle must not collapse the freeze to w/aspect (e.g.
        // after a wider-aspect refetch).
        const w = previewPanelWidthRef.current;
        if (w !== dragStartW) {
          // Follow the (possibly clamped) final width; the freeze effect
          // re-measures offsetHeight on the next render and stays consistent.
          setPreviewHeightFromWidth(w);
        }
        // Persist right away (localStorage + keepalive POST): waiting for the
        // debounced effect would lose the fresh `owned` on a fast reload.
        flushPanelLayoutToBackend();
      },
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps, layoutRowHasMultiplePanels, applyLayoutRowSizes, flushPanelLayoutToBackend]);

  const onUrlAsidePanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const coupled = layoutRowHasMultiplePanels();
    const dragLayout = layoutBoundsInput();
    const preferred = preferredDragRef.current;

    startPanelResizeDrag(e, edge, urlAsidePanelSizeRef, setUrlAsidePanelSize, {
      panelEl: urlAsidePanelRef.current,
      maxW: layoutMaxPanelWidthAtSiblingMins('urlAside', dragLayout),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => {
        if (!coupled) return clampPanelSizeForLayout('urlAside', s, dragLayout);
        const fitted = resizeLayoutGivingWidthTo(dragLayout, 'urlAside', s.w, preferred);
        return { w: fitted.urlAside.w, h: s.h };
      },
      onResizeMove: coupled
        ? (next) => {
            const fitted = resizeLayoutGivingWidthTo(dragLayout, 'urlAside', next.w, preferred);
            // WS-9: refs + DOM per frame, React state commits once on pointerup.
            applyLayoutRowSizes({ ...fitted, urlAside: { ...fitted.urlAside, h: next.h } }, false);
          }
        : undefined,
      onResizeEnd: () => {
        // Ref holds the raw drag height here (pre row-sync), which is the
        // user's choice — the sync's fallback target for later preview shrinks.
        ownedPanelHeightRef.current.urlAside = urlAsidePanelSizeRef.current.h;
        preferredDragRef.current.urlAside = urlAsidePanelSizeRef.current.w;
        if (coupled) {
          // Commit the row state once; keep the live height the drag produced.
          const fitted = resizeLayoutGivingWidthTo(dragLayout, 'urlAside', urlAsidePanelSizeRef.current.w, preferred);
          applyLayoutRowSizes({ ...fitted, urlAside: { ...fitted.urlAside, h: urlAsidePanelSizeRef.current.h } }, true);
        }
        applyLayoutPanelClamps();
        flushPanelLayoutToBackend();
      },
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps, layoutRowHasMultiplePanels, applyLayoutRowSizes, flushPanelLayoutToBackend]);

  const onMainPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const coupled = layoutRowHasMultiplePanels();
    const dragLayout = layoutBoundsInput();
    const preferred = preferredDragRef.current;
    startPanelResizeDrag(e, edge, mainPanelSizeRef, setMainPanelSize, {
      panelEl: mainPanelRef.current,
      maxW: layoutMaxPanelWidthAtSiblingMins('main', dragLayout),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => {
        if (!coupled) return clampPanelSizeForLayout('main', s, dragLayout);
        const fitted = resizeLayoutGivingWidthTo(dragLayout, 'main', s.w, preferred);
        return { w: fitted.main.w, h: s.h };
      },
      onResizeMove: coupled
        ? (next) => {
            const fitted = resizeLayoutGivingWidthTo(dragLayout, 'main', next.w, preferred);
            // WS-9: refs + DOM per frame, React state commits once on pointerup.
            applyLayoutRowSizes({ ...fitted, main: { ...fitted.main, h: next.h } }, false);
          }
        : undefined,
      onResizeEnd: () => {
        // Ref holds the raw drag height here (pre row-sync), which is the
        // user's choice — the sync's fallback target for later preview shrinks.
        ownedPanelHeightRef.current.main = mainPanelSizeRef.current.h;
        preferredDragRef.current.main = mainPanelSizeRef.current.w;
        if (coupled) {
          // Commit the row state once; keep the live height the drag produced.
          const fitted = resizeLayoutGivingWidthTo(dragLayout, 'main', mainPanelSizeRef.current.w, preferred);
          applyLayoutRowSizes({ ...fitted, main: { ...fitted.main, h: mainPanelSizeRef.current.h } }, true);
        }
        applyLayoutPanelClamps();
        flushPanelLayoutToBackend();
      },
    });
  }, [layoutBoundsInput, applyLayoutPanelClamps, layoutRowHasMultiplePanels, applyLayoutRowSizes, flushPanelLayoutToBackend]);

  useEffect(() => {
    if (!previewOpen || previewFullscreen || !previewPanelRef.current || !previewContainerRef.current) return;
    const chromeH = previewPanelRef.current.offsetHeight - previewContainerRef.current.offsetHeight;
    if (chromeH > 0) {
      previewChromeHRef.current = chromeH;
    }
  }, [previewOpen, previewFullscreen, previewVideoAspect, previewVideoReady]);
  useEffect(() => {
    if (!previewOpen || previewFullscreen || !previewContainerRef.current) return;
    const h = previewContainerRef.current.offsetHeight;
    if (h > 0) previewPanelHeightRef.current = h;
  });

  // ── Fetch video info ──

  type FetchVideoInfoHint = {
    durationSec?: number;
    title?: string;
    thumbnailUrl?: string | null;
    createdAt?: string | null;
    views?: number | null;
    /** Native archive video id (same value as archive DB videos.video_id). */
    videoId?: string;
    /** Channel slug/login (broadcaster login for Twitch) — carried into the
     *  synthetic VideoInfo so the TWITCH CLIP button stays enabled before the
     *  (possibly skipped) /api/info/video round-trip. */
    channel?: string | null;
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
    // A new video starts unzoomed — the previous clip's zoom/anchor would
    // show a nonsensical window on a different duration.
    setPreviewTrimZoom(1);
    setPreviewTrimAnchorFrac(0.5);
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
      if (qualityUserTouchedUrlRef.current !== trimmed) {
        setQuality(bestAvailableQuality(cached));
      }
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
        id: hint.videoId ?? trimmed,
        title: hintTitle,
        duration: end,
        duration_string: end > 0 ? fmtDuration(end) : null,
        created_at: hint.createdAt ?? null,
        views: hint.views ?? null,
        uploader: hint.channel ?? null,
        channel: hint.channel ?? null,
        thumbnail: hint.thumbnailUrl || findCachedVideoThumbnail(trimmed, savedChannels),
        webpage_url: trimmed,
        extractor: platform,
        is_live: null,
        qualities: ['source'],
        platform: platform === 'youtube' ? 'YouTube' : platform === 'twitch' ? 'Twitch' : platform === 'kick' ? 'Kick' : null,
      };
      setUrl(trimmed);
      setVideoInfo(synthetic);
      if (qualityUserTouchedUrlRef.current !== trimmed) {
        setQuality(bestAvailableQuality(synthetic));
      }
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
        id: hint.videoId ?? trimmed,
        title: hintTitle,
        duration: end,
        duration_string: fmtDuration(end),
        uploader: hint.channel ?? null,
        channel: hint.channel ?? null,
        thumbnail: hint.thumbnailUrl || findCachedVideoThumbnail(trimmed, savedChannels),
        webpage_url: trimmed,
        extractor: platform,
        is_live: null,
        qualities: ['source'],
        platform: platform === 'youtube' ? 'YouTube' : platform === 'twitch' ? 'Twitch' : platform === 'kick' ? 'Kick' : null,
      };
      setUrl(trimmed);
      setVideoInfo(synthetic);
      if (qualityUserTouchedUrlRef.current !== trimmed) {
        setQuality(bestAvailableQuality(synthetic));
      }
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
          if (qualityUserTouchedUrlRef.current !== trimmed) {
            setQuality(bestAvailableQuality(info));
          }
          const end = Math.max(1, videoInfoDurationSec(info));
          if (end <= 0) {
            setError(t('Could not determine video length'));
            return;
          }
          applyVideoInfoTrim(trimmed, end);
          if (!previewOpen) {
            void resetPreview();
          }
          const isMediaUrl = isClipUrl(trimmed) || /\/videos\//i.test(trimmed) || /^\d+$/.test(trimmed)
            || detectUrlPlatform(trimmed) === 'youtube';
          if (isMediaUrl) {
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
      setError(err.message || t('Could not open folder picker'));
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
      const msg = err instanceof Error ? err.message : t('Could not open folder');
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
        const msg = err instanceof Error ? err.message : t('Failed to remove download');
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
      setError(err instanceof Error ? err.message : t('Download failed'));
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
        title: t('Download clip?'),
        message: t('Save this clip ({size}){note}. Edit the file name below if you want.', { size: human, note: rangeNote }),
        defaultFilename,
      };
    }
    return {
      title: t('Download trim?'),
      message: t('Download "{title}" from {start} to {end}?', { title, start: formatHmsFull(trimStart), end: formatHmsFull(trimEnd) }),
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
      setError(err.message || t('Failed to cancel download'));
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handlePause = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/pause`, {});
    } catch (err: any) {
      setError(err.message || t('Failed to pause download'));
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handleResume = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/resume`, {});
    } catch (err: any) {
      setError(err.message || t('Failed to resume download'));
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handleDeleteHistory = useCallback((id: string) => {
    if (!window.confirm(t('Remove this download from history? The file on disk will also be deleted.'))) return;
    requestDownloadRemoval(id);
  }, [requestDownloadRemoval]);

  const handleRemoveFromQueue = useCallback(async (id: string) => {
    if (!window.confirm(t('Remove this download from the queue? Any partial file on disk will also be deleted.'))) return;
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
    if (!window.confirm(t('Remove {count} download(s) from recent? Files on disk will also be deleted.', { count: selectedRecentIds.size }))) return;
    const ids = [...selectedRecentIds];
    setSelectedRecentIds(new Set());
    ids.forEach((id) => requestDownloadRemoval(id));
  }, [selectedRecentIds, requestDownloadRemoval]);

  const handleBulkDeleteQueue = useCallback(() => {
    if (selectedQueueIds.size === 0) return;
    if (!window.confirm(t('Remove {count} download(s) from the queue? Partial files on disk will also be deleted.', { count: selectedQueueIds.size }))) return;
    const ids = [...selectedQueueIds];
    setSelectedQueueIds(new Set());
    ids.forEach((id) => requestDownloadRemoval(id));
  }, [selectedQueueIds, requestDownloadRemoval]);

  const handleBulkDeleteHistory = useCallback(() => {
    if (selectedHistoryIds.size === 0) return;
    if (!window.confirm(t('Remove {count} download(s) from history? Files on disk will also be deleted.', { count: selectedHistoryIds.size }))) return;
    const ids = [...selectedHistoryIds];
    setSelectedHistoryIds(new Set());
    ids.forEach((id) => requestDownloadRemoval(id));
  }, [selectedHistoryIds, requestDownloadRemoval]);

  const handleBulkDownloadChannelVods = useCallback(async () => {
    if (selectedChannelVodUrls.size === 0) return;
    const count = selectedChannelVodUrls.size;
    if (!window.confirm(t('Download {count} selected item(s)?\n\nEach will download at source quality with no trim.', { count }))) return;
    if (!(await ensureDownloadFolder())) {
      setError(t('Choose a download folder to continue.'));
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
        setError(err instanceof Error ? err.message : t('Failed to start download'));
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
    refreshing?: boolean;
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
  // One scheduled silent follow-up per (channel, mode): the backend serves
  // a stale index instantly and refreshes in the background; this timer
  // pulls the merged +1 back once the delta lands.
  const channelFollowupRef = useRef<Set<string>>(new Set());

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
    // Only bust the cache on explicit forced refresh — non-forced refreshes
    // must hit the backend's 90s in-memory channel cache.
    const cacheBust = opts?.force ? `&_t=${Date.now()}` : '';
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
          days: String(clipRangeDays),
          min_days: String(clipRangeMinDays),
          sort: clipSort,
          kick_slug: ch.kickSlug,
          twitch_login: ch.twitchSlug,
          youtube_slug: ch.youtubeSlug,
        });
        if (slug) params.set('url', slug);
        try {
          let data: ChannelClipsResponse;
          try {
            data = await apiGet<ChannelClipsResponse>(`/api/channel/clips?${params}${cacheBust}`);
          } catch (clipErr: unknown) {
            const msg = clipErr instanceof Error ? clipErr.message : '';
            if (!msg.includes('Clips API not on server') && !msg.includes('Clips API unavailable')) {
              throw clipErr;
            }
            params.set('content', 'clips');
            data = await apiGet<ChannelClipsResponse>(`/api/channel/videos?${params}${cacheBust}`);
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
            const data = await apiGet<ChannelVodsResponse>(`/api/channel/videos?${params}${cacheBust}`);
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
        // No prune here: the vods fetch owns the YouTube slice (it merges
        // /videos + /streams), so replacing it with streams-only would drop
        // regular uploads from state on every VODs-tab visit.
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
            force: opts?.force ? '1' : '',
          });
          try {
            const data = await apiGet<ChannelVodsResponse>(`/api/channel/videos?${params}${cacheBust}`);
            attempted[platform] = true;
            incoming.push(...(data.videos ?? []).map(mapApiChannelItem));
            delete errs[platform];
            if (data.refreshing) anyRefreshing = true;
            const pe = data.per_platform_errors?.[platform];
            if (pe && !isHiddenChannelPlatformError(pe)) errs[platform] = pe;
          } catch (err: unknown) {
            attempted[platform] = true;
            errs[platform] = err instanceof Error ? err.message : `Failed to fetch ${platform} VODs`;
          }
        };
        const vodTasks: Promise<void>[] = [];
        let anyRefreshing = false;
        if (wantKick) vodTasks.push(fetchVods('Kick', ch.kickSlug));
        if (wantTwitch) vodTasks.push(fetchVods('Twitch', ch.twitchSlug));
        if (wantYoutube) vodTasks.push(fetchVods('YouTube', ch.youtubeSlug));
        if (!wantKick) delete errs.Kick;
        if (!wantTwitch) delete errs.Twitch;
        if (!wantYoutube) delete errs.YouTube;
        await Promise.all(vodTasks);
        const latest = savedChannelsRef.current.find((c) => c.id === channelId) ?? ch;
        const prunePlatforms: string[] = [];
        if (!incremental) {
          for (const p of ['Kick', 'Twitch', 'YouTube'] as const) {
            if (attempted[p] && !errs[p] && incoming.some((v) => v.platform === p)) prunePlatforms.push(p);
          }
        }
        const vodVideos = mergeVodLists(latest.vodVideos ?? [], incoming,
          prunePlatforms.length ? { prunePlatforms } : undefined);
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
            // The vods fetch now merges the /streams tab, so a successful
            // YouTube fetch with stream rows satisfies the streams cache —
            // otherwise the prefetch effect would re-fetch content=streams
            // and prune the merged uploads+streams list down to streams.
            streamsFetched: !ch.youtubeSlug?.trim()
              || incoming.some((v) => v.content_kind === 'stream')
              || Boolean(errs.YouTube),
            loading: false,
            updatedAt: new Date().toISOString(),
          });
          // Backend served a stale index and is refreshing in the background:
          // schedule one silent incremental pull so the merged +1 shows up
          // without another spinner.
          if (anyRefreshing) {
            const followKey = `${channelId}:${mode}`;
            if (!channelFollowupRef.current.has(followKey)) {
              channelFollowupRef.current.add(followKey);
              window.setTimeout(() => {
                channelFollowupRef.current.delete(followKey);
                refreshChannel(channelId, ch, mode, { incremental: true, silent: true }).catch(() => {});
              }, 6000);
            }
          }
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
  }, [updateChannel, channelContentFilter, clipRangeDays, clipRangeMinDays, clipSort, resetChannelListPaging, clearChannelRefreshFlight]);

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
        ? channelClipsMissing(ch, effectiveKickEnabled, effectiveTwitchEnabled, effectiveYoutubeEnabled)
        : mode === 'streams'
          ? channelStreamsMissing(ch, effectiveYoutubeEnabled)
          : channelVodsMissing(ch, effectiveKickEnabled, effectiveTwitchEnabled, effectiveYoutubeEnabled);
    if (!needsFetch) return;

    const hasCache = channelHasCachedContent(ch, mode, effectiveKickEnabled, effectiveTwitchEnabled, effectiveYoutubeEnabled);
    void refreshChannelRef.current(selectedChannelId, undefined, mode, {
      silent: hasCache,
      force: !hasCache,
      incremental: hasCache,
    });
  }, [channelContentFilter, effectiveKickEnabled, effectiveTwitchEnabled, effectiveYoutubeEnabled, selectedChannelId]);

  // Re-fetch when the user picks a new clip range or sort — only on the Clips tab.
  useEffect(() => {
    if (channelContentFilter !== 'clips') return;
    if (!channelUiPersistReadyRef.current || !selectedChannelId) return;
    const ch = savedChannelsRef.current.find((c) => c.id === selectedChannelId);
    if (!ch) return;
    void refreshChannelRef.current(selectedChannelId, undefined, 'clips', {
      silent: false,
      force: true,
    });
  }, [clipRangeDays, clipRangeMinDays, clipSort, channelContentFilter, selectedChannelId]);

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

  // Deferred incremental VOD sync — fires once, on the first Channels tab
  // open (not at app startup, so entering the app stays network-quiet).
  // Shows cached channel data immediately, then silently refetches every
  // channel (both VODs and clips); the merge functions
  // (mergeVodLists/mergeClipLists) do incoming-wins merge, so the cached
  // data stays visible until fresh data arrives.
  const incrementalSyncDoneRef = useRef(false);
  useEffect(() => {
    if (tab !== 'channels' || incrementalSyncDoneRef.current) return;
    incrementalSyncDoneRef.current = true;
    const channels = loadSavedChannels();
    const tasks: Array<() => Promise<unknown>> = [];
    channels.forEach((c) => {
      tasks.push(() => refreshChannelRef.current(c.id, c, 'vods', { silent: true, incremental: true }));
      tasks.push(() => refreshChannelRef.current(c.id, c, 'clips', { silent: true, incremental: true }));
      // Stream VODs need the same incremental sync — without it the VODs tab
      // goes stale after the first full fetch and never shows new streams.
      if (c.youtubeSlug?.trim()) {
        tasks.push(() => refreshChannelRef.current(c.id, c, 'streams', { silent: true, incremental: true }));
      }
    });
    // ponytail: cap the sync's concurrency (browser limits ~6 connections per
    // host) — firing the whole grid at once saturates the pool and starves
    // the preview player's manifest fetch past hls.js's 10s timeout. The grid
    // fills in the background; the preview must never wait on it.
    let cursor = 0;
    const runNext = (): void => {
      if (cursor >= tasks.length) return;
      const task = tasks[cursor++];
      void Promise.resolve(task()).finally(runNext);
    };
    for (let i = 0; i < Math.min(3, tasks.length); i += 1) runNext();
  }, [tab]);

  // Warm YouTube preview cache for the top N long-form URLs of every channel
  // (VODs + stream VODs only — shorts/clips are cheap to extract on click and
  // don't justify background warm traffic). Runs while the Channels tab is
  // open so app startup stays quiet — the cache is only needed once the user
  // starts browsing channels.
  const warmedUrlsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (tab !== 'channels') return;
    const channels = savedChannels;
    if (!channels.length) return;
    const PER_KIND = 3;
    const STAGGER_MS = 100;
    const KINDS = ['vods', 'streams'] as const;
    const isYouTubeUrl = (u: string) => /youtube\.com|youtu\.be/.test(u || '');
    const urlsForKind = (ch: typeof channels[number], kind: typeof KINDS[number]): string[] => {
      const pool = (ch.vodVideos ?? []).slice(0, YOUTUBE_WARM_VOD_LIMIT).filter((v) =>
        kind === 'streams' ? v.content_kind === 'stream' : v.content_kind !== 'stream' && v.content_kind !== 'clip',
      );
      const out: string[] = [];
      for (const v of pool) {
        const u = v?.url;
        if (u && isYouTubeUrl(u) && !warmedUrlsRef.current.has(u)) {
          warmedUrlsRef.current.add(u);
          out.push(u);
          if (out.length >= PER_KIND) break;
        }
      }
      return out;
    }
    // ponytail: dedup across kinds so the same URL doesn't get re-warmed
    // (a VOD could appear in both vods and clips in stale localStorage).
    const seenForBatch = new Set<string>();
    const queue: string[] = [];
    for (const ch of channels) {
      for (const kind of KINDS) {
        for (const u of urlsForKind(ch, kind)) {
          if (seenForBatch.has(u)) continue;
          seenForBatch.add(u);
          queue.push(u);
        }
      }
    }
    if (!queue.length) return;
    const sendBatch = (urls: string[]) => {
      fetch('/api/preview/warm/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // ponytail: 360 = PREVIEW_FAST_START_HEIGHT — must match the height
        // create_session reads on click ({vid}:360:v2), or the whole batch
        // warm lands in a cache key nobody reads and every click re-extracts.
        body: JSON.stringify({ urls, prefer_height: 360 }),
      }).catch((e) => console.warn('[warm] fetch failed:', e));
    };
    // ponytail: small batches of 2 URLs at a time, 150ms between batches.
    // Keeps WARM_EXECUTOR (4 workers) under saturation and leaves room for
    // the user's click path.
    let i = 0;
    const BATCH = 4;
    const tick = () => {
      const slice = queue.slice(i, i + BATCH);
      i += BATCH;
      if (!slice.length) return;
      sendBatch(slice);
      if (i < queue.length) setTimeout(tick, STAGGER_MS);
    };
    tick();
  }, [savedChannels, tab]);

  const addChannelFromSlugs = useCallback(async (
    kickSlug: string,
    twitchSlug: string,
    youtubeSlug: string,
  ) => {
    const kick = kickSlug.trim();
    const twitch = twitchSlug.trim();
    const youtube = youtubeSlug.trim();
    if (!kick && !twitch && !youtube) return;
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
      return t('This channel is already linked.');
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
    if (effectiveKickEnabled && channelHasKick) {
      setKickVisibleLimit((n) => {
        const next = n + CHANNEL_EXPAND_STEP;
        markBeyond(kickChannelVideos, next, 'Kick');
        return next;
      });
    }
    if (effectiveTwitchEnabled && channelHasTwitch) {
      setTwitchVisibleLimit((n) => {
        const next = n + CHANNEL_EXPAND_STEP;
        markBeyond(twitchChannelVideos, next, 'Twitch');
        return next;
      });
    }
    if (effectiveYoutubeEnabled && channelHasYoutube) {
      setYoutubeVisibleLimit((n) => {
        const next = n + CHANNEL_EXPAND_STEP;
        markBeyond(youtubeChannelVideos, next, 'YouTube');
        return next;
      });
    }
  }, [
    clipsMode,
    effectiveKickEnabled,
    effectiveTwitchEnabled,
    effectiveYoutubeEnabled,
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
  // ponytail: live preview opens the same player as a VOD but registers the
  // already-resolved HLS playlist directly. No InnerTube/yt-dlp extraction —
  // live CDNs (usher.ttvnw.net, googlevideo manifests, etc.) are token-bound
  // and those extractors can't resolve them. Trim/download still work because
  // the session is a regular PreviewSession (kind=hls) proxied via the same
  // /api/preview/hls/{sid}/master.m3u8 endpoint.
  const openLivePreview = useCallback(async (entry: ChannelLiveStatus['live'][number], entries: ChannelLiveStatus['live'], channelName?: string, channel?: SavedChannel | null): Promise<void> => {
    if (!entry?.url) return;
    const name = channelName || entry.platform || 'Live';
    const item: LivePopupItem = {
      id: ++livePopupIdRef.current,
      entry,
      entries: entries.length > 0 ? entries : [entry],
      channelName: name,
      channel: channel ?? null,
    };
    const res = appendLivePopup(livePopupsRef.current, item, MAX_LIVE_POPUPS);
    if (res.blocked) {
      setLivePopupNotice(t('Max {n} live players at once — close one first.', { n: MAX_LIVE_POPUPS }));
      return;
    }
    livePopupsRef.current = res.items;
    setLivePopups(res.items);
  }, []);

  const closeLivePopup = useCallback((id: number) => {
    livePopupsRef.current = livePopupsRef.current.filter((p) => p.id !== id);
    setLivePopups(livePopupsRef.current);
  }, []);

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
      // ── i18n: honor a saved UI language; on first run (no saved value)
      // seed it from the system language and persist, and seed the caption
      // language (asr_language) from the same family ONLY while it is still
      // the default 'auto' — an explicit user choice is never overridden. ──
      const savedLang = s.ui_language as Lang | '' | undefined;
      if (savedLang === 'en' || savedLang === 'pt-BR' || savedLang === 'es') {
        setLanguage(savedLang);
      } else {
        const detected = detectSystemLanguage();
        setLanguage(detected);
        const seed: Partial<AppSettings> = { ui_language: detected };
        if (!s.asr_language || s.asr_language === 'auto') {
          seed.asr_language = langFamily(detected);
        }
        setSettings((prev) => ({ ...prev, ...seed }));
        void apiPost('/api/settings', seed).catch(() => {});
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
        setUpdateMessage(t("You're on the latest version (v{version}).", { version: ver.version }));
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
      setUpdateMessage(err.message || t('Update check failed'));
    } finally {
      setUpdateChecking(false);
    }
  }, [loadUpdateStatus]);

  const handleApplyUpdate = useCallback(async () => {
    if (!updateInfo) return;
    const isSetup = (updateInfo.asset_name || '').toLowerCase().includes('setup');
    const prompt = isSetup
      ? t('Install VOD.RIP v{version}? The installer will open and this app will close.', { version: updateInfo.version })
      : t('Download VOD.RIP v{version}? The verified zip will open in Explorer — extract it over your install folder, or use Setup.exe from GitHub.', { version: updateInfo.version });
    if (!window.confirm(prompt)) return;
    setUpdateApplying(true);
    setUpdateMessage(null);
    try {
      const res = await apiPost<{ ok: boolean; message?: string }>('/api/update/apply', {});
      setUpdateMessage(res.message || t('Update started'));
      if (!isSetup) setUpdateApplying(false);
    } catch (err: any) {
      setUpdateMessage(err.message || t('Update failed'));
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
      setError(err.message || t('Failed to save settings'));
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
    autoOpenPreviewPendingRef.current = vodUrl.trim();
    setAutoOpenPreviewTick((t) => t + 1);
    setUrl(vodUrl);
    setChannelVodPanelOpen(true);
    setUrlTabBarHidden(true);
    setPreviewChannelBadge(badge ?? null);
    void fetchVideoInfo(vodUrl, hint);
  }, [fetchVideoInfo]);

  /** History-row click → same flow as a channel-list pick (auto-opens the preview). */
  const handleOpenVodFromHistory = useCallback((vodUrl: string) => {
    selectVod(vodUrl);
  }, [selectVod]);

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
      videoId: vod.videoId,
      channel: vod.channel,
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

  /**
   * Native archive video id for the current main-preview entry, when known.
   * Prefer the /api/info id (native); fall back to parsing the URL only when
   * unambiguous. Null → hide the SEARCH THIS VIDEO button.
   */
  const previewArchiveVideoId = useMemo(() => {
    if (isNativeArchiveVideoId(videoInfo?.id)) return videoInfo.id;
    return archiveVideoIdFromUrl(url);
  }, [videoInfo?.id, url]);

  /** Stable scope object for the preview-search popup. App rebuilds this
   *  inline on every render (preview time sync re-renders constantly); the
   *  popup's search effect depends on the scope CONTENT, but a raw literal
   *  would still churn the whole popup subtree each tick. Memoize on the
   *  primitive values so the prop identity is stable. */
  const previewSearchScope = useMemo(
    () => (previewArchiveVideoId ? { videoId: previewArchiveVideoId, title: videoInfo?.title ?? '' } : undefined),
    [previewArchiveVideoId, videoInfo?.title],
  );

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
                  warmYoutubePreviewFull(trimmed, 1500);
                }, 300);
              }
            }}
            placeholder={urlFetched ? t('VOD or clip link') : t('PASTE VOD OR CLIP LINK...')}
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
              <><Loader2 size={16} className="animate-spin" /> {t('Loading...')}</>
            ) : (
              <><Info size={16} strokeWidth={3} /> {t('Extract Info')}</>
            )}
          </button>
        )}

        {videoInfo && loading && (
          <div className="flex items-center justify-center gap-2 py-1 text-[10px] font-mono text-zinc-500">
            <Loader2 size={12} className="animate-spin" />
            {t('Updating…')}
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
                {videoInfo.title || t('Untitled')}
              </h3>
              <p className="text-[9px] text-zinc-500 font-mono truncate">
                {videoInfo.uploader || t('Unknown')}
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
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">{t('Quality')}</span>
                <select value={quality} onChange={(e) => {
                  setQuality(e.target.value);
                  qualityUserTouchedUrlRef.current = url.trim();
                }}
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
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">{t('Est. size')}</span>
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
              {t('Audio only (MP3)')}
            </label>
          )}

          <div className="flex flex-col gap-2.5 shrink-0 py-0.5">
            <div className="flex justify-between items-center gap-2">
              <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500 shrink-0">{t('Trim')}</span>
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
        {t('Open URL')}
      </button>
      <button
        type="button"
        onMouseEnter={() => {
          const trimmed = url.trim();
          if (detectUrlPlatform(trimmed) === 'youtube' && !isClipUrl(trimmed)) {
            warmYoutubePreview(trimmed);
          }
        }}
        onClick={() => {
          if (previewOpen) {
            void resetPreview();
          } else {
            void openPreview();
          }
        }}
        disabled={
          previewOpen
            ? false
            : (previewVideoLoading
              || loading
              || vodDurationSec <= 0
              || trimEndSec <= trimStartSec
              || (url.trim() !== '' && videoInfoUrl !== url.trim()))
        }
        className={`flex-1 min-h-0 ${platformWatchPreviewBtn(urlActionPlatform, previewOpen)} disabled:opacity-40`}
      >
        {previewVideoLoading ? (
          <Loader2 size={12} className="animate-spin shrink-0" />
        ) : previewOpen ? (
          <X size={12} className="shrink-0" />
        ) : (
          <Play size={12} fill="currentColor" className="shrink-0" />
        )}
        {previewOpen ? t('Close preview') : t('Watch preview')}
      </button>
      <button
        onClick={promptStartDownload}
        disabled={loading || !videoInfo}
        className={`flex-1 min-h-0 ${platformVodPanelBtn(urlActionPlatform)}`}
      >
        <Download size={16} strokeWidth={3} />
        <span className="inline-flex items-center">
          <span className="tracking-widest">{currentIsClip ? t('Clip rip it') : t('VOD rip it')}</span>
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
          title={t('Volume')}
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

  const previewTrimView: TrimViewWindow = useMemo(
    () => zoomWindowFromView(previewTrimZoom, previewTrimAnchorFrac, vodDurationSec),
    [previewTrimZoom, previewTrimAnchorFrac, vodDurationSec],
  );

  // Wheel-to-zoom on the trim rail. React's synthetic onWheel is passive at the
  // root, so preventDefault would not stop page scroll — attach a native
  // non-passive listener instead (only while the rail is on screen).
  useEffect(() => {
    const rail = previewNeedleRailRef.current;
    if (!rail || !previewOpen || vodDurationSec <= 0) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      bumpPreviewFsControls();
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return;
      const cursorFrac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const factor = e.deltaY < 0 ? TRIM_ZOOM_STEP : 1 / TRIM_ZOOM_STEP;
      const next = zoomTrimViewAround(previewTrimView, cursorFrac, factor, vodDurationSec);
      setPreviewTrimZoom(next.zoom);
      setPreviewTrimAnchorFrac(next.anchorFrac);
    };
    rail.addEventListener('wheel', onWheel, { passive: false });
    return () => rail.removeEventListener('wheel', onWheel);
  }, [previewOpen, vodDurationSec, previewTrimView, bumpPreviewFsControls]);

  const showClipOpenNotice = useCallback((kind: 'error' | 'ok', text: string) => {
    if (clipOpenNoticeTimerRef.current) window.clearTimeout(clipOpenNoticeTimerRef.current);
    setClipOpenNotice({ kind, text });
    clipOpenNoticeTimerRef.current = window.setTimeout(() => setClipOpenNotice(null), 4000);
  }, []);

  /**
   * VOD: open the Twitch clip mini-preview at the playhead (±60s window, user
   * trims there and creates the clip). Live: open the editor directly — no
   * VOD timeline to select from.
   */
  const openPreviewTwitchClip = useCallback(async () => {
    const login = (videoInfo?.channel || '').trim();
    if (!login) {
      showClipOpenNotice('error', t('Extract VOD info first (need the channel login)'));
      return;
    }
    if (isClipUrl(url.trim())) {
      showClipOpenNotice('error', t('A clip can\u2019t be clipped'));
      return;
    }
    if (isLive) {
      setClipOpening(true);
      try {
        const res = await openTwitchClipEditor({ broadcasterLogin: login });
        showClipOpenNotice('ok', `Twitch clip editor opened — ${res.url}`);
      } catch {
        showClipOpenNotice('error', t('Failed to open the Twitch clip editor'));
      } finally {
        setClipOpening(false);
      }
      return;
    }
    const vodId = archiveVideoIdFromUrl(url) ?? undefined;
    if (!vodId) {
      showClipOpenNotice('error', t('Not a Twitch VOD URL'));
      return;
    }
    setTwitchClipPopup({
      url: url.trim(),
      broadcasterLogin: login,
      vodId,
      playheadSec: previewTimeUi,
      vodDurationSec,
    });
  }, [videoInfo?.channel, isLive, url, previewTimeUi, vodDurationSec, showClipOpenNotice]);

  const previewClipPct = vodDurationSec > 0
    ? {
        start: secToFrac(previewTrimStart, previewTrimView) * 100,
        end: secToFrac(previewTrimEnd, previewTrimView) * 100,
        play: secToFrac(previewTimeUi, previewTrimView) * 100,
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
            {previewTrimZoom > 1 && (
              <span
                className="block text-[7px] text-zinc-500"
                title={t('Scroll on the rail to zoom')}
              >
                ×{previewTrimZoom >= 10 ? Math.round(previewTrimZoom) : previewTrimZoom.toFixed(1)}
              </span>
            )}
          </span>
          <div
            ref={previewNeedleRailRef}
            className={`preview-needle-rail relative flex-1 ${
              previewFullscreen ? 'bg-white/10' : 'bg-zinc-800/80'
            }`}
            title={t('Drag needles to set preview clip range')}
            onClick={(e) => {
              if (e.target !== e.currentTarget) return;
              const rail = previewNeedleRailRef.current;
              if (!rail || vodDurationSec <= 0) return;
              const rect = rail.getBoundingClientRect();
              const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
              seekPreviewVideo(fracToSec(frac, previewTrimView));
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
              aria-label={t('Clip in')}
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={previewTrimStart}
              className="preview-needle preview-needle-in absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${previewClipPct.start}%` }}
              onPointerDown={(e) => beginPreviewNeedleDrag(e, 'in')}
            />
            <div
              role="slider"
              aria-label={t('Clip out')}
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
            title={t('Selected clip length')}
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
          title={t('Selected clip length')}
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
        {urlPlatform === 'twitch' && (
          <button
            type="button"
            onClick={() => void openPreviewTwitchClip()}
            disabled={clipOpening || !videoInfo?.channel?.trim()}
            className={`${previewCtrlBtn(previewFullscreen, true)} flex items-center gap-1.5`}
            title={
              !videoInfo?.channel?.trim()
                ? t('Extract VOD info first to enable Twitch clip')
                : isLive
                  ? t('Open Twitch clip editor for this live stream')
                  : t('Open the Twitch clip mini-preview at the playhead')
            }
          >
            {clipOpening ? <Loader2 size={16} className="animate-spin" /> : <TwitchLogoIcon size={15} className="shrink-0" />}
            {/* Brand term — intentionally NOT translated (user request: literal "twitch clip"). */}
            <span className="text-[9px] font-bold uppercase tracking-wider whitespace-nowrap leading-none">twitch clip</span>
          </button>
        )}
        {isLive && (
          <button
            type="button"
            onClick={() => {
              const hls = previewHlsRef.current;
              if (!hls?.media) return;
              // Belt-and-suspenders: verify the Hls instance is in live mode
              if (hls.levels[0]?.details?.live !== true) return;
              if (typeof (hls as any).seekToLiveEdge === 'function') {
                (hls as any).seekToLiveEdge();
                return;
              }
              // Fallback for older HLS.js — prefer liveEdgePosition, else compute
              if (!(hls as any).liveEdgePosition && !isFinite(hls.media.duration)) return;
              const syncDur = (hls.config as any).liveSyncDuration ?? 3;
              const pos = (hls as any).liveEdgePosition ?? hls.media.duration - syncDur;
              if (pos > 0 && isFinite(pos)) hls.media.currentTime = pos;
            }}
            className={previewCtrlBtn(false, true)}
            title={t('Real Time — snap to live edge')}
          >
            <span className="text-[9px] font-bold tracking-wider">{t('● LIVE')}</span>
          </button>
        )}
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
            title={t('Exit fullscreen')}>
            <Minimize2 size={18} />
          </button>
        ) : (
          <button type="button" onClick={() => void togglePreviewFullscreen()}
            disabled={!previewVideoReady}
            className={previewCtrlBtn(false, true)}
            title={t('Fullscreen')}>
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
          : 'gap-6'
      }`}
        style={
          rowEdgeInsets
            ? { width: rowEdgeInsets.usableWidth, maxWidth: rowEdgeInsets.usableWidth }
            : !triplePanelLayout && !splitLayout
              ? { width: effectiveLayout.main.w }
              : undefined
        }
      >
      {previewOpen && (
        <div
          ref={previewPanelRef}
          className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white p-4 flex flex-col gap-3 min-h-0 min-w-0 ${platformCardShadow(layoutPlatform, true)}`}
          style={{ width: effectivePreviewPanelWidth }}
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
                    {previewChannelBadge.isClip ? t('Channel clip preview') : isLive ? t('Live stream') : t('Channel VOD preview')}
                  </span>
                  {!isLive && videoInfo?.title && (
                    <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
                      {videoInfo.title}
                    </p>
                  )}
                  {!isLive && previewSessionMetaRef.current?.channelLanguage ? (
                    <span
                      title={`Channel language: ${previewSessionMetaRef.current.channelLanguage}`}
                      className="mt-0.5 inline-block border border-zinc-700 px-1 py-px text-[7px] font-bold uppercase tracking-wider text-zinc-400 leading-tight"
                    >
                      {previewSessionMetaRef.current.channelLanguage}
                    </span>
                  ) : null}
                  {isLive && (
                    <span className="inline-flex items-center gap-1 mt-0.5">
                      <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
                      <span className="text-[10px] font-bold text-red-400">LIVE</span>
                    </span>
                  )}
                </div>
              </div>
            ) : (
              <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 pt-0.5">
                Preview
              </span>
            )}
            <div className="flex items-center gap-1.5 shrink-0">
              <button
                type="button"
                onClick={togglePreviewSearch}
                aria-pressed={previewSearchOpen}
                title={t('Search the local archive (transcripts + chat)')}
                className={`flex items-center gap-1 border-2 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                  previewSearchOpen
                    ? 'bg-white text-black border-white'
                    : 'border-zinc-700 bg-zinc-800/60 text-zinc-300 hover:border-white hover:text-white'
                }`}
              >
                <Search size={10} className="shrink-0" />
                {previewArchiveVideoId ? t('SEARCH THIS VIDEO') : t('SEARCH ARCHIVE')}
              </button>
              <button type="button" onClick={() => void resetPreview()} className="text-zinc-500 hover:text-white p-1 shrink-0">
                <X size={18} />
              </button>
            </div>
          </div>
          <div ref={previewRowRef} className="flex flex-row gap-2 w-full min-h-0 items-stretch" data-preview-panel>
            <div
              ref={previewContainerRef}
              tabIndex={0}
              role="application"
              aria-label={t('Trim preview player')}
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
                  ? 'relative flex-1 min-w-0 border-0'
                  : 'relative flex-1 min-w-0 shrink-0 border-2 border-zinc-700'
              }`}
              style={!previewFullscreen ? { height: previewPanelHeightRef.current || Math.round(effectivePreviewPanelWidth / Math.max(0.01, previewVideoAspect)), maxHeight: previewVideoAspect < 1 ? '80vh' : undefined, transition: 'max-height 0.3s ease' } : undefined}

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
                      title={t('YouTube trim preview')}
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
                        {urlPlatform === 'youtube' ? t('Starting YouTube preview…') : t('Preparing preview…')}
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
                {clipOpenNotice && (
                  <div className={`flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-wider px-1 ${
                    clipOpenNotice.kind === 'error' ? 'text-red-400' : 'text-[#53fc18]'
                  }`}>
                    <AlertCircle size={11} className="shrink-0" />
                    <span className="truncate">{clipOpenNotice.text}</span>
                  </div>
                )}
              </div>
              {previewFullscreen && (
                <div
                  className="absolute bottom-0 right-0 z-30 w-10 h-10 cursor-pointer"
                  title={t('Exit fullscreen')}
                  onClick={() => void togglePreviewFullscreen()}
                />
              )}
            </div>
            <PreviewChatPanel
              platform={activePlatform}
              videoId={previewArchiveVideoId}
              currentTime={previewTimeUi}
              // Click-to-seek: chat/transcript/event rows and the subtitle
              // caption seek the CURRENT preview player. handlePreviewChatSeek
              // widens the trim when the click falls outside it so the jump
              // always lands on the archive-absolute offset (seekPreviewVideo
              // no-ops until the player is ready; the shared seekToTimestamp
              // helper owns the dispatch).
              onSeek={handlePreviewChatSeek}
              hidden={previewFullscreen}
              // Gates only the URL-only live-captions fetch (video-first);
              // the archive payload starts at session-create so the Twitch
              // chat backfill kicks off before canplay.
              started={previewVideoReady}
              // Reserve the player's layout minimum: the chat panel may never
              // eat into it, whatever the user's stored panel width says.
              // Row budget = card - p-4(16) - border-2(2) per side, minus the
              // PREVIEW_PANEL_MIN_W(280) player reserve and the row's gap-2(8).
              // Below the panel's own minimum there is no room for chat at all
              // and it collapses to zero width. Keep the 36 in sync with the
              // card's p-4/border-2 classes.
              maxWidth={Math.max(0, effectivePreviewPanelWidth - PREVIEW_PANEL_MIN_W - 8 - 36)}
            />
          </div>
          {!previewFullscreen && (
            <PanelResizeHandles onPointerDown={onPreviewPanelResize} />
          )}
        </div>
      )}
      {(showUrlInSidebar || showUrlInPreviewMiddle) && (
        <div
          ref={urlAsidePanelRef}
          className={`group relative shrink-0 overflow-clip bg-zinc-950 border-2 border-white p-4 flex flex-col gap-2 min-h-0 ${platformCardShadow(layoutPlatform, true)}`}
          style={{ width: effectiveLayout.urlAside.w, height: effectiveLayout.urlAside.h, overflowClipMargin: 6 }}
        >
          {showUrlInSidebar && (
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">{t('Selected VOD')}</span>
              <button
                type="button"
                onClick={() => {
                  setChannelVodPanelOpen(false);
                  setVideoInfo(null);
                  setUrl('');
                  setPreviewChannelBadge(null);
                }}
                className="text-zinc-500 hover:text-white p-1"
                title={t('Clear selection')}
              >
                <X size={14} />
              </button>
            </div>
          )}
          {showUrlInPreviewMiddle && (
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">{t('VOD · Trim')}</span>
            </div>
          )}
          <div className="flex-[2] min-h-0 overflow-hidden flex flex-col">
            {urlTabContent}
          </div>
          {urlAsideActionBar}
          {/* urlAside uses overflow-clip + overflow-clip-margin so the
              resize handles can straddle its border like the other panels. */}
          <PanelResizeHandles onPointerDown={onUrlAsidePanelResize} />
        </div>
      )}
      <div
        ref={mainPanelRef}
        style={{ width: effectiveLayout.main.w, height: effectiveLayout.main.h }}
        // @container: VOD-row chrome (thumbnail, index badge, open-in-browser)
        // hides below 320px so the title keeps a real share and stays single-line.
        className={`relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white flex flex-col @container ${
          triplePanelLayout ? 'p-4 gap-3' : urlMainCompact ? 'p-4 gap-2' : 'p-6 gap-4'
        } ${platformCardShadow(layoutPlatform)}`}
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
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> {'//'} <span className="text-[#F03030]">YouTube</span> {t('Downloader')}
              </p>
            )}
            {triplePanelLayout && !urlMainCompact && (
              <p className="text-zinc-500 text-[9px] font-mono tracking-widest uppercase mt-0.5 truncate">
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> {'//'} <span className="text-[#F03030]">YouTube</span>
              </p>
            )}
          </div>
          <div className={`flex gap-1 shrink-0 ${mainCardHeaderCompact ? 'mt-1' : 'mt-2'}`}>
            <button
              type="button"
              onClick={() => { setArchiveSearchScope(null); setArchiveSearchOpen((o) => !o); }}
              title={t('Search the local archive (transcripts + chat)')}
              aria-pressed={archiveSearchOpen}
              className={`flex items-center gap-1 border-2 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                archiveSearchOpen
                  ? 'bg-white text-black border-white'
                  : 'border-zinc-700 border-b-yellow-300/60 bg-zinc-800/60 text-yellow-100/90 hover:border-white hover:text-white'
              }`}
            >
              <Search size={10} className="shrink-0" />
              {t('SEARCH CHAT')}
            </button>
            <div className="w-2 h-2 bg-[#53fc18] rounded-full animate-pulse" />
            <div className="w-2 h-2 bg-[#9146FF] rounded-full animate-pulse" style={{ animationDelay: '0.5s' }} />
            <div className="w-2 h-2 bg-[#F03030] rounded-full animate-pulse" style={{ animationDelay: '1s' }} />
          </div>
        </div>

        {/* ── TABS ── */}
        <div className="flex w-full border-2 border-zinc-800 font-mono text-[10px] uppercase font-bold tracking-widest shrink-0">
          {visibleTabs.map((tabId) => (
            <button
              key={tabId}
              onClick={() => setTab(tabId)}
              className={`flex-1 min-w-0 text-center transition-all flex items-center justify-center gap-2 ${
                mainCardHeaderCompact ? 'py-2' : 'py-3'
              } ${
                tab === tabId ? 'bg-white text-black' : 'bg-transparent text-zinc-500 hover:text-white'
              }`}
            >
              {tabId === 'url' && <Link2 size={14} className="shrink-0" />}
              {tabId === 'channels' && <Users size={14} className="shrink-0" />}
              {tabId === 'queue' && <Download size={14} className="shrink-0" />}
              {tabId === 'settings' && <Settings2 size={14} className="shrink-0" />}
              <span className="truncate">{tabId === 'url' ? t('URL') : tabId === 'channels' ? t('CHANNELS') : tabId === 'queue' ? t('HISTORY') : t('SETTINGS')}</span>
            </button>
          ))}
        </div>

        {/* ── ERROR ── */}
        {error && (
          <div className="border-2 border-red-500/75 bg-red-500/15 p-3 text-red-300 text-xs font-mono flex items-center gap-2 shrink-0">
            <AlertCircle size={14} className="shrink-0" />
            <span className="min-w-0">{error}</span>
            {previewRetry && (
              <button
                type="button"
                onClick={() => void retryPreview()}
                title={t('Retry this preview only')}
                className="ml-auto shrink-0 flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-2 py-1 text-[10px] font-bold uppercase tracking-wider"
              >
                <RefreshCw size={12} />
                {t('Retry')}
              </button>
            )}
            <button onClick={() => { setError(null); previewRetryingRef.current = false; setPreviewRetryBoth(null); }} className={`${previewRetry ? '' : 'ml-auto '}text-red-400/60 hover:text-red-400 shrink-0`}>
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
                placeholder={t('KICK / TWITCH / YOUTUBE NAME OR URL...')}
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
            {livePopupNotice && (
              <p className="text-amber-400 text-[10px] font-mono">{livePopupNotice}</p>
            )}

            {savedChannels.length > 0 && (
              <div ref={channelListRef} className="flex flex-col gap-1">
                {savedChannels.map((ch, index) => {
                  const liveStatus = channelLiveStatuses[ch.id];
                  return (
                  <>
                  <ChannelRow
                    ch={ch}
                    index={index}
                    selected={ch.id === selectedChannelId}
                    isEditing={editingChannelId === ch.id}
                    editingChannelName={editingChannelName}
                    dragId={channelDragId}
                    dropInsertIndex={channelDropInsertIndex}
                    isLast={index === savedChannels.length - 1}
                    savedChannelsLength={savedChannels.length}
                    liveStatus={liveStatus}
                    channelListRef={channelListRef}
                    toggleChannelSelection={toggleChannelSelection}
                    removeChannel={removeChannel}
                    refreshChannel={refreshChannel}
                    clearChannelRefreshFlight={clearChannelRefreshFlight}
                    startRenameChannel={startRenameChannel}
                    commitRenameChannel={commitRenameChannel}
                    setEditingChannelId={setEditingChannelId}
                    setEditingChannelName={setEditingChannelName}
                    removePlatformFromChannel={removePlatformFromChannel}
                    channelContentFilter={channelContentFilter}
                    setSavedChannels={setSavedChannels}
                    setChannelDragId={setChannelDragId}
                    setChannelDropInsertIndex={setChannelDropInsertIndex}
                    openLivePreview={openLivePreview}
                    onOpenChannelSearch={openChannelSearch}
                  />
                  {selectedChannelId === ch.id && (
                    <div className="flex flex-col gap-2 ml-1 pl-2 border-l-2 border-zinc-700 py-1 min-w-0">
                      {(() => {
                        const platformFiltersOn = Number(effectiveKickEnabled) + Number(effectiveTwitchEnabled) + Number(effectiveYoutubeEnabled);
                        // Per-channel lock: a channel whose only platform is enabled must keep it on,
                        // otherwise the chip toggle (which is global) empties this channel's view.
                        const channelPlatformsOn = (['Kick', 'Twitch', 'YouTube'] as const).filter((p) => {
                          const s = p === 'Kick' ? ch.kickSlug : p === 'Twitch' ? ch.twitchSlug : ch.youtubeSlug;
                          const on = p === 'Kick' ? effectiveKickEnabled : p === 'Twitch' ? effectiveTwitchEnabled : effectiveYoutubeEnabled;
                          return Boolean(s?.trim()) && on;
                        }).length;
                        const chipLocked = (enabled: boolean) => enabled && (platformFiltersOn <= 1 || channelPlatformsOn <= 1);
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
                            ? effectiveKickEnabled
                            : platform === 'Twitch'
                              ? effectiveTwitchEnabled
                              : effectiveYoutubeEnabled;
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
                                    if (chipLocked(enabled)) return;
                                    setEnabled((v) => !v);
                                  }}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                      e.preventDefault();
                                      if (chipLocked(enabled)) return;
                                      setEnabled((v) => !v);
                                    }
                                  }}
                                  title={chipLocked(enabled) ? t('At least one platform filter must stay on') : undefined}
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
                          {youtubePlatformOnly ? t('Videos') : t('VODs')}
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
                          {youtubePlatformOnly ? t('Shorts') : t('Clips')}
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
                      {channelContentFilter === 'clips' && !youtubePlatformOnly && (
                        <div className="flex flex-wrap items-center gap-1 font-mono text-[9px] uppercase w-full min-w-0 pt-0.5">
                          <span className="text-zinc-500 shrink-0 mr-1">{t('Range:')}</span>
                          {([
                            { label: t('Today'), days: 1 },
                            { label: '1–7d', days: 7 },
                            { label: '7–14d', days: 14 },
                            { label: '14d–1mo', days: 30 },
                            { label: '1–6mo', days: 180 },
                            { label: '6mo–1y', days: 365 },
                            { label: 'All', days: 0 },
                          ] as const).map((r) => (
                            <button
                              key={r.label}
                              type="button"
                              onClick={() => setClipRangeDays(r.days)}
                              className={`px-1.5 py-0.5 border ${
                                clipRangeDays === r.days
                                  ? 'border-white text-white bg-zinc-900'
                                  : 'border-zinc-700 text-zinc-500 hover:text-white'
                              }`}
                            >
                              {r.label}
                            </button>
                          ))}
                          <span className="text-zinc-500 shrink-0 ml-2 mr-1">{t('Sort:')}</span>
                          <button
                            type="button"
                            onClick={() => setClipSort('date')}
                            className={`px-1.5 py-0.5 border ${
                              clipSort === 'date'
                                ? 'border-white text-white bg-zinc-900'
                                : 'border-zinc-700 text-zinc-500 hover:text-white'
                            }`}
                          >
                            {t('Newest')}
                          </button>
                          <button
                            type="button"
                            onClick={() => setClipSort('views')}
                            className={`px-1.5 py-0.5 border ${
                              clipSort === 'views'
                                ? 'border-white text-white bg-zinc-900'
                                : 'border-zinc-700 text-zinc-500 hover:text-white'
                            }`}
                          >
                            {t('Most Views')}
                          </button>
                        </div>
                      )}
                      </div>
                        );
                      })()}
                      {channelsLoading && visibleChannelVideos.length === 0 ? (
                        <div className="flex justify-center py-4 text-zinc-500">
                          <Loader2 size={18} className="animate-spin" />
                        </div>
                      ) : visibleChannelVideos.length === 0 ? (
                        <>
                          {selectedChannelFirstLiveEntry && (
                            <div
                              role="button"
                              tabIndex={0}
                              onClick={() => void openLivePreview(selectedChannelFirstLiveEntry, selectedChannelLiveEntries, selectedChannel?.displayName, selectedChannel)}
                              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void openLivePreview(selectedChannelFirstLiveEntry, selectedChannelLiveEntries); } }}
                              className="flex items-center gap-1.5 px-1.5 py-1 rounded border border-red-800/40 bg-zinc-800/20 hover:bg-zinc-800/50 cursor-pointer"
                            >
                              <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse shrink-0" />
                              <span className="text-[9px] font-bold text-red-400 shrink-0">{t('● LIVE')}</span>
                              <span className="text-[10px] text-zinc-300 truncate">{selectedChannelFirstLiveEntry.title}</span>
                              <span className="text-[9px] text-zinc-500 shrink-0 ml-auto">{t('LIVE')}</span>
                              {selectedChannelFirstLiveEntry.viewer_count != null && (
                                <span className="text-[9px] text-zinc-500 shrink-0">{selectedChannelFirstLiveEntry.viewer_count}w</span>
                              )}
                            </div>
                          )}
                          <p className="text-center text-zinc-600 font-mono text-[10px] py-3">
                            {channelContentFilter === 'clips'
                              ? (youtubePlatformOnly ? t('No shorts') : t('No clips'))
                              : channelContentFilter === 'streams'
                                ? t('No VODs')
                                : (youtubePlatformOnly ? t('No videos') : t('No VODs'))}
                          </p>
                        </>
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
                                {t('Select all')}
                              </label>
                              <button
                                type="button"
                                onClick={handleBulkDownloadChannelVods}
                                className={platformBulkDownloadBtn(bulkDownloadPlatform, bulkDownloadPlatforms.size > 1)}
                              >
                                <Download size={10} /> {t('Download {count}', { count: selectedChannelVodUrls.size })}
                              </button>
                            </div>
                          )}
                          <div className={`flex flex-col gap-1 transition-opacity duration-150 ${channelsLoading ? 'opacity-60' : ''}`}>
                          {selectedChannelFirstLiveEntry && (
                            <div
                              role="button"
                              tabIndex={0}
                              onClick={() => void openLivePreview(selectedChannelFirstLiveEntry, selectedChannelLiveEntries, selectedChannel?.displayName, selectedChannel)}
                              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void openLivePreview(selectedChannelFirstLiveEntry, selectedChannelLiveEntries); } }}
                              className="flex items-center gap-1.5 px-1.5 py-1 rounded border border-red-800/40 bg-zinc-800/20 hover:bg-zinc-800/50 cursor-pointer"
                            >
                              <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse shrink-0" />
                              <span className="text-[9px] font-bold text-red-400 shrink-0">{t('● LIVE')}</span>
                              <span className="text-[10px] text-zinc-300 truncate">{selectedChannelFirstLiveEntry.title}</span>
                              <span className="text-[9px] text-zinc-500 shrink-0 ml-auto">{t('LIVE')}</span>
                              {selectedChannelFirstLiveEntry.viewer_count != null && (
                                <span className="text-[9px] text-zinc-500 shrink-0">{selectedChannelFirstLiveEntry.viewer_count}w</span>
                              )}
                            </div>
                          )}
                          {visibleChannelVideos.map((v, i) => {
                            const fullUrl = buildVodUrl(v);
                            const subline = channelVodSubline(v);
                            const durSec = channelVideoDurationSec(v);
                            const isClipItem = v.content_kind === 'clip' || channelContentFilter === 'clips';
                            const isShortItem = (v.url || '').includes('/shorts/');
                            const isMembersOnly = isMembersOnlyVideo(v);
                            const isSyntheticYt = (v.platform || '').toLowerCase() === 'youtube' && isSyntheticArchiveId(v.id);
                            const isActiveVod = url.trim() === fullUrl.trim();
                            const rowAccent = platformAccentColor(v.platform);
                            const rowBorder = platformActiveBorder(v.platform);
                            return (
                              <div
                                key={`${v.platform}-${v.id}-${i}`}
                                role="button"
                                tabIndex={isSyntheticYt ? -1 : 0}
                                aria-disabled={isSyntheticYt || undefined}
                                title={isSyntheticYt ? t('Live chat capture — no video') : undefined}
                                data-youtube-warm={v.platform === 'youtube' && !isMembersOnly && !isClipItem && !isShortItem ? fullUrl : undefined}
                                onClick={() => {
                                  if (isSyntheticYt) return;
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
                                  channel: v.channel,
                                  skipNetwork: true,
                                })}}
                                onMouseEnter={() => {
                                  if (v.platform === 'youtube' && !isMembersOnly && !isClipItem) {
                                    warmYoutubePreview(fullUrl);
                                    // ponytail: longer-delay full-VOD mux on hover.
                                    // Fires after ~1s of mouse rest so it only runs
                                    // when the user is genuinely browsing rather than
                                    // sweeping the list. Cache hit makes the next
                                    // click ~instant from local MP4.
                                    warmYoutubePreviewFull(fullUrl, 1000);
                                  }
                                }}
                                onMouseLeave={() => {
                                  if (v.platform === 'youtube' && !isMembersOnly) cancelWarmYoutubePreviewFull(fullUrl);
                                }}
                                onKeyDown={(e) => {
                                  if (isSyntheticYt) return;
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
                                      channel: v.channel,
                                      skipNetwork: true,
                                    });
                                  }
                                }}
                                className={`flex items-center gap-1 border bg-zinc-950 px-2 py-1.5 hover:border-zinc-600 hover:text-white ${isSyntheticYt ? 'cursor-not-allowed' : 'cursor-pointer group'} ${
                                  isActiveVod ? `${rowBorder} bg-zinc-900` : 'border-zinc-800'
                                }`}
                              >
                                <label
                                  className="flex items-center self-stretch pl-2 -ml-2 pr-1 cursor-pointer"
                                  onClick={(e) => {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    if (isSyntheticYt) return;
                                    toggleChannelVodSelection(fullUrl);
                                  }}
                                >
                                  <input
                                    type="checkbox"
                                    checked={selectedChannelVodUrls.has(fullUrl)}
                                    readOnly
                                    tabIndex={-1}
                                    disabled={isSyntheticYt}
                                    className="shrink-0 pointer-events-none"
                                    style={vodCheckboxStyle(rowAccent)}
                                  />
                                </label>
                                <span className="@max-xs:hidden shrink-0"><ChannelClipThumb video={v} /></span>
                                <span className="@max-xs:hidden shrink-0"><ChannelListIndexBadge platform={v.platform} index={v.platformListIndex} /></span>
                                <div className="flex-1 min-w-0 text-left text-[11px] font-mono text-zinc-300 group-hover:text-white">
                                  <span className="truncate flex items-center gap-1">
                                    <PlatformVodIcon platform={v.platform} />
                                    <span className="truncate">
                                      {v.title || t('Untitled')}
                                      {durSec != null ? (
                                        <span className="text-zinc-500 ml-1">
                                          {isClipItem ? fmtClipDuration(durSec) : fmtShort(durSec)}
                                        </span>
                                      ) : null}
                                      {v.channel_language ? (
                                        <span
                                          title={`Channel language: ${v.channel_language}`}
                                          className="ml-1 shrink-0 border border-zinc-700 px-1 py-px text-[8px] font-bold uppercase tracking-wider text-zinc-400"
                                        >
                                          {v.channel_language}
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
                                {isMembersOnly ? (
                                  <span
                                    title={t('Members-only video — preview requires channel membership')}
                                    className="shrink-0 border border-amber-900 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-amber-600 flex items-center gap-0.5 cursor-not-allowed"
                                  >
                                    <Eye size={10} />
                                    Members
                                  </span>
                                ) : isSyntheticYt ? (
                                  <span
                                    title={t('Live chat capture — no video')}
                                    className="shrink-0 border border-zinc-800 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-zinc-600 flex items-center gap-0.5 cursor-not-allowed"
                                  >
                                    <Eye size={10} />
                                    Chat only
                                  </span>
                                ) : (
                                <button
                                  type="button"
                                  title={isClipItem ? t('Preview clip') : t('Preview VOD')}
                                  onMouseEnter={() => {
                                    if (v.platform === 'youtube' && !isClipItem) {
                                      warmYoutubePreview(fullUrl);
                                      warmYoutubePreviewFull(fullUrl, 1000);
                                    }
                                  }}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    void openExplorePlayer(v);
                                  }}
                                  className="shrink-0 border border-zinc-700 px-1 py-0.5 text-[8px] font-bold uppercase text-zinc-400 hover:border-white hover:text-white flex items-center gap-0.5"
                                >
                                  <Eye size={10} />
                                  Preview
                                </button>
                                )}
                                <button
                                  type="button"
                                  title={isSyntheticYt ? t('Live chat capture — no video') : t('Open in browser')}
                                  disabled={isSyntheticYt}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    window.open(fullUrl, '_blank', 'noopener,noreferrer');
                                  }}
                                  className="text-zinc-600 hover:text-white p-1 shrink-0 @max-xs:hidden disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-zinc-600"
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
                  </>
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
            onOpenVod={handleOpenVodFromHistory}
          />
        )}

        {tab === 'settings' && (
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

      </div>

      {/* Handles must be a direct child of the panel, NOT of the tab-content
          scroll container: on CHANNELS/HISTORY/SETTINGS that container carries
          .custom-scrollbar (contain: layout paint), which would make it the
          containing block for the absolute handle host and park the handles at
          the scroll-area edge instead of the panel border. */}
      <PanelResizeHandles onPointerDown={onMainPanelResize} />
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
              onOpenHit={openArchiveHit}
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
              onOpenHit={openArchiveHit}
              savedChannels={savedChannels}
            />
          ))}
        </>,
        document.getElementById('explore-portal') ?? document.body,
      )}
      <div className="fixed bottom-10 right-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        TWITCH
      </div>

      {livePopups.map((popup, idx) => {
        // DVR REPLAY archive context (channel slug + newest public in-progress
        // VOD) resolved from the ENTRY's own channel — the live button on any
        // row may be clicked, not just the selected channel's.
        const { channelSlug, vodUrl } = liveArchiveContext(
          popup.channel,
          popup.entry.platform,
        );
        return (
          <LivePlayerPopup
            key={popup.id}
            entry={popup.entry}
            entries={popup.entries}
            channelName={popup.channelName}
            channelSlug={channelSlug}
            vodUrl={vodUrl}
            cascadeIndex={idx}
            onClose={() => closeLivePopup(popup.id)}
            onOpenHit={openArchiveHit}
            savedChannels={savedChannels}
          />
        );
      })}
      <NeedleGlancePopup glance={needleGlance} vodDurationSec={vodDurationSec} />
      {twitchClipPopup && (
        <TwitchClipPopup
          url={twitchClipPopup.url}
          broadcasterLogin={twitchClipPopup.broadcasterLogin}
          vodId={twitchClipPopup.vodId}
          playheadSec={twitchClipPopup.playheadSec}
          vodDurationSec={twitchClipPopup.vodDurationSec}
          zIndex={EXPLORE_POPUP_Z + 200}
          onClose={() => setTwitchClipPopup(null)}
          onClipCreated={(editorUrl) =>
            showClipOpenNotice('ok', `Twitch clip editor opened — ${editorUrl}`)}
        />
      )}
      {archiveSearchOpen && (
        <ArchiveSearchPopup
          zIndex={SEARCH_POPUP_Z}
          onClose={() => { setArchiveSearchOpen(false); setArchiveSearchScope(null); setArchiveSearchChannel(null); }}
          onOpenHit={openArchiveHit}
          scope={archiveSearchScope ?? undefined}
          savedChannels={savedChannels}
          initialChannel={archiveSearchChannel ?? undefined}
        />
      )}
      {previewOpen && previewSearchOpen && (
        <ArchiveSearchPopup
          zIndex={SEARCH_POPUP_Z}
          initialPos={previewSearchAnchorRef.current ?? undefined}
          onClose={() => setPreviewSearchOpen(false)}
          onOpenHit={openArchiveHit}
          onSeekHit={previewArchiveVideoId ? (hit) => handlePreviewChatSeek(hit.offset_sec) : undefined}
          onSeekOffset={previewArchiveVideoId ? (sec) => handlePreviewChatSeek(sec) : undefined}
          scope={previewSearchScope}
          savedChannels={savedChannels}
        />
      )}
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
