import React, { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react';
import { createPortal } from 'react-dom';
import { Captions, ExternalLink, Languages, Loader2, Maximize2, Minimize2, MessageSquare, Pause, Play, Search, Type, Volume2, VolumeX, RefreshCw, X } from 'lucide-react';
import { apiDelete, apiPost } from '../hooks/useApiClient';
import { archiveVideoIdFromUrl } from '../archiveScope';
import { buildVodUrl } from '../channelUtils';
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
import TwitchClipPopup from './TwitchClipPopup';
import { previewRetryAfterError, type PreviewRetryState } from '../previewRetry';
import { attachPreviewBufferingListeners, type PreviewBufferingHandle } from '../previewPlayerUtils';
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
  /** Confirmed-missing Kick/YouTube channel (live master 404): the popup swaps
   *  the player for a centered channel-name input, and the submitted name is
   *  handed up so App can open the add-channel editor. */
  onNotFoundChannel?: (name: string) => void;
  /** Optional saved channels (App state) — unioned into the channel dropdown. */
  savedChannels?: SavedChannel[];
  /** Position cascade offset — 0 = default corner; each sibling steps 28px. */
  cascadeIndex?: number;
  /** Shared floating-player z-ladder rank (App owns the monotonic counter).
   *  Omitted → falls back to LIVE_POPUP_ACTIVE_Z. */
  zIndex?: number;
  /** App raises this popup's ladder rank on pointer-down (drag to front). */
  onBringToFront?: () => void;
  /** Pre-warmed live session from channel hover — consumed on mount to skip the POST. */
  liveSessionPrefetchRef?: MutableRefObject<{ url: string; session: PreviewSessionResponse } | null>;
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

/** One live-caption block: the wall-clock window in epoch seconds (PDT-
 *  anchored from the media playlist) + the backend's pipeline latency (wall
 *  ms since the window's audio completed; absent without a PDT anchor). */
interface CaptionBlock {
  text: string;
  start: number;
  end: number;
  latencyMs?: number;
}
/** Re-snapshot the archive playlist while parked in REPLAY (grows while live). */
const REPLAY_RESNAPSHOT_MS = 30_000;
/** Live popup start ceiling, including session creation and first frame. */
const FIRST_FRAME_STALL_MS = 60_000;
/** Live session POST stall budget per attempt — after this the attempt is
 *  aborted and the next retry (or fallback entry) takes over. 15s covers
 *  cold-backend wake + transcode ramp without pinning the spinner. */
const SESSION_STALL_MS = 15_000;
/** Maximum retries for the session creation POST before giving up. */
const LIVE_SESSION_MAX_RETRIES = 3;
/** Inactivity fade for the transport + header ('rapido' per the user) — the
 *  App fullscreen pattern is 200ms; the live popup watches at 2500ms so a
 *  paused read of the rail/menus never flickers. */
const LIVE_CONTROLS_HIDE_MS = 2_500;
/** Retained live back-buffer (hls.js backBufferLength) — ArrowLeft/Right seek
 *  inside the ~20s retained window when the live has NO DVR archive. */
const LIVE_BACK_BUFFER_SEC = 20;
/** Never land the back-buffer arrow seek ON the live edge — stay a hair below
 *  it (hls.js's own sync target rides ~liveSyncDuration behind the edge). */
const LIVE_EDGE_SEEK_SAFETY_SEC = 0.75;

// ---------------------------------------------------------------------------
// Live captions clock anchor
// ---------------------------------------------------------------------------
//
// The backend streams caption blocks with WALL-CLOCK window times (start/end
// in epoch seconds, PDT-anchored from the media playlist). Rendering on
// arrival drifts: the transcript trails the audio by the transcribe latency
// AND the player lags the live edge, so "arrival time" means different wall
// times on different machines. Instead the overlay is anchored to the VIDEO
// clock: FRAG_BUFFERED frags carry their program date time, giving a 1:1
// currentTime → wall-epoch map; a block is shown once the mapped epoch
// reaches its window and skipped if the player already live-synced past it.
// The offset is then SELF-ADAPTIVE per machine — stalls freeze the video
// clock and pause the overlay automatically, no configuration.
/** A block shows when the video clock reaches end − lead (a hair before its
 *  window finishes, so the text is on screen as the speech completes). */
const CAPTION_LEAD_SEC = 0.25;
/** A block whose window ended more than this long before the video clock is
 *  stale (the player live-synced/seeked past it) — dropped, never shown. */
const CAPTION_STALE_SKIP_SEC = 1.0;
/** Newest (pos → pdt) frag anchors kept for the currentTime→wall map. */
const CAPTION_MAX_PDT_ANCHORS = 16;
/** Caption-box resize is MOUSE-DRAG ONLY (the old A−/A+ buttons are gone):
 *  the overlay's corner grip maps vertical drag distance onto this font-size
 *  clamp; the choice persists under the same key the buttons used. */
const CAPTION_FONT_MIN_PX = 14;
const CAPTION_FONT_MAX_PX = 48;
const CAPTION_FONT_STORAGE_KEY = 'vodrip.live.captionFontSize';
/** Per-session caption translate-target override — the in-player selector
 *  (pt-BR / English / Español) sends ?lang= on the caption SSE so the
 *  backend NLLB target follows the selection; null = follow the app
 *  language (backend default). Persisted like the font size. */
const CAPTION_LANG_OPTIONS = [
  { value: 'pt', label: 'pt-BR' },
  { value: 'en', label: 'English' },
  { value: 'es', label: 'Español' },
] as const;
type CaptionLang = (typeof CAPTION_LANG_OPTIONS)[number]['value'];
const CAPTION_LANG_STORAGE_KEY = 'vodrip.live.captionLang';
/** Caption SSE reconnect budget — bounded backoff (1.5s/3s/6s) so a
 *  recovered ASR/translate pipeline resumes captions WITHOUT user action,
 *  but a truly dead stream (channel offline, engine gone) gives up and hides
 *  the CC cluster instead of hammering the backend forever. */
const CAPTION_RECONNECT_MAX = 3;
const CAPTION_RECONNECT_BASE_MS = 1500;
/** Quality-pin gate: the fixed policy level is applied only after the player
 *  buffered a safe cushion ahead of the playhead (see armQualityPin). */
const QUALITY_PIN_BUFFER_SEC = 8;
/** Safety timer — pin the policy level even if the buffer never crosses the
 *  cushion (slow network) so the multi-player cap still lands. */
const QUALITY_PIN_SAFETY_MS = 15_000;
// Caption/live AV alignment: backend's CAPTION_TARGET_ALIGN_SEC (~0.9s) is the
// comment-constant wall target; the player honors it by riding 2 segments
// behind the edge (~4s at 2s segments) vs the 3-segment baseline (~6s) ONLY
// while captions are visible (text actually heard) and the stream is stable
// (forward buffer > 4s). Bounded 2-3, never touches liveSyncDuration
// (hls.js throws on count+duration mix), lowLatencyMode stays false.
const CAPTION_LIVE_SYNC_COUNT_CAPTIONED = 2;
const CAPTION_LIVE_SYNC_COUNT_BASELINE = 3;
/** Tolerance matching the cached VOD's created_at to the current session
 *  start (fast-clip guard): the CURRENT broadcast's VOD row is created at
 *  stream start; a PREVIOUS broadcast is hours older. ±5 min absorbs clock
 *  skew / cache-probe latency without ever matching an older broadcast. */
const LIVE_VOD_SESSION_MATCH_SEC = 5 * 60;


/** Wall epoch seconds of a frag's PROGRAM-DATE-TIME, with the backend's
 *  _parse_iso_epoch semantics: an explicit zone offset is honored, a NAIVE
 *  value (no offset) is UTC. hls.js's own programDateTime is
 *  Date.parse(raw), which reads naive values as LOCAL time — on a naive-PDT
 *  stream that would put the anchor clock hours off the backend's caption
 *  clock (start/end/latency_ms are UTC epochs), the prime drift suspect. */
function parsePdtEpochSec(raw: string | null | undefined, ms: number | null | undefined): number {
  if (typeof raw === 'string' && raw.trim()) {
    let v = raw.trim();
    // Mirror the backend's _parse_iso_epoch: Z and explicit offsets parse
    // as-is; a NAIVE value (no zone suffix) defaults to UTC.
    if (/[zZ]$/.test(v)) v = v.slice(0, -1) + '+00:00';
    else if (!/[+-]\d{2}:?\d{2}$/.test(v)) v = v + '+00:00';
    const t = Date.parse(v);
    if (Number.isFinite(t)) return t / 1000;
  }
  return typeof ms === 'number' && Number.isFinite(ms) ? ms / 1000 : Number.NaN;
}

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

export function LivePlayerPopup({ entry, entries, channelName, onClose, channelSlug, channel, vodUrl, onOpenHit, onNotFoundChannel, savedChannels, cascadeIndex = 0, zIndex, onBringToFront, liveSessionPrefetchRef }: LivePlayerPopupProps) {
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
  const firstFrameTimerRef = useRef<number | null>(null);
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
  /** Twitch clip mini-preview — opened at the live playhead (120s window,
   *  user trims there and creates the clip). Mirrors the preview clip popup
   *  state (ChannelExplorePopup). */
  const [clipPopup, setClipPopup] = useState<{
    url: string;
    broadcasterLogin: string;
    vodId: string;
    playheadSec: number;
    vodDurationSec: number;
    reuseSession?: { sessionId: string; trimTimeline: boolean } | null;
  } | null>(null);
  // Transient clip notice (VOD guard feedback).
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
  // Loading can outlive the session POST while the first media frame arrives;
  // only an unresolved session request blocks auto-hide.
  const sessionPendingRef = useRef(true);
  const [error, setError] = useState<string | null>(null);
  // Confirmed-missing Kick/YouTube channel (live master 404): swaps the player
  // for a centered channel-name input until the user submits a correction.
  const [notFound, setNotFound] = useState(false);
  const [missingName, setMissingName] = useState('');
  // Seed the not-found input with the current channel name each time the
  // state activates (the name differs per entry in the fallback chain).
  useEffect(() => {
    if (notFound) setMissingName(channelName);
  }, [notFound, channelName]);
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
  const bufferingHandleRef = useRef<PreviewBufferingHandle | null>(null);
  /** True once the video has started playing at least once (playing/canplay
   *  fired). The buffering overlay is suppressed during the initial load
   *  spinner and only shows after playback has started then stalled. */
  const hasPlayedOnceRef = useRef(false);
  const pendingReplaySeekRef = useRef<number | null>(null);
  // True when the user paused (togglePlay) — unexpected pauses (e.g. the
  // play() promise aborting on a live-sync seek) auto-resume instead.
  const userPausedRef = useRef(false);

  // Inactivity auto-hide for the transport + header: a ref-backed timer
  // hides ~2.5s after the last interaction; any mousemove/keydown/pointerdown/
  // touchstart bumps it back. The timer only ARMS when a hide-block is clear —
  // paused, an unresolved session, errors, or open menus keep the controls up.
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

  // --- Real-time live captions (CC overlay) ---
  // The backend runs ONE captioner per (platform, channel) — audio-only HLS
  // rendition, ~2s windows, parakeet ASR — and streams caption blocks over
  // SSE. The popup probes /available once per playing entry (the parakeet
  // gate 503s otherwise) and only then shows the CC toggle; the overlay
  // shows the latest block whose window the VIDEO clock reached (blocks are
  // anchored to the wall clock via the frag PDT map — see CAPTION_LEAD_SEC /
  // captionClockSync below). Captions are ON by default when available (the
  // user's intent: the transcript IS the feature), toggle hides them.
  const captionSource = useMemo(() => {
    const plat = (activeEntry.platform || '').toLowerCase();
    if (plat !== 'twitch' && plat !== 'kick' && plat !== 'youtube') return null;
    const saved = plat === 'kick' ? channel?.kickSlug
      : plat === 'twitch' ? channel?.twitchSlug
      : channel?.youtubeSlug;
    const slug = liveChatSlugFromUrl(activeEntry.url, plat) ?? saved ?? channelSlug;
    return slug ? { platform: plat, channel: slug } : null;
  }, [activeEntry.url, activeEntry.platform, channel, channelSlug]);
  const [captionsAvailable, setCaptionsAvailable] = useState(false);
  const [captionsEnabled, setCaptionsEnabled] = useState(true);
  // Backend NLLB translate gate — the /available contract exposes
  // translation_available (false in the slim frozen base). When false the
  // in-player language selector is disabled (captions still stream in the
  // source language).
  const [captionTranslationAvailable, setCaptionTranslationAvailable] = useState(false);
  // Overlay font size (px) — seeded from localStorage; the overlay's corner
  // drag grip (mouse-drag resize only, no A−/A+ buttons) steps it within the
  // clamp; the save effect below writes every change back so the user's
  // preference survives reopen (storage blocked → keep the default).
  const [captionFontSize, setCaptionFontSize] = useState<number>(() => {
    try {
      const n = Number(localStorage.getItem(CAPTION_FONT_STORAGE_KEY));
      if (Number.isInteger(n) && n >= CAPTION_FONT_MIN_PX && n <= CAPTION_FONT_MAX_PX) return n;
    } catch {
      /* storage unavailable */
    }
    return CAPTION_FONT_MIN_PX;
  });
  useEffect(() => {
    try {
      localStorage.setItem(CAPTION_FONT_STORAGE_KEY, String(captionFontSize));
    } catch {
      /* storage blocked — the preference simply won't persist */
    }
  }, [captionFontSize]);
  // Caption translate-target override — the in-player selector (pt-BR /
  // English / Español) reconnects the caption SSE with ?lang= so the backend
  // NLLB target follows the selection; null = follow the app language.
  const [captionLang, setCaptionLang] = useState<CaptionLang | null>(() => {
    try {
      const v = localStorage.getItem(CAPTION_LANG_STORAGE_KEY);
      if (v === 'pt' || v === 'en' || v === 'es') return v;
    } catch {
      /* storage unavailable */
    }
    return null;
  });
  useEffect(() => {
    try {
      if (captionLang) localStorage.setItem(CAPTION_LANG_STORAGE_KEY, captionLang);
      else localStorage.removeItem(CAPTION_LANG_STORAGE_KEY);
    } catch {
      /* storage blocked — the choice simply won't persist */
    }
  }, [captionLang]);
  const [captionLangMenuOpen, setCaptionLangMenuOpen] = useState(false);
  const [captionFontSizeMenuOpen, setCaptionFontSizeMenuOpen] = useState(false);
  /** True once at least one caption was received via SSE — controls the
   *  overlay visibility independently of captionsAvailable (the /available
   *  probe result). Show captions as soon as text arrives, even while the
   *  probe is still resolving. */
  const [captionsHeard, setCaptionsHeard] = useState(false);
  // Delay SSE creation by one render cycle to avoid React 19 StrictMode
  // double-mount creating two EventSource instances. The probe runs in
  // parallel; the SSE opens before the probe resolves (~50-200ms fetch).
  const [sseReady, setSseReady] = useState(false);
  useEffect(() => { setSseReady(true); }, []);
  const [caption, setCaption] = useState<CaptionBlock | null>(null);
  // Caption clock anchor state — refs (mutated by hls events + SSE, read by
  const pdtAnchorsRef = useRef<{ pos: number; pdt: number }[]>([]);
  const captionOriginRef = useRef<number | null>(null);
  const pendingCaptionsRef = useRef<CaptionBlock[]>([]);
  // Stall guard: first fatal BUFFER_STALLED_ERROR nudges to liveSyncPosition;
  // only a second fatal within the window or a stall persisting >2s advances
  // to the next fallback entry (prevents hopping channel on transient jitter).
  const stallGuardRef = useRef<{ at: number; count: number }>({ at: 0, count: 0 });
  // Caption SSE reconnect budget — survives effect cleanups so the bounded
  // backoff spans reconnects (a cleanup-per-tick reset would unbounded the
  // retries); resets on a healthy caption or a stream change.
  const captionRetryRef = useRef<{ attempt: number; timer: number | null }>({ attempt: 0, timer: null });
  // Bumping re-opens the caption EventSource (see the SSE effect).
  const [captionSseTick, setCaptionSseTick] = useState(0);
  // Cold-start fallback:
  // inside the video area (not the OS desktop). Offset in pixels from the
  // default bottom-center position; resets on stream change.
  const captionOverlayDragRef = useRef<{ offsetX: number; offsetY: number }>({ offsetX: 0, offsetY: 0 });
  const [captionOverlayOffset, setCaptionOverlayOffset] = useState({ x: 0, y: 0 });
  const captionOverlayDragStartRef = useRef<{ pointerX: number; pointerY: number; offsetX: number; offsetY: number } | null>(null);
  // Cold-start fallback: the newest block dropped by the stale-head shift
  // when EVERY queued block is stale (first anchor landed after the pending
  // windows passed) — re-shown while nothing fresh is due so the overlay
  // never goes blank (see captionClockSync).
  const staleFallbackRef = useRef<CaptionBlock | null>(null);
  const captionEdgeRef = useRef(0);
  const archiveDurationRef = useRef(0);

  /** Map the video's timeline position to the wall-clock epoch the backend's
   *  caption times live on. Primary: the nearest frag (pos → pdt) anchor
   *  from FRAG_BUFFERED — a 1:1 timeline→wall map (Twitch/Kick live
   *  playlists carry PROGRAM-DATE-TIME). Fallback (no anchors yet / no-PDT
   *  stream): broadcast-relative seconds + an origin calibrated from the
   *  first caption's window (the video sits at the block's due point when it
   *  arrives — the transcript trails the video by design). NaN = unmapped —
   *  callers degrade to show-on-arrival. */
  const captionEpochOf = useCallback((currentTime: number): number => {
    const anchors = pdtAnchorsRef.current;
    if (anchors.length > 0) {
      let best = anchors[0];
      let bestDist = Math.abs(best.pos - currentTime);
      for (const a of anchors) {
        const d = Math.abs(a.pos - currentTime);
        if (d < bestDist) {
          best = a;
          bestDist = d;
        }
      }
      return best.pdt + (currentTime - best.pos);
    }
    const origin = captionOriginRef.current;
    if (origin === null) return Number.NaN;
    return origin + liveBroadcastPositionSec(
      archiveDurationRef.current,
      captionEdgeRef.current,
      currentTime,
    );
  }, []);

  /** Wall-clock epoch (UTC seconds) when the CURRENT broadcast started —
   *  frag PDT anchors map any buffered position to wall time (pdt − pos is
   *  constant across a session); the caption fallback origin is the epoch at
   *  broadcast position 0. NaN while neither clock exists yet. */
  const liveSessionStartEpoch = useCallback((): number => {
    const anchors = pdtAnchorsRef.current;
    if (anchors.length > 0) {
      const newest = anchors[anchors.length - 1]; // newest frag — least drift
      return newest.pdt - newest.pos;
    }
    return captionOriginRef.current ?? Number.NaN;
  }, []);

  /** Promote pending captions whose window the video clock has reached, drop
   *  stale ones, and hide a shown caption the clock ran far past (live-sync
   *  seek / resume after a stall). Runs on every timeupdate + caption
   *  arrival — stalls freeze the clock, so the overlay pauses automatically
   *  and the queued blocks catch up when playback resumes. */
  const captionClockSync = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    const epoch = captionEpochOf(video.currentTime);
    const pending = pendingCaptionsRef.current;
    if (!Number.isFinite(epoch)) {
      // No clock map yet (pre-first-fragment / pre-first-caption) — show the
      // newest arrival rather than nothing (graceful degradation).
      if (pending.length > 0) setCaption(pending[pending.length - 1]);
      return;
    }
    // Stale head: blocks the video already played past (stall recovery/seek).
    // Cold start: the FIRST anchor can land after the pending windows already
    // passed (the stream was live before the map existed) — dropping every
    // queued block would leave the overlay blank until the next caption.
    // Remember the last dropped block; when ALL pending is stale it becomes
    // the fallback (re-shown below while nothing fresh is due).
    let lastStale: CaptionBlock | null = null;
    while (pending.length > 0 && pending[0].end < epoch - CAPTION_STALE_SKIP_SEC) {
      lastStale = pending.shift()!;
    }
    if (lastStale && pending.length === 0) staleFallbackRef.current = lastStale;
    let promoted = false;
    while (pending.length > 0 && pending[0].end - CAPTION_LEAD_SEC <= epoch) {
      staleFallbackRef.current = null; // fresh block lands — clear the fallback
      setCaption(pending.shift()!); // newest due block wins (React batches)
      promoted = true;
    }
    if (!promoted) {
      // Nothing due — hide a shown caption the clock ran past without a
      // fresh block queued behind it, UNLESS the cold-start fallback holds
      // the newest stale block (never go blank while queued text exists).
      setCaption((cur) => (
        cur && cur.end >= epoch - CAPTION_STALE_SKIP_SEC ? cur : staleFallbackRef.current
      ));
    }
  }, [captionEpochOf]);

  useEffect(() => {
    setCaption(null); // a new stream starts with a clean overlay
    setCaptionsAvailable(false); // …and a clean availability (per-stream)
    setCaptionTranslationAvailable(false); // …and a clean translate gate (per-stream)
    setCaptionsHeard(false); // …and a clean heard-state (per-stream)
    const st = captionRetryRef.current;
    if (st.timer != null) window.clearTimeout(st.timer);
    captionRetryRef.current = { attempt: 0, timer: null }; // fresh stream — fresh retry budget
    if (!captionSource) return;
    let cancelled = false;
    let probeAttempt = 0;
    const probe = () => {
      fetch(`/api/live/captions/available?${new URLSearchParams(captionSource)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((body) => {
          if (cancelled) return;
          // available = runtime + parakeet ready; pending = runtime exists but
          // the model downloads on first caption use. BOTH permit an explicit
          // caption stream, so the CC toggle shows in either case (the SSE
          // opens regardless of the gate — see the caption SSE effect).
          if (body?.available === true || body?.pending === true) {
            setCaptionsAvailable(true);
            setCaptionTranslationAvailable(body?.translation_available === true);
            return;
          }
          // Unavailable (models missing) or empty — bounded re-probe: the
          // parakeet gate can flip mid-session (model install), and a
          // transient failure at open must not leave the CC cluster dead
          // (captions "not activating" until manual interaction). After the
          // budget the stream simply has no captions.
          probeAttempt += 1;
          if (probeAttempt < CAPTION_RECONNECT_MAX) {
            st.timer = window.setTimeout(probe, CAPTION_RECONNECT_BASE_MS * 2 ** (probeAttempt - 1));
          }
        })
        .catch(() => {
          if (cancelled) return;
          probeAttempt += 1;
          if (probeAttempt < CAPTION_RECONNECT_MAX) {
            st.timer = window.setTimeout(probe, CAPTION_RECONNECT_BASE_MS * 2 ** (probeAttempt - 1));
          }
        });
    };
    probe();
    return () => {
      cancelled = true;
      if (st.timer != null) {
        window.clearTimeout(st.timer);
        st.timer = null;
      }
    };
  }, [captionSource]);

  useEffect(() => {
    // Open SSE as soon as the caption source exists and captions are enabled.
    // Don't wait for the /available probe — captions arrive immediately via
    // SSE while the probe is still resolving (fixes "captions not activating
    // on open"). Probe in parallel to hide CC if models are truly missing.
    // sseReady delays creation by one render cycle to avoid StrictMode
    // double-mount creating two EventSource instances.
    if (!captionSource || !captionsEnabled || !sseReady) return;
    // jsdom has no EventSource — the chat panel degrades the same way; the
    // tests stub it and drive the handlers directly.
    if (typeof EventSource === 'undefined') return;
    const params = new URLSearchParams(captionSource);
    // Per-session translate-target override — the backend captioner follows
    // the selector's family instead of the app language.
    if (captionLang) params.set('lang', captionLang);
    const es = new EventSource(`/api/live/captions?${params}`);
    let heardCaption = false;
    /** Bounded backoff reconnect: a fresh SSE connection restarts the
     *  backend captioner worker (get_captioner → acquire), so an ASR /
     *  translate pipeline failure self-heals WITHOUT user action. After the
     *  budget is exhausted the stream is truly dead → hide the CC cluster. */
    const armReconnect = () => {
      const st = captionRetryRef.current;
      if (st.timer != null) return;
      if (st.attempt >= CAPTION_RECONNECT_MAX) {
        setCaptionsAvailable(false);
        es.close();
        return;
      }
      st.attempt += 1;
      st.timer = window.setTimeout(() => {
        st.timer = null;
        // Re-probe the parakeet gate (models may be gone) before re-opening.
        fetch(`/api/live/captions/available?${new URLSearchParams(captionSource)}`)
          .then((r) => (r.ok ? r.json() : null))
          .then((body) => {
            // Same gate as the startup probe: pending still allows a stream.
            if (body?.available === true || body?.pending === true) {
              setCaptionTranslationAvailable(body?.translation_available === true);
              setCaptionSseTick((n) => n + 1);
            } else setCaptionsAvailable(false);
          })
          .catch(() => setCaptionSseTick((n) => n + 1)); // transient probe failure — still retry
      }, CAPTION_RECONNECT_BASE_MS * 2 ** (st.attempt - 1));
    };
    es.addEventListener('caption', (ev) => {
      heardCaption = true;
      setCaptionsHeard(true);
      captionRetryRef.current.attempt = 0; // healthy frame — reset the retry budget
      try {
        const data = JSON.parse((ev as MessageEvent).data) as CaptionBlock;
        const video = videoRef.current;
        // First block on a no-anchor LIVE timeline calibrates the fallback
        // origin — the video sits at the block's due point when it arrives
        // (the transcript trails the video by design). Runs BEFORE epochOf:
        // epoch is NaN until the origin exists, and the calibration itself
        // is what makes the mapping finite. Replay is excluded: the video
        // sits at a dragged/stale position, so arrival-due would anchor the
        // whole replay timeline wrong (replay snapshots carry PDT and get
        // real anchors instead).
        if (video && modeRef.current === 'live'
          && pdtAnchorsRef.current.length === 0 && captionOriginRef.current === null) {
          const b = liveBroadcastPositionSec(
            archiveDurationRef.current,
            captionEdgeRef.current,
            video.currentTime,
          );
          if (Number.isFinite(b)) captionOriginRef.current = data.end - CAPTION_LEAD_SEC - b;
        }
        const epoch = video ? captionEpochOf(video.currentTime) : Number.NaN;
        if (Number.isFinite(epoch)) {
          // The player already live-synced past the window — skip the stale
          // block (never render text the video has finished).
          if (data.end < epoch - CAPTION_STALE_SKIP_SEC) return;
        }
        // Hold the block until the video clock reaches its window
        // (captionClockSync promotes it; stalls pause the overlay).
        pendingCaptionsRef.current.push(data);
        captionClockSync();
      } catch {
        // malformed frame — keep the last caption
      }
    });
    es.addEventListener('offline', () => {
      // Pipeline failure (ASR crash / confirmed offline / translate break) —
      // close and reconnect with backoff; a fresh connection restarts the
      // captioner. Never a silent dead stream, never an endless reconnect
      // storm (bounded budget above).
      es.close();
      armReconnect();
    });
    es.addEventListener('error', () => {
      // Before the first caption: likely a gate 503 (models missing) or a
      // connect failure — own the retry (bounded) instead of EventSource's
      // unbounded native auto-reconnect. After the first caption: a
      // transient network blip — the native auto-reconnect handles it.
      if (!heardCaption) {
        es.close();
        armReconnect();
      }
    });
    return () => {
      es.close();
      const st = captionRetryRef.current;
      if (st.timer != null) {
        window.clearTimeout(st.timer);
        st.timer = null;
      }
      // NOTE: attempt is NOT reset here — the bounded budget spans
      // reconnects (a cleanup-per-tick reset would make retries unbounded).
      // It resets on a healthy caption or a stream change (probe effect).
      // A new stream / toggle must not inherit the previous timeline's queue.
      if (!captionsEnabled) setCaption(null);
      pendingCaptionsRef.current = [];
      staleFallbackRef.current = null;
      // Reset drag offset — a new stream's overlay starts at default position.
      captionOverlayDragRef.current = { offsetX: 0, offsetY: 0 };
      setCaptionOverlayOffset({ x: 0, y: 0 });
    };
  }, [captionSource, captionsEnabled, sseReady, captionEpochOf, captionClockSync, captionLang, captionSseTick]);

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
  // Quality-pin gate: the policy level is applied only once the player has
  // buffered a safe cushion ahead of the playhead. Pinning at MANIFEST_PARSED
  // switched levels while the live-edge buffer was ~1-2s deep (targetLatency
  // 0) — the SOURCE-level fragment fetch (high bitrate, proxied) underran it
  // and playback stalled ~3s in, right after the <1s start (user report).
  // ABR stays on the fast low level during warm-up; armQualityPin lands the
  // pin once the forward buffer crosses QUALITY_PIN_BUFFER_SEC.
  const qualityPinArmedRef = useRef(false);
  const qualityPinTimerRef = useRef<number | null>(null);
  const applyQualityPolicy = useCallback(() => {
    if (!qualityPinArmedRef.current) return; // warm-up — never switch on a shallow buffer
    const hls = hlsRef.current;
    if (!hls || modeRef.current !== 'live' || !hls.levels || hls.levels.length === 0) return;
    const multi = liveQualityCountNow() > 1;
    const idx = qualityLevelForPolicy(hls.levels, multi);
    if (idx < 0) return;
    hls.currentLevel = idx;
    setCurrentLevel(idx);
  }, []);

  /** Arm + apply the quality-policy level. Called from FRAG_BUFFERED once
   *  the forward buffer crosses the cushion (and from a safety timer) —
   *  NEVER from MANIFEST_PARSED, where the switch would drain the shallow
   *  live-edge buffer and stall playback. */
  const armQualityPin = useCallback(() => {
    if (qualityPinArmedRef.current) return;
    qualityPinArmedRef.current = true;
    if (qualityPinTimerRef.current != null) {
      window.clearTimeout(qualityPinTimerRef.current);
      qualityPinTimerRef.current = null;
    }
    applyQualityPolicy();
  }, [applyQualityPolicy]);

  // Registry membership: register on mount (notifies every player — this one
  // no-ops until its manifest parses), unregister on unmount (reverts the
  // remaining players to their policy for the lower count).
  useEffect(() => registerLivePlayer(() => { applyQualityPolicy(); }), [applyQualityPolicy]);
  // Caption-aware live sync: when captions are visible (text heard + enabled)
  // ride 0.6-1.0s closer to the edge (count 2 vs 3) only if buffer > 4s.
  // One knob, count-only — never touches liveSyncDuration (mix throws).
  // Hysteresis: downshift at >4.5s, upshift at <3.5s — prevents
  // oscillation when forward buffer hovers near the 4s threshold.
  const CAPTION_SYNC_DOWNSHIFT_SEC = 4.5;
  const CAPTION_SYNC_UPSHIFT_SEC = 3.5;
  const maybeTuneCaptionLiveSync = useCallback(() => {
    const hls: any = hlsRef.current;
    const video = videoRef.current;
    if (!hls || modeRef.current !== 'live' || !video) return;
    const captionOn = captionsEnabled && captionsHeard;
    const buffered = (() => {
      try {
        const b = video.buffered;
        if (!b || b.length === 0) return 0;
        return b.end(b.length - 1) - video.currentTime;
      } catch { return 0; }
    })();
    const currentCount = hls.config.liveSyncDurationCount;
    let want: number;
    if (captionOn && currentCount === CAPTION_LIVE_SYNC_COUNT_CAPTIONED) {
      // Currently at count 2 — only shift back to 3 if buffer drops below 3.5s.
      want = buffered < CAPTION_SYNC_UPSHIFT_SEC
        ? CAPTION_LIVE_SYNC_COUNT_BASELINE
        : CAPTION_LIVE_SYNC_COUNT_CAPTIONED;
    } else if (captionOn && buffered > CAPTION_SYNC_DOWNSHIFT_SEC) {
      // Currently at count 3 (or baseline) — shift to 2 only when buffer is healthy.
      want = CAPTION_LIVE_SYNC_COUNT_CAPTIONED;
    } else {
      want = CAPTION_LIVE_SYNC_COUNT_BASELINE;
    }
    if (hls.config.liveSyncDurationCount !== want) {
      hls.config.liveSyncDurationCount = want;
      // Catch-up nudge only when tightening (3→2) and already stable.
      if (want === CAPTION_LIVE_SYNC_COUNT_CAPTIONED) {
        const pos = hls.liveSyncPosition;
        if (typeof pos === 'number' && Number.isFinite(pos)) {
          // One-time small nudge — capped so it never seeks backward or jumps far.
          const delta = pos - video.currentTime;
          if (delta > 0.5 && delta < 6) video.currentTime = pos;
        }
      }
    }
  }, [captionsEnabled, captionsHeard]);
  // Re-evaluate on caption state flips and once per FRAG_BUFFERED / timeupdate.
  useEffect(() => { maybeTuneCaptionLiveSync(); }, [maybeTuneCaptionLiveSync]);


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
    if (!qualityMenuOpen && !volumeMenuOpen && !captionLangMenuOpen && !captionFontSizeMenuOpen) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (volumeMenuOpen && target.closest('[data-volume-menu]')) return;
      if (qualityMenuOpen && target.closest('[data-quality-menu]')) return;
      if (captionLangMenuOpen && target.closest('[data-caption-lang-menu]')) return;
      if (captionFontSizeMenuOpen && target.closest('[data-caption-font-size-menu]')) return;
      setQualityMenuOpen(false);
      setVolumeMenuOpen(false);
      setCaptionLangMenuOpen(false);
      setCaptionFontSizeMenuOpen(false);
    };
    window.addEventListener('mousedown', handler);
    return () => window.removeEventListener('mousedown', handler);
  }, [qualityMenuOpen, volumeMenuOpen, captionLangMenuOpen, captionFontSizeMenuOpen]);

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
  const hideBlocked = paused || (loading && sessionPendingRef.current) || error !== null
    || qualityMenuOpen || captionLangMenuOpen || captionFontSizeMenuOpen || isTransportTextFocused();
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
    bufferingHandleRef.current?.clearStall();
    setBuffering(false);
    if (hlsRef.current) {
      try {
        hlsRef.current.destroy();
      } catch {
        /* ignore */
      }
      hlsRef.current = null;
    }
    // The caption clock map belongs to the OLD timeline — a new hls instance
    // (mode switch / session recreate) starts from a fresh anchor set.
    pdtAnchorsRef.current = [];
    captionOriginRef.current = null;
    // A new instance re-arms the quality pin from its own MANIFEST_PARSED.
    if (qualityPinTimerRef.current != null) {
      window.clearTimeout(qualityPinTimerRef.current);
      qualityPinTimerRef.current = null;
    }
    qualityPinArmedRef.current = false;
    // Reset stall guard across sessions so a previous channel's state
    // doesn't cause the next channel to hop to fallback on first jitter.
    stallGuardRef.current = { at: 0, count: 0 };
  }, []);

  const clearFirstFrameWatchdog = useCallback(() => {
    if (firstFrameTimerRef.current != null) {
      window.clearTimeout(firstFrameTimerRef.current);
      firstFrameTimerRef.current = null;
    }
  }, []);

  const failFirstFrameWatchdog = useCallback(() => {
    clearFirstFrameWatchdog();
    destroyHls();
    setError(t('Live playback failed — try again'));
    setLoading(false);
    markPreviewError();
  }, [clearFirstFrameWatchdog, destroyHls, markPreviewError, t]);


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
      lowLatencyMode: false, // proxy + LL-HLS stalls — disabled for stable playback
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      // Retained back-buffer = the arrow-seek window: LIVE without a DVR
      // archive still lets ArrowLeft/Right rewind ~30s into the stream (the
      // rail stays disabled — see the keydown listener below).
      backBufferLength: LIVE_BACK_BUFFER_SEC,
      startFragPrefetch: true,
      // hls.js 1.6 deprecated fragLoadingTimeOut/manifestLoadingTimeOut in
      // favor of fragLoadPolicy/playlistLoadPolicy with granular control.
      // Fast-fail: TTFB 8s (detect dead CDN fast), total load 15s, 2 retries
      // with 0/1s delay. Live segments must arrive quickly — a 30s timeout
      // means a dead request holds the buffer while it drains.
      fragLoadPolicy: {
        default: {
          maxTimeToFirstByteMs: 8000,
          maxLoadTimeMs: 15000,
          timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
          errorRetry: { maxNumRetry: 3, retryDelayMs: 1000, maxRetryDelayMs: 4000 },
        },
      },
      playlistLoadPolicy: {
        default: {
          maxTimeToFirstByteMs: 5000,
          maxLoadTimeMs: 8000,
          timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
          errorRetry: { maxNumRetry: 3, retryDelayMs: 1000, maxRetryDelayMs: 4000 },
        },
      },
      testBandwidth: false,
      // 3 segments behind the edge — more cushion than count 2 for
      // proxy-mediated playback; ~6s at 2s segments.
      liveSyncDurationCount: 3, // per-platform audio HLS picker lives in backend (_parse_master_audio_url + _resolve_live_master)
      // hls.js REQUIRES liveMaxLatencyDurationCount > liveSyncDurationCount
      // — 8 ≈ 16s at 2s segments.
      liveMaxLatencyDurationCount: 8,
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
      // 1.1× catch-up: slow enough to avoid the "audio/video running" feel
      // that 1.2-1.5× produces; still recovers from drift within ~10s.
      maxLiveSyncPlaybackRate: 1.1,
      startPosition: -1,
      autoStartLoad: !replay,
    });
    hlsRef.current = hls;
    // e2e probe hook (same convention as window.__vodripAdSegmentsStripped).
    (window as unknown as { __livePopupHls?: Hls }).__livePopupHls = hls;
    hls.loadSource(src);
    hls.attachMedia(video);
    let networkRetries = 0;
    let mediaRetries = 0;

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
        // DEFERRED quality pin — see armQualityPin: applying the policy
        // level here switches while the live-edge buffer is ~1-2s deep and
        // stalls playback ~3s in. The FRAG_BUFFERED handler applies it once
        // the forward buffer is deep; the safety timer covers slow networks.
        qualityPinArmedRef.current = false;
        if (qualityPinTimerRef.current != null) {
          window.clearTimeout(qualityPinTimerRef.current);
          qualityPinTimerRef.current = null;
        }
        qualityPinTimerRef.current = window.setTimeout(armQualityPin, QUALITY_PIN_SAFETY_MS);
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
    });

    hls.on(Hls.Events.LEVEL_SWITCHED, (_e, data) => {
      // Guard -1 (auto): ABR switches report -1 and would highlight nothing.
      if (modeRef.current === 'replay') return;
      if (typeof data?.level === 'number' && data.level >= 0) setCurrentLevel(data.level);
    });

    // Caption clock anchor: every buffered fragment carries its program date
    // time (wall epoch ms) — keep a small (pos → pdt) map so the overlay can
    // be shown against the VIDEO clock instead of on arrival (see
    // captionEpochOf). Frags re-buffer on live-sync seeks / level switches;
    // dedupe by position and cap the list to the newest frags.
    hls.on(Hls.Events.FRAG_BUFFERED, (_e, data) => {
      const frag = data?.frag;
      if (!frag) return;
      const pos = frag.start;
      // Deferred quality-pin arming (live only): once the forward buffer
      // crosses the safety cushion, the policy level can switch without
      // stalling the live edge (see armQualityPin). Runs even without a PDT
      // anchor (the anchor path below may skip the frag).
      if (modeRef.current === 'live' && typeof pos === 'number' && Number.isFinite(pos)) {
        const v = videoRef.current;
        const dur = typeof frag.duration === 'number' ? frag.duration : 0;
        if (v && !qualityPinArmedRef.current && pos + dur - v.currentTime >= QUALITY_PIN_BUFFER_SEC) {
          armQualityPin();
        }
      }
      // Re-parse the raw PDT tag (UTC-default, same base as the backend's
      // caption times) instead of trusting hls.js's local-zone Date.parse
      // — naive PDTs would drift the anchor clock by the user's UTC offset.
      const pdt = parsePdtEpochSec(
        frag.rawProgramDateTime,
        typeof frag.programDateTime === 'number' ? frag.programDateTime : null,
      );
      if (typeof pos !== 'number' || !Number.isFinite(pos) || !Number.isFinite(pdt)) {
        // No PDT anchor — still re-evaluate caption live-sync on buffer depth.
        maybeTuneCaptionLiveSync();
        return;
      }
      const anchors = pdtAnchorsRef.current;
      const existing = anchors.findIndex((a) => a.pos === pos);
      if (existing >= 0) anchors[existing] = { pos, pdt };
      else anchors.push({ pos, pdt });
      if (anchors.length > CAPTION_MAX_PDT_ANCHORS) {
        anchors.splice(0, anchors.length - CAPTION_MAX_PDT_ANCHORS);
      }
      maybeTuneCaptionLiveSync();
    });

    hls.on(Hls.Events.ERROR, (_e, data) => {
      // Confirmed-missing channel (Kick/YouTube): the live master URL proxies
      // to a 404 when the channel id/slug has no playable stream — the channel
      // doesn't exist, not a transient blip. Surface the centered channel-name
      // input instead of advancing the fallback chain or showing retry.
      if (modeRef.current === 'live'
        && data?.type === Hls.ErrorTypes.NETWORK_ERROR
        && (data.response as { code?: number } | undefined)?.code === 404
        && (sessionPlatformRef.current === 'kick' || sessionPlatformRef.current === 'youtube')) {
        setNotFound(true);
        setLoading(false);
        setError(null);
        markPreviewError();
        return;
      }
      // Stall guard (live only): hls.js 1.6.2 reports a stall as
      // BUFFER_STALLED_ERROR — NON-fatal once per stall period (hls.js
      // nudges and retries first), then fatal if the nudges fail. A
      // transient <1s jitter must NOT advance to the next entry (that
      // deletes the session, kills the player, and restarts a different
      // channel at its live edge). First fatal stalls nudge to
      // liveSyncPosition; only a second fatal within 2s advances.
      if (modeRef.current === 'live' && data?.fatal === true
        && data?.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR) {
        const now = Date.now();
        const g = stallGuardRef.current;
        const withinWindow = now - g.at < 2000;
        if (withinWindow && g.count >= 1) {
          if (tryAdvanceEntry()) return;
        } else {
          // Nudge to liveSyncPosition instead of hopping channel.
          const v = videoRef.current;
          const pos: any = (hls as any).liveSyncPosition;
          if (v && typeof pos === 'number' && Number.isFinite(pos)) {
            v.currentTime = pos;
            stallGuardRef.current = { at: now, count: g.count + (withinWindow ? 1 : 0) + 1 };
            return;
          }
          if (withinWindow) {
            stallGuardRef.current = { at: now, count: g.count + 1 };
            if (tryAdvanceEntry()) return;
          } else {
            stallGuardRef.current = { at: now, count: 1 };
            return;
          }
        }
      }
      const liveStallNetwork = modeRef.current === 'live' && data?.fatal === true
        && data?.type === Hls.ErrorTypes.NETWORK_ERROR;
      if (liveStallNetwork && tryAdvanceEntry()) return;
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
          clearFirstFrameWatchdog();
          setError(t('Live playback failed — try again'));
          setLoading(false);
          markPreviewError();
          break;
        case Hls.ErrorTypes.MEDIA_ERROR:
          if (mediaRetries < 1) {
            mediaRetries += 1;
            hls.recoverMediaError();
            break;
          }
          clearFirstFrameWatchdog();
          setError(t('Live playback failed — try again'));
          setLoading(false);
          markPreviewError();
          break;
        default:
          clearFirstFrameWatchdog();
          setError(t('Live playback failed — try again'));
          setLoading(false);
          markPreviewError();
          break;
      }
    });

    if (startPos >= 0 && modeRef.current !== 'replay') hls.startLoad(startPos);
    else if (modeRef.current !== 'replay') hls.startLoad();
  }, [destroyHls, onAdRotation, clearRetry, clearFirstFrameWatchdog, markPreviewError, tryAdvanceEntry, recreateSessionInvisible, armQualityPin]); // ponytail: maybeTuneCaptionLiveSync removed — only called from a separate useEffect, never inside createHlsPlayer; including it caused CC toggle to cascade into HLS player destroy/recreate

  // Cleanup player on unmount
  const cleanup = useCallback(() => {
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
    setNotFound(false);
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
  // Retries up to LIVE_SESSION_MAX_RETRIES with exponential backoff on any
  // error (not just AbortError), and consumes a pre-warmed session from
  // liveSessionPrefetchRef when available.
  useEffect(() => {
    let cancelled = false;
    // Start the hls.js chunk load in parallel with the session POST — the
    // ~900KB dynamic import used to sit on the playback critical path
    // (App.tsx also preloads it when the Channels tab renders; this covers
    // any other open path and makes the two fetches overlap instead of
    // chaining).
    void import('hls.js').catch(() => {});

    (async () => {
      sessionPendingRef.current = true;
      // Pre-loop setup: invisible recreate / loading state, body construction
      if (invisibleRecreateRef.current) {
        invisibleRecreateRef.current = false;
      } else {
        setLoading(true);
      }
      setError(null);
      setNotFound(false);
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

      // Consume a pre-warmed session (from channel hover) if the URL matches.
      // ponytail: skip the entire retry loop — the session is ready.
      const prefetched =
        liveSessionPrefetchRef?.current?.url === activeEntry.url
          ? liveSessionPrefetchRef.current.session
          : null;
      if (prefetched) {
        if (liveSessionPrefetchRef) liveSessionPrefetchRef.current = null;
        if (cancelled) return;
        try {
          await handleLiveSessionSuccess(prefetched, cancelled);
        } catch (err) {
          if (cancelled) return;
          setError(err instanceof Error ? err.message : t('Failed to start live stream'));
          setLoading(false);
          markPreviewError();
        }
        return;
      }

      // Normal flow: POST /api/preview/live with per-attempt stall guard
      // and exponential backoff on failure.
      for (let attempt = 0; attempt <= LIVE_SESSION_MAX_RETRIES; attempt++) {
        if (cancelled) return;
        const controller = new AbortController();
        const stallTimer = window.setTimeout(() => controller.abort(), SESSION_STALL_MS);
        try {
          const res = await apiPost<PreviewSessionResponse>('/api/preview/live', body, { signal: controller.signal });
          window.clearTimeout(stallTimer);
          if (cancelled) {
            if (res?.session_id) apiDelete(`/api/preview/session/${res.session_id}`).catch(() => {});
            return;
          }
          if (!res) {
            if (tryAdvanceEntry()) return;
            setError(t('No response from server'));
            setLoading(false);
            markPreviewError();
            return;
          }
          await handleLiveSessionSuccess(res, cancelled);
          return; // success — exit the loop
        } catch (err) {
          window.clearTimeout(stallTimer);
          if (cancelled) return;
          if (attempt < LIVE_SESSION_MAX_RETRIES) {
            // Delete stale session if one was created
            if (sessionIdRef.current) {
              void apiDelete(`/api/preview/session/${sessionIdRef.current}`).catch(() => {});
              sessionIdRef.current = null;
            }
            await new Promise((r) => setTimeout(r, Math.pow(2, attempt) * 1000));
            continue;
          }
          // All retries exhausted
          if (tryAdvanceEntry()) return;
          const stalled = err instanceof DOMException && err.name === 'AbortError';
          setError(stalled
            ? t('Live session is taking too long to start')
            : (err instanceof Error ? err.message : t('Failed to start live stream')));
          setLoading(false);
          markPreviewError();
          return;
        }
      }
    })();

    /** Shared success path: set session state, warm rail, attach player. */
    async function handleLiveSessionSuccess(
      res: PreviewSessionResponse,
      isCancelled: boolean,
    ): Promise<void> {
      sessionPendingRef.current = false;
      if (isCancelled) {
        if (res?.session_id) apiDelete(`/api/preview/session/${res.session_id}`).catch(() => {});
        return;
      }
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
          if (isCancelled) return;
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
    }

    return () => {
      cancelled = true;
      // Clear prefetch for this URL if it wasn't consumed
      if (liveSessionPrefetchRef?.current?.url === activeEntry.url) {
        liveSessionPrefetchRef.current = null;
      }
      destroyHls();
    };
  }, [activeEntry.url, activeEntry.headers, activeEntry.platform, vodUrl, abortRef, createHlsPlayer, destroyHls, retryTick, markPreviewError, clearRetry, tryAdvanceEntry, liveSessionPrefetchRef]);

  // Sync transport state from the video element
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const bufferingHandle = attachPreviewBufferingListeners(video, (stalling) => {
      if (stalling && !hasPlayedOnceRef.current) return;
      setBuffering(stalling);
    });
    bufferingHandleRef.current = bufferingHandle;
    const onTimeClearBuffer = () => {
      if (!hasPlayedOnceRef.current) return;
      if (video.readyState >= 3 && !video.paused && video.buffered.length) {
        const ahead = video.buffered.end(video.buffered.length - 1) - video.currentTime;
        if (ahead > 0.15) bufferingHandle.clearStall();
      }
    };
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
    const onTime = () => {
      setRailTime(video.currentTime);
      // Caption clock anchor — promotes queued blocks whose window the video
      // reached (and hides stale ones after live-sync seeks).
      captionClockSync();
    };
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
    // MANIFEST_PARSED no longer ends the spinner: a manifest is just the
    // playlist facade and its bytes arrive before any decodable frame — a dead
    // upstream thus showed a black player instead of the spinner. Spinner ends
    // on the first DECODED frame (loadeddata) instead.
    const onLoadedData = () => {
      clearFirstFrameWatchdog();
      setLoading(false);
      clearRetry();
    };
    const onMediaError = () => {
      if (!sessionPendingRef.current) failFirstFrameWatchdog();
    };
    const onPlayingMarkPlayed = () => { hasPlayedOnceRef.current = true; };
    video.addEventListener('play', onPlay);
    video.addEventListener('pause', onPause);
    video.addEventListener('volumechange', onVolumeChange);
    video.addEventListener('timeupdate', onTime);
    video.addEventListener('durationchange', onDuration);
    video.addEventListener('timeupdate', onTimeClearBuffer);
    video.addEventListener('playing', onPlayingMarkPlayed);
    video.addEventListener('error', onMediaError);
    video.addEventListener('playing', onFirstFrame);
    video.addEventListener('timeupdate', onFirstFrame);
    video.addEventListener('canplay', onPlayingMarkPlayed);
    video.addEventListener('loadeddata', onLoadedData);
    return () => {
      bufferingHandle.detach();
      bufferingHandleRef.current = null;
      video.removeEventListener('play', onPlay);
      video.removeEventListener('pause', onPause);
      video.removeEventListener('volumechange', onVolumeChange);
      video.removeEventListener('timeupdate', onTime);
      video.removeEventListener('durationchange', onDuration);
      video.removeEventListener('playing', onPlayingMarkPlayed);
      video.removeEventListener('playing', onFirstFrame);
      video.removeEventListener('error', onMediaError);
      video.removeEventListener('timeupdate', onFirstFrame);
      video.removeEventListener('timeupdate', onTimeClearBuffer);
      video.removeEventListener('canplay', onPlayingMarkPlayed);
      video.removeEventListener('loadeddata', onLoadedData);
    };
  }, [captionClockSync, clearFirstFrameWatchdog, clearRetry, failFirstFrameWatchdog]);

  useEffect(() => {
    clearFirstFrameWatchdog();
    if (!loading) return;
    firstFrameTimerRef.current = window.setTimeout(failFirstFrameWatchdog, FIRST_FRAME_STALL_MS);
    return clearFirstFrameWatchdog;
  }, [activeEntry.url, clearFirstFrameWatchdog, failFirstFrameWatchdog, loading, retryTick]);

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
    // ponytail: defensive guard — clear stale timer from a previous switchToReplay
    // call that didn't reach the interval setup (e.g. early return on !archiveReadyRef).
    if (replayTimerRef.current != null) {
      window.clearInterval(replayTimerRef.current);
      replayTimerRef.current = null;
    }
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
    // Lightweight path: reload the snapshot URL on the EXISTING HLS instance
    // instead of destroyHls + createHlsPlayer (which caused a 1-2s stall
    // every 30s). hls.loadSource() re-fetches the manifest and re-parses it
    // without tearing down the player, buffer, or event handlers. The
    // MANIFEST_PARSED handler re-applies replay positioning via startLoad().
    if (replayTimerRef.current != null) window.clearInterval(replayTimerRef.current);
    replayTimerRef.current = window.setInterval(() => {
      const v = videoRef.current;
      const hls = hlsRef.current;
      if (!v || !hls || modeRef.current !== 'replay') return;
      // A user seek is in flight (250ms debounce) — skip this tick or the
      // resnapshot would race against the seek's own loadSource.
      if (pendingReplaySeekRef.current != null) return;
      const sid = sessionIdRef.current;
      if (!sid || !archiveReadyRef.current) return;
      const pos = Math.max(0, v.currentTime);
      // Cache-bust forces a fresh ENDLIST snapshot from the backend.
      hls.loadSource(replaySnapshotUrl(sid));
      // The MANIFEST_PARSED handler calls startLoad(startPos) with the
      // closure-captured startPos from the INITIAL createHlsPlayer call.
      // For resnapshots we need the CURRENT position — override after parse.
      const HlsCtor = hlsCtorRef.current;
      if (HlsCtor) {
        let landed = false;
        const onParsed = () => {
          if (landed) return;
          landed = true;
          window.clearTimeout(safety);
          hls.off(HlsCtor.Events.MANIFEST_PARSED, onParsed);
          hls.startLoad(pos);
        };
        const safety = window.setTimeout(() => {
          if (!landed) {
            landed = true;
            hls.off(HlsCtor.Events.MANIFEST_PARSED, onParsed);
          }
        }, 10_000);
        hls.on(HlsCtor.Events.MANIFEST_PARSED, onParsed);
      }
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

  // --- Twitch clip mini-preview (identical to the preview clip buttons) ---
  const showClipNotice = useCallback((msg: string) => {
    setClipNotice(msg);
    if (clipNoticeTimerRef.current != null) window.clearTimeout(clipNoticeTimerRef.current);
    clipNoticeTimerRef.current = window.setTimeout(() => setClipNotice(null), 3500);
  }, []);

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
  // Captions toggle lit/unlit — ON lights up with the platform accent
  // (same color as the transport shadow), OFF dims the plain transport
  // button so the state reads at a glance. Captions only exist for
  // twitch/kick, so the accent map never needs the youtube fallback.
  const ccAccent = ctrlPlatform === 'kick'
    ? { border: 'border-[#53fc18]', bg: 'bg-[#53fc18]/15', text: 'text-[#53fc18]', hover: 'hover:bg-[#53fc18]/30' }
    : { border: 'border-[#9146FF]', bg: 'bg-[#9146FF]/15', text: 'text-[#9146FF]', hover: 'hover:bg-[#9146FF]/30' };
  const ccBtnLit = `border-2 ${ccAccent.border} ${ccAccent.bg} ${ccAccent.text} p-1.5 ${platformButtonShadow(ctrlPlatform)} ${ccAccent.hover}`;

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
  // level's targetduration — 0 × 2s = 0 with the zero-count target, so the
  // true edge IS liveSyncPosition (the sub-second part clamp is hls.js's
  // liveSyncPosition, not part of the edge lag).
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
  // Render-scope mirrors for the caption clock anchor (refs keep the SSE +
  // timeupdate listeners on stable callbacks).
  captionEdgeRef.current = liveEdgeSec;
  archiveDurationRef.current = archiveDuration;
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

  /**
   * Open the Twitch clip mini-preview at the live playhead (120s window, user
   * trims there and creates the clip) — same flow as the preview clip buttons
   * (App.openPreviewTwitchClip / ChannelExplorePopup.openExploreTwitchClip).
   * Requires a DVR VOD URL (vodUrl, resolved by App.liveArchiveContext); the
   * channel page alone has no VOD timeline to select from.
   */
  /** P1-1: vodUrl is the channel's newest PUBLIC cached VOD — it only carries
   *  the CURRENT broadcast's timeline when that broadcast is the cached one
   *  (live + cached, or DVR replay). When the current broadcast is not cached
   *  yet (stream live minutes ago) or is members-only, vodUrl points at a
   *  PREVIOUS broadcast: applying the live playhead to its timeline would
   *  clip the wrong content. Verify the cached VOD row's created_at matches
   *  the current session start. Fail closed while the clock is unmapped. */
  const isVodForCurrentSession = useCallback((url: string): boolean => {
    const sessionStart = liveSessionStartEpoch();
    if (!Number.isFinite(sessionStart)) return false;
    const vod = (channel?.vodVideos ?? []).find((v) => buildVodUrl(v) === url);
    const created = vod?.created_at ? Date.parse(vod.created_at) / 1000 : Number.NaN;
    return Number.isFinite(created)
      && Math.abs(created - sessionStart) <= LIVE_VOD_SESSION_MATCH_SEC;
  }, [channel?.vodVideos, liveSessionStartEpoch]);

  const handleFastClip = useCallback(() => {
    const platform = (activeEntry.platform || '').toLowerCase();
    // Twitch path: slug from the playing entry's URL or channel.
    // Kick/YouTube path: the channel's twitchSlug resolves the twin VOD.
    const twitchSlug = channel?.twitchSlug;
    const slug = platform === 'twitch'
      ? (liveChatSlugFromUrl(activeEntry.url, platform) ?? channelSlug ?? '')
      : (twitchSlug ?? '');
    if (!slug) {
      showClipNotice(t('Channel login missing — cannot open the Twitch editor'));
      return;
    }
    // Twitch platform: use vodUrl (existing behavior).
    if (platform === 'twitch') {
      const url = vodUrl ?? activeEntry.url;
      const vodId = archiveVideoIdFromUrl(url);
      if (!vodId) {
        showClipNotice(t('Not a Twitch VOD URL'));
        return;
      }
      if (vodUrl && !isVodForCurrentSession(url)) {
        showClipNotice(t('Not a Twitch VOD URL'));
        return;
      }
      try { videoRef.current?.pause(); } catch { /* ignore */ }
      pauseOtherPreviews();
      setClipPopup({
        url,
        broadcasterLogin: slug,
        vodId,
        playheadSec: currentSec,
        vodDurationSec: archiveDuration,
        reuseSession: sessionIdRef.current
          ? { sessionId: sessionIdRef.current, trimTimeline: false }
          : null,
      });
      return;
    }
    // Kick/YouTube path: find a matching twitch VOD for the current session.
    if (!twitchSlug) {
      showClipNotice(t('No Twitch VOD for this session'));
      return;
    }
    const twitchVod = (channel?.vodVideos ?? []).find((v) => {
      const plat = (v.platform ?? '').toLowerCase();
      return plat === 'twitch' && v.url && isVodForCurrentSession(buildVodUrl(v));
    });
    if (!twitchVod) {
      showClipNotice(t('No Twitch VOD for this session'));
      return;
    }
    const twitchUrl = buildVodUrl(twitchVod);
    try { videoRef.current?.pause(); } catch { /* ignore */ }
    pauseOtherPreviews();
    setClipPopup({
      url: twitchUrl,
      broadcasterLogin: twitchSlug,
      vodId: twitchVod.id,
      playheadSec: currentSec,
      vodDurationSec: archiveDuration,
      reuseSession: sessionIdRef.current
        ? { sessionId: sessionIdRef.current, trimTimeline: false }
        : null,
    });
  }, [activeEntry.url, activeEntry.platform, channelSlug, channel, vodUrl, currentSec, archiveDuration, showClipNotice, t, isVodForCurrentSession]);

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
  // seek inside the retained ~20s live back-buffer. hls.js retains
  // LIVE_BACK_BUFFER_SEC behind the playhead and the finite live timeline
  // (liveDurationInfinity false) makes currentTime seeks land in it. The
  // listener is window-level, so it keeps working while the auto-hidden
  // controls are pointer-events-none.
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
      // (or past) the live edge. No-op before the edge is known (edgeSec 0).
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
  }, [railDisabled, railMax, handleRailChange, computeLiveEdgeSec]);

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
      <div className="flex flex-1 min-h-0 overflow-hidden" style={{ minHeight: 0 }}>
      <div
        style={{
          flex: 1,
          minWidth: 0,
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

        {notFound && (
          <form
            data-live-not-found
            className="absolute inset-0 z-[2] flex flex-col items-center justify-center gap-3 bg-black/85 text-center px-6"
            onSubmit={(e) => {
              e.preventDefault();
              const name = missingName.trim();
              if (name) onNotFoundChannel?.(name);
            }}
          >
            <div className="flex items-center gap-1.5 text-zinc-400 text-[10px] font-mono uppercase tracking-widest">
              <Search size={12} />
              {t('Channel not found')}
            </div>
            <input
              type="text"
              value={missingName}
              onChange={(e) => setMissingName(e.target.value)}
              placeholder={t('Kick or YouTube name')}
              aria-label={t('Kick or YouTube name')}
              autoFocus
              className="w-full max-w-[240px] rounded-md border-2 border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-zinc-100 outline-none focus:border-white"
            />
            <button
              type="submit"
              disabled={!missingName.trim()}
              className="rounded-md border-2 border-zinc-600 bg-zinc-800 px-3 py-1 text-[10px] font-black uppercase tracking-wider text-zinc-200 hover:border-white hover:text-white disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            >
              {t('Open channel')}
            </button>
          </form>
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
        {!error && ((!sessionPendingRef.current && (mode === 'live' || mode === 'replay')) || !loading) && (
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

              <div
                className="relative"
                data-player-menu
                data-volume-menu
                onMouseEnter={() => setVolumeMenuOpen(true)}
                onMouseLeave={() => setVolumeMenuOpen(false)}
              >
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); toggleMute(); }}
                  title="Volume"
                  className={transportBtn}
                >
                  {muted || volume === 0 ? <VolumeX size={15} /> : <Volume2 size={15} />}
                </button>
                {volumeMenuOpen && (
                  <div className="absolute left-full bottom-0 z-30 ml-1.5 flex items-center gap-2 border-2 border-zinc-600 bg-zinc-950 px-2.5 py-2 shadow-lg">
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

              {/* Live captions toggle — CC overlay over the video. Rendered
                  when the gate reports available OR pending (both allow an
                  explicit caption stream — pending downloads the model on
                  first use); truly unavailable → no button, no overlay. */}
              {captionsAvailable && (
                <>
                  <button
                    type="button"
                    onClick={() => setCaptionsEnabled((on) => !on)}
                    aria-pressed={captionsEnabled}
                    aria-label={captionsEnabled ? t('Hide captions') : t('Live captions')}
                    title={captionsEnabled ? t('Hide captions') : t('Live captions')}
                    className={captionsEnabled ? ccBtnLit : `${transportBtn} opacity-40`}
                  >
                    <Captions size={15} />
                  </button>
                  {/* Caption language — per-session translate-target override
                      (pt-BR / English / Español) for the live captions,
                      INSIDE the player next to the CC toggle. ?lang= on the
                      caption SSE makes the backend NLLB target follow the
                      selection; "Auto" = the app language (default). */}
                  <div className="relative" data-caption-lang-menu>
                    <button
                      type="button"
                      onClick={() => setCaptionLangMenuOpen((o) => !o)}
                      aria-haspopup="menu"
                      aria-expanded={captionLangMenuOpen}
                      aria-label={captionTranslationAvailable ? t('Caption language') : t('Caption translation unavailable')}
                      title={captionTranslationAvailable ? t('Caption language') : t('Caption translation unavailable')}
                      disabled={!captionTranslationAvailable}
                      className={`${transportBtn} ${captionTranslationAvailable ? '' : 'opacity-40'}`}
                    >
                      <Languages size={15} aria-hidden />
                    </button>
                    {captionLangMenuOpen && (
                      <div className="absolute bottom-full left-0 z-30 mb-1.5 flex flex-col border-2 border-zinc-600 bg-zinc-950 shadow-lg">
                        {CAPTION_LANG_OPTIONS.map((o) => (
                          <button
                            key={o.value}
                            type="button"
                            onClick={() => { setCaptionLang(o.value); setCaptionLangMenuOpen(false); }}
                            className={`px-2 py-1 text-left text-[10px] font-bold ${captionLang === o.value ? ccAccent.text : 'text-zinc-300 hover:bg-zinc-800'}`}
                          >
                            {o.label}
                          </button>
                        ))}
                        <button
                          type="button"
                          onClick={() => { setCaptionLang(null); setCaptionLangMenuOpen(false); }}
                          className={`px-2 py-1 text-left text-[10px] font-bold ${captionLang === null ? ccAccent.text : 'text-zinc-300 hover:bg-zinc-800'}`}
                        >
                          Auto
                        </button>
                      </div>
                    )}
                  </div>
                  {/* Caption size menu — A−/A+ buttons ±2px clamp,
                      persisted via the same localStorage key. Outside-click
                      handled by the shared mousedown listener. Uses the same
                      Type icon as the quality menu's gear. */}
                  <div className="relative" data-caption-font-size-menu>
                    <button
                      type="button"
                      onClick={() => setCaptionFontSizeMenuOpen((o) => !o)}
                      aria-haspopup="menu"
                      aria-expanded={captionFontSizeMenuOpen}
                      aria-label={t('Caption size')}
                      title={t('Caption size')}
                      className={transportBtn}
                    >
                      <Type size={15} aria-hidden />
                    </button>
                    {captionFontSizeMenuOpen && (
                      <div className="absolute bottom-full right-0 z-30 mb-1.5 flex items-center gap-1 border-2 border-zinc-600 bg-zinc-950 px-2 py-1.5 shadow-lg">
                        <button
                          type="button"
                          onClick={() => setCaptionFontSize((n) => Math.max(CAPTION_FONT_MIN_PX, n - 2))}
                          title={t('Smaller captions')}
                          className="text-[10px] font-bold text-zinc-300 hover:text-white px-1 py-0.5"
                        >
                          A−
                        </button>
                        <span className="font-mono text-[9px] text-zinc-400">{captionFontSize}px</span>
                        <button
                          type="button"
                          onClick={() => setCaptionFontSize((n) => Math.min(CAPTION_FONT_MAX_PX, n + 2))}
                          title={t('Larger captions')}
                          className="text-[10px] font-bold text-zinc-300 hover:text-white px-1 py-0.5"
                        >
                          A+
                        </button>
                      </div>
                    )}
                  </div>
                </>
              )}

              {/* Twitch clip — rightmost of the left-side transport
                  buttons. Opens the in-app mini-preview at the live playhead
                  (120s window, user trims 5..60s, then the Twitch editor).
                  Shows for Twitch platform OR when the channel has a
                  twitchSlug (Kick/YouTube lives with a twin Twitch stream). */}
              {(ctrlPlatform === 'twitch' || !!channel?.twitchSlug) && (
                <button
                  type="button"
                  onClick={handleFastClip}
                  title={t('Open the Twitch clip mini-preview at the playhead')}
                  className={`${transportBtn} flex items-center gap-1.5`}
                >
                  <TwitchLogoIcon size={15} className="shrink-0" />
                  {/* Logo already says Twitch — label stays bare "clip" (user request). */}
                  <span className="text-[9px] font-bold uppercase tracking-wider whitespace-nowrap leading-none">clip</span>
                </button>
              )}

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

        {/* Live captions overlay — draggable inside the video area. The
            entire overlay is pointer-events-auto for drag; the text
            stopPropagation prevents stealing video click-to-pause. */}
        {captionsHeard && captionsEnabled && caption && !error && (
          <div
            data-live-captions-overlay
            className="pointer-events-auto absolute inset-x-0 bottom-16 z-[5] flex justify-center px-4 select-none"
            style={{
              transform: captionOverlayOffset.x !== 0 || captionOverlayOffset.y !== 0
                ? `translate(${captionOverlayOffset.x}px, ${captionOverlayOffset.y}px)`
                : undefined,
            }}
            onPointerDown={(e) => {
              e.stopPropagation();
              captionOverlayDragStartRef.current = {
                pointerX: e.clientX,
                pointerY: e.clientY,
                offsetX: captionOverlayDragRef.current.offsetX,
                offsetY: captionOverlayDragRef.current.offsetY,
              };
              const onMove = (ev: PointerEvent) => {
                const st = captionOverlayDragStartRef.current;
                if (!st) return;
                const dx = ev.clientX - st.pointerX;
                const dy = ev.clientY - st.pointerY;
                const nx = Math.max(-200, Math.min(200, st.offsetX + dx));
                const ny = Math.max(-100, Math.min(100, st.offsetY + dy));
                captionOverlayDragRef.current = { offsetX: nx, offsetY: ny };
                setCaptionOverlayOffset({ x: nx, y: ny });
              };
              const onUp = () => {
                captionOverlayDragStartRef.current = null;
                window.removeEventListener('pointermove', onMove);
                window.removeEventListener('pointerup', onUp);
              };
              window.addEventListener('pointermove', onMove);
              window.addEventListener('pointerup', onUp);
            }}
          >
            <p
              className={`${captionFontSize >= 34 ? 'line-clamp-4' : captionFontSize >= 24 ? 'line-clamp-3' : 'line-clamp-2'} max-w-[95%] rounded bg-black/60 px-3 py-1.5 text-center font-semibold leading-snug text-zinc-100 [text-shadow:0_1px_2px_rgba(0,0,0,0.9)] backdrop-blur-[2px] cursor-grab active:cursor-grabbing`}
              style={{ fontSize: captionFontSize }}
              onPointerDown={(e) => e.stopPropagation()}
            >
              {caption.text}
            </p>
          </div>
        )}
      </div>
      {/* Docked live chat — always mounted when chat sources exist (preloads
          EventSource + /api/chat/history on live open); visually hidden
          when chatOpen is false so the EventSource starts immediately. */}
      {!isFullscreen && chatSources.length > 0 && (
        <div
          className={chatOpen ? 'flex min-h-0 overflow-hidden shrink-0' : 'hidden w-0 overflow-hidden'}
          style={chatOpen ? { width: LIVE_CHAT_PANEL_W, minHeight: 0, overflow: 'hidden' } : { position: 'absolute', height: 0, width: 0, overflow: 'hidden' }}
          aria-hidden={!chatOpen}
        >
          <LiveChatPanel
            sources={chatSources}
            onClose={toggleChat}
          />
        </div>
      )}
      </div>
      {clipPopup && (
        <TwitchClipPopup
          url={clipPopup.url}
          broadcasterLogin={clipPopup.broadcasterLogin}
          vodId={clipPopup.vodId}
          playheadSec={clipPopup.playheadSec}
          vodDurationSec={clipPopup.vodDurationSec}
          reuseSession={clipPopup.reuseSession}
          // The clip title defaults to the VOD's title (user-mandated: the
          // clip keeps the ORIGINAL title) — sent as vodrip_title so the
          // extension fills the editor's required field.
          vodTitle={activeEntry.title ?? ''}
          // Ladder-derived: the live popup's shared-ladder rank + 50 headroom
          // (same pattern as the explore player's clip popup).
          zIndex={(zIndex ?? LIVE_POPUP_ACTIVE_Z) + 50}
          // Inherit the live popup's volume so opening the clip window never
          // resets the user's level.
          initialVolume={volume}
          onClose={() => setClipPopup(null)}
        />
      )}
    </div>,
    // Mount inside #explore-portal with the explore/local-file players so all
    // popups share one DOM subtree (it is a static mount point with no stacking
    // context — see index.css), letting the shared ladder z compete at root
    // level for cross-type bring-to-front (live vs explore vs search panel).
    document.getElementById('explore-portal') ?? document.body,
  );
}
