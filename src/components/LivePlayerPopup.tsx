import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ExternalLink, Loader2, Maximize2, Minimize2, MessageSquare, Pause, Play, Search, Volume2, VolumeX, RefreshCw, X } from 'lucide-react';
import { apiDelete, apiPost } from '../hooks/useApiClient';
import { openTwitchLiveClipEditorInBrowser, reportClipEvent } from '../twitchClip';
import { useI18n } from '../i18n';
import type { PanelSize, PreviewSessionResponse, SavedChannel } from '../types';
import ArchiveSearchPopup from './ArchiveSearchPopup';
import type { ArchiveSearchHit, ArchiveVideoRow } from '../archiveSearchUtils';
import {
  PanelResizeHandles,
  type ResizeEdge,
} from '../explorePopupUtils';
import {
  LIVE_PANEL_MAX_H,
  LIVE_PANEL_MAX_W,
  LIVE_PANEL_MIN_H,
  LIVE_PANEL_MIN_W,
  LIVE_POPUP_ACTIVE_Z,
  PREVIEW_KEY_SKIP_SEC,
  PREVIEW_VIDEO_ASPECT_DEFAULT,
  panelPosAfterResize,
  startPanelResizeDrag,
} from '../layoutUtils';
import { shouldIgnorePlayerKeyEvent } from '../keyboardUtils';
import PreviewQualityMenu from '../PreviewQualityMenu';
import {
  TRIM_ZOOM_STEP,
  type TrimViewWindow,
  maxTrimZoomForDuration,
  secToFrac,
  zoomTrimViewAround,
  zoomWindowFromView,
} from '../trimUtils';
import { platformButtonShadow, platformPreviewCtrlBtn, type PlatformStyleKey } from '../platformStyles';
import { createTwitchAdRotationHandler, twitchAdBlockHlsConfig } from '../twitchAdBlock';
import {
  clampClipSeconds,
  clipCooldownRemaining,
  FAST_CLIP_COOLDOWN_MS,
  FAST_CLIP_DEFAULT_SEC,
  filterLiveLevels,
  liveBroadcastPositionSec,
  liveChatSlugFromUrl,
  livePanelSizeFromAspect,
  parsePlaylistTotalSec,
  qualityLevelForPolicy,
  replaySeekTarget,
  type LivePanelAspectClamp,
} from '../livePlayerLevels';
import LiveChatPanel, { LIVE_CHAT_PANEL_W } from './LiveChatPanel';
import TwitchLogoIcon from './TwitchLogoIcon';
import { previewRetryAfterError, type PreviewRetryState } from '../previewRetry';
import { noteUserUnpause, pauseOtherPreviews, registerPreviewPlayback } from '../previewPlaybackBus';
import { nextLiveEntry } from '../liveEntryFallback';
import { fmtDuration } from '../formatters';
import { createFullscreenGate, nativeFullscreenAdapter, type FullscreenGate } from '../utils/fullscreenGate';
// hls.js is ~900KB and the original file deliberately code-splits it out of the
// main bundle; a static import would pull it into the initial chunk.
import type Hls from 'hls.js';

interface LiveEntry {
  url: string;
  title?: string;
  platform?: string;
  headers?: Record<string, string>;
}

interface LivePlayerPopupProps {
  entry: LiveEntry;
  /** All of the channel's live entries (the badge list) — the popup
   *  auto-advances to the next one when the opened entry's session fails or
   *  stalls. Defaults to just `entry` when omitted. */
  entries?: LiveEntry[];
  channelName: string;
  onClose: () => void;
  /** Platform slug for the open-channel button (twitchSlug/kickSlug/youtubeSlug). */
  channelSlug?: string;
  /** Saved channel — its kick/twitch/youtube slugs resolve multi-chat rooms
   *  when the channel is live on more than one platform. */
  channel?: SavedChannel | null;
  /** Channel's current (in-progress) VOD URL — DVR REPLAY archive source. */
  vodUrl?: string;
  /** Open an archive hit in the explore-player flow (App owns the popup stack). */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => void;
  /** Optional saved channels (App state) — unioned into the channel dropdown. */
  savedChannels?: SavedChannel[];
  /** Position cascade offset — 0 = default corner; each sibling steps 28px. */
  cascadeIndex?: number;
  /** Shared floating-player z-ladder rank (App owns the monotonic counter).
   *  Omitted → falls back to LIVE_POPUP_ACTIVE_Z. */
  zIndex?: number;
  /** App raises this popup's ladder rank on pointer-down (drag to front). */
  onBringToFront?: () => void;
}

type DragState = {
  startX: number;
  startY: number;
  offsetX: number;
  offsetY: number;
} | null;

interface LevelInfo {
  index: number;
  label: string;
  height: number;
}

const POPUP_WIDTH = 480;
const POPUP_HEIGHT = 320;
const RESIZE_MARGIN = 32; // keep at least 16px of the popup on screen while resizing
/** Live preview chat stays CLOSED by default — the header toggle opens it
 *  (the popup then grows to fit the docked panel). */
const INITIAL_CHAT_OPEN = false;
/** Fallback header height for the aspect-lock math when the header has not
 *  been measured yet (drag starts before the first paint). */
const POPUP_HEADER_EST = 52;

/** Honest fast-clip capability payload — this build has NO server-side live
 *  clip path, so `available` is always false and `reason`/`needed` document
 *  the exact backend requirement (never a fake clip id). */
interface LiveClipCapability {
  available: boolean;
  reason?: string;
  needed?: string[];
}
/** Re-snapshot the archive playlist while parked in REPLAY (grows while live). */
const REPLAY_RESNAPSHOT_MS = 30_000;
/** Live session POST stall budget — after this the popup advances to the next
 *  live entry (or surfaces the error) instead of pinning the spinner. */
const SESSION_STALL_MS = 8_000;
/** Inactivity fade for the transport + header ('rapido' per the user) — the
 *  App fullscreen pattern is 200ms; the live popup watches at 2500ms so a
 *  paused read of the rail/menus never flickers. */
const LIVE_CONTROLS_HIDE_MS = 2_500;
/** Retained live back-buffer (hls.js backBufferLength) — ArrowLeft/Right seek
 *  inside it when the live has NO DVR archive (the rail stays disabled). */
const LIVE_BACK_BUFFER_SEC = 30;
/** Never land the back-buffer arrow seek ON the live edge — stay a hair below
 *  it (hls.js's own sync target rides ~liveSyncDuration behind the edge). */
const LIVE_EDGE_SEEK_SAFETY_SEC = 0.75;

// ---------------------------------------------------------------------------
// Live quality policy registry (module-scope — ponytail: a plain counter +
// subscriber Set, no store dependency; the whole policy is one number).
//
// Every open live popup registers on mount and unregisters on unmount. The
// count drives each player's fixed quality (see qualityLevelForPolicy):
// ONE player → SOURCE (highest quality); MULTIPLE players → the ≤480p ladder.
// Subscribers are notified with the NEW count on every register/unregister so
// ALL registered players re-apply their policy immediately — opening a second
// live caps every player, closing one restores the rest to source.
// ---------------------------------------------------------------------------
const liveQualitySubscribers = new Set<(count: number) => void>();
let liveQualityCount = 0;

/** Register a live player; returns the unregister function. The subscriber
 *  fires synchronously on register (count includes this player). */
export function registerLivePlayer(subscriber: (count: number) => void): () => void {
  liveQualitySubscribers.add(subscriber);
  liveQualityCount += 1;
  for (const sub of liveQualitySubscribers) sub(liveQualityCount);
  return () => {
    // Idempotent: a second unregister (StrictMode cleanup double-invoke,
    // defensive consumer calls) must not decrement again — the count drives
    // every other player's quality.
    if (!liveQualitySubscribers.delete(subscriber)) return;
    liveQualityCount = Math.max(0, liveQualityCount - 1);
    for (const sub of liveQualitySubscribers) sub(liveQualityCount);
  };
}

/** Current number of registered live players — the policy's multi input. */
export function liveQualityCountNow(): number {
  return liveQualityCount;
}

/** Test hook — reset the module-level registry between tests. */
export function __resetLivePlayerRegistryForTests(): void {
  liveQualitySubscribers.clear();
  liveQualityCount = 0;
}

export function LivePlayerPopup({ entry, entries, channelName, onClose, channelSlug, channel, vodUrl, onOpenHit, savedChannels, cascadeIndex = 0, zIndex, onBringToFront }: LivePlayerPopupProps) {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const railRef = useRef<HTMLInputElement>(null);
  // Spawned windows take focus (shared raise-to-front contract) — the popup
  // is the active surface the moment it opens.
  useEffect(() => {
    popupRef.current?.focus({ preventScroll: true });
  }, []);
  // Join the shared preview-pause bus: opening a live pauses every other
  // preview, and opening a clip/preview elsewhere pauses this live. Without
  // this, a live kept playing audio under any new preview (user report).
  useEffect(() => {
    pauseOtherPreviews();
    const pause = () => {
      const video = videoRef.current;
      if (video && !video.paused) {
        video.pause();
        setPaused(true);
      }
    };
    return registerPreviewPlayback(pause);
  }, []);
  const hlsRef = useRef<Hls | null>(null);
  const hlsCtorRef = useRef<typeof Hls | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const sessionRef = useRef<PreviewSessionResponse | null>(null);
  // Fallback chain: the popup opens on entries[0]; on session failure it
  // advances through the rest, one attempt each (see nextLiveEntry). The
  // index lives in a ref so late hls.js error callbacks read the CURRENT
  // entry, not the one captured when the callback was created.
  const allEntries = useMemo<LiveEntry[]>(
    () => (entries && entries.length > 0 ? entries : [entry]),
    [entries, entry],
  );
  const [entryIndex, setEntryIndex] = useState(0);
  const entryIndexRef = useRef(0);
  /** The entry this popup is currently playing (== `entry` until a fallback). */
  const activeEntry = allEntries[entryIndex] ?? entry;
  /** Quality policy: platform drives the live level ladder (youtube →
   *  360/720/1080 or 360-anon; twitch/kick → up to source). */
  const sessionPlatformRef = useRef((activeEntry.platform || '').toLowerCase());
  // First-frame timing marker (fast-start verification hook).
  const firstFrameStartRef = useRef<number>(performance.now());
  const firstFrameLoggedRef = useRef(false);
  const sizeRef = useRef<PanelSize>({ w: POPUP_WIDTH, h: POPUP_HEIGHT });
  // Header height + video aspect, captured at drag start / media load so the
  // aspect-lock math uses real geometry (no re-layout mid-drag).
  const headerRef = useRef<HTMLDivElement>(null);
  const videoAspectRef = useRef<number>(PREVIEW_VIDEO_ASPECT_DEFAULT);
  // Live chat docks right of the video — open by default so the feature is
  // visible; the popup opens wide enough that the video keeps 480px.
  const [chatOpen, setChatOpen] = useState(INITIAL_CHAT_OPEN);
  const chatOpenRef = useRef(INITIAL_CHAT_OPEN);
  useEffect(() => { chatOpenRef.current = chatOpen; }, [chatOpen]);
  // Fast CLIP: seconds input (5..60) + 5s cooldown + transient notification.
  const [clipSeconds, setClipSeconds] = useState(FAST_CLIP_DEFAULT_SEC);
  const lastClipAtRef = useRef(0);
  const [clipCooldownLeft, setClipCooldownLeft] = useState(0);
  const clipCooldownTimerRef = useRef<number | null>(null);
  const [clipNotice, setClipNotice] = useState<string | null>(null);
  const clipNoticeTimerRef = useRef<number | null>(null);
  const [position, setPosition] = useState(() => {
    const w = Math.min(window.innerWidth - RESIZE_MARGIN, POPUP_WIDTH + (INITIAL_CHAT_OPEN ? LIVE_CHAT_PANEL_W : 0));
    return {
      x: Math.max(8, window.innerWidth - w - 24 - cascadeIndex * 28),
      y: 80 + cascadeIndex * 28,
    };
  });
  const posRef = useRef(position);
  const [size, setSize] = useState<PanelSize>({
    // Chat open by default → the popup is wider; clamp so a small viewport
    // never spawns an off-screen popup (resize re-clamps the same way).
    w: Math.min(window.innerWidth - RESIZE_MARGIN, POPUP_WIDTH + (INITIAL_CHAT_OPEN ? LIVE_CHAT_PANEL_W : 0)),
    h: POPUP_HEIGHT,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Per-media retry state — the live pipeline is a single stage (session
  // create + attach happen together), so every retry re-runs it end-to-end.
  const [previewRetry, setPreviewRetry] = useState<PreviewRetryState | null>(null);
  const previewRetryRef = useRef<PreviewRetryState | null>(null);
  const previewRetryingRef = useRef(false);
  /** ONE invisible session recreate per popup (fatal NETWORK_ERROR): the first
   *  fatal error re-creates the backend session (fresh usher/master tokens)
   *  before the startLoad retries — a stale session's startLoad cannot recover. */
  const silentRecreateDoneRef = useRef(false);
  /** True while the mount effect is re-running as that invisible recreate —
   *  the effect then skips the loading swap (keeps the last frame visible). */
  const invisibleRecreateRef = useRef(false);
  const [retryTick, setRetryTick] = useState(0);
  const clearRetry = useCallback(() => {
    previewRetryingRef.current = false;
    previewRetryRef.current = null;
    setPreviewRetry(null);
  }, []);
  const markPreviewError = useCallback(() => {
    const wasRetry = previewRetryingRef.current;
    previewRetryingRef.current = false;
    setPreviewRetry(previewRetryAfterError(previewRetryRef.current, activeEntry.url, 'session', wasRetry));
  }, [activeEntry.url]);
  /** Advance to the next live entry; false when the chain is exhausted (the
   *  caller then surfaces the error as usual). Deleting the stale session
   *  mirrors retryPreview — a failed session must not linger on the backend. */
  const tryAdvanceEntry = useCallback((): boolean => {
    const next = nextLiveEntry(allEntries, entryIndexRef.current);
    if (!next) return false;
    const sid = sessionIdRef.current;
    if (sid) {
      void apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      sessionIdRef.current = null;
    }
    sessionRef.current = null;
    entryIndexRef.current += 1;
    setEntryIndex(entryIndexRef.current);
    return true;
  }, [allEntries]);
  const [drag, setDrag] = useState<DragState>(null);

  // Transport state
  const [paused, setPaused] = useState(false);
  const [muted, setMuted] = useState(true);
  const [volume, setVolume] = useState(1);
  const [volumeMenuOpen, setVolumeMenuOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  /** Docked archive-search panel (global search — live has no archive identity). */
  const [searchOpen, setSearchOpen] = useState(false);

  // Quality state
  const [levels, setLevels] = useState<LevelInfo[]>([]);
  const [currentLevel, setCurrentLevel] = useState(-1);
  const [qualityMenuOpen, setQualityMenuOpen] = useState(false);
  const [abortRef] = useState(() => new AbortController());

  // Buffering UX (mirrors the mini preview player's waiting→spinner debounce)
  const [buffering, setBuffering] = useState(false);
  const bufferingTimerRef = useRef<number | null>(null);
  const pendingReplaySeekRef = useRef<number | null>(null);
  // True when the user paused (togglePlay) — unexpected pauses (e.g. the
  // play() promise aborting on a live-sync seek) auto-resume instead.
  const userPausedRef = useRef(false);

  // Inactivity auto-hide for the transport + header: a ref-backed timer
  // hides ~2.5s after the last interaction; any mousemove/keydown/pointerdown/
  // touchstart bumps it back. The timer only ARMS when a hide-block is clear —
  // paused/loading/error/open menus keep the controls up regardless.
  const [controlsVisible, setControlsVisible] = useState(true);
  const controlsHideTimerRef = useRef<number | null>(null);

  // DVR state — LIVE (default, live master) vs REPLAY (ENDLIST snapshot of the
  // channel's in-progress VOD). Mode switches recreate the hls instance so a
  // seek never breaks the other mode's playback.
  const [mode, setMode] = useState<'live' | 'replay'>('live');
  const modeRef = useRef<'live' | 'replay'>('live');
  const [archiveDuration, setArchiveDuration] = useState(0); // backend-probed, session creation
  const archiveReadyRef = useRef(false); // lazy archive resolved (replay rail usable)
  const [archiveReady, setArchiveReady] = useState(false);
  const [snapshotDuration, setSnapshotDuration] = useState(0); // replay: video.duration
  const [railTime, setRailTime] = useState(0);
  // Wheel-zoom on the replay rail — same precision UX as the main preview
  // trim rail: (zoom, anchorFrac) fully describes the visible window.
  const [railZoom, setRailZoom] = useState(1);
  const [railAnchorFrac, setRailAnchorFrac] = useState(0.5);
  // Mirror railTime for the window keydown/wheel listeners — reattaching on
  // every timeupdate (4 Hz) would churn the listeners for no reason.
  const railTimeRef = useRef(0);
  const replayTimerRef = useRef<number | null>(null);

  // Keep refs in sync with state (drag/resize use the latest size without re-subscribing)
  useEffect(() => { sizeRef.current = size; }, [size]);
  useEffect(() => { posRef.current = position; }, [position]);
  useEffect(() => { modeRef.current = mode; }, [mode]);
  useEffect(() => { railTimeRef.current = railTime; }, [railTime]);

  /** Cache-busted archive snapshot URL — each new URL forces a fresh ENDLIST snapshot. */
  const replaySnapshotUrl = useCallback((sid: string) => {
    return `/api/preview/hls/${sid}/resource?id=replay-playlist&t=${Date.now()}`;
  }, []);

  const channelUrl = useMemo(() => {
    const plat = (activeEntry.platform || '').toLowerCase();
    if (!channelSlug) return null;
    if (plat === 'youtube') return `https://www.youtube.com/@${channelSlug}`;
    if (plat === 'kick') return `https://kick.com/${channelSlug}`;
    return `https://www.twitch.tv/${channelSlug}`;
  }, [activeEntry.platform, channelSlug]);

  // Live chat rooms — ONE merged source per distinct platform the channel is
  // live on (multi-stream → multi-chat with filter chips). Each slug prefers
  // the playing entry's URL (fallback chain can advance to a different
  // channel), then the channel's own slug for that platform, then the
  // archive-context slug. Single-platform streams produce one source and the
  // panel shows no filters (original behavior).
  const chatSources = useMemo(() => {
    const order = { kick: 0, twitch: 1, youtube: 2 } as Record<string, number>;
    const byPlatform = new Map<string, string>();
    for (const e of allEntries) {
      const plat = (e.platform || '').toLowerCase();
      if (!plat || byPlatform.has(plat)) continue;
      const saved = plat === 'kick' ? channel?.kickSlug
        : plat === 'twitch' ? channel?.twitchSlug
        : channel?.youtubeSlug;
      const slug = liveChatSlugFromUrl(e.url, plat) ?? saved ?? channelSlug;
      if (slug) byPlatform.set(plat, slug);
    }
    return [...byPlatform.entries()]
      .sort((a, b) => (order[a[0]] ?? 9) - (order[b[0]] ?? 9))
      .map(([platform, slug]) => ({ platform, slug }));
  }, [allEntries, channel, channelSlug]);

  // Handle level selection (original hls.levels indices)
  const handleQualitySelect = useCallback((index: number) => {
    if (modeRef.current === 'replay') return; // snapshot is single-level — no switching
    if (hlsRef.current) {
      hlsRef.current.currentLevel = index;
      setCurrentLevel(index);
    }
    setQualityMenuOpen(false);
  }, []);

  // Live quality policy — apply the FIXED level the open-player count demands
  // (single → SOURCE, multiple → ≤480p ladder). Runs on MANIFEST_PARSED and on
  // every registry count change. A pinned currentLevel disables ABR: hls.js's
  // autoLevelEnabled is a getter-only (manualLevel !== -1 ⇒ false) — assigning
  // it directly would throw in strict mode, and it stays off for the session
  // (the hls instance is destroyed on unmount, so there is no auto-level state
  // to restore). The quality menu can still override — the next count change
  // re-applies the policy on top.
  const applyQualityPolicy = useCallback(() => {
    const hls = hlsRef.current;
    if (!hls || modeRef.current !== 'live' || !hls.levels || hls.levels.length === 0) return;
    const multi = liveQualityCountNow() > 1;
    const idx = qualityLevelForPolicy(hls.levels, multi);
    if (idx < 0) return;
    hls.currentLevel = idx;
    setCurrentLevel(idx);
  }, []);

  // Registry membership: register on mount (notifies every player — this one
  // no-ops until its manifest parses), unregister on unmount (reverts the
  // remaining players to their policy for the lower count).
  useEffect(() => registerLivePlayer(() => { applyQualityPolicy(); }), [applyQualityPolicy]);

  // vaft midroll rotation: after repeated stitched segments the backend swaps
  // this session's usher master to the next player type in place — reloading
  // the same proxied URL serves the rotated stream. Failure = keep stripping.
  const onAdRotation = React.useMemo(
    () => createTwitchAdRotationHandler({
      getSessionId: () => sessionIdRef.current,
      getHls: () => hlsRef.current,
      getVideo: () => videoRef.current,
      requestRotation: (sid) =>
        apiPost<{ ok?: boolean; master_url?: string }>(`/api/preview/live/rotate/${sid}`, {}),
    }),
    [],
  );

  // Close menus on outside click: a menu stays open only while the pointerdown
  // lands inside its own layout (wrapper + toggle button). Any other click —
  // other player buttons included — closes it. Re-registers as the open state
  // flips so the closure sees the current menu set.
  useEffect(() => {
    if (!qualityMenuOpen && !volumeMenuOpen) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (volumeMenuOpen && target.closest('[data-volume-menu]')) return;
      if (qualityMenuOpen && target.closest('[data-quality-menu]')) return;
      setQualityMenuOpen(false);
      setVolumeMenuOpen(false);
    };
    window.addEventListener('mousedown', handler);
    return () => window.removeEventListener('mousedown', handler);
  }, [qualityMenuOpen, volumeMenuOpen]);

  // --- Auto-hide: bump + hide-block rules ---
  // This build has no clip-seconds POPOVER (the seconds field is inline in
  // the transport); the focused-text-input check below covers the equivalent
  // pause: while the user is actively editing the field the controls stay up.
  const isTransportTextFocused = () => {
    const el = document.activeElement;
    return el instanceof HTMLElement
      && el.closest('[data-live-transport]') !== null
      && el.tagName === 'INPUT'
      && (el as HTMLInputElement).type === 'text';
  };
  const hideBlocked = paused || loading || error !== null
    || volumeMenuOpen || qualityMenuOpen || isTransportTextFocused();
  const hideBlockedRef = useRef(hideBlocked);
  hideBlockedRef.current = hideBlocked;
  const controlsHidden = !controlsVisible && !hideBlocked;

  /** Interaction anywhere in the popup — show the controls and restart the
   *  inactivity countdown (same bump-on-interaction UX as App's fullscreen
   *  player, just a longer window). */
  const bumpControls = useCallback(() => {
    setControlsVisible(true);
    if (controlsHideTimerRef.current != null) {
      window.clearTimeout(controlsHideTimerRef.current);
      controlsHideTimerRef.current = null;
    }
    if (hideBlockedRef.current) return;
    controlsHideTimerRef.current = window.setTimeout(() => {
      controlsHideTimerRef.current = null;
      setControlsVisible(false);
    }, LIVE_CONTROLS_HIDE_MS);
  }, []);

  // Block flips cancel the countdown (a pause/menu must not hide on a stale
  // timer); clearing a block — or bump() re-showing — re-arms it so the fade
  // always restarts from the LAST interaction.
  useEffect(() => {
    if (hideBlocked) {
      if (controlsHideTimerRef.current != null) {
        window.clearTimeout(controlsHideTimerRef.current);
        controlsHideTimerRef.current = null;
      }
      return;
    }
    if (!controlsVisible || controlsHideTimerRef.current != null) return;
    controlsHideTimerRef.current = window.setTimeout(() => {
      controlsHideTimerRef.current = null;
      setControlsVisible(false);
    }, LIVE_CONTROLS_HIDE_MS);
  }, [hideBlocked, controlsVisible]);

  const destroyHls = useCallback(() => {
    if (replayTimerRef.current != null) {
      window.clearInterval(replayTimerRef.current);
      replayTimerRef.current = null;
    }
    if (pendingReplaySeekRef.current != null) {
      window.clearTimeout(pendingReplaySeekRef.current);
      pendingReplaySeekRef.current = null;
    }
    if (bufferingTimerRef.current != null) {
      window.clearTimeout(bufferingTimerRef.current);
      bufferingTimerRef.current = null;
      setBuffering(false);
    }
    if (hlsRef.current) {
      try {
        hlsRef.current.destroy();
      } catch {
        /* ignore */
      }
      hlsRef.current = null;
    }
  }, []);

  // Debounced waiting→overlay (mirrors attachPreviewBufferingListeners).
  const showBuffering = useCallback(() => {
    if (bufferingTimerRef.current != null) return;
    bufferingTimerRef.current = window.setTimeout(() => {
      bufferingTimerRef.current = null;
      setBuffering(true);
    }, 150);
  }, []);
  const clearBuffering = useCallback(() => {
    if (bufferingTimerRef.current != null) {
      window.clearTimeout(bufferingTimerRef.current);
      bufferingTimerRef.current = null;
    }
    setBuffering(false);
  }, []);

  /** ONE invisible session recreate per popup — the first fatal NETWORK_ERROR
   *  deletes the stale session and re-runs the mount effect (re-POST + re-
   *  attach) with NO error UI: the frame stays up and the buffering overlay
   *  covers the swap. Subsequent fatals fall back to the startLoad retries. */
  const recreateSessionInvisible = useCallback(() => {
    invisibleRecreateRef.current = true;
    setError(null);
    const sid = sessionIdRef.current;
    if (sid) {
      void apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      sessionIdRef.current = null;
    }
    sessionRef.current = null;
    setRetryTick((t) => t + 1);
  }, []);

  /** Create an hls.js instance for *src*; live mode applies the quality policy after parse. */
  const createHlsPlayer = useCallback(async (src: string, startPos: number): Promise<any | null> => {
    const video = videoRef.current;
    if (!video) return null;
    if (!hlsCtorRef.current) {
      try {
        hlsCtorRef.current = (await import('hls.js')).default;
      } catch {
        setError(t('HLS not supported in this browser'));
        setLoading(false);
        markPreviewError();
        return null;
      }
    }
    const Hls = hlsCtorRef.current;
    if (!Hls || !Hls.isSupported()) {
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        video.addEventListener('loadedmetadata', () => { setLoading(false); clearRetry(); }, { once: true });
        video.play().catch(() => {});
        return null;
      }
      setError(t('HLS not supported in this browser'));
      setLoading(false);
      markPreviewError();
      return null;
    }
    destroyHls();
    const replay = modeRef.current === 'replay';
    // Replay: autoStartLoad off — the position is applied in MANIFEST_PARSED
    // (startLoad before the manifest parses is a no-op, which would start the
    // snapshot at 0 instead of the dragged time).
    // hls.js surface mirrors the mini preview player (App.tsx) except the
    // buffer/live-sync geometry (see the config below): enableWorker,
    // lowLatencyMode on (LL-HLS part handling), long timeouts, the adblock
    // pLoader, and the live sync knobs. capLevelToPlayerSize is DELIBERATELY
    // absent — the mini preview caps to its panel size, the live popup must
    // keep the stream's source resolution.
    // startLevel: 0 = the LOWEST manifest level — hls.js 1.6.2 sorts levels
    // ascending (dist/hls.mjs: "sort levels from lowest to highest"), so
    // index 0 is the smallest fragment → fastest first frame. MANIFEST_PARSED
    // then pins currentLevel to the quality policy (SOURCE alone, ≤480p ladder
    // with multiple open players) — see applyQualityPolicy.
    const hls = new Hls({
      ...twitchAdBlockHlsConfig({ live: true, onAdRotation }),
      enableWorker: true,
      startLevel: 0,
      lowLatencyMode: true,
      // Live latency target: ONE segment behind the live edge
      // (liveSyncDurationCount 1) — the ~2-5s band the official Kick/Twitch
      // pages run. lowLatencyMode also enables LL-HLS part handling; the
      // backend prefers Twitch LL masters and non-LL playlists play
      // identically with it on. hls.js THROWS when count and duration sync
      // variants are mixed, so the count knobs are the only live-sync
      // geometry here — computeLiveEdgeSec mirrors hls.js's targetLatency
      // (count × level targetduration) to keep the edge math exact.
      // maxLiveSyncPlaybackRate 1.5 recovers from any drift by playing up to
      // 1.5× instead of stalling; liveMaxLatencyDurationCount 6 (≈12s at 2s
      // segments) is the force-resync ceiling for slow networks. maxBufferLength
      // 20 keeps the buffer deep so the tighter target does not reintroduce
      // the old 3s-target rebuffer flash (feat/live-buffering).
      maxBufferLength: 20,
      maxMaxBufferLength: 40,
      // Retained back-buffer = the arrow-seek window: LIVE without a DVR
      // archive still lets ArrowLeft/Right rewind ~30s into the stream (the
      // rail stays disabled — see the keydown listener below).
      backBufferLength: LIVE_BACK_BUFFER_SEC,
      startFragPrefetch: true,
      fragLoadingTimeOut: 20000,
      manifestLoadingTimeOut: 10000,
      testBandwidth: false,
      liveSyncDurationCount: 1,
      // hls.js REQUIRES liveMaxLatencyDurationCount > liveSyncDurationCount
      // (config validation throws otherwise) — 6 ≈ 12s at 2s segments.
      liveMaxLatencyDurationCount: 6,
      // twitchAdBlockHlsConfig injects the DURATION variants (liveSyncDuration
      // 3 / liveMaxLatencyDuration 10) for the mini preview; hls.js throws on
      // mixing count + duration variants, so null them — the count knobs
      // above are the popup's only live-sync geometry.
      liveSyncDuration: undefined,
      liveMaxLatencyDuration: undefined,
      // liveDurationInfinity false → the browser sees a FINITE live timeline
      // (duration = buffered end) instead of Infinity, so setting currentTime
      // can seek back into the retained back-buffer above.
      liveDurationInfinity: false,
      maxLiveSyncPlaybackRate: 1.5,
      startPosition: -1,
      autoStartLoad: !replay,
    });
    hlsRef.current = hls;
    // e2e probe hook (same convention as window.__vodripAdSegmentsStripped).
    (window as unknown as { __livePopupHls?: Hls }).__livePopupHls = hls;
    hls.loadSource(src);
    hls.attachMedia(video);
    let networkRetries = 0;

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (modeRef.current === 'live') {
        // Quality: keep ORIGINAL hls.levels indices. Twitch/Kick live go up
        // to source (highest manifest level — may exceed 1080p); YouTube live
        // offers exactly the policy ladder 360/720/1080, or 360-only when the
        // session is anonymous (no user cookies).
        // The MENU offers the filterLiveLevels set (YouTube allowHeights
        // policy, 360 floor) while PLAYBACK pins the open-player-count policy:
        // one popup → SOURCE (highest bitrate), several → ≤480p ladder
        // (see qualityLevelForPolicy). startLevel: 0 already picked the lowest
        // level for the first fragment; the policy is a fixed currentLevel
        // (autoLevelEnabled off) from here on.
        const isYoutube = sessionPlatformRef.current === 'youtube';
        const { levels: filtered } = filterLiveLevels(
          hls.levels.map((l, i) => ({
            index: i,
            height: l.height || 0,
            bitrate: l.bitrate || 0,
          })),
          isYoutube
            ? { allowHeights: sessionRef.current?.anonymous ? [360] : [360, 720, 1080] }
            : undefined,
        );
        setLevels(filtered.map((l) => ({
          index: l.index,
          // height 0 = the lone source-level media playlist (Twitch usher
          // media playlists carry no RESOLUTION) — label it Auto like the
          // mini preview's previewLevelLabel rather than "0p".
          label: l.height > 0 ? `${l.height}p` : 'Auto',
          height: l.height,
        })));
        applyQualityPolicy();
      } else {
        // REPLAY: single-level ENDLIST snapshot — hls.js reports the duration.
        if (Number.isFinite(video.duration)) {
          setSnapshotDuration(video.duration);
          // Keep the LIVE-mode rail length fresh (the archive grows while the
          // broadcast runs) — same value the backend sums server-side.
          if (video.duration > 0) setArchiveDuration(video.duration);
        }
        if (startPos >= 0) {
          hls.stopLoad();
          hls.startLoad(startPos);
        }
      }
      setLoading(false);
      clearRetry();
    });

    hls.on(Hls.Events.LEVEL_SWITCHED, (_e, data) => {
      // Guard -1 (auto): ABR switches report -1 and would highlight nothing.
      if (modeRef.current === 'replay') return;
      if (typeof data?.level === 'number' && data.level >= 0) setCurrentLevel(data.level);
    });

    hls.on(Hls.Events.ERROR, (_e, data) => {
      // Stall guard (live only): hls.js 1.6.2 reports a stall as
      // BUFFER_STALLED_ERROR ("stall detected" — non-fatal once per stall
      // period, fatal after nudges fail) and breaks hard as a fatal
      // NETWORK_ERROR. Either, with another live entry available, jumps to
      // the next entry instead of waiting out the backend stall. Replay/rail
      // is untouched — only LIVE mode qualifies.
      const liveStall = modeRef.current === 'live' && (
        data?.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR ||
        (data?.type === Hls.ErrorTypes.NETWORK_ERROR && data.fatal === true)
      );
      if (liveStall && tryAdvanceEntry()) return;
      if (!data?.fatal) return;
      // Same retry wiring as the mini preview player: bounded network retries
      // (resume near the current position), media error recovery, then error.
      switch (data.type) {
        case Hls.ErrorTypes.NETWORK_ERROR:
          // Invisible session recreate FIRST (fix D): a fatal NETWORK_ERROR on
          // a live stream is usually a stale session (expired usher/master
          // token) that startLoad cannot fix — one delete+re-POST recovers.
          // Only after that do the bounded startLoad retries run.
          if (modeRef.current === 'live' && !silentRecreateDoneRef.current) {
            silentRecreateDoneRef.current = true;
            recreateSessionInvisible();
            break;
          }
          if (networkRetries < 4) {
            networkRetries += 1;
            window.setTimeout(() => {
              if (hlsRef.current !== hls) return;
              const t = videoRef.current?.currentTime;
              hls.startLoad(t && t > 0 ? t : -1);
            }, networkRetries * 1000);
            break;
          }
          setError(t('Live playback failed — try again'));
          setLoading(false);
          break;
        case Hls.ErrorTypes.MEDIA_ERROR:
          hls.recoverMediaError();
          break;
        default:
          setError(t('Live playback failed — try again'));
          setLoading(false);
          break;
      }
    });

    if (startPos >= 0 && modeRef.current !== 'replay') hls.startLoad(startPos);
    else if (modeRef.current !== 'replay') hls.startLoad();
    return hls;
  }, [destroyHls, onAdRotation, clearRetry, markPreviewError, tryAdvanceEntry, recreateSessionInvisible, applyQualityPolicy]);

  // Cleanup player on unmount
  const cleanup = useCallback(() => {
    if (clipCooldownTimerRef.current != null) {
      window.clearTimeout(clipCooldownTimerRef.current);
      clipCooldownTimerRef.current = null;
    }
    if (clipNoticeTimerRef.current != null) {
      window.clearTimeout(clipNoticeTimerRef.current);
      clipNoticeTimerRef.current = null;
    }
    if (controlsHideTimerRef.current != null) {
      window.clearTimeout(controlsHideTimerRef.current);
      controlsHideTimerRef.current = null;
    }
    abortRef.abort();
    destroyHls();
    const sid = sessionIdRef.current;
    if (sid) {
      apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      sessionIdRef.current = null;
    }
    sessionRef.current = null;
    if (videoRef.current) {
      videoRef.current.src = '';
      videoRef.current.load();
    }
  }, [abortRef, destroyHls]);

  // Close handler
  const handleClose = useCallback(() => {
    cleanup();
    onClose();
  }, [cleanup, onClose]);

  /** RETRY this live stream only — delete any stale session and re-run the
   *  mount effect end-to-end (live has a single stage: create + attach). */
  const retryPreview = useCallback(() => {
    const ctx = previewRetryRef.current;
    if (!ctx) return;
    setError(null);
    previewRetryingRef.current = true;
    const sid = sessionIdRef.current;
    if (sid) {
      void apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      sessionIdRef.current = null;
    }
    setRetryTick((t) => t + 1);
  }, []);

  // Create preview session on mount — one run per entry: advancing a fallback
  // changes activeEntry.url, which re-runs this effect; cleanup aborts the
  // in-flight POST and destroys the hls instance, so the switch never leaks.
  useEffect(() => {
    let cancelled = false;
    // Start the hls.js chunk load in parallel with the session POST — the
    // ~900KB dynamic import used to sit on the playback critical path
    // (App.tsx also preloads it when the Channels tab renders; this covers
    // any other open path and makes the two fetches overlap instead of
    // chaining).
    void import('hls.js').catch(() => {});
    // Stall guard: abort the session POST after 8s — a hung backend must not
    // pin the spinner (apiPost's own budget is 60s x 3 retries). The abort
    // rejects as AbortError; the catch advances to the next entry if one
    // exists, else surfaces the error (with the retry button) as today.
    const controller = new AbortController();
    const stallTimer = window.setTimeout(() => controller.abort(), SESSION_STALL_MS);

    (async () => {
      try {
        if (invisibleRecreateRef.current) {
          // Invisible retry: keep the current frame under the buffering
          // overlay — no loading swap for the one automatic recreate.
          invisibleRecreateRef.current = false;
        } else {
          setLoading(true);
        }
        setError(null);
        const body: {
          url: string;
          is_live: boolean;
          headers?: Record<string, string>;
          platform?: string;
          vod_url?: string;
        } = { url: activeEntry.url, is_live: true };
        if (activeEntry.headers) body.headers = activeEntry.headers;
        if (activeEntry.platform) body.platform = activeEntry.platform;
        if (vodUrl) body.vod_url = vodUrl;

        const res = await apiPost<PreviewSessionResponse>('/api/preview/live', body, { signal: controller.signal });
        if (cancelled) {
          // StrictMode double-mount (dev) / entry switch: this mount is gone
          // but the POST completed — delete the orphan session instead of
          // leaking it for its 30min TTL.
          if (res?.session_id) apiDelete(`/api/preview/session/${res.session_id}`).catch(() => {});
          return;
        }
        window.clearTimeout(stallTimer);
        if (!res) {
          if (tryAdvanceEntry()) return;
          setError(t('No response from server'));
          setLoading(false);
          markPreviewError();
          return;
        }

        sessionIdRef.current = res.session_id;
        sessionRef.current = res;
        sessionPlatformRef.current = (activeEntry.platform || '').toLowerCase();
        setArchiveDuration(res.archive_duration ?? 0);

        // Warm the LIVE rail: the backend resolves the DVR archive lazily on
        // this snapshot request (off the playback critical path), so fetch it
        // unconditionally and sum its EXTINF durations — the rail then spans
        // the whole broadcast from the start instead of sitting at 1s until
        // the first REPLAY switch. No archive → the request 404s and the rail
        // stays off (same as a live stream with no DVR).
        fetch(replaySnapshotUrl(res.session_id))
          .then((r) => (r.ok ? r.text() : ''))
          .then((text) => {
            if (cancelled) return;
            const total = parsePlaylistTotalSec(text);
            if (total > 0) {
              setArchiveDuration(total);
              archiveReadyRef.current = true;
              setArchiveReady(true);
            }
          })
          .catch(() => {});

        // Attach player to video element
        const video = videoRef.current;
        if (!video) { setLoading(false); return; }

        const src = res.master_url || res.playback_url;
        // Live sessions return playback_url == master_url (both .m3u8) — decide by kind, not presence.
        const isHls = res.kind === 'hls' || !!src && src.includes('.m3u8');
        if (src && !isHls) {
          // Progressive stream
          video.src = src;
          video.addEventListener('loadedmetadata', () => { setLoading(false); clearRetry(); }, { once: true });
          video.play().catch(() => {});
        } else if (src) {
          await createHlsPlayer(src, -1);
        } else {
          setLoading(false);
        }

        video.play().catch(() => {});
      } catch (err: unknown) {
        if (!cancelled) {
          if (tryAdvanceEntry()) return;
          const stalled = err instanceof DOMException && err.name === 'AbortError';
          setError(stalled
            ? t('Live session is taking too long to start')
            : (err instanceof Error ? err.message : t('Failed to start live stream')));
          setLoading(false);
          markPreviewError();
        }
      }
    })();

    return () => {
      cancelled = true;
      window.clearTimeout(stallTimer);
      controller.abort();
      destroyHls();
    };
  }, [activeEntry.url, activeEntry.headers, activeEntry.platform, vodUrl, abortRef, createHlsPlayer, destroyHls, retryTick, markPreviewError, clearRetry, tryAdvanceEntry]);

  // Sync transport state from the video element
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onPlay = () => { setPaused(false); userPausedRef.current = false; };
    const onPause = () => {
      setPaused(true);
      // Not user-initiated (live-sync seek interrupted an autoplay promise
      // while buffering, e.g. in a background tab) — resume once data is
      // available, bounded retries.
      if (!userPausedRef.current && hlsRef.current && !video.ended) {
        const resume = (attempt: number) => {
          const v = videoRef.current;
          if (!v || !hlsRef.current || v.ended || userPausedRef.current) return;
          if (v.paused) {
            void v.play().catch(() => {
              if (attempt < 2) window.setTimeout(() => resume(attempt + 1), 400);
            });
          }
        };
        window.setTimeout(() => resume(0), 250);
      }
    };
    const onVolumeChange = () => { setMuted(video.muted); setVolume(video.volume); };
    const onTime = () => setRailTime(video.currentTime);
    const onDuration = () => {
      if (modeRef.current === 'replay' && Number.isFinite(video.duration)) {
        setSnapshotDuration(video.duration);
      }
    };
    // First-frame timing marker — logs ms from popup open to the first
    // decodable frame (readyState >= 3, unpaused, past t=0). One-shot; the
    // `playing`/`timeupdate` pair covers both the initial start and resume
    // paths without polling.
    const onFirstFrame = () => {
      if (firstFrameLoggedRef.current) return;
      const v = videoRef.current;
      if (!v || v.readyState < 3 || v.paused || !(v.currentTime > 0)) return;
      firstFrameLoggedRef.current = true;
      console.info('[live] first-frame', Math.round(performance.now() - firstFrameStartRef.current));
    };
    video.addEventListener('play', onPlay);
    video.addEventListener('pause', onPause);
    video.addEventListener('volumechange', onVolumeChange);
    video.addEventListener('timeupdate', onTime);
    video.addEventListener('durationchange', onDuration);
    video.addEventListener('waiting', showBuffering);
    video.addEventListener('playing', clearBuffering);
    video.addEventListener('playing', onFirstFrame);
    video.addEventListener('timeupdate', onFirstFrame);
    video.addEventListener('canplay', clearBuffering);
    return () => {
      video.removeEventListener('play', onPlay);
      video.removeEventListener('pause', onPause);
      video.removeEventListener('volumechange', onVolumeChange);
      video.removeEventListener('timeupdate', onTime);
      video.removeEventListener('durationchange', onDuration);
      video.removeEventListener('waiting', showBuffering);
      video.removeEventListener('playing', clearBuffering);
      video.removeEventListener('playing', onFirstFrame);
      video.removeEventListener('timeupdate', onFirstFrame);
      video.removeEventListener('canplay', clearBuffering);
    };
  }, [showBuffering, clearBuffering]);

  // Track fullscreen state — element-equality like App.tsx and
  // ChannelExplorePopup: this popup only claims fullscreen when IT is the
  // fullscreen element. The old `!!document.fullscreenElement` made the icon
  // and toggle lie whenever the main preview (or another popup) was
  // fullscreen: the button showed "Exit" and clicking exited THAT surface.
  useEffect(() => {
    const onFsChange = () => {
      fsGateRef.current?.sync();
      setIsFullscreen(document.fullscreenElement === popupRef.current);
    };
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);

  // --- Transport handlers ---
  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      userPausedRef.current = false;
      noteUserUnpause();
      video.play().catch(() => {});
    } else {
      userPausedRef.current = true;
      video.pause();
    }
  }, []);

  const toggleMute = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !video.muted;
    setMuted(video.muted);
    if (!video.muted && video.paused) video.play().catch(() => {});
  }, []);

  const onVolumeChange = useCallback((next: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.volume = next;
    video.muted = next === 0;
    setVolume(next);
    setMuted(next === 0);
    if (next > 0 && video.paused) video.play().catch(() => {});
  }, []);

  const snapToLiveEdge = useCallback(() => {
    const hls = hlsRef.current;
    const video = videoRef.current;
    if (!video) return;
    // hls.js 1.6.2 exposes the edge as a getter (seekToLiveEdge was removed).
    const livePos = hls?.liveSyncPosition;
    if (typeof livePos === 'number' && isFinite(livePos)) {
      video.currentTime = livePos;
      return;
    }
    try {
      const s = video.seekable;
      if (s && s.length > 0) video.currentTime = s.end(s.length - 1);
    } catch {
      // native-HLS live edge can be behind the seekable end on some platforms
    }
  }, []);

  // --- DVR mode switching ---
  const switchToReplay = useCallback((sec: number) => {
    if (pendingReplaySeekRef.current != null) {
      window.clearTimeout(pendingReplaySeekRef.current);
      pendingReplaySeekRef.current = null;
    }
    const sid = sessionIdRef.current;
    const video = videoRef.current;
    if (!sid || !video || !archiveReadyRef.current) return;
    // Set the ref synchronously — createHlsPlayer reads modeRef.current and
    // the state effect would not have flushed by the time it runs.
    modeRef.current = 'replay';
    setMode('replay');
    setLoading(true);
    setError(null);
    void createHlsPlayer(replaySnapshotUrl(sid), Math.max(0, sec));
    video.play().catch(() => {});
    // Re-snapshot while parked so the rail keeps growing with the broadcast.
    if (replayTimerRef.current != null) window.clearInterval(replayTimerRef.current);
    replayTimerRef.current = window.setInterval(() => {
      const v = videoRef.current;
      if (!v || modeRef.current !== 'replay') return;
      // A user seek is in flight (250ms debounce) — skip this tick or the
      // resnapshot recreate would win the race against the seek's instance.
      if (pendingReplaySeekRef.current != null) return;
      // ponytail: reloadWindowHlsAtPosition patches the level URL in-place — on
      // an ended snapshot the buffered timeline is already full, no new frag
      // buffers, and FRAG_BUFFERED never fires (45s hang). A full re-create
      // (same path as drag-past-edge) deterministically re-syncs duration.
      switchToReplay(Math.max(0, v.currentTime));
    }, REPLAY_RESNAPSHOT_MS);
  }, [createHlsPlayer, replaySnapshotUrl]);

  const switchToLive = useCallback(() => {
    if (pendingReplaySeekRef.current != null) {
      window.clearTimeout(pendingReplaySeekRef.current);
      pendingReplaySeekRef.current = null;
    }
    const sid = sessionIdRef.current;
    const video = videoRef.current;
    const sess = sessionRef.current;
    if (!sid || !video || !sess) return;
    modeRef.current = 'live';
    setMode('live');
    setLoading(true);
    setError(null);
    const src = sess.master_url || sess.playback_url;
    if (!src) { setLoading(false); return; }
    void createHlsPlayer(src, -1).then((hls) => {
      if (hls && modeRef.current === 'live') {
        const livePos = hls.liveSyncPosition;
        if (typeof livePos === 'number' && isFinite(livePos)) video.currentTime = livePos;
      }
    });
    video.play().catch(() => {});
  }, [createHlsPlayer]);

  /**
   * DVR seek (rail drag) into a past part of the stream: debounce so a drag
   * does not recreate the hls player on every mousemove, then restart the
   * ENDLIST snapshot load at the target — the smallest robust fast-seek: the
   * snapshot timeline is VOD, hls.js lands on the fragment containing the
   * target (Twitch segments start with keyframes) and only a small buffer
   * (maxBufferLength 6) is fetched before playback resumes.
   */
  const scheduleReplaySwitch = useCallback((sec: number) => {
    if (pendingReplaySeekRef.current != null) {
      window.clearTimeout(pendingReplaySeekRef.current);
    }
    pendingReplaySeekRef.current = window.setTimeout(() => {
      pendingReplaySeekRef.current = null;
      switchToReplay(sec);
    }, 250);
  }, [switchToReplay]);

  const handleRailChange = useCallback((next: number) => {
    if (mode === 'live') {
      // Dragging back in LIVE mode switches to REPLAY at the dragged position.
      scheduleReplaySwitch(next);
      return;
    }
    const video = videoRef.current;
    const { inSnapshot } = replaySeekTarget(next, snapshotDuration);
    if (inSnapshot && video) {
      video.currentTime = Math.max(0, next);
    } else {
      scheduleReplaySwitch(next); // past the snapshot edge — re-snapshot and land there
    }
  }, [mode, snapshotDuration, scheduleReplaySwitch]);

  // Element-equality, matching App.tsx / ChannelExplorePopup (the gate's
  // default `(active, el) => active === el`). The old `active != null` exited
  // ANY element's fullscreen: with the main preview fullscreen, the popup's
  // button exited the PREVIEW instead of entering its own fullscreen — the
  // "second click acts like a different fullscreen type" off-by-one.
  const fsGateRef = useRef<FullscreenGate | null>(null);
  if (fsGateRef.current === null) {
    fsGateRef.current = createFullscreenGate(nativeFullscreenAdapter);
  }

  const toggleFullscreen = useCallback(() => {
    const el = popupRef.current;
    if (!el) return;
    fsGateRef.current?.toggle(el);
  }, []);

  // --- Live chat dock toggle ---
  const toggleChat = useCallback(() => {
    const next = !chatOpenRef.current;
    chatOpenRef.current = next;
    setChatOpen(next);
    // Keep the VIDEO at its current width: growing/shrinking the popup by
    // the chat panel width, never stealing pixels from the video area.
    const s = sizeRef.current;
    const maxW = Math.min(window.innerWidth - RESIZE_MARGIN, LIVE_PANEL_MAX_W);
    const w = next
      ? Math.min(maxW, s.w + LIVE_CHAT_PANEL_W)
      : Math.max(LIVE_PANEL_MIN_W, s.w - LIVE_CHAT_PANEL_W);
    if (w !== s.w) {
      // Video width unchanged → aspect height unchanged (aspect-lock holds).
      setSize({ w, h: s.h });
    }
  }, []);

  // --- Fast CLIP (5s cooldown, seconds input, honest capability report) ---
  const showClipNotice = useCallback((msg: string) => {
    setClipNotice(msg);
    if (clipNoticeTimerRef.current != null) window.clearTimeout(clipNoticeTimerRef.current);
    clipNoticeTimerRef.current = window.setTimeout(() => setClipNotice(null), 3500);
  }, []);

  const handleFastClip = useCallback(async () => {
    const now = Date.now();
    if (clipCooldownRemaining(lastClipAtRef.current, now) > 0) return; // 2nd click within 5s ignored
    lastClipAtRef.current = now;
    const platform = (activeEntry.platform || '').toLowerCase();
    const slug = liveChatSlugFromUrl(activeEntry.url, platform) ?? channelSlug ?? '';
    const durationSec = clampClipSeconds(clipSeconds);
    reportClipEvent('live_clip_clicked', { platform, slug, durationSec });
    // Countdown ticker on the button (250ms steps).
    const tick = () => {
      const left = clipCooldownRemaining(lastClipAtRef.current, Date.now(), FAST_CLIP_COOLDOWN_MS);
      setClipCooldownLeft(left);
      if (left > 0) {
        clipCooldownTimerRef.current = window.setTimeout(tick, 250);
      } else {
        clipCooldownTimerRef.current = null;
      }
    };
    if (clipCooldownTimerRef.current != null) window.clearTimeout(clipCooldownTimerRef.current);
    tick();
    if (platform === 'twitch') {
      // Twitch live clips run in Twitch's own browser editor (player Clip
      // button) driven by the cookie extension — no Helix/OAuth server path
      // (audited; backend live.py has no clip mutation). Open the editor and
      // let the extension fill + publish with the session cookie.
      try {
        const title = (activeEntry.title || '').trim();
        openTwitchLiveClipEditorInBrowser(slug, durationSec, title);
        reportClipEvent('live_clip_editor_open', { platform, slug, durationSec, title });
        showClipNotice(t('Opening Twitch clip editor…'));
      } catch (err) {
        showClipNotice(`${t('Clip unavailable')}: ${err instanceof Error ? err.message : ''}`);
      }
      return;
    }
    try {
      const res = await apiPost<LiveClipCapability>('/api/live/clip', {
        platform,
        slug,
        duration_sec: durationSec,
      });
      // The backend never fabricates a clip: it reports what it CAN do. When
      // a server-side path arrives, `available` flips and this shows success.
      showClipNotice(res.available
        ? t('Clip created')
        : `${t('Clip unavailable')}: ${t(res.reason ?? '')}`);
    } catch (err) {
      showClipNotice(`${t('Clip unavailable')}: ${err instanceof Error ? err.message : ''}`);
    }
  }, [activeEntry.url, activeEntry.platform, activeEntry.title, channelSlug, clipSeconds, showClipNotice, t]);

  // --- Resize: aspect-locked (video keeps the stream's aspect; chat docks
  //     right of the video, so the video area = popup − chat panel width) ---
  const handleResize = useCallback((e: React.PointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const startSize = { ...sizeRef.current };
    const startPos = { ...posRef.current };
    const viewport = { w: window.innerWidth, h: window.innerHeight };
    const headerH = headerRef.current?.offsetHeight ?? POPUP_HEADER_EST;
    const chatW = chatOpenRef.current ? LIVE_CHAT_PANEL_W : 0;
    const maxW = Math.min(viewport.w - RESIZE_MARGIN, LIVE_PANEL_MAX_W);
    const maxH = Math.min(viewport.h - RESIZE_MARGIN, LIVE_PANEL_MAX_H);
    const clamp: LivePanelAspectClamp = {
      minW: LIVE_PANEL_MIN_W,
      minH: LIVE_PANEL_MIN_H,
      maxW,
      maxH,
    };
    const applyPos = (next: PanelSize) => {
      const p = panelPosAfterResize(edge, startPos, startSize, next, viewport);
      posRef.current = p;
      setPosition(p);
    };
    startPanelResizeDrag(e, edge, sizeRef, setSize, {
      panelEl: popupRef.current,
      maxW,
      maxH,
      clampSize: (s) =>
        livePanelSizeFromAspect(edge, startSize, s, videoAspectRef.current, headerH, chatW, clamp),
      onResizeMove: (next) => applyPos(next),
      onResizeEnd: () => applyPos(sizeRef.current),
    });
  }, []);

  // --- Dragging (header bar only, so transport buttons don't fight the drag) ---
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest('.live-popup-close') || t.closest('.live-popup-link') || t.closest('.live-popup-search')) return;
    setDrag({ startX: e.clientX, startY: e.clientY, offsetX: posRef.current.x, offsetY: posRef.current.y });
  }, []);

  useEffect(() => {
    if (!drag) return;
    const handleMouseMove = (e: MouseEvent) => {
      const s = sizeRef.current;
      setPosition({
        x: Math.max(0, Math.min(window.innerWidth - s.w, drag.offsetX + e.clientX - drag.startX)),
        y: Math.max(0, Math.min(window.innerHeight - s.h, drag.offsetY + e.clientY - drag.startY)),
      });
    };
    const handleMouseUp = () => setDrag(null);
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [drag]);

  // Transport buttons match the main preview player (platform accent when
  // docked, glass when the popup is fullscreen).
  const ctrlPlatform = (activeEntry.platform ?? 'kick') as PlatformStyleKey;
  const transportBtn = platformPreviewCtrlBtn(ctrlPlatform, isFullscreen, false);

  const archiveAvailable = archiveReady;  // Live rail: pinned at the archive edge; dragging back opens REPLAY.
  // Replay rail: full snapshot duration; max follows hls.js video.duration.
  const railMax = mode === 'live'
    ? (archiveDuration > 0 ? archiveDuration : 1)
    : (snapshotDuration > 0 ? snapshotDuration : 1);
  const railDisabled = mode === 'live' && !archiveAvailable;
  const railValue = mode === 'replay'
    ? Math.min(Math.max(0, railTime), railMax)
    : railMax;

  // Live edge on the player timeline (liveSyncPosition is real stream time;
  // add the configured sync lag for the true edge). Falls back to the
  // seekable end. Used to map currentTime to broadcast-relative seconds AND
  // to clamp the back-buffer arrow seek (no-archive live) below.
  // The sync lag mirrors hls.js's LiveSyncController.targetLatency: hls.js
  // writes the computed target into config.liveSyncDuration at runtime; until
  // then (and for the count-based config) it is liveSyncDurationCount × the
  // level's targetduration (2s default = one 2s segment).
  const computeLiveEdgeSec = useCallback((): number => {
    const h = hlsRef.current;
    if (h) {
      const pos = typeof h.liveSyncPosition === 'number' ? h.liveSyncPosition : Number.NaN;
      if (Number.isFinite(pos) && pos > 0) {
        const c = h.config;
        let lag = typeof c.liveSyncDuration === 'number' && Number.isFinite(c.liveSyncDuration)
          ? c.liveSyncDuration
          : Number.NaN;
        if (!Number.isFinite(lag)) {
          const count = typeof c.liveSyncDurationCount === 'number' ? c.liveSyncDurationCount : 3;
          const level = h.levels?.[h.loadLevel];
          const td = level?.details?.targetduration;
          lag = count * (typeof td === 'number' && td > 0 ? td : 2);
        }
        return pos + lag;
      }
    }
    const v = videoRef.current;
    const s = v?.seekable;
    return s && s.length > 0 ? s.end(s.length - 1) : 0;
  }, []);
  const liveEdgeSec = computeLiveEdgeSec();
  // Timestamps (current / total) — ticking from timeupdate. LIVE totals the
  // growing archive (broadcast duration); REPLAY totals the snapshot.
  const totalSec = mode === 'replay'
    ? (snapshotDuration > 0 ? snapshotDuration : archiveDuration)
    : (archiveDuration > 0 ? archiveDuration : liveEdgeSec);
  const currentSec = mode === 'replay'
    ? railTime
    : liveBroadcastPositionSec(archiveDuration, liveEdgeSec, railTime);
  // The live edge lags the playhead between playlist refreshes — never show
  // a total smaller than the current position.
  const displayTotal = Math.max(totalSec, currentSec);

  // Zoomed rail window — zoom=1 → the full 0..railMax (pixel-identical to
  // the unzoomed rail); otherwise the window anchored at railAnchorFrac.
  const railView: TrimViewWindow = useMemo(
    () => zoomWindowFromView(railZoom, railAnchorFrac, railMax),
    [railZoom, railAnchorFrac, railMax],
  );

  // Clamp the zoom when the rail duration shrinks (new session / mode
  // switch): a window wider than the new rail is meaningless — fall back to
  // the full view (zoom 1, anchor reset).
  useEffect(() => {
    const max = maxTrimZoomForDuration(railMax);
    if (railZoom > max) {
      setRailZoom(1);
      setRailAnchorFrac(0.5);
    }
  }, [railZoom, railMax]);

  // Wheel-to-zoom on the replay rail. React's synthetic onWheel is passive at
  // the root, so preventDefault would not stop page scroll — attach a native
  // non-passive listener instead (same pattern as App.tsx's preview trim
  // rail, reattached when the view/window changes).
  useEffect(() => {
    const rail = railRef.current;
    if (!rail || railDisabled || railMax <= 0) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = rail.getBoundingClientRect();
      // Cursor fraction: the pointer position when it is over the rail,
      // else the current rail position (zoom around the playhead).
      const cursorFrac = rect.width > 0 && e.clientX >= rect.left && e.clientX <= rect.right
        ? Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
        : secToFrac(
            modeRef.current === 'replay'
              ? Math.min(Math.max(0, railTimeRef.current), railMax)
              : railMax,
            railView,
          );
      const factor = e.deltaY < 0 ? TRIM_ZOOM_STEP : 1 / TRIM_ZOOM_STEP;
      // zoomTrimViewAround clamps the result so the window stays inside
      // [0, railMax] and never narrower than TRIM_MIN_WINDOW_SEC.
      const next = zoomTrimViewAround(railView, cursorFrac, factor, railMax);
      setRailZoom(next.zoom);
      setRailAnchorFrac(next.anchorFrac);
    };
    rail.addEventListener('wheel', onWheel, { passive: false });
    return () => rail.removeEventListener('wheel', onWheel);
  }, [railDisabled, railMax, railView]);

  // ArrowLeft/ArrowRight ±PREVIEW_KEY_SKIP_SEC seek on the live popup.
  // Window-level so the rail needs no focus; Space/ArrowUp/ArrowDown/F stay
  // with the popup's buttons. railTime is read via a ref so the listener
  // does not reattach on every timeupdate. WITHOUT an archive the rail stays
  // disabled/undraggable (user asked for KEYBOARD seek only), so the arrows
  // instead seek inside the retained live back-buffer — hls.js retains
  // LIVE_BACK_BUFFER_SEC behind the playhead (backBufferLength 30) and the
  // finite live timeline (liveDurationInfinity false) makes currentTime
  // seeks land in it. The listener is window-level, so it keeps working
  // while the auto-hidden controls are pointer-events-none.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (shouldIgnorePlayerKeyEvent(e as unknown as React.KeyboardEvent)) return;
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      e.stopPropagation();
      if (!railDisabled) {
        if (e.key === 'ArrowLeft') {
          handleRailChange(Math.max(0, railTimeRef.current - PREVIEW_KEY_SKIP_SEC));
        } else {
          handleRailChange(Math.min(railMax, railTimeRef.current + PREVIEW_KEY_SKIP_SEC));
        }
        return;
      }
      // LIVE without archive — seek within the retained back-buffer, clamped
      // to [max(0, edge − 30), edge − 0.75] so the playhead never lands on
      // (or past) the live edge. No-op while the session is still loading or
      // before the edge is known (edgeSec 0).
      if (loading) return;
      const video = videoRef.current;
      const edgeSec = computeLiveEdgeSec();
      if (!video || edgeSec <= 0) return;
      const lo = Math.max(0, edgeSec - LIVE_BACK_BUFFER_SEC);
      const hi = edgeSec - LIVE_EDGE_SEEK_SAFETY_SEC;
      if (hi <= lo) return;
      const delta = e.key === 'ArrowLeft' ? -PREVIEW_KEY_SKIP_SEC : PREVIEW_KEY_SKIP_SEC;
      video.currentTime = Math.min(hi, Math.max(lo, video.currentTime + delta));
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [railDisabled, railMax, handleRailChange, computeLiveEdgeSec, loading]);

  return createPortal(
    <div
      ref={popupRef}
      tabIndex={-1}
      className="group border-2 border-zinc-700 bg-zinc-950"
      data-live-popup
      onPointerDownCapture={onBringToFront}
      // Inactivity auto-hide: any interaction inside the popup (mouse move,
      // click/touch, key — the popup root holds focus, so keydown from a
      // focused descendant bubbles here) bumps the controls back up.
      onMouseMove={bumpControls}
      onPointerDown={bumpControls}
      onTouchStart={bumpControls}
      onKeyDown={bumpControls}
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        width: size.w,
        height: size.h,
        // Rank from the shared floating-player ladder (App assigns at open and
        // re-assigns on pointer-down). Omitted → classic active-state z: above
        // the floating archive search (SEARCH_POPUP_Z); unmount on close
        // restores order.
        zIndex: zIndex ?? LIVE_POPUP_ACTIVE_Z,
        boxShadow: '6px 6px 0px 0px rgba(9,9,11,0.9)',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <PanelResizeHandles onPointerDown={handleResize} />

      {/* Header bar — drag handle, same chrome as the mini preview player
          (eyebrow label + title + LIVE badge, labeled SEARCH ARCHIVE button,
          close X). */}
      <div
        ref={headerRef}
        data-live-header
        onMouseDown={handleMouseDown}
        className={`flex items-start justify-between gap-2 px-2 py-1.5 bg-zinc-900 border-b-2 border-zinc-800 select-none shrink-0 transition-opacity duration-300 ${
          controlsHidden ? 'opacity-0 pointer-events-none' : 'opacity-100'
        }`}
        style={{ cursor: drag ? 'grabbing' : 'grab' }}
      >
        <div className="flex items-start gap-1.5 min-w-0">
          <div className="min-w-0">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
              {mode === 'live' ? t('Live stream') : t('Live replay')}
            </span>
            <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
              {channelName}
              {activeEntry.title ? ` — ${activeEntry.title}` : ''}
            </p>
            {mode === 'live' ? (
              <span className="inline-flex items-center gap-1 mt-0.5">
                <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
                <span className="text-[10px] font-bold text-red-400">LIVE</span>
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 mt-0.5">
                <span className="h-1.5 w-1.5 rounded-full bg-zinc-500" />
                <span className="text-[10px] font-bold text-zinc-400">REPLAY</span>
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {channelUrl && (
            <button
              className="live-popup-link text-zinc-500 hover:text-white p-1 shrink-0"
              onClick={() => window.open(channelUrl, '_blank', 'noopener,noreferrer')}
              title={t('Open channel')}
            >
              <ExternalLink size={13} />
            </button>
          )}
          {/* Search affordance is fullscreen-hostile (the docked panel covers
              the video) — remove it while fullscreen, like ChannelExplorePopup
              hides its header. State survives: the panel reopens on exit. */}
          {!isFullscreen && (
            <button
              type="button"
              onClick={() => setSearchOpen((o) => !o)}
              aria-pressed={searchOpen}
              className={`live-popup-search flex items-center gap-1 border-2 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                searchOpen
                  ? 'bg-white text-black border-white'
                  : 'border-zinc-700 bg-zinc-800/60 text-zinc-300 hover:border-white hover:text-white'
              }`}
              title={t('Search the local archive (transcripts + chat)')}
            >
              <Search size={10} className="shrink-0" />
              {searchOpen ? t('CLOSE SEARCH') : t('SEARCH ARCHIVE')}
            </button>
          )}
          {/* Live chat dock toggle — same chrome language as SEARCH ARCHIVE. */}
          <button
            type="button"
            onClick={toggleChat}
            aria-pressed={chatOpen}
            className={`live-popup-chat flex items-center gap-1 border-2 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
              chatOpen
                ? 'bg-white text-black border-white'
                : 'border-zinc-700 bg-zinc-800/60 text-zinc-300 hover:border-white hover:text-white'
            }`}
            title={chatOpen ? t('Close live chat') : t('Live chat')}
          >
            <MessageSquare size={10} className="shrink-0" />
            {chatOpen ? t('Close live chat') : t('Live chat')}
          </button>
          <button
            className="live-popup-close text-zinc-500 hover:text-white p-1 shrink-0"
            onClick={handleClose}
            title={t('Close')}
          >
            <X size={16} />
          </button>
        </div>
      </div>

      {/* Video area + docked live chat — the chat panel takes its own column
          right of the video (same side-dock pattern as the archive preview's
          chat panel), never over the video while fullscreen. */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
      <div
        style={{
          flex: 1,
          position: 'relative',
          background: '#000',
          overflow: 'hidden',
        }}
      >
        <div
          data-live-video-area
          className={`absolute inset-0 z-0 ${controlsHidden ? 'cursor-none' : 'cursor-pointer'}`}
          onClick={() => {
            if (!loading && !error) togglePlay();
          }}
        >
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            onLoadedMetadata={() => {
              // Lock the resize math to the STREAM's aspect (not the panel's
              // box) — the reason the old free-form resize letterboxed.
              const v = videoRef.current;
              if (v && v.videoWidth > 0 && v.videoHeight > 0) {
                videoAspectRef.current = v.videoWidth / v.videoHeight;
              }
            }}
            style={{ width: '100%', height: '100%', objectFit: 'contain' }}
          />
        </div>

        {loading && (
          <div className="absolute inset-0 z-[1] flex items-center justify-center bg-black/60 pointer-events-none">
            <Loader2 size={40} className="animate-spin text-zinc-300" />
            <span className="ml-3 text-zinc-300 text-xs font-mono">
              {mode === 'replay' ? t('Loading replay…') : t('Loading live stream…')}
            </span>
          </div>
        )}

        {error && (
          <div
            className="absolute inset-0 z-[1] flex flex-col items-center justify-center gap-2.5 bg-black/70 text-red-400 text-xs font-mono text-center px-4"
          >
            <div>{error}</div>
            {previewRetry && (
              <button
                type="button"
                onClick={retryPreview}
                title={t('Retry this live stream')}
                className="flex items-center gap-1.5 text-[10px] font-black uppercase tracking-wider text-red-400 border-2 border-red-800 bg-red-950/30 px-2.5 py-1 hover:border-red-500 hover:text-red-300 cursor-pointer"
              >
                <RefreshCw size={12} />
                Retry
              </button>
            )}
          </div>
        )}

        {!loading && !error && buffering && (
          <div
            className="absolute inset-0 z-[1] flex items-center justify-center bg-black/50 text-zinc-300 text-xs font-mono pointer-events-none"
          >
            <Loader2 size={28} className="animate-spin text-zinc-200/90 mr-2" />
            Buffering…
          </div>
        )}

        {/* Floating archive-search popup — standalone window over the
            video (App's floating variant), never embedded in the small
            player. Hidden while this popup is fullscreen; state survives:
            the popup reopens on exit. */}
        {!isFullscreen && searchOpen && (
          <ArchiveSearchPopup
            zIndex={(zIndex ?? LIVE_POPUP_ACTIVE_Z) + 50}
            onClose={() => setSearchOpen(false)}
            onOpenHit={onOpenHit}
            savedChannels={savedChannels}
          />
        )}

        {/* Transport controls — same layout as the mini preview player: a
            timeline row (current/total timestamps + rail) above the transport
            row (play, volume, live-edge, quality, fullscreen). No trim here. */}
        {!loading && !error && (
          <div
            data-live-transport
            className={`px-2 py-1.5 bg-gradient-to-t from-black/85 to-black/0 transition-all duration-300 ${
              controlsHidden ? 'opacity-0 translate-y-2 pointer-events-none' : 'opacity-100 translate-y-0'
            }`}
            style={{
              position: 'absolute',
              insetInline: 0,
              bottom: 0,
              zIndex: 10,
            }}
          >
            <div className="flex items-center gap-1.5">
              <span className="w-11 shrink-0 font-mono text-[9px] text-zinc-300">
                {fmtDuration(currentSec)}
              </span>
              <div className="relative flex-1 min-w-0">
                <input
                  ref={railRef}
                  type="range"
                  min={railView.start}
                  max={railView.end}
                  step={railZoom > 1 ? 0.1 : 0.5}
                  value={Math.min(railView.end, Math.max(railView.start, railValue))}
                  disabled={railDisabled}
                  onChange={(e) => handleRailChange(parseFloat(e.target.value))}
                  className={`h-1 w-full ${mode === 'replay' ? 'accent-blue-400' : 'accent-red-500'} ${railDisabled ? 'opacity-60' : ''}`}
                  aria-label={mode === 'replay' ? t('Seek within replay') : t('Seek back into the broadcast (replay)')}
                  title={
                    (mode === 'replay'
                      ? t('Replay of the current broadcast — drag to seek')
                      : (railDisabled ? t('Replay unavailable for this channel') : t('Drag back to watch the past part of the stream')))
                    + (railZoom > 1 ? ` (${t('Scroll on the rail to zoom')})` : '')
                  }
                />
                {railZoom > 1 && (
                  <span
                    className="pointer-events-none absolute -top-1.5 right-0 font-mono text-[7px] text-zinc-500"
                    title={t('Scroll on the rail to zoom')}
                  >
                    ×{railZoom >= 10 ? Math.round(railZoom) : railZoom.toFixed(1)}
                  </span>
                )}
              </div>
              <span className="w-11 shrink-0 text-right font-mono text-[9px] text-zinc-400">
                {fmtDuration(displayTotal)}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-1.5">
              <button
                type="button"
                onClick={togglePlay}
                title={paused ? t('Play') : t('Pause')}
                className={transportBtn}
              >
                {paused ? <Play size={15} /> : <Pause size={15} />}
              </button>

              <div className="relative" data-player-menu data-volume-menu>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setVolumeMenuOpen((o) => !o);
                  }}
                  title="Volume"
                  className={transportBtn}
                >
                  {muted || volume === 0 ? <VolumeX size={15} /> : <Volume2 size={15} />}                </button>
                {volumeMenuOpen && (
                  <div className="absolute bottom-full left-0 z-30 mb-1.5 flex items-center gap-2 border-2 border-zinc-600 bg-zinc-950 px-2.5 py-2 shadow-lg">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleMute();
                      }}
                      title={muted ? t('Unmute') : t('Mute')}
                      className={transportBtn}
                    >
                      {muted || volume === 0 ? <VolumeX size={15} /> : <Volume2 size={15} />}
                    </button>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={muted ? 0 : volume}
                      onChange={(e) => onVolumeChange(parseFloat(e.target.value))}
                      className="h-1.5 w-24 accent-white"
                      aria-label="Volume"
                    />
                  </div>
                )}
              </div>

              {/* Fast CLIP — one click, no popups: seconds input (5..60) +
                  5s cooldown; the backend reports its real capability
                  (never fakes a clip). */}
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={handleFastClip}
                  disabled={clipCooldownLeft > 0}
                  title={clipCooldownLeft > 0
                    ? t('Clip cooldown {n}s', { n: Math.ceil(clipCooldownLeft / 1000) })
                    : t('Create a clip of the live stream')}
                  className={`${platformPreviewCtrlBtn(ctrlPlatform, false)} flex items-center gap-1.5 disabled:pointer-events-none`}
                >
                  <TwitchLogoIcon size={15} className="shrink-0" />
                  {/* Logo already says Twitch — label stays bare "clip" (user request). */}
                  <span className="text-[9px] font-bold uppercase tracking-wider whitespace-nowrap leading-none">
                    {clipCooldownLeft > 0 ? `${Math.ceil(clipCooldownLeft / 1000)}s` : 'clip'}
                  </span>
                </button>
                <div className="relative shrink-0">
                  <input
                    type="text"
                    inputMode="numeric"
                    value={String(clipSeconds)}
                    onChange={(e) => {
                      const digits = e.target.value.replace(/\D/g, '');
                      const v = digits === '' ? NaN : parseInt(digits, 10);
                      setClipSeconds(Number.isFinite(v) ? clampClipSeconds(v) : FAST_CLIP_DEFAULT_SEC);
                    }}
                    onFocus={(e) => e.currentTarget.select()}
                    onKeyDown={(e) => {
                      // Full-selection Backspace removes ONE digit (30 → 3,
                      // clamped to 5), not the whole value; typing still
                      // replaces it all.
                      const el = e.currentTarget;
                      if (e.key === 'Backspace' && el.selectionStart === 0 && el.selectionEnd === el.value.length) {
                        e.preventDefault();
                        const digits = el.value.slice(0, -1).replace(/\D/g, '');
                        const v = digits === '' ? NaN : parseInt(digits, 10);
                        setClipSeconds(Number.isFinite(v) ? clampClipSeconds(v) : FAST_CLIP_DEFAULT_SEC);
                        // React re-renders the new value asynchronously; drop
                        // the caret to the end so typing appends (30→5→5X).
                        requestAnimationFrame(() => {
                          el.setSelectionRange(el.value.length, el.value.length);
                        });
                      }
                    }}
                    className={`w-10 bg-black border-2 border-white/60 text-white text-[9px] font-mono py-1.5 pl-1 pr-3 text-right caret-white focus:border-white focus:bg-zinc-900 focus:outline-none ${platformButtonShadow(ctrlPlatform)}`}
                    aria-label={t('Clip duration (seconds)')}
                    title={t('Clip duration (seconds)')}
                  />
                  <span className="pointer-events-none absolute right-1 top-1/2 -translate-y-1/2 text-[9px] font-mono text-zinc-500">
                    s
                  </span>
                </div>
              </div>

              {clipNotice && (
                <div
                  role="status"
                  className="absolute left-0 right-0 -top-9 mx-2 rounded border-2 border-zinc-700 bg-zinc-950/95 px-2 py-1 text-[9px] font-mono text-zinc-200 shadow-lg"
                >
                  {clipNotice}
                </div>
              )}

              <div className="ml-auto flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => (mode === 'replay' ? switchToLive() : snapToLiveEdge())}
                  title={mode === 'replay' ? t('Return to live') : t('Snap to live edge')}
                  className="flex items-center gap-1 border-2 border-red-800 bg-red-950/30 px-1.5 py-1 text-[9px] font-bold tracking-wider text-red-400 hover:border-red-500 hover:text-red-300"
                >
                  <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-red-500" />
                  LIVE
                </button>

                <PreviewQualityMenu
                  levels={levels}
                  currentLevel={currentLevel}
                  menuOpen={qualityMenuOpen}
                  setMenuOpen={setQualityMenuOpen}
                  onSelect={handleQualitySelect}
                  disabled={!levels.length || mode === 'replay'}
                  buttonClassName={transportBtn}
                  popoverClassName={isFullscreen
                    ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
                    : 'border-2 border-zinc-600 bg-zinc-950'}
                />

                <button
                  type="button"
                  onClick={toggleFullscreen}
                  title={isFullscreen ? t('Exit fullscreen') : t('Fullscreen')}
                  className={transportBtn}
                >
                  {isFullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
      {/* Docked live chat — same side-dock rule as the archive search panel:
          hidden while this popup is fullscreen. Multi-stream channels merge
          one stream per live platform (filter chips appear in the panel). */}
      {chatOpen && !isFullscreen && chatSources.length > 0 && (
        <LiveChatPanel
          sources={chatSources}
          onClose={toggleChat}
        />
      )}
      </div>
    </div>,
    // Mount inside #explore-portal with the explore/local-file players so all
    // popups share one DOM subtree (it is a static mount point with no stacking
    // context — see index.css), letting the shared ladder z compete at root
    // level for cross-type bring-to-front (live vs explore vs search panel).
    document.getElementById('explore-portal') ?? document.body,
  );
}
