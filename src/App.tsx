import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Download, Scissors, Info, Play, Link2, X, FastForward, Clock,
  Users, Database, Settings2, Tv, StopCircle, Loader2,
  CheckCircle2, AlertCircle, RefreshCw, FolderOpen, Pencil, Plus
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
  title?: string | null;
  channel?: string | null;
}

interface DownloadsResponse {
  active: DownloadState[];
  history: DownloadState[];
}

interface ChannelVideo {
  id: string;
  platform: string;
  title: string;
  duration: number | null;
  created_at: string | null;
  views: number | null;
  thumbnail_url: string | null;
  url: string;
  channel: string;
}

interface AppSettings {
  download_folder: string;
  download_threads: number;
  max_cache_mb: number;
  throttle_kib: number;
  ffmpeg_path: string;
  temp_folder: string;
  oauth: string;
  quality: string;
}

interface SavedChannel {
  id: string;
  displayName: string;
  kickSlug: string;
  twitchSlug: string;
  videos: ChannelVideo[];
  errors: Record<string, string>;
  updatedAt: string;
  loading?: boolean;
}

type Tab = 'url' | 'channels' | 'queue' | 'settings';

// ─── API ─────────────────────────────────────────────────────────────────────

const API_BASE = '';

const BACKEND_HINT =
  'Backend not running. In a terminal run: npm run dev:all  (or npm run dev:api in one terminal and npm run dev in another). API must be on http://localhost:7897.';

function apiErrorMessage(res: Response, fallback: string): string {
  if (res.status === 500 || res.status === 502 || res.status === 503) {
    return BACKEND_HINT;
  }
  return fallback;
}

async function apiGet<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`);
  } catch {
    throw new Error(BACKEND_HINT);
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(apiErrorMessage(res, err.detail || `HTTP ${res.status}`));
  }
  return res.json();
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(BACKEND_HINT);
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(apiErrorMessage(res, err.detail || `HTTP ${res.status}`));
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
// Format a VOD's `created_at` for display. Backend returns either an ISO
// string (Kick) or YYYYMMDD (Twitch) — normalize to YYYY-MM-DD and drop
// anything we can't parse. Returns empty string when no date is present
// so the row can hide the date cell.
function fmtDate(value: string | null | undefined): string {
  if (!value) return '';
  const raw = String(value).trim();
  if (!raw) return '';
  // Twitch: YYYYMMDD
  if (/^\d{8}$/.test(raw)) {
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  }
  // ISO-ish: just take the first 10 chars if they look like a date.
  const m = raw.match(/^(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : '';
}

function parseHms(t: string): number {
  const p = t.split(':').map(Number);
  if (p.length !== 3 || p.some(isNaN)) return 0;
  return p[0] * 3600 + p[1] * 60 + p[2];
}

const CHANNEL_INITIAL_VISIBLE = 5;
const CHANNEL_EXPAND_STEP = 10;
const CHANNEL_FETCH_LIMIT = 100;
const CHANNELS_STORAGE_KEY = 'vodrip_saved_channels';
const MAX_SAVED_CHANNELS = 10;

function detectUrlPlatform(u: string): 'kick' | 'twitch' | null {
  const l = u.toLowerCase();
  if (l.includes('kick.com')) return 'kick';
  if (l.includes('twitch.tv')) return 'twitch';
  return null;
}

function actionBtnHover(platform: 'kick' | 'twitch' | null): string {
  if (platform === 'kick') {
    return 'hover:bg-[#53fc18] hover:text-black hover:border-[#53fc18] hover:shadow-[4px_4px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return 'hover:bg-[#9146FF] hover:text-black hover:border-[#9146FF] hover:shadow-[4px_4px_0px_0px_#9146FF]';
  }
  return 'hover:bg-white hover:text-black hover:border-white';
}

function loadSavedChannels(): SavedChannel[] {
  try {
    const raw = localStorage.getItem(CHANNELS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistChannels(channels: SavedChannel[]) {
  localStorage.setItem(CHANNELS_STORAGE_KEY, JSON.stringify(channels));
}

function basename(path: string): string {
  return path.split(/[/\\]/).pop() || path;
}

function parseVideoTs(value: string | null | undefined): number {
  if (!value) return 0;
  const raw = String(value).trim();
  if (/^\d{8}$/.test(raw)) {
    return new Date(`${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}T00:00:00Z`).getTime() || 0;
  }
  const t = Date.parse(raw);
  return Number.isNaN(t) ? 0 : t;
}

function buildVodUrl(v: ChannelVideo): string {
  const isTw = v.platform === 'Twitch';
  const twitchId = isTw && v.id.startsWith('v') ? v.id.slice(1) : v.id;
  return v.url || (isTw
    ? `https://www.twitch.tv/videos/${twitchId}`
    : `https://kick.com/${v.channel || ''}/videos/${v.id}`);
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
  const urlPlatform = detectUrlPlatform(url);
  const [startTime, setStartTime] = useState('00:00:00');
  const [endTime, setEndTime] = useState('04:20:00');
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState(0);

  // Queue
  const [activeDownloads, setActiveDownloads] = useState<DownloadState[]>([]);
  const [historyDownloads, setHistoryDownloads] = useState<DownloadState[]>([]);
  const [lastCompleted, setLastCompleted] = useState<DownloadState | null>(null);
  // Channels — persisted in localStorage (survives server restarts).
  const [savedChannels, setSavedChannels] = useState<SavedChannel[]>(() => loadSavedChannels());
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(
    () => loadSavedChannels()[0]?.id ?? null,
  );
  const [addChannelInput, setAddChannelInput] = useState('');
  const [channelsError, setChannelsError] = useState<string | null>(null);
  // Platform filter for channel browsing. Default: both enabled. Pure
  // presentation state — the backend always returns every VOD for the
  // channel across both platforms (capped to the last 14 days); this list
  // just narrows what we render.
  const [kickEnabled, setKickEnabled] = useState(true);
  const [twitchEnabled, setTwitchEnabled] = useState(true);
  // How many cached VODs to show per platform (expand is client-side only).
  const [kickVisibleLimit, setKickVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);
  const [twitchVisibleLimit, setTwitchVisibleLimit] = useState(CHANNEL_INITIAL_VISIBLE);

  const selectedChannel = useMemo(
    () => savedChannels.find((c) => c.id === selectedChannelId) ?? null,
    [savedChannels, selectedChannelId],
  );

  const allChannelVideos = selectedChannel?.videos ?? [];

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
    return items.sort((a, b) => parseVideoTs(b.created_at) - parseVideoTs(a.created_at));
  }, [
    kickChannelVideos,
    twitchChannelVideos,
    kickEnabled,
    twitchEnabled,
    kickVisibleLimit,
    twitchVisibleLimit,
  ]);

  const canExpandKick = kickEnabled && kickVisibleLimit < kickChannelVideos.length;
  const canExpandTwitch = twitchEnabled && twitchVisibleLimit < twitchChannelVideos.length;
  const canExpandChannelList = canExpandKick || canExpandTwitch;

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

  const pickDownloadFolder = useCallback(async (): Promise<string | null> => {
    const res = await apiPost<{ path: string | null }>('/api/pick-folder', {});
    if (res.path) {
      try {
        const s = await apiGet<AppSettings>('/api/settings');
        setSettings(s);
      } catch {
        setSettings((prev) => (prev ? { ...prev, download_folder: res.path! } : prev));
      }
    }
    return res.path;
  }, []);

  const ensureDownloadFolder = useCallback(async (): Promise<boolean> => {
    let folder = settings?.download_folder?.trim();
    if (!folder) {
      try {
        const s = await apiGet<AppSettings>('/api/settings');
        folder = s.download_folder?.trim();
        setSettings(s);
      } catch {
        /* ignore */
      }
    }
    if (folder) return true;
    const picked = await pickDownloadFolder();
    return Boolean(picked);
  }, [settings?.download_folder, pickDownloadFolder]);

  const openFolder = useCallback(async (filePath: string) => {
    if (!filePath) return;
    try {
      await apiPost('/api/open-folder', { path: filePath });
    } catch (err: any) {
      setError(err.message || 'Could not open folder');
    }
  }, []);

  // ── Start download ──

  const handleStartDownload = useCallback(async () => {
    if (!videoInfo) return;
    setError(null);
    if (!(await ensureDownloadFolder())) {
      setError('Choose a download folder to continue.');
      return;
    }
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
  }, [videoInfo, url, quality, startTime, endTime, ensureDownloadFolder]);

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
      const data = await apiGet<DownloadsResponse>('/api/downloads');
      setActiveDownloads(data.active || []);
      setHistoryDownloads(data.history || []);
      const live = data.active?.find((d) => d.download_id === downloading);
      if (live) setDownloadProgress(live.progress);
      if (downloading) {
        const done = data.history?.find(
          (d) => d.download_id === downloading && d.status === 'Completed',
        );
        if (done) setLastCompleted(done);
      }
    } catch {}
  }, [downloading]);

  // Poll downloads while on queue tab
  useEffect(() => {
    if (tab !== 'queue') return;
    refreshDownloads();
    const id = setInterval(refreshDownloads, 2000);
    return () => clearInterval(id);
  }, [tab, refreshDownloads]);

  // ── Channel browsing (localStorage) ──

  type ChannelResponse = {
    videos: ChannelVideo[];
    channel: string;
    platforms: string[];
    days: number;
    per_platform_errors?: Record<string, string>;
  };

  useEffect(() => {
    persistChannels(savedChannels);
  }, [savedChannels]);

  const updateChannel = useCallback((id: string, patch: Partial<SavedChannel>) => {
    setSavedChannels((prev) => prev.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }, []);

  const refreshChannel = useCallback(async (channelId: string) => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    updateChannel(channelId, { loading: true });
    setChannelsError(null);
    setKickVisibleLimit(CHANNEL_INITIAL_VISIBLE);
    setTwitchVisibleLimit(CHANNEL_INITIAL_VISIBLE);

    const errs: Record<string, string> = {};
    const merged: ChannelVideo[] = [];

    const fetchOne = async (platform: 'Kick' | 'Twitch', slug: string) => {
      const qs = `url=${encodeURIComponent(slug)}&limit=${CHANNEL_FETCH_LIMIT}&days=14&platforms=${encodeURIComponent(platform)}`;
      try {
        const data = await apiGet<ChannelResponse>(`/api/channel/videos?${qs}`);
        merged.push(...data.videos);
        const pe = data.per_platform_errors?.[platform];
        if (pe) errs[platform] = pe;
      } catch (err: any) {
        errs[platform] = err.message || `Failed to fetch ${platform}`;
      }
    };

    await fetchOne('Kick', ch.kickSlug);
    await new Promise((r) => setTimeout(r, 300));
    await fetchOne('Twitch', ch.twitchSlug);

    updateChannel(channelId, {
      videos: merged,
      errors: errs,
      loading: false,
      updatedAt: new Date().toISOString(),
    });

    const errKeys = Object.keys(errs).filter((k) => errs[k]);
    if (errKeys.length) {
      setChannelsError(
        merged.length
          ? `Partial results — ${errKeys.map((k) => `${k}: ${errs[k]}`).join(' | ')}`
          : errKeys.map((k) => `${k}: ${errs[k]}`).join(' | '),
      );
    }
  }, [savedChannels, updateChannel]);

  const handleAddChannel = useCallback(async () => {
    const raw = addChannelInput.trim();
    if (!raw) return;
    if (savedChannels.length >= MAX_SAVED_CHANNELS) {
      setChannelsError(`Maximum ${MAX_SAVED_CHANNELS} channels.`);
      return;
    }
    const slug = raw.replace(/^https?:\/\//, '').split('/').pop()?.split('?')[0] || raw;
    const id = `ch_${Date.now().toString(36)}`;
    const entry: SavedChannel = {
      id,
      displayName: slug,
      kickSlug: slug,
      twitchSlug: slug,
      videos: [],
      errors: {},
      updatedAt: '',
    };
    setSavedChannels((prev) => [...prev, entry]);
    setSelectedChannelId(id);
    setAddChannelInput('');
    await refreshChannel(id);
  }, [addChannelInput, savedChannels.length, refreshChannel]);

  const handleExpandChannelList = useCallback(() => {
    if (kickEnabled) setKickVisibleLimit((n) => n + CHANNEL_EXPAND_STEP);
    if (twitchEnabled) setTwitchVisibleLimit((n) => n + CHANNEL_EXPAND_STEP);
  }, [kickEnabled, twitchEnabled]);

  const renameChannelDisplay = useCallback((channelId: string) => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    const next = window.prompt('Display name for this channel:', ch.displayName);
    if (next?.trim()) updateChannel(channelId, { displayName: next.trim() });
  }, [savedChannels, updateChannel]);

  const editPlatformSlug = useCallback((channelId: string, platform: 'Kick' | 'Twitch') => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    const current = platform === 'Kick' ? ch.kickSlug : ch.twitchSlug;
    const next = window.prompt(`${platform} channel slug to fetch:`, current);
    if (!next?.trim()) return;
    const slug = next.trim();
    if (platform === 'Kick') updateChannel(channelId, { kickSlug: slug });
    else updateChannel(channelId, { twitchSlug: slug });
  }, [savedChannels, updateChannel]);

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

  const loadSettings = useCallback(async () => {
    try {
      const s = await apiGet<AppSettings>('/api/settings');
      setSettings(s);
      setQuality(s.quality || '1080p');
    } catch {}
  }, []);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

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
        if (data.type === 'progress') setDownloadProgress(Number(data.data) || 0);
        if (data.type === 'complete') {
          setDownloadProgress(100);
          es.close();
          setDownloading(null);
          refreshDownloads();
        }
        if (data.type === 'error') {
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
                  className={`w-full bg-zinc-800 text-white font-black uppercase py-4 flex items-center justify-center gap-2 transition-all duration-300 disabled:opacity-50 border-2 border-zinc-700 ${actionBtnHover(urlPlatform)}`}
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
                  <div className={`absolute inset-0 translate-x-1.5 translate-y-1.5 group-hover:translate-x-1 group-hover:translate-y-1 transition-transform ${
                    urlPlatform === 'kick' ? 'bg-[#53fc18]' : urlPlatform === 'twitch' ? 'bg-[#9146FF]' : 'bg-gradient-to-r from-[#53fc18] to-[#9146FF]'
                  }`} />
                  <div className={`relative bg-black border-2 border-white py-4 flex items-center justify-center gap-3 transition-colors ${
                    urlPlatform === 'kick'
                      ? 'group-hover:bg-[#53fc18] group-hover:text-black group-hover:border-[#53fc18]'
                      : urlPlatform === 'twitch'
                      ? 'group-hover:bg-[#9146FF] group-hover:text-black group-hover:border-[#9146FF]'
                      : 'group-hover:bg-white group-hover:text-black'
                  }`}>
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
                Add Channel (saved locally)
              </label>
              <div className="flex gap-2">
                <input type="text" value={addChannelInput}
                  onChange={(e) => setAddChannelInput(e.target.value)}
                  placeholder="CHANNEL SLUG OR URL..."
                  onKeyDown={(e) => e.key === 'Enter' && handleAddChannel()}
                  className="flex-1 bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 px-3 py-2.5 focus:outline-none focus:border-white uppercase text-xs" />
                <button onClick={handleAddChannel} disabled={channelsLoading || !addChannelInput.trim()}
                  className="bg-white text-black font-black uppercase px-3 hover:bg-white hover:shadow-[4px_4px_0px_0px_#9146FF] transition-all text-xs border-2 border-white disabled:opacity-50 flex items-center gap-1">
                  <Plus size={14} /> Add
                </button>
              </div>
            </div>

            <div className="flex flex-col gap-1.5 max-h-[120px] overflow-y-auto pr-1 custom-scrollbar">
              {savedChannels.length === 0 ? (
                <p className="text-[10px] text-zinc-600 font-mono py-2">No saved channels yet.</p>
              ) : savedChannels.map((ch) => (
                <div key={ch.id}
                  className={`flex items-center gap-1 border-2 px-2 py-1.5 ${
                    ch.id === selectedChannelId ? 'border-white bg-zinc-900' : 'border-zinc-800'
                  }`}>
                  <button type="button" onClick={() => setSelectedChannelId(ch.id)}
                    className="flex-1 text-left text-xs font-mono text-zinc-200 truncate hover:text-white">
                    {ch.displayName}
                    <span className="text-zinc-600 text-[9px] ml-1">
                      ({ch.kickSlug}/{ch.twitchSlug})
                    </span>
                  </button>
                  <button type="button" title="Rename display name"
                    onClick={() => renameChannelDisplay(ch.id)}
                    className="text-zinc-500 hover:text-white p-0.5">
                    <Pencil size={12} />
                  </button>
                  <button type="button" title="Refresh VODs"
                    onClick={() => refreshChannel(ch.id)}
                    disabled={ch.loading}
                    className="text-zinc-500 hover:text-white p-0.5 disabled:opacity-40">
                    {ch.loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                  </button>
                  <button type="button" title="Remove channel"
                    onClick={() => removeChannel(ch.id)}
                    className="text-zinc-600 hover:text-red-400 p-0.5">
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>

            {selectedChannel && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 mr-1">
                Filters
              </span>
              <div className="group relative">
                <label
                  className={`flex items-center gap-1.5 px-2 py-1 border-2 cursor-pointer font-mono text-[10px] uppercase font-bold tracking-wider transition-colors ${
                    kickEnabled
                      ? 'bg-[#53fc18]/15 border-[#53fc18] text-[#53fc18]'
                      : 'bg-zinc-950 border-zinc-800 text-zinc-500 hover:border-zinc-600'
                  }`}
                >
                  <input type="checkbox" checked={kickEnabled}
                    onChange={(e) => setKickEnabled(e.target.checked)}
                    className="accent-[#53fc18] w-3 h-3" />
                  Kick
                  {kickBrowseLoading && <Loader2 size={10} className="animate-spin" />}
                </label>
                <button type="button" title="Edit Kick slug"
                  onClick={() => selectedChannelId && editPlatformSlug(selectedChannelId, 'Kick')}
                  className="absolute -top-1 -right-1 opacity-0 group-hover:opacity-100 bg-zinc-900 border border-[#53fc18] text-[#53fc18] p-0.5 rounded-sm">
                  <Pencil size={10} />
                </button>
              </div>
              <div className="group relative">
                <label
                  className={`flex items-center gap-1.5 px-2 py-1 border-2 cursor-pointer font-mono text-[10px] uppercase font-bold tracking-wider transition-colors ${
                    twitchEnabled
                      ? 'bg-[#9146FF]/15 border-[#9146FF] text-[#9146FF]'
                      : 'bg-zinc-950 border-zinc-800 text-zinc-500 hover:border-zinc-600'
                  }`}
                >
                  <input type="checkbox" checked={twitchEnabled}
                    onChange={(e) => setTwitchEnabled(e.target.checked)}
                    className="accent-[#9146FF] w-3 h-3" />
                  Twitch
                  {twitchBrowseLoading && <Loader2 size={10} className="animate-spin" />}
                </label>
                <button type="button" title="Edit Twitch slug"
                  onClick={() => selectedChannelId && editPlatformSlug(selectedChannelId, 'Twitch')}
                  className="absolute -top-1 -right-1 opacity-0 group-hover:opacity-100 bg-zinc-900 border border-[#9146FF] text-[#9146FF] p-0.5 rounded-sm">
                  <Pencil size={10} />
                </button>
              </div>
            </div>
            )}
            {channelsError && (
              <div className="text-red-400 text-xs font-mono border-2 border-red-500/30 p-2">
                {channelsError}
              </div>
            )}
            {allChannelVideos.length > 0 && !channelsLoading && (
              <p className="text-[9px] font-mono text-zinc-500 uppercase tracking-wider">
                {kickEnabled && (
                  <span className="text-[#53fc18]">
                    Kick {Math.min(kickVisibleLimit, kickChannelVideos.length)}/{kickChannelVideos.length}
                  </span>
                )}
                {kickEnabled && twitchEnabled && <span className="text-zinc-700 mx-2">•</span>}
                {twitchEnabled && (
                  <span className="text-[#9146FF]">
                    Twitch {Math.min(twitchVisibleLimit, twitchChannelVideos.length)}/{twitchChannelVideos.length}
                  </span>
                )}
              </p>
            )}
            <div className="flex flex-col gap-2 max-h-[320px] overflow-y-auto pr-1 custom-scrollbar">
              {visibleChannelVideos.length === 0 && !channelsLoading ? (
                <div className="text-center text-zinc-600 font-mono text-xs py-8 border-2 border-dashed border-zinc-800">
                  {allChannelVideos.length === 0
                    ? 'ENTER A CHANNEL URL TO BROWSE VODS.'
                    : (kickEnabled || twitchEnabled
                        ? 'NO VODS FOUND FOR THE SELECTED PLATFORMS.'
                        : 'SELECT AT LEAST ONE PLATFORM FILTER.')}
                </div>
              ) : (
                visibleChannelVideos.map((v, i) => {
                  const isTw = v.platform === 'Twitch';
                  const color = isTw ? '#9146FF' : '#53fc18';
                  const dateStr = fmtDate(v.created_at);
                  const fullUrl = buildVodUrl(v);
                  return (
                    <button key={`${v.platform}-${v.id}-${i}`} onClick={() => selectVod(fullUrl)}
                      className="text-left text-xs bg-zinc-950 border border-zinc-800 p-2 hover:border-zinc-500 transition-colors flex flex-col gap-1 group/btn">
                      <div className="flex justify-between items-center gap-2">
                        <span className="truncate flex items-center gap-2 font-mono text-zinc-300 group-hover/btn:text-white">
                          <Tv size={12} style={{ color }} />
                          {v.title || 'Untitled'}
                        </span>
                        <span className="text-[10px] text-white font-mono font-bold shrink-0 ml-2">
                          {v.duration ? fmtShort(v.duration) : '?'}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 text-[9px] text-zinc-500 font-mono pl-5">
                        <span style={{ color }}>{v.platform}</span>
                        {dateStr && (
                          <>
                            <span className="text-zinc-700">•</span>
                            <span>{dateStr}</span>
                          </>
                        )}
                      </div>
                    </button>
                  );
                })
              )}
            </div>
            {canExpandChannelList && (
              <button
                type="button"
                onClick={handleExpandChannelList}
                disabled={kickBrowseLoading || twitchBrowseLoading}
                className="w-full border-2 border-zinc-700 text-zinc-300 hover:border-white hover:text-white font-mono text-[10px] uppercase font-bold tracking-wider py-2 transition-colors disabled:opacity-50"
              >
                Show more (+{CHANNEL_EXPAND_STEP} per platform)
              </button>
            )}
          </div>
        )}

        {/* ════════════════════════════ QUEUE TAB ════════════════════════════ */}
        {tab === 'queue' && (
          <div className="flex flex-col gap-4">
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
                  <span className="text-xs font-mono text-zinc-300 truncate pr-2">
                    {activeDownloads.find((d) => d.download_id === downloading)?.title || 'Downloading...'}
                  </span>
                  <span className="text-xs font-mono text-[#53fc18] shrink-0">{downloadProgress}%</span>
                </div>
                <div className="w-full h-2 bg-zinc-800 border border-zinc-700">
                  <div className="h-full bg-gradient-to-r from-[#53fc18] to-[#9146FF] transition-all duration-300"
                    style={{ width: `${Math.max(downloadProgress, 2)}%` }} />
                </div>
              </div>
            )}

            {lastCompleted?.output_file && (
              <div className="border-2 border-[#53fc18]/40 bg-zinc-900/60 p-2 flex flex-col gap-2">
                <span className="text-xs font-mono text-[#53fc18] truncate">
                  ✓ {lastCompleted.title || basename(lastCompleted.output_file)}
                </span>
                <button type="button" onClick={() => openFolder(lastCompleted.output_file)}
                  className="text-[10px] font-mono uppercase font-bold text-white hover:text-[#53fc18] flex items-center gap-1 w-fit">
                  <FolderOpen size={12} /> View in folder
                </button>
              </div>
            )}

            <div className="flex flex-col gap-2 max-h-[160px] overflow-y-auto pr-1 custom-scrollbar">
              {activeDownloads.length === 0 && !downloading ? (
                <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
                  NO ACTIVE DOWNLOADS.
                </div>
              ) : activeDownloads.map((dl) => {
                const isTw = dl.platform === 'Twitch';
                const color = isTw ? '#9146FF' : '#53fc18';
                return (
                  <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-900/40 p-3 flex flex-col gap-2">
                    <div className="flex justify-between items-center gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: color }} />
                        <span className="text-xs font-mono text-zinc-300 truncate">
                          {dl.title || dl.url}
                        </span>
                      </div>
                      <span className="text-[10px] font-mono text-zinc-400 shrink-0">{dl.status}</span>
                    </div>
                    <div className="w-full h-1.5 bg-zinc-800">
                      <div className="h-full bg-gradient-to-r from-[#53fc18] to-[#9146FF] transition-all"
                        style={{ width: `${Math.max(dl.progress, 2)}%` }} />
                    </div>
                    <div className="flex justify-between items-center text-[10px] text-zinc-500 font-mono gap-2">
                      <span className="truncate">{basename(dl.output_file)}</span>
                      <button onClick={() => handleCancel(dl.download_id)}
                        className="text-zinc-500 hover:text-red-400 flex items-center gap-1 shrink-0">
                        <StopCircle size={12} /> Cancel
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="border-t-2 border-zinc-800 pt-3 flex flex-col gap-2">
              <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                History
              </span>
              <div className="flex flex-col gap-2 max-h-[160px] overflow-y-auto pr-1 custom-scrollbar">
                {historyDownloads.length === 0 ? (
                  <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
                    NO HISTORY YET.
                  </div>
                ) : historyDownloads.map((dl) => {
                  const isTw = dl.platform === 'Twitch';
                  const color = isTw ? '#9146FF' : '#53fc18';
                  return (
                    <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-950 p-2 flex flex-col gap-1.5">
                      <div className="flex justify-between items-center gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: color }} />
                          <span className="text-xs font-mono text-zinc-300 truncate">
                            {dl.title || dl.url}
                          </span>
                        </div>
                        <span className={`text-[10px] font-mono shrink-0 ${
                          dl.status === 'Completed' ? 'text-[#53fc18]' :
                          dl.status === 'Failed' ? 'text-red-400' : 'text-yellow-400'
                        }`}>{dl.status}</span>
                      </div>
                      <div className="flex justify-between items-center text-[10px] text-zinc-500 font-mono gap-2">
                        <span className="truncate">{basename(dl.output_file)}</span>
                        {dl.status === 'Completed' && dl.output_file && (
                          <button type="button" onClick={() => openFolder(dl.output_file)}
                            className="text-zinc-400 hover:text-white flex items-center gap-1 shrink-0">
                            <FolderOpen size={12} /> Open folder
                          </button>
                        )}
                      </div>
                      {dl.error && <span className="text-[10px] text-red-400 font-mono">{dl.error}</span>}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        )}

        {/* ════════════════════════════ SETTINGS TAB ════════════════════════════ */}
        {tab === 'settings' && settings && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                Download Folder
              </label>
              <div className="flex gap-2">
                <input type="text" readOnly value={settings.download_folder || '(not set)'}
                  placeholder="Choose a folder..."
                  className="flex-1 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs truncate" />
                <button type="button" onClick={pickDownloadFolder}
                  className="bg-white text-black font-black uppercase px-3 text-[10px] border-2 border-white hover:bg-[#53fc18] hover:border-[#53fc18] shrink-0 flex items-center gap-1">
                  <FolderOpen size={14} /> Browse
                </button>
              </div>
            </div>

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
              <input type="text" value={settings.quality || '1080p'}
                onChange={(e) => setSettings({ ...settings, quality: e.target.value })}
                placeholder="1080p"
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
