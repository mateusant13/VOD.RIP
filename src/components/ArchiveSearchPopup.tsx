/**
 * Archive search popup — the "local Google" UI.
 *
 * Searches the local archive (transcripts + chat) via GET /api/archive/search,
 * opens the hit in the existing explore-player flow at the hit offset
 * (App passes the vod with initialTimeSec), and shows the nearby chat window
 * (±30s) with a marker line at the hit moment.
 *
 * Pure text helpers (offset format, highlight spans, chat grouping) live in
 * archiveSearchUtils.ts and are covered by vitest — no network in there.
 */
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, FileText, Loader2, MessageSquare, RefreshCw, Search, X } from 'lucide-react';
import { apiGet } from '../hooks/useApiClient';
import {
  EXPLORE_PANEL_BOX_MIN_H,
  EXPLORE_PANEL_BOX_MIN_W,
  VIEWPORT_EDGE_LOCK,
  PanelResizeHandles,
  applyExplorePopupWindowPosition,
  panelResizeHandleInset,
  startExplorePanelBoxResize,
  startFloatingPanelDrag,
  type PanelPos,
  type ResizeEdge,
} from '../explorePopupUtils';
import {
  ARCHIVE_KIND_LABELS,
  ARCHIVE_KINDS,
  ARCHIVE_LANG_LABELS,
  ARCHIVE_LANGS,
  ARCHIVE_PLATFORMS,
  ARCHIVE_SOURCES,
  ARCHIVE_SOURCE_LABELS,
  buildArchiveVodUrl,
  buildSearchUrl,
  formatArchiveOffset,
  groupChatWindow,
  highlightQuerySpans,
  isValidDateParam,
  kindLabel,
  snippetAroundMatch,
  type ArchiveChatMessage,
  type ArchiveEnrichEntry,
  type ArchiveSearchHit,
  type ArchiveSearchResponse,
  type ArchiveSource,
  type ArchiveVideoRow,
} from '../archiveSearchUtils';
import { deriveChannelDisplayName, displayTitle } from '../channelUtils';
import { resolveChatColor } from '../chatColors';
import type { SavedChannel } from '../types';
import PlatformVodIcon from './PlatformVodIcon';

interface ArchiveSearchPopupProps {
  zIndex: number;
  onClose: () => void;
  /** Open the hit in the explore-player flow (App owns the popup stack). */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => void;
  /** When provided, clicking a hit row seeks instead of opening: the row
   *  click calls onSeekHit(hit) and a small per-row 'open' affordance still
   *  calls onOpenHit. Absent → current behavior (row click opens). */
  onSeekHit?: (hit: ArchiveSearchHit) => void;
  /** Render as a plain flex container filling its parent — no floating
   *  positioning, no drag/resize chrome, no zIndex (player-embedded use). */
  embedded?: boolean;
  /** Optional per-video scope: search only this archived video; channel + platform are implied. */
  scope?: { videoId: string; title: string };
  /** Optional saved channels (App state) — unioned into the channel dropdown. */
  savedChannels?: SavedChannel[];
  /** Floating-mode seed position (e.g. anchored next to the preview panel);
   *  defaults to the viewport top-right. Ignored in embedded mode. */
  initialPos?: PanelPos;
  /** Optional seed for the channel dropdown (comma-joined slugs, as the
   *  dropdown options carry) — the channel-list row Search action passes it
   *  to scope the popup to one channel. Absent → all channels (unchanged). */
  initialChannel?: string;
}

type SearchStatus = 'idle' | 'loading' | 'done' | 'error';

const POPUP_WIDTH = 460;
/** Floating-mode seed height — the search panel is tall by default. */
function defaultPopupHeight(): number {
  return Math.min(Math.round(window.innerHeight * 0.88), 760);
}
const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_LIMIT = 30;
const REMOTE_LIMIT = 20;
const CHAT_HALF_SEC = 30;
/** Floating-mode seed position — the pre-chrome location (top 80, right 24). */
const SEED_Y = 80;
const SEED_RIGHT = 24;

const platformAccent: Record<string, string> = {
  twitch: 'text-[#9146FF]',
  kick: 'text-[#53fc18]',
  youtube: 'text-[#F03030]',
};

/** PlatformVodIcon expects capitalized platform names; archive rows are lowercase. */
const PLATFORM_ICON_NAME: Record<string, string> = {
  youtube: 'YouTube',
  twitch: 'Twitch',
  kick: 'Kick',
};

function videoTitle(video: ArchiveVideoRow | undefined, hit: ArchiveSearchHit): string {
  // WS-4: prefer the original (non-auto-translated) YouTube title when the
  // API row carries it; displayTitle keeps this in lockstep with the other
  // display paths.
  const t = displayTitle({ title: video?.title, originalTitle: video?.originalTitle ?? hit.originalTitle });
  return t !== 'Untitled' ? t : hit.video_id;
}

export function ArchiveSearchPopup({ zIndex, onClose, onOpenHit, onSeekHit, embedded = false, scope, savedChannels, initialPos, initialChannel }: ArchiveSearchPopupProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const posRef = useRef<PanelPos | null>(null);
  // Seed exactly once from the caller-supplied anchor (else viewport top-right).
  const initialPosRef = useRef<PanelPos | null>(initialPos ?? null);
  const sizeRef = useRef<{ w: number; h: number } | null>(null);
  const [, setPos] = useState<PanelPos | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [inputQuery, setInputQuery] = useState('');
  const [query, setQuery] = useState('');
  // Filters — empty values mean "all".
  const [channelFilter, setChannelFilter] = useState(initialChannel ?? '');
  const [platformFilter, setPlatformFilter] = useState<string[]>([]);
  const [kindFilter, setKindFilter] = useState<string[]>([]);
  const [sourceFilter, setSourceFilter] = useState<ArchiveSource>('both');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  /** True = ignore the stored date range (default). A date pick unchecks it;
   *  re-checking keeps the stored values but search ignores them again. */
  const [everyDay, setEveryDay] = useState(true);
  /** '' | 'pt' | 'en' — transcript language filter ('' = all). */
  const [langFilter, setLangFilter] = useState<'' | 'pt' | 'en'>('');
  const [semanticOn, setSemanticOn] = useState(false);
  const [status, setStatus] = useState<SearchStatus>('idle');
  const [error, setError] = useState<string | null>(null);
  const [hits, setHits] = useState<ArchiveSearchHit[]>([]);
  const [videos, setVideos] = useState<Record<string, ArchiveVideoRow>>({});
  const [selected, setSelected] = useState<{ hit: ArchiveSearchHit; video: ArchiveVideoRow | undefined } | null>(null);
  const [chat, setChat] = useState<ArchiveChatMessage[] | null>(null);
  const [chatStatus, setChatStatus] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [chatError, setChatError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  /** Background indexing work kicked by the backend on this search ([] idle). */
  const [enriching, setEnriching] = useState<ArchiveEnrichEntry[]>([]);
  /** Channel the backend auto-scoped this query to (first token matched a slug). */
  const [channelHint, setChannelHint] = useState<string | null>(null);
  /** User dismissed the hint chip — next request opts out (hint=0). */
  const [hintDisabled, setHintDisabled] = useState(false);
  /** Remote YouTube channel-title search (kind='youtube' hits). */
  const [remoteHits, setRemoteHits] = useState<ArchiveSearchHit[]>([]);
  const [remoteStatus, setRemoteStatus] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const remoteGenRef = useRef(0);
  const mountedRef = useRef(true);
  const searchGenRef = useRef(0);
  const debounceRef = useRef<number | null>(null);
  const chatGenRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
      searchGenRef.current += 1;
      chatGenRef.current += 1;
    };
  }, []);

  /** Floating-mode geometry — position + size are in-memory (no persistence). */
  const seedPos = useCallback((w: number): PanelPos => ({
    x: Math.max(8, window.innerWidth - SEED_RIGHT - w),
    y: SEED_Y,
  }), []);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (embedded || !el) return;
    if (!posRef.current) {
      posRef.current = initialPosRef.current ?? seedPos(sizeRef.current?.w ?? POPUP_WIDTH);
      setPos(posRef.current);
    }
    if (!sizeRef.current) sizeRef.current = { w: POPUP_WIDTH, h: defaultPopupHeight() };
    const sized = sizeRef.current;
    el.style.width = sized ? `${sized.w}px` : `${POPUP_WIDTH}px`;
    el.style.height = sized ? `${sized.h}px` : '';
    el.style.maxHeight = sized ? '' : '75vh';
    applyExplorePopupWindowPosition(el, posRef.current);
  }, [embedded, size, seedPos]);

  // Keep the floating panel on-screen when the viewport shrinks.
  useEffect(() => {
    if (embedded) return;
    const fit = () => {
      const el = containerRef.current;
      if (!el || !posRef.current) return;
      const margin = VIEWPORT_EDGE_LOCK + panelResizeHandleInset(true);
      const p = {
        x: Math.max(margin, Math.min(posRef.current.x, window.innerWidth - margin - el.offsetWidth)),
        y: Math.max(margin, Math.min(posRef.current.y, window.innerHeight - margin - el.offsetHeight)),
      };
      posRef.current = p;
      applyExplorePopupWindowPosition(el, p);
      setPos(p);
    };
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, [embedded]);

  /** Lazy floating-chrome bootstrap: seed pos + adopt the measured size
   *  (the initial render is auto-height) before a drag/resize gesture. */
  const ensureFloatingChrome = useCallback(() => {
    const el = containerRef.current;
    if (!el) return null;
    if (!posRef.current) {
      posRef.current = seedPos(sizeRef.current?.w ?? POPUP_WIDTH);
      setPos(posRef.current);
    }
    if (!sizeRef.current) {
      sizeRef.current = { w: el.offsetWidth || POPUP_WIDTH, h: defaultPopupHeight() };
      setSize(sizeRef.current);
    }
    el.style.maxHeight = '';
    return posRef.current;
  }, [seedPos]);

  const onDragStart = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = containerRef.current;
    if (!el || !ensureFloatingChrome()) return;
    startFloatingPanelDrag(e, posRef, setPos, el);
  }, [ensureFloatingChrome]);

  const onResizeStart = useCallback((e: React.PointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const el = containerRef.current;
    if (!el || !ensureFloatingChrome()) return;
    startExplorePanelBoxResize(
      e,
      edge,
      sizeRef as React.MutableRefObject<{ w: number; h: number }>,
      setSize,
      {
        panelEl: el,
        min: { w: EXPLORE_PANEL_BOX_MIN_W, h: EXPLORE_PANEL_BOX_MIN_H },
        posRef,
        setPos,
      },
    );
  }, [ensureFloatingChrome]);

  // Video title map — fetched once; hits reference it for channel/title.
  useEffect(() => {
    let cancelled = false;
    void apiGet<{ videos: ArchiveVideoRow[] }>('/api/archive/videos')
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, ArchiveVideoRow> = {};
        for (const v of res.videos ?? []) {
          map[`${(v.platform || '').toLowerCase()}:${v.video_id}`] = v;
        }
        setVideos(map);
      })
      .catch(() => { /* search still works with video_id fallbacks */ });
    return () => { cancelled = true; };
  }, []);

  /** Archived channel strings grouped case-insensitively, keeping every
   *  distinct casing as it appears in the DB. The backend channel filter
   *  matches v.channel case-insensitively, so option values carry every
   *  distinct variant. */
  const archivedChannelGroups = useMemo(() => {
    const groups = new Map<string, { variants: Map<string, number> }>();
    for (const v of Object.values(videos)) {
      const c = (v.channel || '').trim();
      if (!c) continue;
      const key = c.toLowerCase();
      const g = groups.get(key) ?? { variants: new Map<string, number>() };
      g.variants.set(c, (g.variants.get(c) ?? 0) + 1);
      groups.set(key, g);
    }
    return groups;
  }, [videos]);

  /**
   * Dropdown options: archived channels (grouped case-insensitively) ∪ saved
   * channels (display names derived from per-platform slugs). A saved
   * channel's slugs win the group: label = display name, value = saved slugs
   * ∪ archived variants (so the backend matches ANY of them). Unclaimed
   * groups get the most-frequent casing as canonical label and every variant
   * in the value. Deduped by lowercased label.
   */
  const channelOptions = useMemo(() => {
    const options = new Map<string, { label: string; value: string }>();
    const claimed = new Set<string>();
    for (const ch of savedChannels ?? []) {
      // Canonical slug order mirrors deriveChannelDisplayName (twitch first).
      const slugs = [ch.twitchSlug, ch.kickSlug, ch.youtubeSlug]
        .map((s) => (s || '').trim())
        .filter(Boolean);
      if (slugs.length === 0) continue;
      const label = deriveChannelDisplayName(ch.kickSlug, ch.twitchSlug, ch.youtubeSlug);
      // Absorb every archived variant whose lower slug matches this channel.
      const variants = new Set<string>();
      for (const key of new Set(slugs.map((s) => s.toLowerCase()))) {
        const g = archivedChannelGroups.get(key);
        if (g) {
          claimed.add(key);
          for (const v of g.variants.keys()) variants.add(v);
        }
      }
      const value = [...new Set([...slugs, ...variants])].join(',');
      options.set(label.toLowerCase(), { label, value });
    }
    for (const [key, g] of archivedChannelGroups) {
      if (claimed.has(key)) continue;
      // Canonical casing = most frequent in the archive (ties → first seen).
      let label = key;
      let best = -1;
      for (const [variant, count] of g.variants) {
        if (count > best) { best = count; label = variant; }
      }
      if (options.has(label.toLowerCase())) continue; // saved channel owns it
      options.set(label.toLowerCase(), { label, value: [...g.variants.keys()].join(',') });
    }
    return [...options.values()].sort((a, b) => a.label.localeCompare(b.label));
  }, [savedChannels, archivedChannelGroups]);

  /** YouTube handle for the current scope (explicit dropdown channel or
   *  backend hint) — null when no remote search applies. A single slug is
   *  sent as-is even when the frontend doesn't know the channel: the backend
   *  resolves it against its own saved_channels and reports an error note
   *  when there is no YouTube handle (the frontend list may lag the backend,
   *  e.g. channels present only via archived rows). */
  const remoteYtHandle = useMemo(() => {
    const scopeValue = channelFilter || (channelHint && !hintDisabled ? channelHint : null);
    if (!scopeValue) return null;
    const slugs = scopeValue
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    if (slugs.length === 0) return null;
    const first = slugs[0].toLowerCase();
    for (const ch of savedChannels ?? []) {
      const chSlugs = [ch.twitchSlug, ch.kickSlug, ch.youtubeSlug]
        .map((s) => (s || '').trim().toLowerCase())
        .filter(Boolean);
      if (chSlugs.some((s) => s === first)) return ch.youtubeSlug?.trim() || null;
    }
    return slugs[0];
  }, [channelFilter, channelHint, hintDisabled, savedChannels]);

  const togglePlatform = useCallback((p: string) => {
    setPlatformFilter((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  }, []);

  const toggleKind = useCallback((k: string) => {
    setKindFilter((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));
  }, []);

  const toggleLang = useCallback((lang: 'pt' | 'en') => {
    setLangFilter((cur) => (cur === lang ? '' : lang));
  }, []);

  /** Distinct transcript languages among current hits (chips show only when ≥2). */
  const langsPresent = useMemo(() => {
    const langs = new Set<string>();
    for (const h of hits) {
      if (h.kind === 'transcript' && h.lang) langs.add(h.lang);
    }
    return langs;
  }, [hits]);

  // Debounced search against the archive FTS index.
  useEffect(() => {
    if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => setQuery(inputQuery.trim()), SEARCH_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
    };
  }, [inputQuery]);

  // A new query (or an explicit dropdown channel) resets the dismissed
  // hint so the backend may hint again. Must run BEFORE the search effect
  // so the same commit's request never sees a stale hintDisabled.
  useEffect(() => {
    setChannelHint(null);
    setHintDisabled(false);
  }, [query, channelFilter]);

  useEffect(() => {
    if (!query) {
      searchGenRef.current += 1;
      setStatus('idle');
      setHits([]);
      setEnriching([]);
      setError(null);
      return;
    }
    const gen = ++searchGenRef.current;
    setStatus('loading');
    setError(null);
    // Date inputs can hold partial/typed garbage; invalid values become unset.
    // everyDay=true ignores the stored range (default; a date pick unchecks it).
    // channel_hint applies only while no explicit dropdown channel is set.
    const url = buildSearchUrl({
      query,
      // The hint is an implicit backend scope — echoing it here as an
      // explicit channel param suppresses the hint and re-fires the search
      // forever. The API applies the scope itself; only an explicit
      // dropdown channel (or the ✕ dismissal via hint=0) goes on the wire.
      channel: scope ? null : channelFilter,
      platforms: scope ? null : platformFilter,
      kinds: kindFilter,
      source: sourceFilter,
      videoId: scope?.videoId ?? null,
      dateFrom: !everyDay && isValidDateParam(dateFrom) ? dateFrom : null,
      dateTo: !everyDay && isValidDateParam(dateTo) ? dateTo : null,
      lang: langFilter || null,
      limit: SEARCH_LIMIT,
      hint: hintDisabled ? false : undefined,
      semantic: semanticOn && sourceFilter !== 'chat',
    });
    void apiGet<ArchiveSearchResponse>(url)
      .then((res) => {
        if (!mountedRef.current || gen !== searchGenRef.current) return;
        setHits(res.hits ?? []);
        setEnriching(res.enriching ?? []);
        setChannelHint(res.channel_hint ?? null);
        setStatus('done');
      })
      .catch(() => {
        if (!mountedRef.current || gen !== searchGenRef.current) return;
        setHits([]);
        setError('Archive search is unavailable — is the backend running?');
        setStatus('error');
      });
  }, [query, channelFilter, platformFilter, kindFilter, dateFrom, dateTo, retryTick, sourceFilter, scope, everyDay, langFilter, hintDisabled, semanticOn]);

  // Remote YouTube channel-title search: the local index only holds the
  // newest ~100 uploads per saved channel (the panel fetch cap), so old
  // series are unreachable locally. Runs for "both" source only, and only
  // when the scope resolves to a saved channel with a YouTube handle.
  useEffect(() => {
    remoteGenRef.current += 1;
    const excludedPlatform = platformFilter.length > 0 && !platformFilter.includes('youtube');
    const excludedKind = kindFilter.length > 0 && !kindFilter.includes('vod');
    if (!query || scope || sourceFilter !== 'both' || excludedPlatform || excludedKind || !remoteYtHandle) {
      setRemoteHits([]);
      setRemoteStatus('idle');
      setRemoteError(null);
      return;
    }
    const gen = remoteGenRef.current;
    setRemoteStatus('loading');
    setRemoteError(null);
    const url = `/api/archive/search/remote?q=${encodeURIComponent(query)}&channel=${encodeURIComponent(remoteYtHandle)}&limit=${REMOTE_LIMIT}`;
    void apiGet<{ hits: ArchiveSearchHit[]; error?: string | null }>(url)
      .then((res) => {
        if (!mountedRef.current || gen !== remoteGenRef.current) return;
        setRemoteHits(res.hits ?? []);
        setRemoteError(res.error ?? null);
        setRemoteStatus('done');
      })
      .catch(() => {
        if (!mountedRef.current || gen !== remoteGenRef.current) return;
        setRemoteHits([]);
        setRemoteError('YouTube search unavailable');
        setRemoteStatus('error');
      });
  }, [query, scope, sourceFilter, platformFilter, kindFilter, remoteYtHandle]);

  // Remote hit → open in the player (no nearby-chat panel; not archived).
  const openRemoteHit = useCallback((hit: ArchiveSearchHit) => {
    if (!onOpenHit) return;
    if (!buildArchiveVodUrl(hit.platform, hit.video_id, hit.channel ?? undefined)) return;
    onOpenHit(hit, undefined);
  }, [onOpenHit]);

  // Nearby chat ±30s for the selected hit.
  const selectHit = useCallback((hit: ArchiveSearchHit) => {
    const video = videos[`${(hit.platform || '').toLowerCase()}:${hit.video_id}`];
    setSelected({ hit, video });
    // Watchdog synthetic rows (youtube-live-…) have no watchable URL — still
    // show nearby chat, but never hand the hit to the preview flow (open or seek).
    if (!buildArchiveVodUrl(hit.platform, hit.video_id, video?.channel)) return;
    if (onSeekHit) {
      onSeekHit(hit);
    } else {
      onOpenHit(hit, video);
    }
  }, [videos, onOpenHit, onSeekHit]);

  useEffect(() => {
    if (!selected) {
      setChat(null);
      setChatStatus('idle');
      return;
    }
    const gen = ++chatGenRef.current;
    const { hit } = selected;
    setChatStatus('loading');
    setChatError(null);
    void apiGet<{ messages: ArchiveChatMessage[] }>(
      `/api/archive/videos/${encodeURIComponent(hit.platform)}/${encodeURIComponent(hit.video_id)}/chat`
      + `?offset=${hit.offset_sec}&half=${CHAT_HALF_SEC}`,
    )
      .then((res) => {
        if (!mountedRef.current || gen !== chatGenRef.current) return;
        setChat(res.messages ?? []);
        setChatStatus('done');
      })
      .catch(() => {
        if (!mountedRef.current || gen !== chatGenRef.current) return;
        setChat([]);
        setChatError('Could not load nearby chat.');
        setChatStatus('error');
      });
  }, [selected, retryTick]);

  const retrySearch = useCallback(() => {
    setRetryTick((t) => t + 1);
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') onClose();
  }, [onClose]);

  const groups = selected && chat ? groupChatWindow(chat, selected.hit.offset_sec) : null;

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-label="Archive search"
      onKeyDown={handleKeyDown}
      className={
        embedded
          ? 'flex flex-col gap-2 p-3 border-2 border-white bg-zinc-950 shadow-2xl w-full h-full min-h-0'
          : 'fixed flex flex-col gap-2 p-3 border-2 border-white bg-zinc-950 shadow-2xl'
      }
      style={embedded ? undefined : { zIndex }}
    >
      <div
        className={`flex items-center justify-between gap-2 shrink-0 ${embedded ? '' : 'cursor-grab active:cursor-grabbing'}`}
        onPointerDown={embedded ? undefined : onDragStart}
      >
        <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">
          Archive search
        </span>
        <button
          type="button"
          onClick={onClose}
          title="Close (Esc)"
          className="text-zinc-500 hover:text-white p-0.5 shrink-0"
        >
          <X size={14} />
        </button>
      </div>

      {scope && (
        <div
          className="flex items-center gap-1.5 border border-zinc-700 border-b-yellow-300/60 bg-zinc-800/60 text-yellow-100/90 px-1.5 py-1 shrink-0 min-w-0"
          aria-label="Searching in this video"
        >
          <Search size={10} className="shrink-0" />
          <span className="text-[8px] font-mono uppercase tracking-widest font-bold truncate">
            Searching in this video: {scope.title || scope.videoId}
          </span>
        </div>
      )}

      <div className="flex gap-1 shrink-0">
        <div className="flex flex-1 items-center gap-1.5 bg-zinc-900 border-2 border-zinc-800 focus-within:border-white px-1.5">
          <Search size={12} className="text-zinc-500 shrink-0" />
          <input
            autoFocus
            type="text"
            value={inputQuery}
            onChange={(e) => setInputQuery(e.target.value)}
            placeholder={scope ? 'SEARCH THIS VIDEO...' : 'SEARCH TRANSCRIPTS + CHAT...'}
            className="flex-1 bg-transparent text-white font-mono placeholder:text-zinc-600 text-[11px] py-1 focus:outline-none min-w-0"
          />
          {status === 'loading' && <Loader2 size={12} className="text-zinc-500 animate-spin shrink-0" />}
        </div>
      </div>

      {/* Auto-scope chip (backend channel_hint) + background-indexing status line. */}
      <div className="flex flex-col gap-1 shrink-0">
        {channelHint && !channelFilter && !hintDisabled && (
          <div
            className="flex items-center gap-1.5 text-[8px] font-mono uppercase tracking-widest text-yellow-200/90 border border-yellow-300/40 bg-yellow-300/10 px-1.5 py-0.5 self-start"
            aria-label="Channel scope hint"
          >
            <span className="font-bold">scoped to {channelHint}</span>
            <button
              type="button"
              onClick={() => {
                setChannelHint(null);
                setHintDisabled(true);
              }}
              title="Remove channel scope"
              aria-label="Remove channel scope"
              className="text-yellow-200/90 hover:text-white p-0.5 -m-0.5"
            >
              <X size={10} />
            </button>
          </div>
        )}
        {enriching.length > 0 && (
          <div className="flex items-center gap-1.5 text-[9px] font-mono text-zinc-500" aria-live="polite">
            <Loader2 size={10} className="animate-spin shrink-0" />
            <span>
              Indexing {enriching.length} video{enriching.length === 1 ? '' : 's'}
              {' '}({enriching.map((e) => (e.kind === 'transcript' ? 'transcript' : 'chat backfill')).join(', ')})
              …
            </span>
          </div>
        )}
      </div>

      {/* ── FILTERS — every filter applies live to the same debounced search ── */}
      <div className="flex flex-col gap-1 shrink-0 border border-zinc-800 bg-zinc-900/40 p-1.5">
        {!scope && (
          <div className="flex items-center gap-1.5 min-w-0">
            <label htmlFor="archive-filter-channel" className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">
              Channel
            </label>
            <select
              id="archive-filter-channel"
              value={channelFilter}
              onChange={(e) => {
                setChannelFilter(e.target.value);
                if (e.target.value) setChannelHint(null); // explicit pick wins over the hint
              }}
              className="flex-1 min-w-0 bg-zinc-900 border border-zinc-700 text-white text-[10px] font-mono px-1 py-0.5 focus:outline-none focus:border-white"
            >
              <option value="">ALL CHANNELS</option>
              {channelOptions.map((o) => (
                <option key={o.label} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
        )}
        {!scope && (
          <div className="flex items-center gap-1.5">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">Platform</span>
            <div className="flex gap-1 flex-wrap">
              {ARCHIVE_PLATFORMS.map((p) => (
                <button
                  key={p}
                  type="button"
                  aria-pressed={platformFilter.includes(p)}
                  onClick={() => togglePlatform(p)}
                  className={`flex items-center gap-1 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors ${
                    platformFilter.includes(p)
                      ? 'bg-white text-black border-white'
                      : 'border-zinc-700 text-zinc-400 hover:border-white hover:text-white'
                  }`}
                >
                  <PlatformVodIcon platform={PLATFORM_ICON_NAME[p] ?? p} className="w-3 h-3" />
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="flex items-center gap-1.5">
          <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">Day</span>
          <input
            type="date"
            aria-label="From date"
            value={dateFrom}
            onChange={(e) => { setDateFrom(e.target.value); setEveryDay(false); }}
            className="w-30 min-w-0 bg-zinc-900 border border-zinc-700 text-white text-[10px] font-mono px-1 py-0.5 focus:outline-none focus:border-white [color-scheme:dark]"
          />
          <span className="text-zinc-600 text-[9px] shrink-0">→</span>
          <input
            type="date"
            aria-label="To date"
            value={dateTo}
            onChange={(e) => { setDateTo(e.target.value); setEveryDay(false); }}
            className="w-30 min-w-0 bg-zinc-900 border border-zinc-700 text-white text-[10px] font-mono px-1 py-0.5 focus:outline-none focus:border-white [color-scheme:dark]"
          />
          <button
            type="button"
            aria-pressed={everyDay}
            onClick={() => setEveryDay((v) => !v)}
            title="Every day: ignore the date range (picking a date unchecks this)"
            className={`shrink-0 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors ${
              everyDay
                ? 'bg-white text-black border-white'
                : 'border-zinc-700 text-yellow-200/90 hover:border-white hover:text-white'
            }`}
          >
            EVERY DAY
          </button>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">Kind</span>
          <div className="flex gap-1 flex-wrap">
            {ARCHIVE_KINDS.map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={kindFilter.includes(k)}
                onClick={() => toggleKind(k)}
                className={`px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors ${
                  kindFilter.includes(k)
                    ? 'bg-white text-black border-white'
                    : 'border-zinc-700 text-zinc-400 hover:border-white hover:text-white'
                }`}
              >
                {ARCHIVE_KIND_LABELS[k as keyof typeof ARCHIVE_KIND_LABELS]}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">Source</span>
          <div className="flex border-2 border-zinc-700">
            {ARCHIVE_SOURCES.map((s, i) => (
              <button
                key={s}
                type="button"
                aria-pressed={sourceFilter === s}
                onClick={() => setSourceFilter(s)}
                className={`px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                  i > 0 ? 'border-l-2 border-zinc-700' : ''
                } ${
                  sourceFilter === s
                    ? 'bg-white text-black'
                    : 'text-zinc-400 hover:bg-zinc-800 hover:text-white'
                }`}
              >
                {ARCHIVE_SOURCE_LABELS[s]}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            aria-pressed={semanticOn}
            disabled={sourceFilter === 'chat'}
            onClick={() => setSemanticOn((v) => !v)}
            title={
              sourceFilter === 'chat'
                ? 'Concept search covers transcripts only'
                : 'Concept search: finds moments by meaning, not just words (first use downloads the embed model)'
            }
            className={`px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors disabled:opacity-40 ${
              semanticOn
                ? 'bg-white text-black border-white'
                : 'border-zinc-700 text-zinc-400 hover:border-white hover:text-white'
            }`}
          >
            SEMANTIC
          </button>
        </div>
        {langsPresent.size >= 2 && (
          <div className="flex items-center gap-1.5">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">Lang</span>
            <div className="flex gap-1 flex-wrap">
              {ARCHIVE_LANGS.map((l) => (
                <button
                  key={l}
                  type="button"
                  aria-pressed={langFilter === l}
                  onClick={() => toggleLang(l)}
                  title={`Only show ${ARCHIVE_LANG_LABELS[l]} transcript rows`}
                  className={`px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors ${
                    langFilter === l
                      ? 'bg-white text-black border-white'
                      : 'border-zinc-700 text-zinc-400 hover:border-white hover:text-white'
                  }`}
                >
                  {ARCHIVE_LANG_LABELS[l]}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {status === 'error' && (
        <div className="border-2 border-red-500/75 bg-red-500/15 p-2 text-red-300 text-[10px] font-mono flex items-center gap-2 shrink-0">
          <span className="min-w-0 flex-1">{error}</span>
          <button
            type="button"
            onClick={retrySearch}
            title="Retry search"
            className="shrink-0 flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider"
          >
            <RefreshCw size={10} />
            Retry
          </button>
        </div>
      )}

      {status === 'done' && hits.length === 0 && remoteStatus !== 'loading' && (
        <p className="text-[10px] font-mono text-zinc-500 shrink-0">
          No results for &quot;{query}&quot; —{' '}
          {remoteStatus === 'done' && remoteHits.length === 0 && !remoteError
            ? 'nothing local matches and YouTube found nothing either'
            : 'nothing archived matches yet'}
          .
        </p>
      )}

      {/* Every hit is a partial word match (no row contains all query
          words) — say so instead of presenting fuzzy noise as exact. */}
      {hits.length > 0 && !hits.some((h) => !h.partial) && (
        <p className="text-[10px] font-mono text-amber-500/80 shrink-0">
          No exact match for &quot;{query}&quot; — showing closest word matches.
        </p>
      )}

      {/* ── HITS ── */}
      {hits.length > 0 && (
        <div className="flex flex-col gap-1.5 overflow-y-auto custom-scrollbar pr-1 min-h-0">
          {hits.map((hit) => {
            const video = videos[`${(hit.platform || '').toLowerCase()}:${hit.video_id}`];
            const spans = highlightQuerySpans(hit.text, query);
            const snippet = snippetAroundMatch(hit.text, query);
            let cursor = 0;
            const nodes: React.ReactNode[] = [];
            for (const span of spans) {
              if (span.start > cursor) nodes.push(snippet.slice(cursor, span.start));
              nodes.push(<mark key={span.start} className="bg-yellow-300 text-black px-0">{snippet.slice(span.start, span.end)}</mark>);
              cursor = span.end;
            }
            if (cursor < snippet.length) nodes.push(snippet.slice(cursor));
            const isSelected = selected?.hit === hit;
            return (
              <div key={`${hit.kind}:${hit.platform}:${hit.video_id}:${hit.offset_sec}`} className="flex items-stretch gap-1">
                <button
                  type="button"
                  onClick={() => selectHit(hit)}
                  className={`text-left border-2 p-1.5 flex flex-col gap-1 flex-1 min-w-0 transition-colors ${
                    isSelected
                      ? 'border-white bg-zinc-900'
                      : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-500'
                  }`}
                >
                <span className="flex items-center gap-1.5 min-w-0">
                  {hit.kind === 'transcript' || hit.kind === 'title'
                    ? <FileText size={10} className="text-zinc-400 shrink-0" />
                    : <MessageSquare size={10} className="text-zinc-400 shrink-0" />}
                  <span className="text-[8px] font-mono uppercase tracking-widest border border-zinc-700 px-1 py-px text-zinc-300 shrink-0">
                    {hit.kind}
                  </span>
                  {hit.kind === 'transcript' && hit.lang && (
                    <span className="text-[8px] font-mono uppercase tracking-widest border border-zinc-700 px-1 py-px text-zinc-500 shrink-0">
                      {ARCHIVE_LANG_LABELS[hit.lang] ?? hit.lang}
                    </span>
                  )}
                  {hit.semantic && (
                    <span className="text-[8px] font-mono uppercase tracking-widest border border-cyan-700 text-cyan-400 px-1 py-px shrink-0">
                      SEM
                    </span>
                  )}
                  <span className={`text-[9px] font-mono uppercase tracking-widest shrink-0 ${platformAccent[hit.platform] ?? 'text-zinc-400'}`}>
                    {hit.platform}
                  </span>
                  {hit.video_kind && hit.video_kind !== 'vod' && (
                    <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-600 border border-zinc-800 px-1 py-px shrink-0">
                      {kindLabel(hit.video_kind)}
                    </span>
                  )}
                  <span className="text-[9px] font-bold uppercase truncate text-zinc-200 min-w-0 flex-1">
                    {videoTitle(video, hit)}
                  </span>
                  <span className="text-[9px] font-mono text-zinc-400 shrink-0">
                    {formatArchiveOffset(hit.offset_sec)}
                  </span>
                </span>
                <span className="text-[10px] leading-snug text-zinc-400 break-words">
                  {(hit.channel ?? video?.channel) ? (
                    <span className="text-zinc-500 mr-1">@{(hit.channel ?? video?.channel)}</span>
                  ) : null}
                  {nodes}
                </span>
                </button>
                {onSeekHit && buildArchiveVodUrl(hit.platform, hit.video_id, video?.channel) && (
                  <button
                    type="button"
                    onClick={() => onOpenHit(hit, video)}
                    title="Open in player"
                    aria-label={`Open ${videoTitle(video, hit)} in player`}
                    className="border-2 border-zinc-800 bg-zinc-900/60 hover:border-white text-zinc-400 hover:text-white px-1.5 flex items-center shrink-0"
                  >
                    <ExternalLink size={10} />
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ── REMOTE YOUTUBE RESULTS (channel-scoped title search) ── */}
      {(remoteStatus === 'loading' || remoteStatus === 'done' || remoteStatus === 'error') && (
        <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-1.5 min-h-0 shrink-0">
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">
              YouTube results{remoteYtHandle ? ` · @${remoteYtHandle}` : ''}
            </span>
            {remoteStatus === 'loading' && (
              <Loader2 size={10} className="animate-spin text-zinc-500 shrink-0" />
            )}
          </div>
          {remoteStatus === 'error' && (
            <p className="text-[9px] font-mono text-red-400/80 shrink-0">{remoteError}</p>
          )}
          {remoteStatus === 'done' && remoteHits.length === 0 && (
            <p className="text-[9px] font-mono text-zinc-600 shrink-0">
              {remoteError ?? `No YouTube matches for "${query}"`}
            </p>
          )}
          {remoteStatus === 'done' && remoteHits.length > 0 && (
            <div className="flex flex-col gap-1 overflow-y-auto custom-scrollbar pr-1 min-h-0">
              {remoteHits.map((hit) => (
                <button
                  key={hit.video_id}
                  type="button"
                  onClick={() => openRemoteHit(hit)}
                  title="Open in the player — download from there"
                  className="text-left border-2 border-zinc-800 bg-zinc-900/60 hover:border-zinc-500 p-1.5 flex flex-col gap-1 transition-colors"
                >
                  <span className="flex items-center gap-1.5 min-w-0">
                    <span className="text-[8px] font-mono uppercase tracking-widest border border-zinc-700 px-1 py-px text-zinc-300 shrink-0">
                      youtube
                    </span>
                    <span className="text-[9px] font-mono uppercase tracking-widest text-[#F03030] shrink-0">
                      YouTube
                    </span>
                    <span className="text-[9px] font-bold uppercase truncate text-zinc-200 min-w-0 flex-1">
                      {displayTitle({ title: hit.title, originalTitle: hit.originalTitle })}
                    </span>
                    {hit.duration_string && (
                      <span className="text-[9px] font-mono text-zinc-400 shrink-0">
                        {hit.duration_string}
                      </span>
                    )}
                  </span>
                  <span className="text-[10px] text-zinc-500 break-words">
                    @{hit.channel ?? remoteYtHandle} — click to open in the player
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── NEARBY CHAT — "below the player" panel ── */}
      {selected && (
        <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-2 min-h-0">
          <div className="flex items-center justify-between gap-2 shrink-0">
            <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">
              Nearby chat ±{CHAT_HALF_SEC}s
            </span>
            <span className="text-[9px] font-mono text-zinc-400 shrink-0">
              {videoTitle(selected.video, selected.hit)} @ {formatArchiveOffset(selected.hit.offset_sec)}
            </span>
          </div>
          {chatStatus === 'loading' && (
            <div className="flex items-center gap-1.5 text-zinc-500 text-[10px] font-mono shrink-0">
              <Loader2 size={11} className="animate-spin" />
              Loading chat window...
            </div>
          )}
          {chatStatus === 'error' && (
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-red-300 text-[10px] font-mono flex-1">{chatError}</span>
              <button
                type="button"
                onClick={retrySearch}
                className="shrink-0 flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-red-300"
              >
                <RefreshCw size={10} />
                Retry
              </button>
            </div>
          )}
          {chatStatus === 'done' && groups && chat && (
            <div className="flex flex-col gap-0.5 overflow-y-auto custom-scrollbar pr-1 max-h-52 min-h-0">
              {chat.length === 0 && (
                <p className="text-[10px] font-mono text-zinc-500">No archived chat near this moment.</p>
              )}
              {groups.before.map((m) => (
                <p key={`b:${m.offset_sec}:${m.username}:${m.text}`} className="text-[10px] leading-snug text-zinc-400 break-words">
                  <span className="text-zinc-600 font-mono mr-1">{formatArchiveOffset(m.offset_sec)}</span>
                  <span className="font-bold" style={{ color: resolveChatColor(m.color, m.username, m.platform) }}>{m.username}:</span> {m.text}
                  {typeof m.spam_count === 'number' && m.spam_count > 1 && (
                    <span className="text-[9px] font-mono text-zinc-600 ml-1" title={`${m.spam_count} identical messages collapsed`}>
                      ×{m.spam_count}
                    </span>
                  )}
                </p>
              ))}
              {groups.before.length + groups.after.length > 0 && (
                <div className="flex items-center gap-1.5 my-0.5 shrink-0">
                  <span className="h-px flex-1 bg-yellow-300/60" />
                  <span className="text-[8px] font-mono uppercase tracking-widest text-yellow-300 bg-yellow-300/10 border border-yellow-300/40 px-1 py-px">
                    Hit moment {formatArchiveOffset(selected.hit.offset_sec)}
                  </span>
                  <span className="h-px flex-1 bg-yellow-300/60" />
                </div>
              )}
              {groups.after.map((m) => (
                <p key={`a:${m.offset_sec}:${m.username}:${m.text}`} className="text-[10px] leading-snug text-zinc-400 break-words">
                  <span className="text-zinc-600 font-mono mr-1">{formatArchiveOffset(m.offset_sec)}</span>
                  <span className="font-bold" style={{ color: resolveChatColor(m.color, m.username, m.platform) }}>{m.username}:</span> {m.text}
                  {typeof m.spam_count === 'number' && m.spam_count > 1 && (
                    <span className="text-[9px] font-mono text-zinc-600 ml-1" title={`${m.spam_count} identical messages collapsed`}>
                      ×{m.spam_count}
                    </span>
                  )}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
      {!embedded && (
        <PanelResizeHandles onPointerDown={onResizeStart} />
      )}
    </div>
  );
}

export default ArchiveSearchPopup;
