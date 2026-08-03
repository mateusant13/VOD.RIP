import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ExternalLink, Maximize2, Minimize2, Pause, Play, Volume2, VolumeX, RefreshCw } from 'lucide-react';
import { apiDelete, apiPost } from '../hooks/useApiClient';
import type { PanelSize, PreviewSessionResponse } from '../types';
import {
  PanelResizeHandles,
  panelResizeHandleInset,
  type ResizeEdge,
} from '../explorePopupUtils';
import {
  LIVE_PANEL_MIN_W,
  panelPosAfterResize,
  startPanelResizeDrag,
} from '../layoutUtils';
import PreviewQualityMenu from '../PreviewQualityMenu';
import { createTwitchAdRotationHandler, twitchAdBlockHlsConfig } from '../twitchAdBlock';
import { filterLiveLevels, liveBroadcastPositionSec, parsePlaylistTotalSec, replaySeekTarget } from '../livePlayerLevels';
import { previewRetryAfterError, type PreviewRetryState } from '../previewRetry';
import { fmtDuration } from '../formatters';
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
  channelName: string;
  onClose: () => void;
  /** Platform slug for the open-channel button (twitchSlug/kickSlug/youtubeSlug). */
  channelSlug?: string;
  /** Channel's current (in-progress) VOD URL — DVR REPLAY archive source. */
  vodUrl?: string;
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
const POPUP_MIN_H = 200;
const RESIZE_MARGIN = 32; // keep at least 16px of the popup on screen while resizing
/** Re-snapshot the archive playlist while parked in REPLAY (grows while live). */
const REPLAY_RESNAPSHOT_MS = 30_000;

export function LivePlayerPopup({ entry, channelName, onClose, channelSlug, vodUrl }: LivePlayerPopupProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const hlsCtorRef = useRef<typeof Hls | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const sessionRef = useRef<PreviewSessionResponse | null>(null);
  /** Quality policy: platform drives the live level ladder (youtube →
   *  360/720/1080 or 360-anon; twitch/kick → up to source). */
  const sessionPlatformRef = useRef((entry.platform || '').toLowerCase());
  const sizeRef = useRef<PanelSize>({ w: POPUP_WIDTH, h: POPUP_HEIGHT });
  const [position, setPosition] = useState({ x: window.innerWidth - POPUP_WIDTH - 24, y: 80 });
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
    setPreviewRetry(previewRetryAfterError(previewRetryRef.current, entry.url, 'session', wasRetry));
  }, [entry.url]);
  const [drag, setDrag] = useState<DragState>(null);

  // Transport state
  const [paused, setPaused] = useState(false);
  const [muted, setMuted] = useState(true);
  const [volume, setVolume] = useState(1);
  const [volumeMenuOpen, setVolumeMenuOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);

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
    const plat = (entry.platform || '').toLowerCase();
    if (!channelSlug) return null;
    if (plat === 'youtube') return `https://www.youtube.com/@${channelSlug}`;
    if (plat === 'kick') return `https://kick.com/${channelSlug}`;
    return `https://www.twitch.tv/${channelSlug}`;
  }, [entry.platform, channelSlug]);

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
    const hls = new Hls({
      ...twitchAdBlockHlsConfig({ live: true, onAdRotation }),
      enableWorker: true,
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
        // Default stays the level closest to 360 (main-player convention: set
        // hls.loadLevel AFTER MANIFEST_PARSED — never startLevel before load).
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
  }, [destroyHls, onAdRotation, clearRetry, markPreviewError]);

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

  // Create preview session on mount
  useEffect(() => {
    let cancelled = false;

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
        } = { url: entry.url, is_live: true };
        if (entry.headers) body.headers = entry.headers;
        if (entry.platform) body.platform = entry.platform;
        if (vodUrl) body.vod_url = vodUrl;

        const res = await apiPost<PreviewSessionResponse>('/api/preview/live', body);
        if (cancelled) return;
        if (!res) { setError('No response from server'); setLoading(false); markPreviewError(); return; }

        sessionIdRef.current = res.session_id;
        sessionRef.current = res;
        sessionPlatformRef.current = (entry.platform || '').toLowerCase();
        setArchiveDuration(res.archive_duration ?? 0);

        // Warm the LIVE rail: the backend probes archive_duration only when
        // the replay playlist is fetched, so fetch the snapshot once and sum
        // its EXTINF durations — the rail then spans the whole broadcast from
        // the start instead of sitting at 1s until the first REPLAY switch.
        if (res.archive_url) {
          fetch(replaySnapshotUrl(res.session_id))
            .then((r) => (r.ok ? r.text() : ''))
            .then((text) => {
              const total = parsePlaylistTotalSec(text);
              if (total > 0) setArchiveDuration(total);
            })
            .catch(() => {});
        }

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
          setError(err instanceof Error ? err.message : 'Failed to start live stream');
          setLoading(false);
          markPreviewError();
        }
      }
    })();

    return () => { cancelled = true; destroyHls(); };
  }, [entry.url, entry.headers, entry.platform, vodUrl, abortRef, createHlsPlayer, destroyHls, retryTick, markPreviewError, clearRetry]);

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
    video.addEventListener('play', onPlay);
    video.addEventListener('pause', onPause);
    video.addEventListener('volumechange', onVolumeChange);
    video.addEventListener('timeupdate', onTime);
    video.addEventListener('durationchange', onDuration);
    video.addEventListener('waiting', showBuffering);
    video.addEventListener('playing', clearBuffering);
    video.addEventListener('canplay', clearBuffering);
    return () => {
      video.removeEventListener('play', onPlay);
      video.removeEventListener('pause', onPause);
      video.removeEventListener('volumechange', onVolumeChange);
      video.removeEventListener('timeupdate', onTime);
      video.removeEventListener('durationchange', onDuration);
      video.removeEventListener('waiting', showBuffering);
      video.removeEventListener('playing', clearBuffering);
      video.removeEventListener('canplay', clearBuffering);
    };
  }, [showBuffering, clearBuffering]);

  // Track fullscreen state
  useEffect(() => {
    const onFsChange = () => setIsFullscreen(!!document.fullscreenElement);
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
    const sess = sessionRef.current;
    if (!sid || !video || !sess?.archive_url) return;
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

  const toggleFullscreen = useCallback(() => {
    const el = popupRef.current;
    if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else el.requestFullscreen().catch(() => {});
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
      maxW: viewport.w - RESIZE_MARGIN,
      maxH: viewport.h - RESIZE_MARGIN,
      clampSize: (s) => ({
        w: Math.min(viewport.w - RESIZE_MARGIN, Math.max(LIVE_PANEL_MIN_W, s.w)),
        h: Math.min(viewport.h - RESIZE_MARGIN, Math.max(POPUP_MIN_H, s.h)),
      }),
      onResizeMove: (next) => applyPos(next),
      onResizeEnd: () => applyPos(sizeRef.current),
    });
  }, []);

  // --- Dragging (header bar only, so transport buttons don't fight the drag) ---
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('.live-popup-close') || (e.target as HTMLElement).closest('.live-popup-link')) return;
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

  const transportBtn = 'flex items-center justify-center rounded p-1 text-white/80 hover:bg-white/10 hover:text-white';

  const archiveAvailable = Boolean(sessionRef.current?.archive_url);
  // Live rail: pinned at the archive edge; dragging back opens REPLAY.
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
      className="group"
      data-live-popup
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        width: size.w,
        height: size.h,
        zIndex: 500,
        borderRadius: 8,
        boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
        background: '#111',
        border: '1px solid #333',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <PanelResizeHandles onPointerDown={handleResize} insetPx={panelResizeHandleInset(true)} />

      {/* Header bar — drag handle */}
      <div
        onMouseDown={handleMouseDown}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '6px 10px',
          background: '#1a1a2e',
          borderRadius: '8px 8px 0 0',
          cursor: drag ? 'grabbing' : 'grab',
          userSelect: 'none',
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: 12, color: mode === 'live' ? '#e06c75' : '#61afef', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginRight: 8 }}>
          {mode === 'live' ? '🔴 LIVESTREAM' : '⏪ REPLAY'} — {channelName}{entry.title ? ` — ${entry.title}` : ''}
        </span>

        <span style={{ display: 'flex', alignItems: 'center', gap: 2, flexShrink: 0 }}>
          {channelUrl && (
            <button
              className="live-popup-link"
              onClick={() => window.open(channelUrl, '_blank', 'noopener,noreferrer')}
              style={{
                background: 'none',
                border: 'none',
                color: '#888',
                cursor: 'pointer',
                fontSize: 16,
                lineHeight: 1,
                padding: '2px 6px',
                borderRadius: 4,
                display: 'flex',
                alignItems: 'center',
              }}
              title="Open channel"
            >
              <ExternalLink size={14} />
            </button>
          )}
          <button
            className="live-popup-close"
            onClick={handleClose}
            style={{
              background: 'none',
              border: 'none',
              color: '#888',
              cursor: 'pointer',
              fontSize: 16,
              lineHeight: 1,
              padding: '2px 6px',
              borderRadius: 4,
              flexShrink: 0,
            }}
            title="Close"
          >
            ✕
          </button>
        </span>
      </div>

      {/* Video area */}
      <div
        style={{
          flex: 1,
          position: 'relative',
          background: '#000',
          borderRadius: '0 0 8px 8px',
          overflow: 'hidden',
        }}
      >
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          style={{ width: '100%', height: '100%', objectFit: 'contain' }}
        />

        {loading && (
          <div
            style={{
              position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
              justifyContent: 'center', background: 'rgba(0,0,0,0.6)', color: '#aaa', fontSize: 13,
            }}
          >
            {mode === 'replay' ? 'Loading replay…' : 'Loading live stream…'}
          </div>
        )}

        {error && (
          <div
            style={{
              position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: 10,
              background: 'rgba(0,0,0,0.7)', color: '#e06c75', fontSize: 13,
              padding: 16, textAlign: 'center',
            }}
          >
            <div>{error}</div>
            {previewRetry && (
              <button
                type="button"
                onClick={retryPreview}
                title="Retry this live stream"
                style={{
                  display: 'flex', alignItems: 'center', gap: 6, fontSize: 11,
                  fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
                  color: '#e06c75', border: '1px solid rgba(224,108,117,0.5)',
                  background: 'rgba(224,108,117,0.08)', padding: '4px 10px', cursor: 'pointer',
                }}
              >
                <RefreshCw size={12} />
                Retry
              </button>
            )}
          </div>
        )}

        {!loading && !error && buffering && (
          <div
            style={{
              position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
              justifyContent: 'center', background: 'rgba(0,0,0,0.5)', color: '#aaa', fontSize: 12,
              pointerEvents: 'none',
            }}
          >
            Buffering…
          </div>
        )}

        {/* Transport controls — same layout as the mini preview player: a
            timeline row (current/total timestamps + rail) above the transport
            row (play, volume, live-edge, quality, fullscreen). No trim here. */}
        {!loading && !error && (
          <div
            data-live-transport
            className="px-2 py-1.5"
            style={{
              position: 'absolute',
              insetInline: 0,
              bottom: 0,
              zIndex: 10,
              background: 'linear-gradient(to top, rgba(0,0,0,0.85), rgba(0,0,0,0))',
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
                  <div className="absolute bottom-full left-0 z-30 mb-1.5 flex items-center gap-2 rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-2 shadow-lg">
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
                <button
                  type="button"
                  onClick={() => (mode === 'replay' ? switchToLive() : snapToLiveEdge())}
                  title={mode === 'replay' ? 'Return to live' : 'Snap to live edge'}
                  className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-red-500 hover:bg-white/10"
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
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
