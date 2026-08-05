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
  ChevronRight,
  FileText,
  Loader2,
  MessageSquare,
  RefreshCw,
} from 'lucide-react';
import { apiGet } from '../hooks/useApiClient';
import { activePanelRowIndex } from '../previewPlayerUtils';
import { formatArchiveOffset } from '../archiveSearchUtils';
import { resolveChatColor } from '../chatColors';

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
/** Matches the backend default; 12h+ of dense chat stays under this cap. */
const PANEL_LIMIT = 200_000;
const CHAT_ROW_H = 24;
const TRANSCRIPT_ROW_H = 22;
/** Rows rendered on each side of the active row (fixed-height virtualization). */
const WINDOW = 150;
const EMPTY_TRANSCRIPT: PreviewPanelTranscriptRow[] = [];
const EMPTY_CHAT: PreviewPanelChatRow[] = [];
const EMPTY_EVENTS: PreviewPanelEventRow[] = [];

interface PreviewChatPanelProps {
  platform: string | null;
  videoId: string | null;
  currentTime: number;
  /** True hides the panel (fullscreen) while keeping its state mounted. */
  hidden?: boolean;
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
  ref,
}: {
  row: PreviewPanelChatRow;
  active: boolean;
  platform: string | null;
  ref?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={ref}
      data-panel-row
      aria-current={active ? 'true' : undefined}
      style={{ height: CHAT_ROW_H }}
      className={`flex items-baseline gap-1 px-2 overflow-hidden border-l-2 whitespace-nowrap ${
        active
          ? 'bg-yellow-300/10 border-yellow-300 text-zinc-100'
          : 'border-transparent text-zinc-400'
      }`}
    >
      <span className="text-zinc-600 font-mono text-[9px] shrink-0">
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
          className="text-[9px] font-mono text-zinc-500 shrink-0"
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
  ref,
}: {
  row: PreviewPanelTranscriptRow;
  active: boolean;
  ref?: React.Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={ref}
      data-panel-row
      aria-current={active ? 'true' : undefined}
      style={{ height: TRANSCRIPT_ROW_H }}
      className={`flex items-baseline gap-1 px-2 overflow-hidden border-l-2 whitespace-nowrap ${
        active
          ? 'bg-yellow-300/10 border-yellow-300 text-zinc-100'
          : 'border-transparent text-zinc-400'
      }`}
    >
      <span className="text-zinc-600 font-mono text-[9px] shrink-0">
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
}: PreviewChatPanelProps) {
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState<PreviewPanelTab>('chat');
  const [width, setWidth] = useState<number>(() => {
    try {
      const w = Number(localStorage.getItem(PANEL_W_KEY));
      return w >= PANEL_MIN_W && w <= PANEL_MAX_W ? w : PANEL_DEFAULT_W;
    } catch {
      return PANEL_DEFAULT_W;
    }
  });
  const [payload, setPayload] = useState<PreviewPanelPayload | null>(null);
  const [fetchState, setFetchState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [retryTick, setRetryTick] = useState(0);
  const [ytSubtitles, setYtSubtitles] = useState<PreviewSubtitlesPayload | null>(null);
  const [subsFetchState, setSubsFetchState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');

  const payloadCacheRef = useRef<Map<string, PreviewPanelPayload>>(new Map());
  const subsCacheRef = useRef<Map<string, PreviewSubtitlesPayload>>(new Map());
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
    const cached = payloadCacheRef.current.get(payloadKey);
    if (cached) {
      setPayload(cached);
      setFetchState('done');
      return;
    }
    let cancelled = false;
    setFetchState('loading');
    apiGet<PreviewPanelPayload>(`/api/preview/panel/${payloadKey}?limit=${PANEL_LIMIT}`)
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
  }, [payloadKey, retryTick]);

  // Default tab: land on whichever source actually exists (first open only).
  useEffect(() => {
    if (userPickedTabRef.current || fetchState !== 'done' || !payload) return;
    if (tab === 'chat' && !payload.has_chat && payload.has_transcript) setTab('transcript');
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
  }, [subtitlesOnly, videoId, retryTick]);

  // Subtitles-only previews have no chat/transcript tabs to land on.
  useEffect(() => {
    if (subtitlesOnly && tab !== 'subtitles') setTab('subtitles');
  }, [subtitlesOnly, tab]);

  // ── Rows / active index ---------------------------------------------------
  const chatRows = useMemo(() => payload?.chat ?? EMPTY_CHAT, [payload]);
  const transcriptRows = useMemo(() => payload?.transcript ?? EMPTY_TRANSCRIPT, [payload]);
  const chatOffsets = useMemo(() => chatRows.map((r) => r.offset_sec), [chatRows]);
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
  const activeTimelineIdx = useMemo(
    () => activePanelRowIndex(timelineOffsets, currentTime),
    [timelineOffsets, currentTime],
  );
  // Subtitles-tab rows: the archive transcript for archived videos, the
  // live-fetched YouTube captions for URL-only previews.
  const subtitleRows = subtitlesOnly ? (ytSubtitles?.rows ?? EMPTY_TRANSCRIPT) : transcriptRows;
  const subtitleOffsets = useMemo(() => subtitleRows.map((r) => r.offset_sec), [subtitleRows]);
  const activeSubtitleIdx = useMemo(
    () => activePanelRowIndex(subtitleOffsets, currentTime),
    [subtitleOffsets, currentTime],
  );

  const list = tab === 'chat' ? chatRows : timelineRows;
  const rowH = tab === 'chat' ? CHAT_ROW_H : TRANSCRIPT_ROW_H;
  const activeIdx = tab === 'chat' ? activeChatIdx : activeTimelineIdx;
  activeIdxRef.current = activeIdx;
  rowHRef.current = rowH;

  // Fixed-height virtualization: window of WINDOW rows around the active row
  // with spacer divs above/below, so scroll height stays exact (active row's
  // content offset is always activeIdx * rowH).
  const windowStart = useMemo(() => {
    const maxStart = Math.max(0, list.length - 2 * WINDOW - 1);
    return Math.max(0, Math.min(activeIdx - WINDOW, maxStart));
  }, [list.length, activeIdx]);
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
    if (fetchState !== 'done') return;
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
  }, [activeIdx, fetchState]);

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

  // ── Self-contained width resize (rAF + pointer capture + direct writes) ---
  const widthRef = useRef(width);
  widthRef.current = width;

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
        const w = Math.min(PANEL_MAX_W, Math.max(PANEL_MIN_W, startW + (startX - ev.clientX)));
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
      const w = widthRef.current;
      try {
        localStorage.setItem(PANEL_W_KEY, String(w));
      } catch {
        /* storage blocked */
      }
      setWidth(w); // state commit on pointerup only
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
  }, []);

  // ── Render ----------------------------------------------------------------
  return (
    <div
      ref={panelRef}
      data-preview-chat-panel
      className={`shrink-0 self-stretch min-w-0 ${hidden ? 'hidden' : ''}`}
      style={open ? { width } : undefined}
    >
      {!open ? (
        <button
          type="button"
          data-preview-chat-panel-collapsed
          onClick={() => setOpen(true)}
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
            {(subtitlesOnly ? TABS.filter((t) => t.id === 'subtitles') : TABS).map(
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
              {subtitlesOnly && subsFetchState !== 'done' && (
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
              {tab === 'chat' && !payload.has_chat && <EmptyState text="No archived chat for this video." />}
              {tab === 'transcript' && !payload.has_transcript && timelineRows.length === 0 && (
                <EmptyState text="No transcript for this video." />
              )}
              {list.length > 0 && (
                <>
                  <div style={{ height: topPad }} />
                  {slice.map((row, i) => {
                    const idx = windowStart + i;
                    const active = idx === activeIdx;
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
