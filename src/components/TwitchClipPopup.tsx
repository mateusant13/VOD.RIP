/**
 * Twitch clip mini-preview — the "Twitch clip" button on the main preview and
 * the explore popup opens this floating player on a 120s window around
 * the click (60s left + 60s right). The user trims a 5..60s
 * selection on the window timeline, then 'Create clip' opens Twitch's own
 * clip editor in the OS default browser with the selection as vodrip_* params
 * (vod_offset = selection END — see twitchClip.ts); the VOD.RIP cookie
 * extension (clip_assist.mjs content script) fills the title, drives the
 * editor's window and clicks Save, then posts the published clip URL to
 * /api/twitch/clips/record so it shows in the app's clip history with a
 * download button. The browser path works with the plain session cookie —
 * no API token scopes or editor role needed.
 *
 * The mini preview reuses the main preview's session machinery: one session
 * per popup with crop_start/crop_end = the window, exactly like App.tsx's
 * openPreview passes the trim range. Sessions created here are cached by VOD
 * URL (_clipSessionCache) and reused on re-open — the popup probes the cached
 * session's master and adopts it instead of POSTing a fresh session (no
 * re-resolve). ponytail: no quality menu here — the session starts at the
 * fast-start tier (360p) and plays the default level; the main/explore
 * previews remain the full quality experience.
 */

import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import { Loader2, Pause, Play, RefreshCw, Volume2, VolumeX, X } from 'lucide-react';
import { apiDelete, apiGet, apiPost } from '../hooks/useApiClient';
import { useI18n } from '../i18n';
import {
  TWITCH_CLIP_MAX_SEC,
  TWITCH_CLIP_MIN_SEC,
  clampClipSelection,
  clipRailDragTarget,
  initialClipSelection,
  openTwitchClipEditorInBrowser,
  reportClipEvent,
  twitchClipDurationError,
  twitchClipWindow,
} from '../twitchClip';
import {
  PREVIEW_SCRUB_HEIGHT,
  attachPreviewBufferingListeners,
  attachProgressivePreview,
  createPreviewSessionWithRetry,
  detachProgressivePreview,
  pinHlsToLowestLevel,
  playPreviewWithAudio,
  resolvePreviewPlayback,
} from '../previewPlayerUtils';
import { fracToSec, secToFrac, trimButtonDeltaForEndpoint } from '../trimUtils';
import { PREVIEW_DEFAULT_VOLUME, panelPosAfterResize, startPanelResizeDrag } from '../layoutUtils';
import { PanelResizeHandles, type ResizeEdge } from '../explorePopupUtils';
import type { PanelSize } from '../types';
import { twitchAdBlockHlsConfig } from '../twitchAdBlock';
import { pauseOtherPreviews, autoPauseOtherPreviews, noteUserUnpause, registerPreviewPlayback } from '../previewPlaybackBus';
import { formatHmsFull } from '../utils';
import ClipDurationAdjustButtons from './ClipDurationAdjustButtons';
import TwitchLogoIcon from './TwitchLogoIcon';

const POPUP_W = 460;

/**
 * Min panel size — the CSS contract that guarantees every button row is 100%
 * visible. Width: the widest row is the Clip trim row — w-9 label (36) +
 * gap (8) + 4 compact duration buttons (~100) + gap (8) + w-11 length
 * readout (44) + a usable rail (~100) + px-2 padding (16) = 312 → 320.
 * Height: header (~40) + video at min width (320 * 9/16 = 180) + trim
 * section (~129) = 349 → 360. Enforced BOTH as CSS min-width/min-height on
 * the panel AND as the resize-time clamp (clampSize), so the panel can never
 * render smaller than its buttons need — during a drag, after a drag, or on
 * mount. Min wins even on a degenerate viewport smaller than the panel
 * (mirrors clampExplorePanelBox: the panel may overflow a tiny viewport
 * rather than become unusable).
 */
const CLIP_PANEL_MIN_W = 320;
const CLIP_PANEL_MIN_H = 360;
export { CLIP_PANEL_MIN_W, CLIP_PANEL_MIN_H };
/** Keep at least this much of the panel on screen while resizing. */
const RESIZE_MARGIN = 32;

/**
 * Reusable clip-preview sessions keyed by VOD URL. Closing the popup used to
 * DELETE its session, so every reopen re-created one and the backend
 * re-resolved the VOD from scratch (slow). Sessions are kept here instead —
 * a reopen for the same VOD probes the cached session's master and adopts it
 * (zero POST, zero resolve); a stale one (backend TTL 30 min / LRU cap) falls
 * through to a fresh create. Replacing an entry drops the superseded session;
 * the map is capped so long browsing never accumulates ids.
 */
const _clipSessionCache = new Map<string, string>();
const _CLIP_SESSION_CACHE_MAX = 8;

function _clipSessionKey(url: string, start: number, end: number): string {
  return `${url}#${Math.round(start)}-${Math.round(end)}@${PREVIEW_SCRUB_HEIGHT}`;
}

function _cacheClipSession(key: string, sessionId: string): void {
  const prev = _clipSessionCache.get(key);
  if (prev && prev !== sessionId) {
    void apiDelete(`/api/preview/session/${prev}`).catch(() => {});
  }
  _clipSessionCache.set(key, sessionId);
  if (_clipSessionCache.size > _CLIP_SESSION_CACHE_MAX) {
    const oldestKey = _clipSessionCache.keys().next().value as string;
    const evicted = _clipSessionCache.get(oldestKey);
    _clipSessionCache.delete(oldestKey);
    if (evicted) void apiDelete(`/api/preview/session/${evicted}`).catch(() => {});
  }
}

interface TwitchClipPopupProps {
  /** VOD URL the mini preview plays (same session machinery as the main preview). */
  url: string;
  broadcasterLogin: string;
  vodId: string;
  /** VOD time of the click — the 120s window is 60s left + 60s right of this. */
  playheadSec: number;
  /** VOD length; <=0/unknown → the upper window edge is unclamped. */
  vodDurationSec: number;
  /** Unused for window placement (click always wins). Kept so callers can
   * still pass a short 5–60s trim as the initial selection. */
  anchorRange?: { start: number; end: number };
  /** Original VOD title; every browser clip uses this title verbatim. */
  vodTitle: string;
  zIndex: number;
  onClose: () => void;
  /** Volume the popup should start at — inherited from the opening preview
   * so opening the clip window never resets the user's volume level. */
  initialVolume?: number;
  /** Session of the opening preview (same VOD, full-VOD HLS proxy): reuse it
   * so segments already fetched hit that session's disk cache instead of
   * re-downloading from the CDN. Skipped when trimTimeline is true (the
   * preview HLS is a short trim, not the full VOD). */
  reuseSession?: { sessionId: string; trimTimeline: boolean } | null;
  /** When the Download checkbox is on, Create also enqueues this VOD crop. */
  onDownloadSelection?: (sel: {
    start: number;
    end: number;
    url: string;
    vodId: string;
    channel: string;
    title: string;
  }) => void;
}

export default function TwitchClipPopup({
  url,
  broadcasterLogin,
  vodId,
  playheadSec,
  vodDurationSec,
  anchorRange,
  vodTitle,
  zIndex,
  onClose,
  initialVolume = PREVIEW_DEFAULT_VOLUME,
  onDownloadSelection,
}: TwitchClipPopupProps) {
  const { t } = useI18n();
  const volumeRef = useRef(initialVolume);
  const win = useMemo(
    () => twitchClipWindow(playheadSec, vodDurationSec, anchorRange),
    [playheadSec, vodDurationSec, anchorRange],
  );
  const winLen = win.end - win.start;
  const windowTooShort = winLen < TWITCH_CLIP_MIN_SEC;

  const [selection, setSelection] = useState(() => initialClipSelection(win, anchorRange, playheadSec));
  const selectionRef = useRef(selection);
  const commitSelection = useCallback((next: { start: number; end: number }) => {
    selectionRef.current = next;
    setSelection(next);
  }, []);
  const [lastEndpoint, setLastEndpoint] = useState<'in' | 'out'>('out');
  const lastEndpointRef = useRef<'in' | 'out'>('out');
  const markEndpoint = useCallback((which: 'in' | 'out') => {
    lastEndpointRef.current = which;
    setLastEndpoint(which);
  }, []);
  const selLen = Math.max(0, selection.end - selection.start);
  // The browser flow never accepts a user-supplied/custom title. The title
  // comes from the VOD metadata and is passed to Twitch unchanged.
  const clipTitle = vodTitle.trim();

  // Floating panel position (draggable via the header, like the other popups).
  const [position, setPosition] = useState(() => ({
    x: Math.max(8, window.innerWidth - POPUP_W - 24),
    y: 80,
  }));
  const posRef = useRef(position);
  // Panel size — explicit width/height so the panel is resizable in ALL
  // directions (same [data-panel-resize] machinery as the live player popup).
  // h starts 0 = auto height; the mount effect adopts the natural height so a
  // resize drag never starts from an unmeasured box.
  const [size, setSize] = useState<PanelSize>(() => ({ w: POPUP_W, h: 0 }));
  const sizeRef = useRef(size);
  useEffect(() => { sizeRef.current = size; }, [size]);
  const [drag, setDrag] = useState<{
    startX: number; startY: number; offsetX: number; offsetY: number;
  } | null>(null);
  /** Window-body drag in progress — drives the grab/grabbing cursor. */
  const [windowDragging, setWindowDragging] = useState(false);

  const popupRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const railRef = useRef<HTMLDivElement>(null);
  // Spawned windows take focus (shared raise-to-front contract) — the popup
  // is the active surface the moment it opens.
  useEffect(() => {
    popupRef.current?.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    pauseOtherPreviews();
    const pause = () => {
      const video = videoRef.current;
      if (video && !video.paused) {
        video.pause();
        setPlaying(false);
      }
    };
    return registerPreviewPlayback(pause);
  }, []);
  /** In-flight window-body drag (move the whole selection along the VOD). */
  const windowDragRef = useRef<{
    pointerId: number;
    startClientX: number;
    grabOffsetSec: number;
    selLen: number;
    moved: boolean;
    prevUserSelect: string;
  } | null>(null);
  /** A window drag ends with a synthetic click on the bar — suppress its seek. */
  const suppressBarClickRef = useRef(false);
  /** Range/handle drag is preview-scrubbing; timeupdate must not move the playhead. */
  const holdPlayheadRef = useRef(false);
  const hlsRef = useRef<Hls | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  /** Load start of this clip preview — any user unpause at/after this
   *  timestamp suppresses the load-complete auto-pause. */
  const loadingSinceRef = useRef(0);
  const timelineOffsetRef = useRef(0);
  const currentTimeRef = useRef(Math.max(win.start, Math.min(win.end, playheadSec)));

  const [playback, setPlayback] = useState<{
    url: string;
    kind: 'hls' | 'progressive';
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [buffering, setBuffering] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(initialVolume);
  const [volumeHovered, setVolumeHovered] = useState(false);
  const [currentTime, setCurrentTime] = useState(currentTimeRef.current);
  const [retryTick, setRetryTick] = useState(0);
  // The <video> is autoPlay: with the click's user activation the browser can
  // start playback at the element's default volume (1.0) BEFORE canplay fires,
  // so the play() call never runs and the 0.3 default is never applied. Pin
  // the volume on mount and on every retry/src swap — deterministic 30%.
  useEffect(() => {
    const video = videoRef.current;
    if (video) video.volume = volumeRef.current;
  }, [retryTick]);
  const [clipNotice, setClipNotice] = useState<{ kind: 'error' | 'ok'; text: string } | null>(null);
  const clipNoticeTimerRef = useRef<number | null>(null);
  const [downloadWithClip, setDownloadWithClip] = useState(false);

  const showClipNotice = useCallback((kind: 'error' | 'ok', text: string) => {
    if (clipNoticeTimerRef.current) window.clearTimeout(clipNoticeTimerRef.current);
    setClipNotice({ kind, text });
    clipNoticeTimerRef.current = window.setTimeout(() => setClipNotice(null), 4000);
  }, []);

  // ── Preview session (mirrors App.tsx openPreview: crop window = trim range) ──
  useEffect(() => {
    let cancelled = false;
    // Auto-pause guard: any user unpause at/after this instant suppresses
    // the load-complete pause when this clip preview finishes loading.
    loadingSinceRef.current = Date.now();
    // Full-VOD HLS proxy (no window mux): video time is absolute VOD time.
    // Adopt an existing same-VOD session so the proxy serves segments from
    // that session's disk cache instead of re-downloading from the CDN (and
    // no POST / re-resolve at all). Probes the master first — a stale session
    // falls through to a fresh one.
    const adoptSession = async (sid: string): Promise<boolean> => {
      try {
        const probe = await fetch(`/api/preview/hls/${sid}/master.m3u8`);
        if (!probe.ok) throw new Error(`stale session ${sid.slice(0, 8)}`);
        if (cancelled) return true;
        sessionIdRef.current = sid;
        timelineOffsetRef.current = 0;
        setPlayback({ url: `/api/preview/hls/${sid}/master.m3u8`, kind: 'hls' });
        setLoading(false);
        return true;
      } catch {
        return false;
      }
    };
    (async () => {
      // Dedicated 160p crop of the 120s click window — do not adopt the main
      // preview's 720p session; scrubbing has to stay ugly and fast.
      const cacheKey = _clipSessionKey(url, win.start, win.end);
      const cachedSid = _clipSessionCache.get(cacheKey);
      if (cachedSid && await adoptSession(cachedSid)) return;
      try {
        const res = await createPreviewSessionWithRetry({
          url,
          crop_start: win.start,
          crop_end: win.end,
          prefer_height: PREVIEW_SCRUB_HEIGHT,
        });
        if (cancelled) {
          // ponytail: StrictMode double-invoke — both runs share the SAME
          // session (inflight dedup in createPreviewSessionWithRetry). Only
          // delete when the applied run used a different session.
          const orphanSid = res.session_id;
          window.setTimeout(() => {
            if (sessionIdRef.current !== orphanSid) {
              void apiDelete(`/api/preview/session/${orphanSid}`).catch(() => {});
            }
          }, 5000);
          return;
        }
        sessionIdRef.current = res.session_id;
        _cacheClipSession(_clipSessionKey(url, win.start, win.end), res.session_id);
        // Window-muxed MP4 (trim_timeline) is 0-based; Twitch VOD HLS is
        // absolute VOD time. offset maps video time → VOD time.
        timelineOffsetRef.current = res.trim_timeline === true ? win.start : 0;
        setPlayback({ ...resolvePreviewPlayback(url, res) });
        setLoading(false);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t('Could not start preview'));
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      const hls = hlsRef.current;
      if (hls) {
        try { hls.stopLoad(); hls.detachMedia(); hls.destroy(); } catch { /* ignore */ }
        hlsRef.current = null;
      }
      const video = videoRef.current;
      if (video) detachProgressivePreview(video);
      // Created sessions are intentionally NOT deleted here — they live in
      // _clipSessionCache so re-opening the popup for the same VOD reuses the
      // session instead of re-resolving from scratch. The backend TTL (30 min)
      // and LRU cap reclaim idle sessions.
      sessionIdRef.current = null;
    };
  }, [url, win.start, win.end, retryTick]);

  // ── Playback attach (HLS for Twitch VODs, progressive fallback) ──
  useEffect(() => {
    if (!playback?.url) return;
    let cancelled = false;
    const video = videoRef.current;
    if (!video) return;
    const bufferingHandle = attachPreviewBufferingListeners(video, (s) => {
      if (!cancelled) setBuffering(s);
    });
    setLoading(true);
    setBuffering(false);
    setReady(false);

    const initialVideoTime = timelineOffsetRef.current > 0
      ? Math.max(0, playheadSec - win.start)
      : Math.max(win.start, Math.min(win.end, playheadSec));

    const clampToWindow = () => {
      const base = timelineOffsetRef.current;
      const lo = Math.max(0, win.start - base);
      const hi = Math.max(lo, win.end - base);
      let t = video.currentTime;
      if (t < lo) t = lo;
      else if (t > hi) {
        t = hi;
        if (!video.paused) video.pause();
      }
      if (Math.abs(video.currentTime - t) > 0.05) video.currentTime = t;
      if (holdPlayheadRef.current) return;
      const vodTime = t + base;
      currentTimeRef.current = vodTime;
      setCurrentTime(vodTime);
    };
    video.addEventListener('timeupdate', clampToWindow);

    const onCanPlay = () => {
      if (cancelled) return;
      setReady(true);
      setBuffering(false);
      setLoading(false);
      video.volume = volumeRef.current;
      if (video.paused) {
        autoPauseOtherPreviews(loadingSinceRef.current);
        void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
          setPlaying(!video.paused);
        });
      }
    };

    if (playback.kind === 'progressive') {
      attachProgressivePreview(video, playback.url);
      const onLoadMetadata = () => {
        // window-muxed MP4 is 0-based (offset set above) — land the click moment.
        const t = timelineOffsetRef.current > 0
          ? Math.max(0, playheadSec - win.start)
          : Math.max(win.start, Math.min(win.end, playheadSec));
        video.currentTime = t;
      };
      video.addEventListener('loadedmetadata', onLoadMetadata, { once: true });
      video.addEventListener('canplay', onCanPlay, { once: true });
      return () => {
        cancelled = true;
        video.removeEventListener('loadedmetadata', onLoadMetadata);
        video.removeEventListener('timeupdate', clampToWindow);
        video.removeEventListener('canplay', onCanPlay);
        detachProgressivePreview(video);
        bufferingHandle.detach();
      };
    }

    const hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 12,
      maxBufferLength: 6,
      maxMaxBufferLength: 12,
      startFragPrefetch: true,
      fragLoadingTimeOut: 20000,
      manifestLoadingTimeOut: 10000,
      testBandwidth: false,
      startLevel: 0,
      capLevelToPlayerSize: false,
      ...twitchAdBlockHlsConfig({}),
      startPosition: initialVideoTime,
    });
    hlsRef.current = hls;
    pinHlsToLowestLevel(hls);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      pinHlsToLowestLevel(hls);
      if (!cancelled) hls.startLoad(initialVideoTime);
    });
    hls.on(Hls.Events.ERROR, (_evt, data) => {
      if (cancelled || !data.fatal) return;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        hls.startLoad();
      } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        hls.recoverMediaError();
      } else {
        setError(t('Preview failed — retry'));
        setLoading(false);
      }
    });
    hls.loadSource(playback.url);
    video.addEventListener('canplay', onCanPlay, { once: true });

    return () => {
      cancelled = true;
      video.removeEventListener('timeupdate', clampToWindow);
      video.removeEventListener('canplay', onCanPlay);
      try { hls.stopLoad(); hls.detachMedia(); hls.destroy(); } catch { /* ignore */ }
      if (hlsRef.current === hls) hlsRef.current = null;
      bufferingHandle.detach();
    };
    // Re-attach only when the session's playback actually changes (a retry
    // creates a fresh session → new playback object). retryTick must NOT be a
    // dep: it would rebuild the player against the just-deleted session.
  }, [playback, win.start, win.end, playheadSec]);

  const seekTo = useCallback((vodSec: number) => {
    const video = videoRef.current;
    if (!video) return;
    pinHlsToLowestLevel(hlsRef.current);
    const t = Math.max(win.start, Math.min(win.end, vodSec));
    video.currentTime = Math.max(0, t - timelineOffsetRef.current);
    currentTimeRef.current = t;
    setCurrentTime(t);
  }, [win]);

  /** Seek the picture without moving the playhead (range/handle preview scrub). */
  const seekPreview = useCallback((vodSec: number) => {
    const video = videoRef.current;
    if (!video) return;
    pinHlsToLowestLevel(hlsRef.current);
    const t = Math.max(win.start, Math.min(win.end, vodSec));
    video.currentTime = Math.max(0, t - timelineOffsetRef.current);
  }, [win]);

  const resumeFromPlayhead = useCallback((wasPlaying: boolean, restoreSec: number) => {
    const video = videoRef.current;
    // Seek back to the playhead before releasing the hold, so a late
    // timeupdate from the preview-scrub position cannot steal it.
    seekTo(restoreSec);
    holdPlayheadRef.current = false;
    if (wasPlaying && video) {
      void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
        setPlaying(!video.paused);
      });
    } else {
      setPlaying(false);
    }
  }, [seekTo]);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video || !ready) return;
    if (video.paused) {
      noteUserUnpause();
      void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
        setPlaying(!video.paused);
      });
    } else {
      video.pause();
      setPlaying(false);
    }
  }, [ready]);

  const toggleMute = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !video.muted;
    setMuted(video.muted);
  }, []);

  const setVolumeLevel = useCallback((level: number) => {
    const v = Math.max(0, Math.min(1, level));
    const video = videoRef.current;
    if (video) {
      video.volume = v;
      video.muted = v <= 0;
    }
    volumeRef.current = v > 0 ? v : volumeRef.current;
    setVolume(v);
    setMuted(v <= 0);
  }, []);

  // ── Trimmer ──
  const beginHandleDrag = useCallback((
    e: ReactPointerEvent<HTMLElement>,
    which: 'in' | 'out',
  ) => {
    markEndpoint(which);
    e.preventDefault();
    e.stopPropagation();
    const rail = railRef.current;
    if (!rail) return;
    const handle = e.currentTarget;
    const pointerId = e.pointerId;
    handle.setPointerCapture(pointerId);
    const fixed = which === 'in' ? selectionRef.current.end : selectionRef.current.start;
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    const video = videoRef.current;
    const wasPlaying = !!(video && !video.paused);
    const restoreSec = currentTimeRef.current;
    holdPlayheadRef.current = true;
    if (video && !video.paused) {
      video.pause();
      setPlaying(false);
    }
    pinHlsToLowestLevel(hlsRef.current);

    const xToSec = (clientX: number) => {
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return win.start;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return fracToSec(frac, win);
    };

    let ended = false;
    const endDrag = () => {
      if (ended) return;
      ended = true;
      document.body.style.userSelect = prevUserSelect;
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', endDrag);
      handle.removeEventListener('pointercancel', endDrag);
      handle.removeEventListener('lostpointercapture', endDrag);
      try { handle.releasePointerCapture(pointerId); } catch { /* ignore */ }
      resumeFromPlayhead(wasPlaying, restoreSec);
    };
    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      const sec = xToSec(ev.clientX);
      const res = which === 'in'
        ? clampClipSelection(sec, fixed, win.start, win.end, { move: 'in', fixedEnd: fixed })
        : clampClipSelection(fixed, sec, win.start, win.end, { move: 'out', fixedStart: fixed });
      commitSelection(res);
      seekPreview(which === 'in' ? res.start : res.end);
    };
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);
    handle.addEventListener('lostpointercapture', endDrag);
  }, [win, commitSelection, markEndpoint, resumeFromPlayhead, seekPreview]);

  // Grab the selection WINDOW (between the edge handles) and move it along
  // the VOD; the edge handles keep resizing. The grab offset is recorded in
  // seconds on pointerdown (grab point → selection start), so the window
  // tracks the pointer 1:1 instead of jumping by the grab-point delta.
  const beginWindowDrag = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (e.button !== 0) return;
    const rail = railRef.current;
    if (!rail) return;
    const rect = rail.getBoundingClientRect();
    if (rect.width <= 0) return;
    const pointerId = e.pointerId;
    e.preventDefault();
    e.stopPropagation();
    const target = e.currentTarget;
    target.setPointerCapture(pointerId);
    setWindowDragging(true);
    const winLen = win.end - win.start;
    const sel = selectionRef.current;
    const selLen = sel.end - sel.start;
    const grabFrac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const grabSec = win.start + grabFrac * winLen;
    const state = {
      pointerId,
      startClientX: e.clientX,
      grabOffsetSec: grabSec - sel.start,
      selLen,
      moved: false,
      prevUserSelect: document.body.style.userSelect,
    };
    windowDragRef.current = state;
    suppressBarClickRef.current = false;
    document.body.style.userSelect = 'none';
    const video = videoRef.current;
    const wasPlaying = Boolean(video && !video.paused);
    const restoreSec = currentTimeRef.current;

    const endDrag = () => {
      if (windowDragRef.current !== state) return;
      windowDragRef.current = null;
      setWindowDragging(false);
      document.body.style.userSelect = state.prevUserSelect;
      suppressBarClickRef.current = true;
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', endDrag);
      target.removeEventListener('pointercancel', endDrag);
      target.removeEventListener('lostpointercapture', endDrag);
      try { target.releasePointerCapture(pointerId); } catch { /* ignore */ }
      if (!state.moved) {
        holdPlayheadRef.current = false;
        seekTo(grabSec);
        if (wasPlaying && video) {
          void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
            setPlaying(!video.paused);
          });
        }
        return;
      }
      resumeFromPlayhead(wasPlaying, restoreSec);
    };
    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== state.pointerId) return;
      if (!state.moved) {
        if (Math.abs(ev.clientX - state.startClientX) < 3) return;
        state.moved = true;
        holdPlayheadRef.current = true;
        if (video && !video.paused) {
          video.pause();
          setPlaying(false);
        }
        pinHlsToLowestLevel(hlsRef.current);
      }
      const r = rail.getBoundingClientRect();
      if (r.width <= 0) return;
      const frac = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
      const sec = win.start + frac * winLen;
      const newStart = Math.max(win.start, Math.min(win.end - state.selLen, sec - state.grabOffsetSec));
      commitSelection({ start: newStart, end: newStart + state.selLen });
      seekPreview(newStart + state.grabOffsetSec);
    };

    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', endDrag);
    target.addEventListener('pointercancel', endDrag);
    target.addEventListener('lostpointercapture', endDrag);
  }, [win, commitSelection, resumeFromPlayhead, seekPreview, seekTo]);

  const beginPlayheadScrub = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (e.button !== 0) return;
    const rail = railRef.current;
    if (!rail) return;
    const pointerId = e.pointerId;
    e.preventDefault();
    e.stopPropagation();
    const target = e.currentTarget;
    target.setPointerCapture(pointerId);
    pinHlsToLowestLevel(hlsRef.current);
    const video = videoRef.current;
    const wasPlaying = !!(video && !video.paused);
    if (video && !video.paused) {
      video.pause();
      setPlaying(false);
    }
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    const xToSec = (clientX: number) => {
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return win.start;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return fracToSec(frac, win);
    };
    seekTo(xToSec(e.clientX));
    let ended = false;
    const endDrag = () => {
      if (ended) return;
      ended = true;
      document.body.style.userSelect = prevUserSelect;
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', endDrag);
      target.removeEventListener('pointercancel', endDrag);
      target.removeEventListener('lostpointercapture', endDrag);
      try { target.releasePointerCapture(pointerId); } catch { /* ignore */ }
      if (wasPlaying && video) {
        void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
          setPlaying(!video.paused);
        });
      }
    };
    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      pinHlsToLowestLevel(hlsRef.current);
      seekTo(xToSec(ev.clientX));
    };
    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', endDrag);
    target.addEventListener('pointercancel', endDrag);
    target.addEventListener('lostpointercapture', endDrag);
  }, [win, seekTo]);

  const adjustSelection = useCallback((buttonDelta: number) => {
    const sel = selectionRef.current;
    const which = lastEndpointRef.current;
    const delta = trimButtonDeltaForEndpoint(which, buttonDelta);
    const res = which === 'in'
      ? clampClipSelection(
        sel.start - delta, sel.end, win.start, win.end,
        { move: 'in', fixedEnd: sel.end },
      )
      : clampClipSelection(
        sel.start, sel.end + delta, win.start, win.end,
        { move: 'out', fixedStart: sel.start },
      );
    commitSelection(res);
  }, [win, commitSelection]);

  // Same range, but driven in the OS browser by the VOD.RIP cookie extension
  // (clip_assist.mjs content script) — works with the plain browser-login
  // session cookie, no API clip scopes needed. When the
  // extension is not paired yet, run the app's auto-installer FIRST (it
  // stages the extension, restarts the browser, and the extension self-pairs
  // via /api/session/cookies), then open the editor once pairing settles.
  const createInBrowser = useCallback(async () => {
    const sel = selectionRef.current;
    const err = twitchClipDurationError(sel.end - sel.start);
    if (err) {
      showClipNotice('error', err);
      return;
    }
    if (!clipTitle) {
      showClipNotice('error', t('Original VOD title unavailable'));
      return;
    }
    try {
      const status = await apiGet<{
        paired: boolean;
        platforms?: { twitch?: { lastGrabAt?: string | null } };
      }>('/api/session/cookies/status');
      if (!status.paired) {
        // The bridge token can be empty while the extension is installed and
        // running (the pair lives in the extension's storage; the backend
        // re-pairs on the extension's next cookie push — its 10-min heartbeat).
        // The editor clip is published by the extension's content script using
        // the PAGE session cookie, so a de-paired-but-active extension still
        // completes the clip. Only run the auto-installer when the extension
        // is demonstrably inactive (no Twitch cookie push in ~11 min) — the
        // chrome://extensions window it opens is pure noise for an extension
        // that is already loaded.
        const lastGrab = status.platforms?.twitch?.lastGrabAt;
        const extActive =
          !!lastGrab && Date.now() - new Date(lastGrab).getTime() < 11 * 60_000;
        if (!extActive) {
          showClipNotice('error', t('Installing the VOD.RIP cookie extension — one moment…'));
          const inst = await apiPost<{ ok: boolean; started?: boolean; alreadyInstalled?: boolean }>(
            '/api/session/cookies/auto-install',
            {},
          );
          if (!inst.ok && !inst.started && !inst.alreadyInstalled) {
            showClipNotice('error', t('Could not install the cookie extension — open Settings → Cookies'));
            return;
          }
          // The extension's service worker pushes cookies on install/startup
          // and on a 10-minute alarm; give the re-pair room so a running-but-
          // quiet extension never dead-ends into "timed out".
          const deadline = Date.now() + 700_000;
          let paired = false;
          while (Date.now() < deadline && !paired) {
            await new Promise((r) => setTimeout(r, 2500));
            try {
              const st = await apiGet<{ paired: boolean; auto_install?: { state?: string } }>(
                '/api/session/cookies/status',
              );
              // The extension pairs the moment it boots — BEFORE the installer
              // reports done and closes its chrome://extensions window. Opening
              // the editor at 'paired' alone can land the tab inside that window
              // and lose it to the cleanup. Wait for the install to actually
              // finish (state leaves 'running') before opening.
              const installDone = !st.auto_install || st.auto_install.state !== 'running';
              paired = !!st.paired && installDone;
            } catch { /* backend mid-restart during the install — keep polling */ }
          }
          if (!paired) {
            showClipNotice('error', t('Extension install timed out — open Settings → Cookies'));
            return;
          }
          showClipNotice('ok', t('Cookie extension ready — opening Twitch…'));
        }
      }
    } catch {
      // status/install endpoints unreachable — open the editor anyway; the
      // page still loads (the extension just won't auto-publish).
    }
    reportClipEvent('create_clicked', {
      method: 'browser',
      startSec: sel.start,
      endSec: sel.end,
      durationSec: sel.end - sel.start,
      title: clipTitle,
      download: downloadWithClip,
    });
    if (downloadWithClip) {
      const payload = {
        start: sel.start,
        end: sel.end,
        url,
        vodId,
        channel: broadcasterLogin,
        title: clipTitle,
      };
      if (onDownloadSelection) {
        onDownloadSelection(payload);
      } else {
        void apiPost('/api/download/video', {
          url,
          quality: 'source',
          crop_start: sel.start,
          crop_end: sel.end,
          title: clipTitle,
          channel: broadcasterLogin,
          duration: sel.end - sel.start,
        }).catch(() => {});
      }
    }
    openTwitchClipEditorInBrowser(vodId, broadcasterLogin, sel.start, sel.end, clipTitle, vodDurationSec);
    showClipNotice('ok', t('Opened in your browser — the VOD.RIP extension fills the editor and publishes'));
  }, [vodId, broadcasterLogin, clipTitle, showClipNotice, t, vodDurationSec, downloadWithClip, onDownloadSelection, url]);

  const railView = useMemo(() => ({ start: win.start, end: win.end }), [win]);
  const playFrac = secToFrac(currentTime, railView) * 100;
  const selStartFrac = secToFrac(selection.start, railView) * 100;
  const selEndFrac = secToFrac(selection.end, railView) * 100;

  const createDisabled = windowTooShort
    || !clipTitle
    || selLen < TWITCH_CLIP_MIN_SEC || selLen > TWITCH_CLIP_MAX_SEC;
  const createDisabledTitle = !clipTitle
    ? t('Original VOD title unavailable')
    : windowTooShort
      ? t('The {seconds}s window is too short to clip (min {min}s)', { seconds: Math.round(winLen), min: TWITCH_CLIP_MIN_SEC })
      : selLen > TWITCH_CLIP_MAX_SEC
        ? t('Trim the selection to {max}s or less', { max: TWITCH_CLIP_MAX_SEC })
        : selLen < TWITCH_CLIP_MIN_SEC
          ? t('Select at least {min}s', { min: TWITCH_CLIP_MIN_SEC })
          : t("Open Twitch's clip editor — {len}s ending at {time}", { len: Math.round(selLen), time: formatHmsFull(selection.end) });

  // Keep the drag-offset ref in sync with the applied position — posRef was
  // never written, so every grab after the first offset from the INITIAL
  // position and teleported the popup back to the right edge on pointerdown.
  useEffect(() => {
    posRef.current = position;
  }, [position]);

  // Adopt the natural content height once — the panel opens auto-height at
  // its natural size (the exact pre-resize look), then becomes explicitly
  // sized so drag/resize can reason in pixels. Also clamps the initial
  // position so the bottom buttons are never below the fold on a short
  // window (the reported cut-off bug).
  useLayoutEffect(() => {
    const el = popupRef.current;
    if (!el || sizeRef.current.h > 0) return;
    const h = el.offsetHeight;
    if (h <= 0) return;
    sizeRef.current = { w: POPUP_W, h };
    setSize(sizeRef.current);
    const margin = 8;
    const start = posRef.current;
    const p = {
      x: Math.max(margin, Math.min(start.x, window.innerWidth - POPUP_W - margin)),
      y: Math.max(margin, Math.min(start.y, window.innerHeight - h - margin)),
    };
    posRef.current = p;
    setPosition(p);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only
  }, []);

  // Window resize while the popup is open: keep the whole panel inside the
  // viewport so a shrink never strands the bottom buttons below the fold.
  useEffect(() => {
    const fit = () => {
      const el = popupRef.current;
      if (!el) return;
      const s = sizeRef.current;
      const w = s.w || el.offsetWidth || POPUP_W;
      const h = s.h || el.offsetHeight || CLIP_PANEL_MIN_H;
      const margin = 8;
      const p = {
        x: Math.max(margin, Math.min(posRef.current.x, window.innerWidth - w - margin)),
        y: Math.max(margin, Math.min(posRef.current.y, window.innerHeight - h - margin)),
      };
      posRef.current = p;
      setPosition(p);
    };
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);

  const handleHeaderMouseDown = useCallback((e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest('.twitch-clip-popup-close')) return;
    setDrag({ startX: e.clientX, startY: e.clientY, offsetX: posRef.current.x, offsetY: posRef.current.y });
  }, []);

  useEffect(() => {
    if (!drag) return;
    const onMove = (e: MouseEvent) => {
      // Clamp against the panel's LIVE size (not a fixed constant) so the
      // panel can never be dragged with its bottom buttons below the fold.
      const s = sizeRef.current;
      const w = s.w || POPUP_W;
      const h = s.h || window.innerHeight;
      setPosition({
        x: Math.max(0, Math.min(window.innerWidth - w, drag.offsetX + e.clientX - drag.startX)),
        y: Math.max(0, Math.min(window.innerHeight - h, drag.offsetY + e.clientY - drag.startY)),
      });
    };
    const onUp = () => setDrag(null);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [drag]);

  // ── Resize (same [data-panel-resize] pattern as the live player popup) ──
  // 8-directional: west/north edges move the panel so the opposite edge stays
  // put (panelPosAfterResize keeps the panel inside the viewport). The min
  // contract is clamped at RESIZE time (clampSize) — min wins even on a
  // degenerate viewport smaller than the panel.
  const handleResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const el = popupRef.current;
    if (!el) return;
    if (sizeRef.current.h <= 0) {
      // The mount measure hasn't run (or measured 0): seed from the DOM so a
      // drag never starts from an unmeasured box.
      sizeRef.current = { w: POPUP_W, h: el.offsetHeight || CLIP_PANEL_MIN_H };
    }
    const startSize = { ...sizeRef.current };
    const startPos = posRef.current;
    const viewport = { w: window.innerWidth, h: window.innerHeight };
    const applyPos = (next: PanelSize) => {
      const p = panelPosAfterResize(edge, startPos, startSize, next, viewport);
      posRef.current = p;
      setPosition(p);
    };
    startPanelResizeDrag(e, edge, sizeRef, setSize, {
      panelEl: el,
      maxW: viewport.w - RESIZE_MARGIN,
      maxH: viewport.h - RESIZE_MARGIN,
      clampSize: (s) => ({
        w: Math.max(CLIP_PANEL_MIN_W, Math.min(s.w, viewport.w - RESIZE_MARGIN)),
        h: Math.max(CLIP_PANEL_MIN_H, Math.min(s.h, viewport.h - RESIZE_MARGIN)),
      }),
      onResizeMove: (next) => applyPos(next),
      onResizeEnd: () => applyPos(sizeRef.current),
    });
  }, []);

  return createPortal(
    <div
      ref={popupRef}
      tabIndex={-1}
      className="border-2 border-zinc-700 bg-zinc-950 flex flex-col"
      data-twitch-clip-popup
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        width: size.w,
        height: size.h > 0 ? size.h : 'auto',
        // Min-size contract: the panel can never render smaller than its
        // button rows need — also enforced at resize time by clampSize.
        minWidth: CLIP_PANEL_MIN_W,
        minHeight: CLIP_PANEL_MIN_H,
        zIndex,
        boxShadow: '6px 6px 0px 0px rgba(9,9,11,0.9)',
      }}
    >
      <PanelResizeHandles onPointerDown={handleResize} />

      {/* Header — drag handle + close */}
      <div
        onMouseDown={handleHeaderMouseDown}
        className="flex items-center justify-between gap-2 px-2 py-1.5 bg-zinc-900 border-b-2 border-zinc-800 select-none shrink-0"
        style={{ cursor: drag ? 'grabbing' : 'grab' }}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <TwitchLogoIcon size={12} className="text-[#9146FF] shrink-0" />
          <div className="min-w-0">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
              {t('Twitch clip')}
            </span>
            <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
              {broadcasterLogin}
            </p>
          </div>
        </div>
        <button
          type="button"
          className="twitch-clip-popup-close text-zinc-500 hover:text-white p-1 shrink-0"
          onClick={onClose}
          title={t('Close')}
        >
          <X size={16} />
        </button>
      </div>

      {/* Video — click toggles play/pause. flex-1 + min-h-0: the video area
          absorbs ALL vertical slack (grows when the panel is resized taller,
          letterboxed via objectFit:contain; shrinks when shorter) so the trim
          section below — every button row — never gets squished or clipped at
          any panel size at or above the min contract. */}
      <div className="relative bg-black flex-1 min-h-0" style={{ aspectRatio: '16/9' }}>
        <div
          className="absolute inset-0 z-0 cursor-pointer"
          onClick={() => { if (!loading && !error) togglePlay(); }}
        >
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted={muted}
            style={{ width: '100%', height: '100%', objectFit: 'contain' }}
          />
        </div>
        {loading && (
          <div className="absolute inset-0 z-[1] flex items-center justify-center bg-black/60 text-zinc-300 text-xs font-mono pointer-events-none">
            <Loader2 size={28} className="animate-spin text-zinc-300 mr-2" />
            {t('Preparing clip window…')}
          </div>
        )}
        {error && (
          <div className="absolute inset-0 z-[1] flex flex-col items-center justify-center gap-2.5 bg-black/70 text-red-400 text-xs font-mono text-center px-4">
            <div>{error}</div>
            <button
              type="button"
              onClick={() => { setError(null); setLoading(true); setRetryTick((t) => t + 1); }}
              title={t('Retry the clip window preview')}
              className="flex items-center gap-1.5 text-[10px] font-black uppercase tracking-wider text-red-400 border-2 border-red-800 bg-red-950/30 px-2.5 py-1 hover:border-red-500 hover:text-red-300 cursor-pointer"
            >
              <RefreshCw size={12} />
              {t('Retry')}
            </button>
          </div>
        )}
        {!loading && !error && buffering && (
          <div className="absolute inset-0 z-[1] flex items-center justify-center bg-black/50 text-zinc-300 text-xs font-mono pointer-events-none">
            <Loader2 size={24} className="animate-spin text-zinc-200/90 mr-2" />
            {t('Buffering…')}
          </div>
        )}
        {/* Transport: play/pause — only after loading completes */}
        {!loading && !error && (
          <div
            className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 px-2 py-1.5 bg-gradient-to-t from-black/85 to-black/0"
          >
            <button
              type="button"
              onClick={togglePlay}
              disabled={!ready}
              className="flex items-center gap-1 border-2 border-zinc-600 bg-zinc-900/80 px-1.5 py-1 text-zinc-200 hover:border-white disabled:opacity-40 disabled:pointer-events-none"
              title={playing ? t('Pause') : t('Play')}
            >
              {playing ? <Pause size={13} /> : <Play size={13} />}
            </button>
            <span className="ml-auto text-[9px] font-mono text-zinc-400 tabular-nums">
              {formatHmsFull(currentTime)}
            </span>
          </div>
        )}
        {/* Volume: always rendered (mute/unmute is useful even while loading) */}
        <div className="absolute bottom-0 left-0 z-10 px-2 py-1.5">
          <div
            className="relative flex items-center"
            data-volume-menu
            onMouseEnter={() => setVolumeHovered(true)}
            onMouseLeave={() => setVolumeHovered(false)}
          >
            <button
              type="button"
              onClick={toggleMute}
              className="flex items-center gap-1 border-2 border-zinc-600 bg-zinc-900/80 px-1.5 py-1 text-zinc-200 hover:border-white disabled:opacity-40 disabled:pointer-events-none"
              title={muted ? t('Unmute') : t('Mute')}
            >
              {muted ? <VolumeX size={13} /> : <Volume2 size={13} />}
            </button>
            {volumeHovered && (
              <div
                className="absolute left-full bottom-0 ml-1.5 z-30 flex items-center gap-2 px-2.5 py-2 shadow-lg border-2 border-zinc-600 bg-zinc-950"
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
                  className="w-24 accent-white h-1.5"
                />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Trim rail: 5..60s selection on the 120s click window */}
      <div className="px-2 py-1.5 flex flex-col gap-1 shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-[8px] font-mono uppercase w-9 shrink-0 tracking-wider text-zinc-600">
            {t('Range')}
          </span>
          <span className="flex items-center gap-1" title={t('Clip start (VOD time)')}>
            <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-500">{t('Start')}</span>
            <span className="text-[10px] font-bold text-zinc-300 tabular-nums">{formatHmsFull(selection.start)}</span>
          </span>
          <span className="text-[9px] font-mono text-zinc-600">–</span>
          <span className="flex items-center gap-1" title={t('Clip end (VOD time)')}>
            <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-500">{t('End')}</span>
            <span className="text-[10px] font-bold text-[#9146FF] tabular-nums">{formatHmsFull(selection.end)}</span>
          </span>
          <span className="ml-auto text-[8px] font-mono uppercase tracking-wider text-zinc-600">
            {t('H:M:S')}
          </span>
        </div>
        <div className="flex items-stretch gap-2 pt-1.5">
          <span className="text-[8px] font-mono uppercase w-9 shrink-0 tracking-wider text-zinc-600 self-center">
            {t('Clip')}
          </span>
          <div
            ref={railRef}
            className="relative flex-1 h-6 bg-zinc-800/80 cursor-pointer overflow-visible"
            title={t('Click: move playhead. Drag the range: move it. Drag the playhead: scrub.')}
            onPointerDown={(e) => {
              if (e.button !== 0 || e.target !== e.currentTarget) return;
              const rail = railRef.current;
              if (!rail) return;
              const rect = rail.getBoundingClientRect();
              if (rect.width <= 0) return;
              const kind = clipRailDragTarget(
                e.clientX - rect.left, rect.width, playFrac, selStartFrac, selEndFrac,
              );
              if (kind === 'playhead') {
                beginPlayheadScrub(e);
                return;
              }
              if (kind === 'range') {
                beginWindowDrag(e);
                return;
              }
              seekTo(fracToSec((e.clientX - rect.left) / rect.width, railView));
              beginPlayheadScrub(e);
            }}
          >
            <div
              className="absolute top-0 bottom-0 touch-none select-none"
              style={{
                left: `${selStartFrac}%`,
                width: `${Math.max(0, selEndFrac - selStartFrac)}%`,
                cursor: windowDragging ? 'grabbing' : 'grab',
              }}
              title={t('Drag to move the clip range — preview scrubs; release resumes from the playhead')}
              onPointerDown={(e) => {
                const rail = railRef.current;
                if (!rail) return;
                const rect = rail.getBoundingClientRect();
                const kind = clipRailDragTarget(
                  e.clientX - rect.left, rect.width, playFrac, selStartFrac, selEndFrac,
                );
                if (kind === 'playhead') {
                  beginPlayheadScrub(e);
                  return;
                }
                beginWindowDrag(e);
              }}
              onClick={() => {
                // Range click must not steal the playhead — playback stays
                // on the triangle even when it sits outside the selection.
                suppressBarClickRef.current = false;
              }}
            >
              <div className="absolute top-1/2 -translate-y-1/2 h-1.5 w-full bg-[#9146FF]/60 pointer-events-none" />
            </div>
            <div
              className="absolute -top-1.5 bottom-0 w-4 -translate-x-1/2 z-[3] touch-none cursor-ew-resize"
              style={{ left: `${playFrac}%` }}
              title={t('Scrub playhead')}
              onPointerDown={beginPlayheadScrub}
            >
              <div className="absolute top-0 left-1/2 -translate-x-1/2 w-0 h-0 border-l-[4px] border-r-[4px] border-t-[5px] border-l-transparent border-r-transparent border-t-white" />
              <div className="absolute top-1.5 bottom-0 left-1/2 w-px bg-white/80 -translate-x-1/2 pointer-events-none" />
            </div>
            <div
              role="slider"
              aria-label={t('Clip start')}
              aria-valuemin={win.start}
              aria-valuemax={win.end}
              aria-valuenow={selection.start}
              className="absolute top-0 bottom-0 w-2 -translate-x-1/2 z-[2] touch-none cursor-ew-resize bg-white border-x border-zinc-900"
              style={{ left: `${selStartFrac}%` }}
              onPointerDown={(e) => beginHandleDrag(e, 'in')}
            />
            <div
              role="slider"
              aria-label={t('Clip end')}
              aria-valuemin={win.start}
              aria-valuemax={win.end}
              aria-valuenow={selection.end}
              className="absolute top-0 bottom-0 w-2 -translate-x-1/2 z-[2] touch-none cursor-ew-resize bg-[#9146FF] border-x border-zinc-900"
              style={{ left: `${selEndFrac}%` }}
              onPointerDown={(e) => beginHandleDrag(e, 'out')}
            />
          </div>
          <ClipDurationAdjustButtons
            compact
            onAdjust={adjustSelection}
            activeEndpoint={lastEndpoint}
            disabled={windowTooShort}
          />
          <span
            className="text-[9px] font-mono w-11 shrink-0 text-right text-zinc-300 tabular-nums self-center"
            title={t('Selected clip length')}
            data-clip-duration-seconds={Math.round(selLen)}
          >
            {Math.round(selLen)}s
          </span>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={clipTitle}
            readOnly
            aria-label={t('Original VOD title')}
            autoComplete="off"
            spellCheck={false}
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white px-2 py-1 text-[10px] font-mono opacity-80 cursor-not-allowed"
          />
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[8px] font-mono text-zinc-600 tabular-nums">
            {t('window {start} – {end}', { start: formatHmsFull(win.start), end: formatHmsFull(win.end) })}
          </span>
          <div className="flex items-center gap-1.5">
            <label
              className="flex items-center gap-1 cursor-pointer select-none"
              title={t('Also download this range when creating the clip')}
            >
              <input
                type="checkbox"
                checked={downloadWithClip}
                onChange={(e) => setDownloadWithClip(e.target.checked)}
                className="accent-[#9146FF]"
              />
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-400">
                {t('Download')}
              </span>
            </label>
            <button
              type="button"
              onClick={() => void createInBrowser()}
              disabled={createDisabled}
              className="border-2 border-[#9146FF] bg-[#9146FF]/20 px-2.5 py-1 text-white hover:bg-[#9146FF]/35 disabled:opacity-40 disabled:pointer-events-none"
              title={createDisabledTitle}
              aria-label={t('Create clip')}
            >
              <TwitchLogoIcon size={12} />
            </button>
          </div>
        </div>
        {clipNotice && (
          <div className={`flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-wider ${
            clipNotice.kind === 'error' ? 'text-red-400' : 'text-[#53fc18]'
          }`}>
            <span className="truncate">{clipNotice.text}</span>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
