import { useState, useEffect, useCallback, useMemo, useRef, type Dispatch, type KeyboardEvent, type MouseEvent, type MutableRefObject, type ReactNode, type SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import {
  Download, Scissors, Info, Play, Pause, Link2, X, FastForward, Clock,
  Users, Database, Settings2, StopCircle, Loader2,
  CheckCircle2, AlertCircle, RefreshCw, FolderOpen, Pencil, Plus,
  ExternalLink, Eye, Volume2, VolumeX, Maximize2, Minimize2, Settings, ArrowRightToLine,
} from 'lucide-react';
import kickIcon from '@/assets/platforms/kick.ico';
import twitchIcon from '@/assets/platforms/twitch.png';

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

interface ListedChannelVideo extends ChannelVideo {
  /** 1-based index within the currently visible list for this platform. */
  platformListIndex: number;
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
const PREVIEW_KEY_SKIP_SEC = 5;
const PREVIEW_FS_CONTROLS_HIDE_MS = 200;
type PanelSize = { w: number; h: number };

const PREVIEW_PANEL_DEFAULT: PanelSize = { w: 640, h: 400 };
const URL_ASIDE_PANEL_DEFAULT: PanelSize = { w: 288, h: 384 };
const MAIN_PANEL_DEFAULT: PanelSize = { w: 448, h: 448 };
const EXPLORE_POPUP_DEFAULT: PanelSize = { w: 288, h: 320 };
const PANEL_MIN: PanelSize = { w: 200, h: 180 };
const PANEL_MAX_W = 1000;
const EXPLORE_POPUP_MAX_W = 960;
const CARD_BORDER_PX = 2;

function panelMaxHeight() {
  return Math.round(window.innerHeight * 0.92);
}

/** Distance from panel padding edge to outer colored shadow corner (border + shadow offset). */
function panelResizeHandleInset(compact: boolean): number {
  return CARD_BORDER_PX + (compact ? 4 : 6);
}

function PanelResizeHandle({
  onMouseDown,
  insetPx,
}: {
  onMouseDown: (e: MouseEvent) => void;
  insetPx: number;
}) {
  return (
    <div
      role="separator"
      aria-orientation="both"
      title="Resize"
      onMouseDown={onMouseDown}
      style={{ bottom: -insetPx, right: -insetPx }}
      className="absolute z-30 w-5 h-5 cursor-se-resize flex items-end justify-end p-0.5 pointer-events-auto"
    >
      <span className="block w-2.5 h-2.5 border-r-2 border-b-2 border-zinc-500" />
    </div>
  );
}

function startPanelResizeDrag(
  e: MouseEvent,
  sizeRef: MutableRefObject<PanelSize>,
  setSize: Dispatch<SetStateAction<PanelSize>>,
  opts?: { maxW?: number; maxH?: number },
) {
  e.preventDefault();
  e.stopPropagation();
  const startX = e.clientX;
  const startY = e.clientY;
  const { w: startW, h: startH } = sizeRef.current;
  const maxW = opts?.maxW ?? PANEL_MAX_W;
  const maxH = opts?.maxH ?? panelMaxHeight();
  const onMove = (ev: MouseEvent) => {
    const next = {
      w: Math.min(maxW, Math.max(PANEL_MIN.w, startW + ev.clientX - startX)),
      h: Math.min(maxH, Math.max(PANEL_MIN.h, startH + ev.clientY - startY)),
    };
    sizeRef.current = next;
    setSize(next);
  };
  const onUp = () => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

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

function PlatformVodIcon({ platform, className = 'w-3.5 h-3.5' }: { platform: string; className?: string }) {
  const isTw = platform === 'Twitch';
  return (
    <img
      src={isTw ? twitchIcon : kickIcon}
      alt={isTw ? 'Twitch' : 'Kick'}
      className={`shrink-0 object-contain ${className}`}
      draggable={false}
    />
  );
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
  const [previewVolume, setPreviewVolume] = useState(1);
  const [previewFullscreen, setPreviewFullscreen] = useState(false);
  const [previewFsControlsVisible, setPreviewFsControlsVisible] = useState(true);
  const [previewLevels, setPreviewLevels] = useState<PreviewLevelOption[]>([]);
  const [previewQualityLevel, setPreviewQualityLevel] = useState(0);
  const [previewQualityMenuOpen, setPreviewQualityMenuOpen] = useState(false);
  const [previewVolumeMenuOpen, setPreviewVolumeMenuOpen] = useState(false);
  const [channelVodPanelOpen, setChannelVodPanelOpen] = useState(false);
  /** URL tab hidden from bar after picking a VOD from channels; restored only on page refresh. */
  const [urlTabBarHidden, setUrlTabBarHidden] = useState(false);
  const [previewTrimStart, setPreviewTrimStart] = useState(0);
  const [previewTrimEnd, setPreviewTrimEnd] = useState(3600);
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  const previewContainerRef = useRef<HTMLDivElement>(null);
  const previewControlsRef = useRef<HTMLDivElement>(null);
  const previewHlsRef = useRef<Hls | null>(null);
  const previewVolumeRef = useRef(1);
  const previewFsHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const previewInitialSeekDoneRef = useRef(false);
  const previewInitialPlayDoneRef = useRef(false);
  const previewSuppressPlayRef = useRef(false);
  const previewTrimStartRef = useRef(0);
  const previewTrimEndRef = useRef(3600);

  // Channel explore player (floating popup for browsing VODs)
  const [exploreOpen, setExploreOpen] = useState(false);
  const [exploreVod, setExploreVod] = useState<{
    url: string;
    title: string;
    platform: string;
    durationSec: number;
  } | null>(null);
  const [exploreHlsUrl, setExploreHlsUrl] = useState<string | null>(null);
  const [exploreLoading, setExploreLoading] = useState(false);
  const [exploreReady, setExploreReady] = useState(false);
  const [explorePlaying, setExplorePlaying] = useState(false);
  const [exploreMuted, setExploreMuted] = useState(false);
  const [exploreVolume, setExploreVolume] = useState(1);
  const [exploreVolumeMenuOpen, setExploreVolumeMenuOpen] = useState(false);
  const [exploreCurrentTime, setExploreCurrentTime] = useState(0);
  const [previewPanelSize, setPreviewPanelSize] = useState(PREVIEW_PANEL_DEFAULT);
  const [urlAsidePanelSize, setUrlAsidePanelSize] = useState(URL_ASIDE_PANEL_DEFAULT);
  const [mainPanelSize, setMainPanelSize] = useState(MAIN_PANEL_DEFAULT);
  const [explorePopupSize, setExplorePopupSize] = useState(EXPLORE_POPUP_DEFAULT);
  const [exploreFullscreen, setExploreFullscreen] = useState(false);
  const [exploreFsControlsVisible, setExploreFsControlsVisible] = useState(true);
  const [exploreError, setExploreError] = useState<string | null>(null);
  const exploreVideoRef = useRef<HTMLVideoElement>(null);
  const exploreContainerRef = useRef<HTMLDivElement>(null);
  const exploreHlsRef = useRef<Hls | null>(null);
  const exploreSessionIdRef = useRef<string | null>(null);
  const exploreFsHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const exploreInitialPlayDoneRef = useRef(false);
  const previewPanelSizeRef = useRef(PREVIEW_PANEL_DEFAULT);
  const urlAsidePanelSizeRef = useRef(URL_ASIDE_PANEL_DEFAULT);
  const mainPanelSizeRef = useRef(MAIN_PANEL_DEFAULT);
  const explorePopupSizeRef = useRef(EXPLORE_POPUP_DEFAULT);
  const exploreVolumeRef = useRef(1);

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
    const sorted = items.sort((a, b) => parseVideoTs(b.created_at) - parseVideoTs(a.created_at));
    let kickN = 0;
    let twitchN = 0;
    return sorted.map((v): ListedChannelVideo => ({
      ...v,
      platformListIndex: v.platform === 'Kick' ? ++kickN : ++twitchN,
    }));
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
    setPreviewVolumeMenuOpen(false);
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
      video.muted = true;
      setPreviewMuted(true);
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

  const seekPreviewPercent = useCallback((fraction: number) => {
    const start = previewTrimStartRef.current;
    const end = previewTrimEndRef.current;
    const t = start + (end - start) * Math.max(0, Math.min(1, fraction));
    seekPreviewVideo(t);
  }, [seekPreviewVideo]);

  const setPreviewVolumeLevel = useCallback((level: number) => {
    const video = previewVideoRef.current;
    if (!video) return;
    const v = Math.max(0, Math.min(1, level));
    video.volume = v;
    previewVolumeRef.current = v;
    setPreviewVolume(v);
    if (v <= 0) {
      video.muted = true;
      setPreviewMuted(true);
    } else {
      video.muted = false;
      setPreviewMuted(false);
    }
  }, []);

  const bumpPreviewFsControls = useCallback(() => {
    setPreviewFsControlsVisible(true);
    if (previewFsHideTimerRef.current) {
      window.clearTimeout(previewFsHideTimerRef.current);
    }
    if (previewFullscreen) {
      previewFsHideTimerRef.current = window.setTimeout(() => {
        setPreviewFsControlsVisible(false);
      }, PREVIEW_FS_CONTROLS_HIDE_MS);
    }
  }, [previewFullscreen]);

  const handlePreviewContainerKeyDown = useCallback((e: KeyboardEvent) => {
    if (!previewVideoReady) return;
    const tag = (e.target as HTMLElement).tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    const { key } = e;
    const transportKeys = [' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End',
      '0', '1', '2', '3', '4', '5', '6', '7', '8', '9'];
    if (!transportKeys.includes(key)) return;

    e.preventDefault();
    e.stopPropagation();

    if (key === ' ') {
      togglePreviewPlay();
      return;
    }
    if (key === 'ArrowLeft') {
      skipPreview(-PREVIEW_KEY_SKIP_SEC);
      return;
    }
    if (key === 'ArrowRight') {
      skipPreview(PREVIEW_KEY_SKIP_SEC);
      return;
    }
    if (key === 'ArrowUp') {
      setPreviewVolumeLevel(previewVolumeRef.current + 0.1);
      return;
    }
    if (key === 'ArrowDown') {
      setPreviewVolumeLevel(previewVolumeRef.current - 0.1);
      return;
    }
    if (key === 'Home' || key === '0') {
      seekPreviewPercent(0);
      return;
    }
    if (key === 'End') {
      seekPreviewPercent(1);
      return;
    }
    if (key >= '1' && key <= '9') {
      seekPreviewPercent(parseInt(key, 10) * 0.1);
    }
  }, [previewVideoReady, togglePreviewPlay, skipPreview, setPreviewVolumeLevel, seekPreviewPercent]);

  const focusPreviewPlayer = useCallback(() => {
    previewContainerRef.current?.focus();
  }, []);

  const togglePreviewFullscreen = useCallback(async () => {
    const container = previewContainerRef.current;
    if (!container || !previewVideoReady) return;
    try {
      if (!document.fullscreenElement) {
        await container.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      /* fullscreen denied or unsupported */
    }
  }, [previewVideoReady]);

  useEffect(() => {
    const onFullscreenChange = () => {
      const fs = document.fullscreenElement === previewContainerRef.current;
      setPreviewFullscreen(fs);
      setPreviewFsControlsVisible(!fs);
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, []);

  const anyPlayerMenuOpen = previewQualityMenuOpen || previewVolumeMenuOpen || exploreVolumeMenuOpen;

  useEffect(() => {
    if (!anyPlayerMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest('[data-player-menu]')) return;
      setPreviewQualityMenuOpen(false);
      setPreviewVolumeMenuOpen(false);
      setExploreVolumeMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [anyPlayerMenuOpen]);

  useEffect(() => {
    if (!previewOpen || !previewVideoReady) return;
    const video = previewVideoRef.current;
    if (video) {
      video.volume = previewVolumeRef.current;
    }
    const t = window.setTimeout(() => focusPreviewPlayer(), 0);
    return () => window.clearTimeout(t);
  }, [previewOpen, previewVideoReady, focusPreviewPlayer]);

  // ── Channel explore player ──

  const destroyExplorePlayer = useCallback(() => {
    if (exploreHlsRef.current) {
      exploreHlsRef.current.destroy();
      exploreHlsRef.current = null;
    }
    const video = exploreVideoRef.current;
    if (video) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
  }, []);

  const closeExplorePlayer = useCallback(async () => {
    if (document.fullscreenElement === exploreContainerRef.current) {
      try { await document.exitFullscreen(); } catch { /* ignore */ }
    }
    const sid = exploreSessionIdRef.current;
    destroyExplorePlayer();
    exploreSessionIdRef.current = null;
    exploreInitialPlayDoneRef.current = false;
    if (exploreFsHideTimerRef.current) {
      window.clearTimeout(exploreFsHideTimerRef.current);
      exploreFsHideTimerRef.current = null;
    }
    setExploreOpen(false);
    setExploreVod(null);
    setExploreHlsUrl(null);
    setExploreLoading(false);
    setExploreReady(false);
    setExplorePlaying(false);
    setExploreMuted(false);
    setExploreVolume(1);
    setExploreVolumeMenuOpen(false);
    setExploreCurrentTime(0);
    setExploreFullscreen(false);
    setExploreFsControlsVisible(true);
    setExploreError(null);
    if (sid) {
      try { await apiDelete(`/api/preview/session/${sid}`); } catch { /* ignore */ }
    }
  }, [destroyExplorePlayer]);

  const openExplorePlayer = useCallback(async (v: ChannelVideo) => {
    const vodUrl = buildVodUrl(v);
    const durationSec = v.duration ? Math.max(2, Math.floor(v.duration)) : 7200;
    const oldSid = exploreSessionIdRef.current;
    destroyExplorePlayer();
    if (oldSid) {
      try { await apiDelete(`/api/preview/session/${oldSid}`); } catch { /* ignore */ }
    }
    exploreSessionIdRef.current = null;
    exploreInitialPlayDoneRef.current = false;
    setExploreVod({
      url: vodUrl,
      title: v.title || 'Untitled',
      platform: v.platform,
      durationSec,
    });
    setExploreOpen(true);
    setExploreLoading(true);
    setExploreReady(false);
    setExplorePlaying(false);
    setExploreMuted(false);
    setExploreVolume(1);
    setExploreVolumeMenuOpen(false);
    setExploreCurrentTime(0);
    setExploreError(null);
    try {
      const res = await apiPost<{ session_id: string; master_url: string }>('/api/preview/session', {
        url: vodUrl,
        crop_start: 0,
        crop_end: durationSec,
      });
      exploreSessionIdRef.current = res.session_id;
      setExploreHlsUrl(res.master_url);
    } catch (err: any) {
      setExploreError(err.message || 'Could not start player');
      setExploreLoading(false);
    }
  }, [destroyExplorePlayer]);

  const toggleExplorePlay = useCallback(() => {
    const video = exploreVideoRef.current;
    if (!video || !exploreReady) return;
    if (video.paused) {
      void video.play().catch(() => {});
      setExplorePlaying(true);
    } else {
      video.pause();
      setExplorePlaying(false);
    }
  }, [exploreReady]);

  const setExploreVolumeLevel = useCallback((level: number) => {
    const video = exploreVideoRef.current;
    if (!video) return;
    const v = Math.max(0, Math.min(1, level));
    video.volume = v;
    if (v > 0) {
      exploreVolumeRef.current = v;
    }
    setExploreVolume(v);
    if (v <= 0) {
      video.muted = true;
      setExploreMuted(true);
    } else {
      video.muted = false;
      setExploreMuted(false);
    }
  }, []);

  const seekExploreVideo = useCallback((sec: number) => {
    const video = exploreVideoRef.current;
    if (!video || !exploreReady || !exploreVod) return;
    const t = Math.max(0, Math.min(sec, exploreVod.durationSec));
    if (Math.abs(video.currentTime - t) > 0.2) {
      video.currentTime = t;
    }
    setExploreCurrentTime(t);
  }, [exploreReady, exploreVod]);

  const handleExploreTimeUpdate = useCallback(() => {
    const video = exploreVideoRef.current;
    if (!video) return;
    setExploreCurrentTime(video.currentTime);
  }, []);

  const onPreviewPanelResize = useCallback((e: MouseEvent) => {
    startPanelResizeDrag(e, previewPanelSizeRef, setPreviewPanelSize);
  }, []);

  const onUrlAsidePanelResize = useCallback((e: MouseEvent) => {
    startPanelResizeDrag(e, urlAsidePanelSizeRef, setUrlAsidePanelSize);
  }, []);

  const onMainPanelResize = useCallback((e: MouseEvent) => {
    startPanelResizeDrag(e, mainPanelSizeRef, setMainPanelSize);
  }, []);

  const onExplorePanelResize = useCallback((e: MouseEvent) => {
    startPanelResizeDrag(e, explorePopupSizeRef, setExplorePopupSize, {
      maxW: EXPLORE_POPUP_MAX_W,
      maxH: Math.round(window.innerHeight * 0.85),
    });
  }, []);

  const toggleExploreFullscreen = useCallback(async () => {
    const container = exploreContainerRef.current;
    if (!container || !exploreReady) return;
    try {
      if (!document.fullscreenElement) {
        await container.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      /* fullscreen denied or unsupported */
    }
  }, [exploreReady]);

  const bumpExploreFsControls = useCallback(() => {
    setExploreFsControlsVisible(true);
    if (exploreFsHideTimerRef.current) {
      window.clearTimeout(exploreFsHideTimerRef.current);
    }
    if (exploreFullscreen) {
      exploreFsHideTimerRef.current = window.setTimeout(() => {
        setExploreFsControlsVisible(false);
      }, PREVIEW_FS_CONTROLS_HIDE_MS);
    }
  }, [exploreFullscreen]);

  useEffect(() => {
    const onFullscreenChange = () => {
      const fs = document.fullscreenElement === exploreContainerRef.current;
      setExploreFullscreen(fs);
      setExploreFsControlsVisible(!fs);
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, []);

  useEffect(() => {
    if (!exploreOpen || !exploreHlsUrl) return;
    const video = exploreVideoRef.current;
    if (!video) return;

    setExploreLoading(true);
    setExploreReady(false);

    const onCanPlay = () => {
      setExploreReady(true);
      setExploreLoading(false);
      video.muted = false;
      video.volume = 1;
      exploreVolumeRef.current = 1;
      setExploreVolume(1);
      setExploreMuted(false);
      if (!exploreInitialPlayDoneRef.current && video.paused) {
        exploreInitialPlayDoneRef.current = true;
        void video.play().catch(() => {
          video.muted = true;
          setExploreMuted(true);
          void video.play().catch(() => {});
        });
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
        startPosition: 0,
      });
      exploreHlsRef.current = hls;
      hls.loadSource(exploreHlsUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        const mapped: PreviewLevelOption[] = (data.levels ?? hls.levels).map((l, i) => ({
          index: i,
          height: l.height,
          label: previewLevelLabel(l.height, l.bitrate),
        }));
        mapped.sort((a, b) => a.height - b.height);
        const defaultIdx = mapped.length > 1
          ? levelIndexForHeight(mapped, PREVIEW_DEFAULT_HEIGHT)
          : lowestLevelIndex(mapped);
        hls.loadLevel = defaultIdx;
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
            setExploreError('Playback failed — try again');
            setExploreLoading(false);
            hls.destroy();
            exploreHlsRef.current = null;
            break;
        }
      });
      return () => {
        video.removeEventListener('canplay', onCanPlay);
        hls.destroy();
        exploreHlsRef.current = null;
      };
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = exploreHlsUrl;
      video.addEventListener('canplay', onCanPlay, { once: true });
      return () => {
        video.removeEventListener('canplay', onCanPlay);
        video.removeAttribute('src');
        video.load();
      };
    }

    setExploreError('HLS playback is not supported in this browser');
    setExploreLoading(false);
  }, [exploreOpen, exploreHlsUrl]);

  useEffect(() => () => { void closeExplorePlayer(); }, [closeExplorePlayer]);

  // ── Fetch video info ──

  const fetchVideoInfo = useCallback(async (videoUrl: string) => {
    const trimmed = videoUrl.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    try {
      const info = await apiGet<VideoInfo>(`/api/info/video?id=${encodeURIComponent(trimmed)}`);
      setUrl(trimmed);
      setVideoInfo(info);
      setQuality(bestAvailableQuality(info));
      const dur = info.duration ? Math.floor(info.duration) : 3600;
      setTrimStartSec(0);
      setTrimEndSec(Math.max(1, dur));
      // Keep the current preview playing until the user hits Preview on the new VOD.
      if (!previewOpen) {
        void resetPreview();
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [previewOpen, resetPreview]);

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
    setUrlTabBarHidden(true);
    void fetchVideoInfo(vodUrl);
  }, [fetchVideoInfo]);

  const carryExploreToUrl = useCallback(() => {
    if (!exploreVod?.url) return;
    selectVod(exploreVod.url);
  }, [exploreVod, selectVod]);

  // ── Size estimate ──
  const clipSec = Math.max(0, trimEndSec - trimStartSec);

  const rates: Record<string, number> = {
    source: 180, '1080p60': 180, '1080p': 120, '720p60': 70,
    '720p': 70, '480p': 35, '360p': 18,
  };
  const mbPerMin = rates[quality] || 70;
  const estSize = (clipSec / 60) * mbPerMin;

  const activePlatform = detectVideoPlatform(videoInfo, url);
  const channelsSplitActive = channelVodPanelOpen && !previewOpen;
  const showUrlInSidebar = channelsSplitActive;
  const showUrlInPreviewMiddle = previewOpen;
  const urlPanelAside = showUrlInSidebar || showUrlInPreviewMiddle;
  const splitLayout = urlPanelAside;
  const triplePanelLayout = previewOpen && urlPanelAside;
  const showUrlInMainCard = tab === 'url' && !urlPanelAside && !urlTabBarHidden;
  const urlMainCompact = showUrlInMainCard && Boolean(videoInfo);
  const mainCardHeaderCompact = triplePanelLayout || urlMainCompact;
  const visibleTabs: Tab[] = urlPanelAside || urlTabBarHidden
    ? ['channels', 'queue', 'settings']
    : ['url', 'channels', 'queue', 'settings'];

  useEffect(() => {
    if ((urlPanelAside || urlTabBarHidden) && tab === 'url') {
      setTab('channels');
    }
  }, [urlPanelAside, urlTabBarHidden, tab]);

  const urlFetched = Boolean(videoInfo);
  const extractBtnHoverClass = urlPanelAside && !previewOpen
    ? actionBtnHover(null)
    : actionBtnHover(urlPlatform);
  const urlInputClass = urlFetched
    ? 'w-full bg-zinc-950 border border-zinc-800 text-zinc-400 font-mono placeholder:text-zinc-600 pl-7 pr-7 py-1 focus:outline-none focus:border-zinc-500 transition-colors text-[10px] truncate'
    : 'w-full bg-zinc-900 border-2 border-zinc-800 text-white font-mono placeholder:text-zinc-600 pl-10 pr-10 py-3 focus:outline-none focus:border-white transition-colors uppercase text-sm';

  const urlTabContent = (
    <div className="flex flex-col gap-2 min-h-0 h-full">
      <div className="flex flex-col gap-1 shrink-0">
        <div className="relative group">
          <div className={`absolute inset-y-0 left-0 flex items-center pointer-events-none text-white/40 ${urlFetched ? 'pl-2' : 'pl-3'}`}>
            <Link2 size={urlFetched ? 12 : 18} strokeWidth={urlFetched ? 2 : 3} />
          </div>
          <input
            type="text"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={urlFetched ? 'VOD link' : 'PASTE VOD LINK HERE...'}
            onKeyDown={(e) => e.key === 'Enter' && handleGetInfo()}
            className={urlInputClass}
          />
          {url && (
            <button type="button" onClick={() => setUrl('')}
              className={`absolute inset-y-0 right-0 flex items-center text-zinc-500 hover:text-white ${urlFetched ? 'pr-2' : 'pr-3'}`}>
              <X size={urlFetched ? 12 : 18} strokeWidth={urlFetched ? 2 : 3} />
            </button>
          )}
        </div>

        {!videoInfo && (
          <button
            onClick={handleGetInfo}
            disabled={!url || loading}
            className={`w-full bg-zinc-800 text-white font-black uppercase py-3 flex items-center justify-center gap-2 transition-all duration-300 disabled:opacity-50 border-2 border-zinc-700 ${extractBtnHoverClass}`}
          >
            {loading ? (
              <><Loader2 size={16} className="animate-spin" /> Loading...</>
            ) : (
              <><Info size={16} strokeWidth={3} /> Extract Info</>
            )}
          </button>
        )}

        {videoInfo && loading && (
          <div className="flex items-center justify-center gap-2 py-1 text-[10px] font-mono text-zinc-500">
            <Loader2 size={12} className="animate-spin" />
            Updating…
          </div>
        )}
      </div>

      {videoInfo && (
        <div className="flex flex-col gap-2 min-h-0 flex-1">
          <div className="border border-zinc-800 p-2 flex gap-2 bg-zinc-900/80 relative overflow-hidden shrink-0">
            <div className={`absolute top-0 right-0 w-10 h-10 opacity-15 blur-xl ${
              videoInfo.platform?.toLowerCase() === 'kick' ? 'bg-[#53fc18]' : 'bg-[#9146FF]'
            }`} />
            <div className="w-12 h-9 bg-zinc-800 border border-zinc-700 flex items-center justify-center shrink-0 overflow-hidden">
              {videoInfo.thumbnail ? (
                <img src={videoInfo.thumbnail} alt="" className="w-full h-full object-cover" />
              ) : (
                <Play size={12} className="text-zinc-500" />
              )}
            </div>
            <div className="flex flex-col justify-center overflow-hidden w-full min-w-0 gap-0.5">
              <h3 className="font-bold truncate uppercase text-[10px] leading-tight">
                {videoInfo.title || 'Untitled'}
              </h3>
              <p className="text-[9px] text-zinc-500 font-mono truncate">
                {videoInfo.uploader || 'Unknown'}
                {videoInfo.created_at ? ` · ${fmtDateAndAgo(videoInfo.created_at)}` : ''}
              </p>
              <div className="flex justify-between items-center gap-1 text-[9px] font-mono text-zinc-500">
                <span className="flex items-center gap-0.5 truncate">
                  <Clock size={9} /> {videoInfo.duration_string || fmtDuration(videoInfo.duration || 0)}
                </span>
                <span className="flex items-center gap-0.5 shrink-0 text-zinc-300">
                  <Database size={9} className={
                    videoInfo.platform?.toLowerCase() === 'kick' ? 'text-[#53fc18]' : 'text-[#9146FF]'
                  } /> {bytes(estSize)}
                </span>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2 shrink-0">
            <div className="flex flex-col gap-0.5">
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">Quality</span>
              <select value={quality} onChange={(e) => setQuality(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono py-1 px-1.5 focus:outline-none focus:border-white text-[10px] cursor-pointer">
                {videoInfo.qualities.length > 0 ? (
                  videoInfo.qualities.map((q) => <option key={q} value={q.toLowerCase()}>{q}</option>)
                ) : (
                  <>
                    <option value="source">Source</option>
                    <option value="1080p">1080p</option>
                    <option value="720p">720p</option>
                    <option value="480p">480p</option>
                    <option value="360p">360p</option>
                  </>
                )}
              </select>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-600">Est. size</span>
              <div className="w-full bg-zinc-950 border border-zinc-800 text-white font-mono py-1 px-1.5 text-[10px] flex items-center justify-center">
                {bytes(estSize)}
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-2.5 shrink-0 py-0.5">
            <div className="flex justify-between items-center">
              <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500">Trim</span>
              <span className="text-xs font-mono text-zinc-400">{formatHmsFull(trimEndSec - trimStartSec)}</span>
            </div>
            <div className="flex justify-between text-xs font-mono text-white px-0.5">
              <span>{formatHmsFull(trimStartSec)}</span>
              <span className="text-zinc-500">{formatHmsFull(trimEndSec)}</span>
            </div>
            <input type="range" min={0} max={vodDurationSec} step={1} value={Math.min(trimStartSec, trimEndSec - 1)}
              onChange={(e) => {
                const v = Number(e.target.value);
                setTrimStartSec(Math.min(v, trimEndSec - 1));
              }}
              className="url-trim-range w-full accent-white" />
            <input type="range" min={0} max={vodDurationSec} step={1} value={Math.max(trimEndSec, trimStartSec + 1)}
              onChange={(e) => {
                const v = Number(e.target.value);
                setTrimEndSec(Math.max(v, trimStartSec + 1));
              }}
              className="url-trim-range w-full accent-white" />
            <button type="button" onClick={openPreview}
              disabled={previewVideoLoading || trimEndSec <= trimStartSec}
              className="w-full border border-zinc-700 text-zinc-400 hover:border-white hover:text-white font-mono text-[9px] uppercase font-bold py-1 flex items-center justify-center gap-1 disabled:opacity-40">
              {previewVideoLoading ? <Loader2 size={11} className="animate-spin" /> : <Eye size={11} />}
              Preview
            </button>
          </div>

          <button
            onClick={handleStartDownload}
            disabled={!!downloading}
            className={`w-full mt-auto shrink-0 disabled:opacity-50 border-2 border-white bg-black py-2 flex items-center justify-center gap-2 text-xs font-black uppercase tracking-widest transition-[transform,box-shadow,background-color,color] duration-150 hover:bg-white hover:text-black ${
              urlPlatform === 'kick'
                ? 'shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18] hover:translate-x-0.5 hover:translate-y-0.5'
                : urlPlatform === 'twitch'
                  ? 'shadow-[3px_3px_0px_0px_#9146FF] hover:shadow-[2px_2px_0px_0px_#9146FF] hover:translate-x-0.5 hover:translate-y-0.5'
                  : 'shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18] hover:translate-x-0.5 hover:translate-y-0.5'
            }`}
          >
            <Download size={16} strokeWidth={3} />
            <span>Rip VOD</span>
          </button>
        </div>
      )}
    </div>
  );

  const previewCtrlBtn = (fsOverlay: boolean, large = false) => {
    const pad = large ? 'p-2' : 'p-1.5';
    return fsOverlay
      ? `border border-white/20 bg-black/20 text-zinc-100/90 hover:bg-black/35 hover:border-white/50 ${pad} disabled:opacity-30 backdrop-blur-[1px]`
      : `border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white ${pad} disabled:opacity-40`;
  };

  const renderVolumeControl = (opts: {
    volume: number;
    muted: boolean;
    menuOpen: boolean;
    setMenuOpen: Dispatch<SetStateAction<boolean>>;
    onVolumeChange: (level: number) => void;
    disabled: boolean;
    buttonClassName: string;
    popoverFs?: boolean;
    onMenuOpen?: () => void;
  }) => {
    const displayVol = opts.muted ? 0 : opts.volume;
    const popoverClass = opts.popoverFs
      ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
      : 'border-2 border-zinc-600 bg-zinc-950';
    return (
      <div className="relative" data-player-menu>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            opts.onMenuOpen?.();
            opts.setMenuOpen((o) => !o);
          }}
          disabled={opts.disabled}
          className={opts.buttonClassName}
          title="Volume"
        >
          {opts.muted || opts.volume <= 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
        </button>
        {opts.menuOpen && (
          <div
            className={`absolute bottom-full left-0 mb-1.5 z-30 flex items-center gap-2 px-2.5 py-2 shadow-lg ${popoverClass}`}
            onClick={(e) => e.stopPropagation()}
          >
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={displayVol}
              disabled={opts.disabled}
              onChange={(e) => opts.onVolumeChange(parseFloat(e.target.value))}
              className={`w-24 accent-white ${opts.popoverFs ? 'h-1' : 'h-1.5'}`}
            />
          </div>
        )}
      </div>
    );
  };

  const previewTimelineUi = (
    <div className="flex items-center gap-2">
      <span className={`text-[9px] font-mono w-11 shrink-0 ${previewFullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}>
        {formatHmsFull(previewCurrentTime)}
      </span>
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
      <span className={`text-[9px] font-mono w-11 shrink-0 text-right ${previewFullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}>
        {formatHmsFull(previewTrimEnd)}
      </span>
    </div>
  );

  const previewTransportUi = (opts: { fsCornerExit?: boolean }) => (
    <div className={`flex items-center gap-2 ${opts.fsCornerExit ? 'pr-14' : 'justify-between'}`}>
      <div className="flex items-center gap-1.5">
        <button type="button" onClick={togglePreviewPlay}
          disabled={!previewVideoReady}
          className={previewCtrlBtn(previewFullscreen, true)}>
          {previewPlaying ? <Pause size={18} /> : <Play size={18} />}
        </button>
        {renderVolumeControl({
          volume: previewVolume,
          muted: previewMuted,
          menuOpen: previewVolumeMenuOpen,
          setMenuOpen: setPreviewVolumeMenuOpen,
          onVolumeChange: setPreviewVolumeLevel,
          disabled: !previewVideoReady,
          buttonClassName: previewCtrlBtn(previewFullscreen, true),
          popoverFs: previewFullscreen,
          onMenuOpen: () => setPreviewQualityMenuOpen(false),
        })}
        {previewLevels.length > 1 && (
          <div className="relative" data-player-menu>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setPreviewVolumeMenuOpen(false);
                setPreviewQualityMenuOpen((o) => !o);
              }}
              disabled={!previewVideoReady}
              className={previewCtrlBtn(previewFullscreen)}
              title="Video quality"
            >
              <Settings size={15} />
            </button>
            {previewQualityMenuOpen && (
              <div className="absolute bottom-full left-0 mb-1 z-30 min-w-[7rem] border-2 border-zinc-600 bg-zinc-950 shadow-lg py-1">
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
      </div>
      {!opts.fsCornerExit && (
        <button type="button" onClick={() => void togglePreviewFullscreen()}
          disabled={!previewVideoReady}
          className={previewCtrlBtn(previewFullscreen, true)}
          title="Fullscreen">
          <Maximize2 size={18} />
        </button>
      )}
    </div>
  );

  const exploreFsCtrlBtn = 'border border-white/20 bg-black/25 text-zinc-100 p-2 disabled:opacity-30 backdrop-blur-[1px]';

  const exploreVolumeUi = (fs: boolean) => renderVolumeControl({
    volume: exploreVolume,
    muted: exploreMuted,
    menuOpen: exploreVolumeMenuOpen,
    setMenuOpen: setExploreVolumeMenuOpen,
    onVolumeChange: setExploreVolumeLevel,
    disabled: !exploreReady,
    buttonClassName: fs ? exploreFsCtrlBtn : previewCtrlBtn(false, true),
    popoverFs: fs,
  });

  const exploreTimelineUi = exploreVod ? (
    <div className="flex items-center gap-1.5 w-full shrink-0">
      <span className={`text-[9px] font-mono w-10 shrink-0 ${exploreFullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}>
        {formatHmsFull(exploreCurrentTime)}
      </span>
      <input
        type="range"
        min={0}
        max={exploreVod.durationSec}
        step={0.25}
        value={Math.min(exploreCurrentTime, exploreVod.durationSec)}
        disabled={!exploreReady}
        onChange={(e) => seekExploreVideo(parseFloat(e.target.value))}
        className="flex-1 accent-white disabled:opacity-40 h-1"
      />
      <span className={`text-[9px] font-mono w-10 shrink-0 text-right ${exploreFullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}>
        {formatHmsFull(exploreVod.durationSec)}
      </span>
    </div>
  ) : null;

  return (
    <div
      className={`min-h-screen flex justify-center items-center selection:bg-white selection:text-black bg-[#09090b] ${
        splitLayout
          ? 'overflow-x-auto px-5 py-4'
          : 'p-4'
      }`}
      style={{
        backgroundImage: 'radial-gradient(#27272a 1px, transparent 1px)',
        backgroundSize: '24px 24px',
        scrollbarGutter: 'stable',
      }}
    >
      <div className={`flex items-start ${
        triplePanelLayout
          ? 'w-full max-w-[calc(100vw-2.5rem)] gap-3 justify-center'
          : splitLayout
            ? 'w-max max-w-none gap-6'
            : 'w-full max-w-md justify-center gap-6'
      }`}>
      {previewOpen && (
        <div
          className={`relative shrink-0 bg-zinc-950 border-2 border-white p-4 flex flex-col gap-3 min-h-0 min-w-0 ${platformCardShadow(activePlatform, true)}`}
          style={{
            width: previewPanelSize.w,
            height: previewPanelSize.h,
          }}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-bold uppercase tracking-widest text-zinc-300">Trim preview</span>
            <span className="text-[10px] font-mono text-zinc-500">HLS playback · trim sync</span>
            <button type="button" onClick={() => void resetPreview()} className="text-zinc-500 hover:text-white p-1">
              <X size={18} />
            </button>
          </div>
          <div className="flex flex-col gap-2 w-full" data-preview-panel>
            <div
              ref={previewContainerRef}
              tabIndex={0}
              role="application"
              aria-label="Trim preview player"
              onKeyDown={handlePreviewContainerKeyDown}
              onMouseMove={previewFullscreen ? bumpPreviewFsControls : undefined}
              onMouseLeave={previewFullscreen ? () => {
                if (previewFsHideTimerRef.current) window.clearTimeout(previewFsHideTimerRef.current);
                previewFsHideTimerRef.current = window.setTimeout(() => {
                  setPreviewFsControlsVisible(false);
                }, PREVIEW_FS_CONTROLS_HIDE_MS);
              } : undefined}
              onFocus={focusPreviewPlayer}
              onClick={(e) => {
                focusPreviewPlayer();
                if ((e.target as HTMLElement).tagName === 'VIDEO') {
                  togglePreviewPlay();
                }
              }}
              className="relative w-full aspect-video bg-black border-2 border-zinc-700 overflow-hidden outline-none focus:ring-2 focus:ring-white/30"
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
              />
              {previewVideoLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/60 z-20">
                  <Loader2 size={40} className="animate-spin text-zinc-300" />
                </div>
              )}
              {previewFullscreen && (
                <>
                  <div
                    ref={previewControlsRef}
                    className={`absolute inset-x-0 bottom-0 z-10 flex flex-col gap-1 px-2 pb-2 pt-3 bg-gradient-to-t from-black/35 to-transparent transition-opacity duration-150 ${
                      previewFsControlsVisible ? 'opacity-100' : 'opacity-0 pointer-events-none'
                    }`}
                    onClick={(e) => e.stopPropagation()}
                    onMouseMove={bumpPreviewFsControls}
                  >
                    {previewTimelineUi}
                    {previewTransportUi({ fsCornerExit: true })}
                  </div>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); void togglePreviewFullscreen(); }}
                    disabled={!previewVideoReady}
                    className={`absolute bottom-0 right-0 z-20 flex items-end justify-end min-w-[3.5rem] min-h-[3.5rem] p-4 pointer-events-auto ${previewCtrlBtn(true, true)}`}
                    title="Exit fullscreen"
                  >
                    <Minimize2 size={18} />
                  </button>
                </>
              )}
            </div>
            {!previewFullscreen && (
              <div
                ref={previewControlsRef}
                className="flex flex-col gap-1.5 w-full shrink-0"
              >
                {previewTimelineUi}
                {previewTransportUi({ fsCornerExit: false })}
              </div>
            )}
          </div>
          {!previewFullscreen && (
            <PanelResizeHandle onMouseDown={onPreviewPanelResize} insetPx={panelResizeHandleInset(true)} />
          )}
        </div>
      )}
      {(showUrlInSidebar || showUrlInPreviewMiddle) && (
        <div
          className={`relative shrink-0 bg-zinc-950 border-2 border-white p-4 flex flex-col gap-2 min-h-0 ${platformCardShadow(activePlatform, true)}`}
          style={{ width: urlAsidePanelSize.w, height: urlAsidePanelSize.h }}
        >
          {showUrlInSidebar && (
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">Selected VOD</span>
              <button
                type="button"
                onClick={() => { setChannelVodPanelOpen(false); setVideoInfo(null); setUrl(''); }}
                className="text-zinc-500 hover:text-white p-1"
                title="Clear selection"
              >
                <X size={14} />
              </button>
            </div>
          )}
          {showUrlInPreviewMiddle && (
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">VOD · Trim</span>
            </div>
          )}
          <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
            {urlTabContent}
          </div>
          <PanelResizeHandle onMouseDown={onUrlAsidePanelResize} insetPx={panelResizeHandleInset(true)} />
        </div>
      )}
      <div
        className={`relative shrink-0 bg-zinc-950 border-2 border-white flex flex-col overflow-hidden min-h-0 ${
          triplePanelLayout ? 'p-4 gap-3' : urlMainCompact ? 'p-4 gap-2' : 'p-6 gap-4'
        } ${platformCardShadow(activePlatform)} transition-all duration-300`}
        style={{ width: mainPanelSize.w, height: mainPanelSize.h }}
      >

        {/* ── HEADER ── */}
        <div className="flex justify-between items-start shrink-0 min-w-0 gap-2">
          <div className="flex flex-col min-w-0">
            <h1 className={`font-black uppercase tracking-tighter truncate ${
              mainCardHeaderCompact ? 'text-2xl' : 'text-4xl md:text-5xl'
            }`}>
              VOD<span className="text-[#9146FF]">.</span>RIP
            </h1>
            {!mainCardHeaderCompact && (
              <p className="text-zinc-400 text-[10px] font-mono tracking-widest uppercase mt-1">
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span> Downloader
              </p>
            )}
            {triplePanelLayout && !urlMainCompact && (
              <p className="text-zinc-500 text-[9px] font-mono tracking-widest uppercase mt-0.5 truncate">
                <span className="text-[#53fc18]">Kick</span> {'//'} <span className="text-[#9146FF]">Twitch</span>
              </p>
            )}
          </div>
          <div className={`flex gap-1 shrink-0 ${mainCardHeaderCompact ? 'mt-1' : 'mt-2'}`}>
            <div className="w-2 h-2 bg-[#53fc18] rounded-full animate-pulse" />
            <div className="w-2 h-2 bg-[#9146FF] rounded-full animate-pulse" style={{ animationDelay: '0.5s' }} />
          </div>
        </div>

        {/* ── TABS ── */}
        <div className="flex w-full border-2 border-zinc-800 font-mono text-[10px] uppercase font-bold tracking-widest shrink-0">
          {visibleTabs.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 text-center transition-all flex items-center justify-center gap-2 ${
                mainCardHeaderCompact ? 'py-2' : 'py-3'
              } ${
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
          <div className="border-2 border-red-500/50 bg-red-500/10 p-3 text-red-400 text-xs font-mono flex items-center gap-2 shrink-0">
            <AlertCircle size={14} />
            {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400/60 hover:text-red-400">
              <X size={14} />
            </button>
          </div>
        )}

        <div className={`flex-1 min-h-0 ${
          showUrlInMainCard
            ? 'overflow-hidden flex flex-col'
            : 'overflow-y-auto overflow-x-hidden custom-scrollbar pr-1 pb-2 overscroll-y-contain'
        }`}>
        {/* ════════════════════════════ URL TAB ════════════════════════════ */}
        {showUrlInMainCard && urlTabContent}

        {/* ════════════════════════════ CHANNELS TAB ════════════════════════════ */}
        {tab === 'channels' && (
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
              <div className="flex flex-col gap-1">
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
                  <div className="flex flex-col gap-1">
                    {visibleChannelVideos.map((v, i) => {
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
                          <span
                            className={`shrink-0 w-4 text-center text-[9px] font-mono font-bold tabular-nums ${
                              v.platform === 'Kick' ? 'text-[#53fc18]' : 'text-[#9146FF]'
                            }`}
                            title={`${v.platform} #${v.platformListIndex}`}
                          >
                            {v.platformListIndex}
                          </span>
                          <div className="flex-1 min-w-0 text-left text-[11px] font-mono text-zinc-300 group-hover:text-white">
                            <span className="truncate flex items-center gap-1">
                              <PlatformVodIcon platform={v.platform} />
                              <span className="truncate">
                                {v.title || 'Untitled'}
                                {v.duration ? <span className="text-zinc-500 ml-1">{fmtShort(v.duration)}</span> : null}
                              </span>
                            </span>
                            {v.created_at && (
                              <span className="text-[9px] text-zinc-400 block truncate">
                                {fmtDateAndAgo(v.created_at)}
                              </span>
                            )}
                          </div>
                          <button
                            type="button"
                            title="Preview VOD"
                            onClick={(e) => {
                              e.stopPropagation();
                              void openExplorePlayer(v);
                            }}
                            className="shrink-0 border border-zinc-700 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-zinc-400 hover:border-white hover:text-white flex items-center gap-0.5"
                          >
                            <Eye size={10} />
                            Preview
                          </button>
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
                  className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white shrink-0 flex items-center gap-1 disabled:opacity-50">
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

            <button onClick={handleSaveSettings}
              className="w-full bg-zinc-900 text-zinc-200 font-black uppercase py-3 flex items-center justify-center gap-2 text-xs border-2 border-zinc-600 hover:border-white hover:text-white transition-colors">
              {settingsSaved ? <><CheckCircle2 size={16} /> Saved!</> : 'Save Settings'}
            </button>
          </div>
        )}

        <PanelResizeHandle onMouseDown={onMainPanelResize} insetPx={panelResizeHandleInset(false)} />
      </div>
      </div>
      </div>

      {/* Background */}
      <div className="fixed top-10 left-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        KICK
      </div>
      {exploreOpen && exploreVod && createPortal(
        <div
          ref={exploreContainerRef}
          style={exploreFullscreen ? undefined : {
            width: explorePopupSize.w,
            height: explorePopupSize.h,
          }}
          className={`relative flex flex-col bg-zinc-950 border-2 border-white min-h-0 ${
            platformCardShadow(exploreVod.platform === 'Twitch' ? 'twitch' : 'kick', true)
          } ${
            exploreFullscreen
              ? 'fixed inset-0 z-[200] w-screen h-screen p-0 gap-0'
              : 'fixed bottom-5 right-5 z-[200] p-3 gap-2'
          }`}
          onMouseMove={exploreFullscreen ? bumpExploreFsControls : undefined}
        >
          <div className={`flex flex-col min-h-0 ${exploreFullscreen ? 'h-full gap-0' : 'h-full gap-2 relative'}`}>
          {!exploreFullscreen && (
            <div className="flex items-start justify-between gap-2 shrink-0">
              <div className="min-w-0">
                <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
                  Channel explore
                </span>
                <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
                  {exploreVod.title}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void closeExplorePlayer()}
                className="text-zinc-500 hover:text-white p-0.5 shrink-0"
                title="Close player"
              >
                <X size={14} />
              </button>
            </div>
          )}
          <div
            className={`relative bg-black overflow-hidden ${
              exploreFullscreen ? 'flex-1 min-h-0 border-0' : 'flex-1 min-h-0 border-2 border-zinc-700'
            }`}
            onClick={(e) => {
              if ((e.target as HTMLElement).tagName === 'VIDEO') {
                toggleExplorePlay();
              }
            }}
          >
            <video
              ref={exploreVideoRef}
              className="w-full h-full object-contain"
              muted={exploreMuted}
              playsInline
              onTimeUpdate={handleExploreTimeUpdate}
              onPlay={() => setExplorePlaying(true)}
              onPause={() => setExplorePlaying(false)}
            />
            {exploreLoading && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/60 z-20">
                <Loader2 size={28} className="animate-spin text-zinc-300" />
              </div>
            )}
            {exploreError && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/80 z-20 p-3">
                <p className="text-red-400 text-[10px] font-mono text-center">{exploreError}</p>
              </div>
            )}
            {exploreFullscreen && (
              <>
                <div
                  className={`absolute inset-x-0 bottom-0 z-10 flex flex-col gap-1.5 px-3 pb-3 pt-8 bg-gradient-to-t from-black/50 to-transparent transition-opacity duration-150 ${
                    exploreFsControlsVisible ? 'opacity-100' : 'opacity-0 pointer-events-none'
                  }`}
                  onMouseMove={bumpExploreFsControls}
                >
                  {exploreTimelineUi}
                  <div className="flex items-center gap-2 pr-14">
                    <button
                      type="button"
                      onClick={toggleExplorePlay}
                      disabled={!exploreReady}
                      className="border border-white/20 bg-black/25 text-zinc-100 p-2 disabled:opacity-30 backdrop-blur-[1px]"
                    >
                      {explorePlaying ? <Pause size={18} /> : <Play size={18} />}
                    </button>
                    {exploreVolumeUi(true)}
                    <button
                      type="button"
                      onClick={carryExploreToUrl}
                      className="border border-white/20 bg-black/25 text-zinc-100 px-2 py-2 backdrop-blur-[1px] flex items-center gap-1 text-[8px] font-bold uppercase tracking-wider"
                      title="Send to URL panel for rip"
                    >
                      <ArrowRightToLine size={14} />
                      URL
                    </button>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void toggleExploreFullscreen()}
                  disabled={!exploreReady}
                  className="absolute bottom-0 right-0 z-20 flex items-end justify-end min-w-[3.5rem] min-h-[3.5rem] p-4 pointer-events-auto border border-white/20 bg-black/25 text-zinc-100 backdrop-blur-[1px] disabled:opacity-30"
                  title="Exit fullscreen"
                >
                  <Minimize2 size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => void closeExplorePlayer()}
                  className="absolute top-3 right-3 z-20 text-zinc-400 hover:text-white p-2 pointer-events-auto"
                  title="Close player"
                >
                  <X size={20} />
                </button>
              </>
            )}
          </div>
          {!exploreFullscreen && (
            <>
              {exploreTimelineUi}
              <p className="text-[8px] font-mono text-zinc-600 uppercase tracking-wider text-center shrink-0">
                Fullscreen to explore
              </p>
              <div className="flex items-center justify-between gap-2 shrink-0">
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={toggleExplorePlay}
                    disabled={!exploreReady}
                    className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white p-2 disabled:opacity-40"
                  >
                    {explorePlaying ? <Pause size={18} /> : <Play size={18} />}
                  </button>
                  {exploreVolumeUi(false)}
                  <button
                    type="button"
                    onClick={carryExploreToUrl}
                    className="border-2 border-zinc-600 text-zinc-200 hover:border-white hover:text-white px-2 py-2 disabled:opacity-40 flex items-center gap-1 text-[8px] font-bold uppercase tracking-wider"
                    title="Send to URL panel for rip"
                  >
                    <ArrowRightToLine size={14} />
                    URL
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => void toggleExploreFullscreen()}
                  disabled={!exploreReady}
                  className="border-2 border-white bg-black text-white hover:bg-white hover:text-black p-2 disabled:opacity-40 shadow-[2px_2px_0px_0px_#53fc18]"
                  title="Fullscreen"
                >
                  <Maximize2 size={18} />
                </button>
              </div>
            </>
          )}
          </div>
          {!exploreFullscreen && (
            <PanelResizeHandle onMouseDown={onExplorePanelResize} insetPx={panelResizeHandleInset(true)} />
          )}
        </div>,
        document.body,
      )}
      <div className="fixed bottom-10 right-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        TWITCH
      </div>
    </div>
  );
}
