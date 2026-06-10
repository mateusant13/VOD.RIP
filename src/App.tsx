import { useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef, type CSSProperties, type Dispatch, type KeyboardEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type ReactNode, type SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import {
  Download, Scissors, Info, Play, Pause, Link2, X, FastForward, Clock,
  Users, Database, Settings2, StopCircle, Loader2,
  CheckCircle2, AlertCircle, RefreshCw, FolderOpen, Pencil, Plus, Trash2,
  ExternalLink, Eye, Volume2, VolumeX, Maximize2, Minimize2, ArrowRightToLine,
} from 'lucide-react';
import kickIcon from '@/assets/platforms/kick.ico';
import twitchIcon from '@/assets/platforms/twitch.png';
import ChannelExplorePopup, { type ExplorePopupVod } from './ChannelExplorePopup';
import PreviewQualityMenu from './PreviewQualityMenu';
import {
  PREVIEW_MAIN_DEFAULT_HEIGHT,
  applyHlsQualityLevel,
  mapHlsLevels,
  type PreviewLevelOption,
} from './previewPlayerUtils';

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
  duration_string?: string | null;
  created_at: string | null;
  views: number | null;
  thumbnail_url: string | null;
  url: string;
  channel: string;
  content_kind?: 'vod' | 'clip';
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
  vodVideos: ChannelVideo[];
  clipVideos: ChannelVideo[];
  errors: Record<string, string>;
  updatedAt: string;
  loading?: boolean;
  /** Legacy — migrated to vodVideos / clipVideos on load */
  videos?: ChannelVideo[];
}

type Tab = 'url' | 'channels' | 'queue' | 'settings';

// ─── API ─────────────────────────────────────────────────────────────────────

const API_BASE = '';
const API_TIMEOUT_MS = 45_000;

const BACKEND_HINT =
  'Backend not running. In a terminal run: npm run dev:all  (or npm run dev:api in one terminal and npm run dev in another). API must be on http://localhost:7897.';

function apiErrorMessage(res: Response, fallback: string): string {
  if (res.status === 500 || res.status === 502 || res.status === 503) {
    return BACKEND_HINT;
  }
  return fallback;
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    return await fetch(`${API_BASE}${path}`, { ...init, signal: controller.signal });
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error(
        'Request timed out — the API may be hung. Stop and restart: npm run dev:api (or npm run dev:all).',
      );
    }
    throw new Error(BACKEND_HINT);
  } finally {
    window.clearTimeout(timer);
  }
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await apiFetch(path);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(apiErrorMessage(res, err.detail || `HTTP ${res.status}`));
  }
  return res.json();
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(apiErrorMessage(res, err.detail || `HTTP ${res.status}`));
  }
  return res.json();
}

async function apiDelete(path: string): Promise<void> {
  const res = await apiFetch(path, { method: 'DELETE' });
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
function normalizeVideoDateInput(value: string): string {
  const raw = value.trim();
  // Kick API: "YYYY-MM-DD HH:MM:SS"
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw)) {
    return `${raw.replace(' ', 'T')}Z`;
  }
  // ISO without timezone
  if (/^\d{4}-\d{2}-\d{2}T/.test(raw) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw)) {
    return `${raw}Z`;
  }
  return raw;
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return '';
  const raw = String(value).trim();
  if (!raw) return '';
  // Twitch yt-dlp: YYYYMMDD
  if (/^\d{8}$/.test(raw)) {
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  }
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

function formatClipDurationTag(sec: number): string {
  sec = Math.max(1, Math.floor(sec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m > 0) return `${m}m${s}s`;
  return `${s}s`;
}

function formatClipDurationHuman(sec: number): string {
  sec = Math.max(1, Math.floor(sec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m > 0) return `${m}:${s.toString().padStart(2, '0')}`;
  return `${s}s`;
}

type NeedleGlanceState = {
  which: 'in' | 'out';
  x: number;
  y: number;
  sec: number;
  rangeStart: number;
  rangeEnd: number;
  deltaSec: number;
};

function NeedleGlancePopup({
  glance,
  vodDurationSec,
}: {
  glance: NeedleGlanceState | null;
  vodDurationSec: number;
}) {
  if (!glance || vodDurationSec <= 0) return null;

  const clipLen = Math.max(1, glance.rangeEnd - glance.rangeStart);
  const zoomWindowSec = Math.max(30, vodDurationSec * 0.08);
  const winStart = Math.max(0, glance.sec - zoomWindowSec / 2);
  const winEnd = Math.min(vodDurationSec, winStart + zoomWindowSec);
  const winDur = Math.max(1, winEnd - winStart);
  const needlePct = ((glance.sec - winStart) / winDur) * 100;
  const zoomSelStart = Math.max(0, ((glance.rangeStart - winStart) / winDur) * 100);
  const zoomSelEnd = Math.min(100, ((glance.rangeEnd - winStart) / winDur) * 100);

  const deltaLabel = glance.deltaSec === 0
    ? null
    : `${glance.deltaSec > 0 ? '+' : ''}${glance.deltaSec}s`;

  return createPortal(
    <div
      className="needle-glance-popup fixed z-[500] pointer-events-none select-none"
      style={{ left: Math.min(glance.x + 14, window.innerWidth - 200), top: Math.max(12, glance.y - 108) }}
    >
      <div className="border-2 border-zinc-500 bg-zinc-950/95 px-3 py-2 shadow-[4px_4px_0px_0px_rgba(113,113,122,0.5)] min-w-[168px]">
        <div className="text-[9px] font-mono uppercase tracking-widest text-zinc-500 mb-1">
          {glance.which === 'in' ? 'In point' : 'Out point'}
        </div>
        <div className="text-2xl font-mono font-bold text-white tabular-nums leading-none">
          {formatHmsFull(glance.sec)}
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] font-mono text-zinc-400">
          <span>Selection</span>
          <span className="text-zinc-200">{formatHmsFull(clipLen)}</span>
        </div>
        {deltaLabel && (
          <div className="text-[10px] font-mono text-zinc-400 mt-0.5">
            Moving <span className="text-white">{deltaLabel}</span>
          </div>
        )}
        <div className="needle-glance-zoom-rail relative h-5 mt-2 rounded-sm bg-zinc-800 overflow-hidden">
          <div
            className="absolute top-1 bottom-1 bg-zinc-500/35 border-y border-zinc-400/50"
            style={{ left: `${zoomSelStart}%`, width: `${Math.max(2, zoomSelEnd - zoomSelStart)}%` }}
          />
          <div
            className="absolute top-0 bottom-0 w-0.5 bg-white -translate-x-1/2"
            style={{ left: `${needlePct}%` }}
          />
        </div>
        <div className="flex justify-between text-[8px] font-mono text-zinc-600 mt-0.5 tabular-nums">
          <span>{formatHmsFull(winStart)}</span>
          <span>{formatHmsFull(winEnd)}</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function DownloadConfirmDialog({
  open,
  title,
  message,
  detail,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  detail?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-[400] flex items-center justify-center bg-black/75 p-4"
      onClick={onCancel}
      role="presentation"
    >
      <div
        className="border-2 border-white bg-zinc-950 max-w-md w-full p-4 font-mono shadow-[6px_6px_0px_0px_#ffffff20]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="download-confirm-title"
      >
        <h3 id="download-confirm-title" className="text-sm font-black uppercase tracking-wider text-white">
          {title}
        </h3>
        <p className="text-xs text-zinc-300 mt-2 leading-relaxed">{message}</p>
        {detail && (
          <p className="text-[10px] text-zinc-500 mt-2 border border-zinc-800 bg-zinc-900/80 px-2 py-1.5">
            {detail}
          </p>
        )}
        <div className="flex gap-2 mt-4 justify-end">
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1.5 border-2 border-zinc-700 text-zinc-400 text-[10px] font-bold uppercase hover:border-zinc-500 hover:text-white"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="px-3 py-1.5 border-2 border-white bg-white text-black text-[10px] font-black uppercase hover:bg-zinc-200"
          >
            Yes, download
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function EditableHmsTime({
  valueSec,
  minSec,
  maxSec,
  onChange,
  className = '',
}: {
  valueSec: number;
  minSec: number;
  maxSec: number;
  onChange: (sec: number) => void;
  className?: string;
}) {
  const [editing, setEditing] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  const clamp = useCallback(
    (sec: number) => Math.max(minSec, Math.min(maxSec, Math.floor(sec))),
    [minSec, maxSec],
  );

  const commit = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const parsed = parseHms((el.textContent || '').trim());
    onChange(clamp(parsed));
    setEditing(false);
  }, [clamp, onChange]);

  useLayoutEffect(() => {
    if (!editing || !ref.current) return;
    ref.current.textContent = formatHmsFull(valueSec);
    const el = ref.current;
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    el.focus();
  }, [editing, valueSec]);

  if (!editing) {
    return (
      <span
        role="button"
        tabIndex={0}
        title="Click to edit (HH:MM:SS)"
        className={`cursor-text rounded px-0.5 hover:bg-zinc-800/80 focus:outline-none focus:ring-1 focus:ring-zinc-600 ${className}`}
        onClick={() => setEditing(true)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setEditing(true);
          }
          if (e.key === 'ArrowUp') {
            e.preventDefault();
            onChange(clamp(valueSec + 1));
          }
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            onChange(clamp(valueSec - 1));
          }
        }}
      >
        {formatHmsFull(valueSec)}
      </span>
    );
  }

  return (
    <span
      ref={ref}
      contentEditable
      suppressContentEditableWarning
      className={`rounded px-0.5 bg-zinc-800 outline-none ring-1 ring-zinc-500 ${className}`}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          commit();
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          setEditing(false);
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          onChange(clamp(valueSec + 1));
          if (ref.current) ref.current.textContent = formatHmsFull(clamp(valueSec + 1));
        }
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          onChange(clamp(valueSec - 1));
          if (ref.current) ref.current.textContent = formatHmsFull(clamp(valueSec - 1));
        }
      }}
    />
  );
}

const PREVIEW_KEY_SKIP_SEC = 5;
const PREVIEW_FS_CONTROLS_HIDE_MS = 200;
const PREVIEW_DEFAULT_VOLUME = 0.3;

/** Let text fields, modifiers (Ctrl+A, etc.), and contenteditable keep native behavior. */
function shouldIgnorePlayerKeyEvent(e: KeyboardEvent): boolean {
  if (e.ctrlKey || e.metaKey || e.altKey) return true;
  const el = e.target as HTMLElement;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el as HTMLInputElement).type;
    return type !== 'range' && type !== 'checkbox' && type !== 'radio';
  }
  return false;
}
type PanelSize = { w: number; h: number };
type PanelPos = { x: number; y: number };

const PREVIEW_PANEL_DEFAULT_W = 640;
const PREVIEW_PANEL_MIN_W = 280;
const PREVIEW_PANEL_CHROME_H_EST = 120;
const PREVIEW_PANEL_PAD_H = 32;
const PREVIEW_VIDEO_ASPECT_DEFAULT = 16 / 9;
const URL_ASIDE_PANEL_DEFAULT: PanelSize = { w: 288, h: 384 };
const MAIN_PANEL_DEFAULT: PanelSize = { w: 448, h: 448 };
const PANEL_MIN: PanelSize = { w: 200, h: 180 };
const PANEL_MAX_W = 1000;
/** Minimum clear space between panel chrome (incl. shadow) and viewport edges. */
const VIEWPORT_EDGE_LOCK = 40;
const EXPLORE_POPUP_Z = 9999;
const MAX_EXPLORE_POPUPS = 5;
const LAYOUT_ROW_GAP_TRIPLE = 12;
const LAYOUT_ROW_GAP_SPLIT = 24;
const EXPLORE_PANEL_DEFAULT_W = 288;
const EXPLORE_PANEL_MIN_W = 100;
const EXPLORE_PANEL_MAX_W = 960;
const EXPLORE_PANEL_CHROME_H_EST = 156;
const EXPLORE_PANEL_PAD_V = 24;
const EXPLORE_PANEL_PAD_H = 24;
const EXPLORE_VIDEO_ASPECT_DEFAULT = 16 / 9;
const CARD_BORDER_PX = 2;

function panelMaxHeight() {
  return Math.round(window.innerHeight * 0.92);
}

type LayoutPanelKey = 'preview' | 'urlAside' | 'main';

interface LayoutPanelBoundsInput {
  previewOpen: boolean;
  urlPanelAside: boolean;
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
}

function viewportContentBox(shadowPad = panelResizeHandleInset(false)): { maxW: number; maxH: number } {
  return {
    maxW: Math.max(PANEL_MIN.w, window.innerWidth - VIEWPORT_EDGE_LOCK * 2 - shadowPad),
    maxH: Math.max(PANEL_MIN.h, window.innerHeight - VIEWPORT_EDGE_LOCK * 2 - shadowPad),
  };
}

function layoutRowGap(previewOpen: boolean, urlPanelAside: boolean): number {
  const count = (previewOpen ? 1 : 0) + (urlPanelAside ? 1 : 0) + 1;
  if (count <= 1) return 0;
  return previewOpen && urlPanelAside ? LAYOUT_ROW_GAP_TRIPLE : LAYOUT_ROW_GAP_SPLIT;
}

function layoutMaxPanelWidth(target: LayoutPanelKey, layout: LayoutPanelBoundsInput): number {
  const { maxW } = viewportContentBox();
  const count = (layout.previewOpen ? 1 : 0) + (layout.urlPanelAside ? 1 : 0) + 1;
  const gapTotal = Math.max(0, count - 1) * layoutRowGap(layout.previewOpen, layout.urlPanelAside);

  let othersW = 0;
  if (layout.previewOpen && target !== 'preview') othersW += layout.preview.w;
  if (layout.urlPanelAside && target !== 'urlAside') othersW += layout.urlAside.w;
  if (target !== 'main') othersW += layout.main.w;

  return Math.max(PANEL_MIN.w, Math.min(PANEL_MAX_W, maxW - othersW - gapTotal));
}

function layoutMaxPanelHeight(): number {
  return Math.min(panelMaxHeight(), viewportContentBox().maxH);
}

function clampPanelSizeForLayout(
  target: LayoutPanelKey,
  size: PanelSize,
  layout: LayoutPanelBoundsInput,
): PanelSize {
  const maxW = layoutMaxPanelWidth(target, layout);
  const maxH = layoutMaxPanelHeight();
  return {
    w: Math.min(maxW, Math.max(PANEL_MIN.w, size.w)),
    h: Math.min(maxH, Math.max(PANEL_MIN.h, size.h)),
  };
}

function clampAllLayoutPanels(layout: LayoutPanelBoundsInput): {
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
} {
  const maxH = layoutMaxPanelHeight();
  let preview = { ...layout.preview };
  let urlAside = { ...layout.urlAside };
  let main = { ...layout.main };
  const snapshot = (): LayoutPanelBoundsInput => ({
    ...layout,
    preview,
    urlAside,
    main,
  });

  if (layout.previewOpen) {
    const w = clampPreviewPanelWidth(
      preview.w,
      PREVIEW_PANEL_CHROME_H_EST,
      PREVIEW_VIDEO_ASPECT_DEFAULT,
      snapshot(),
    );
    preview = { w, h: preview.h };
  }
  if (layout.urlPanelAside) {
    urlAside = clampPanelSizeForLayout('urlAside', { ...urlAside, h: Math.min(urlAside.h, maxH) }, snapshot());
  }
  main = clampPanelSizeForLayout('main', { ...main, h: Math.min(main.h, maxH) }, snapshot());

  return { preview, urlAside, main };
}

function exploreViewportBox(): { maxW: number; maxH: number } {
  const shadowPad = panelResizeHandleInset(true);
  const box = viewportContentBox(shadowPad);
  return {
    maxW: Math.max(EXPLORE_PANEL_MIN_W, box.maxW),
    maxH: Math.max(140, box.maxH),
  };
}

function maxExplorePanelWidth(chromeH: number, aspect: number): number {
  const { maxW, maxH } = exploreViewportBox();
  const capW = Math.min(EXPLORE_PANEL_MAX_W, maxW);
  const videoMaxW = capW - EXPLORE_PANEL_PAD_H;
  const videoMaxH = Math.max(80, maxH - chromeH - EXPLORE_PANEL_PAD_V);
  const videoMaxWFromH = videoMaxH * aspect;
  return Math.floor(Math.min(videoMaxW, videoMaxWFromH) + EXPLORE_PANEL_PAD_H);
}

function clampExplorePanelWidth(width: number, chromeH: number, aspect: number): number {
  const maxW = maxExplorePanelWidth(chromeH, aspect);
  return Math.min(maxW, Math.max(EXPLORE_PANEL_MIN_W, width));
}

function maxPreviewPanelWidth(
  chromeH: number,
  aspect: number,
  layout: LayoutPanelBoundsInput,
): number {
  const shadowPad = panelResizeHandleInset(true);
  const { maxH } = viewportContentBox(shadowPad);
  const capW = Math.min(PANEL_MAX_W, layoutMaxPanelWidth('preview', layout));
  const videoMaxW = capW - PREVIEW_PANEL_PAD_H;
  const videoMaxH = Math.max(100, maxH - chromeH - PREVIEW_PANEL_PAD_H);
  const videoMaxWFromH = videoMaxH * aspect;
  return Math.floor(Math.min(videoMaxW, videoMaxWFromH) + PREVIEW_PANEL_PAD_H);
}

function clampPreviewPanelWidth(
  width: number,
  chromeH: number,
  aspect: number,
  layout: LayoutPanelBoundsInput,
): number {
  const minW = Math.min(PREVIEW_PANEL_MIN_W, maxPreviewPanelWidth(chromeH, aspect, layout));
  const maxW = maxPreviewPanelWidth(chromeH, aspect, layout);
  return Math.min(maxW, Math.max(minW, width));
}

function defaultExplorePopupPosition(panelW: number, panelH: number): PanelPos {
  const shadowPad = panelResizeHandleInset(true);
  return {
    x: window.innerWidth - VIEWPORT_EDGE_LOCK - panelW - shadowPad,
    y: window.innerHeight - VIEWPORT_EDGE_LOCK - panelH - shadowPad,
  };
}

function applyExplorePopupWindowPosition(el: HTMLElement, pos: PanelPos) {
  el.style.position = 'fixed';
  el.style.top = `${pos.y}px`;
  el.style.left = `${pos.x}px`;
  el.style.right = 'auto';
  el.style.bottom = 'auto';
  el.style.zIndex = String(EXPLORE_POPUP_Z);
}

function layoutExplorePopupWindow(
  el: HTMLElement,
  width: number,
  posRef: MutableRefObject<PanelPos | null>,
): PanelPos {
  applyPanelWidth(el, width);
  if (!posRef.current) {
    posRef.current = defaultExplorePopupPosition(el.offsetWidth, el.offsetHeight);
  }
  applyExplorePopupWindowPosition(el, posRef.current);
  return posRef.current;
}

function edgeAffectsWest(edge: ResizeEdge): boolean {
  return edge === 'w' || edge === 'nw' || edge === 'sw';
}

function edgeAffectsNorth(edge: ResizeEdge): boolean {
  return edge === 'n' || edge === 'ne' || edge === 'nw';
}

function applyExplorePopupFullscreenPosition(el: HTMLElement) {
  el.style.position = 'fixed';
  el.style.top = '0';
  el.style.left = '0';
  el.style.right = '0';
  el.style.bottom = '0';
  el.style.zIndex = String(EXPLORE_POPUP_Z);
}

/** Distance from panel padding edge to outer colored shadow corner (border + shadow offset). */
function panelResizeHandleInset(compact: boolean): number {
  return CARD_BORDER_PX + (compact ? 4 : 6);
}

type ResizeEdge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

const RESIZE_EDGE_CURSORS: Record<ResizeEdge, string> = {
  n: 'ns-resize',
  s: 'ns-resize',
  e: 'ew-resize',
  w: 'ew-resize',
  ne: 'nesw-resize',
  nw: 'nwse-resize',
  se: 'nwse-resize',
  sw: 'nesw-resize',
};

function calcPanelSizeFromEdge(
  edge: ResizeEdge,
  startW: number,
  startH: number,
  dx: number,
  dy: number,
): PanelSize {
  let w = startW;
  let h = startH;
  if (edge === 'e' || edge === 'ne' || edge === 'se') w = startW + dx;
  else if (edge === 'w' || edge === 'nw' || edge === 'sw') w = startW - dx;
  if (edge === 's' || edge === 'se' || edge === 'sw') h = startH + dy;
  else if (edge === 'n' || edge === 'ne' || edge === 'nw') h = startH - dy;
  return { w, h };
}

function widthDeltaFromEdge(edge: ResizeEdge, dx: number, dy: number, aspect: number): number {
  switch (edge) {
    case 'e': return dx;
    case 'w': return -dx;
    case 's': return dy * aspect;
    case 'n': return -dy * aspect;
    case 'se': return Math.max(dx, dy * aspect);
    case 'sw': return Math.max(-dx, dy * aspect);
    case 'ne': return Math.max(dx, -dy * aspect);
    case 'nw': return Math.max(-dx, -dy * aspect);
    default: return dx;
  }
}

function PanelResizeHandles({
  onPointerDown,
  insetPx,
}: {
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => void;
  insetPx: number;
}) {
  const hit = 'absolute z-50 pointer-events-auto select-none touch-none';
  const edgePad = 12;

  const edgeProps = (edge: ResizeEdge, style: CSSProperties, hoverCursorClass: string, sizeClass = '') => ({
    'data-panel-resize': true as const,
    'aria-hidden': true as const,
    onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => onPointerDown(e, edge),
    style: { ...style, touchAction: 'none' },
    className: `${hit} cursor-default ${hoverCursorClass} ${sizeClass}`.trim(),
  });

  return (
    <>
      <div {...edgeProps('n', { top: -insetPx - 3, left: edgePad, right: edgePad, height: 6 }, 'group-hover:cursor-ns-resize')} />
      <div {...edgeProps('s', { bottom: -insetPx - 3, left: edgePad, right: edgePad, height: 6 }, 'group-hover:cursor-ns-resize')} />
      <div {...edgeProps('e', { right: -insetPx - 3, top: edgePad, bottom: edgePad, width: 6 }, 'group-hover:cursor-ew-resize')} />
      <div {...edgeProps('w', { left: -insetPx - 3, top: edgePad, bottom: edgePad, width: 6 }, 'group-hover:cursor-ew-resize')} />
      <div {...edgeProps('nw', { top: -insetPx, left: -insetPx }, 'group-hover:cursor-nwse-resize', 'w-4 h-4')} />
      <div {...edgeProps('ne', { top: -insetPx, right: -insetPx }, 'group-hover:cursor-nesw-resize', 'w-4 h-4')} />
      <div {...edgeProps('sw', { bottom: -insetPx, left: -insetPx }, 'group-hover:cursor-nesw-resize', 'w-4 h-4')} />
      <div {...edgeProps('se', { bottom: -insetPx, right: -insetPx }, 'group-hover:cursor-nwse-resize', 'w-4 h-4')} />
    </>
  );
}

function applyPanelSize(el: HTMLElement, size: PanelSize) {
  el.style.width = `${size.w}px`;
  el.style.height = `${size.h}px`;
}

function startPanelResizeDrag(
  e: ReactPointerEvent<HTMLDivElement>,
  edge: ResizeEdge,
  sizeRef: MutableRefObject<PanelSize>,
  setSize: Dispatch<SetStateAction<PanelSize>>,
  opts?: {
    maxW?: number;
    maxH?: number;
    panelEl?: HTMLElement | null;
    clampSize?: (size: PanelSize) => PanelSize;
  },
) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const { w: startW, h: startH } = sizeRef.current;
  const maxW = opts?.maxW ?? PANEL_MAX_W;
  const maxH = opts?.maxH ?? panelMaxHeight();
  const panelEl = opts?.panelEl ?? null;

  if (panelEl) {
    panelEl.style.willChange = 'width, height';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = RESIZE_EDGE_CURSORS[edge];

  const calcSize = (clientX: number, clientY: number): PanelSize => {
    const raw = calcPanelSizeFromEdge(edge, startW, startH, clientX - startX, clientY - startY);
    return {
      w: Math.min(maxW, Math.max(PANEL_MIN.w, raw.w)),
      h: Math.min(maxH, Math.max(PANEL_MIN.h, raw.h)),
    };
  };

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    let next = calcSize(ev.clientX, ev.clientY);
    if (opts?.clampSize) next = opts.clampSize(next);
    sizeRef.current = next;
    if (panelEl) {
      applyPanelSize(panelEl, next);
    }
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
    if (panelEl) {
      panelEl.style.willChange = '';
    }
    const final = opts?.clampSize ? opts.clampSize(sizeRef.current) : sizeRef.current;
    sizeRef.current = final;
    if (panelEl) {
      applyPanelSize(panelEl, final);
    }
    setSize({ ...final });
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}

function applyPanelWidth(el: HTMLElement, width: number) {
  el.style.width = `${width}px`;
  el.style.height = '';
}

function startPanelWidthResize(
  e: ReactPointerEvent<HTMLDivElement>,
  edge: ResizeEdge,
  widthRef: MutableRefObject<number>,
  setWidth: Dispatch<SetStateAction<number>>,
  opts: {
    panelEl: HTMLElement | null;
    clampWidth: (w: number) => number;
    aspect: number;
    posRef?: MutableRefObject<PanelPos | null>;
    setPos?: Dispatch<SetStateAction<PanelPos | null>>;
  },
) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const startX = e.clientX;
  const startY = e.clientY;
  const startW = widthRef.current;
  const startPos = opts.posRef?.current ? { ...opts.posRef.current } : null;
  const panelEl = opts.panelEl;
  const clamp = opts.clampWidth;

  if (panelEl) {
    panelEl.style.willChange = 'width';
  }
  const prevUserSelect = document.body.style.userSelect;
  const prevCursor = document.body.style.cursor;
  document.body.style.userSelect = 'none';
  document.body.style.cursor = RESIZE_EDGE_CURSORS[edge];

  const applyWidthAndPos = (nextW: number) => {
    widthRef.current = nextW;
    if (panelEl) {
      applyPanelWidth(panelEl, nextW);
    }
    if (startPos && opts.posRef && panelEl) {
      let x = startPos.x;
      let y = startPos.y;
      if (edgeAffectsWest(edge)) {
        x = startPos.x + startW - nextW;
      }
      if (edgeAffectsNorth(edge)) {
        y = startPos.y - (nextW - startW) / opts.aspect;
      }
      const pos = { x, y };
      opts.posRef.current = pos;
      applyExplorePopupWindowPosition(panelEl, pos);
    }
  };

  const onMove = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    const delta = widthDeltaFromEdge(edge, ev.clientX - startX, ev.clientY - startY, opts.aspect);
    applyWidthAndPos(clamp(startW + delta));
  };

  const onUp = (ev: PointerEvent) => {
    if (ev.pointerId !== e.pointerId) return;
    handle.releasePointerCapture(e.pointerId);
    handle.removeEventListener('pointermove', onMove);
    handle.removeEventListener('pointerup', onUp);
    handle.removeEventListener('pointercancel', onUp);
    document.body.style.userSelect = prevUserSelect;
    document.body.style.cursor = prevCursor;
    if (panelEl) {
      panelEl.style.willChange = '';
    }
    const finalW = clamp(widthRef.current);
    applyWidthAndPos(finalW);
    setWidth(finalW);
    if (opts.setPos && opts.posRef?.current) {
      opts.setPos({ ...opts.posRef.current });
    }
  };

  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);
}

function sourceQualityOptionLabel(resolutionLabel: string): string {
  return `source/${resolutionLabel.toLowerCase()}`;
}

function maxStreamQualityLabel(
  qualities: string[],
  previewLevels: PreviewLevelOption[],
): string {
  if (previewLevels.length) {
    const maxH = Math.max(...previewLevels.map((l) => l.height));
    if (maxH > 0) {
      const match = qualities.find((q) => {
        const m = q.match(/(\d+)/);
        return m && parseInt(m[1], 10) === maxH;
      });
      if (match) return match.toLowerCase();
      return `${maxH}p`;
    }
  }
  if (qualities.length) return qualities[0].toLowerCase();
  return '1080p';
}

const CHANNEL_INITIAL_VISIBLE = 5;
const CHANNEL_EXPAND_STEP = 10;
const CHANNEL_FETCH_LIMIT = 100;
/** Cheap head fetch on page load — merge only ids not already cached. */
const CHANNEL_INCREMENTAL_LIMIT = 25;
const CHANNELS_STORAGE_KEY = 'vodrip_saved_channels';
const PANEL_LAYOUT_STORAGE_KEY = 'vodrip_panel_layout';

interface PersistedPanelLayout {
  previewPanelWidth: number;
  urlAside: PanelSize;
  main: PanelSize;
}

function clampLayoutNumber(value: unknown, min: number, max: number, fallback: number): number {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}

function clampStoredPanelSize(value: unknown, fallback: PanelSize): PanelSize {
  if (!value || typeof value !== 'object') return fallback;
  const o = value as { w?: unknown; h?: unknown };
  const maxH = typeof window !== 'undefined' ? panelMaxHeight() : fallback.h;
  return {
    w: clampLayoutNumber(o.w, PANEL_MIN.w, PANEL_MAX_W, fallback.w),
    h: clampLayoutNumber(o.h, PANEL_MIN.h, maxH, fallback.h),
  };
}

function loadPanelLayout(): PersistedPanelLayout {
  const fallback: PersistedPanelLayout = {
    previewPanelWidth: PREVIEW_PANEL_DEFAULT_W,
    urlAside: URL_ASIDE_PANEL_DEFAULT,
    main: MAIN_PANEL_DEFAULT,
  };
  try {
    const raw = localStorage.getItem(PANEL_LAYOUT_STORAGE_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<PersistedPanelLayout>;
    return {
      previewPanelWidth: clampLayoutNumber(
        parsed.previewPanelWidth,
        PREVIEW_PANEL_MIN_W,
        PANEL_MAX_W,
        PREVIEW_PANEL_DEFAULT_W,
      ),
      urlAside: clampStoredPanelSize(parsed.urlAside, URL_ASIDE_PANEL_DEFAULT),
      main: clampStoredPanelSize(parsed.main, MAIN_PANEL_DEFAULT),
    };
  } catch {
    return fallback;
  }
}

function persistPanelLayout(layout: PersistedPanelLayout) {
  try {
    localStorage.setItem(PANEL_LAYOUT_STORAGE_KEY, JSON.stringify(layout));
  } catch {
    /* quota / private mode */
  }
}
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

function isClipUrl(u: string): boolean {
  const l = u.toLowerCase();
  if (l.includes('clips.twitch.tv')) return true;
  if (l.includes('twitch.tv') && l.includes('/clip/')) return true;
  if (l.includes('kick.com') && l.includes('/clips/')) return true;
  return false;
}

function channelVideoDurationSec(v: ChannelVideo): number | null {
  if (v.duration != null && v.duration > 0) return Math.floor(v.duration);
  if (v.duration_string) {
    const parts = v.duration_string.split(':').map(Number);
    if (parts.length === 3 && parts.every((n) => !Number.isNaN(n))) {
      return parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
    if (parts.length === 2 && parts.every((n) => !Number.isNaN(n))) {
      return parts[0] * 60 + parts[1];
    }
  }
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

function normalizeSavedChannel(ch: SavedChannel): SavedChannel {
  const { videos: legacy, ...rest } = ch;
  let vodVideos = ch.vodVideos;
  let clipVideos = ch.clipVideos;
  if (vodVideos === undefined && clipVideos === undefined && Array.isArray(legacy)) {
    vodVideos = legacy.filter((v) => !isLikelyClip(v));
    clipVideos = legacy.filter(isLikelyClip);
  }
  return {
    ...rest,
    vodVideos: vodVideos ?? [],
    clipVideos: clipVideos ?? [],
    loading: false,
  };
}

function loadSavedChannels(): SavedChannel[] {
  try {
    const raw = localStorage.getItem(CHANNELS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((ch) => normalizeSavedChannel(ch as SavedChannel));
  } catch {
    return [];
  }
}

function persistChannels(channels: SavedChannel[]) {
  const toStore = channels.map(({ loading: _loading, ...ch }) => ch);
  localStorage.setItem(CHANNELS_STORAGE_KEY, JSON.stringify(toStore));
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
  if (!raw) return 0;
  if (/^\d{8}$/.test(raw)) {
    return new Date(`${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}T00:00:00Z`).getTime() || 0;
  }
  const t = Date.parse(normalizeVideoDateInput(raw));
  return Number.isNaN(t) ? 0 : t;
}

function fmtViews(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`;
  return String(n);
}

function channelVodSubline(v: ChannelVideo): string {
  const parts: string[] = [];
  const when = fmtDateAndAgo(v.created_at);
  if (when) parts.push(when);
  const durSec = channelVideoDurationSec(v);
  if (durSec != null) parts.push(fmtDuration(durSec));
  if (v.views != null && Number(v.views) > 0) {
    parts.push(`${fmtViews(Number(v.views))} views`);
  }
  return parts.join(' · ');
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

const CLIP_MAX_DURATION_SEC = 60;

function isLikelyClip(v: ChannelVideo): boolean {
  if (v.content_kind === 'clip') {
    if (v.duration != null && v.duration > CLIP_MAX_DURATION_SEC) return false;
    return true;
  }
  const url = (v.url || '').toLowerCase();
  if (url.includes('/videos/') && !url.includes('/clips/') && !url.includes('/clip/')) {
    return false;
  }
  if (url.includes('/clips/') || url.includes('clips.twitch.tv') || url.includes('/clip/')) {
    if (v.duration != null && v.duration > CLIP_MAX_DURATION_SEC) return false;
    return true;
  }
  return false;
}

function channelVideoKey(v: ChannelVideo): string {
  return `${v.platform}:${v.id}`;
}

function mapApiChannelItem(v: ChannelVideo & { thumbnail?: string | null }): ChannelVideo {
  return {
    ...v,
    thumbnail_url: v.thumbnail_url ?? v.thumbnail ?? null,
  };
}

/** Merge feeds newest-first; incoming wins on duplicate ids (metadata refresh). */
function mergeVodLists(existing: ChannelVideo[], incoming: ChannelVideo[]): ChannelVideo[] {
  const map = new Map<string, ChannelVideo>();
  for (const v of incoming.map(mapApiChannelItem)) {
    map.set(channelVideoKey(v), v);
  }
  for (const v of existing) {
    const k = channelVideoKey(v);
    if (!map.has(k)) map.set(k, v);
  }
  return Array.from(map.values()).sort(
    (a, b) => parseVideoTs(b.created_at) - parseVideoTs(a.created_at),
  );
}

function buildVodUrl(v: ChannelVideo): string {
  if (v.url) return v.url;
  const isTw = v.platform === 'Twitch';
  const isClip = v.content_kind === 'clip';
  const twitchId = isTw && v.id.startsWith('v') ? v.id.slice(1) : v.id;
  if (isTw) {
    return isClip
      ? `https://clips.twitch.tv/${twitchId}`
      : `https://www.twitch.tv/videos/${twitchId}`;
  }
  return isClip
    ? `https://kick.com/${v.channel || ''}/clips/${v.id}`
    : `https://kick.com/${v.channel || ''}/videos/${v.id}`;
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
  const [previewMuted, setPreviewMuted] = useState(false);
  const [previewVolume, setPreviewVolume] = useState(PREVIEW_DEFAULT_VOLUME);
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
  const previewVolumeRef = useRef(PREVIEW_DEFAULT_VOLUME);
  const previewFsHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const previewInitialSeekDoneRef = useRef(false);
  const previewInitialPlayDoneRef = useRef(false);
  const previewSuppressPlayRef = useRef(false);
  const previewTrimStartRef = useRef(0);
  const previewTrimEndRef = useRef(3600);
  const previewNeedleRailRef = useRef<HTMLDivElement>(null);
  const [needleGlance, setNeedleGlance] = useState<NeedleGlanceState | null>(null);
  const [downloadConfirmOpen, setDownloadConfirmOpen] = useState(false);
  const trimDragOriginRef = useRef(0);
  const urlTrimPointerRef = useRef({ x: 0, y: 0 });

  // Channel explore players (up to 5 floating popups)
  const [explorePopups, setExplorePopups] = useState<{ id: string; vod: ExplorePopupVod; layoutIndex: number }[]>([]);
  const [exploreZOrder, setExploreZOrder] = useState<Record<string, number>>({});
  const [anyExploreVolumeMenuOpen, setAnyExploreVolumeMenuOpen] = useState(false);
  const [exploreVolumeMenuCloseTick, setExploreVolumeMenuCloseTick] = useState(0);
  const explorePauseMapRef = useRef(new Map<string, () => void>());
  const exploreVolumeMenusRef = useRef(new Set<string>());
  const exploreZCounterRef = useRef(0);
  const [initialPanelLayout] = useState(loadPanelLayout);
  const [previewPanelWidth, setPreviewPanelWidth] = useState(initialPanelLayout.previewPanelWidth);
  const [previewVideoAspect, setPreviewVideoAspect] = useState(PREVIEW_VIDEO_ASPECT_DEFAULT);
  const [urlAsidePanelSize, setUrlAsidePanelSize] = useState(initialPanelLayout.urlAside);
  const [mainPanelSize, setMainPanelSize] = useState(initialPanelLayout.main);
  const previewPanelWidthRef = useRef(initialPanelLayout.previewPanelWidth);
  const previewVideoAspectRef = useRef(PREVIEW_VIDEO_ASPECT_DEFAULT);
  const previewChromeHRef = useRef(PREVIEW_PANEL_CHROME_H_EST);
  const urlAsidePanelSizeRef = useRef(initialPanelLayout.urlAside);
  const mainPanelSizeRef = useRef(initialPanelLayout.main);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const urlAsidePanelRef = useRef<HTMLDivElement>(null);
  const mainPanelRef = useRef<HTMLDivElement>(null);

  // Persist main layout panels (preview / URL aside / main card) — not channel explore popups.
  useEffect(() => {
    persistPanelLayout({
      previewPanelWidth,
      urlAside: urlAsidePanelSize,
      main: mainPanelSize,
    });
  }, [previewPanelWidth, urlAsidePanelSize, mainPanelSize]);

  // Queue
  const [activeDownloads, setActiveDownloads] = useState<DownloadState[]>([]);
  const [historyDownloads, setHistoryDownloads] = useState<DownloadState[]>([]);
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
  const [channelContentFilter, setChannelContentFilter] = useState<'vods' | 'clips'>('vods');

  const selectedChannel = useMemo(
    () => savedChannels.find((c) => c.id === selectedChannelId) ?? null,
    [savedChannels, selectedChannelId],
  );

  const allChannelVideos = useMemo(() => {
    if (!selectedChannel) return [];
    return channelContentFilter === 'clips'
      ? (selectedChannel.clipVideos ?? [])
      : (selectedChannel.vodVideos ?? []);
  }, [selectedChannel, channelContentFilter]);

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
    const sorted = channelContentFilter === 'clips'
      ? items.sort((a, b) => (Number(b.views) || 0) - (Number(a.views) || 0))
      : items.sort((a, b) => parseVideoTs(b.created_at) - parseVideoTs(a.created_at));
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
    channelContentFilter,
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

  const seekPreviewVideo = useCallback((sec: number, force = false) => {
    const video = previewVideoRef.current;
    if (!video || !previewVideoReady) return;
    const t = Math.max(0, Math.min(sec, vodDurationSec));
    if (force || Math.abs(video.currentTime - t) > 0.05) {
      video.currentTime = t;
      setPreviewCurrentTime(t);
    }
  }, [previewVideoReady, vodDurationSec]);

  const openPreview = useCallback(async () => {
    if (!url.trim()) return;
    const clip = isClipUrl(url.trim());
    if (!clip && trimEndSec <= trimStartSec) return;
    const start = clip ? 0 : trimStartSec;
    const end = clip ? vodDurationSec : trimEndSec;
    previewTrimStartRef.current = start;
    previewTrimEndRef.current = end;
    setPreviewTrimStart(start);
    setPreviewTrimEnd(end);
    previewInitialSeekDoneRef.current = false;
    previewInitialPlayDoneRef.current = false;
    previewVolumeRef.current = PREVIEW_DEFAULT_VOLUME;
    setPreviewVolume(PREVIEW_DEFAULT_VOLUME);
    setPreviewMuted(false);
    previewVideoAspectRef.current = PREVIEW_VIDEO_ASPECT_DEFAULT;
    setPreviewVideoAspect(PREVIEW_VIDEO_ASPECT_DEFAULT);
    const clampedPreviewW = clampPreviewPanelWidth(
      previewPanelWidthRef.current,
      previewChromeHRef.current,
      PREVIEW_VIDEO_ASPECT_DEFAULT,
      {
        previewOpen: true,
        urlPanelAside: true,
        preview: { w: previewPanelWidthRef.current, h: 0 },
        urlAside: urlAsidePanelSizeRef.current,
        main: mainPanelSizeRef.current,
      },
    );
    previewPanelWidthRef.current = clampedPreviewW;
    setPreviewPanelWidth(clampedPreviewW);
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
        crop_start: start,
        crop_end: end,
        prefer_height: PREVIEW_MAIN_DEFAULT_HEIGHT,
      });
      setPreviewSessionId(res.session_id);
      setPreviewHlsUrl(res.master_url);
    } catch (err: any) {
      setError(err.message || 'Preview failed');
      setPreviewOpen(false);
      setPreviewVideoLoading(false);
    }
  }, [url, trimEndSec, trimStartSec, vodDurationSec, previewSessionId, destroyPreviewPlayer]);

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
      video.volume = PREVIEW_DEFAULT_VOLUME;
      previewVolumeRef.current = PREVIEW_DEFAULT_VOLUME;
      setPreviewVolume(PREVIEW_DEFAULT_VOLUME);
      video.muted = false;
      setPreviewMuted(false);
      performInitialSeek();
      if (!previewInitialPlayDoneRef.current && video.paused) {
        previewInitialPlayDoneRef.current = true;
        void video.play().catch(() => {
          video.muted = true;
          setPreviewMuted(true);
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
        startPosition: previewTrimStartRef.current,
      });
      previewHlsRef.current = hls;
      hls.loadSource(previewHlsUrl);
      hls.attachMedia(video);
      let levelsInitialized = false;
      const syncPreviewLevels = (levels = hls.levels, applyDefault = false) => {
        const { mapped, defaultIndex } = mapHlsLevels(levels, PREVIEW_MAIN_DEFAULT_HEIGHT);
        if (!mapped.length) return;
        setPreviewLevels(mapped);
        if (!levelsInitialized || applyDefault) {
          levelsInitialized = true;
          hls.loadLevel = defaultIndex;
          setPreviewQualityLevel(defaultIndex);
        }
      };

      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        syncPreviewLevels(data.levels ?? hls.levels, true);
      });
      hls.on(Hls.Events.LEVELS_UPDATED, () => {
        syncPreviewLevels(hls.levels);
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

  useEffect(() => {
    if (!previewOpen) return;
    previewTrimStartRef.current = trimStartSec;
    previewTrimEndRef.current = trimEndSec;
    setPreviewTrimStart(trimStartSec);
    setPreviewTrimEnd(trimEndSec);
  }, [previewOpen, trimStartSec, trimEndSec]);

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
      applyHlsQualityLevel(hls, levelIndex, forceLoad);
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

  const applyTrimRange = useCallback((
    rawStart: number,
    rawEnd: number,
    opts?: { seek?: 'in' | 'out' },
  ) => {
    const dur = Math.max(1, vodDurationSec);
    let start = Math.floor(rawStart);
    let end = Math.floor(rawEnd);
    if (start >= end) {
      if (opts?.seek === 'in') {
        end = Math.min(dur, start + 1);
      } else {
        start = Math.max(0, end - 1);
      }
    }
    start = Math.max(0, Math.min(start, dur - 1));
    end = Math.min(dur, Math.max(end, start + 1));
    setTrimStartSec(start);
    setTrimEndSec(end);
    previewTrimStartRef.current = start;
    previewTrimEndRef.current = end;
    setPreviewTrimStart(start);
    setPreviewTrimEnd(end);
    if (opts?.seek === 'in') seekPreviewVideo(start, true);
    else if (opts?.seek === 'out') seekPreviewVideo(end, true);
    return { start, end };
  }, [vodDurationSec, seekPreviewVideo]);

  const updateNeedleGlance = useCallback((
    which: 'in' | 'out',
    ev: PointerEvent,
    rangeStart: number,
    rangeEnd: number,
    activeSec: number,
    deltaSec: number,
  ) => {
    setNeedleGlance({
      which,
      x: ev.clientX,
      y: ev.clientY,
      sec: activeSec,
      rangeStart,
      rangeEnd,
      deltaSec,
    });
  }, []);

  const beginPreviewNeedleDrag = useCallback((
    e: ReactPointerEvent<HTMLElement>,
    which: 'in' | 'out',
  ) => {
    e.preventDefault();
    e.stopPropagation();
    const rail = previewNeedleRailRef.current;
    if (!rail || vodDurationSec <= 0) return;

    const xToSec = (clientX: number) => {
      const rect = rail.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return Math.round(frac * vodDurationSec);
    };

    const dragOrigin = which === 'in' ? trimStartSec : trimEndSec;
    trimDragOriginRef.current = dragOrigin;
    let start = trimStartSec;
    let end = trimEndSec;

    const onMove = (ev: PointerEvent) => {
      const sec = xToSec(ev.clientX);
      if (which === 'in') {
        start = sec;
        if (start >= end) end = Math.min(vodDurationSec, start + 1);
        start = Math.max(0, Math.min(start, end - 1));
      } else {
        end = sec;
        if (end <= start) start = Math.max(0, end - 1);
        end = Math.min(vodDurationSec, Math.max(end, start + 1));
      }
      const applied = applyTrimRange(start, end, { seek: which });
      const activeSec = which === 'in' ? applied.start : applied.end;
      updateNeedleGlance(
        which,
        ev,
        applied.start,
        applied.end,
        activeSec,
        activeSec - dragOrigin,
      );
    };

    const onUp = () => {
      setNeedleGlance(null);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    onMove(e.nativeEvent);
  }, [trimStartSec, trimEndSec, vodDurationSec, applyTrimRange, updateNeedleGlance]);

  const handleUrlTrimSlider = useCallback((
    which: 'in' | 'out',
    value: number,
    pointer?: { x: number; y: number },
  ) => {
    const dragOrigin = trimDragOriginRef.current;
    let start = trimStartSec;
    let end = trimEndSec;
    if (which === 'in') {
      start = value;
      if (start >= end) end = Math.min(vodDurationSec, start + 1);
      start = Math.max(0, Math.min(start, end - 1));
    } else {
      end = value;
      if (end <= start) start = Math.max(0, end - 1);
      end = Math.min(vodDurationSec, Math.max(end, start + 1));
    }
    const applied = applyTrimRange(start, end, { seek: which });
    if (pointer) {
      const activeSec = which === 'in' ? applied.start : applied.end;
      setNeedleGlance({
        which,
        x: pointer.x,
        y: pointer.y,
        sec: activeSec,
        rangeStart: applied.start,
        rangeEnd: applied.end,
        deltaSec: activeSec - dragOrigin,
      });
    }
  }, [trimStartSec, trimEndSec, vodDurationSec, applyTrimRange]);

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

  const handlePreviewContainerKeyDown = useCallback((e: KeyboardEvent) => {
    if (!previewVideoReady) return;
    if (shouldIgnorePlayerKeyEvent(e)) return;

    const { key } = e;
    const transportKeys = [' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End',
      '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'f', 'F'];
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
    if (key.toLowerCase() === 'f') {
      void togglePreviewFullscreen();
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
  }, [
    previewVideoReady,
    togglePreviewPlay,
    skipPreview,
    setPreviewVolumeLevel,
    seekPreviewPercent,
    togglePreviewFullscreen,
  ]);

  useEffect(() => {
    const onFullscreenChange = () => {
      const fs = document.fullscreenElement === previewContainerRef.current;
      setPreviewFullscreen(fs);
      setPreviewFsControlsVisible(!fs);
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, []);

  const anyPlayerMenuOpen = previewQualityMenuOpen || previewVolumeMenuOpen || anyExploreVolumeMenuOpen;

  useEffect(() => {
    if (!anyPlayerMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest('[data-player-menu]')) return;
      setPreviewQualityMenuOpen(false);
      setPreviewVolumeMenuOpen(false);
      setExploreVolumeMenuCloseTick((t) => t + 1);
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

  // ── Channel explore players ──

  const pauseAllExplorePopups = useCallback(() => {
    explorePauseMapRef.current.forEach((pause) => pause());
  }, []);

  const registerExplorePause = useCallback((id: string, pause: () => void) => {
    explorePauseMapRef.current.set(id, pause);
  }, []);

  const unregisterExplorePause = useCallback((id: string) => {
    explorePauseMapRef.current.delete(id);
  }, []);

  const handleExploreVolumeMenuOpen = useCallback((id: string, open: boolean) => {
    if (open) exploreVolumeMenusRef.current.add(id);
    else exploreVolumeMenusRef.current.delete(id);
    setAnyExploreVolumeMenuOpen(exploreVolumeMenusRef.current.size > 0);
  }, []);

  const assignExplorePopupZ = useCallback((id: string) => {
    exploreZCounterRef.current += 1;
    const rank = exploreZCounterRef.current;
    setExploreZOrder((prev) => ({ ...prev, [id]: rank }));
  }, []);

  const bringExplorePopupToFront = useCallback((id: string) => {
    assignExplorePopupZ(id);
  }, [assignExplorePopupZ]);

  const closeExplorePopup = useCallback((id: string) => {
    explorePauseMapRef.current.delete(id);
    exploreVolumeMenusRef.current.delete(id);
    setAnyExploreVolumeMenuOpen(exploreVolumeMenusRef.current.size > 0);
    setExploreZOrder((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setExplorePopups((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const openExplorePlayer = useCallback((v: ListedChannelVideo) => {
    pauseAllExplorePopups();
    const vod: ExplorePopupVod = {
      url: buildVodUrl(v),
      title: v.title || 'Untitled',
      platform: v.platform,
      durationSec: v.duration ? Math.max(2, Math.floor(v.duration)) : 7200,
      platformListIndex: v.platformListIndex,
    };
    const id = crypto.randomUUID();
    assignExplorePopupZ(id);
    setExplorePopups((prev) => {
      const next = [...prev, { id, vod, layoutIndex: prev.length }];
      if (next.length > MAX_EXPLORE_POPUPS) {
        const dropped = next.slice(0, next.length - MAX_EXPLORE_POPUPS);
        dropped.forEach((entry) => {
          explorePauseMapRef.current.delete(entry.id);
          exploreVolumeMenusRef.current.delete(entry.id);
        });
        setExploreZOrder((zPrev) => {
          const zNext = { ...zPrev };
          for (const entry of dropped) delete zNext[entry.id];
          return zNext;
        });
        return next.slice(-MAX_EXPLORE_POPUPS);
      }
      return next;
    });
  }, [pauseAllExplorePopups, assignExplorePopupZ]);

  const layoutBoundsInput = useCallback((): LayoutPanelBoundsInput => {
    const aside = previewOpen || channelVodPanelOpen;
    return {
      previewOpen,
      urlPanelAside: aside,
      preview: { w: previewPanelWidthRef.current, h: 0 },
      urlAside: urlAsidePanelSizeRef.current,
      main: mainPanelSizeRef.current,
    };
  }, [previewOpen, channelVodPanelOpen]);

  const handlePreviewLoadedMetadata = useCallback(() => {
    const video = previewVideoRef.current;
    if (!video?.videoWidth || !video?.videoHeight) return;
    const aspect = video.videoWidth / video.videoHeight;
    previewVideoAspectRef.current = aspect;
    setPreviewVideoAspect(aspect);
    const layout = layoutBoundsInput();
    const clampedW = clampPreviewPanelWidth(
      previewPanelWidthRef.current,
      previewChromeHRef.current,
      aspect,
      { ...layout, previewOpen: true },
    );
    previewPanelWidthRef.current = clampedW;
    setPreviewPanelWidth(clampedW);
    if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, clampedW);
  }, [layoutBoundsInput]);

  const applyLayoutPanelClamps = useCallback(() => {
    const layout = layoutBoundsInput();
    const clamped = clampAllLayoutPanels(layout);
    if (layout.previewOpen) {
      const w = clampPreviewPanelWidth(
        clamped.preview.w,
        previewChromeHRef.current,
        previewVideoAspectRef.current,
        layout,
      );
      previewPanelWidthRef.current = w;
      setPreviewPanelWidth(w);
      if (previewPanelRef.current) applyPanelWidth(previewPanelRef.current, w);
    }
    if (layout.urlPanelAside) {
      urlAsidePanelSizeRef.current = clamped.urlAside;
      setUrlAsidePanelSize(clamped.urlAside);
      if (urlAsidePanelRef.current) applyPanelSize(urlAsidePanelRef.current, clamped.urlAside);
    }
    mainPanelSizeRef.current = clamped.main;
    setMainPanelSize(clamped.main);
    if (mainPanelRef.current) applyPanelSize(mainPanelRef.current, clamped.main);
  }, [layoutBoundsInput]);

  useEffect(() => {
    applyLayoutPanelClamps();
    const onResize = () => applyLayoutPanelClamps();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [applyLayoutPanelClamps, previewOpen, channelVodPanelOpen]);

  const onPreviewPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    startPanelWidthResize(e, edge, previewPanelWidthRef, setPreviewPanelWidth, {
      panelEl: previewPanelRef.current,
      aspect: previewVideoAspectRef.current,
      clampWidth: (w) => clampPreviewPanelWidth(
        w,
        previewChromeHRef.current,
        previewVideoAspectRef.current,
        layoutBoundsInput(),
      ),
    });
  }, [layoutBoundsInput]);

  const onUrlAsidePanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const layout = layoutBoundsInput();
    startPanelResizeDrag(e, edge, urlAsidePanelSizeRef, setUrlAsidePanelSize, {
      panelEl: urlAsidePanelRef.current,
      maxW: layoutMaxPanelWidth('urlAside', layout),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => clampPanelSizeForLayout('urlAside', s, layoutBoundsInput()),
    });
  }, [layoutBoundsInput]);

  const onMainPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const layout = layoutBoundsInput();
    startPanelResizeDrag(e, edge, mainPanelSizeRef, setMainPanelSize, {
      panelEl: mainPanelRef.current,
      maxW: layoutMaxPanelWidth('main', layout),
      maxH: layoutMaxPanelHeight(),
      clampSize: (s) => clampPanelSizeForLayout('main', s, layoutBoundsInput()),
    });
  }, [layoutBoundsInput]);

  useEffect(() => {
    if (!previewOpen || previewFullscreen || !previewPanelRef.current || !previewContainerRef.current) return;
    const chromeH = previewPanelRef.current.offsetHeight - previewContainerRef.current.offsetHeight;
    if (chromeH > 0) {
      previewChromeHRef.current = chromeH;
    }
  }, [previewOpen, previewFullscreen, previewPanelWidth, previewVideoAspect, previewVideoReady]);

  // ── Fetch video info ──

  const fetchVideoInfo = useCallback(async (videoUrl: string) => {
    const trimmed = videoUrl.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    try {
      const infoPath = isClipUrl(trimmed) ? '/api/info/clip' : '/api/info/video';
      const info = await apiGet<VideoInfo>(`${infoPath}?id=${encodeURIComponent(trimmed)}`);
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

  const promptStartDownload = useCallback(() => {
    if (!videoInfo) return;
    const clipDownload = isClipUrl(url.trim());
    if (!clipDownload && trimEndSec <= trimStartSec) {
      setError('Set a valid trim range before downloading.');
      return;
    }
    setDownloadConfirmOpen(true);
  }, [videoInfo, url, trimStartSec, trimEndSec]);

  // ── Refresh downloads ──

  const refreshDownloads = useCallback(async () => {
    try {
      const data = await apiGet<DownloadsResponse>('/api/downloads');
      setActiveDownloads(data.active || []);
      setHistoryDownloads(data.history || []);
    } catch {}
  }, []);

  const executeStartDownload = useCallback(async () => {
    setDownloadConfirmOpen(false);
    if (!videoInfo) return;
    setError(null);
    if (!(await ensureDownloadFolder())) {
      setError('Choose a download folder to continue.');
      return;
    }
    const clipDownload = isClipUrl(url.trim());
    if (!clipDownload && trimEndSec <= trimStartSec) {
      setError('Set a valid trim range before downloading.');
      return;
    }
    try {
      const endpoint = clipDownload ? '/api/download/clip' : '/api/download/video';
      const body = clipDownload
        ? { url: url.trim(), quality: quality || undefined }
        : {
            url: url.trim(),
            quality: quality || undefined,
            crop_start: trimStartSec,
            crop_end: trimEndSec,
          };
      await apiPost<{ download_id: string; status: string }>(endpoint, body);
      setTab('queue');
      refreshDownloads();
    } catch (err: any) {
      setError(err.message);
    }
  }, [videoInfo, url, quality, trimStartSec, trimEndSec, ensureDownloadFolder, refreshDownloads]);

  const downloadConfirmCopy = useMemo(() => {
    const clipDownload = isClipUrl(url.trim());
    const title = videoInfo?.title || 'Untitled';
    if (clipDownload) {
      const dur = videoInfo?.duration
        ? Math.floor(videoInfo.duration)
        : Math.max(1, trimEndSec - trimStartSec);
      const tag = formatClipDurationTag(dur);
      const human = formatClipDurationHuman(dur);
      return {
        title: 'Download clip?',
        message: `Download "${title}" (${human})?`,
        detail: `File will include clip_${tag} in the name.`,
      };
    }
    return {
      title: 'Download trim?',
      message: `Download "${title}" from ${formatHmsFull(trimStartSec)} to ${formatHmsFull(trimEndSec)}?`,
      detail: `Trim length: ${formatHmsFull(trimEndSec - trimStartSec)}`,
    };
  }, [url, videoInfo, trimStartSec, trimEndSec]);

  // ── Cancel download ──

  const handleCancel = useCallback(async (id: string) => {
    try {
      await apiPost(`/api/download/${id}/cancel`, {});
    } catch (err: any) {
      setError(err.message || 'Failed to cancel download');
    }
    refreshDownloads();
  }, [refreshDownloads]);

  const handleDeleteHistory = useCallback(async (id: string) => {
    try {
      await apiDelete(`/api/download/${id}`);
      setHistoryDownloads((prev) => prev.filter((d) => d.download_id !== id));
    } catch (err: any) {
      setError(err.message || 'Failed to remove from history');
      refreshDownloads();
    }
  }, [refreshDownloads]);

  // Poll while any download is active (any tab)
  useEffect(() => {
    if (activeDownloads.length === 0) return;
    refreshDownloads();
    const id = setInterval(refreshDownloads, 1000);
    return () => clearInterval(id);
  }, [activeDownloads.length, refreshDownloads]);

  // Refresh queue when opening the tab
  useEffect(() => {
    if (tab !== 'queue') return;
    refreshDownloads();
  }, [tab, refreshDownloads]);

  // ── Channel browsing (localStorage) ──

  type ChannelVodsResponse = {
    videos: ChannelVideo[];
    channel: string;
    platforms: string[];
    content?: 'vods';
    days: number;
    per_platform_errors?: Record<string, string>;
  };

  type ChannelClipsResponse = {
    clips: ChannelVideo[];
    channel: string;
    platforms: string[];
    content?: 'clips';
    per_platform_errors?: Record<string, string>;
  };

  useEffect(() => {
    persistChannels(savedChannels);
  }, [savedChannels]);

  const updateChannel = useCallback((id: string, patch: Partial<SavedChannel>) => {
    setSavedChannels((prev) => prev.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }, []);

  const refreshChannel = useCallback(async (
    channelId: string,
    channelOverride?: SavedChannel,
    contentMode?: 'vods' | 'clips',
    opts?: { incremental?: boolean },
  ) => {
    const ch = channelOverride ?? savedChannels.find((c) => c.id === channelId);
    if (!ch) return;
    const mode = contentMode ?? channelContentFilter;
    const incremental = opts?.incremental ?? false;

    if (!incremental) {
      updateChannel(channelId, { loading: true });
      setKickVisibleLimit(CHANNEL_INITIAL_VISIBLE);
      setTwitchVisibleLimit(CHANNEL_INITIAL_VISIBLE);
    }
    if (!incremental) setChannelsError(null);

    const errs: Record<string, string> = { ...ch.errors };
    const incoming: ChannelVideo[] = [];

    try {
      if (mode === 'clips') {
        const fetchClips = async (platform: 'Kick' | 'Twitch', slug: string) => {
          const qs = `url=${encodeURIComponent(slug)}&platforms=${encodeURIComponent(platform)}&limit=10`;
          try {
            const data = await apiGet<ChannelClipsResponse>(`/api/channel/clips?${qs}`);
            if (data.content && data.content !== 'clips') {
              errs[platform] = 'Clips API unavailable — restart API (npm run dev:api)';
              return;
            }
            incoming.push(...(data.clips ?? []).map(mapApiChannelItem));
            const pe = data.per_platform_errors?.[platform];
            if (pe) errs[platform] = pe;
          } catch (err: any) {
            errs[platform] = err.message || `Failed to fetch ${platform} clips`;
          }
        };
        await Promise.all([
          fetchClips('Kick', ch.kickSlug),
          fetchClips('Twitch', ch.twitchSlug),
        ]);
        const clipVideos = incoming
          .filter(isLikelyClip)
          .sort((a, b) => (Number(b.views) || 0) - (Number(a.views) || 0));
        updateChannel(channelId, {
          clipVideos,
          errors: errs,
          loading: false,
          updatedAt: new Date().toISOString(),
        });
      } else {
        const limit = incremental ? CHANNEL_INCREMENTAL_LIMIT : CHANNEL_FETCH_LIMIT;
        const fetchVods = async (platform: 'Kick' | 'Twitch', slug: string) => {
          const qs = `url=${encodeURIComponent(slug)}&limit=${limit}&days=14&platforms=${encodeURIComponent(platform)}`;
          try {
            const data = await apiGet<ChannelVodsResponse>(`/api/channel/videos?${qs}`);
            incoming.push(...(data.videos ?? []).map(mapApiChannelItem));
            const pe = data.per_platform_errors?.[platform];
            if (pe) errs[platform] = pe;
          } catch (err: any) {
            errs[platform] = err.message || `Failed to fetch ${platform} VODs`;
          }
        };
        await Promise.all([
          fetchVods('Kick', ch.kickSlug),
          fetchVods('Twitch', ch.twitchSlug),
        ]);
        const vodVideos = mergeVodLists(ch.vodVideos ?? [], incoming);
        updateChannel(channelId, {
          vodVideos,
          errors: errs,
          loading: false,
          updatedAt: new Date().toISOString(),
        });
      }

      const errKeys = Object.keys(errs).filter((k) => errs[k]);
      if (errKeys.length && !incremental) {
        const hasItems = mode === 'clips'
          ? incoming.length > 0
          : (ch.vodVideos?.length ?? 0) > 0 || incoming.length > 0;
        setChannelsError(
          hasItems
            ? `Partial results — ${errKeys.map((k) => `${k}: ${errs[k]}`).join(' | ')}`
            : errKeys.map((k) => `${k}: ${errs[k]}`).join(' | '),
        );
      }
    } finally {
      if (!incremental) {
        updateChannel(channelId, { loading: false });
      }
    }
  }, [savedChannels, updateChannel, channelContentFilter]);

  const refreshChannelRef = useRef(refreshChannel);
  refreshChannelRef.current = refreshChannel;

  // On page load: cheap incremental VOD sync for every saved channel (merge new ids only).
  const incrementalSyncDoneRef = useRef(false);
  useEffect(() => {
    if (incrementalSyncDoneRef.current) return;
    incrementalSyncDoneRef.current = true;
    const channels = loadSavedChannels();
    channels.forEach((c) => {
      void refreshChannelRef.current(c.id, c, 'vods', { incremental: true });
    });
  }, []);

  // Fetch clips when user switches to Only clips and cache is empty.
  useEffect(() => {
    if (channelContentFilter !== 'clips' || !selectedChannelId) return;
    const ch = savedChannels.find((c) => c.id === selectedChannelId);
    if (!ch || (ch.clipVideos?.length ?? 0) > 0) return;
    void refreshChannel(selectedChannelId, ch, 'clips');
  }, [channelContentFilter, selectedChannelId, savedChannels, refreshChannel]);

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
      vodVideos: [],
      clipVideos: [],
      errors: {},
      updatedAt: '',
    };
    setSavedChannels((prev) => [...prev, entry]);
    setSelectedChannelId(id);
    setAddChannelInput('');
    await refreshChannel(id, entry, 'vods');
    if (channelContentFilter === 'clips') {
      await refreshChannel(id, entry, 'clips');
    }
  }, [addChannelInput, savedChannels.length, refreshChannel, channelContentFilter]);

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

  const commitEditPlatformSlug = useCallback(async () => {
    if (!editingSlug) return;
    const slug = editingSlugValue.trim();
    if (!slug) return;
    const ch = savedChannels.find((c) => c.id === editingSlug.channelId);
    if (!ch) return;

    const prevSlug = editingSlug.platform === 'Kick' ? ch.kickSlug : ch.twitchSlug;
    const channelId = editingSlug.channelId;
    const updated: SavedChannel = editingSlug.platform === 'Kick'
      ? { ...ch, kickSlug: slug }
      : { ...ch, twitchSlug: slug };

    setEditingSlug(null);
    setEditingSlugValue('');

    if (slug === prevSlug) return;

    if (editingSlug.platform === 'Kick') {
      updateChannel(channelId, { kickSlug: slug });
    } else {
      updateChannel(channelId, { twitchSlug: slug });
    }
    await refreshChannel(channelId, updated);
  }, [editingSlug, editingSlugValue, savedChannels, updateChannel, refreshChannel]);

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

  // ── Fill VOD from channel ──
  const selectVod = useCallback((vodUrl: string) => {
    setUrl(vodUrl);
    setChannelVodPanelOpen(true);
    setUrlTabBarHidden(true);
    void fetchVideoInfo(vodUrl);
  }, [fetchVideoInfo]);

  const currentIsClip = isClipUrl(url);

  // ── Size estimate ──
  const clipSec = currentIsClip && videoInfo?.duration
    ? Math.max(1, Math.floor(videoInfo.duration))
    : Math.max(0, trimEndSec - trimStartSec);

  const rates: Record<string, number> = {
    source: 180, '1080p60': 180, '1080p': 120, '720p60': 70,
    '720p': 70, '480p': 35, '360p': 18,
  };
  const mbPerMin = rates[quality] || 70;
  const estSize = (clipSec / 60) * mbPerMin;

  const sourceQualityLabel = useMemo(
    () => sourceQualityOptionLabel(maxStreamQualityLabel(videoInfo?.qualities ?? [], previewLevels)),
    [videoInfo?.qualities, previewLevels],
  );

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
            placeholder={urlFetched ? 'VOD or clip link' : 'PASTE VOD OR CLIP LINK...'}
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
            className={`w-full bg-zinc-800 text-white font-black uppercase py-3 flex items-center justify-center gap-2 transition-all duration-300 disabled:opacity-50 disabled:cursor-default border-2 border-zinc-700 ${extractBtnHoverClass}`}
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
                    <option value="source">{sourceQualityLabel}</option>
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

          {!currentIsClip && (
          <div className="flex flex-col gap-2.5 shrink-0 py-0.5">
            <div className="flex justify-between items-center">
              <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500">Trim</span>
              <span className="text-xs font-mono text-zinc-400">{formatHmsFull(trimEndSec - trimStartSec)}</span>
            </div>
            <div className="flex justify-between text-xs font-mono text-white px-0.5">
              <EditableHmsTime
                valueSec={trimStartSec}
                minSec={0}
                maxSec={Math.max(0, Math.min(trimEndSec - 1, vodDurationSec - 1))}
                onChange={(sec) => handleUrlTrimSlider('in', sec)}
              />
              <EditableHmsTime
                valueSec={trimEndSec}
                minSec={Math.min(vodDurationSec, trimStartSec + 1)}
                maxSec={vodDurationSec}
                onChange={(sec) => handleUrlTrimSlider('out', sec)}
                className="text-zinc-500"
              />
            </div>
            <input type="range" min={0} max={vodDurationSec} step={1} value={Math.min(trimStartSec, trimEndSec - 1)}
              onPointerDown={(e) => {
                trimDragOriginRef.current = trimStartSec;
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
              }}
              onPointerMove={(e) => {
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
              }}
              onInput={(e) => {
                handleUrlTrimSlider(
                  'in',
                  Number((e.target as HTMLInputElement).value),
                  urlTrimPointerRef.current,
                );
              }}
              onPointerUp={() => setNeedleGlance(null)}
              onPointerCancel={() => setNeedleGlance(null)}
              className="url-trim-range w-full accent-zinc-400" />
            <input type="range" min={0} max={vodDurationSec} step={1} value={Math.max(trimEndSec, trimStartSec + 1)}
              onPointerDown={(e) => {
                trimDragOriginRef.current = trimEndSec;
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
              }}
              onPointerMove={(e) => {
                urlTrimPointerRef.current = { x: e.clientX, y: e.clientY };
              }}
              onInput={(e) => {
                handleUrlTrimSlider(
                  'out',
                  Number((e.target as HTMLInputElement).value),
                  urlTrimPointerRef.current,
                );
              }}
              onPointerUp={() => setNeedleGlance(null)}
              onPointerCancel={() => setNeedleGlance(null)}
              className="url-trim-range w-full accent-zinc-400" />
            <button type="button" onClick={openPreview}
              disabled={previewVideoLoading || trimEndSec <= trimStartSec}
              className="w-full border border-zinc-700 text-zinc-400 hover:border-white hover:text-white font-mono text-[9px] uppercase font-bold py-1 flex items-center justify-center gap-1 disabled:opacity-40">
              {previewVideoLoading ? <Loader2 size={11} className="animate-spin" /> : <Eye size={11} />}
              Preview
            </button>
          </div>
          )}

          {currentIsClip && (
            <button type="button" onClick={openPreview}
              disabled={previewVideoLoading || vodDurationSec <= 0}
              className="w-full border border-zinc-700 text-zinc-400 hover:border-white hover:text-white font-mono text-[9px] uppercase font-bold py-1 flex items-center justify-center gap-1 disabled:opacity-40 shrink-0">
              {previewVideoLoading ? <Loader2 size={11} className="animate-spin" /> : <Eye size={11} />}
              Preview clip
            </button>
          )}

          <button
            onClick={promptStartDownload}
            className={`w-full mt-auto shrink-0 border-2 border-white bg-black py-2 flex items-center justify-center gap-2 text-xs font-black uppercase tracking-widest transition-[transform,box-shadow,background-color,color] duration-150 hover:bg-white hover:text-black ${
              urlPlatform === 'kick'
                ? 'shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18] hover:translate-x-0.5 hover:translate-y-0.5'
                : urlPlatform === 'twitch'
                  ? 'shadow-[3px_3px_0px_0px_#9146FF] hover:shadow-[2px_2px_0px_0px_#9146FF] hover:translate-x-0.5 hover:translate-y-0.5'
                  : 'shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18] hover:translate-x-0.5 hover:translate-y-0.5'
            }`}
          >
            <Download size={16} strokeWidth={3} />
            <span>{currentIsClip ? 'Clip rip it!' : 'VOD rip it!'}</span>
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

  const previewClipPct = vodDurationSec > 0
    ? {
        start: (trimStartSec / vodDurationSec) * 100,
        end: (trimEndSec / vodDurationSec) * 100,
        play: (previewCurrentTime / vodDurationSec) * 100,
      }
    : { start: 0, end: 100, play: 0 };

  const previewTimelineUi = (
    <div className="flex flex-col gap-0.5 w-full">
      {vodDurationSec > 0 && (
        <div className="flex items-center gap-2">
          <span className={`text-[8px] font-mono uppercase w-11 shrink-0 tracking-wider ${
            previewFullscreen ? 'text-zinc-400' : 'text-zinc-600'
          }`}>
            Clip
          </span>
          <div
            ref={previewNeedleRailRef}
            className={`preview-needle-rail relative flex-1 h-3 ${
              previewFullscreen ? 'bg-white/10' : 'bg-zinc-800/80'
            }`}
            title="Drag needles to set download range"
          >
            <div
              className="preview-needle-region absolute top-1/2 -translate-y-1/2 h-1 pointer-events-none"
              style={{
                left: `${previewClipPct.start}%`,
                width: `${Math.max(0, previewClipPct.end - previewClipPct.start)}%`,
              }}
            />
            <div
              className="preview-needle-playhead absolute top-0 bottom-0 w-px bg-white/50 -translate-x-1/2 pointer-events-none z-[1]"
              style={{ left: `${previewClipPct.play}%` }}
            />
            <div
              role="slider"
              aria-label="Clip in"
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={trimStartSec}
              className="preview-needle preview-needle-in absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${previewClipPct.start}%` }}
              onPointerDown={(e) => beginPreviewNeedleDrag(e, 'in')}
            />
            <div
              role="slider"
              aria-label="Clip out"
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={trimEndSec}
              className="preview-needle preview-needle-out absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${previewClipPct.end}%` }}
              onPointerDown={(e) => beginPreviewNeedleDrag(e, 'out')}
            />
          </div>
          <span className={`text-[8px] font-mono w-11 shrink-0 text-right ${
            previewFullscreen ? 'text-zinc-300/90' : 'text-zinc-500'
          }`}>
            {formatHmsFull(trimEndSec - trimStartSec)}
          </span>
        </div>
      )}
      <div className="flex items-center gap-2">
        <span className={`text-[9px] font-mono w-11 shrink-0 ${previewFullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}>
          {formatHmsFull(previewCurrentTime)}
        </span>
        <input
          type="range"
          min={0}
          max={vodDurationSec}
          step={0.25}
          value={Math.min(Math.max(previewCurrentTime, 0), vodDurationSec)}
          disabled={!previewVideoReady || vodDurationSec <= 0}
          onChange={(e) => seekPreviewVideo(parseFloat(e.target.value))}
          className="flex-1 accent-white disabled:opacity-40"
        />
        <span className={`text-[9px] font-mono w-11 shrink-0 text-right ${previewFullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}>
          {formatHmsFull(vodDurationSec)}
        </span>
      </div>
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
        <PreviewQualityMenu
          levels={previewLevels}
          currentLevel={previewQualityLevel}
          menuOpen={previewQualityMenuOpen}
          setMenuOpen={setPreviewQualityMenuOpen}
          onSelect={applyPreviewQuality}
          disabled={!previewVideoReady}
          buttonClassName={previewCtrlBtn(previewFullscreen)}
          onMenuOpen={() => setPreviewVolumeMenuOpen(false)}
          popoverClassName={previewFullscreen
            ? 'border border-white/20 bg-black/85 backdrop-blur-sm'
            : 'border-2 border-zinc-600 bg-zinc-950'}
        />
      </div>
      <div className="flex items-center gap-1.5 ml-auto">
        <button
          type="button"
          onClick={promptStartDownload}
          disabled={!previewVideoReady || !videoInfo || (!currentIsClip && trimEndSec <= trimStartSec)}
          className={previewCtrlBtn(previewFullscreen, true)}
          title={currentIsClip ? 'Download clip' : 'Download selected trim'}
        >
          <Scissors size={18} />
        </button>
        {!opts.fsCornerExit && (
          <button type="button" onClick={() => void togglePreviewFullscreen()}
            disabled={!previewVideoReady}
            className={previewCtrlBtn(previewFullscreen, true)}
            title="Fullscreen">
            <Maximize2 size={18} />
          </button>
        )}
      </div>
    </div>
  );

  return (
    <div
      className="h-screen max-h-screen min-h-0 flex justify-center items-center overflow-hidden p-4 selection:bg-white selection:text-black bg-[#09090b]"
      style={{
        backgroundImage: 'radial-gradient(#27272a 1px, transparent 1px)',
        backgroundSize: '24px 24px',
      }}
    >
      <div className={`flex items-start max-w-full min-w-0 ${
        triplePanelLayout
          ? 'w-full gap-3 justify-center'
          : splitLayout
            ? 'w-full gap-6 justify-center'
            : 'w-full max-w-md justify-center gap-6'
      }`}>
      {previewOpen && (
        <div
          ref={previewPanelRef}
          className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white p-4 flex flex-col gap-3 min-h-0 min-w-0 ${platformCardShadow(activePlatform, true)}`}
          style={{ width: previewPanelWidth }}
        >
          <div className="flex items-center justify-end shrink-0">
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
              className="relative w-full shrink-0 bg-black border-2 border-zinc-700 overflow-hidden outline-none focus:ring-2 focus:ring-white/30"
              style={previewFullscreen ? undefined : { aspectRatio: previewVideoAspect }}
            >
              <video
                ref={previewVideoRef}
                className="w-full h-full object-contain"
                muted={previewMuted}
                playsInline
                onLoadedMetadata={handlePreviewLoadedMetadata}
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
            <PanelResizeHandles onPointerDown={onPreviewPanelResize} insetPx={panelResizeHandleInset(true)} />
          )}
        </div>
      )}
      {(showUrlInSidebar || showUrlInPreviewMiddle) && (
        <div
          ref={urlAsidePanelRef}
          className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white p-4 flex flex-col gap-2 min-h-0 ${platformCardShadow(activePlatform, true)}`}
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
          <PanelResizeHandles onPointerDown={onUrlAsidePanelResize} insetPx={panelResizeHandleInset(true)} />
        </div>
      )}
      <div
        ref={mainPanelRef}
        className={`group relative shrink-0 overflow-visible bg-zinc-950 border-2 border-white flex flex-col min-h-0 ${
          triplePanelLayout ? 'p-4 gap-3' : urlMainCompact ? 'p-4 gap-2' : 'p-6 gap-4'
        } ${platformCardShadow(activePlatform)}`}
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
                      onClick={(e) => { e.stopPropagation(); refreshChannel(ch.id, undefined, channelContentFilter); }}
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
                <div className="flex items-center gap-2 font-mono text-[10px] uppercase shrink-0">
                  <span className="text-zinc-500">Show:</span>
                  <button
                    type="button"
                    onClick={() => {
                      if (channelContentFilter !== 'vods') {
                        setChannelContentFilter('vods');
                        if (selectedChannelId) void refreshChannel(selectedChannelId, undefined, 'vods');
                      }
                    }}
                    className={`px-2 py-0.5 border font-bold ${
                      channelContentFilter === 'vods'
                        ? 'border-white text-white bg-zinc-900'
                        : 'border-zinc-700 text-zinc-500 hover:text-white'
                    }`}
                  >
                    VODs
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (channelContentFilter !== 'clips') {
                        setChannelContentFilter('clips');
                        if (selectedChannelId) void refreshChannel(selectedChannelId, undefined, 'clips');
                      }
                    }}
                    className={`px-2 py-0.5 border font-bold ${
                      channelContentFilter === 'clips'
                        ? 'border-white text-white bg-zinc-900'
                        : 'border-zinc-700 text-zinc-500 hover:text-white'
                    }`}
                  >
                    Only clips
                  </button>
                </div>
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
                  <p className="text-center text-zinc-600 font-mono text-[10px] py-4">
                    {channelContentFilter === 'clips' ? 'No clips' : 'No VODs'}
                  </p>
                ) : (
                  <div className="flex flex-col gap-1">
                    {visibleChannelVideos.map((v, i) => {
                      const fullUrl = buildVodUrl(v);
                      const subline = channelVodSubline(v);
                      const durSec = channelVideoDurationSec(v);
                      const isClipItem = v.content_kind === 'clip' || channelContentFilter === 'clips';
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
                                {durSec != null ? <span className="text-zinc-500 ml-1">{fmtShort(durSec)}</span> : null}
                              </span>
                            </span>
                            {subline && (
                              <span className="text-[9px] text-zinc-400 block truncate">
                                {subline}
                              </span>
                            )}
                          </div>
                          {!isClipItem && (
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
                          )}
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

            <div className="flex flex-col gap-2 max-h-[280px] overflow-y-auto pr-1 custom-scrollbar">
              {activeDownloads.length === 0 ? (
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
                        <PlatformVodIcon platform={dl.platform} className="w-4 h-4" />
                        <span className="text-xs font-mono text-zinc-300 truncate">
                          {dl.title || dl.url}
                        </span>
                      </div>
                      <span className="text-[10px] font-mono shrink-0" style={{ color }}>
                        {dl.progress > 0 ? `${dl.progress}%` : dl.status}
                      </span>
                    </div>
                    <div className="w-full h-2 bg-zinc-800 border border-zinc-700">
                      <div className="h-full bg-gradient-to-r from-[#53fc18] to-[#9146FF] transition-all duration-300"
                        style={{ width: `${Math.max(dl.progress, dl.status === 'Starting...' ? 2 : 0)}%` }} />
                    </div>
                    <div className="flex justify-between items-center text-[10px] text-zinc-500 font-mono gap-2">
                      <span className="truncate">{basename(dl.output_file)}</span>
                      <div className="flex items-center gap-2 shrink-0">
                        {dl.output_file && (
                          <button
                            type="button"
                            onClick={() => openFolder(dl.output_file)}
                            className="text-zinc-400 hover:text-white flex items-center gap-1"
                            title="Show in folder"
                          >
                            <FolderOpen size={12} /> Folder
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => handleCancel(dl.download_id)}
                          className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                        >
                          <StopCircle size={12} /> Cancel
                        </button>
                      </div>
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
                ) : historyDownloads.map((dl) => (
                    <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-950 p-2 flex flex-col gap-1.5">
                      <div className="flex justify-between items-center gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <PlatformVodIcon platform={dl.platform} className="w-4 h-4" />
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
                        <div className="flex items-center gap-2 shrink-0">
                          {dl.output_file && (
                            <button
                              type="button"
                              onClick={() => openFolder(dl.output_file)}
                              className="text-zinc-400 hover:text-white flex items-center gap-1"
                              title="Show in folder"
                            >
                              <FolderOpen size={12} /> Folder
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => handleDeleteHistory(dl.download_id)}
                            className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                            title="Remove from history"
                          >
                            <Trash2 size={12} /> Delete
                          </button>
                        </div>
                      </div>
                      {dl.error && <span className="text-[10px] text-red-400 font-mono">{dl.error}</span>}
                    </div>
                ))}
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

        <PanelResizeHandles onPointerDown={onMainPanelResize} insetPx={panelResizeHandleInset(false)} />
      </div>
      </div>
      </div>

      {/* Background */}
      <div className="fixed top-10 left-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        KICK
      </div>
      {explorePopups.length > 0 && createPortal(
        <>
          {explorePopups.map((entry) => (
            <ChannelExplorePopup
              key={entry.id}
              id={entry.id}
              vod={entry.vod}
              zIndex={EXPLORE_POPUP_Z + (exploreZOrder[entry.id] ?? 0)}
              stackIndex={entry.layoutIndex}
              volumeMenuCloseTick={exploreVolumeMenuCloseTick}
              onClose={() => closeExplorePopup(entry.id)}
              onCarryToUrl={selectVod}
              onRegisterPause={registerExplorePause}
              onUnregisterPause={unregisterExplorePause}
              onVolumeMenuOpen={handleExploreVolumeMenuOpen}
              onBringToFront={() => bringExplorePopupToFront(entry.id)}
            />
          ))}
        </>,
        document.getElementById('explore-portal') ?? document.body,
      )}
      <div className="fixed bottom-10 right-10 text-zinc-800 font-black text-9xl opacity-10 pointer-events-none select-none z-[-1] blur-sm">
        TWITCH
      </div>

      <NeedleGlancePopup glance={needleGlance} vodDurationSec={vodDurationSec} />
      <DownloadConfirmDialog
        open={downloadConfirmOpen}
        title={downloadConfirmCopy.title}
        message={downloadConfirmCopy.message}
        detail={downloadConfirmCopy.detail}
        onConfirm={() => void executeStartDownload()}
        onCancel={() => setDownloadConfirmOpen(false)}
      />
    </div>
  );
}
