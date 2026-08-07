import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ExternalLink, Loader2, Maximize2, Minimize2, Pause, Play, Search, Volume2, VolumeX, RefreshCw, X, AlertCircle } from 'lucide-react';
import { apiDelete, apiPost } from '../hooks/useApiClient';
import { openTwitchClipEditor } from '../twitchClip';
import TwitchLogoIcon from './TwitchLogoIcon';
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
  panelPosAfterResize,
  startPanelResizeDrag,
} from '../layoutUtils';
import PreviewQualityMenu from '../PreviewQualityMenu';
import { platformPreviewCtrlBtn, type PlatformStyleKey } from '../platformStyles';
import { createTwitchAdRotationHandler, twitchAdBlockHlsConfig } from '../twitchAdBlock';
import { filterLiveLevels, liveBroadcastPositionSec, parsePlaylistTotalSec, replaySeekTarget } from '../livePlayerLevels';
import { previewRetryAfterError, type PreviewRetryState } from '../previewRetry';
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
  /** Channel's current (in-progress) VOD URL — DVR REPLAY archive source. */
  vodUrl?: string;
  /** Open an archive hit in the explore-player flow (App owns the popup stack). */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => void;
  /** Optional saved channels (App state) — unioned into the channel dropdown. */
  savedChannels?: SavedChannel[];
  /** Position cascade offset — 0 = default corner; each sibling steps 28px. */
  cascadeIndex?: number;
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
/** Re-snapshot the archive playlist while parked in REPLAY (grows while live). */
const REPLAY_RESNAPSHOT_MS = 30_000;
/** Live session POST stall budget — after this the popup advances to the next
 *  live entry (or surfaces the error) instead of pinning the spinner. */
const SESSION_STALL_MS = 8_000;

export function LivePlayerPopup({ entry, entries, channelName, onClose, channelSlug, vodUrl, onOpenHit, savedChannels, cascadeIndex = 0 }: LivePlayerPopupProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
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
  const [position, setPosition] = useState(() => ({
    x: window.innerWidth - POPUP_WIDTH - 24 - cascadeIndex * 28,
    y: 80 + cascadeIndex * 28,
  }));
  const posRef = useRef(position);
  const [size, setSize] = useState<PanelSize>({ w: POPUP_WIDTH, h: POPUP_HEIGHT });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Per-media retry state — the live pipeline is a single stage (session
  // create + attach happen together), so every retry re-runs it end-to-end.
  const [previewRetry, setPreviewRetry] = useState<PreviewRetryState | null>(null);
  const previewRetryRef = useRef<PreviewRetryState | null>(null);
  const previewRetryingRef = useRef(false);
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
  /** Twitch clip editor open — transient notice in the transport row. */
  const [clipNotice, setClipNotice] = useState<{ kind: 'error' | 'ok'; text: string } | null>(null);
  const [clipOpening, setClipOpening] = useState(false);
  const clipNoticeTimerRef = useRef<number | null>(null);
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
  const replayTimerRef = useRef<number | null>(null);

  // Keep refs in sync with state (drag/resize use the latest size without re-subscribing)
  useEffect(() => { sizeRef.current = size; }, [size]);
  useEffect(() => { posRef.current = position; }, [position]);
  useEffect(() => { modeRef.current = mode; }, [mode]);

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

  // Handle level selection (original hls.levels indices)
  const handleQualitySelect = useCallback((index: number) => {
    if (modeRef.current === 'replay') return; // snapshot is single-level — no switching
    if (hlsRef.current) {
      hlsRef.current.currentLevel = index;
      setCurrentLevel(index);
    }
    setQualityMenuOpen(false);
  }, []);

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

  // Close menus on outside click
  useEffect(() => {
    if (!qualityMenuOpen && !volumeMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(e.target as Node)) {
        setQualityMenuOpen(false);
        setVolumeMenuOpen(false);
      }
    };
    window.addEventListener('mousedown', handler);
    return () => window.removeEventListener('mousedown', handler);
  }, [qualityMenuOpen, volumeMenuOpen]);

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

  /** Create an hls.js instance for *src*; live mode defaults 360p after parse. */
  const createHlsPlayer = useCallback(async (src: string, startPos: number): Promise<any | null> => {
    const video = videoRef.current;
    if (!video) return null;
    if (!hlsCtorRef.current) {
      try {
        hlsCtorRef.current = (await import('hls.js')).default;
      } catch {
        setError('HLS not supported in this browser');
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
      setError('HLS not supported in this browser');
      setLoading(false);
      markPreviewError();
      return null;
    }
    destroyHls();
    const replay = modeRef.current === 'replay';
    // Replay: autoStartLoad off — the position is applied in MANIFEST_PARSED
    // (startLoad before the manifest parses is a no-op, which would start the
    // snapshot at 0 instead of the dragged time).
    // hls.js surface mirrors the mini preview player exactly (App.tsx):
    // enableWorker, lowLatencyMode off, small buffers, long timeouts, the
    // adblock pLoader, and the live sync knobs. capLevelToPlayerSize is
    // DELIBERATELY absent — the mini preview caps to its panel size, the
    // live popup must keep the stream's source resolution.
    // startLevel: 0 = the LOWEST manifest level — hls.js 1.6.2 sorts levels
    // ascending (dist/hls.mjs: "sort levels from lowest to highest"), so
    // index 0 is the smallest fragment → fastest first frame. MANIFEST_PARSED
    // then moves loadLevel to the policy default (closest to 360p).
    const hls = new Hls({
      ...twitchAdBlockHlsConfig({ live: true, onAdRotation }),
      enableWorker: true,
      startLevel: 0,
      lowLatencyMode: false,
      maxBufferLength: 6,
      maxMaxBufferLength: 12,
      backBufferLength: 12,
      startFragPrefetch: true,
      fragLoadingTimeOut: 20000,
      manifestLoadingTimeOut: 10000,
      testBandwidth: false,
      liveSyncDuration: 3,
      liveMaxLatencyDuration: 10,
      liveDurationInfinity: true,
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
        // Default stays the level closest to 360, set AFTER MANIFEST_PARSED
        // (config startLevel: 0 already picked the lowest level for the first
        // fragment; loadLevel governs everything from here on).
        const isYoutube = sessionPlatformRef.current === 'youtube';
        const { levels: filtered, defaultIndex } = filterLiveLevels(
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
        if (defaultIndex >= 0 && defaultIndex < hls.levels.length) {
          hls.loadLevel = defaultIndex;
          setCurrentLevel(defaultIndex);
        }
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
          if (networkRetries < 2) {
            networkRetries += 1;
            window.setTimeout(() => {
              if (hlsRef.current !== hls) return;
              const t = videoRef.current?.currentTime;
              hls.startLoad(t && t > 0 ? t : -1);
            }, networkRetries * 500);
            break;
          }
          setError('Live playback failed — try again');
          setLoading(false);
          break;
        case Hls.ErrorTypes.MEDIA_ERROR:
          hls.recoverMediaError();
          break;
        default:
          setError('Live playback failed — try again');
          setLoading(false);
          break;
      }
    });

    if (startPos >= 0 && modeRef.current !== 'replay') hls.startLoad(startPos);
    else if (modeRef.current !== 'replay') hls.startLoad();
    return hls;
  }, [destroyHls, onAdRotation, clearRetry, markPreviewError, tryAdvanceEntry]);

  // Cleanup player on unmount
  const cleanup = useCallback(() => {
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
        setLoading(true);
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
        if (cancelled) return;
        window.clearTimeout(stallTimer);
        if (!res) {
          if (tryAdvanceEntry()) return;
          setError('No response from server');
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
            ? 'Live session is taking too long to start'
            : (err instanceof Error ? err.message : 'Failed to start live stream'));
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

  // --- Resize (same [data-panel-resize] pattern as the VOD preview panel) ---
  const handleResize = useCallback((e: React.PointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const startSize = { ...sizeRef.current };
    const startPos = { ...posRef.current };
    const viewport = { w: window.innerWidth, h: window.innerHeight };
    const applyPos = (next: PanelSize) => {
      const p = panelPosAfterResize(edge, startPos, startSize, next, viewport);
      posRef.current = p;
      setPosition(p);
    };
    startPanelResizeDrag(e, edge, sizeRef, setSize, {
      panelEl: popupRef.current,
      maxW: Math.min(viewport.w - RESIZE_MARGIN, LIVE_PANEL_MAX_W),
      maxH: Math.min(viewport.h - RESIZE_MARGIN, LIVE_PANEL_MAX_H),
      clampSize: (s) => ({
        w: Math.min(viewport.w - RESIZE_MARGIN, Math.max(LIVE_PANEL_MIN_W, Math.min(s.w, LIVE_PANEL_MAX_W))),
        h: Math.min(viewport.h - RESIZE_MARGIN, Math.max(LIVE_PANEL_MIN_H, Math.min(s.h, LIVE_PANEL_MAX_H))),
      }),
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
  const transportBtn = platformPreviewCtrlBtn(
    (activeEntry.platform ?? 'kick') as PlatformStyleKey,
    isFullscreen,
    false,
  );

  const showClipNotice = useCallback((kind: 'error' | 'ok', text: string) => {
    if (clipNoticeTimerRef.current) window.clearTimeout(clipNoticeTimerRef.current);
    setClipNotice({ kind, text });
    clipNoticeTimerRef.current = window.setTimeout(() => setClipNotice(null), 4000);
  }, []);

  /** Open Twitch's clip editor for the live broadcast (web editor picks the window). */
  const openLiveTwitchClip = useCallback(async () => {
    const login = (channelSlug || '').trim();
    if (!login) {
      showClipNotice('error', 'Channel login missing — cannot open the Twitch editor');
      return;
    }
    setClipOpening(true);
    try {
      const res = await openTwitchClipEditor({ broadcasterLogin: login });
      showClipNotice('ok', `Twitch clip editor opened — ${res.url}`);
    } catch {
      showClipNotice('error', 'Failed to open the Twitch clip editor');
    } finally {
      setClipOpening(false);
    }
  }, [channelSlug, showClipNotice]);

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
  // seekable end. Used to map currentTime to broadcast-relative seconds.
  const liveEdgeSec = (() => {
    const h = hlsRef.current;
    if (h) {
      const pos = typeof h.liveSyncPosition === 'number' ? h.liveSyncPosition : Number.NaN;
      if (Number.isFinite(pos) && pos > 0) return pos + (h.config.liveSyncDuration ?? 3);
    }
    const v = videoRef.current;
    const s = v?.seekable;
    return s && s.length > 0 ? s.end(s.length - 1) : 0;
  })();
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

  return createPortal(
    <div
      ref={popupRef}
      className="group border-2 border-zinc-700 bg-zinc-950"
      data-live-popup
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        width: size.w,
        height: size.h,
        // Active-state z: an open preview must float above the floating
        // archive search (SEARCH_POPUP_Z); unmount on close restores order.
        zIndex: LIVE_POPUP_ACTIVE_Z,
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
        onMouseDown={handleMouseDown}
        className="flex items-start justify-between gap-2 px-2 py-1.5 bg-zinc-900 border-b-2 border-zinc-800 select-none shrink-0"
        style={{ cursor: drag ? 'grabbing' : 'grab' }}
      >
        <div className="flex items-start gap-1.5 min-w-0">
          <div className="min-w-0">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
              {mode === 'live' ? 'Live stream' : 'Live replay'}
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
              title="Open channel"
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
              title="Search the local archive (transcripts + chat)"
            >
              <Search size={10} className="shrink-0" />
              {searchOpen ? 'CLOSE SEARCH' : 'SEARCH ARCHIVE'}
            </button>
          )}
          <button
            className="live-popup-close text-zinc-500 hover:text-white p-1 shrink-0"
            onClick={handleClose}
            title="Close"
          >
            <X size={16} />
          </button>
        </div>
      </div>

      {/* Video area — same as the mini preview player: click toggles
          play/pause, loading + buffering show spinner overlays. */}
      <div
        style={{
          flex: 1,
          position: 'relative',
          background: '#000',
          overflow: 'hidden',
        }}
      >
        <div
          className="absolute inset-0 z-0 cursor-pointer"
          onClick={() => {
            if (!loading && !error) togglePlay();
          }}
        >
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            style={{ width: '100%', height: '100%', objectFit: 'contain' }}
          />
        </div>

        {loading && (
          <div className="absolute inset-0 z-[1] flex items-center justify-center bg-black/60 pointer-events-none">
            <Loader2 size={40} className="animate-spin text-zinc-300" />
            <span className="ml-3 text-zinc-300 text-xs font-mono">
              {mode === 'replay' ? 'Loading replay…' : 'Loading live stream…'}
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
                title="Retry this live stream"
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

        {/* Docked archive-search panel — never over the video while this
            popup is fullscreen (matches ChannelExplorePopup's pattern). */}
        {!isFullscreen && searchOpen && (
          <div
            className="absolute inset-0 z-20 flex flex-col bg-zinc-950"
          >
            <ArchiveSearchPopup
              embedded
              zIndex={0}
              onClose={() => setSearchOpen(false)}
              onOpenHit={onOpenHit}
              savedChannels={savedChannels}
            />
          </div>
        )}

        {/* Transport controls — same layout as the mini preview player: a
            timeline row (current/total timestamps + rail) above the transport
            row (play, volume, live-edge, quality, fullscreen). No trim here. */}
        {!loading && !error && (
          <div
            data-live-transport
            className="px-2 py-1.5 bg-gradient-to-t from-black/85 to-black/0"
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
              <input
                type="range"
                min={0}
                max={railMax}
                step={0.5}
                value={railValue}
                disabled={railDisabled}
                onChange={(e) => handleRailChange(parseFloat(e.target.value))}
                className={`h-1 flex-1 ${mode === 'replay' ? 'accent-blue-400' : 'accent-red-500'} ${railDisabled ? 'opacity-60' : ''}`}
                aria-label={mode === 'replay' ? 'Seek within replay' : 'Seek back into the broadcast (replay)'}
                title={mode === 'replay'
                  ? 'Replay of the current broadcast — drag to seek'
                  : (railDisabled ? 'Replay unavailable for this channel' : 'Drag back to watch the past part of the stream')}
              />
              <span className="w-11 shrink-0 text-right font-mono text-[9px] text-zinc-400">
                {fmtDuration(displayTotal)}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-1.5">
              <button
                type="button"
                onClick={togglePlay}
                title={paused ? 'Play' : 'Pause'}
                className={transportBtn}
              >
                {paused ? <Play size={15} /> : <Pause size={15} />}
              </button>

              <div className="relative" data-player-menu>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setVolumeMenuOpen((o) => !o);
                  }}
                  title="Volume"
                  className={transportBtn}
                >
                  {muted || volume === 0 ? <VolumeX size={15} /> : <Volume2 size={15} />}
                </button>
                {volumeMenuOpen && (
                  <div className="absolute bottom-full left-0 z-30 mb-1.5 flex items-center gap-2 border-2 border-zinc-600 bg-zinc-950 px-2.5 py-2 shadow-lg">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleMute();
                      }}
                      title={muted ? 'Unmute' : 'Mute'}
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

              <div className="ml-auto flex items-center gap-1.5">
                {(activeEntry.platform || '').toLowerCase() === 'twitch' && (
                  <button
                    type="button"
                    onClick={() => void openLiveTwitchClip()}
                    disabled={clipOpening || !channelSlug?.trim()}
                    className={transportBtn}
                    title={
                      !channelSlug?.trim()
                        ? 'Channel login missing — cannot open the Twitch editor'
                        : 'Open Twitch clip editor for this live stream'
                    }
                  >
                    {clipOpening ? <Loader2 size={15} className="animate-spin" /> : <TwitchLogoIcon size={14} />}
                    <span className="text-[9px] font-bold uppercase tracking-wider">Twitch clip</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => (mode === 'replay' ? switchToLive() : snapToLiveEdge())}
                  title={mode === 'replay' ? 'Return to live' : 'Snap to live edge'}
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
                  title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
                  className={transportBtn}
                >
                  {isFullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
                </button>
              </div>
            </div>
            {clipNotice && (
              <div className={`mt-1 flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-wider ${
                clipNotice.kind === 'error' ? 'text-red-400' : 'text-[#53fc18]'
              }`}>
                <AlertCircle size={11} className="shrink-0" />
                <span className="truncate">{clipNotice.text}</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
