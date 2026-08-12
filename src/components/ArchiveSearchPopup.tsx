/**
 * Archive search popup — the "local Google" UI.
 *
 * Searches the local archive (transcripts + chat) via GET /api/archive/search,
 * opens the hit in the existing explore-player flow at the hit offset
 * (App passes the vod with initialTimeSec), and shows the chat history from
 * the hit onward (the whole remaining VOD, scrollable) with a marker line at
 * the hit moment.
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
  ARCHIVE_FILTER_KINDS,
  ARCHIVE_KIND_LABELS,
  ARCHIVE_LANG_LABELS,
  ARCHIVE_LANGS,
  ARCHIVE_PLATFORMS,
  ARCHIVE_SOURCES,
  ARCHIVE_SOURCE_LABELS,
  buildArchiveVodUrl,
  buildSearchUrl,
  enrichKindCounts,
  formatArchiveOffset,
  formatRelativeDate,
  highlightQuerySpans,
  hitPlatforms,
  isValidDateParam,
  todayIso,
  kindLabel,
  resolveOpenTargets,
  snippetAroundMatch,
  type ArchiveChatMessage,
  type ArchiveEnrichEntry,
  type ArchiveOpenTarget,
  type ArchiveSearchHit,
  type ArchiveSearchResponse,
  type ArchiveSource,
  type ArchiveVideoRow,
} from '../archiveSearchUtils';
import { deriveChannelDisplayName, displayTitle } from '../channelUtils';
import { resolveChatColor } from '../chatColors';
import { seekToTimestamp } from '../seekToTimestamp';
import { langFamily, useI18n } from '../i18n';
import type { SavedChannel } from '../types';
import PlatformVodIcon from './PlatformVodIcon';

interface ArchiveSearchPopupProps {
  zIndex: number;
  onClose: () => void;
  /** Floating-mode bring-to-front: the root's pointer-capture bumps the
   *  popup's rank so a clicked search popup climbs above every player and
   *  the second search instance (App wires this to popupZCounterRef).
   *  Absent in embedded mode. */
  onBringToFront?: () => void;
  /** Open the hit in the explore-player flow (App owns the popup stack).
   *  `targets` = resolvable per-platform open targets for the hit's
   *  canonical VOD (primary first) — App picks the least-opened platform. */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined, targets?: ArchiveOpenTarget[]) => void;
  /** When provided, clicking a hit row seeks instead of opening: the row
   *  click calls onSeekHit(hit) and a small per-row 'open' affordance still
   *  calls onOpenHit. Absent → current behavior (row click opens). */
  onSeekHit?: (hit: ArchiveSearchHit) => void;
  /** When provided, chat-history messages (CHAT FROM HIT) also seek: clicking
   *  a message seeks the host's current player to the message's offset_sec
   *  (same player as onSeekHit, via the shared seekToTimestamp contract).
   *  Absent → the messages stay read-only. */
  onSeekOffset?: (offsetSec: number) => void;
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
/** Literal-word searches page through matches; 300 rows (5 videos × 60,
 *  bounded server-side) is a full scroll of variety — the old 2000 flooded
 *  the page with one video's repeated chat and took 20s+ under load. The
 *  list renders incrementally, so even this stays smooth. */
const SEARCH_LIMIT_LITERAL = 100000;
/** Semantic (embedding) search stays tight — it is expensive per candidate. */
const SEARCH_LIMIT_SEMANTIC = 30;
/** How many hits render per scroll batch. */
const HITS_RENDER_CHUNK = 200;
/** How many chat-history rows render per scroll batch — the from-offset
 *  mode can return thousands, so the panel grows incrementally like the
 *  hits list instead of mounting every row at once. */
const CHAT_RENDER_CHUNK = 500;
const REMOTE_LIMIT = 20;
/** Floating-mode seed position — the pre-chrome location (top 80, right 24). */
const SEED_Y = 80;
const SEED_RIGHT = 24;

/** Kind-badge display words — i18n keys (message → chat, transcript →
 *  speech). 'title' and remote 'youtube' badges keep their raw kind values. */
const KIND_BADGE_LABEL: Record<string, string> = {
  message: 'chat',
  transcript: 'speech',
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

export function ArchiveSearchPopup({ zIndex, onClose, onOpenHit, onSeekHit, onSeekOffset, embedded = false, scope, savedChannels, initialPos, initialChannel, onBringToFront }: ArchiveSearchPopupProps) {
  const { t, lang } = useI18n();
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
  /** Multi-select content sources; all three ON by default. */
  const [sourceFilter, setSourceFilter] = useState<ArchiveSource[]>([...ARCHIVE_SOURCES]);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  /** True = ignore the stored date range (default). A date pick unchecks it;
   *  re-checking keeps the stored values but search ignores them again. */
  const [everyDay, setEveryDay] = useState(true);
  /** '' | 'pt' | 'en' — transcript language filter ('' = all). Defaults to
   *  the app UI language; clicking the active chip again opts back into ''
   *  (all languages). */
  const [langFilter, setLangFilter] = useState<'' | 'pt' | 'en'>(langFamily(lang) === 'pt' ? 'pt' : 'en');
  /** Chat author filter ('' = all authors); '@' tolerated, case-insensitive. */
  const [userFilter, setUserFilter] = useState('');
  const [searchMode, setSearchMode] = useState<'exact' | 'broad' | 'semantic'>('exact');
  const [status, setStatus] = useState<SearchStatus>('idle');
  const [error, setError] = useState<string | null>(null);
  const [hits, setHits] = useState<ArchiveSearchHit[]>([]);
  /** How many hits are actually in the DOM — grows as the user scrolls, so
   *  an uncapped result set never mounts thousands of rows at once. */
  const [visibleCount, setVisibleCount] = useState(HITS_RENDER_CHUNK);
  const hitsScrollRef = useRef<HTMLDivElement>(null);
  const visibleCountRef = useRef(HITS_RENDER_CHUNK);
  useEffect(() => {
    visibleCountRef.current = visibleCount;
  }, [visibleCount]);
  const [videos, setVideos] = useState<Record<string, ArchiveVideoRow>>({});
  const [selected, setSelected] = useState<{ hit: ArchiveSearchHit; video: ArchiveVideoRow | undefined } | null>(null);
  const [chat, setChat] = useState<ArchiveChatMessage[] | null>(null);
  const [chatStatus, setChatStatus] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [chatError, setChatError] = useState<string | null>(null);
  /** Platforms present in the selected hit's canonical dedupe group (the
   *  chat endpoint merges every member). [] before the first page lands. */
  const [chatPlatforms, setChatPlatforms] = useState<string[]>([]);
  /** Platforms hidden by the chips — ALL on by default, at least one stays
   *  visible. */
  const [chatHidden, setChatHidden] = useState<Set<string>>(new Set());
  /** Fetch-path mirrors of the group state (fetchChatPage's closure must
   *  stay stable across pages — state copies are render-only). */
  const chatPlatformsRef = useRef<string[]>([]);
  /** Per-platform resume offsets echoed back as `offsets=` on continuation
   *  pages (each group member keysets independently). */
  const chatNextOffsetsRef = useRef<Record<string, number>>({});
  /** Backend capped the from-offset history at its row limit (tail cut). */
  const [chatTruncated, setChatTruncated] = useState(false);
  /** How many chat rows are mounted — grows on scroll (long histories). */
  const [chatVisibleCount, setChatVisibleCount] = useState(CHAT_RENDER_CHUNK);
  const chatScrollRef = useRef<HTMLDivElement>(null);
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
  // `scope` is a caller-supplied object literal — App rebuilds the
  // preview-search scope on every render, so its IDENTITY is unstable. Only
  // its content matters to the search: depending on the object itself would
  // re-fire the debounced request on every parent render (the observed
  // ~1-2s request storm). Effects depend on these primitives instead.
  const scopeActive = scope != null;
  const scopeVideoId = scope?.videoId ?? null;
  /** Keyboard-navigated hit row (−1 = none). Reset whenever the list changes. */
  const [activeIdx, setActiveIdx] = useState(-1);
  const hitRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setActiveIdx(-1);
  }, [hits]);

  /** Platform-filtered hits. The chips drive the backend `platform` param,
   *  but a hit matches a chip when ANY of its platforms (dedupe membership)
   *  equals it — re-check locally so the UI contract holds even while the
   *  backend still filters by the primary platform only. */
  const displayHits = useMemo(() => {
    if (platformFilter.length === 0) return hits;
    return hits.filter((h) => hitPlatforms(h).some((p) => platformFilter.includes(p)));
  }, [hits, platformFilter]);

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
    // Empty query is still a valid search when the chat-author filter is
    // set: the backend returns that author's whole history, newest first.
    if (!query && !userFilter.trim()) {
      searchGenRef.current += 1;
      setStatus('idle');
      setHits([]);
      setVisibleCount(HITS_RENDER_CHUNK);
      setEnriching([]);
      setError(null);
      return;
    }
    // A 1-char query is a keystroke mid-word, not a search: firing it
    // sends a 300-row request for 'o' (millions of matches, seconds of
    // latency) that the next keystroke immediately cancels. Wait for ≥2
    // chars (the author-history path above stays untouched).
    if (query.trim().length < 2 && !userFilter.trim()) {
      searchGenRef.current += 1;
      setStatus('idle');
      setHits([]);
      setVisibleCount(HITS_RENDER_CHUNK);
      setEnriching([]);
      setError(null);
      return;
    }
    const gen = ++searchGenRef.current;
    setStatus('loading');
    setError(null);
    // Date inputs can hold partial/typed garbage; invalid values become unset.
    // everyDay=true ignores the stored range (default; a date pick unchecks it).
    // With the range active, a start date without an end closes at today —
    // an open-ended range would reach into future-dated rows. The mirror
    // case (end only) stays open at the start.
    const rangeActive = !everyDay;
    const fromOk = rangeActive && isValidDateParam(dateFrom);
    const toOk = rangeActive && isValidDateParam(dateTo);
    const today = todayIso();
    const url = buildSearchUrl({
      query,
      // The hint is an implicit backend scope — echoing it here as an
      // explicit channel param suppresses the hint and re-fires the search
      // forever. The API applies the scope itself; only an explicit
      // dropdown channel (or the ✕ dismissal via hint=0) goes on the wire.
      channel: scopeActive ? null : channelFilter,
      platforms: scopeActive ? null : platformFilter,
      kinds: kindFilter,
      source: sourceFilter,
      videoId: scopeVideoId,
      dateFrom: fromOk ? dateFrom : null,
      dateTo: toOk ? dateTo : fromOk ? today : null,
      lang: langFilter || null,
      username: userFilter || null,
      limit: searchMode === 'semantic' ? SEARCH_LIMIT_SEMANTIC : SEARCH_LIMIT_LITERAL,
      hint: hintDisabled ? false : undefined,
      semantic: searchMode === 'semantic' && sourceFilter.includes('transcript'),
      mode: searchMode === 'semantic' ? 'semantic' : searchMode,
    });
    void apiGet<ArchiveSearchResponse>(url)
      .then((res) => {
        if (!mountedRef.current || gen !== searchGenRef.current) return;
        setHits(res.hits ?? []);
        setVisibleCount(HITS_RENDER_CHUNK);
        if (hitsScrollRef.current) hitsScrollRef.current.scrollTop = 0;
        setEnriching(res.enriching ?? []);
        setChannelHint(res.channel_hint ?? null);
        setStatus('done');
      })
      .catch(() => {
        if (!mountedRef.current || gen !== searchGenRef.current) return;
        setHits([]);
        setError(t('Archive search is unavailable — is the backend running?'));
        setStatus('error');
      });
  }, [query, channelFilter, platformFilter, kindFilter, dateFrom, dateTo, retryTick, sourceFilter, scopeActive, scopeVideoId, everyDay, langFilter, hintDisabled, searchMode, userFilter]);

  // Remote YouTube channel-title search: the local index only holds the
  // newest ~100 uploads per saved channel (the panel fetch cap), so old
  // series are unreachable locally. Runs only when every source is
  // selected (the "both"-equivalent default), and only when the scope
  // resolves to a saved channel with a YouTube handle.
  useEffect(() => {
    remoteGenRef.current += 1;
    const excludedPlatform = platformFilter.length > 0 && !platformFilter.includes('youtube');
    const excludedKind = kindFilter.length > 0 && !kindFilter.includes('vod');
    const allSources = sourceFilter.length === ARCHIVE_SOURCES.length;
    if (!query || scopeActive || !allSources || excludedPlatform || excludedKind || !remoteYtHandle) {
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
        setRemoteError(t('YouTube search unavailable'));
        setRemoteStatus('error');
      });
  }, [query, scopeActive, sourceFilter, platformFilter, kindFilter, remoteYtHandle]);

  // Resolve the hit's per-platform open targets (primary first) and hand
  // them to App, which picks the least-opened platform this session. arg[1]
  // stays the primary platform's video row (existing App contract).
  const openHit = useCallback((hit: ArchiveSearchHit, fallbackVideo: ArchiveVideoRow | undefined) => {
    const targets = resolveOpenTargets(hit, videos);
    const primary = targets.find((t) => t.platform === (hit.platform || '').toLowerCase());
    onOpenHit(hit, primary?.video ?? fallbackVideo, targets);
  }, [videos, onOpenHit]);

  // Remote hit → open in the player (no chat-history panel; not archived).
  const openRemoteHit = useCallback((hit: ArchiveSearchHit) => {
    if (!onOpenHit) return;
    if (!buildArchiveVodUrl(hit.platform, hit.video_id, hit.channel ?? undefined)) return;
    onOpenHit(hit, undefined);
  }, [onOpenHit]);

  // Chat history from the selected hit onward.
  const selectHit = useCallback((hit: ArchiveSearchHit) => {
    const video = videos[`${(hit.platform || '').toLowerCase()}:${hit.video_id}`];
    setSelected({ hit, video });
    // Watchdog synthetic rows (youtube-live-…) have no watchable URL — still
    // show the chat history, but never hand the hit to the preview flow (open or seek).
    if (!buildArchiveVodUrl(hit.platform, hit.video_id, video?.channel)) return;
    if (onSeekHit) {
      onSeekHit(hit);
    } else {
      openHit(hit, video);
    }
  }, [videos, onSeekHit, openHit]);

  // ── Chat history pagination ────────────────────────────────────────────
  /** Next page's fetch offset — the last delivered row's offset_sec (the
   *  backend's >= boundary re-includes equal-offset rows; the append dedupes
   *  them, so paging never duplicates or gaps). */
  const chatOffsetRef = useRef(0);
  /** In-flight guard: one continuation page at a time. */
  const chatFetchingRef = useRef(false);
  /** True until the first page lands — only it resets the scroll position. */
  const chatFirstRef = useRef(true);

  /** Fetch one page of the from-hit history (half=0). The first page
   *  replaces the list; a truncated tail is continued from the last row's
   *  offset_sec by the scroll handler (near-bottom). */
  const fetchChatPage = useCallback(async (gen: number) => {
    if (chatFetchingRef.current || !selected) return;
    chatFetchingRef.current = true;
    const { hit } = selected;
    const first = chatFirstRef.current;
    try {
      // Multi-platform groups resume each member from its own last offset;
      // single-platform (or first page) keeps the plain global-offset URL.
      const groupMode = chatPlatformsRef.current.length > 1;
      const offsetsParam =
        groupMode && Object.keys(chatNextOffsetsRef.current).length > 0
          ? '&offsets=' + Object.entries(chatNextOffsetsRef.current)
              .map(([p, o]) => `${p}:${o}`).join(',')
          : '';
      const res = await apiGet<{
        messages: ArchiveChatMessage[];
        truncated?: boolean;
        platforms?: string[];
        next_offsets?: Record<string, number>;
      }>(
        `/api/archive/videos/${encodeURIComponent(hit.platform)}/${encodeURIComponent(hit.video_id)}/chat`
        + `?offset=${chatOffsetRef.current}&half=0${offsetsParam}`,
      );
      if (!mountedRef.current || gen !== chatGenRef.current) return;
      const msgs = res.messages ?? [];
      if (msgs.length > 0) {
        chatOffsetRef.current = msgs[msgs.length - 1].offset_sec;
      }
      if (res.platforms && res.platforms.length > 0) {
        chatPlatformsRef.current = res.platforms;
        setChatPlatforms(res.platforms);
      }
      if (res.next_offsets && Object.keys(res.next_offsets).length > 0) {
        chatNextOffsetsRef.current = res.next_offsets;
      }
      if (first) {
        chatFirstRef.current = false;
        setChatHidden(new Set()); // ALL platforms on by default
        setChat(msgs);
        setChatVisibleCount(CHAT_RENDER_CHUNK);
        if (chatScrollRef.current) chatScrollRef.current.scrollTop = 0;
      } else {
        // Append, dropping rows the previous page already delivered (the
        // backend re-includes the equal-offset boundary run). The key
        // carries the platform — two group members may share offset/user/text.
        setChat((prev) => {
          const seen = new Set(
            (prev ?? []).map((m) => `${m.platform}|${m.offset_sec}|${m.username}|${m.text}`),
          );
          const fresh = msgs.filter((m) => !seen.has(`${m.platform}|${m.offset_sec}|${m.username}|${m.text}`));
          return [...(prev ?? []), ...fresh];
        });
        // The near-bottom scroll that fired this fetch clamped the mounted
        // chunk to the then-current chat length — make a chunk of the
        // appended rows mountable right away (slice caps at the real length;
        // further mounting stays scroll-driven).
        setChatVisibleCount((c) => c + Math.min(msgs.length, CHAT_RENDER_CHUNK));
      }
      setChatTruncated(Boolean(res.truncated));
      setChatStatus('done');
    } catch {
      // A failed continuation page keeps what is already loaded — the next
      // near-bottom scroll retries it. Only the first page is a hard error.
      if (!mountedRef.current || gen !== chatGenRef.current) return;
      if (first) {
        setChat([]);
        setChatTruncated(false);
        setChatError(t('Could not load chat history.'));
        setChatStatus('error');
      }
    } finally {
      if (gen === chatGenRef.current) chatFetchingRef.current = false;
    }
  }, [selected, t]);

  useEffect(() => {
    if (!selected) {
      setChat(null);
      setChatTruncated(false);
      setChatStatus('idle');
      setChatPlatforms([]);
      setChatHidden(new Set());
      chatPlatformsRef.current = [];
      chatNextOffsetsRef.current = {};
      return;
    }
    const gen = ++chatGenRef.current;
    const { hit } = selected;
    setChatStatus('loading');
    setChatError(null);
    chatOffsetRef.current = hit.offset_sec;
    chatFirstRef.current = true;
    chatFetchingRef.current = false;
    // half=0 → the backend's "from offset onward" mode: the whole chat
    // history from the hit to the end of the VOD, page by page (a truncated
    // tail continues from the last row's offset_sec on scroll).
    void fetchChatPage(gen);
  }, [selected, retryTick, fetchChatPage]);

  const retrySearch = useCallback(() => {
    setRetryTick((t) => t + 1);
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      onClose();
      return;
    }
    // Arrow/Enter navigation only while typing in the search box — other
    // controls (selects, date inputs) own their arrow keys.
    if (e.target !== inputRef.current) return;
    const n = Math.min(displayHits.length, visibleCountRef.current);
    // Reveal the chunk holding the target row so the highlight is visible.
    const ensureVisible = (i: number) => {
      if (i >= visibleCountRef.current) {
        setVisibleCount((c) => Math.min(displayHits.length, Math.max(c + HITS_RENDER_CHUNK, i + 1)));
      }
    };
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!n) return;
      setActiveIdx((i) => {
        const next = i + 1 >= n ? 0 : i + 1;
        ensureVisible(next);
        hitRefs.current[next]?.scrollIntoView?.({ block: 'nearest' });
        return next;
      });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (!n) return;
      setActiveIdx((i) => {
        const next = i <= 0 ? n - 1 : i - 1;
        ensureVisible(next);
        hitRefs.current[next]?.scrollIntoView?.({ block: 'nearest' });
        return next;
      });
    } else if (e.key === 'Enter' && activeIdx >= 0 && activeIdx < n) {
      e.preventDefault();
      selectHit(displayHits[activeIdx]);
    } else if (e.key === 'Enter') {
      // No arrow-selected hit → Enter re-runs the search with the CURRENT
      // filters (refresh affordance for a done search; typing a new query
      // with contexto re-fires through the debounced effect).
      e.preventDefault();
      retrySearch();
    }
  }, [onClose, displayHits, activeIdx, selectHit, retrySearch]);

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-label={t('Archive search')}
      onKeyDown={handleKeyDown}
      onPointerDownCapture={embedded ? undefined : onBringToFront}
      className={
        embedded
          ? 'flex flex-col gap-2 p-3 border-2 border-white bg-zinc-950 shadow-2xl w-full h-full min-h-0 overflow-hidden'
          : 'fixed flex flex-col gap-2 p-3 border-2 border-white bg-zinc-950 shadow-2xl overflow-hidden'
      }
      style={embedded ? undefined : { zIndex }}
    >
      <div
        className={`flex items-center justify-between gap-2 shrink-0 relative z-[60] ${embedded ? '' : 'cursor-grab active:cursor-grabbing'}`}
        onPointerDown={embedded ? undefined : onDragStart}
      >
        <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">
          {t('Archive search')}
        </span>
        <button
          type="button"
          onClick={onClose}
          title={t('Close (Esc)')}
          className="text-zinc-500 hover:text-white p-0.5 shrink-0"
        >
          <X size={14} />
        </button>
      </div>

      {scope && (
        <div
          className="flex items-center gap-1.5 border border-zinc-700 border-b-yellow-300/60 bg-zinc-800/60 text-yellow-100/90 px-1.5 py-1 shrink-0 min-w-0"
          aria-label={t('Searching in this video: {title}', { title: scope.title || scope.videoId })}
        >
          <Search size={10} className="shrink-0" />
          <span className="text-[8px] font-mono uppercase tracking-widest font-bold truncate">
            {t('Searching in this video: {title}', { title: scope.title || scope.videoId })}
          </span>
        </div>
      )}

      <div className="flex gap-1 shrink-0">
        <div className="flex flex-1 items-center gap-1.5 bg-zinc-900 border-2 border-zinc-800 focus-within:border-white px-1.5">
          <Search size={12} className="text-zinc-500 shrink-0" />
          <input
            ref={inputRef}
            autoFocus
            type="text"
            value={inputQuery}
            onChange={(e) => setInputQuery(e.target.value)}
            placeholder={scope ? t('SEARCH THIS VIDEO...') : t('SEARCH TRANSCRIPTS + CHAT...')}
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
            <span className="font-bold">{t('scoped to {channel}', { channel: channelHint })}</span>
            <button
              type="button"
              onClick={() => {
                setChannelHint(null);
                setHintDisabled(true);
              }}
              title={t('Remove channel scope')}
              aria-label={t('Remove channel scope')}
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
              {enriching.length === 1
                ? t('Indexing {count} video', { count: enriching.length })
                : t('Indexing {count} videos', { count: enriching.length })}
              {' '}({enrichKindCounts(enriching)
                .map(({ kind, count }) => {
                  const label = kind === 'chat' ? t('chat backfill') : kind === 'transcribe' ? t('transcription') : kind;
                  return count > 1 ? `${label} (${count})` : label;
                })
                .join(', ')})
            </span>
          </div>
        )}
      </div>

      {/* ── FILTERS — every filter applies live to the same debounced search ── */}
      <div className="flex flex-col gap-1.5 shrink-0 border border-zinc-800 bg-zinc-900/40 p-1.5">
        {!scope && (
          <div className="flex items-center gap-1.5 min-w-0">
            <label htmlFor="archive-filter-channel" className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">
              {t('Channel')}
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
              <option value="">{t('ALL CHANNELS')}</option>
              {channelOptions.map((o) => (
                <option key={o.label} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
        )}
        <div className="grid grid-cols-2 gap-x-2 gap-y-1.5">
          {!scope && (
            <div className="flex items-center gap-1.5 min-w-0">
              <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">{t('Platform')}</span>
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
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">{t('Kind')}</span>
            <div className="flex gap-1 flex-wrap">
              {ARCHIVE_FILTER_KINDS.map((k) => (
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
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">{t('Day')}</span>
          <input
            type="date"
            aria-label={t('From date')}
            value={dateFrom}
            onChange={(e) => { setDateFrom(e.target.value); setEveryDay(false); }}
            className="w-30 min-w-0 bg-zinc-900 border border-zinc-700 text-white text-[10px] font-mono px-1 py-0.5 focus:outline-none focus:border-white [color-scheme:dark]"
          />
          <span className="text-zinc-600 text-[9px] shrink-0">→</span>
          <input
            type="date"
            aria-label={t('To date')}
            value={dateTo}
            onChange={(e) => { setDateTo(e.target.value); setEveryDay(false); }}
            className="w-30 min-w-0 bg-zinc-900 border border-zinc-700 text-white text-[10px] font-mono px-1 py-0.5 focus:outline-none focus:border-white [color-scheme:dark]"
          />
          <button
            type="button"
            aria-pressed={everyDay}
            onClick={() => {
              // Unchecking with no range seeded starts from today — the
              // toggle must have an immediate effect even before a date is
              // picked (an empty range would behave exactly like every day).
              if (everyDay && !dateFrom && !dateTo) setDateFrom(todayIso());
              setEveryDay((v) => !v);
            }}
            title={t('Every day: ignore the date range. Off = filter by day — a start date without an end closes at today; unchecking with no dates starts from today')}
            className={`shrink-0 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors ${
              everyDay
                ? 'bg-white text-black border-white'
                : 'border-zinc-700 text-yellow-200/90 hover:border-white hover:text-white'
            }`}
          >
            {t('EVERY DAY')}
          </button>
        </div>
        <div className="grid grid-cols-1 gap-y-1.5 items-start">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">{t('Source')}</span>
            <div className="flex border-2 border-zinc-700">
              {ARCHIVE_SOURCES.map((s, i) => {
                const on = sourceFilter.includes(s);
                return (
                  <button
                    key={s}
                    type="button"
                    aria-pressed={on}
                    onClick={() =>
                      setSourceFilter((cur) => {
                        const next = on
                          ? cur.filter((x) => x !== s)
                          : [...cur, s];
                        // Never empty out — an empty source set would
                        // silently mean "all" on the backend (param
                        // omitted), reading as a bug.
                        return next.length > 0 ? next : [...ARCHIVE_SOURCES];
                      })
                    }
                    className={`px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                      i > 0 ? 'border-l-2 border-zinc-700' : ''
                    } ${
                      on
                        ? 'bg-white text-black'
                        : 'text-zinc-400 hover:bg-zinc-800 hover:text-white'
                    }`}
                  >
                    {t(ARCHIVE_SOURCE_LABELS[s])}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="flex items-center gap-1.5 min-w-0 pt-1 mt-0.5">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">{t('Mode')}</span>
            {([
              ['exact', 'EXACT', t('Exact phrase: finds the words in order, even inside a longer sentence')],
              ['broad', 'BROAD', t('Broad match: close spellings and related words')],
              ['semantic', 'CONTEXT', t('Context search (semantic): finds moments by meaning, not just exact words')],
            ] as const).map(([id, label, title]) => (
              <button
                key={id}
                type="button"
                aria-pressed={searchMode === id}
                disabled={id === 'semantic' && !sourceFilter.includes('transcript')}
                onClick={() => setSearchMode(id)}
                title={id === 'semantic' && !sourceFilter.includes('transcript')
                  ? t('Context search covers transcripts only')
                  : title}
                className={`px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors disabled:opacity-40 ${
                  searchMode === id
                    ? 'bg-white text-black border-white'
                    : 'border-zinc-700 text-zinc-400 hover:border-white hover:text-white'
                }`}
              >
                {t(label)}
              </button>
            ))}
            {langsPresent.size >= 2 && (
              <div className="flex gap-1 flex-wrap">
                {ARCHIVE_LANGS.map((l) => (
                  <button
                    key={l}
                    type="button"
                    aria-pressed={langFilter === l}
                    onClick={() => toggleLang(l)}
                    title={t('Only show {lang} transcript rows', { lang: ARCHIVE_LANG_LABELS[l] })}
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
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">{t('User')}</span>
          <input
            type="text"
            aria-label={t('Chat author')}
            placeholder="user1,user2…"
            title={t('Chat author filter — comma-separate multiple users; leave the search box empty to list their whole history')}
            value={userFilter}
            onChange={(e) => setUserFilter(e.target.value)}
            spellCheck={false}
            className="min-w-0 flex-1 bg-zinc-900 border border-zinc-700 text-white text-[10px] font-mono px-1 py-0.5 focus:outline-none focus:border-white"
          />
          {userFilter && (
            <button
              type="button"
              onClick={() => setUserFilter('')}
              title={t('Clear user filter')}
              className="shrink-0 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border border-zinc-700 text-zinc-400 hover:border-white hover:text-white"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* ── RESULTS REGION — every scrollable list is bounded so many
          hits can never push the panel's fixed blocks out of view ── */}
      <div className="flex flex-col gap-1.5 min-h-0 flex-1">
      {status === 'error' && (
        <div className="border-2 border-red-500/75 bg-red-500/15 p-2 text-red-300 text-[10px] font-mono flex items-center gap-2 shrink-0">
          <span className="min-w-0 flex-1">{error}</span>
          <button
            type="button"
            onClick={retrySearch}
            title={t('Retry search')}
            className="shrink-0 flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider"
          >
            <RefreshCw size={10} />
            {t('Retry')}
          </button>
        </div>
      )}

      {status === 'done' && hits.length === 0 && remoteStatus !== 'loading' && (
        <p className="text-[10px] font-mono text-zinc-500 shrink-0">
          {query.trim()
            ? (remoteStatus === 'done' && remoteHits.length === 0 && !remoteError
                ? t('No results for "{query}" — nothing local matches and YouTube found nothing either.', { query })
                : t('No results for "{query}" — nothing archived matches yet.', { query }))
            : t('No archived messages from {users}.', { users: userFilter.trim().split(',').map((u) => `@${u.trim()}`).join(', ') })}
        </p>
      )}

      {/* Result count + coverage note. Every hit is a partial word match
          (no row contains all query words) — say so instead of presenting
          fuzzy noise as exact. */}
      {status === 'done' && displayHits.length > 0 && (
        <div className="flex items-center justify-between gap-2 shrink-0">
          <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">
            {displayHits.length} {displayHits.length === 1 ? t('result') : t('results')}
            {remoteStatus === 'done' && remoteHits.length > 0 && (
              <span className="text-zinc-600"> · +{remoteHits.length} YouTube</span>
            )}
          </span>
          <span className="flex items-center gap-1.5 shrink-0">
            {!displayHits.some((h) => !h.partial) && (
              <span className="text-[9px] font-mono uppercase tracking-widest text-amber-500/80">
                {t('closest matches')}
              </span>
            )}
            <button
              type="button"
              onClick={retrySearch}
              title={t('Refresh search')}
              aria-label={t('Refresh search')}
              className="text-zinc-500 hover:text-white p-0.5 shrink-0"
            >
              <RefreshCw size={11} />
            </button>
          </span>
        </div>
      )}

      {/* ── HITS — incremental: only `visibleCount` rows are mounted; the
          scroll handler reveals the next chunk so an uncapped literal search
          stays smooth. ── */}
      {displayHits.length > 0 && (
        <div
          ref={hitsScrollRef}
          onScroll={() => {
            const el = hitsScrollRef.current;
            if (!el) return;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 160) {
              setVisibleCount((c) => Math.min(displayHits.length, c + HITS_RENDER_CHUNK));
            }
          }}
          className="flex flex-col gap-1.5 overflow-y-auto custom-scrollbar pr-1 min-h-0 flex-1"
        >
          {displayHits.slice(0, visibleCount).map((hit, idx) => {
            const video = videos[`${(hit.platform || '').toLowerCase()}:${hit.video_id}`];
            const relativeDate = formatRelativeDate(hit.date);
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
            const isActive = activeIdx === idx;
            return (
              <div key={`${hit.kind}:${hit.platform}:${hit.video_id}:${hit.offset_sec}`} className="flex items-stretch gap-1">
                <button
                  ref={(el) => { hitRefs.current[idx] = el; }}
                  type="button"
                  onClick={() => selectHit(hit)}
                  aria-current={isActive ? 'true' : undefined}
                  className={`text-left border-2 p-1.5 flex flex-col gap-1 flex-1 min-w-0 transition-colors ${
                    isSelected
                      ? 'border-white bg-zinc-900'
                      : isActive
                        ? 'border-yellow-300/70 bg-zinc-900'
                        : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-500'
                  }`}
                >
                <span className="flex items-center gap-1.5 min-w-0">
                  {hit.kind === 'transcript' || hit.kind === 'title'
                    ? <FileText size={10} className="text-zinc-400 shrink-0" />
                    : <MessageSquare size={10} className="text-zinc-400 shrink-0" />}
                  <span className="text-[8px] font-mono uppercase tracking-widest border border-zinc-700 px-1 py-px text-zinc-300 shrink-0">
                    {KIND_BADGE_LABEL[hit.kind] ? t(KIND_BADGE_LABEL[hit.kind]) : hit.kind}
                  </span>
                  {hit.kind === 'transcript' && hit.lang && (
                    <span className="text-[8px] font-mono uppercase tracking-widest border border-zinc-700 px-1 py-px text-zinc-500 shrink-0">
                      {ARCHIVE_LANG_LABELS[hit.lang] ?? hit.lang}
                    </span>
                  )}
                  {hit.semantic && (
                    <span className="text-[8px] font-mono uppercase tracking-widest border border-cyan-700 text-cyan-400 px-1 py-px shrink-0">
                      CTX
                    </span>
                  )}
                  {hitPlatforms(hit).map((p) => (
                    <PlatformVodIcon key={p} platform={PLATFORM_ICON_NAME[p] ?? p} className="w-3.5 h-3.5" />
                  ))}
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
                  {relativeDate && (
                    <span
                      title={hit.date ? new Date(hit.date).toLocaleString() : undefined}
                      className="text-[9px] font-mono text-zinc-500 shrink-0"
                    >
                      {relativeDate}
                    </span>
                  )}
                </span>
                <span className="text-[10px] leading-snug text-zinc-400 break-words">
                  {hit.kind === 'message' && hit.author ? (
                    <span className="text-yellow-200/80 mr-1 shrink-0">{hit.author}:</span>
                  ) : null}
                  {(hit.channel ?? video?.channel) ? (
                    <span className="text-zinc-500 mr-1">@{(hit.channel ?? video?.channel)}</span>
                  ) : null}
                  {nodes}
                </span>
                </button>
                {onSeekHit && buildArchiveVodUrl(hit.platform, hit.video_id, video?.channel) && (
                  <button
                    type="button"
                    onClick={() => openHit(hit, video)}
                    title={t('Open in player')}
                    aria-label={t('Open {title} in player', { title: videoTitle(video, hit) })}
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
        <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-1.5 min-h-0 flex-none max-h-[38%]">
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">
              {t('YouTube results')}{remoteYtHandle ? ` · @${remoteYtHandle}` : ''}
              {remoteStatus === 'done' && remoteHits.length > 0 && (
                <span className="text-zinc-600"> · {remoteHits.length}</span>
              )}
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
              {remoteError ?? t('No YouTube matches for "{query}"', { query })}
            </p>
          )}
          {remoteStatus === 'done' && remoteHits.length > 0 && (
            <div className="flex flex-col gap-1 overflow-y-auto custom-scrollbar pr-1 min-h-0 flex-1">
              {remoteHits.map((hit) => (
                <button
                  key={hit.video_id}
                  type="button"
                  onClick={() => openRemoteHit(hit)}
                  title={t('Open in the player — download from there')}
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
                    @{hit.channel ?? remoteYtHandle} — {t('click to open in the player')}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── CHAT FROM HIT — the whole remaining history, scrollable ── */}
      {selected && (
        <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-2 min-h-0 flex-none max-h-[38%]">
          <div className="flex items-center justify-between gap-2 shrink-0">
            <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500">
              {t('Chat from hit')}
            </span>
            <span className="flex items-center gap-2 min-w-0">
              <span
                className="text-[9px] font-mono text-zinc-400 truncate"
                title={`${videoTitle(selected.video, selected.hit)} @ ${formatArchiveOffset(selected.hit.offset_sec)}`}
              >
                {videoTitle(selected.video, selected.hit)} @ {formatArchiveOffset(selected.hit.offset_sec)}
              </span>
              <button
                type="button"
                onClick={() => setSelected(null)}
                title={t('Close')}
                className="text-zinc-500 hover:text-white p-0.5 shrink-0"
              >
                <X size={12} />
              </button>
            </span>
          </div>
          {chatPlatforms.length > 1 && (
            <div className="flex items-center gap-1 flex-wrap shrink-0" role="group" aria-label={t('Platform')}>
              {chatPlatforms.map((p) => {
                const visible = !chatHidden.has(p);
                const lastVisible =
                  visible && chatPlatforms.filter((q) => !chatHidden.has(q)).length <= 1;
                return (
                  <button
                    key={p}
                    type="button"
                    aria-pressed={visible}
                    disabled={lastVisible}
                    onClick={() => {
                      setChatHidden((prev) => {
                        const next = new Set(prev);
                        if (next.has(p)) next.delete(p);
                        else next.add(p);
                        return next;
                      });
                    }}
                    title={t('Show/hide {platform} chat', { platform: p })}
                    className={`flex items-center gap-1 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold border transition-colors disabled:opacity-40 ${
                      visible
                        ? 'bg-white text-black border-white'
                        : 'border-zinc-700 text-zinc-500 hover:border-white hover:text-white'
                    }`}
                  >
                    <PlatformVodIcon platform={PLATFORM_ICON_NAME[p] ?? p} className="w-3 h-3" />
                    {p}
                  </button>
                );
              })}
            </div>
          )}
          {chatStatus === 'loading' && (
            <div className="flex items-center gap-1.5 text-zinc-500 text-[10px] font-mono shrink-0">
              <Loader2 size={11} className="animate-spin" />
              {t('Loading chat history...')}
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
                {t('Retry')}
              </button>
            </div>
          )}
          {chatStatus === 'done' && selected && chat && (() => {
            // Client-side platform filter: hidden-platform rows are still
            // fetched (pagination stays group-consistent), just not shown.
            const visibleChat = chat.filter((m) => !chatHidden.has(m.platform));
            return (
            <div
              ref={chatScrollRef}
              onScroll={() => {
                const el = chatScrollRef.current;
                if (!el) return;
                if (el.scrollTop + el.clientHeight >= el.scrollHeight - 160) {
                  setChatVisibleCount((c) => Math.min(visibleChat.length, c + CHAT_RENDER_CHUNK));
                  if (chatTruncated && chatStatus === 'done') {
                    // Tail still pending: pull the next page (serialized by
                    // chatFetchingRef, appended + deduped in fetchChatPage).
                    void fetchChatPage(chatGenRef.current);
                  }
                }
              }}
              className="flex flex-col gap-0.5 overflow-y-auto custom-scrollbar pr-1 min-h-0 flex-1"
            >
              {visibleChat.length === 0 && (
                <p className="text-[10px] font-mono text-zinc-500">{t('No archived chat from this moment.')}</p>
              )}
              {visibleChat.length > 0 && (
                <div className="flex items-center gap-1.5 my-0.5 shrink-0">
                  <span className="h-px flex-1 bg-yellow-300/60" />
                  <span className="text-[8px] font-mono uppercase tracking-widest text-yellow-300 bg-yellow-300/10 border border-yellow-300/40 px-1 py-px">
                    {t('Hit moment {offset}', { offset: formatArchiveOffset(selected.hit.offset_sec) })}
                  </span>
                  <span className="h-px flex-1 bg-yellow-300/60" />
                </div>
              )}
              {visibleChat.slice(0, chatVisibleCount).map((m) => (
                <p
                  key={`c:${m.platform}:${m.offset_sec}:${m.username}:${m.text}`}
                  onClick={onSeekOffset ? () => seekToTimestamp(m.offset_sec, onSeekOffset) : undefined}
                  title={onSeekOffset ? t('Seek to {offset}', { offset: formatArchiveOffset(m.offset_sec) }) : undefined}
                  className={`text-[10px] leading-snug text-zinc-200 break-words ${onSeekOffset ? 'cursor-pointer select-none' : ''}`}
                >
                  {chatPlatforms.length > 1 && (
                    <PlatformVodIcon platform={PLATFORM_ICON_NAME[m.platform] ?? m.platform} className="w-2.5 h-2.5 inline-block align-baseline mr-1 opacity-70" />
                  )}
                  <span className="text-zinc-400 font-mono mr-1">{formatArchiveOffset(m.offset_sec)}</span>
                  <span className="font-bold" style={{ color: resolveChatColor(m.color, m.username, m.platform) }}>{m.username}:</span> {m.text}
                  {typeof m.spam_count === 'number' && m.spam_count > 1 && (
                    <span className="text-[9px] font-mono text-zinc-500 ml-1" title={t('{count} identical messages collapsed', { count: m.spam_count })}>
                      ×{m.spam_count}
                    </span>
                  )}
                </p>
              ))}
              {chatTruncated && (
                <p className="text-[9px] font-mono text-zinc-600 shrink-0" data-chat-truncated>
                  {t('Chat history continues — scroll to load more ({loaded} messages loaded).', { loaded: chat.length })}
                </p>
              )}
            </div>
            );
          })()}
        </div>
      )}
      </div>{/* /RESULTS REGION */}
      {!embedded && (
        <PanelResizeHandles onPointerDown={onResizeStart} />
      )}
    </div>
  );
}

export default ArchiveSearchPopup;
