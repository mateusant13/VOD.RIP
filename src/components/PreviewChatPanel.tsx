/**
 * WS-2 preview chat panel — right-side collapsible panel on the preview
 * surface with Chat / Transcript / Subtitles tabs, synced to playback time.
 * URL-only YouTube previews (no archive row: no transcript, no chat) render
 * subtitles-only: the panel fetches the video's own captions (en/pt/es,
 * manual preferred over auto) from /api/subtitles instead of offering chat
 * or transcription.
 *
 * Performance contract (acceptance #6):
 *  - All panel state (open/tab/width/data) lives INSIDE this component, so
 *    toggling tabs, collapsing, resizing or loading rows never re-renders
 *    the player (App.tsx only re-renders on the pre-existing previewTimeUi
 *    throttle, ~4 Hz — not per frame).
 *  - Rows are memoized and the rendered list is a ±WINDOW slice around the
 *    active row (fixed row heights + spacer divs), so a 100k-row chat never
 *    mounts more than ~300 DOM rows.
 *  - The panel's width resize is self-contained (rAF + pointer capture +
 *    direct style writes, state commit on pointerup) — it deliberately does
 *    NOT call startExplorePanelWidthResize/startFloatingPanelDrag (WS-9
 *    owns those shared helpers).
 */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Captions,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  FileText,
  Loader2,
  MessageSquare,
  RefreshCw,
  Search,
  X,
} from 'lucide-react';
import { apiGet } from '../hooks/useApiClient';
import { activePanelRowIndex } from '../previewPlayerUtils';
import { formatArchiveOffset } from '../archiveSearchUtils';
import { resolveChatColor } from '../chatColors';
import { seekToTimestamp } from '../seekToTimestamp';

export interface PreviewPanelTranscriptRow {
  offset_sec: number;
  text: string;
}
export interface PreviewPanelChatRow {
  offset_sec: number;
  text: string;
  username: string;
  spam_count: number;
  /** Platform-provided username color (#RRGGBB); null = palette fallback. */
  color?: string | null;
}
/** PANNs acoustic detection (LAUGH, CLAP, ...) with real boundaries. */
export interface PreviewPanelEventRow {
  offset_sec: number;
  end_sec: number;
  event: string;
  score: number;
}
export interface PreviewPanelPayload {
  transcript: PreviewPanelTranscriptRow[];
  chat: PreviewPanelChatRow[];
  events: PreviewPanelEventRow[];
  has_transcript: boolean;
  has_chat: boolean;
  /** Twitch-chat backfill status for the panel envelope (absent on
   *  Kick/YouTube/archived payloads): 'running' → the backend is filling
   *  chat in the background and the panel polls; 'done' → archive complete;
   *  'idle' → nothing will come. */
  backfill?: 'idle' | 'running' | 'done';
  /** 0..1 progress of the in-flight backfill (row-count estimate). */
  backfill_progress?: number;
  /** Total chat rows in the archive for this video (the returned chat may
   *  be a bounded playhead window of it while the backfill runs). */
  total_rows?: number;
}

/** Live YouTube captions for URL-only previews (no archive row). */
export interface PreviewSubtitlesPayload {
  url: string;
  lang: string | null;
  source: 'manual' | 'auto' | null;
  has_subtitles: boolean;
  rows: PreviewPanelTranscriptRow[];
}

/** One row of the Transcript-tab timeline: a transcript segment or an
 *  acoustic event, merged chronologically by offset_sec. */
type TimelineRow =
  | ({ kind: 'transcript' } & PreviewPanelTranscriptRow)
  | ({ kind: 'event' } & PreviewPanelEventRow);

export type PreviewPanelTab = 'chat' | 'transcript' | 'subtitles';

const PANEL_MIN_W = 220;
const PANEL_MAX_W = 560;
const PANEL_DEFAULT_W = 320;
const PANEL_W_KEY = 'vodrip.preview.chatPanelWidth';
/** Width the collapsed strip occupies (matches the w-7 button). */
const PANEL_STRIP_W = 28;
/** Matches the backend default; 12h+ of dense chat stays under this cap. */
const PANEL_LIMIT = 200_000;
/** While a Twitch backfill is 'running' the panel refreshes at this rate
 *  (the backend bounds each response to a playhead window, so polling stays
 *  cheap and chat appears progressively instead of after the whole run). */
const PANEL_POLL_MS = 2500;
const CHAT_ROW_H = 24;
const TRANSCRIPT_ROW_H = 22;
/** Rows rendered on each side of the active row (fixed-height virtualization). */
const WINDOW = 150;
const EMPTY_TRANSCRIPT: PreviewPanelTranscriptRow[] = [];
const EMPTY_CHAT: PreviewPanelChatRow[] = [];
const EMPTY_EVENTS: PreviewPanelEventRow[] = [];

/** Persisted panel width (stored on drag commit). The rendered width may be
 *  clamped below it by the host's `maxWidth`; the stored value resurfaces
 *  when the host has room again. */
export function readPreviewChatPanelWidth(): number {
  try {
    const w = Number(localStorage.getItem(PANEL_W_KEY));
    return w >= PANEL_MIN_W && w <= PANEL_MAX_W ? w : PANEL_DEFAULT_W;
  } catch {
    return PANEL_DEFAULT_W;
  }
}

interface PreviewChatPanelProps {
  platform: string | null;
  videoId: string | null;
  currentTime: number;
  /** Click-to-seek: the host's CURRENT-player seek (main preview or the
   *  popup's own player). When provided, chat/transcript/event rows and the
   *  subtitle caption become clickable and seek to the row's offset_sec.
   *  Absent → rows keep the scroll-only behavior (no player to seek). */
  onSeek?: (offsetSec: number) => void;
  /** True hides the panel (fullscreen) while keeping its state mounted. */
  hidden?: boolean;
  /** Live-captions gate: while false the panel does NOT fetch the video's
   *  own YouTube subtitles (URL-only previews only), so the preview's first
   *  bytes go to video playback instead of racing that network work. The
   *  host flips it on the player's canplay signal; defaults to true for
   *  callers without a player (tests, non-preview hosts). The ARCHIVE
   *  payload (chat/transcript) deliberately ignores this gate — it starts
   *  at session-create so a Twitch backfill kicks off as early as possible;
   *  the video-first PLAYBACK gate lives in the hosts and is untouched. */
  started?: boolean;
  /** Initial open state. The explore popup opens collapsed (small strip)
   *  so the mini preview stays player-sized by default; the main preview
   *  keeps the panel open. */
  defaultOpen?: boolean;
  /** Cap on the rendered width. The host reserves player space so the video
   *  never drops below its layout minimum; below PANEL_MIN_W there is no room
   *  at all and the panel collapses to zero width (no strip). Defaults to the
   *  panel's own max (no cap). */
  maxWidth?: number;
  /** Reports the space the panel actually occupies: width = rendered width
   *  (open), strip width (collapsed), or 0 (space-forced). The explore popup
   *  sizes its container from this. */
  onLayoutChange?: (info: { open: boolean; width: number }) => void;
}

const TABS: ReadonlyArray<{
  id: PreviewPanelTab;
  label: string;
  icon: typeof MessageSquare;
}> = [
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'transcript', label: 'Transcript', icon: FileText },
  { id: 'subtitles', label: 'Subtitles', icon: Captions },
];

const ChatRow = memo(function ChatRow({
  row,
  active,
  platform,
  onSeek,
  ref,
}: {
  row: PreviewPanelChatRow;
  active: boolean;
  platform: string | null;
  onSeek?: (offsetSec: number) => void;
  ref?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={ref}
      data-panel-row
      aria-current={active ? 'true' : undefined}
      style={{ height: CHAT_ROW_H }}
      onClick={onSeek ? () => seekToTimestamp(row.offset_sec, onSeek) : undefined}
      title={onSeek ? `Seek to ${formatArchiveOffset(row.offset_sec)}` : undefined}
      className={`flex items-baseline gap-1 px-2 overflow-hidden border-l-2 whitespace-nowrap ${
        active
          ? 'bg-yellow-300/10 border-yellow-300 text-zinc-100'
          : 'border-transparent text-zinc-300 hover:bg-zinc-900/70'
      } ${onSeek ? 'cursor-pointer select-none' : ''}`}
    >
      <span className="text-zinc-400 font-mono text-[9px] shrink-0">
        {formatArchiveOffset(row.offset_sec)}
      </span>
      <span
        className="font-bold text-[10px] shrink-0"
        style={{ color: resolveChatColor(row.color, row.username, platform) }}
      >
        {row.username}:
      </span>
      <span className="text-[10px] leading-snug truncate" title={row.text}>
        {row.text}
      </span>
      {typeof row.spam_count === 'number' && row.spam_count > 1 && (
        <span
          className="text-[9px] font-mono text-zinc-400 shrink-0"
          title={`${row.spam_count} identical messages collapsed`}
        >
          ×{row.spam_count}
        </span>
      )}
    </div>
  );
});

const TranscriptRow = memo(function TranscriptRow({
  row,
  active,
  onSeek,
  ref,
}: {
  row: PreviewPanelTranscriptRow;
  active: boolean;
  onSeek?: (offsetSec: number) => void;
  ref?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={ref}
      data-panel-row
      aria-current={active ? 'true' : undefined}
      style={{ height: TRANSCRIPT_ROW_H }}
      onClick={onSeek ? () => seekToTimestamp(row.offset_sec, onSeek) : undefined}
      title={onSeek ? `Seek to ${formatArchiveOffset(row.offset_sec)}` : undefined}
      className={`flex items-baseline gap-1 px-2 overflow-hidden border-l-2 whitespace-nowrap ${
        active
          ? 'bg-yellow-300/10 border-yellow-300 text-zinc-100'
          : 'border-transparent text-zinc-300 hover:bg-zinc-900/70'
      } ${onSeek ? 'cursor-pointer select-none' : ''}`}
    >
      <span className="text-zinc-400 font-mono text-[9px] shrink-0">
        {formatArchiveOffset(row.offset_sec)}
      </span>
      <span className="text-[10px] leading-snug truncate" title={row.text}>
        {row.text}
      </span>
    </div>
  );
});

/** Acoustic-event row: amber LABEL + duration, interleaved with transcript
 *  segments by offset_sec; tooltip carries the exact range + confidence. */
const EventRow = memo(function EventRow({
  row,
  active,
  ref,
}: {
  row: PreviewPanelEventRow;
  active: boolean;
  ref?: React.Ref<HTMLDivElement>;
}) {
  const durSec = Math.max(0, row.end_sec - row.offset_sec);
  return (
    <div
      ref={ref}
      data-panel-row
      data-event-row={row.event}
      aria-current={active ? 'true' : undefined}
      style={{ height: TRANSCRIPT_ROW_H }}
      title={`${row.event} — ${formatArchiveOffset(row.offset_sec)} to ${formatArchiveOffset(row.end_sec)} (${durSec.toFixed(1)}s, confidence ${(row.score * 100).toFixed(0)}%)`}
      className={`flex items-baseline gap-1 px-2 overflow-hidden border-l-2 whitespace-nowrap ${
        active
          ? 'bg-amber-300/15 border-amber-300 text-amber-200'
          : 'border-transparent text-amber-300/70'
      }`}
    >
      <span className="text-zinc-600 font-mono text-[9px] shrink-0">
        {formatArchiveOffset(row.offset_sec)}
      </span>
      <span className="font-bold text-[9px] uppercase tracking-widest shrink-0">
        {row.event}
      </span>
      <span className="text-[9px] font-mono text-zinc-500 shrink-0">({durSec.toFixed(1)}s)</span>
      <span className="flex-1" />
      <span className="text-[8px] font-mono text-zinc-600 shrink-0">
        {(row.score * 100).toFixed(0)}%
      </span>
    </div>
  );
});

function EmptyState({ text }: { text: string }) {
  return (
    <div className="flex-1 min-h-0 flex items-center justify-center px-4" data-panel-empty>
      <p className="text-[10px] font-mono text-zinc-500 text-center leading-relaxed">{text}</p>
    </div>
  );
}

export function PreviewChatPanel({
  platform,
  videoId,
  currentTime,
  hidden = false,
  started = true,
  defaultOpen = true,
  maxWidth: maxWidthProp,
  onLayoutChange,
}: PreviewChatPanelProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [tab, setTab] = useState<PreviewPanelTab>('chat');
  const [width, setWidth] = useState<number>(readPreviewChatPanelWidth);
  const [payload, setPayload] = useState<PreviewPanelPayload | null>(null);
  const [fetchState, setFetchState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [retryTick, setRetryTick] = useState(0);
  /** Bumped by the backfill poll loop (and the one refresh after 'done')
   *  to re-run the payload fetch without touching the cache. */
  const [pollTick, setPollTick] = useState(0);
  const [ytSubtitles, setYtSubtitles] = useState<PreviewSubtitlesPayload | null>(null);
  const [subsFetchState, setSubsFetchState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  /** Inline chat-history search (filter + prev/next cursor), see below. */
  const [chatQuery, setChatQuery] = useState('');
  const [searchIdx, setSearchIdx] = useState(-1);

  const payloadCacheRef = useRef<Map<string, PreviewPanelPayload>>(new Map());
  const subsCacheRef = useRef<Map<string, PreviewSubtitlesPayload>>(new Map());
  /** Last seen backfill status, to detect the running→done transition (the
   *  panel refreshes once more so the final poll also carries the complete
   *  archive). */
  const lastBackfillRef = useRef<string | null>(null);
  /** Playhead read at fetch time (never a fetch dependency — currentTime
   *  changes ~4 Hz and must not re-trigger requests; the panel syncs locally
   *  while seeking). */
  const currentTimeRef = useRef(currentTime);
  currentTimeRef.current = currentTime;
  const userPickedTabRef = useRef(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const activeRowRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const autoScrollingRef = useRef(false);
  const activeIdxRef = useRef(-1);
  const rowHRef = useRef(CHAT_ROW_H);
  const prevActiveIdxRef = useRef<number | null>(null);

  // ── Data ----------------------------------------------------------------
  const payloadKey = platform && videoId ? `${platform}/${videoId}` : '';
  useEffect(() => {
    if (!payloadKey) {
      setPayload(null);
      setFetchState('done');
      return;
    }
    // Cache hits only apply to the initial load — polls (pollTick > 0) must
    // always hit the network to see the backfill's progress.
    const cached = payloadCacheRef.current.get(payloadKey);
    if (cached && pollTick === 0) {
      setPayload(cached);
      setFetchState('done');
      return;
    }
    // The archive payload starts at session-create (as early as the key
    // exists), NOT on canplay: a Twitch VOD's chat backfill must kick off
    // before playback so near-playhead chat is already archived when the
    // video starts. The video-first PLAYBACK gate is host-side and
    // untouched. offset_sec seeds the backfill at the playhead (read from
    // the ref so playhead motion never re-triggers this effect) and centers
    // the backend's bounded chat window while the backfill runs.
    let cancelled = false;
    setFetchState('loading');
    const offset = Math.max(0, currentTimeRef.current);
    apiGet<PreviewPanelPayload>(
      `/api/preview/panel/${payloadKey}?limit=${PANEL_LIMIT}&offset_sec=${offset}`,
    )
      .then((p) => {
        if (cancelled) return;
        const cache = payloadCacheRef.current;
        cache.set(payloadKey, p);
        if (cache.size > 8) {
          // ponytail: bounded in-memory panel cache (LRU-ish by insertion
          // order); upgrade path: move the payloads to IndexedDB if preview
          // sessions ever reopen >8 distinct videos per app run.
          const oldest = cache.keys().next().value;
          if (oldest !== undefined) cache.delete(oldest);
        }
        setPayload(p);
        setFetchState('done');
      })
      .catch(() => {
        if (!cancelled) setFetchState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [payloadKey, retryTick, pollTick]);

  // While a Twitch backfill is 'running', refresh every PANEL_POLL_MS so
  // chat appears progressively (the backend bounds each response to a
  // playhead window, so polls stay cheap). Errors do not stop the loop —
  // the next tick retries; the loop ends when the status leaves 'running'.
  const backfillRunning = payload?.backfill === 'running';
  useEffect(() => {
    if (!backfillRunning) return;
    const t = window.setTimeout(() => setPollTick((n) => n + 1), PANEL_POLL_MS);
    return () => window.clearTimeout(t);
  }, [backfillRunning, pollTick]);

  // One extra refresh on the running→done transition: the final poll should
  // carry the complete archive (the run may have finished between polls).
  // The tracker resets on video switch so a cross-video transition never
  // fires.
  useEffect(() => {
    lastBackfillRef.current = null;
  }, [payloadKey]);
  useEffect(() => {
    const prev = lastBackfillRef.current;
    lastBackfillRef.current = payload?.backfill ?? null;
    if (prev === 'running' && payload?.backfill === 'done') {
      setPollTick((n) => n + 1);
    }
  }, [payloadKey, payload?.backfill]);

  // Default tab: land on whichever source actually exists (first open only).
  // While a Twitch backfill is 'running' the chat tab stays put (it shows a
  // loading indicator and fills in) instead of bouncing to the transcript.
  useEffect(() => {
    if (userPickedTabRef.current || fetchState !== 'done' || !payload) return;
    if (tab === 'chat' && !payload.has_chat && payload.backfill !== 'running' && payload.has_transcript)
      setTab('transcript');
    else if (tab === 'transcript' && !payload.has_transcript && payload.has_chat) setTab('chat');
  }, [payload, tab, fetchState]);

  // URL-only YouTube previews (no archive transcript/chat rows): the panel
  // is subtitles-only — fetch the video's own captions (en/pt/es, manual
  // preferred over auto) instead of offering chat or transcription, which
  // a bare URL has no archive data for.
  const subtitlesOnly =
    platform === 'youtube' && !!payload && !payload.has_transcript && !payload.has_chat;
  useEffect(() => {
    if (!subtitlesOnly || !videoId) {
      setYtSubtitles(null);
      setSubsFetchState('idle');
      return;
    }
    const cached = subsCacheRef.current.get(videoId);
    if (cached) {
      setYtSubtitles(cached);
      setSubsFetchState('done');
      return;
    }
    // Video-first gate (same as the panel fetch above): URL-only previews
    // fetch YouTube captions over the network — wait for canplay.
    if (!started) {
      setSubsFetchState('idle');
      return;
    }
    let cancelled = false;
    setSubsFetchState('loading');
    const watchUrl = `https://www.youtube.com/watch?v=${videoId}`;
    apiGet<PreviewSubtitlesPayload>(`/api/subtitles?url=${encodeURIComponent(watchUrl)}&langs=en,pt,es`)
      .then((p) => {
        if (cancelled) return;
        subsCacheRef.current.set(videoId, p);
        setYtSubtitles(p);
        setSubsFetchState('done');
      })
      .catch(() => {
        if (!cancelled) setSubsFetchState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [subtitlesOnly, videoId, retryTick, started]);

  // Subtitles are a YouTube feature: the caption display is only offered for
  // YouTube videos (URL-only previews fetch live captions; archived ones show
  // their transcript). Twitch/Kick VODs and clips get the Transcript tab (the
  // archive's auto-transcript) but no Subtitles tab.
  const subtitlesTabEnabled = platform === 'youtube';
  // Subtitles-only previews have no chat/transcript tabs to land on.
  useEffect(() => {
    if (subtitlesOnly && tab !== 'subtitles') setTab('subtitles');
  }, [subtitlesOnly, tab]);
  // Stale tab across video switches: a non-YouTube video must never keep the
  // Subtitles tab selected (its tab is hidden for that platform).
  useEffect(() => {
    if (!subtitlesTabEnabled && tab === 'subtitles') setTab('chat');
  }, [subtitlesTabEnabled, tab]);

  // ── Rows / active index ---------------------------------------------------
  const chatRows = useMemo(() => payload?.chat ?? EMPTY_CHAT, [payload]);
  const transcriptRows = useMemo(() => payload?.transcript ?? EMPTY_TRANSCRIPT, [payload]);
  /** Chat-tab list: the full history, or only rows matching the inline search
   *  query (case-insensitive on username and text). */
  const chatList = useMemo(() => {
    const q = chatQuery.trim().toLowerCase();
    if (!q) return chatRows;
    return chatRows.filter(
      (r) => r.username.toLowerCase().includes(q) || r.text.toLowerCase().includes(q),
    );
  }, [chatRows, chatQuery]);
  const chatOffsets = useMemo(() => chatList.map((r) => r.offset_sec), [chatList]);
  /** The search is live only on the chat tab with a non-empty query. */
  const qActive = chatQuery.trim().length > 0 && tab === 'chat';
  // Transcript-tab timeline: transcript segments + acoustic events merged in
  // chronological order (ties put the segment first — an event usually starts
  // at a segment boundary). Raw transcriptRows/offsets stay separate for the
  // Subtitles tab, which must index into segments only.
  const timelineRows = useMemo(() => {
    const rows: TimelineRow[] = transcriptRows.map((r) => ({ kind: 'transcript', ...r }));
    for (const e of payload?.events ?? EMPTY_EVENTS) {
      rows.push({ kind: 'event', ...e });
    }
    rows.sort(
      (a, b) => a.offset_sec - b.offset_sec || (a.kind === 'transcript' ? -1 : 1),
    );
    return rows;
  }, [transcriptRows, payload]);
  const timelineOffsets = useMemo(() => timelineRows.map((r) => r.offset_sec), [timelineRows]);
  const activeChatIdx = useMemo(
    () => activePanelRowIndex(chatOffsets, currentTime),
    [chatOffsets, currentTime],
  );
  const activeChatIdxRef = useRef(-1);
  activeChatIdxRef.current = activeChatIdx;
  const activeTimelineIdx = useMemo(
    () => activePanelRowIndex(timelineOffsets, currentTime),
    [timelineOffsets, currentTime],
  );
  // Subtitles-tab rows: the archive transcript for archived YouTube videos,
  // the live-fetched YouTube captions for URL-only previews. Non-YouTube
  // platforms have no Subtitles tab (transcript lives in the Transcript tab).
  const subtitleRows = subtitlesTabEnabled
    ? (subtitlesOnly ? (ytSubtitles?.rows ?? EMPTY_TRANSCRIPT) : transcriptRows)
    : EMPTY_TRANSCRIPT;
  const subtitleOffsets = useMemo(() => subtitleRows.map((r) => r.offset_sec), [subtitleRows]);
  const activeSubtitleIdx = useMemo(
    () => activePanelRowIndex(subtitleOffsets, currentTime),
    [subtitleOffsets, currentTime],
  );

  const list = tab === 'chat' ? chatList : timelineRows;
  const rowH = tab === 'chat' ? CHAT_ROW_H : TRANSCRIPT_ROW_H;
  const activeIdx = tab === 'chat' ? activeChatIdx : activeTimelineIdx;
  activeIdxRef.current = activeIdx;
  rowHRef.current = rowH;
  /** Row the view centers on: the search cursor while a query is active,
   *  the playback-synced row otherwise. */
  const focusIdx = qActive && searchIdx >= 0 ? searchIdx : activeIdx;

  // Fixed-height virtualization: window of WINDOW rows around the focus row
  // with spacer divs above/below, so scroll height stays exact (the focus
  // row's content offset is always focusIdx * rowH).
  const windowStart = useMemo(() => {
    const maxStart = Math.max(0, list.length - 2 * WINDOW - 1);
    return Math.max(0, Math.min(focusIdx - WINDOW, maxStart));
  }, [list.length, focusIdx]);
  const windowEnd = Math.min(list.length, windowStart + 2 * WINDOW + 1);
  const slice = useMemo(() => list.slice(windowStart, windowEnd), [list, windowStart, windowEnd]);
  const topPad = windowStart * rowH;
  const bottomPad = (list.length - windowEnd) * rowH;

  // ── Playback sync (seek + play) ------------------------------------------
  // Re-arm follow on tab switch: the new list needs its active row scrolled
  // into view even when the index happens to be unchanged.
  useEffect(() => {
    prevActiveIdxRef.current = null;
    followRef.current = true;
  }, [tab]);

  useEffect(() => {
    if (fetchState !== 'done' || qActive) return;
    if (prevActiveIdxRef.current === activeIdx) return;
    prevActiveIdxRef.current = activeIdx;
    const el = scrollRef.current;
    if (!el || !followRef.current) return;
    if (activeIdx < 0) {
      el.scrollTop = 0;
      return;
    }
    autoScrollingRef.current = true;
    activeRowRef.current?.scrollIntoView({ block: 'nearest' });
  }, [activeIdx, fetchState, qActive]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (autoScrollingRef.current) {
      autoScrollingRef.current = false;
      return;
    }
    // The active row's content offset is fixed (activeIdx * rowH). When the
    // user scrolls more than 1.5 viewports away from it, they are reading
    // elsewhere — stop auto-following until they click a row / switch tab.
    const activeTop = activeIdxRef.current * rowHRef.current;
    if (Math.abs(el.scrollTop - activeTop) > el.clientHeight * 1.5) {
      followRef.current = false;
    }
  }, []);

  const handleRowAreaClick = useCallback((e: React.MouseEvent) => {
    const target = (e.target as HTMLElement).closest('[data-panel-row]') as HTMLElement | null;
    if (!target) return;
    followRef.current = true;
    target.scrollIntoView({ block: 'nearest' });
  }, []);

  // ── Inline chat search -----------------------------------------------------
  // Activating the query jumps the cursor to the match nearest playback time
  // (the filtered list's active row); clearing it re-arms playback follow.
  useEffect(() => {
    if (qActive) {
      setSearchIdx(activeChatIdxRef.current >= 0 ? activeChatIdxRef.current : 0);
    } else {
      setSearchIdx(-1);
      prevActiveIdxRef.current = null;
    }
  }, [qActive]);

  // Keep the cursor in range while typing shrinks the match set.
  useEffect(() => {
    if (!qActive || searchIdx < 0) return;
    if (chatList.length === 0) {
      if (searchIdx !== -1) setSearchIdx(-1);
    } else if (searchIdx >= chatList.length) {
      setSearchIdx(Math.max(0, chatList.length - 1));
    }
  }, [qActive, chatList.length, searchIdx]);

  // Scroll the cursor row into view (exact content offset: fixed row height).
  useEffect(() => {
    if (!qActive || searchIdx < 0) return;
    const el = scrollRef.current;
    if (!el) return;
    autoScrollingRef.current = true;
    el.scrollTop = searchIdx * CHAT_ROW_H;
  }, [qActive, searchIdx]);

  const stepSearch = useCallback(
    (dir: 1 | -1) => {
      setSearchIdx((i) => {
        const n = chatList.length;
        if (n === 0) return -1;
        if (i < 0) return dir > 0 ? 0 : n - 1;
        return (i + dir + n) % n;
      });
    },
    [chatList.length],
  );

  // ── Self-contained width resize (rAF + pointer capture + direct writes) ---
  const widthCap = Math.min(PANEL_MAX_W, maxWidthProp ?? PANEL_MAX_W);
  const spaceForced = open && widthCap < PANEL_MIN_W;
  const renderedW = spaceForced ? 0 : open ? Math.min(width, widthCap) : PANEL_STRIP_W;
  const widthRef = useRef(renderedW);
  widthRef.current = renderedW;

  // The host (main preview / explore popup) reserves player space so the
  // video never drops below its layout minimum — report what we actually
  // occupy so the popup can size its container.
  useEffect(() => {
    onLayoutChange?.({ open: open && !spaceForced, width: renderedW });
  }, [onLayoutChange, open, spaceForced, renderedW]);

  const onResizeStart = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const el = panelRef.current;
    if (!el) return;
    const startX = e.clientX;
    const startW = widthRef.current;
    try {
      el.setPointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
    let raf = 0;
    const onMove = (ev: PointerEvent) => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        // Clamp to the host's cap: the player's minimum width wins over the
        // user's wider preference while space is tight (stored width keeps
        // the preference; it resurfaces when the cap lifts).
        const raw = startW + (startX - ev.clientX);
        const w = Math.min(widthCap, Math.max(PANEL_MIN_W, raw));
        widthRef.current = w;
        // Direct style write: zero React re-renders while dragging.
        el.style.width = `${w}px`;
      });
    };
    const onUp = (ev: PointerEvent) => {
      cancelAnimationFrame(raf);
      try {
        el.releasePointerCapture(ev.pointerId);
      } catch {
        /* noop */
      }
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
      const raw = startW + (startX - ev.clientX);
      const stored = Math.min(PANEL_MAX_W, Math.max(PANEL_MIN_W, raw));
      widthRef.current = Math.min(widthCap, stored);
      try {
        localStorage.setItem(PANEL_W_KEY, String(stored));
      } catch {
        /* storage blocked */
      }
      setWidth(stored); // state commit on pointerup only
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
  }, [widthCap]);

  // ── Render ----------------------------------------------------------------
  return (
    <div
      ref={panelRef}
      data-preview-chat-panel
      className={`shrink-0 self-stretch min-w-0 ${hidden ? 'hidden' : ''}`}
      style={spaceForced ? { width: 0 } : open ? { width: renderedW } : undefined}
    >
      {spaceForced ? null : !open ? (
        <button
          type="button"
          data-preview-chat-panel-collapsed
          onClick={() => {
            // Re-arm playback follow when opening from the collapsed strip:
            // the effect guards on activeIdx changing, so a panel opened
            // mid-playback at an unchanged index must still jump to the
            // row under the playhead.
            prevActiveIdxRef.current = null;
            followRef.current = true;
            setOpen(true);
          }}
          className="w-7 h-full flex flex-col items-center justify-center gap-1.5 border-l-2 border-zinc-800 bg-zinc-950 text-zinc-500 hover:text-white hover:bg-zinc-900"
          title={subtitlesOnly ? 'Open preview subtitles' : 'Open preview chat panel'}
        >
          {subtitlesOnly ? <Captions size={13} /> : <MessageSquare size={13} />}
          <span className="[writing-mode:vertical-rl] rotate-180 text-[8px] font-mono uppercase tracking-widest">
            {subtitlesOnly ? 'Subs' : 'Chat'}
          </span>
        </button>
      ) : (
        <div className="relative h-full flex flex-col bg-zinc-950 border-l-2 border-zinc-800 min-w-0">
          <div
            data-panel-resize-handle
            onPointerDown={onResizeStart}
            className="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize z-10 group/resize"
            title="Resize panel"
          >
            <div className="absolute left-0 top-0 bottom-0 w-0.5 bg-zinc-700 group-hover/resize:bg-zinc-500" />
          </div>
          <div className="flex items-center gap-0.5 border-b-2 border-zinc-800 px-1.5 py-1 shrink-0">
            {(subtitlesOnly
              ? TABS.filter((t) => t.id === 'subtitles')
              : subtitlesTabEnabled
                ? TABS
                : TABS.filter((t) => t.id !== 'subtitles')
            ).map(
              ({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => {
                    userPickedTabRef.current = true;
                    setTab(id);
                  }}
                  aria-pressed={tab === id}
                  className={`flex items-center gap-1 px-1.5 py-0.5 text-[8px] font-mono uppercase tracking-widest font-bold transition-colors ${
                    tab === id
                      ? 'bg-white text-black'
                      : 'text-zinc-500 hover:text-white hover:bg-zinc-800/60'
                  }`}
                >
                  <Icon size={10} className="shrink-0" />
                  {label}
                </button>
              ),
            )}
            <div className="flex-1" />
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-zinc-500 hover:text-white p-1"
              title="Collapse panel"
            >
              <ChevronRight size={14} />
            </button>
          </div>
          {fetchState === 'done' && payload && tab === 'chat' && payload.has_chat && (
            <div
              data-chat-search
              className="flex items-center gap-1.5 border-b-2 border-zinc-800 px-1.5 py-1 shrink-0"
            >
              <Search size={10} className="text-zinc-500 shrink-0" />
              <input
                value={chatQuery}
                onChange={(e) => setChatQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    stepSearch(e.shiftKey ? -1 : 1);
                  } else if (e.key === 'Escape') {
                    e.preventDefault();
                    setChatQuery('');
                  }
                }}
                placeholder="Search chat…"
                aria-label="Search chat history"
                spellCheck={false}
                className="flex-1 min-w-0 bg-zinc-900 border border-zinc-700 px-1.5 py-0.5 text-[10px] font-mono text-zinc-200 outline-none focus:border-white placeholder:text-zinc-600"
              />
              {qActive && (
                <span
                  data-chat-search-count
                  className="text-[9px] font-mono text-zinc-500 shrink-0"
                >
                  {chatList.length > 0 ? `${searchIdx + 1}/${chatList.length}` : '0/0'}
                </span>
              )}
              {qActive && (
                <>
                  <button
                    type="button"
                    onClick={() => stepSearch(-1)}
                    title="Previous match (Shift+Enter)"
                    className="text-zinc-500 hover:text-white p-0.5"
                    aria-label="Previous match"
                  >
                    <ChevronUp size={11} />
                  </button>
                  <button
                    type="button"
                    onClick={() => stepSearch(1)}
                    title="Next match (Enter)"
                    className="text-zinc-500 hover:text-white p-0.5"
                    aria-label="Next match"
                  >
                    <ChevronDown size={11} />
                  </button>
                  <button
                    type="button"
                    onClick={() => setChatQuery('')}
                    title="Clear search (Esc)"
                    className="text-zinc-500 hover:text-white p-0.5"
                    aria-label="Clear chat search"
                  >
                    <X size={11} />
                  </button>
                </>
              )}
            </div>
          )}
          {!started && subsFetchState === 'idle' && subtitlesOnly && (
            <div className="flex-1 min-h-0 flex items-center justify-center gap-2 text-zinc-600 px-4">
              <span className="text-[10px] font-mono text-center">
                Video plays first — subtitles load once playback starts.
              </span>
            </div>
          )}
          {fetchState === 'loading' && (
            <div className="flex-1 min-h-0 flex items-center justify-center gap-2 text-zinc-500">
              <Loader2 size={13} className="animate-spin" />
              <span className="text-[10px] font-mono">Loading panel…</span>
            </div>
          )}
          {fetchState === 'error' && (
            <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 px-4">
              <span className="text-red-300 text-[10px] font-mono text-center">
                Couldn&apos;t load panel data.
              </span>
              <button
                type="button"
                onClick={() => setRetryTick((t) => t + 1)}
                className="flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-red-300"
              >
                <RefreshCw size={10} />
                Retry
              </button>
            </div>
          )}
          {fetchState === 'done' && !payload && (
            // platform/videoId resolved empty (clip/live/channel/local-file
            // previews) — there is no archive key to fetch, so say so
            // instead of rendering a silent blank panel.
            <EmptyState text="Chat and transcript history aren't available for this kind of preview." />
          )}
          {fetchState === 'done' && payload && tab === 'subtitles' && (
            <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar flex flex-col items-center justify-center gap-2 px-3 py-4">
              {subtitlesOnly && subsFetchState === 'loading' && (
                <div className="flex flex-col items-center justify-center gap-2 text-zinc-500">
                  <Loader2 size={13} className="animate-spin" />
                  <span className="text-[10px] font-mono">Loading subtitles…</span>
                </div>
              )}
              {subtitlesOnly && subsFetchState === 'error' && (
                <div className="flex flex-col items-center justify-center gap-2 px-4">
                  <span className="text-red-300 text-[10px] font-mono text-center">
                    Couldn&apos;t load subtitles.
                  </span>
                  <button
                    type="button"
                    onClick={() => setRetryTick((t) => t + 1)}
                    className="flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-red-300"
                  >
                    <RefreshCw size={10} />
                    Retry
                  </button>
                </div>
              )}
              {subtitlesOnly && subsFetchState === 'done' && (!ytSubtitles?.has_subtitles || subtitleRows.length === 0) && (
                <p className="text-[10px] font-mono text-zinc-500 text-center leading-relaxed">
                  No subtitles available for this video.
                </p>
              )}
              {!subtitlesOnly && !payload.has_transcript && (
                <p className="text-[10px] font-mono text-zinc-500 text-center leading-relaxed">
                  No captions for this video.
                </p>
              )}
              {subtitleRows.length > 0 &&
                (activeSubtitleIdx >= 0 ? (
                  <>
                    <span className="text-[9px] font-mono uppercase tracking-widest text-zinc-500 shrink-0">
                      {formatArchiveOffset(currentTime)}
                    </span>
                    <p className="text-sm leading-relaxed text-zinc-100 text-center break-words" data-subtitle-line>
                      {subtitleRows[activeSubtitleIdx].text}
                    </p>
                  </>
                ) : (
                  <p className="text-[10px] font-mono text-zinc-500 text-center leading-relaxed">
                    No caption at this moment.
                  </p>
                ))}
            </div>
          )}
          {fetchState === 'done' && payload && tab !== 'subtitles' && (
            <div
              ref={scrollRef}
              onScroll={handleScroll}
              onClick={handleRowAreaClick}
              className="flex-1 min-h-0 overflow-y-auto custom-scrollbar"
              data-panel-rows
            >
              {tab === 'chat' && !payload.has_chat && (
                <EmptyState
                  text={
                    payload.backfill === 'running'
                      ? 'Loading chat…'
                      : 'No archived chat for this video.'
                  }
                />
              )}
              {tab === 'chat' && qActive && chatList.length === 0 && (
                <EmptyState text={`No chat messages match “${chatQuery.trim()}”.`} />
              )}
              {tab === 'transcript' && !payload.has_transcript && timelineRows.length === 0 && (
                <EmptyState text="No transcript for this video." />
              )}
              {list.length > 0 && (
                <>
                  <div style={{ height: topPad }} />
                  {slice.map((row, i) => {
                    const idx = windowStart + i;
                    const active = idx === focusIdx;
                    if (tab === 'chat') {
                      return (
                        <ChatRow
                          key={`c${idx}`}
                          row={row as PreviewPanelChatRow}
                          active={active}
                          platform={platform}
                          ref={active ? activeRowRef : undefined}
                        />
                      );
                    }
                    const tl = row as TimelineRow;
                    return tl.kind === 'event' ? (
                      <EventRow
                        key={`e${idx}`}
                        row={tl}
                        active={active}
                        ref={active ? activeRowRef : undefined}
                      />
                    ) : (
                      <TranscriptRow
                        key={`t${idx}`}
                        row={tl}
                        active={active}
                        ref={active ? activeRowRef : undefined}
                      />
                    );
                  })}
                  <div style={{ height: bottomPad }} />
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default PreviewChatPanel;
