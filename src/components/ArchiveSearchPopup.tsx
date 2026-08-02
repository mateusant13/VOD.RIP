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
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { FileText, Loader2, MessageSquare, RefreshCw, Search, X } from 'lucide-react';
import { apiGet } from '../hooks/useApiClient';
import {
  formatArchiveOffset,
  groupChatWindow,
  highlightQuerySpans,
  snippetAroundMatch,
  type ArchiveChatMessage,
  type ArchiveSearchHit,
  type ArchiveVideoRow,
} from '../archiveSearchUtils';

interface ArchiveSearchPopupProps {
  zIndex: number;
  onClose: () => void;
  /** Open the hit in the explore-player flow (App owns the popup stack). */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => void;
}

type SearchStatus = 'idle' | 'loading' | 'done' | 'error';

const POPUP_WIDTH = 460;
const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_LIMIT = 30;
const CHAT_HALF_SEC = 30;

const platformAccent: Record<string, string> = {
  twitch: 'text-[#9146FF]',
  kick: 'text-[#53fc18]',
  youtube: 'text-[#F03030]',
};

function videoTitle(video: ArchiveVideoRow | undefined, hit: ArchiveSearchHit): string {
  const t = video?.title?.trim();
  return t ? t : hit.video_id;
}

export function ArchiveSearchPopup({ zIndex, onClose, onOpenHit }: ArchiveSearchPopupProps) {
  const [inputQuery, setInputQuery] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<SearchStatus>('idle');
  const [error, setError] = useState<string | null>(null);
  const [hits, setHits] = useState<ArchiveSearchHit[]>([]);
  const [videos, setVideos] = useState<Record<string, ArchiveVideoRow>>({});
  const [selected, setSelected] = useState<{ hit: ArchiveSearchHit; video: ArchiveVideoRow | undefined } | null>(null);
  const [chat, setChat] = useState<ArchiveChatMessage[] | null>(null);
  const [chatStatus, setChatStatus] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [chatError, setChatError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
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

  // Debounced search against the archive FTS index.
  useEffect(() => {
    if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => setQuery(inputQuery.trim()), SEARCH_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
    };
  }, [inputQuery]);

  useEffect(() => {
    if (!query) {
      searchGenRef.current += 1;
      setStatus('idle');
      setHits([]);
      setError(null);
      return;
    }
    const gen = ++searchGenRef.current;
    setStatus('loading');
    setError(null);
    void apiGet<{ hits: ArchiveSearchHit[] }>(
      `/api/archive/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`,
    )
      .then((res) => {
        if (!mountedRef.current || gen !== searchGenRef.current) return;
        setHits(res.hits ?? []);
        setStatus('done');
      })
      .catch(() => {
        if (!mountedRef.current || gen !== searchGenRef.current) return;
        setHits([]);
        setError('Archive search is unavailable — is the backend running?');
        setStatus('error');
      });
  }, [query, retryTick]);

  // Nearby chat ±30s for the selected hit.
  const selectHit = useCallback((hit: ArchiveSearchHit) => {
    const video = videos[`${(hit.platform || '').toLowerCase()}:${hit.video_id}`];
    setSelected({ hit, video });
    onOpenHit(hit, video);
  }, [videos, onOpenHit]);

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
      role="dialog"
      aria-label="Archive search"
      onKeyDown={handleKeyDown}
      className="fixed flex flex-col gap-2 p-3 border-2 border-white bg-zinc-950 shadow-2xl"
      style={{ zIndex, width: POPUP_WIDTH, top: 80, right: 24, maxHeight: '75vh' }}
    >
      <div className="flex items-center justify-between gap-2 shrink-0">
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

      <div className="flex gap-1 shrink-0">
        <div className="flex flex-1 items-center gap-1.5 bg-zinc-900 border-2 border-zinc-800 focus-within:border-white px-1.5">
          <Search size={12} className="text-zinc-500 shrink-0" />
          <input
            autoFocus
            type="text"
            value={inputQuery}
            onChange={(e) => setInputQuery(e.target.value)}
            placeholder="SEARCH TRANSCRIPTS + CHAT..."
            className="flex-1 bg-transparent text-white font-mono placeholder:text-zinc-600 text-[11px] py-1 focus:outline-none min-w-0"
          />
          {status === 'loading' && <Loader2 size={12} className="text-zinc-500 animate-spin shrink-0" />}
        </div>
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

      {status === 'done' && hits.length === 0 && (
        <p className="text-[10px] font-mono text-zinc-500 shrink-0">
          No results for &quot;{query}&quot; — nothing archived matches yet.
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
              <button
                key={`${hit.kind}:${hit.platform}:${hit.video_id}:${hit.offset_sec}`}
                type="button"
                onClick={() => selectHit(hit)}
                className={`text-left border-2 p-1.5 flex flex-col gap-1 transition-colors ${
                  isSelected
                    ? 'border-white bg-zinc-900'
                    : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-500'
                }`}
              >
                <span className="flex items-center gap-1.5 min-w-0">
                  {hit.kind === 'transcript'
                    ? <FileText size={10} className="text-zinc-400 shrink-0" />
                    : <MessageSquare size={10} className="text-zinc-400 shrink-0" />}
                  <span className="text-[8px] font-mono uppercase tracking-widest border border-zinc-700 px-1 py-px text-zinc-300 shrink-0">
                    {hit.kind}
                  </span>
                  <span className={`text-[9px] font-mono uppercase tracking-widest shrink-0 ${platformAccent[hit.platform] ?? 'text-zinc-400'}`}>
                    {hit.platform}
                  </span>
                  <span className="text-[9px] font-bold uppercase truncate text-zinc-200 min-w-0 flex-1">
                    {videoTitle(video, hit)}
                  </span>
                  <span className="text-[9px] font-mono text-zinc-400 shrink-0">
                    {formatArchiveOffset(hit.offset_sec)}
                  </span>
                </span>
                <span className="text-[10px] leading-snug text-zinc-400 break-words">
                  {video?.channel ? (
                    <span className="text-zinc-500 mr-1">@{video.channel}</span>
                  ) : null}
                  {nodes}
                </span>
              </button>
            );
          })}
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
                  <span className="text-zinc-200 font-bold">{m.username}:</span> {m.text}
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
                  <span className="text-zinc-200 font-bold">{m.username}:</span> {m.text}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ArchiveSearchPopup;
