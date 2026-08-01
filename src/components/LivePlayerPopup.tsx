import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Maximize2, Minimize2, Pause, Play, Volume2, VolumeX } from 'lucide-react';
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
  bitrate: number;
}

const POPUP_WIDTH = 480;
const POPUP_HEIGHT = 320;
const POPUP_MIN_H = 200;
const RESIZE_MARGIN = 32; // keep at least 16px of the popup on screen while resizing

export function LivePlayerPopup({ entry, channelName, onClose }: LivePlayerPopupProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const hlsRef = useRef<any>(null);
  const sessionIdRef = useRef<string | null>(null);
  const sizeRef = useRef<PanelSize>({ w: POPUP_WIDTH, h: POPUP_HEIGHT });
  const [position, setPosition] = useState({ x: window.innerWidth - POPUP_WIDTH - 24, y: 80 });
  const posRef = useRef(position);
  const [size, setSize] = useState<PanelSize>({ w: POPUP_WIDTH, h: POPUP_HEIGHT });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
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

  // Keep refs in sync with state (drag/resize use the latest size without re-subscribing)
  useEffect(() => { sizeRef.current = size; }, [size]);
  useEffect(() => { posRef.current = position; }, [position]);

  // Handle level selection
  const handleQualitySelect = useCallback((index: number) => {
    if (hlsRef.current) {
      hlsRef.current.currentLevel = index;
      setCurrentLevel(index);
    }
    setQualityMenuOpen(false);
  }, []);

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

  // Cleanup player on unmount
  const cleanup = useCallback(() => {
    abortRef.abort();
    if (hlsRef.current) {
      hlsRef.current.destroy();
      hlsRef.current = null;
    }
    const sid = sessionIdRef.current;
    if (sid) {
      apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      sessionIdRef.current = null;
    }
    if (videoRef.current) {
      videoRef.current.src = '';
      videoRef.current.load();
    }
  }, [abortRef]);

  // Close handler
  const handleClose = useCallback(() => {
    cleanup();
    onClose();
  }, [cleanup, onClose]);

  // Create preview session on mount
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        setLoading(true);
        setError(null);
        const body: Record<string, any> = { url: entry.url, is_live: true };
        if (entry.headers) body.headers = entry.headers;
        if (entry.platform) body.platform = entry.platform;

        const res = await apiPost<PreviewSessionResponse>('/api/preview/live', body);
        if (cancelled) return;
        if (!res) { setError('No response from server'); setLoading(false); return; }

        sessionIdRef.current = res.session_id;

        // Attach player to video element
        const video = videoRef.current;
        if (!video) { setLoading(false); return; }

        const src = res.master_url || res.playback_url;
        // Live sessions return playback_url == master_url (both .m3u8) — decide by kind, not presence.
        const isHls = res.kind === 'hls' || !!src && src.includes('.m3u8');
        if (src && !isHls) {
          // Progressive stream
          video.src = src;
          video.addEventListener('loadedmetadata', () => setLoading(false), { once: true });
          video.play().catch(() => {});
        } else if (src) {
          // HLS stream — use hls.js if available, else native HLS
          try {
            const Hls = (await import('hls.js')).default;
            if (cancelled) return;
            if (Hls.isSupported()) {
              const hls = new Hls();
              hlsRef.current = hls;
              hls.loadSource(src);
              hls.attachMedia(video);

              hls.on(Hls.Events.MANIFEST_PARSED, () => {
                if (cancelled) return;
                setLoading(false);
                // Populate quality levels
                const lvls: LevelInfo[] = hls.levels.map((l: any, i: number) => ({
                  index: i,
                  label: `${l.height}p (${(l.bitrate / 1000).toFixed(0)}kbps)`,
                  height: l.height,
                  bitrate: l.bitrate,
                }));
                setLevels(lvls);
              });

              hls.on(Hls.Events.LEVEL_SWITCHED, (_e: any, data: any) => {
                setCurrentLevel(data.level);
              });
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
              video.src = src;
              video.addEventListener('loadedmetadata', () => setLoading(false), { once: true });
              video.play().catch(() => {});
            } else {
              setError('HLS not supported in this browser');
              setLoading(false);
            }
          } catch {
            // hls.js failed to load, try native HLS
            if (video.canPlayType('application/vnd.apple.mpegurl')) {
              video.src = src;
              video.addEventListener('loadedmetadata', () => setLoading(false), { once: true });
              video.play().catch(() => {});
            } else {
              setError('HLS not supported');
              setLoading(false);
            }
          }
        } else {
          setLoading(false);
        }

        video.play().catch(() => {});
      } catch (err: any) {
        if (!cancelled) {
          setError(err?.message || 'Failed to start live stream');
          setLoading(false);
        }
      }
    })();

    return () => { cancelled = true; };
  }, [entry.url, entry.headers, entry.platform, abortRef]);

  // Sync transport state from the video element
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onPlay = () => setPaused(false);
    const onPause = () => setPaused(true);
    const onVolumeChange = () => { setMuted(video.muted); setVolume(video.volume); };
    video.addEventListener('play', onPlay);
    video.addEventListener('pause', onPause);
    video.addEventListener('volumechange', onVolumeChange);
    return () => {
      video.removeEventListener('play', onPlay);
      video.removeEventListener('pause', onPause);
      video.removeEventListener('volumechange', onVolumeChange);
    };
  }, []);

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
    if (video.paused) video.play().catch(() => {});
    else video.pause();
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
    if (hls && typeof hls.seekToLiveEdge === 'function') {
      hls.seekToLiveEdge();
      return;
    }
    const video = videoRef.current;
    if (!video) return;
    try {
      const s = video.seekable;
      if (s && s.length > 0) video.currentTime = s.end(s.length - 1);
    } catch {
      // native-HLS live edge can be behind the seekable end on some platforms
    }
  }, []);

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
    if ((e.target as HTMLElement).closest('.live-popup-close')) return;
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

  return createPortal(
    <div
      ref={popupRef}
      className="group"
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
        <span style={{ fontSize: 12, color: '#e06c75', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginRight: 8 }}>
          🔴 LIVE — {channelName}{entry.title ? ` — ${entry.title}` : ''}
        </span>

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
            Loading live stream…
          </div>
        )}

        {error && (
          <div
            style={{
              position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
              justifyContent: 'center', background: 'rgba(0,0,0,0.7)', color: '#e06c75', fontSize: 13,
              padding: 16, textAlign: 'center',
            }}
          >
            {error}
          </div>
        )}

        {/* Transport controls (live: seek disabled, LIVE snap-to-edge button) */}
        {!loading && !error && (
          <div
            data-live-transport
            className="flex items-center gap-1.5 px-2 py-1.5"
            style={{
              position: 'absolute',
              insetInline: 0,
              bottom: 0,
              zIndex: 10,
              background: 'linear-gradient(to top, rgba(0,0,0,0.85), rgba(0,0,0,0))',
            }}
          >
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

            <div className="flex flex-1 items-center gap-1.5">
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={1}
                disabled
                className="h-1 flex-1 accent-red-500 opacity-60"
                aria-label="Seeking disabled on live stream"
                title="Seeking is disabled on live streams"
              />
              <button
                type="button"
                onClick={snapToLiveEdge}
                title="Snap to live edge"
                className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-red-500 hover:bg-white/10"
              >
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-red-500" />
                LIVE
              </button>
            </div>

            <PreviewQualityMenu
              levels={levels}
              currentLevel={currentLevel}
              menuOpen={qualityMenuOpen}
              setMenuOpen={setQualityMenuOpen}
              onSelect={handleQualitySelect}
              disabled={!levels.length}
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
        )}
      </div>
    </div>,
    document.body,
  );
}
