/**
 * Start/end chat-range markers — shared by every chat-history surface
 * (preview chat panel, QueueTab clip chat viewer).
 *
 * Each message row shows two hover-revealed affordances (green S = start,
 * red E = end). Hover is pure CSS (group-hover + opacity transition) — no
 * per-row React state and no re-render storms; the host keeps ONE
 * (start, end) pair per chat surface. Clicking sets the boundary; the row
 * that IS a marker shows its filled badge without hover. The chips header
 * shows which offsets are set (green = start, red = end) and clears them.
 *
 * The pair feeds the next download: when a marker is set, the download also
 * writes <media>.chat.txt with the messages inside [start, end].
 */

import { X } from 'lucide-react';
import { useI18n } from '../i18n';
import { formatArchiveOffset } from '../archiveSearchUtils';

export type ChatMarkerKind = 'start' | 'end';

export interface ChatMarkers {
  start: number | null;
  end: number | null;
}

export const EMPTY_CHAT_MARKERS: ChatMarkers = { start: null, end: null };

/** Clamp helper: setting start bumps a too-small end up to it, setting end
 *  pulls a too-large start down to it, so the stored pair always describes a
 *  valid [start, end] range (never an inverted one the txt writer would
 *  silently turn into an empty file). */
export function applyChatMarker(
  kind: ChatMarkerKind,
  offset: number,
  prev: ChatMarkers,
): ChatMarkers {
  if (kind === 'start') {
    const end = prev.end != null && prev.end < offset ? offset : prev.end;
    return { start: offset, end };
  }
  const start = prev.start != null && prev.start > offset ? offset : prev.start;
  return { start, end: offset };
}

/** Row overlay: green/red marker buttons revealed on row hover (pure CSS),
 *  filled when the row IS the set marker. Stop-propagation keeps the click
 *  from triggering the row's own click (seek). The parent row must be
 *  `relative` and carry the `group/marker` Tailwind class. */
export function ChatRowMarkers({
  offsetSec,
  markers,
  onSetMarker,
}: {
  offsetSec: number;
  markers: ChatMarkers;
  onSetMarker: (kind: ChatMarkerKind, offsetSec: number) => void;
}) {
  const { t } = useI18n();
  const isStart = markers.start != null && offsetSec === markers.start;
  const isEnd = markers.end != null && offsetSec === markers.end;
  return (
    <span
      data-chat-row-markers
      onClick={(e) => e.stopPropagation()}
      className={`absolute right-0 top-0 bottom-0 z-10 flex items-center gap-0.5 pr-1 pl-1 bg-zinc-950/90 transition-opacity duration-100 ${
        isStart || isEnd ? 'opacity-100' : 'opacity-0 group-hover/marker:opacity-100'
      }`}
    >
      <button
        type="button"
        data-marker-set="start"
        aria-label={t('Set start marker')}
        title={t('Set start marker')}
        onClick={(e) => {
          e.stopPropagation();
          onSetMarker('start', offsetSec);
        }}
        className={`w-3.5 h-3.5 shrink-0 rounded-full border text-[7px] font-black leading-none flex items-center justify-center ${
          isStart
            ? 'bg-[#53fc18] border-[#53fc18] text-black'
            : 'border-[#53fc18] text-[#53fc18] hover:bg-[#53fc18] hover:text-black'
        }`}
      >
        S
      </button>
      <button
        type="button"
        data-marker-set="end"
        aria-label={t('Set end marker')}
        title={t('Set end marker')}
        onClick={(e) => {
          e.stopPropagation();
          onSetMarker('end', offsetSec);
        }}
        className={`w-3.5 h-3.5 shrink-0 rounded-full border text-[7px] font-black leading-none flex items-center justify-center ${
          isEnd
            ? 'bg-[#ef4444] border-[#ef4444] text-white'
            : 'border-[#ef4444] text-[#ef4444] hover:bg-[#ef4444] hover:text-white'
        }`}
      >
        E
      </button>
    </span>
  );
}

/** Compact header showing the set range (green START / red END chips) with
 *  click-to-clear, plus an optional hint while nothing is set. */
export function ChatMarkerChips({
  markers,
  onClear,
  hint,
}: {
  markers: ChatMarkers;
  onClear: (kind: ChatMarkerKind) => void;
  hint?: string;
}) {
  const { t } = useI18n();
  return (
    <div
      data-chat-markers
      className="flex items-center gap-1.5 border-b-2 border-zinc-800 px-1.5 py-1 shrink-0 min-h-[26px]"
    >
      <button
        type="button"
        data-marker-chip="start"
        aria-label={t('Set start marker')}
        title={markers.start != null ? t('Clear start marker') : undefined}
        onClick={() => {
          if (markers.start != null) onClear('start');
        }}
        className={`flex items-center gap-1 border px-1 py-0.5 text-[8px] font-mono font-bold uppercase tracking-wider ${
          markers.start != null
            ? 'border-[#53fc18] bg-[#53fc18]/10 text-[#53fc18]'
            : 'border-zinc-700 text-zinc-600 hover:text-zinc-400'
        }`}
      >
        {t('Start')}
        <span className="tabular-nums normal-case">
          {markers.start != null ? formatArchiveOffset(markers.start) : '—'}
        </span>
        {markers.start != null && <X size={8} className="shrink-0" />}
      </button>
      <button
        type="button"
        data-marker-chip="end"
        aria-label={t('Set end marker')}
        title={markers.end != null ? t('Clear end marker') : undefined}
        onClick={() => {
          if (markers.end != null) onClear('end');
        }}
        className={`flex items-center gap-1 border px-1 py-0.5 text-[8px] font-mono font-bold uppercase tracking-wider ${
          markers.end != null
            ? 'border-[#ef4444] bg-[#ef4444]/10 text-[#ef4444]'
            : 'border-zinc-700 text-zinc-600 hover:text-zinc-400'
        }`}
      >
        {t('End')}
        <span className="tabular-nums normal-case">
          {markers.end != null ? formatArchiveOffset(markers.end) : '—'}
        </span>
        {markers.end != null && <X size={8} className="shrink-0" />}
      </button>
      {hint && <span className="text-[8px] font-mono text-zinc-600 truncate">{hint}</span>}
    </div>
  );
}
