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

import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  Captions,
  Download,
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
import { apiGet, apiPost } from '../hooks/useApiClient';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { t, useI18n } from '../i18n';
import { activePanelRowIndex } from '../previewPlayerUtils';
import { formatArchiveOffset } from '../archiveSearchUtils';
import { resolveChatColor } from '../chatColors';
import { seekToTimestamp } from '../seekToTimestamp';
import { ChatEmoteText, useChatEmotes, type EmoteMap } from '../chatEmotes';
import {
  applyChatMarker,
  ChatMarkerChips,
  ChatRowMarkers,
  EMPTY_CHAT_MARKERS,
  type ChatMarkerKind,
  type ChatMarkers,
} from './ChatRangeMarkers';

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
  /** True when `chat` is a bounded slice of the archive, not the whole
   *  timeline: the backfill is still running (playhead window) or the
   *  archive exceeded the panel's row cap. The UI shows a small note. */
  chat_truncated?: boolean;
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
/** Matches the backend cap; dense archives are reported as chat_truncated. */
const PANEL_LIMIT = 20_000;
/** While a Twitch backfill is 'running' the panel refreshes at this rate
 *  (the backend bounds each response to a playhead window, so polling stays
 *  cheap and chat appears progressively instead of after the whole run). */
const PANEL_POLL_MS = 2500;
const CHAT_ROW_H = 24;
/** Transcript cues WRAP — they used to be a fixed 22px one-liner with
 *  `truncate`, which cut every Parakeet fragment (2-6 words) to an ellipsis
 *  at 10px. Heights are now MODELLED, not fixed: the row box is sized from
 *  the same line count the text is clamped to, so the virtualiser's offset
 *  table stays exact. */
const TRANSCRIPT_LINE_H = 19; // 13px text @ 1.46 leading
const TRANSCRIPT_ROW_PAD_Y = 12; // py-1.5
const TRANSCRIPT_MAX_LINES = 4; // collapsed clamp — Parakeet cues are short
const TRANSCRIPT_EXPANDED_MAX_LINES = 24; // expanded ceiling (~468px)
const EVENT_ROW_H = 26;
/** Mirrors the --text-ui-sm token (13px) used by the cue body. */
const TRANSCRIPT_TEXT_PX = 13;
/** Average advance width of the body font as a fraction of its size — the
 *  line count is estimated, so this errs wide (over-count lines and the row
 *  gets slack; under-count and the clamp shows an ellipsis, i.e. the exact
 *  bug this model exists to kill). */
const TRANSCRIPT_CHAR_W = 0.55;
/** Width every cue row reserves for the timestamp + the expand affordance, so
 *  the text column (and therefore the height model) is identical whether or
 *  not a long cue renders its toggle. */
const TRANSCRIPT_GUTTER = 68;
/** Rows rendered on each side of the focus row for the fixed-height chat
 *  list. */
const WINDOW = 150;
/** Transcript rows are ~3x taller (they wrap), so the mounted window is
 *  smaller — still bounded at ~41 DOM rows. */
const TIMELINE_WINDOW = 20;
/** Rows kept rendered above the scroll anchor when the user has scrolled off
 *  the playhead, so fast upward scrolling never shows blank spacer. */
const TIMELINE_ANCHOR_BACK = 12;
/** Subtitle search results render in a plain (unvirtualised) list. */
const SUBTITLE_MATCH_CAP = 200;
/** Panel filter debounce — matches ArchiveSearchPopup's 250ms, halved: the
 *  list is already bounded to the payload the panel holds. */
const SEARCH_DEBOUNCE_MS = 200;
const EMPTY_TRANSCRIPT: PreviewPanelTranscriptRow[] = [];
const EMPTY_CHAT: PreviewPanelChatRow[] = [];
const EMPTY_EVENTS: PreviewPanelEventRow[] = [];

/** Lines a cue occupies: measured against the available text width, clamped
 *  to the collapsed/expanded budget. 1 line minimum (an empty cue still
 *  occupies a row). */
export function transcriptCueLines(
  text: string,
  textWidthPx: number,
  expanded: boolean,
): number {
  const cap = expanded ? TRANSCRIPT_EXPANDED_MAX_LINES : TRANSCRIPT_MAX_LINES;
  const perLine = Math.max(
    8,
    Math.floor(textWidthPx / (TRANSCRIPT_TEXT_PX * TRANSCRIPT_CHAR_W)),
  );
  const need = Math.max(1, Math.ceil(text.length / perLine));
  return Math.min(need, cap);
}

/** Box height for a cue with `lines` lines — must match the rendered
 *  box exactly (py-1.5 padding + N clamped lines) or the offset table drifts. */
export function transcriptCueHeight(lines: number): number {
  return lines * TRANSCRIPT_LINE_H + TRANSCRIPT_ROW_PAD_Y;
}

/** True when a cue is long enough that expanding it would show MORE than the
 *  collapsed clamp — i.e. the expand toggle has something to reveal. A cue
 *  that exactly fills the clamp gets no toggle (tapping it would do nothing). */
export function transcriptCueExpandable(text: string, textWidthPx: number): boolean {
  return (
    transcriptCueLines(text, textWidthPx, true) > TRANSCRIPT_MAX_LINES
  );
}

/** Prefix-sum offsets (px) of the rendered list: `offsets[i]` is the content
 *  offset of row i, `offsets[list.length]` the total scroll height. Chat rows
 *  stay fixed-height; transcript cues use the wrap model; acoustic events are
 *  one line. */
function rowOffsetsFor(
  list: ReadonlyArray<TimelineRow | PreviewPanelChatRow>,
  kind: 'chat' | 'timeline',
  textWidthPx: number,
  expanded: ReadonlySet<number>,
): number[] {
  const offsets = new Array<number>(list.length + 1);
  offsets[0] = 0;
  for (let i = 0; i < list.length; i++) {
    const row = list[i];
    let h: number;
    if (kind === 'chat') h = CHAT_ROW_H;
    else {
      const tl = row as TimelineRow;
      h =
        tl.kind === 'event'
          ? EVENT_ROW_H
          : transcriptCueHeight(transcriptCueLines(tl.text, textWidthPx, expanded.has(tl.offset_sec)));
    }
    offsets[i + 1] = offsets[i] + h;
  }
  return offsets;
}

/** Row covering content offset `y` (binary search — the offset table is
 *  sorted, so this is O(log n) instead of the old `scrollTop / rowH`). */
export function rowIndexAtOffset(offsets: number[], y: number): number {
  let lo = 0;
  let hi = offsets.length - 2; // last row index
  if (hi < 0 || y < 0) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (offsets[mid] <= y) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

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
  /** Channel slug/login (Twitch broadcaster login) for channel-scoped custom
   *  emotes (BTTV/FFZ/7TV). Absent → the chat renders plain text (the emotes
   *  API requires a slug; there is no global-only mode). */
  channel?: string | null;
  /** Click-to-seek: the host's CURRENT-player seek (main preview or the
   *  popup's own player). When provided, chat/transcript/event rows and the
   *  subtitle caption become clickable and seek to the row's offset_sec.
   *  Absent → rows keep the scroll-only behavior (no player to seek). */
  onSeek?: (offsetSec: number) => void;
  /** True hides the panel (fullscreen) while keeping its state mounted. */
  hidden?: boolean;
  /** Initial open state. The explore popup opens collapsed (small strip)
   *  so the mini preview stays player-sized by default; the main preview
   *  keeps the panel open. */
  defaultOpen?: boolean;
  /** Controlled mode: when `open` is provided the HOST owns the open state
   *  (changes flow out via onOpenChange) and the collapsed side strip is NOT
   *  rendered — the host shows its own toggle button instead. Used by the
   *  explore mini preview, whose chat opens from a button next to
   *  'Search this video'. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Cap on the rendered width. The host reserves player space so the video
   *  never drops below its layout minimum; below PANEL_MIN_W there is no room
   *  at all and the panel collapses to zero width (no strip). Defaults to the
   *  panel's own max (no cap). */
  maxWidth?: number;
  /** Reports the space the panel actually occupies: width = rendered width
   *  (open), strip width (collapsed), or 0 (space-forced). The explore popup
   *  sizes its container from this. */
  onLayoutChange?: (info: { open: boolean; width: number }) => void;
  /** Fired whenever the start/end chat markers change (null = unset). The
   *  host lifts the pair so the NEXT download of this video also writes a
   *  <media>.chat.txt covering [start, end]. Keep the callback stable
   *  (useCallback) — it is invoked on every marker change and video switch. */
  onMarkersChange?: (markers: ChatMarkers) => void;
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
  emotes,
  onSeek,
  markers,
  onSetMarker,
  ref,
}: {
  row: PreviewPanelChatRow;
  active: boolean;
  platform: string | null;
  emotes: EmoteMap;
  onSeek?: (offsetSec: number) => void;
  markers: ChatMarkers;
  onSetMarker: (kind: ChatMarkerKind, offsetSec: number) => void;
  ref?: React.Ref<HTMLDivElement>;
}) {
  const { t } = useI18n();
  return (
    <div
      ref={ref}
      data-panel-row
      aria-current={active ? 'true' : undefined}
      style={{ height: CHAT_ROW_H }}
      onClick={onSeek ? () => seekToTimestamp(row.offset_sec, onSeek) : undefined}
      title={onSeek ? t('Seek to {offset}', { offset: formatArchiveOffset(row.offset_sec) }) : undefined}
      className={`relative group/marker flex items-baseline gap-1 px-2 overflow-hidden border-l-2 whitespace-nowrap ${
        active
          ? 'bg-yellow-300/10 border-yellow-300 text-zinc-100'
          : 'border-transparent text-zinc-200 hover:bg-zinc-900/70'
      } ${onSeek ? 'cursor-pointer select-none' : ''}`}
    >
      <span className="text-zinc-300 font-mono text-[9px] shrink-0">
        {formatArchiveOffset(row.offset_sec)}
      </span>
      <span
        className="font-bold text-[10px] shrink-0"
        style={{ color: resolveChatColor(row.color, row.username, platform) }}
      >
        {row.username}:
      </span>
      <span className="text-[10px] leading-snug truncate" title={row.text}>
        <ChatEmoteText text={row.text} emotes={emotes} />
      </span>
      {typeof row.spam_count === 'number' && row.spam_count > 1 && (
        <span
          className="text-[9px] font-mono text-zinc-400 shrink-0"
          title={`${row.spam_count} identical messages collapsed`}
        >
          ×{row.spam_count}
        </span>
      )}
      <ChatRowMarkers offsetSec={row.offset_sec} markers={markers} onSetMarker={onSetMarker} />
    </div>
  );
});

/** Transcript cue: the text WRAPS (13px body, never a clipped one-liner) and
 *  the row is a real button, so it is focusable, Enter/Space seeks and a
 *  screen reader announces it. A cue too long for the collapsed budget gets
 *  its own expand toggle — a sibling button, never nested inside the seek
 *  button (invalid HTML and it would swallow the Enter key). */
const TranscriptRow = memo(function TranscriptRow({
  row,
  active,
  onSeek,
  height,
  lines,
  expandable,
  expanded,
  onToggle,
  ref,
}: {
  row: PreviewPanelTranscriptRow;
  active: boolean;
  onSeek?: (offsetSec: number) => void;
  height: number;
  lines: number;
  expandable: boolean;
  expanded: boolean;
  onToggle: (offsetSec: number) => void;
  ref?: React.Ref<HTMLDivElement>;
}) {
  const { t } = useI18n();
  const offset = formatArchiveOffset(row.offset_sec);
  // Only offer the toggle when expanding actually reveals more text.
  const clamped = expandable || expanded;
  const body = (
    <>
      <span className="text-zinc-400 font-mono text-ui-xs tabular-nums shrink-0 pt-[2px]">
        {offset}
      </span>
      <span
        className="text-ui-sm text-zinc-200 break-words [overflow-wrap:anywhere]"
        style={{
          display: '-webkit-box',
          WebkitBoxOrient: 'vertical',
          WebkitLineClamp: lines,
          overflow: 'hidden',
          lineHeight: `${TRANSCRIPT_LINE_H}px`,
        }}
      >
        {row.text}
      </span>
    </>
  );
  return (
    <div
      ref={ref}
      data-panel-row
      data-panel-cue-row={row.offset_sec}
      aria-current={active ? 'true' : undefined}
      style={{ height }}
      className={`relative flex items-stretch overflow-hidden border-l-2 ${
        active
          ? 'bg-yellow-300/10 border-yellow-300'
          : 'border-transparent hover:bg-zinc-900/70'
      }`}
    >
      {onSeek ? (
        <button
          type="button"
          data-panel-cue={row.offset_sec}
          onClick={() => seekToTimestamp(row.offset_sec, onSeek)}
          title={t('Seek to {offset}', { offset })}
          className="flex-1 min-w-0 flex items-baseline gap-1.5 px-2 py-1.5 text-left cursor-pointer select-text"
        >
          {body}
        </button>
      ) : (
        <div className="flex-1 min-w-0 flex items-baseline gap-1.5 px-2 py-1.5 text-left select-text">
          {body}
        </div>
      )}
      {clamped && (
        <button
          type="button"
          data-panel-cue-expand={row.offset_sec}
          aria-expanded={expanded}
          aria-label={expanded ? t('Collapse cue') : t('Expand cue')}
          title={expanded ? t('Collapse cue') : t('Expand cue')}
          onClick={() => onToggle(row.offset_sec)}
          className="shrink-0 flex items-center px-1.5 text-zinc-500 hover:text-zinc-100 cursor-pointer"
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </button>
      )}
    </div>
  );
});

/** Acoustic-event row: amber LABEL + duration, interleaved with transcript
 *  segments by offset_sec; tooltip carries the exact range + confidence. */
const EventRow = memo(function EventRow({
  row,
  active,
  height,
  onSeek,
  ref,
}: {
  row: PreviewPanelEventRow;
  active: boolean;
  height: number;
  onSeek?: (offsetSec: number) => void;
  ref?: React.Ref<HTMLDivElement>;
}) {
  const { t } = useI18n();
  const durSec = Math.max(0, row.end_sec - row.offset_sec);
  const rangeTitle = t('{event} — {from} to {to} ({sec}s, confidence {pct}%)', {
    event: row.event,
    from: formatArchiveOffset(row.offset_sec),
    to: formatArchiveOffset(row.end_sec),
    sec: durSec.toFixed(1),
    pct: (row.score * 100).toFixed(0),
  });
  const body = (
    <>
      <span className="text-zinc-500 font-mono text-ui-xs tabular-nums shrink-0">
        {formatArchiveOffset(row.offset_sec)}
      </span>
      <span className="font-bold text-ui-xs uppercase tracking-wide shrink-0">
        {row.event}
      </span>
      <span className="text-ui-xs font-mono text-zinc-500 shrink-0">({durSec.toFixed(1)}s)</span>
      <span className="flex-1" />
      <span className="text-ui-xs font-mono text-zinc-600 shrink-0">
        {(row.score * 100).toFixed(0)}%
      </span>
    </>
  );
  return (
    <div
      ref={ref}
      data-panel-row
      data-event-row={row.event}
      aria-current={active ? 'true' : undefined}
      title={rangeTitle}
      style={{ height }}
      className={`relative flex items-stretch overflow-hidden border-l-2 ${
        active
          ? 'bg-amber-300/15 border-amber-300'
          : 'border-transparent hover:bg-zinc-900/70'
      }`}
    >
      {onSeek ? (
        <button
          type="button"
          data-panel-event={row.offset_sec}
          onClick={() => seekToTimestamp(row.offset_sec, onSeek)}
          title={t('Seek to {offset} — {range}', {
            offset: formatArchiveOffset(row.offset_sec),
            range: rangeTitle,
          })}
          className="flex-1 min-w-0 flex items-center gap-1.5 px-2 cursor-pointer select-text text-amber-300"
        >
          {body}
        </button>
      ) : (
        <div
          className="flex-1 min-w-0 flex items-center gap-1.5 px-2 select-text text-amber-300"
        >
          {body}
        </div>
      )}
    </div>
  );
});

function EmptyState({ text, progress }: { text: string; progress?: number }) {
  const hasProgress = progress != null && Number.isFinite(progress);
  return (
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 px-4" data-panel-empty>
      <p className="text-ui-sm font-mono text-zinc-400 text-center leading-relaxed">{text}</p>
      {hasProgress && (
        <div
          className="flex items-center gap-2 w-44"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress * 100)}
          aria-label={t('progress.backfill')}
        >
          <div className="h-1 flex-1 rounded bg-line overflow-hidden">
            <div
              className="h-full bg-mark-start transition-[width] duration-300"
              style={{ width: `${Math.min(100, Math.max(0, Math.round(progress * 100)))}%` }}
            />
          </div>
          <span className="text-ui-xs font-mono text-zinc-400 tabular-nums shrink-0">
            {Math.round(progress * 100)}%
          </span>
        </div>
      )}
    </div>
  );
}

export function PreviewChatPanel({
  platform,
  videoId,
  currentTime,
  channel,
  onSeek,
  hidden = false,
  defaultOpen = true,
  maxWidth: maxWidthProp,
  onLayoutChange,
  onMarkersChange,
  open: controlledOpen,
  onOpenChange,
}: PreviewChatPanelProps) {
  const { t } = useI18n();
  const controlled = controlledOpen !== undefined;
  const [internalOpen, setInternalOpen] = useState(defaultOpen);
  const open = controlled ? controlledOpen : internalOpen;
  const changeOpen = useCallback((v: boolean) => {
    if (controlled) onOpenChange?.(v);
    else setInternalOpen(v);
  }, [controlled, onOpenChange]);
  const [tab, setTab] = useState<PreviewPanelTab>('transcript');
  const [width, setWidth] = useState<number>(readPreviewChatPanelWidth);
  /** Start/end chat markers (green/red). One pair per surface — the host
   *  lifts it via onMarkersChange so the next download writes the chat txt
   *  range. Reset whenever the panel's video changes (markers belong to one
   *  video's chat). */
  const [markers, setMarkers] = useState<ChatMarkers>(EMPTY_CHAT_MARKERS);
  const markersRef = useRef(markers);
  markersRef.current = markers;
  const onMarkersChangeRef = useRef(onMarkersChange);
  onMarkersChangeRef.current = onMarkersChange;
  const setChatMarkers = useCallback((next: ChatMarkers) => {
    markersRef.current = next;
    setMarkers(next);
    onMarkersChangeRef.current?.(next);
  }, []);
  const handleSetMarker = useCallback(
    (kind: ChatMarkerKind, offsetSec: number) => {
      setChatMarkers(applyChatMarker(kind, offsetSec, markersRef.current));
    },
    [setChatMarkers],
  );
  const handleClearMarker = useCallback(
    (kind: ChatMarkerKind) => {
      const cur = markersRef.current;
      setChatMarkers({ ...cur, [kind]: null });
    },
    [setChatMarkers],
  );
  const [payload, setPayload] = useState<PreviewPanelPayload | null>(null);
  const [fetchState, setFetchState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [retryTick, setRetryTick] = useState(0);
  /** Bumped by the backfill poll loop (and the one refresh after 'done')
   *  to re-run the payload fetch without touching the cache. */
  const [pollTick, setPollTick] = useState(0);
  const [ytSubtitles, setYtSubtitles] = useState<PreviewSubtitlesPayload | null>(null);
  const [subsFetchState, setSubsFetchState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  /** Inline search (filter + prev/next cursor) — one query for EVERY tab.
   *  It used to render only under `tab === 'chat'`, i.e. the panel's default
   *  view (transcript — every YouTube/Kick video has no chat) had no search at
   *  all. `query` is the raw input; `debouncedQuery` is what filters, so
   *  typing never re-filters 20k rows per keystroke. */
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebouncedValue(query, SEARCH_DEBOUNCE_MS);
  const [searchIdx, setSearchIdx] = useState(-1);
  /** Cue offsets the user expanded past the collapsed line budget. */
  const [expandedCues, setExpandedCues] = useState<ReadonlySet<number>>(() => new Set());
  const toggleCue = useCallback((offsetSec: number) => {
    setExpandedCues((prev) => {
      const next = new Set(prev);
      if (next.has(offsetSec)) next.delete(offsetSec);
      else next.add(offsetSec);
      return next;
    });
  }, []);
  const tabIdBase = useId();

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
  /** Cursor row of the subtitles search match list (no virtualised scroller). */
  const subtitleCursorRef = useRef<HTMLDivElement>(null);
  /** Tab buttons, for the roving tabindex (arrow keys move focus + selection). */
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const focusTabAfterSwitchRef = useRef(false);
  /** True while the view follows the playhead (recentered on the active
   *  row). The user scrolling >1.5 viewports away turns it off; clicking a
   *  row or switching tabs turns it back on. State (not just a ref) so the
   *  render window can switch between playhead-centered and scroll-riding. */
  const [follow, setFollow] = useState(true);
  const followRef = useRef(true);
  followRef.current = follow;
  /** Last user-scrolled row index — the render window rides this while
   *  `follow` is off, so scrolling never lands on blank spacers. setState
   *  bails out when the row band did not change, keeping fast wheel scrolls
   *  cheap. */
  const [scrollAnchorIdx, setScrollAnchorIdx] = useState(0);
  const autoScrollingRef = useRef(false);
  const activeIdxRef = useRef(-1);
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

  // Channel emotes for twitch history rows (BTTV/FFZ/7TV render-only — the
  // stored row.text is never rewritten). Declared after the payload effect
  // so the panel's archive fetch stays first. Stable map reference (cache),
  // so memoized ChatRows only re-render once when the fetch resolves.
  const emotes = useChatEmotes(platform, channel);

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
  // Video switch: a stale scroll anchor/follow state must not carry into the
  // new list — start at the head, following the playhead again. The search
  // query and the expanded cues belong to the old video's rows too.
  useEffect(() => {
    setFollow(true);
    setScrollAnchorIdx(0);
    setQuery('');
    setExpandedCues(new Set());
  }, [payloadKey]);
  // Markers belong to one video's chat: switching the panel's video clears
  // the pair (and lifts the clear so the host's copy can't leak onto the
  // next video's download). setChatMarkers is stable (useCallback []).
  useEffect(() => {
    setChatMarkers(EMPTY_CHAT_MARKERS);
  }, [payloadKey, setChatMarkers]);
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
    // Fetch the live captions as soon as the URL-only state is known
    // (session-create, in parallel with video loading) — no wait for
    // canplay, so subtitles are ready by the time playback starts. The
    // cache dedupes by videoId, so later re-runs of this effect never
    // re-fetch.
    let cancelled = false;
    setSubsFetchState('loading');
    const watchUrl = `https://www.youtube.com/watch?v=${videoId}`;
    apiGet<PreviewSubtitlesPayload>(`/api/subtitles?url=${encodeURIComponent(watchUrl)}&langs=en,pt,es`)
      .then((p) => {
        if (cancelled) return;
        const cache = subsCacheRef.current;
        cache.set(videoId, p);
        if (cache.size > 8) {
          // ponytail: bounded subtitles cache — same LRU-ish prune as the
          // payload cache; upgrade path: shared cache helper if a third
          // bounded map ever appears.
          const oldest = cache.keys().next().value;
          if (oldest !== undefined) cache.delete(oldest);
        }
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
    if (!subtitlesTabEnabled && tab === 'subtitles') setTab('transcript');
  }, [subtitlesTabEnabled, tab]);
  /** Tabs actually offered for this video/platform, in display order. */
  const visibleTabs = useMemo(
    () =>
      subtitlesOnly
        ? TABS.filter((x) => x.id === 'subtitles')
        : subtitlesTabEnabled
          ? TABS
          : TABS.filter((x) => x.id !== 'subtitles'),
    [subtitlesOnly, subtitlesTabEnabled],
  );
  /** Arrow/Home/End move selection AND focus (roving tabindex) — the tabs are
   *  a real tablist now, not a row of pressed buttons. */
  const onTabListKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const ids = visibleTabs.map((x) => x.id);
      const i = ids.indexOf(tab);
      if (i < 0) return;
      let next = -1;
      if (e.key === 'ArrowRight') next = (i + 1) % ids.length;
      else if (e.key === 'ArrowLeft') next = (i - 1 + ids.length) % ids.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = ids.length - 1;
      else return;
      e.preventDefault();
      userPickedTabRef.current = true;
      focusTabAfterSwitchRef.current = true;
      setTab(ids[next]);
    },
    [tab, visibleTabs],
  );
  useEffect(() => {
    if (!focusTabAfterSwitchRef.current) return;
    focusTabAfterSwitchRef.current = false;
    tabRefs.current[tab]?.focus();
  }, [tab, visibleTabs]);
  const selectTab = useCallback((id: PreviewPanelTab) => {
    userPickedTabRef.current = true;
    setTab(id);
  }, []);

  // ── Rendered width (declared early: the cue line model needs the text
  // column width to size the rows) -------------------------------------------
  const widthCap = Math.min(PANEL_MAX_W, maxWidthProp ?? PANEL_MAX_W);
  const spaceForced = open && widthCap < PANEL_MIN_W;
  const renderedW = spaceForced ? 0 : open ? Math.min(width, widthCap) : controlled ? 0 : PANEL_STRIP_W;
  const widthRef = useRef(renderedW);
  widthRef.current = renderedW;
  /** Text column a cue actually gets (panel width − padding − the reserved
   *  timestamp/expand gutter). */
  const cueTextW = Math.max(120, renderedW - TRANSCRIPT_GUTTER);

  // ── Rows / active index ---------------------------------------------------
  const chatRows = useMemo(() => payload?.chat ?? EMPTY_CHAT, [payload]);
  const transcriptRows = useMemo(() => payload?.transcript ?? EMPTY_TRANSCRIPT, [payload]);
  /** The one search query, shared by every tab (debounced upstream). */
  const q = debouncedQuery.trim().toLowerCase();
  /** Chat-tab list: the full history, or only rows matching the inline search
   *  query (case-insensitive on username and text). */
  const chatList = useMemo(() => {
    if (!q) return chatRows;
    return chatRows.filter(
      (r) => r.username.toLowerCase().includes(q) || r.text.toLowerCase().includes(q),
    );
  }, [chatRows, q]);
  const chatOffsets = useMemo(() => chatList.map((r) => r.offset_sec), [chatList]);
  /** The search is live on whichever tab is showing, with a non-empty query. */
  const qActive = q.length > 0;
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
  /** Transcript-tab list: the full timeline, or only rows matching the search
   *  (cue text, or the event label). */
  const timelineList = useMemo(() => {
    if (!q) return timelineRows;
    return timelineRows.filter((r) =>
      r.kind === 'event' ? r.event.toLowerCase().includes(q) : r.text.toLowerCase().includes(q),
    );
  }, [timelineRows, q]);
  const activeChatIdx = useMemo(
    () => activePanelRowIndex(chatOffsets, currentTime),
    [chatOffsets, currentTime],
  );
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
  /** Subtitles-tab list: caption rows matching the search. */
  const subtitleList = useMemo(() => {
    if (!q) return subtitleRows;
    return subtitleRows.filter((r) => r.text.toLowerCase().includes(q));
  }, [subtitleRows, q]);

  /** The list the search counter + prev/next step through on the visible tab. */
  const searchList = tab === 'chat' ? chatList : tab === 'transcript' ? timelineList : subtitleList;
  /** Search UI is per tab: it used to render only for chat, so the panel's
   *  default (transcript) view had no search at all. */
  const searchEnabled =
    fetchState === 'done' &&
    !!payload &&
    (tab === 'chat'
      ? chatRows.length > 0
      : tab === 'transcript'
        ? timelineRows.length > 0
        : subtitleRows.length > 0);
  const searchLabel =
    tab === 'chat'
      ? t('Search chat history')
      : tab === 'transcript'
        ? t('Search transcript')
        : t('Search subtitles');
  const searchPlaceholder =
    tab === 'chat' ? t('Search chat…') : tab === 'transcript' ? t('Search transcript…') : t('Search subtitles…');
  /** The filter can only see the rows the backend returned. When that slice is
   *  known to be partial, say so INSIDE the search UI — otherwise "no match"
   *  silently reads as "this was never said". Chat reports it explicitly;
   *  the transcript is capped by the same request limit without a flag, so a
   *  payload sitting exactly at the cap is the only signal available. */
  const partialCoverage = useMemo(() => {
    if (!qActive || !payload) return null;
    if (tab === 'chat' && payload.chat_truncated) {
      const total = payload.total_rows ?? payload.chat.length;
      return total > payload.chat.length
        ? { loaded: payload.chat.length, total }
        : null;
    }
    if (tab !== 'subtitles' && transcriptRows.length >= PANEL_LIMIT) {
      return { loaded: transcriptRows.length, total: transcriptRows.length + 1 };
    }
    return null;
  }, [qActive, payload, tab, transcriptRows.length]);
  /** Rows the virtualised list renders (chat + transcript; the subtitles tab
   *  shows one caption at a time). */
  const list = tab === 'chat' ? chatList : timelineList;
  const activeIdx = tab === 'chat' ? activeChatIdx : activeTimelineIdx;
  activeIdxRef.current = activeIdx;
  /** Prefix-sum offsets for the rendered list — the wrap model replaces the
   *  old fixed `rowH`, so spacers, scroll→row mapping and the search cursor
   *  all read the same table. */
  const rowOffsets = useMemo(
    () => rowOffsetsFor(list, tab === 'chat' ? 'chat' : 'timeline', cueTextW, expandedCues),
    [list, tab, cueTextW, expandedCues],
  );
  const rowOffsetsRef = useRef(rowOffsets);
  rowOffsetsRef.current = rowOffsets;
  const totalH = rowOffsets[rowOffsets.length - 1] ?? 0;
  /** Row the view centers on: the search cursor while a query is active,
   *  the playback-synced row otherwise. */
  const focusIdx = qActive && searchIdx >= 0 ? searchIdx : activeIdx;
  /** Half-width of the mounted window, per surface: transcript cues are ~3x
   *  taller than a chat line, so the same row count would mount 3x the DOM. */
  const windowW = tab === 'chat' ? WINDOW : TIMELINE_WINDOW;
  const anchorBack = tab === 'chat' ? 8 : TIMELINE_ANCHOR_BACK;

  // Virtualisation: a window of rows around the focus row with spacer divs
  // above/below, so the scroll height stays exact (offsets come from the
  // height model, not a per-row constant). While the user is not following
  // (scrolled away to read), the window rides the scroll position instead —
  // scrolling always shows real rows, never blank spacers.
  const windowStart = useMemo(() => {
    const maxStart = Math.max(0, list.length - 2 * windowW - 1);
    if (qActive) {
      // Inline search: center on the cursor row (the search effect pins
      // scrollTop to the cursor's offset).
      return Math.max(0, Math.min(focusIdx - windowW, maxStart));
    }
    if (!follow) {
      // User-scrolled: anchor just above the viewport top so the visible
      // band is always rendered content.
      return Math.max(0, Math.min(scrollAnchorIdx - anchorBack, maxStart));
    }
    // Follow mode: center the window on the playback-synced row.
    return Math.max(0, Math.min(focusIdx - windowW, maxStart));
  }, [list.length, focusIdx, scrollAnchorIdx, follow, qActive, windowW, anchorBack]);
  const windowEnd = Math.min(list.length, windowStart + 2 * windowW + 1);
  const slice = useMemo(() => list.slice(windowStart, windowEnd), [list, windowStart, windowEnd]);
  const topPad = rowOffsets[windowStart] ?? 0;
  const bottomPad = totalH - (rowOffsets[windowEnd] ?? totalH);

  // ── Playback sync (seek + play) ------------------------------------------
  // Re-arm follow on tab switch: the new list needs its active row scrolled
  // into view even when the index happens to be unchanged. The query is
  // per-tab state — carrying a chat filter over to the transcript would hide
  // the very rows the user just switched to read.
  useEffect(() => {
    prevActiveIdxRef.current = null;
    setFollow(true);
    setQuery('');
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
    // Drive the render window from the scroll position: rowAtTop becomes
    // the anchor the windowStart memo rides once the user stops following.
    // Variable-height cues → the offset table answers "which row is at this
    // pixel" (binary search); setState bails out when the row band is
    // unchanged, so a fast wheel scroll does not re-render per event.
    const offsets = rowOffsetsRef.current;
    const rowAtTop = rowIndexAtOffset(offsets, el.scrollTop);
    setScrollAnchorIdx((prev) => {
      const next = Math.max(0, rowAtTop);
      return next === prev ? prev : next;
    });
    // The active row's content offset is fixed by the model. When the user
    // scrolls more than 1.5 viewports away from it, they are reading
    // elsewhere — stop auto-following until they click a row / switch tab.
    const activeTop = offsets[activeIdxRef.current] ?? 0;
    if (Math.abs(el.scrollTop - activeTop) > el.clientHeight * 1.5) {
      setFollow(false);
    }
  }, []);

  const handleRowAreaClick = useCallback((e: React.MouseEvent) => {
    const target = (e.target as HTMLElement).closest('[data-panel-row]') as HTMLElement | null;
    if (!target) return;
    setFollow(true);
    target.scrollIntoView({ block: 'nearest' });
  }, []);

  // ── Inline search (every tab) ---------------------------------------------
  // Activating the query jumps the cursor to the match nearest playback time
  // (the filtered list's active row); clearing it re-arms playback follow.
  const activeIdxForSearchRef = useRef(-1);
  activeIdxForSearchRef.current = tab === 'chat' ? activeChatIdx : activeTimelineIdx;
  useEffect(() => {
    if (qActive) {
      setSearchIdx(activeIdxForSearchRef.current >= 0 ? activeIdxForSearchRef.current : 0);
    } else {
      setSearchIdx(-1);
      prevActiveIdxRef.current = null;
    }
  }, [qActive]);

  // Keep the cursor in range while typing shrinks the match set.
  useEffect(() => {
    if (!qActive || searchIdx < 0) return;
    if (searchList.length === 0) {
      if (searchIdx !== -1) setSearchIdx(-1);
    } else if (searchIdx >= searchList.length) {
      setSearchIdx(Math.max(0, searchList.length - 1));
    }
  }, [qActive, searchList.length, searchIdx]);

  // Scroll the cursor row into view (exact content offset from the height
  // model). The subtitles tab has no virtualised scroller — its match list
  // scrolls the cursor row itself into view instead.
  useEffect(() => {
    if (!qActive || searchIdx < 0) return;
    if (tab === 'subtitles') {
      subtitleCursorRef.current?.scrollIntoView({ block: 'nearest' });
      return;
    }
    const el = scrollRef.current;
    if (!el) return;
    autoScrollingRef.current = true;
    el.scrollTop = Math.max(0, (rowOffsets[searchIdx] ?? 0) - 8);
  }, [qActive, searchIdx, tab, rowOffsets]);

  const stepSearch = useCallback(
    (dir: 1 | -1) => {
      setSearchIdx((i) => {
        const n = searchList.length;
        if (n === 0) return -1;
        if (i < 0) return dir > 0 ? 0 : n - 1;
        return (i + dir + n) % n;
      });
    },
    [searchList.length],
  );

  // ── Self-contained width resize (rAF + pointer capture + direct writes) ---
  // widthCap / spaceForced / renderedW / widthRef are declared above (the cue
  // height model needs the rendered width).

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
        controlled ? null : (
        <button
          type="button"
          data-preview-chat-panel-collapsed
          onClick={() => {
            // Re-arm playback follow when opening from the collapsed strip:
            // the effect guards on activeIdx changing, so a panel opened
            // mid-playback at an unchanged index must still jump to the
            // row under the playhead.
            prevActiveIdxRef.current = null;
            setFollow(true);
            changeOpen(true);
          }}
          className="w-7 h-full flex flex-col items-center justify-center gap-1.5 border-l-2 border-line bg-surface text-zinc-400 hover:text-white hover:bg-zinc-900"
          title={subtitlesOnly ? t('Open preview subtitles') : t('Open preview chat panel')}
        >
          {subtitlesOnly ? <Captions size={13} /> : <MessageSquare size={13} />}
          <span className="[writing-mode:vertical-rl] rotate-180 text-ui-xs font-mono uppercase tracking-widest">
            {subtitlesOnly ? t('Subs') : t('Chat')}
          </span>
        </button>
        )
      ) : (
        <div className="relative h-full flex flex-col bg-surface border-l-2 border-line min-w-0">
          <div
            data-panel-resize-handle
            onPointerDown={onResizeStart}
            className="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize z-10 group/resize"
            title={t('Resize panel')}
          >
            <div className="absolute left-0 top-0 bottom-0 w-0.5 bg-zinc-700 group-hover/resize:bg-zinc-500" />
          </div>
          <div
            role="tablist"
            aria-label={t('Preview panel sources')}
            onKeyDown={onTabListKeyDown}
            className="flex items-center gap-0.5 border-b-2 border-line px-1.5 py-1 shrink-0"
          >
            {visibleTabs.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                ref={(el) => {
                  tabRefs.current[id] = el;
                }}
                type="button"
                role="tab"
                id={`${tabIdBase}-tab-${id}`}
                aria-selected={tab === id}
                aria-controls={`${tabIdBase}-panel-${id}`}
                tabIndex={tab === id ? 0 : -1}
                data-panel-tab={id}
                onClick={() => selectTab(id)}
                className={`min-w-0 flex items-center gap-1 px-1.5 py-0.5 text-ui-xs font-mono uppercase tracking-wide font-bold transition-colors truncate ${
                  tab === id
                    ? 'bg-white text-black'
                    : 'text-zinc-400 hover:text-white hover:bg-zinc-800/60'
                }`}
              >
                <Icon size={11} className="shrink-0" />
                {t(label)}
              </button>
            ))}
            <div className="flex-1" />
            {tab === 'chat' && payload?.has_chat && platform && videoId && (
              <button
                type="button"
                title={t('Download chat history')}
                onClick={() => {
                  void apiPost('/api/archive/chat/export', { platform, video_id: videoId, full: true })
                    .then((r) => {
                      const saved = (r as { path?: string } | null)?.path;
                      if (saved) window.alert(t('Saved chat to {path}', { path: saved }));
                    })
                    .catch(() => window.alert(t('No chat history for this video')));
                }}
                className="text-zinc-500 hover:text-white p-1"
              >
                <Download size={12} />
              </button>
            )}
            <button
              type="button"
              onClick={() => changeOpen(false)}
              className="text-zinc-500 hover:text-white p-1"
              title={t('Collapse panel')}
            >
              <ChevronRight size={14} />
            </button>
          </div>
          {searchEnabled && (
            <div
              data-chat-search
              data-search-tab={tab}
              className="flex items-center gap-1.5 border-b-2 border-line px-1.5 py-1 shrink-0"
            >
              <Search size={11} className="text-zinc-500 shrink-0" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    stepSearch(e.shiftKey ? -1 : 1);
                  } else if (e.key === 'Escape') {
                    e.preventDefault();
                    setQuery('');
                  }
                }}
                placeholder={searchPlaceholder}
                aria-label={searchLabel}
                spellCheck={false}
                className="flex-1 min-w-0 bg-surface-raised border border-zinc-700 px-1.5 py-0.5 text-ui-sm text-zinc-100 outline-none focus:border-white placeholder:text-zinc-600"
              />
              {qActive && (
                <span
                  data-chat-search-count
                  className="text-ui-xs font-mono text-zinc-400 shrink-0"
                >
                  {searchList.length > 0 ? `${searchIdx + 1}/${searchList.length}` : '0/0'}
                </span>
              )}
              {/* Partial-coverage warning: the filter can only see the rows the
                  backend returned, so "no match" is not "no such message". */}
              {qActive && partialCoverage && (
                <span
                  data-chat-search-partial
                  title={t('The archive returned only part of this timeline — matches outside the loaded rows are not searchable here.')}
                  className="text-ui-xs font-mono text-amber-300/90 shrink-0"
                >
                  {t('searching {loaded} of {total}', {
                    loaded: partialCoverage.loaded,
                    total: partialCoverage.total,
                  })}
                </span>
              )}
              {qActive && (
                <>
                  <button
                    type="button"
                    onClick={() => stepSearch(-1)}
                    title={t('Previous match (Shift+Enter)')}
                    className="text-zinc-500 hover:text-white p-0.5"
                    aria-label={t('Previous match')}
                  >
                    <ChevronUp size={11} />
                  </button>
                  <button
                    type="button"
                    onClick={() => stepSearch(1)}
                    title={t('Next match (Enter)')}
                    className="text-zinc-500 hover:text-white p-0.5"
                    aria-label={t('Next match')}
                  >
                    <ChevronDown size={11} />
                  </button>
                  <button
                    type="button"
                    onClick={() => setQuery('')}
                    title={t('Clear search (Esc)')}
                    className="text-zinc-500 hover:text-white p-0.5"
                    aria-label={t('Clear search')}
                  >
                    <X size={11} />
                  </button>
                </>
              )}
            </div>
          )}
          {fetchState === 'done' && payload && tab === 'chat' && payload.has_chat && (
            <ChatMarkerChips
              markers={markers}
              onClear={handleClearMarker}
              hint={
                markers.start == null && markers.end == null
                  ? t('Hover a message to set markers')
                  : undefined
              }
            />
          )}
          {fetchState === 'loading' && (
            <div className="flex-1 min-h-0 flex items-center justify-center gap-2 text-zinc-500">
              <Loader2 size={13} className="animate-spin" />
              <span className="text-ui-sm font-mono">{t('Loading panel…')}</span>
            </div>
          )}
          {fetchState === 'error' && (
            <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 px-4">
              <span className="text-red-300 text-ui-sm font-mono text-center">
                {t("Couldn't load panel data.")}
              </span>
              <button
                type="button"
                onClick={() => setRetryTick((t) => t + 1)}
                className="flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-ui-xs font-bold uppercase tracking-wider text-red-300"
              >
                <RefreshCw size={10} />
                {t('Retry')}
              </button>
            </div>
          )}
          {fetchState === 'done' && !payload && (
            // platform/videoId resolved empty (clip/live/channel/local-file
            // previews) — there is no archive key to fetch, so say so
            // instead of rendering a silent blank panel.
            <EmptyState text={t("Chat and transcript history aren't available for this kind of preview.")} />
          )}
          {fetchState === 'done' && payload && tab === 'subtitles' && (
            <div
              role="tabpanel"
              id={`${tabIdBase}-panel-subtitles`}
              aria-labelledby={`${tabIdBase}-tab-subtitles`}
              className="flex-1 min-h-0 overflow-y-auto custom-scrollbar flex flex-col items-center justify-center gap-2 px-3 py-4"
            >
              {subtitlesOnly && subsFetchState === 'loading' && (
                <div className="flex flex-col items-center justify-center gap-2 text-zinc-500">
                  <Loader2 size={13} className="animate-spin" />
                  <span className="text-ui-sm font-mono">{t('Loading subtitles…')}</span>
                </div>
              )}
              {subtitlesOnly && subsFetchState === 'error' && (
                <div className="flex flex-col items-center justify-center gap-2 px-4">
                  <span className="text-red-300 text-ui-sm font-mono text-center">
                    {t("Couldn't load subtitles.")}
                  </span>
                  <button
                    type="button"
                    onClick={() => setRetryTick((t) => t + 1)}
                    className="flex items-center gap-1 border border-red-400/50 hover:border-red-300 hover:bg-red-500/20 px-1.5 py-0.5 text-ui-xs font-bold uppercase tracking-wider text-red-300"
                  >
                    <RefreshCw size={10} />
                    Retry
                  </button>
                </div>
              )}
              {subtitlesOnly && subsFetchState === 'done' && (!ytSubtitles?.has_subtitles || subtitleRows.length === 0) && (
                <p className="text-ui-sm font-mono text-zinc-400 text-center leading-relaxed">
                  {t('No subtitles available for this video.')}
                </p>
              )}
              {!subtitlesOnly && !payload.has_transcript && (
                <p className="text-ui-sm font-mono text-zinc-400 text-center leading-relaxed">
                  {t('No captions for this video.')}
                </p>
              )}
              {subtitleRows.length > 0 &&
                (activeSubtitleIdx >= 0 ? (
                  <>
                    <span className="text-ui-xs font-mono uppercase tracking-wide text-zinc-400 shrink-0">
                      {formatArchiveOffset(currentTime)}
                    </span>
                    <p
                      className={`text-ui-md leading-relaxed text-zinc-100 text-center break-words ${onSeek ? 'cursor-pointer select-none' : ''}`}
                      data-subtitle-line
                      onClick={
                        onSeek
                          ? () => seekToTimestamp(subtitleRows[activeSubtitleIdx].offset_sec, onSeek)
                          : undefined
                      }
                      title={
                        onSeek
                          ? t('Seek to {offset}', { offset: formatArchiveOffset(subtitleRows[activeSubtitleIdx].offset_sec) })
                          : undefined
                      }
                    >
                      {subtitleRows[activeSubtitleIdx].text}
                    </p>
                  </>
                ) : (
                  <p className="text-ui-sm font-mono text-zinc-500 text-center leading-relaxed">
                    {t('No caption at this moment.')}
                  </p>
                ))}
              {/* Search results: the caption view only ever shows ONE line, so
                  the matches get their own list (capped, seekable). */}
              {qActive && subtitleList.length > 0 && (
                <div
                  data-subtitle-matches
                  className="w-full shrink-0 mt-2 border-t border-line pt-2 flex flex-col gap-px"
                >
                  {subtitleList.slice(0, SUBTITLE_MATCH_CAP).map((r, i) => {
                    const lines = transcriptCueLines(r.text, cueTextW, false);
                    return (
                      <TranscriptRow
                        key={`s${i}`}
                        row={r}
                        active={i === searchIdx}
                        onSeek={onSeek}
                        height={transcriptCueHeight(lines)}
                        lines={lines}
                        expandable={false}
                        expanded={false}
                        onToggle={() => {}}
                        ref={i === searchIdx ? subtitleCursorRef : undefined}
                      />
                    );
                  })}
                  {subtitleList.length > SUBTITLE_MATCH_CAP && (
                    <p className="text-ui-xs font-mono text-zinc-500 text-center pt-1">
                      {t('showing first {shown} of {total} matches', {
                        shown: SUBTITLE_MATCH_CAP,
                        total: subtitleList.length,
                      })}
                    </p>
                  )}
                </div>
              )}
              {qActive && subtitleList.length === 0 && (
                <p className="text-ui-sm font-mono text-zinc-500 text-center leading-relaxed mt-2">
                  {t('No captions match “{query}”.', { query: query.trim() })}
                </p>
              )}
            </div>
          )}
          {fetchState === 'done' && payload && tab !== 'subtitles' && (
            <div
              ref={scrollRef}
              onScroll={handleScroll}
              onClick={handleRowAreaClick}
              role="tabpanel"
              id={`${tabIdBase}-panel-${tab}`}
              aria-labelledby={`${tabIdBase}-tab-${tab}`}
              className="flex-1 min-h-0 overflow-y-auto custom-scrollbar"
              data-panel-rows
            >
              {tab === 'chat' && !payload.has_chat && (
                <EmptyState
                  text={
                    payload.backfill === 'running'
                      ? t('Loading chat…')
                      : t('No archived chat for this video.')
                  }
                  progress={
                    payload.backfill === 'running' ? payload.backfill_progress : undefined
                  }
                />
              )}
              {tab === 'chat' && qActive && chatList.length === 0 && (
                <EmptyState
                  text={
                    partialCoverage
                      ? t('No match in the {loaded} loaded messages of {total} — older messages are not loaded.', {
                          loaded: partialCoverage.loaded,
                          total: partialCoverage.total,
                        })
                      : t('No chat messages match “{query}”.', { query: query.trim() })
                  }
                />
              )}
              {tab === 'transcript' && !qActive && !payload.has_transcript && timelineRows.length === 0 && (
                <EmptyState text={t('No transcript for this video.')} />
              )}
              {tab === 'transcript' && qActive && timelineList.length === 0 && (
                <EmptyState
                  text={
                    partialCoverage
                      ? t('No match in the {loaded} loaded segments — the rest of the transcript is not loaded.', {
                          loaded: partialCoverage.loaded,
                        })
                      : t('No transcript segments match “{query}”.', { query: query.trim() })
                  }
                />
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
                          emotes={emotes}
                          onSeek={onSeek}
                          markers={markers}
                          onSetMarker={handleSetMarker}
                          ref={active ? activeRowRef : undefined}
                        />
                      );
                    }
                    const tl = row as TimelineRow;
                    if (tl.kind === 'event') {
                      return (
                        <EventRow
                          key={`e${idx}`}
                          row={tl}
                          active={active}
                          height={EVENT_ROW_H}
                          onSeek={onSeek}
                          ref={active ? activeRowRef : undefined}
                        />
                      );
                    }
                    const expanded = expandedCues.has(tl.offset_sec);
                    const lines = transcriptCueLines(tl.text, cueTextW, expanded);
                    return (
                      <TranscriptRow
                        key={`t${idx}`}
                        row={tl}
                        active={active}
                        onSeek={onSeek}
                        height={transcriptCueHeight(lines)}
                        lines={lines}
                        expandable={transcriptCueExpandable(tl.text, cueTextW)}
                        expanded={expanded}
                        onToggle={toggleCue}
                        ref={active ? activeRowRef : undefined}
                      />
                    );
                  })}
                  <div style={{ height: bottomPad }} />
                </>
              )}
            </div>
          )}
          {fetchState === 'done' && payload && tab === 'chat' && payload.chat_truncated && (
            <p
              className="text-ui-xs font-mono text-zinc-500 shrink-0 px-2 pb-1 text-center"
              data-chat-truncated
            >
              {t('Chat history is incomplete — {loaded} of {total} messages shown.', {
                loaded: payload.chat.length,
                total: payload.total_rows ?? payload.chat.length,
              })}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default PreviewChatPanel;
