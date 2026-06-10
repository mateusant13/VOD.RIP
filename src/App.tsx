import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from 'react';
import Hls from 'hls.js';
import {
  Download, Scissors, Info, Play, Pause, Link2, X, FastForward, Clock,
  Users, Database, Settings2, StopCircle, Loader2,
  CheckCircle2, AlertCircle, RefreshCw, FolderOpen, Pencil, Plus,
  ExternalLink, Eye, Volume2, VolumeX, Maximize2, Minimize2, SkipBack, SkipForward, Settings,
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
  created_at?: string | null;
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

async function apiDelete(path: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
  } catch {
    throw new Error(BACKEND_HINT);
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(apiErrorMessage(res, err.detail || `HTTP ${res.status}`));
  }
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

function fmtRelativeAgo(value: string | null | undefined): string {
  const ts = parseVideoTs(value);
  if (!ts) return '';
  const diffMs = Math.max(0, Date.now() - ts);
  const hours = Math.floor(diffMs / (1000 * 60 * 60));
  const days = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (days >= 1) return days === 1 ? '1 day ago' : `${days} days ago`;
  if (hours >= 1) return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
  const mins = Math.floor(diffMs / (1000 * 60));
  if (mins >= 1) return mins === 1 ? '1 min ago' : `${mins} mins ago`;
  return 'just now';
}

function fmtDateAndAgo(value: string | null | undefined): string {
  const date = fmtDate(value);
  const ago = fmtRelativeAgo(value);
  if (date && ago) return `${date} · ${ago}`;
  return date || ago;
}

function parseHms(t: string): number {
  const p = t.split(':').map(Number);
  if (p.length !== 3 || p.some(isNaN)) return 0;
  return p[0] * 3600 + p[1] * 60 + p[2];
}

function formatHmsFull(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

const PREVIEW_DEFAULT_HEIGHT = 480;
const PREVIEW_FULLSCREEN_HEIGHT = 1080;
const PREVIEW_SKIP_SEC = 10;

interface PreviewLevelOption {
  index: number;
  height: number;
  label: string;
}

function previewLevelLabel(height: number, bitrate?: number): string {
  if (!height) return 'Auto';
  const kbps = bitrate ? Math.round(bitrate / 1000) : 0;
  return kbps > 0 ? `${height}p · ${kbps}k` : `${height}p`;
}

function levelIndexForHeight(levels: PreviewLevelOption[], target: number, preferHighest = false): number {
  if (!levels.length) return 0;
  if (preferHighest) {
    const atOrAbove = levels.filter((l) => l.height >= target);
    if (atOrAbove.length) {
      return atOrAbove.reduce((best, l) => (l.height > best.height ? l : best)).index;
    }
  }
  const matches = levels.filter((l) => l.height === target);
  if (matches.length) {
    const pick = preferHighest ? matches[matches.length - 1] : matches[0];
    return pick.index;
  }
  const below = levels.filter((l) => l.height > 0 && l.height < target);
  if (below.length && !preferHighest) {
    return below[below.length - 1].index;
  }
  const above = levels.filter((l) => l.height > target);
  if (above.length) {
    return above[0].index;
  }
  return levels[0].index;
}

function lowestLevelIndex(levels: PreviewLevelOption[]): number {
  if (!levels.length) return 0;
  return levels.reduce((best, l) => (l.height < best.height ? l : best)).index;
}

const CHANNEL_INITIAL_VISIBLE = 5;
const CHANNEL_EXPAND_STEP = 10;
const CHANNEL_FETCH_LIMIT = 100;
const CHANNELS_STORAGE_KEY = 'vodrip_saved_channels';
const MAX_SAVED_CHANNELS = 10;
/** Highest quality from API list, or source when none listed (Kick). */
function bestAvailableQuality(info: VideoInfo): string {
  if (info.qualities?.length) {
    return info.qualities[0].toLowerCase();
  }
  return 'source';
}

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

function detectVideoPlatform(info: VideoInfo | null, url: string): 'kick' | 'twitch' | null {
  const p = info?.platform?.toLowerCase();
  if (p === 'kick') return 'kick';
  if (p === 'twitch') return 'twitch';
  return detectUrlPlatform(url);
}

function platformCardShadow(platform: 'kick' | 'twitch' | null, compact = false): string {
  if (platform === 'kick') {
    return compact ? 'shadow-[4px_4px_0px_0px_#53fc18]' : 'shadow-[6px_6px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return compact ? 'shadow-[4px_4px_0px_0px_#9146FF]' : 'shadow-[6px_6px_0px_0px_#9146FF]';
  }
  return compact
    ? 'shadow-[4px_4px_0px_0px_#53fc18,6px_6px_0px_0px_#9146FF]'
    : 'shadow-[6px_6px_0px_0px_#53fc18,12px_12px_0px_0px_#9146FF]';
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

function parseChannelInput(raw: string): { displayName: string; kickSlug: string; twitchSlug: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { displayName: '', kickSlug: '', twitchSlug: '' };
  const lower = trimmed.toLowerCase();
  if (lower.includes('kick.com')) {
    const m = trimmed.match(/kick\.com\/([^/?#]+)/i);
    const slug = m?.[1] || trimmed;
    return { displayName: slug, kickSlug: slug, twitchSlug: slug };
  }
  if (lower.includes('twitch.tv')) {
    const m = trimmed.match(/twitch\.tv\/([^/?#]+)/i);
    const slug = m?.[1] || trimmed;
    return { displayName: slug, kickSlug: slug, twitchSlug: slug };
  }
  const slug = trimmed.replace(/^https?:\/\//, '').split('/').pop()?.split('?')[0] || trimmed;
  return { displayName: slug, kickSlug: slug, twitchSlug: slug };
}

/** Settings/section captions — not <label> so clicks never focus nearby inputs. */
function FieldCaption({ children }: { children: ReactNode }) {
  return (
    <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 block">
      {children}
    </span>
  );
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
  const [quality, setQuality] = useState('source');
  const urlPlatform = detectUrlPlatform(url);
  const [trimStartSec, setTrimStartSec] = useState(0);
  const [trimEndSec, setTrimEndSec] = useState(3600);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewSessionId, setPreviewSessionId] = useState<string | null>(null);
  const [previewHlsUrl, setPreviewHlsUrl] = useState<string | null>(null);
  const [previewVideoLoading, setPreviewVideoLoading] = useState(false);
  const [previewVideoReady, setPreviewVideoReady] = useState(false);
  const [previewCurrentTime, setPreviewCurrentTime] = useState(0);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const [previewMuted, setPreviewMuted] = useState(true);
  const [previewFullscreen, setPreviewFullscreen] = useState(false);
  const [previewLevels, setPreviewLevels] = useState<PreviewLevelOption[]>([]);
  const [previewQualityLevel, setPreviewQualityLevel] = useState(0);
  const [previewQualityMenuOpen, setPreviewQualityMenuOpen] = useState(false);
  const [channelVodPanelOpen, setChannelVodPanelOpen] = useState(false);
  const [previewTrimStart, setPreviewTrimStart] = useState(0);
  const [previewTrimEnd, setPreviewTrimEnd] = useState(3600);
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  const previewContainerRef = useRef<HTMLDivElement>(null);
  const previewControlsRef = useRef<HTMLDivElement>(null);
  const previewHlsRef = useRef<Hls | null>(null);
  const previewQualityBeforeFullscreenRef = useRef<number | null>(null);
  const previewPausedBeforeFullscreenRef = useRef(false);
  const previewInitialSeekDoneRef = useRef(false);
  const previewInitialPlayDoneRef = useRef(false);
  const previewSuppressPlayRef = useRef(false);
  const previewTrimStartRef = useRef(0);
  const previewTrimEndRef = useRef(3600);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState(0);

  // Queue
  const [activeDownloads, setActiveDownloads] = useState<DownloadState[]>([]);
  const [historyDownloads, setHistoryDownloads] = useState<DownloadState[]>([]);
  const [lastCompleted, setLastCompleted] = useState<DownloadState | null>(null);
  // Channels — persisted in localStorage (survives server restarts).
  const [savedChannels, setSavedChannels] = useState<SavedChannel[]>(() => loadSavedChannels());
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);
  const [addChannelInput, setAddChannelInput] = useState('');
  const [editingChannelId, setEditingChannelId] = useState<string | null>(null);
  const [editingChannelName, setEditingChannelName] = useState('');
  const [editingSlug, setEditingSlug] = useState<{ channelId: string; platform: 'Kick' | 'Twitch' } | null>(null);
  const [editingSlugValue, setEditingSlugValue] = useState('');
  const [channelsError, setChannelsError] = useState<string | null>(null);
  const [pickingFolder, setPickingFolder] = useState(false);
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

  const vodDurationSec = useMemo(
    () => Math.max(1, Math.floor(videoInfo?.duration || trimEndSec || 1)),
    [videoInfo?.duration, trimEndSec],
  );

  const destroyPreviewPlayer = useCallback(() => {
    if (previewHlsRef.current) {
      previewHlsRef.current.destroy();
      previewHlsRef.current = null;
    }
    const video = previewVideoRef.current;
    if (video) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
  }, []);

  const resetPreview = useCallback(async () => {
    const sid = previewSessionId;
    destroyPreviewPlayer();
    setPreviewOpen(false);
    setPreviewSessionId(null);
    setPreviewHlsUrl(null);
    setPreviewVideoLoading(false);
    setPreviewVideoReady(false);
    setPreviewCurrentTime(0);
    setPreviewPlaying(false);
    setPreviewFullscreen(false);
    setPreviewLevels([]);
    setPreviewQualityLevel(0);
    setPreviewQualityMenuOpen(false);
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    if (sid) {
      try { await apiDelete(`/api/preview/session/${sid}`); } catch { /* ignore */ }
    }
  }, [previewSessionId, destroyPreviewPlayer]);

  const seekPreviewVideo = useCallback((sec: number) => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    const t = Math.max(0, Math.min(sec, vodDurationSec));
    if (Math.abs(video.currentTime - t) > 0.3) {
      video.currentTime = t;
      setPreviewCurrentTime(t);
    }
  }, [previewVideoReady, vodDurationSec]);

  const openPreview = useCallback(async () => {
    if (!url.trim() || trimEndSec <= trimStartSec) return;
    previewTrimStartRef.current = trimStartSec;
    previewTrimEndRef.current = trimEndSec;
    setPreviewTrimStart(trimStartSec);
    setPreviewTrimEnd(trimEndSec);
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    setPreviewOpen(true);
    setPreviewVideoLoading(true);
    setPreviewVideoReady(false);
    setError(null);
    try {
      if (previewSessionId) {
        try { await apiDelete(`/api/preview/session/${previewSessionId}`); } catch { /* ignore */ }
      }
      destroyPreviewPlayer();
      const res = await apiPost<{ session_id: string; master_url: string }>('/api/preview/session', {
        url: url.trim(),
        crop_start: trimStartSec,
        crop_end: trimEndSec,
      });
      setPreviewSessionId(res.session_id);
      setPreviewHlsUrl(res.master_url);
    } catch (err: any) {
      setError(err.message || 'Preview failed');
      setPreviewOpen(false);
      setPreviewVideoLoading(false);
    }
  }, [url, trimEndSec, trimStartSec, previewSessionId, destroyPreviewPlayer]);

  useEffect(() => {
    if (!previewOpen || !previewHlsUrl) return;
    const video = previewVideoRef.current;
    if (!video) return;

    setPreviewVideoLoading(true);
    setPreviewVideoReady(false);

    const performInitialSeek = () => {
      if (previewInitialSeekDoneRef.current) return;
      previewInitialSeekDoneRef.current = true;
      const start = previewTrimStartRef.current;
      if (Number.isFinite(start) && start > 0 && Math.abs(video.currentTime - start) > 0.25) {
        video.currentTime = start;
      }
      setPreviewCurrentTime(video.currentTime);
    };

    const onCanPlay = () => {
      setPreviewVideoReady(true);
      setPreviewVideoLoading(false);
      performInitialSeek();
      if (!previewInitialPlayDoneRef.current && video.paused) {
        previewInitialPlayDoneRef.current = true;
        void video.play().catch(() => {});
      }
    };

    if (Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        maxBufferLength: 20,
        maxMaxBufferLength: 40,
        startFragPrefetch: true,
        capLevelToPlayerSize: false,
        fragLoadingTimeOut: 20000,
        manifestLoadingTimeOut: 10000,
        testBandwidth: false,
        startPosition: previewTrimStartRef.current,
      });
      previewHlsRef.current = hls;
      hls.loadSource(previewHlsUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        const mapped: PreviewLevelOption[] = (data.levels ?? hls.levels).map((l, i) => ({
          index: i,
          height: l.height,
          label: previewLevelLabel(l.height, l.bitrate),
        }));
        mapped.sort((a, b) => a.height - b.height);
        setPreviewLevels(mapped);
        const defaultIdx = mapped.length > 1
          ? levelIndexForHeight(mapped, PREVIEW_DEFAULT_HEIGHT)
          : lowestLevelIndex(mapped);
        hls.loadLevel = defaultIdx;
        setPreviewQualityLevel(defaultIdx);
      });
      video.addEventListener('canplay', onCanPlay, { once: true });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            hls.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError();
            break;
          default:
            setError('Preview playback failed — try again');
            setPreviewVideoLoading(false);
            hls.destroy();
            previewHlsRef.current = null;
            break;
        }
      });
      return () => {
        video.removeEventListener('canplay', onCanPlay);
        hls.destroy();
        previewHlsRef.current = null;
      };
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = previewHlsUrl;
      video.addEventListener('canplay', onCanPlay, { once: true });
      return () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeAttribute('src');
        video.load();
      };
    }

    setError('HLS playback is not supported in this browser');
    setPreviewVideoLoading(false);
  }, [previewOpen, previewHlsUrl]);

  const handlePreviewTimeUpdate = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video) return;
    const t = video.currentTime;
    setPreviewCurrentTime(t);
    const end = previewTrimEndRef.current;
    if (t >= end) {
      video.pause();
      if (Math.abs(video.currentTime - end) > 0.05) {
        video.currentTime = end;
      }
      setPreviewPlaying(false);
    }
  }, []);

  const togglePreviewPlay = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    if (video.paused) {
      const start = previewTrimStartRef.current;
      const end = previewTrimEndRef.current;
      if (video.currentTime >= end - 0.1) {
        video.currentTime = start;
      }
      void video.play();
      setPreviewPlaying(true);
    } else {
      video.pause();
      setPreviewPlaying(false);
    }
  }, [previewVideoReady]);

  const applyPreviewQuality = useCallback((levelIndex: number, forceLoad = false) => {
    const hls = previewHlsRef.current;
    const video = previewVideoRef.current;
    const wasPaused = video?.paused ?? true;
    if (hls && levelIndex >= 0 && levelIndex < hls.levels.length) {
      if (forceLoad) {
        hls.loadLevel = levelIndex;
      } else {
        hls.nextLevel = levelIndex;
      }
      if (wasPaused && video) {
        previewSuppressPlayRef.current = true;
        requestAnimationFrame(() => {
          video.pause();
          previewSuppressPlayRef.current = false;
        });
      }
    }
    setPreviewQualityLevel(levelIndex);
    setPreviewQualityMenuOpen(false);
  }, []);

  const skipPreview = useCallback((deltaSec: number) => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const t = Math.max(start, Math.min(end, video.currentTime + deltaSec));
    seekPreviewVideo(t);
  }, [previewVideoReady, seekPreviewVideo]);

  const togglePreviewFullscreen = useCallback(async () => {
    const container = previewContainerRef.current;
    if (!container || !previewVideoReady) return;
    const video = previewVideoRef.current;
    try {
      if (!document.fullscreenElement) {
        previewPausedBeforeFullscreenRef.current = video?.paused ?? true;
        previewQualityBeforeFullscreenRef.current = previewQualityLevel;
        await container.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      /* fullscreen denied or unsupported */
    }
  }, [previewVideoReady, previewQualityLevel]);

  useEffect(() => {
    const onFullscreenChange = () => {
      const fs = document.fullscreenElement === previewContainerRef.current;
      const video = previewVideoRef.current;
      const hls = previewHlsRef.current;
      setPreviewFullscreen(fs);

      if (fs && hls?.levels.length) {
        previewPausedBeforeFullscreenRef.current = video?.paused ?? true;
        const mapped: PreviewLevelOption[] = hls.levels.map((l, i) => ({
          index: i,
          height: l.height,
          label: previewLevelLabel(l.height, l.bitrate),
        }));
        const idx1080 = levelIndexForHeight(mapped, PREVIEW_FULLSCREEN_HEIGHT, true);
        applyPreviewQuality(idx1080, true);
        if (previewPausedBeforeFullscreenRef.current && video) {
          previewSuppressPlayRef.current = true;
          requestAnimationFrame(() => {
            video.pause();
            previewSuppressPlayRef.current = false;
          });
        }
        return;
      }

      if (!fs && previewQualityBeforeFullscreenRef.current !== null) {
        applyPreviewQuality(previewQualityBeforeFullscreenRef.current, true);
        previewQualityBeforeFullscreenRef.current = null;
        if (previewPausedBeforeFullscreenRef.current && video) {
          previewSuppressPlayRef.current = true;
          requestAnimationFrame(() => {
            video.pause();
            previewSuppressPlayRef.current = false;
          });
        }
      }
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, [applyPreviewQuality]);

  useEffect(() => {
    if (!previewQualityMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const root = previewControlsRef.current;
      if (root && !root.contains(e.target as Node)) {
        setPreviewQualityMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [previewQualityMenuOpen]);

  // ── Fetch video info ──

  const fetchVideoInfo = useCallback(async (videoUrl: string) => {
    const trimmed = videoUrl.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    setVideoInfo(null);
    try {
      const info = await apiGet<VideoInfo>(`/api/info/video?id=${encodeURIComponent(trimmed)}`);
      setVideoInfo(info);
      setQuality(bestAvailableQuality(info));
      const dur = info.duration ? Math.floor(info.duration) : 3600;
      setTrimStartSec(0);
      setTrimEndSec(Math.max(1, dur));
      void resetPreview();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [resetPreview]);

  const handleGetInfo = useCallback(() => fetchVideoInfo(url), [url, fetchVideoInfo]);

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
      setError(err.message || 'Could not open folder picker');
      return null;
    } finally {
      setPickingFolder(false);
    }
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
    const cropStart = trimStartSec > 0 ? trimStartSec : undefined;
    const cropEnd = trimEndSec > 0 ? trimEndSec : undefined;
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
  }, [videoInfo, url, quality, trimStartSec, trimEndSec, ensureDownloadFolder]);

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

  // ── Cancel download ──

  const handleCancel = useCallback(async (id: string) => {
    setActiveDownloads((prev) => prev.filter((d) => d.download_id !== id));
    if (downloading === id) {
      setDownloading(null);
      setDownloadProgress(0);
    }
    try {
      await apiPost(`/api/download/${id}/cancel`, {});
    } catch (err: any) {
      setError(err.message || 'Failed to cancel download');
    }
    refreshDownloads();
  }, [downloading, refreshDownloads]);

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

  const refreshChannel = useCallback(async (channelId: string, channelOverride?: SavedChannel) => {
    const ch = channelOverride ?? savedChannels.find((c) => c.id === channelId);
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
      setChannelsError(`Max ${MAX_SAVED_CHANNELS} channels.`);
      return;
    }
    const { displayName, kickSlug, twitchSlug } = parseChannelInput(raw);
    if (!kickSlug) return;
    const id = `ch_${Date.now().toString(36)}`;
    const entry: SavedChannel = {
      id,
      displayName,
      kickSlug,
      twitchSlug,
      videos: [],
      errors: {},
      updatedAt: '',
    };
    setSavedChannels((prev) => [...prev, entry]);
    setSelectedChannelId(id);
    setAddChannelInput('');
    await refreshChannel(id, entry);
  }, [addChannelInput, savedChannels.length, refreshChannel]);

  const toggleChannelSelection = useCallback((channelId: string) => {
    setSelectedChannelId((prev) => {
      if (prev === channelId) return null;
      setKickVisibleLimit(CHANNEL_INITIAL_VISIBLE);
      setTwitchVisibleLimit(CHANNEL_INITIAL_VISIBLE);
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

  const commitRenameChannel = useCallback(() => {
    if (!editingChannelId) return;
    const next = editingChannelName.trim();
    if (next) updateChannel(editingChannelId, { displayName: next });
    setEditingChannelId(null);
    setEditingChannelName('');
  }, [editingChannelId, editingChannelName, updateChannel]);

  const startEditPlatformSlug = useCallback((channelId: string, platform: 'Kick' | 'Twitch') => {
    const ch = savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    setEditingSlug({ channelId, platform });
    setEditingSlugValue(platform === 'Kick' ? ch.kickSlug : ch.twitchSlug);
  }, [savedChannels]);

  const commitEditPlatformSlug = useCallback(() => {
    if (!editingSlug) return;
    const slug = editingSlugValue.trim();
    if (!slug) return;
    if (editingSlug.platform === 'Kick') {
      updateChannel(editingSlug.channelId, { kickSlug: slug });
    } else {
      updateChannel(editingSlug.channelId, { twitchSlug: slug });
    }
    setEditingSlug(null);
    setEditingSlugValue('');
  }, [editingSlug, editingSlugValue, updateChannel]);

  const handleExpandChannelList = useCallback(() => {
    if (kickEnabled) setKickVisibleLimit((n) => n + CHANNEL_EXPAND_STEP);
    if (twitchEnabled) setTwitchVisibleLimit((n) => n + CHANNEL_EXPAND_STEP);
  }, [kickEnabled, twitchEnabled]);

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
    setChannelVodPanelOpen(true);
    void fetchVideoInfo(vodUrl);
  }, [fetchVideoInfo]);

  useEffect(() => {
    if (previewOpen && tab === 'channels') {
      setTab('url');
    }
  }, [previewOpen, tab]);

  useEffect(() => {
    if (tab !== 'channels') {
      setChannelVodPanelOpen(false);
    }
  }, [tab]);

  // ── Size estimate ──
  const clipSec = Math.max(0, trimEndSec - trimStartSec);

  const rates: Record<string, number> = {
    source: 180, '1080p60': 180, '1080p': 120, '720p60': 70,
    '720p': 70, '480p': 35, '360p': 18,
  };
  const mbPerMin = rates[quality] || 70;
  const estSize = (clipSec / 60) * mbPerMin;

  const activePlatform = detectVideoPlatform(videoInfo, url);
  const splitChannelsLayout = channelVodPanelOpen && tab === 'channels' && !previewOpen;
  const showUrlInSidebar = splitChannelsLayout;
  const showUrlInMainCard = tab === 'url' || previewOpen;
  const visibleTabs: Tab[] = previewOpen
    ? ['url', 'queue', 'settings']
    : ['url', 'channels', 'queue', 'settings'];

  const urlTabContent = (
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
              {videoInfo.created_at && (
                <p className="text-[10px] text-zinc-500 font-mono mt-0.5">
                  {fmtDateAndAgo(videoInfo.created_at)}
                </p>
              )}
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

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <FieldCaption>
                <span className="flex items-center gap-1"><Settings2 size={10} /> Quality</span>
              </FieldCaption>
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
              <FieldCaption>
                <span className="flex items-center gap-1"><FastForward size={10} /> Est. Size</span>
              </FieldCaption>
              <div className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs flex items-center justify-center">
                {bytes(estSize)}
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <FieldCaption>
              <span className="flex items-center gap-1"><Scissors size={10} /> Trim VOD</span>
            </FieldCaption>
            <div className="flex justify-between text-[10px] font-mono text-white">
              <span>{formatHmsFull(trimStartSec)}</span>
              <span className="text-zinc-400">{formatHmsFull(trimEndSec - trimStartSec)} clip</span>
              <span>{formatHmsFull(trimEndSec)}</span>
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[9px] font-mono text-zinc-600 uppercase">Start</span>
              <input type="range" min={0} max={vodDurationSec} step={1} value={Math.min(trimStartSec, trimEndSec - 1)}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setTrimStartSec(Math.min(v, trimEndSec - 1));
                }}
                className="w-full accent-white" />
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[9px] font-mono text-zinc-600 uppercase">End</span>
              <input type="range" min={0} max={vodDurationSec} step={1} value={Math.max(trimEndSec, trimStartSec + 1)}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setTrimEndSec(Math.max(v, trimStartSec + 1));
                }}
                className="w-full accent-white" />
            </div>
            <button type="button" onClick={openPreview}
              disabled={previewVideoLoading || trimEndSec <= trimStartSec}
              className="w-full border-2 border-zinc-700 text-zinc-300 hover:border-white hover:text-white font-mono text-[10px] uppercase font-bold py-2 flex items-center justify-center gap-1.5 disabled:opacity-40">
              {previewVideoLoading ? <Loader2 size={12} className="animate-spin" /> : <Eye size={12} />}
              Preview trim
            </button>
          </div>

          <button onClick={handleStartDownload}
            disabled={!!downloading}
            className="w-full relative group mt-1 disabled:opacity-50">
            <div className={`absolute inset-0 translate-x-1.5 translate-y-1.5 group-hover:translate-x-1 group-hover:translate-y-1 transition-transform ${
              urlPlatform === 'kick' ? 'bg-[#53fc18]' : urlPlatform === 'twitch' ? 'bg-[#9146FF]' : 'bg-gradient-to-r from-[#53fc18] to-[#9146FF]'
            }`} />
            <div className="relative bg-black border-2 border-white py-4 flex items-center justify-center gap-3 transition-colors group-hover:bg-white group-hover:text-black group-hover:border-white">
              <Download size={20} strokeWidth={3} className="group-hover:animate-bounce" />
              <span className="font-black uppercase tracking-widest text-md">Rip VOD</span>
            </div>
          </button>
        </div>
      )}
    </div>
  );

  return (
    <div className={`min-h-screen flex justify-center p-4 selection:bg-white selection:text-black bg-[#09090b] ${
      previewOpen ? 'items-start overflow-x-auto' : 'items-center'
    }`}
         style={{ backgroundImage: 'radial-gradient(#27272a 1px, transparent 1px)', backgroundSize: '24px 24px' }}>
      <div className={`flex items-start gap-6 transition-all duration-300 ${
        previewOpen || splitChannelsLayout ? 'w-max max-w-none' : 'w-full justify-center'
      }`}>
      {previewOpen && (
        <div
          className={`shrink-0 bg-zinc-950 border-2 border-white p-4 flex flex-col gap-3 ${platformCardShadow(activePlatform, true)}`}
          style={{ width: 'min(960px, calc(100vw - 28rem - 2rem))' }}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-bold uppercase tracking-widest text-zinc-300">Trim preview</span>
            <span className="text-[10px] font-mono text-zinc-500">HLS playback · trim sync</span>
            <button type="button" onClick={() => void resetPreview()} className="text-zinc-500 hover:text-white p-1">
              <X size={18} />
            </button>
          </div>
          <div className="flex gap-3 items-stretch min-h-[min(54vw,540px)]">
            <div
              ref={previewContainerRef}
              className="relative flex-1 min-w-0 aspect-video bg-black border-2 border-zinc-700 overflow-hidden group"
            >
              <video
                ref={previewVideoRef}
                className="w-full h-full object-contain"
                muted={previewMuted}
                playsInline
                onTimeUpdate={handlePreviewTimeUpdate}
                onPlay={() => {
                  if (previewSuppressPlayRef.current) {
                    previewVideoRef.current?.pause();
                    return;
                  }
                  setPreviewPlaying(true);
                }}
                onPause={() => setPreviewPlaying(false)}
                onClick={togglePreviewPlay}
              />
              {previewVideoLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/60">
                  <Loader2 size={40} className="animate-spin text-zinc-300" />
                </div>
              )}
            </div>
            <div
              ref={previewControlsRef}
              className="w-44 shrink-0 flex flex-col gap-2 justify-center border-l-2 border-zinc-800 pl-3"
            >
              <p className="text-xs font-mono text-zinc-400 text-center">
                {formatHmsFull(previewCurrentTime)}
              </p>
              <div className="flex items-center gap-1">
                <span className="text-[9px] font-mono text-zinc-600 w-10 shrink-0">{formatHmsFull(previewTrimStart)}</span>
                <input
                  type="range"
                  min={previewTrimStart}
                  max={previewTrimEnd}
                  step={0.25}
                  value={Math.min(Math.max(previewCurrentTime, previewTrimStart), previewTrimEnd)}
                  disabled={!previewVideoReady}
                  onChange={(e) => seekPreviewVideo(parseFloat(e.target.value))}
                  className="flex-1 accent-white disabled:opacity-40"
                />
                <span className="text-[9px] font-mono text-zinc-600 w-10 shrink-0 text-right">{formatHmsFull(previewTrimEnd)}</span>
              </div>
              <div className="flex flex-col gap-1.5 items-stretch">
                <button type="button" onClick={() => skipPreview(-PREVIEW_SKIP_SEC)}
                  disabled={!previewVideoReady}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40 flex justify-center"
                  title={`Back ${PREVIEW_SKIP_SEC}s`}>
                  <SkipBack size={16} />
                </button>
                <button type="button" onClick={togglePreviewPlay}
                  disabled={!previewVideoReady}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40 flex justify-center">
                  {previewPlaying ? <Pause size={16} /> : <Play size={16} />}
                </button>
                <button type="button" onClick={() => skipPreview(PREVIEW_SKIP_SEC)}
                  disabled={!previewVideoReady}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40 flex justify-center"
                  title={`Forward ${PREVIEW_SKIP_SEC}s`}>
                  <SkipForward size={16} />
                </button>
                {previewLevels.length > 1 && (
                  <div className="relative">
                    <button
                      type="button"
                      onClick={() => setPreviewQualityMenuOpen((o) => !o)}
                      disabled={!previewVideoReady || previewFullscreen}
                      className="w-full border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40 flex justify-center"
                      title={previewFullscreen ? 'Quality locked in fullscreen' : 'Video quality'}
                    >
                      <Settings size={16} />
                    </button>
                    {previewQualityMenuOpen && (
                      <div className="absolute right-full top-0 mr-1 z-20 min-w-[7rem] border-2 border-zinc-600 bg-zinc-950 shadow-lg py-1">
                        {previewLevels.map((l) => (
                          <button
                            key={l.index}
                            type="button"
                            onClick={() => applyPreviewQuality(l.index)}
                            className={`block w-full text-left px-2 py-1 text-[10px] font-mono hover:bg-zinc-800 ${
                              l.index === previewQualityLevel ? 'text-white' : 'text-zinc-400'
                            }`}
                          >
                            {l.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
                <button type="button"
                  onClick={() => {
                    setPreviewMuted((m) => !m);
                    const v = previewVideoRef.current;
                    if (v) v.muted = !previewMuted;
                  }}
                  disabled={!previewVideoReady}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40 flex justify-center">
                  {previewMuted ? <VolumeX size={16} /> : <Volume2 size={16} />}
                </button>
                <button type="button" onClick={() => void togglePreviewFullscreen()}
                  disabled={!previewVideoReady}
                  className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40 flex justify-center"
                  title={previewFullscreen ? 'Exit fullscreen' : 'Fullscreen (1080p)'}>
                  {previewFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
                </button>
              </div>
            </div>
          </div>
          <p className="text-[10px] text-zinc-600 font-mono text-center">
            Default {PREVIEW_DEFAULT_HEIGHT}p · fullscreen {PREVIEW_FULLSCREEN_HEIGHT}p
            <span className="text-zinc-700 mx-1">·</span>
            preview trim {formatHmsFull(previewTrimStart)} → {formatHmsFull(previewTrimEnd)}
            <span className="text-zinc-700 mx-1">·</span>
            hit Preview again after moving trim sliders
          </p>
        </div>
      )}
      {showUrlInSidebar && (
        <div className={`shrink-0 w-[28rem] bg-zinc-950 border-2 border-white p-6 flex flex-col gap-5 ${platformCardShadow(activePlatform, true)}`}>
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-mono uppercase tracking-widest text-zinc-400">Selected VOD</span>
            <button
              type="button"
              onClick={() => { setChannelVodPanelOpen(false); setVideoInfo(null); setUrl(''); }}
              className="text-zinc-500 hover:text-white p-1"
              title="Clear selection"
            >
              <X size={14} />
            </button>
          </div>
          {urlTabContent}
        </div>
      )}
      <div className={`relative shrink-0 bg-zinc-950 border-2 border-white p-6 flex flex-col gap-5 ${platformCardShadow(activePlatform)} transition-all duration-300 ${
        previewOpen || splitChannelsLayout ? 'w-[28rem]' : 'w-full max-w-md'
      }`}>

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
          {visibleTabs.map((t) => (
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
        {showUrlInMainCard && urlTabContent}

        {/* ════════════════════════════ CHANNELS TAB ════════════════════════════ */}
        {tab === 'channels' && !previewOpen && (
          <div className="flex flex-col gap-3">
            <div className="flex gap-2">
              <input type="text" value={addChannelInput}
                onChange={(e) => setAddChannelInput(e.target.value)}
                placeholder="CHANNEL NAME OR URL..."
                onKeyDown={(e) => e.key === 'Enter' && handleAddChannel()}
                className="flex-1 bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 px-3 py-2.5 focus:outline-none focus:border-white uppercase text-xs" />
              <button type="button" onClick={handleAddChannel}
                disabled={channelsLoading || !addChannelInput.trim()}
                className="bg-white text-black font-black uppercase px-3 text-xs border-2 border-white disabled:opacity-50">
                <Plus size={14} />
              </button>
            </div>

            {savedChannels.length > 0 && (
              <div className="flex flex-col gap-1 max-h-[100px] overflow-y-auto pr-1 custom-scrollbar">
                {savedChannels.map((ch) => (
                  <div key={ch.id}
                    className={`flex items-center gap-1 border px-2 py-1 ${
                      ch.id === selectedChannelId ? 'border-white bg-zinc-900' : 'border-zinc-800'
                    }`}>
                    {editingChannelId === ch.id ? (
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
                      <button type="button" onClick={() => toggleChannelSelection(ch.id)}
                        className="flex-1 text-left text-xs font-mono text-zinc-200 truncate hover:text-white">
                        {ch.displayName}
                      </button>
                    )}
                    {editingChannelId !== ch.id && (
                      <button type="button" title="Rename"
                        onClick={(e) => { e.stopPropagation(); startRenameChannel(ch.id); }}
                        className="text-zinc-600 hover:text-white p-0.5">
                        <Pencil size={11} />
                      </button>
                    )}
                    <button type="button" title="Refresh"
                      onClick={(e) => { e.stopPropagation(); refreshChannel(ch.id); }}
                      disabled={ch.loading}
                      className="text-zinc-600 hover:text-white p-0.5 disabled:opacity-40">
                      {ch.loading ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
                    </button>
                    <button type="button" title="Remove"
                      onClick={(e) => { e.stopPropagation(); removeChannel(ch.id); }}
                      className="text-zinc-600 hover:text-red-400 p-0.5">
                      <X size={11} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {selectedChannel && (
              <div className="flex items-center gap-2 flex-wrap">
                {(['Kick', 'Twitch'] as const).map((platform) => {
                  const isKick = platform === 'Kick';
                  const enabled = isKick ? kickEnabled : twitchEnabled;
                  const slug = isKick ? selectedChannel.kickSlug : selectedChannel.twitchSlug;
                  const color = isKick ? '#53fc18' : '#9146FF';
                  const loading = isKick ? kickBrowseLoading : twitchBrowseLoading;
                  const editing = editingSlug?.channelId === selectedChannelId && editingSlug.platform === platform;
                  return (
                    <div key={platform} className="group relative flex items-center">
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
                          onClick={() => (isKick ? setKickEnabled : setTwitchEnabled)((v) => !v)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              (isKick ? setKickEnabled : setTwitchEnabled)((v) => !v);
                            }
                          }}
                          className={`flex items-center gap-1 px-2 py-0.5 border font-mono text-[10px] uppercase font-bold cursor-pointer select-none ${
                            enabled ? '' : 'opacity-40'
                          }`}
                          style={enabled ? { borderColor: color, color } : { borderColor: '#3f3f46' }}
                        >
                          <input type="checkbox" checked={enabled} readOnly tabIndex={-1}
                            className="w-3 h-3 pointer-events-none" style={{ accentColor: color }} />
                          <span>{platform}</span>
                          <span className="text-zinc-500 normal-case font-normal">{slug}</span>
                          {loading && <Loader2 size={9} className="animate-spin" />}
                        </div>
                      )}
                      {!editing && (
                        <button type="button" title={`Edit ${platform} name`}
                          onClick={(e) => {
                            e.stopPropagation();
                            startEditPlatformSlug(selectedChannelId!, platform);
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
            )}

            {channelsError && (
              <p className="text-red-400 text-[10px] font-mono">{channelsError}</p>
            )}

            {selectedChannel && (
              <>
                {channelsLoading ? (
                  <div className="flex justify-center py-6 text-zinc-500">
                    <Loader2 size={18} className="animate-spin" />
                  </div>
                ) : visibleChannelVideos.length === 0 ? (
                  <p className="text-center text-zinc-600 font-mono text-[10px] py-4">No VODs</p>
                ) : (
                  <div className="flex flex-col gap-1 max-h-[280px] overflow-y-auto pr-1 custom-scrollbar">
                    {visibleChannelVideos.map((v, i) => {
                      const isTw = v.platform === 'Twitch';
                      const color = isTw ? '#9146FF' : '#53fc18';
                      const fullUrl = buildVodUrl(v);
                      return (
                        <div
                          key={`${v.platform}-${v.id}-${i}`}
                          role="button"
                          tabIndex={0}
                          onClick={() => selectVod(fullUrl)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              selectVod(fullUrl);
                            }
                          }}
                          className="flex items-center gap-1 border border-zinc-800 bg-zinc-950 px-2 py-1.5 hover:border-zinc-600 hover:text-white cursor-pointer group"
                        >
                          <div className="flex-1 min-w-0 text-left text-[11px] font-mono text-zinc-300 group-hover:text-white">
                            <span className="truncate block">
                              <span style={{ color }}>{isTw ? 'TW' : 'K'}</span>
                              {' '}{v.title || 'Untitled'}
                              {v.duration ? <span className="text-zinc-500 ml-1">{fmtShort(v.duration)}</span> : null}
                            </span>
                            {v.created_at && (
                              <span className="text-[9px] text-zinc-600 block truncate">
                                {fmtDateAndAgo(v.created_at)}
                              </span>
                            )}
                          </div>
                          <button
                            type="button"
                            title="Open in browser"
                            onClick={(e) => {
                              e.stopPropagation();
                              window.open(fullUrl, '_blank', 'noopener,noreferrer');
                            }}
                            className="text-zinc-600 hover:text-white p-1 shrink-0"
                          >
                            <ExternalLink size={11} />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}
                {canExpandChannelList && (
                  <button type="button" onClick={handleExpandChannelList}
                    className="text-[10px] font-mono text-zinc-500 hover:text-white uppercase">
                    +{CHANNEL_EXPAND_STEP} more
                  </button>
                )}
              </>
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
              <FieldCaption>Download Folder</FieldCaption>
              <div className="flex gap-2">
                <input type="text" value={settings.download_folder}
                  onChange={(e) => setSettings({ ...settings, download_folder: e.target.value })}
                  placeholder="C:\Users\...\Downloads"
                  className="flex-1 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs truncate focus:outline-none focus:border-white" />
                <button type="button" onClick={pickDownloadFolder} disabled={pickingFolder}
                  className="bg-white text-black font-black uppercase px-3 text-[10px] border-2 border-white hover:bg-[#53fc18] hover:border-[#53fc18] shrink-0 flex items-center gap-1 disabled:opacity-50">
                  {pickingFolder ? <Loader2 size={14} className="animate-spin" /> : <FolderOpen size={14} />}
                  {pickingFolder ? '...' : 'Browse'}
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <FieldCaption>Download Threads</FieldCaption>
                <input type="number" min={1} max={16}
                  value={settings.download_threads}
                  onChange={(e) => setSettings({ ...settings, download_threads: Math.max(1, Math.min(16, parseInt(e.target.value) || 4)) })}
                  className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
              </div>
              <div className="flex flex-col gap-1.5">
                <FieldCaption>Max Cache (MB)</FieldCaption>
                <input type="number" min={50} max={2000}
                  value={settings.max_cache_mb}
                  onChange={(e) => setSettings({ ...settings, max_cache_mb: parseInt(e.target.value) || 200 })}
                  className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <FieldCaption>Throttle (KiB/s, -1 = unlimited)</FieldCaption>
              <input type="number" min={-1}
                value={settings.throttle_kib}
                onChange={(e) => setSettings({ ...settings, throttle_kib: parseInt(e.target.value) || -1 })}
                className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
            </div>

            <div className="flex flex-col gap-1.5">
              <FieldCaption>Preferred Quality</FieldCaption>
              <input type="text" value={settings.quality || '1080p'}
                onChange={(e) => setSettings({ ...settings, quality: e.target.value })}
                placeholder="1080p"
                className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
            </div>

            <div className="flex flex-col gap-1.5">
              <FieldCaption>Twitch OAuth Token</FieldCaption>
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
