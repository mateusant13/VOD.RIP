import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Settings } from 'lucide-react';
import { apiDelete, apiPost } from '../hooks/useApiClient';
import type { PreviewSessionResponse } from '../types';

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

type DragState = { startX: number; startY: number; offsetX: number; offsetY: number } | null;

interface LevelInfo {
  index: number;
  label: string;
  height: number;
  bitrate: number;
}

const POPUP_WIDTH = 480;
const POPUP_HEIGHT = 320;

export function LivePlayerPopup({ entry, channelName, onClose }: LivePlayerPopupProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const hlsRef = useRef<any>(null);
  const sessionIdRef = useRef<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [position, setPosition] = useState({ x: window.innerWidth - POPUP_WIDTH - 24, y: 80 });
  const [drag, setDrag] = useState<DragState>(null);

  // Quality state
  const [levels, setLevels] = useState<LevelInfo[]>([]);
  const [currentLevel, setCurrentLevel] = useState(-1);
  const [qualityMenuOpen, setQualityMenuOpen] = useState(false);
  const [abortRef] = useState(() => new AbortController());

  // Handle level selection
  const handleQualitySelect = useCallback((index: number) => {
    if (hlsRef.current) {
      hlsRef.current.currentLevel = index;
      setCurrentLevel(index);
    }
    setQualityMenuOpen(false);
  }, []);

  // Close quality menu on outside click
  useEffect(() => {
    if (!qualityMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(e.target as Node)) {
        setQualityMenuOpen(false);
      }
    };
    window.addEventListener('mousedown', handler);
    return () => window.removeEventListener('mousedown', handler);
  }, [qualityMenuOpen]);

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

  // Dragging handlers
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('.live-popup-close') || (e.target as HTMLElement).closest('.live-popup-quality')) return;
    setDrag({ startX: e.clientX, startY: e.clientY, offsetX: position.x, offsetY: position.y });
  }, [position]);

  useEffect(() => {
    if (!drag) return;
    const handleMouseMove = (e: MouseEvent) => {
      setPosition({
        x: Math.max(0, Math.min(window.innerWidth - 120, drag.offsetX + e.clientX - drag.startX)),
        y: Math.max(0, Math.min(window.innerHeight - 60, drag.offsetY + e.clientY - drag.startY)),
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

  return createPortal(
    <div
      ref={popupRef}
      onMouseDown={handleMouseDown}
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        width: POPUP_WIDTH,
        height: POPUP_HEIGHT,
        zIndex: 500,
        borderRadius: 8,
        overflow: 'hidden',
        boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
        background: '#111',
        border: '1px solid #333',
        cursor: drag ? 'grabbing' : 'default',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Header bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '6px 10px',
          background: '#1a1a2e',
          cursor: 'grab',
          userSelect: 'none',
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: 12, color: '#e06c75', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginRight: 8 }}>
          🔴 LIVE — {channelName}{entry.title ? ` — ${entry.title}` : ''}
        </span>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {/* Quality switcher */}
          {levels.length > 0 && (
            <div className="live-popup-quality relative" style={{ flexShrink: 0 }}>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setQualityMenuOpen((o) => !o);
                }}
                style={{
                  background: 'rgba(255,255,255,0.1)',
                  border: 'none',
                  color: '#ccc',
                  cursor: 'pointer',
                  fontSize: 13,
                  lineHeight: 1,
                  padding: '3px 6px',
                  borderRadius: 4,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 3,
                }}
                title="Video quality"
              >
                <Settings size={13} />
              </button>
              {qualityMenuOpen && (
                <div
                  style={{
                    position: 'absolute',
                    bottom: '100%',
                    right: 0,
                    marginBottom: 4,
                    background: '#1a1a2e',
                    border: '1px solid #444',
                    borderRadius: 6,
                    minWidth: 140,
                    boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
                    zIndex: 100,
                    padding: '4px 0',
                  }}
                >
                  {levels.map((l) => (
                    <button
                      key={l.index}
                      type="button"
                      onClick={() => handleQualitySelect(l.index)}
                      style={{
                        display: 'block',
                        width: '100%',
                        textAlign: 'left',
                        padding: '4px 10px',
                        fontSize: 11,
                        fontFamily: 'monospace',
                        background: l.index === currentLevel ? 'rgba(255,255,255,0.1)' : 'transparent',
                        border: 'none',
                        color: l.index === currentLevel ? '#fff' : '#999',
                        cursor: 'pointer',
                      }}
                    >
                      {l.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
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
            }}
            title="Close"
          >
            ✕
          </button>
        </div>
      </div>

      {/* Video area */}
      <div style={{ flex: 1, position: 'relative', background: '#000' }}>
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
      </div>
    </div>,
    document.body,
  );
}
