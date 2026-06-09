import { useState, useEffect, useCallback } from 'react';
import {
  Download, Scissors, Info, Play, Link2, X, FastForward, Clock,
  Users, Database, Settings2, Tv, StopCircle, Loader2,
  CheckCircle2, AlertCircle, RefreshCw
} from 'lucide-react';

// ─── TYPES ───────────────────────────────────────────────────────────────────

interface VideoInfo {
  id: string;
  title: string | null;
  duration: number | null;
  duration_string: string | null;
  uploader: string | null;
  thumbnail: string | null;
  webpage_url: string | null;
  extractor: string | null;
  is_live: boolean | null;
  qualities: string[];
  platform: string | null;
}

interface DownloadState {
  download_id: string;
  url: string;
  type: string;
  platform: string;
  status: string;
  progress: number;
  output_file: string;
  error: string | null;
  started_at: string;
}

interface ChannelVideo {
  id: string;
  platform: string;
  title: string;
  duration: number | null;
  created_at: string | null;
  views: number | null;
  thumbnail_url: string | null;
}

interface AppSettings {
  download_threads: number;
  max_cache_mb: number;
  throttle_kib: number;
  ffmpeg_path: string;
  temp_folder: string;
  oauth: string;
  quality: string;
}

type Tab = 'url' | 'channels' | 'queue' | 'settings';

// ─── API ─────────────────────────────────────────────────────────────────────

const API_BASE = '';

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

function fmtDuration(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtShort(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function parseHms(t: string): number {
  const p = t.split(':').map(Number);
  if (p.length !== 3 || p.some(isNaN)) return 0;
  return p[0] * 3600 + p[1] * 60 + p[2];
}

function bytes(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(0)} MB`;
}

// ─── APP ─────────────────────────────────────────────────────────────────────

export default function App() {
  const [tab, setTab] = useState<Tab>('url');

  // URL mode
  const [url, setUrl] = useState('');
  const [videoInfo, setVideoInfo] = useState<VideoInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Download options
  const [quality, setQuality] = useState('1080p');
  const [startTime, setStartTime] = useState('00:00:00');
  const [endTime, setEndTime] = useState('04:20:00');
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState(0);

  // Queue
  const [downloads, setDownloads] = useState<DownloadState[]>([]);

  // Channels
  const [channelsUrl, setChannelsUrl] = useState('');
  const [channelVideos, setChannelVideos] = useState<ChannelVideo[]>([]);
  const [channelsLoading, setChannelsLoading] = useState(false);
  const [channelsError, setChannelsError] = useState<string | null>(null);

  // Settings
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [settingsSaved, setSettingsSaved] = useState(false);

  // ── Fetch video info ──

  const handleGetInfo = useCallback(async () => {
    if (!url.trim()) return;
    setLoading(true);
    setError(null);
    setVideoInfo(null);
    try {
      const info = await apiGet<VideoInfo>(`/api/info/video?id=${encodeURIComponent(url.trim())}`);
      setVideoInfo(info);
      if (info.duration) {
        const total = Math.floor(info.duration);
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const pad = (n: number) => n.toString().padStart(2, '0');
        setEndTime(`${pad(h)}:${pad(m)}:${pad(s)}`);
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [url]);

  // ── Start download ──

  const handleStartDownload = useCallback(async () => {
    if (!videoInfo) return;
    setError(null);
    const cropStart = parseHms(startTime) > 0 ? parseHms(startTime) : undefined;
    const cropEnd = parseHms(endTime) > 0 ? parseHms(endTime) : undefined;
    try {
      const result = await apiPost<{ download_id: string; status: string }>('/api/download/video', {
        url: url.trim(),
        quality: quality || undefined,
        crop_start: cropStart,
        crop_end: cropEnd,
      });
      setDownloading(result.download_id);
      setDownloadProgress(0);
      setTab('queue');
      refreshDownloads();
    } catch (err: any) {
      setError(err.message);
    }
  }, [videoInfo, url, quality, startTime, endTime]);

  // ── Cancel download ──

  const handleCancel = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/cancel`, {});
      refreshDownloads();
    } catch (err: any) {
      setError(err.message || 'Failed to cancel download');
    }
  }, []);

  // ── Refresh downloads ──

  const refreshDownloads = useCallback(async () => {
    try {
      const list = await apiGet<DownloadState[]>('/api/downloads');
      setDownloads(list);
    } catch {}
  }, []);

  // Poll downloads while on queue tab
  useEffect(() => {
    if (tab !== 'queue') return;
    refreshDownloads();
    const id = setInterval(refreshDownloads, 2000);
    return () => clearInterval(id);
  }, [tab, refreshDownloads]);

  // ── Channel browsing ──

  const handleBrowseChannel = useCallback(async () => {
    if (!channelsUrl.trim()) return;
    setChannelsLoading(true);
    setChannelsError(null);
    setChannelVideos([]);
    try {
      const data = await apiGet<{ videos: ChannelVideo[]; channel: string; platform: string }>(
        `/api/channel/videos?url=${encodeURIComponent(channelsUrl.trim())}&limit=20`
      );
      setChannelVideos(data.videos);
    } catch (err: any) {
      setChannelsError(err.message);
    } finally {
      setChannelsLoading(false);
    }
  }, [channelsUrl]);

  // ── Load settings ──

  const loadSettings = useCallback(async () => {
    try {
      const s = await apiGet<AppSettings>('/api/settings');
      setSettings(s);
      if (s.quality) setQuality(s.quality);
    } catch {}
  }, []);

  useEffect(() => {
    if (tab === 'settings') loadSettings();
  }, [tab, loadSettings]);

  const handleSaveSettings = useCallback(async () => {
    if (!settings) return;
    try {
      await apiPost('/api/settings', settings);
      setSettingsSaved(true);
      setError(null);
      setTimeout(() => setSettingsSaved(false), 2000);
    } catch (err: any) {
      setError(err.message || 'Failed to save settings');
    }
  }, [settings]);

  // ── SSE progress tracking ──
  useEffect(() => {
    if (!downloading) return;
    const es = new EventSource(`/api/download/${downloading}/stream`);
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'progress') setDownloadProgress(data.data);
        if (data.type === 'complete' || data.type === 'error') {
          es.close();
          setDownloading(null);
          refreshDownloads();
        }
      } catch {}
    };
    es.onerror = () => {
      es.close();
      setDownloading(null);
      refreshDownloads();
    };
    return () => { es.close(); };
  }, [downloading, refreshDownloads]);

  // ── Fill VOD from channel ──
  const selectVod = useCallback((vodUrl: string) => {
    setUrl(vodUrl);
    setTab('url');
    setVideoInfo(null);
  }, []);

  // ── Size estimate ──
  const clipSec = Math.max(0, parseHms(endTime) - parseHms(startTime));
  const rates: Record<string, number> = {
    source: 180, '1080p60': 120, '1080p': 120, '720p60': 70,
    '720p': 70, '480p': 35, '360p': 18,
  };
  const mbPerMin = rates[quality] || 70;
  const estSize = (clipSec / 60) * mbPerMin;

  return (
    <div className="min-h-screen flex items-center justify-center p-4 selection:bg-[#53fc18] selection:text-black bg-[#09090b]"
         style={{ backgroundImage: 'radial-gradient(#27272a 1px, transparent 1px)', backgroundSize: '24px 24px' }}>
      <div className="relative w-full max-w-md bg-zinc-950 border-4 border-white p-6 flex flex-col gap-5 shadow-[6px_6px_0px_0px_#53fc18,12px_12px_0px_0px_#9146FF] transition-all duration-300">

        {/* ── HEADER ── */}
        <div className="flex justify-between items-start">
          <div className="flex flex-col">
            <h1 className="text-4xl md:text-5xl font-black uppercase tracking-tighter flex items-center gap-2">
              VOD<span className="text-[#9146FF]">.</span>RIP
            </h1>
            <p className="text-zinc-400 text-[10px] font-mono tracking-widest uppercase mt-1">
              <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> Downloader
            </p>
          </div>
          <div className="flex gap-1 mt-2">
            <div className="w-2 h-2 bg-[#53fc18] rounded-full animate-pulse" />
            <div className="w-2 h-2 bg-[#9146FF] rounded-full animate-pulse" style={{ animationDelay: '0.5s' }} />
          </div>
        </div>

        {/* ── TABS ── */}
        <div className="flex w-full border-2 border-zinc-800 font-mono text-[10px] uppercase font-bold tracking-widest">
          {(['url', 'channels', 'queue', 'settings'] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 py-3 text-center transition-all flex items-center justify-center gap-2 ${
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
          <div className="border-2 border-red-500/50 bg-red-500/10 p-3 text-red-400 text-xs font-mono flex items-center gap-2">
            <AlertCircle size={14} />
            {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400/60 hover:text-red-400">
              <X size={14} />
            </button>
          </div>
        )}

        {/* ════════════════════════════ URL TAB ════════════════════════════ */}
        {tab === 'url' && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <div className="relative group">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none transition-colors text-white/40">
                  <Link2 size={18} strokeWidth={3} />
                </div>
                <input
                  type="text"
                  value={url}
                  onChange={(e) => { setUrl(e.target.value); setVideoInfo(null); }}
                  placeholder="PASTE VOD LINK HERE..."
                  onKeyDown={(e) => e.key === 'Enter' && handleGetInfo()}
                  className="w-full bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 pl-10 pr-10 py-3 focus:outline-none focus:border-white transition-colors uppercase text-sm"
                />
                {url && (
                  <button onClick={() => { setUrl(''); setVideoInfo(null); }}
                    className="absolute inset-y-0 right-0 pr-3 flex items-center text-zinc-500 hover:text-white">
                    <X size={18} strokeWidth={3} />
                  </button>
                )}
              </div>

              {!videoInfo && (
                <button
                  onClick={handleGetInfo}
                  disabled={!url || loading}
                  className="w-full bg-zinc-800 text-white font-black uppercase py-4 flex items-center justify-center gap-2 transition-all duration-300 disabled:opacity-50 border-2 border-zinc-700 hover:bg-white hover:text-black hover:shadow-[4px_4px_0px_0px_#53fc18]"
                >
                  {loading ? (
                    <><Loader2 size={18} className="animate-spin" /> Loading...</>
                  ) : (
                    <><Info size={18} strokeWidth={3} /> Extract Info</>
                  )}
                </button>
              )}

              {videoInfo && (
                <button onClick={() => setVideoInfo(null)}
                  className="text-[10px] text-zinc-500 hover:text-white uppercase font-bold text-right tracking-widest">
                  [ Change URL ]
                </button>
              )}
            </div>

            {/* Video Info */}
            {videoInfo && (
              <div className="flex flex-col gap-5 animate-in fade-in slide-in-from-top-2 duration-300">
                <div className="border-2 border-zinc-800 p-3 flex gap-3 bg-zinc-900 relative overflow-hidden group">
                  <div className={`absolute top-0 right-0 w-16 h-16 opacity-20 blur-2xl transition-colors duration-500 ${
                    videoInfo.platform?.toLowerCase() === 'kick' ? 'bg-[#53fc18]' : 'bg-[#9146FF]'
                  }`} />
                  <div className="w-20 h-14 bg-zinc-800 border border-zinc-700 flex items-center justify-center shrink-0 overflow-hidden">
                    {videoInfo.thumbnail ? (
                      <img src={videoInfo.thumbnail} alt="" className="w-full h-full object-cover" />
                    ) : (
                      <Play size={16} className="text-zinc-500" />
                    )}
                  </div>
                  <div className="flex flex-col justify-center overflow-hidden w-full">
                    <h3 className="font-bold truncate uppercase text-xs">
                      {videoInfo.title || 'Untitled'}
                    </h3>
                    <p className="text-[10px] text-zinc-400 font-mono mt-0.5 tracking-wider">
                      Channel: {videoInfo.uploader || 'Unknown'}
                    </p>
                    <div className="flex justify-between items-center mt-1.5 pt-1 border-t border-zinc-800/50">
                      <span className="text-[10px] text-zinc-500 font-mono flex items-center gap-1">
                        <Clock size={10} /> {videoInfo.duration_string || fmtDuration(videoInfo.duration || 0)}
                      </span>
                      <span className="text-[10px] text-white font-mono flex items-center gap-1 bg-zinc-800 px-1.5 py-0.5 rounded-sm">
                        <Database size={10} className={
                          videoInfo.platform?.toLowerCase() === 'kick' ? 'text-[#53fc18]' : 'text-[#9146FF]'
                        } /> {estSize >= 1024 ? `${(estSize/1024).toFixed(1)} GB` : `${estSize.toFixed(0)} MB`}
                      </span>
                    </div>
                  </div>
                </div>

                {/* Quality + Trim */}
                <div className="grid grid-cols-2 gap-3">
                  <div className="flex flex-col gap-1.5">
                    <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 flex items-center gap-1">
                      <Settings2 size={10} /> Quality
                    </label>
                    <select value={quality} onChange={(e) => setQuality(e.target.value)}
                      className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs cursor-pointer">
                      {videoInfo.qualities.length > 0 ? (
                        videoInfo.qualities.map((q) => <option key={q} value={q.toLowerCase()}>{q}</option>)
                      ) : (
                        <>
                          <option value="source">Source (1080p60)</option>
                          <option value="1080p">1080p</option>
                          <option value="720p">720p</option>
                          <option value="480p">480p</option>
                          <option value="360p">360p</option>
                        </>
                      )}
                    </select>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 flex items-center gap-1">
                      <FastForward size={10} /> Est. Size
                    </label>
                    <div className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs flex items-center justify-center">
                      {bytes(estSize)}
                    </div>
                  </div>
                </div>

                {/* Trim */}
                <div className="flex flex-col gap-2">
                  <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 flex items-center gap-1">
                    <Scissors size={10} /> Trim VOD
                  </label>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="flex flex-col gap-1">
                      <div className="relative">
                        <div className="absolute inset-y-0 left-0 pl-2 flex items-center pointer-events-none">
                          <span className="text-[10px] text-zinc-500 font-mono">ST</span>
                        </div>
                        <input type="text" value={startTime}
                          onChange={(e) => setStartTime(e.target.value)}
                          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-700 pl-7 py-2 focus:outline-none focus:border-[#53fc18] text-xs text-center" />
                      </div>
                    </div>
                    <div className="flex flex-col gap-1">
                      <div className="relative">
                        <div className="absolute inset-y-0 left-0 pl-2 flex items-center pointer-events-none">
                          <span className="text-[10px] text-zinc-500 font-mono">EN</span>
                        </div>
                        <input type="text" value={endTime}
                          onChange={(e) => setEndTime(e.target.value)}
                          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-700 pl-7 py-2 focus:outline-none focus:border-[#9146FF] text-xs text-center" />
                      </div>
                    </div>
                  </div>
                </div>

                {/* Download Button */}
                <button onClick={handleStartDownload}
                  disabled={!!downloading}
                  className="w-full relative group mt-1 disabled:opacity-50">
                  <div className="absolute inset-0 bg-gradient-to-r from-[#53fc18] to-[#9146FF] translate-x-1.5 translate-y-1.5 group-hover:translate-x-1 group-hover:translate-y-1 transition-transform" />
                  <div className="relative bg-black border-2 border-white py-4 flex items-center justify-center gap-3 group-hover:bg-white group-hover:text-black transition-colors">
                    <Download size={20} strokeWidth={3} className="group-hover:animate-bounce" />
                    <span className="font-black uppercase tracking-widest text-md">Rip VOD</span>
                  </div>
                </button>
              </div>
            )}
          </div>
        )}

        {/* ════════════════════════════ CHANNELS TAB ════════════════════════════ */}
        {tab === 'channels' && (
          <div className="flex flex-col gap-5">
            <div className="flex flex-col gap-2">
              <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                Browse Channel
              </label>
              <div className="flex gap-2">
                <input type="text" value={channelsUrl}
                  onChange={(e) => setChannelsUrl(e.target.value)}
                  placeholder="CHANNEL URL..."
                  onKeyDown={(e) => e.key === 'Enter' && handleBrowseChannel()}
                  className="flex-1 bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 px-3 py-2.5 focus:outline-none focus:border-white uppercase text-xs" />
                <button onClick={handleBrowseChannel} disabled={channelsLoading}
                  className="bg-white text-black font-black uppercase px-4 hover:bg-[#53fc18] hover:shadow-[4px_4px_0px_0px_#9146FF] transition-all text-xs border-2 border-white hover:border-[#53fc18] disabled:opacity-50">
                  {channelsLoading ? <Loader2 size={14} className="animate-spin" /> : 'Browse'}
                </button>
              </div>
            </div>

            {channelsError && (
              <div className="text-red-400 text-xs font-mono border-2 border-red-500/30 p-2">
                {channelsError}
              </div>
            )}

            <div className="flex flex-col gap-2 max-h-[320px] overflow-y-auto pr-1 custom-scrollbar">
              {channelVideos.length === 0 && !channelsLoading ? (
                <div className="text-center text-zinc-600 font-mono text-xs py-8 border-2 border-dashed border-zinc-800">
                  ENTER A CHANNEL URL TO BROWSE VODS.
                </div>
              ) : (
                channelVideos.map((v, i) => {
                  const isTw = v.platform === 'Twitch';
                  const color = isTw ? '#9146FF' : '#53fc18';
                  return (
                    <button key={i} onClick={() => selectVod(v.id)}
                      className="text-left text-xs bg-zinc-950 border border-zinc-800 p-2 hover:border-zinc-500 transition-colors flex justify-between items-center group/btn">
                      <span className="truncate flex items-center gap-2 font-mono text-zinc-300 group-hover/btn:text-white">
                        <Tv size={12} style={{ color }} />
                        {v.title || 'Untitled'}
                      </span>
                      <span className="text-[10px] text-zinc-600 font-mono shrink-0 ml-2">
                        {v.duration ? fmtShort(v.duration) : '?'}
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          </div>
        )}

        {/* ════════════════════════════ QUEUE TAB ════════════════════════════ */}
        {tab === 'queue' && (
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                Active Downloads
              </span>
              <button onClick={refreshDownloads} className="text-zinc-500 hover:text-white transition-colors">
                <RefreshCw size={14} />
              </button>
            </div>

            {downloading && (
              <div className="border-2 border-[#53fc18]/50 bg-zinc-900 p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-mono text-zinc-300">Downloading...</span>
                  <span className="text-xs font-mono text-[#53fc18]">{downloadProgress}%</span>
                </div>
                <div className="w-full h-2 bg-zinc-800 border border-zinc-700">
                  <div className="h-full bg-gradient-to-r from-[#53fc18] to-[#9146FF] transition-all duration-300"
                    style={{ width: `${downloadProgress}%` }} />
                </div>
              </div>
            )}

            <div className="flex flex-col gap-2 max-h-[320px] overflow-y-auto pr-1 custom-scrollbar">
              {downloads.length === 0 && !downloading ? (
                <div className="text-center text-zinc-600 font-mono text-xs py-8 border-2 border-dashed border-zinc-800">
                  NO ACTIVE DOWNLOADS.
                </div>
              ) : (
                downloads.map((dl) => {
                  const isTw = dl.platform === 'Twitch';
                  const color = isTw ? '#9146FF' : '#53fc18';
                  const isFinished = dl.status === 'Completed' || dl.status === 'Failed' || dl.status === 'Cancelled';
                  return (
                    <div key={dl.download_id}
                      className="border-2 border-zinc-800 bg-zinc-900/40 p-3 flex flex-col gap-2">
                      <div className="flex justify-between items-center">
                        <div className="flex items-center gap-2 min-w-0">
                          <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: color }} />
                          <span className="text-xs font-mono text-zinc-300 truncate">{dl.url}</span>
                        </div>
                        <span className={`text-[10px] font-mono shrink-0 ml-2 ${
                          dl.status === 'Completed' ? 'text-[#53fc18]' :
                          dl.status === 'Failed' ? 'text-red-400' :
                          dl.status === 'Cancelled' ? 'text-yellow-400' : 'text-zinc-400'
                        }`}>
                          {dl.status}
                        </span>
                      </div>
                      {!isFinished && (
                        <div className="w-full h-1.5 bg-zinc-800">
                          <div className="h-full bg-gradient-to-r from-[#53fc18] to-[#9146FF] transition-all"
                            style={{ width: `${dl.progress}%` }} />
                        </div>
                      )}
                      <div className="flex justify-between items-center text-[10px] text-zinc-500 font-mono">
                        <span>{dl.output_file.split('\\').pop()}</span>
                        {!isFinished && (
                          <button onClick={() => handleCancel(dl.download_id)}
                            className="text-zinc-500 hover:text-red-400 flex items-center gap-1">
                            <StopCircle size={12} /> Cancel
                          </button>
                        )}
                        {dl.error && <span className="text-red-400">{dl.error}</span>}
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}

        {/* ════════════════════════════ SETTINGS TAB ════════════════════════════ */}
        {tab === 'settings' && settings && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                  Download Threads
                </label>
                <input type="number" min={1} max={16}
                  value={settings.download_threads}
                  onChange={(e) => setSettings({ ...settings, download_threads: Math.max(1, Math.min(16, parseInt(e.target.value) || 4)) })}
                  className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                  Max Cache (MB)
                </label>
                <input type="number" min={50} max={2000}
                  value={settings.max_cache_mb}
                  onChange={(e) => setSettings({ ...settings, max_cache_mb: parseInt(e.target.value) || 200 })}
                  className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                Throttle (KiB/s, -1 = unlimited)
              </label>
              <input type="number" min={-1}
                value={settings.throttle_kib}
                onChange={(e) => setSettings({ ...settings, throttle_kib: parseInt(e.target.value) || -1 })}
                className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                Preferred Quality
              </label>
              <input type="text" value={settings.quality}
                onChange={(e) => setSettings({ ...settings, quality: e.target.value })}
                placeholder="e.g. 1080p"
                className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                Twitch OAuth Token
              </label>
              <input type="password" value={settings.oauth}
                onChange={(e) => setSettings({ ...settings, oauth: e.target.value })}
                placeholder="oauth_..."
                className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
            </div>

            <button onClick={handleSaveSettings}
              className="w-full bg-white text-black font-black uppercase py-3 flex items-center justify-center gap-2 hover:bg-[#53fc18] transition-all text-xs border-2 border-white hover:border-[#53fc18]">
              {settingsSaved ? <><CheckCircle2 size={16} /> Saved!</> : 'Save Settings'}
            </button>
          </div>
        )}

      </div>

      {/* Background */}
      <div className="fixed top-10 left-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        KICK
      </div>
      <div className="fixed bottom-10 right-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        TWITCH
      </div>
    </div>
  );
}
