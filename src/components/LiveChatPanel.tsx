/**
 * LIVE chat panel for the livestream player popup — real-time messages from
 * the stream's chat, docked to the right of the video (same visual language
 * as PreviewChatPanel rows). NOT history: rows stream in live over SSE from
 * the per-viewer chat stream endpoint (/api/live/chat/stream), which reuses
 * the app's existing chat sinks (Twitch anon IRC / Kick Pusher / yt-dlp
 * live_chat) with a flush callback that forwards rows instead of archiving.
 *
 * Multi-stream support: when the channel is live on >1 platform the panel
 * opens ONE merged stream per platform (each row tagged with its platform)
 * and shows filter chips (All / Kick / Twitch / YouTube) in the header —
 * the chips only render when `sources.length > 1`. Single-platform channels
 * keep the original single-stream behavior, no chips.
 *
 * Connection is EventSource (auto-reconnects); jsdom lacks EventSource, so
 * the panel degrades to an "unavailable" status there (tests).
 */
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { useI18n } from '../i18n';
import { resolveChatColor } from '../chatColors';
import { KICK_COLOR, TWITCH_COLOR, YOUTUBE_COLOR } from '../platformColors';
import { ChatEmoteText, useChatEmotes, type EmoteMap } from '../chatEmotes';

export interface LiveChatRow {
  username: string;
  text: string;
  ts?: string | null;
  color?: string | null;
  user_id?: string | number | null;
  badges?: string[];
  emotes?: string[];
}

/** One chat room to merge into the panel (per live platform). */
export interface LiveChatSource {
  /** Lowercase platform (kick/twitch/youtube). */
  platform: string;
  /** Chat room slug for that platform (login / slug / @handle). */
  slug: string;
}

/** Rows kept in the live window — drop the oldest past this (keeps the DOM
 *  bounded on long streams; the archive owns persistence). */
const MAX_ROWS = 300;
/** Panel width — the popup's aspect-lock reserves this from the video area. */
export const LIVE_CHAT_PANEL_W = 260;

function rowTime(ts: string | null | undefined): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function platformColor(platform: string): string {
  if (platform === 'kick') return KICK_COLOR;
  if (platform === 'twitch') return TWITCH_COLOR;
  if (platform === 'youtube') return YOUTUBE_COLOR;
  return '#a1a1aa';
}

function platformLabel(platform: string): string {
  if (platform === 'kick') return 'Kick';
  if (platform === 'twitch') return 'Twitch';
  if (platform === 'youtube') return 'YouTube';
  return platform;
}

const ChatRow = memo(function ChatRow({
  row,
  showPlatform,
  emotes,
}: {
  row: LiveChatRow & { platform: string };
  showPlatform: boolean;
  emotes: EmoteMap;
}) {
  const { t } = useI18n();
  const time = rowTime(row.ts);
  return (
    <div
      data-live-chat-row
      className="flex items-baseline gap-1 px-2 py-0.5 overflow-hidden border-l-2 border-transparent whitespace-nowrap text-zinc-200 hover:bg-zinc-900/70"
      title={row.text}
    >
      {time ? (
        <span className="text-zinc-500 font-mono text-[9px] shrink-0">{time}</span>
      ) : null}
      {showPlatform ? (
        <span
          className="text-[8px] font-mono uppercase shrink-0"
          style={{ color: platformColor(row.platform) }}
        >
          {platformLabel(row.platform)}
        </span>
      ) : null}
      <span
        className="font-bold text-[10px] shrink-0"
        style={{ color: resolveChatColor(row.color, row.username, row.platform) }}
      >
        {row.username}:
      </span>
      <span className="text-[10px] leading-snug truncate" title={row.text}>
        <ChatEmoteText text={row.text} emotes={emotes} />
      </span>
      {row.badges?.length ? (
        <span className="text-[8px] font-mono text-zinc-500 shrink-0" title={t('Badges')}>
          {row.badges.join(' ')}
        </span>
      ) : null}
    </div>
  );
});

type ChatStatus = 'connecting' | 'live' | 'reconnecting' | 'unsupported' | 'offline';

interface LiveChatPanelProps {
  /** Chat rooms to merge — one EventSource per source. */
  sources: LiveChatSource[];
  /** Close (collapse) the docked panel. */
  onClose: () => void;
}

export default function LiveChatPanel({ sources, onClose }: LiveChatPanelProps) {
  const { t } = useI18n();
  const [rows, setRows] = useState<(LiveChatRow & { platform: string })[]>([]);
  const [status, setStatus] = useState<ChatStatus>('connecting');
  const [filter, setFilter] = useState('all');
  const scrollRef = useRef<HTMLDivElement>(null);
  const esRefs = useRef<EventSource[]>([]);

  const multi = sources.length > 1;
  // Stable chip order: kick, twitch, youtube (deduped).
  const platforms = useMemo(() => {
    const order = { kick: 0, twitch: 1, youtube: 2 } as Record<string, number>;
    return [...new Set(sources.map((s) => s.platform))].sort(
      (a, b) => (order[a] ?? 9) - (order[b] ?? 9),
    );
  }, [sources]);

  useEffect(() => {
    if (sources.length === 0) {
      setStatus('offline');
      return;
    }
    if (typeof EventSource === 'undefined') {
      // jsdom has no EventSource — tests render the panel in this state.
      setStatus('unsupported');
      return;
    }
    setRows([]);
    setStatus('connecting');
    const esList: EventSource[] = [];
    for (const src of sources) {
      const q = new URLSearchParams({ platform: src.platform, slug: src.slug });
      const es = new EventSource(`/api/live/chat/stream?${q.toString()}`);
      es.onopen = () => setStatus('live');
      es.onmessage = (ev: MessageEvent) => {
        try {
          const row = JSON.parse(ev.data) as LiveChatRow;
          if (!row || typeof row.username !== 'string') return;
          const tagged = { ...row, platform: src.platform };
          setRows((prev) =>
            prev.length >= MAX_ROWS ? [...prev.slice(prev.length - MAX_ROWS + 1), tagged] : [...prev, tagged],
          );
        } catch {
          // Ignore malformed frames — keep the stream alive.
        }
      };
      es.onerror = () => {
        // EventSource auto-reconnects; surface the flap without killing the panel.
        setStatus((prev) => (prev === 'live' ? 'reconnecting' : prev === 'connecting' ? 'connecting' : prev));
      };
      esList.push(es);
    }
    esRefs.current = esList;
    return () => {
      esList.forEach((es) => es.close());
      esRefs.current = [];
    };
  }, [sources]);

  // Channel emotes for the panel's twitch source (kick/youtube have no
  // custom emotes). One map for the whole merged panel — multi-source rows
  // share the twitch channel's emotes. The map reference is stable (cache),
  // so memoized ChatRows only re-render once when the fetch resolves.
  const twitchSlug = useMemo(
    () => sources.find((s) => s.platform === 'twitch')?.slug?.trim() || null,
    [sources],
  );
  const emotes = useChatEmotes(twitchSlug ? 'twitch' : null, twitchSlug ?? undefined);

  // Rows visible under the active filter (all → every platform).
  const visibleRows = useMemo(
    () => (filter === 'all' ? rows : rows.filter((r) => r.platform === filter)),
    [rows, filter],
  );

  // Follow the live edge (only when the user is already at the bottom).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !visibleRows.length) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }, [visibleRows]);

  const statusLine = status === 'live'
    ? t('Live chat connected')
    : status === 'connecting' ? t('Connecting…')
    : status === 'reconnecting' ? t('Reconnecting…')
    : status === 'unsupported' ? t('Live chat unavailable')
    : t('Live chat offline');

  return (
    <div
      data-live-chat-panel
      className="flex flex-col bg-zinc-950 border-l-2 border-zinc-800 shrink-0"
      style={{ width: LIVE_CHAT_PANEL_W }}
    >
      <div className="flex items-center justify-between gap-1 px-2 py-1 bg-zinc-900 border-b border-zinc-800 shrink-0">
        <span className="flex items-center gap-1 text-[8px] font-mono uppercase tracking-widest text-zinc-400">
          <span className={`h-1.5 w-1.5 rounded-full ${status === 'live' ? 'bg-emerald-500 animate-pulse' : status === 'reconnecting' ? 'bg-amber-500' : 'bg-zinc-600'}`} />
          {t('Live chat')}
        </span>
        <button
          type="button"
          onClick={onClose}
          title={t('Close live chat')}
          className="text-zinc-500 hover:text-white p-0.5 shrink-0"
        >
          <X size={12} />
        </button>
      </div>
      {multi ? (
        <div className="flex items-center gap-0.5 px-1 py-0.5 bg-zinc-900 border-b border-zinc-800 shrink-0" data-live-chat-filters>
          {['all', ...platforms].map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setFilter(p)}
              data-chat-filter={p}
              className={`px-1 py-0.5 text-[8px] font-mono uppercase tracking-wider border transition-colors ${
                filter === p
                  ? 'bg-white text-black border-white'
                  : 'text-zinc-400 border-zinc-800 hover:text-white hover:border-zinc-500'
              }`}
            >
              {p === 'all' ? t('All') : platformLabel(p)}
            </button>
          ))}
        </div>
      ) : null}
      <div ref={scrollRef} className="flex-1 overflow-y-auto min-h-0" data-live-chat-scroll>
        {visibleRows.length === 0 ? (
          <div className="px-2 py-2 text-[10px] font-mono text-zinc-500">
            {status === 'live' ? t('Waiting for chat…') : statusLine}
          </div>
        ) : (
          visibleRows.map((row, i) => (
            <ChatRow key={`${row.platform}-${row.user_id ?? ''}-${i}`} row={row} showPlatform={multi} emotes={emotes} />
          ))
        )}
      </div>
    </div>
  );
}
