
import { useCallback, useEffect, useState } from 'react';
import { Clapperboard, Download, ExternalLink, FolderOpen, MessageSquare, RefreshCw, Trash2 } from 'lucide-react';
import { vodCheckboxStyle, platformAccentColor } from '../platformColors';
import { ActiveDownloadsList } from './ActiveDownloadsList';
import DownloadThumb from './DownloadThumb';
import PlatformVodIcon from './PlatformVodIcon';
import type { DownloadState } from '../types';
import { useI18n } from '../i18n';
import { deleteTwitchClipHistory, fetchTwitchClipChat, fetchTwitchClipHistory, type TwitchClipRecord, type TwitchClipChat } from '../twitchClip';
import { formatHmsFull } from '../utils';
import { formatArchiveOffset } from '../archiveSearchUtils';
import { resolveChatColor } from '../chatColors';
import NotificationsPanel from './NotificationsPanel';

function isPlayableLocalFile(path: string): boolean {
  return /\.(mp4|mkv|webm|mov|m4v)$/i.test(path);
}

/** One row of GET /api/archive/jobs (Notifications tab). `title` is enriched
 *  by the backend router (LEFT JOIN videos) and may be '' when the video row
 *  is absent. */
export interface ArchiveJobRow {
  id: string;
  kind: 'ingest' | 'chat' | 'transcribe' | 'events' | string;
  platform: string;
  video_id: string;
  status: 'queued' | 'running' | 'done' | 'failed' | string;
  progress: number; // 0..1
  error: string | null;
  created_at: string;
  updated_at: string;
  heartbeat: string | null;
  title?: string;
  /** TASK10 retry bookkeeping — absent on older backends; all optional. */
  attempts?: number;
  max_attempts?: number;
  next_retry_at?: string | null;
}

/** TASK10: a queued job with attempts > 0 was auto-requeued after a failure —
 *  an informational retry, NOT a final failure (only status 'failed' is final). */
export const isRetryJob = (j: Pick<ArchiveJobRow, 'status' | 'attempts'>): boolean =>
  j.status === 'queued' && (j.attempts ?? 0) > 0;

type Props = {
  queueDownloads: DownloadState[];
  recentDownloads?: DownloadState[];
  historyDownloads: DownloadState[];
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onCancel: (id: string) => void;
  onDelete: (id: string) => void;
  onDeleteHistory: (id: string) => void;
  onOpenFolder: (path: string) => void;
  onRefresh: () => void;
  basename: (path: string) => string;
  selectedQueueIds?: Set<string>;
  selectedHistoryIds?: Set<string>;
  onToggleQueueSelection?: (id: string) => void;
  onToggleHistorySelection?: (id: string) => void;
  onBulkDeleteQueue?: () => void;
  onBulkDeleteHistory?: () => void;
  selectedRecentIds?: Set<string>;
  onToggleRecentSelection?: (id: string) => void;
  onBulkDeleteRecent?: () => void;
  onWatchLocal?: (dl: DownloadState) => void;
  /** Click a history row's title to open that VOD in the main preview (URL tab).
   *  The optional hint lets the caller skip the redundant /api/info/video
   *  re-extract when the row already carries title metadata. */
  onOpenVod?: (url: string, hint?: { title?: string; durationSec?: number; skipNetwork?: boolean }) => void;
  /** Download a Twitch clip from the clip-history row (enqueues via /api/download/clip). */
  onDownloadClip?: (clip: TwitchClipRecord) => void;
};

export default function QueueTab({
  queueDownloads,
  recentDownloads = [],
  historyDownloads,
  onPause,
  onResume,
  onCancel,
  onDelete,
  onDeleteHistory,
  onOpenFolder,
  onRefresh,
  basename,
  selectedQueueIds,
  selectedHistoryIds,
  onToggleQueueSelection,
  onToggleHistorySelection,
  onBulkDeleteQueue,
  onBulkDeleteHistory,
  selectedRecentIds,
  onToggleRecentSelection,
  onBulkDeleteRecent,
  onWatchLocal,
  onOpenVod,
  onDownloadClip,
}: Props) {
  const queueAllSelected = queueDownloads.length > 0 && selectedQueueIds?.size === queueDownloads.length;
  const recentAllSelected = recentDownloads.length > 0 && selectedRecentIds?.size === recentDownloads.length;
  const historyAllSelected = historyDownloads.length > 0 && selectedHistoryIds?.size === historyDownloads.length;
  const { t } = useI18n();
  const [historyView, setHistoryView] = useState<'downloads' | 'notifications'>('downloads');
  const [twitchClips, setTwitchClips] = useState<TwitchClipRecord[]>([]);
  const [twitchClipsLoading, setTwitchClipsLoading] = useState(false);
  const [selectedClipIds, setSelectedClipIds] = useState<Set<string>>(new Set());
  const [clipDeleteError, setClipDeleteError] = useState<string | null>(null);
  const loadTwitchClips = () => {
    setTwitchClipsLoading(true);
    fetchTwitchClipHistory()
      .then((rows) => setTwitchClips(rows))
      .catch(() => setTwitchClips([]))
      .finally(() => setTwitchClipsLoading(false));
  };
  const toggleClipSelection = (id: string) => {
    setSelectedClipIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const allClipsSelected = twitchClips.length > 0
    && twitchClips.every((c) => selectedClipIds.has(c.id));
  const toggleAllClips = () => {
    setSelectedClipIds(allClipsSelected ? new Set() : new Set(twitchClips.map((c) => c.id)));
  };
  const handleBulkDeleteClips = async () => {
    if (selectedClipIds.size === 0) return;
    if (!window.confirm(t('Remove {count} Twitch clip(s) from history?', { count: selectedClipIds.size }))) return;
    setClipDeleteError(null);
    try {
      await deleteTwitchClipHistory([...selectedClipIds]);
      setSelectedClipIds(new Set());
      loadTwitchClips();
    } catch {
      setClipDeleteError(t('Failed to delete Twitch clips'));
    }
  };
  /** Open/close the windowed source-VOD chat viewer for one clip history row.
   *  Fetched on demand; closing the panel drops the loaded payload. */
  const [clipChatOpen, setClipChatOpen] = useState<string | null>(null);
  const [clipChat, setClipChat] = useState<TwitchClipChat | null>(null);
  const [clipChatLoading, setClipChatLoading] = useState(false);
  const [clipChatError, setClipChatError] = useState<string | null>(null);
  const toggleClipChat = useCallback((clip: TwitchClipRecord) => {
    if (clipChatOpen === clip.id) {
      setClipChatOpen(null);
      setClipChat(null);
      return;
    }
    setClipChatOpen(clip.id);
    setClipChat(null);
    setClipChatError(null);
    setClipChatLoading(true);
    fetchTwitchClipChat(clip.id)
      .then(setClipChat)
      .catch(() => setClipChatError(t('Could not load chat history.')))
      .finally(() => setClipChatLoading(false));
  }, [clipChatOpen, t]);
  useEffect(() => {
    loadTwitchClips();
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <button type="button" onClick={() => setHistoryView('downloads')}
            className={`text-[9px] font-bold uppercase tracking-widest px-2 py-0.5 border-2 ${historyView === 'downloads' ? 'border-white bg-white text-black' : 'border-zinc-800 text-zinc-500 hover:text-white'}`}>
            {t('History')}
          </button>
          <button type="button" onClick={() => setHistoryView('notifications')}
            className={`text-[9px] font-bold uppercase tracking-widest px-2 py-0.5 border-2 ${historyView === 'notifications' ? 'border-white bg-white text-black' : 'border-zinc-800 text-zinc-500 hover:text-white'}`}>
            {t('Notifications')}
          </button>
        </div>
        <div className="flex items-center gap-2">
          {selectedQueueIds && selectedQueueIds.size > 0 && (
            <button
              type="button"
              onClick={onBulkDeleteQueue}
              className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
            >
              <Trash2 size={12} /> {t('Delete {count}', { count: selectedQueueIds.size })}
            </button>
          )}
          <button onClick={onRefresh} className="text-zinc-500 hover:text-white transition-colors">
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      {historyView === 'notifications' ? (
        <NotificationsPanel />
      ) : (
      <>
      {queueDownloads.length > 0 && onToggleQueueSelection && (
        <div className="flex items-center gap-2 -mt-2">                          <label className="flex items-center gap-1.5 text-[9px] font-mono text-zinc-500 cursor-pointer hover:text-zinc-300">
            <input
              type="checkbox"
              checked={queueAllSelected}
              onChange={() => {
                if (queueAllSelected) {
                  queueDownloads.forEach((d) => onToggleQueueSelection?.(d.download_id));
                } else {
                  queueDownloads.forEach((d) => {
                    if (!selectedQueueIds?.has(d.download_id)) onToggleQueueSelection?.(d.download_id);
                  });
                }
              }}
              className="shrink-0"
              style={vodCheckboxStyle('#fafafa')}
            />
            {t('Select all')}
          </label>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <ActiveDownloadsList
          downloads={queueDownloads}
          onPause={onPause}
          onResume={onResume}
          onCancel={onCancel}
          onDelete={onDelete}
          onOpenFolder={onOpenFolder}
          basename={basename}
          platformIcon={(platform, className) => (
            <PlatformVodIcon platform={platform} className={className} />
          )}
          showCheckbox={Boolean(onToggleQueueSelection)}
          selectedIds={selectedQueueIds}
          onToggleSelect={onToggleQueueSelection}
        />
      </div>

      {recentDownloads.length > 0 && (
        <div className="border-t-2 border-zinc-800 pt-3 flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
              {t('Recent')}
            </span>
            {selectedRecentIds && selectedRecentIds.size > 0 && (
              <button
                type="button"
                onClick={onBulkDeleteRecent}
                className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
              >
                <Trash2 size={12} /> {t('Delete {count}', { count: selectedRecentIds.size })}
              </button>
            )}
          </div>
          {recentDownloads.length > 0 && onToggleRecentSelection && (
            <div className="flex items-center gap-2 -mt-1">
              <label className="flex items-center gap-1.5 text-[9px] font-mono text-zinc-500 cursor-pointer hover:text-zinc-300">
                <input
                  type="checkbox"
                  checked={recentAllSelected}
                  onChange={() => {
                    if (selectedRecentIds?.size === recentDownloads.length) {
                      recentDownloads.forEach((d) => onToggleRecentSelection?.(d.download_id));
                    } else {
                      recentDownloads.forEach((d) => {
                        if (!selectedRecentIds?.has(d.download_id)) onToggleRecentSelection?.(d.download_id);
                      });
                    }
                  }}
                  className="shrink-0"
              style={vodCheckboxStyle('#fafafa')}
                />
                {t('Select all')}
              </label>
            </div>
          )}
          <div className="flex flex-col gap-2">
            <ActiveDownloadsList
              downloads={recentDownloads}
              onPause={onPause}
              onResume={onResume}
              onCancel={onCancel}
              onDelete={onDelete}
              onOpenFolder={onOpenFolder}
              basename={basename}
              platformIcon={(platform, className) => (
                <PlatformVodIcon platform={platform} className={className} />
              )}
              showCheckbox={Boolean(onToggleRecentSelection)}
              selectedIds={selectedRecentIds}
              onToggleSelect={onToggleRecentSelection}
            />
          </div>
        </div>
      )}

      <div className="border-t-2 border-zinc-800 pt-3 flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
            {t('History')}
          </span>
          {selectedHistoryIds && selectedHistoryIds.size > 0 && (
            <button
              type="button"
              onClick={onBulkDeleteHistory}
              className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
            >
              <Trash2 size={12} /> {t('Delete {count}', { count: selectedHistoryIds.size })}
            </button>
          )}
        </div>
        {historyDownloads.length > 0 && onToggleHistorySelection && (
          <div className="flex items-center gap-2 -mt-1">
            <label className="flex items-center gap-1.5 text-[9px] font-mono text-zinc-500 cursor-pointer hover:text-zinc-300">
              <input
                type="checkbox"
                checked={historyAllSelected}
                onChange={() => {
                  if (historyAllSelected) {
                    historyDownloads.forEach((d) => onToggleHistorySelection?.(d.download_id));
                  } else {
                    historyDownloads.forEach((d) => {
                      if (!selectedHistoryIds?.has(d.download_id)) onToggleHistorySelection?.(d.download_id);
                    });
                  }
                }}
                className="shrink-0"
              style={vodCheckboxStyle('#fafafa')}
              />
              {t('Select all')}
            </label>
          </div>
        )}
        <div className="flex flex-col gap-2">
          {historyDownloads.length === 0 ? (
            <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
              {t('NO COMPLETED DOWNLOADS YET.')}
            </div>
          ) : historyDownloads.map((dl) => {
            const checked = selectedHistoryIds?.has(dl.download_id);
            const canWatch = Boolean(onWatchLocal && dl.output_file && isPlayableLocalFile(dl.output_file));
            return (
              <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-950 p-2 flex gap-3">
                <DownloadThumb
                  thumbnail={dl.thumbnail}
                  url={dl.url}
                  platform={dl.platform}
                  // ponytail: thumbnail 2x for readability (w-12 h-9 → w-20 h-12); revert = drop className + this comment
                  className="w-20 h-12"
                  watchable={canWatch}
                  onWatch={canWatch ? () => onWatchLocal!(dl) : undefined}
                />
                <div className="flex flex-col gap-1.5 min-w-0 flex-1">
                <div className="flex justify-between items-center gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    {onToggleHistorySelection ? (
                      <label
                        className="flex items-center gap-2 shrink-0 cursor-pointer"
                        onClick={(e) => {
                          e.preventDefault();
                          onToggleHistorySelection(dl.download_id);
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={!!checked}
                          readOnly
                          tabIndex={-1}
                          className="shrink-0 pointer-events-none"
                          style={vodCheckboxStyle(platformAccentColor(dl.platform))}
                        />
                        <PlatformVodIcon platform={dl.platform} className="w-4 h-4" />
                      </label>
                    ) : (
                      <PlatformVodIcon platform={dl.platform} className="w-4 h-4" />
                    )}
                    {onOpenVod ? (
                      <button
                        type="button"
                        onClick={() => onOpenVod(dl.url, { title: dl.title || undefined, skipNetwork: true })}
                        title={t('Open preview for this VOD')}
                        className="text-xs font-mono text-zinc-300 truncate min-w-0 text-left hover:text-white cursor-pointer"
                      >
                        {dl.title || dl.url}
                      </button>
                    ) : (
                      <span className="text-xs font-mono text-zinc-300 truncate">
                        {dl.title || dl.url}
                      </span>
                    )}
                  </div>
                  <span className="text-[10px] font-mono shrink-0 text-[#53fc18]">{dl.status}</span>
                </div>
                <div className="flex justify-between items-center text-[10px] text-zinc-500 font-mono gap-2">
                  <span className="truncate">{basename(dl.output_file)}</span>
                  <div className="flex items-center gap-2 shrink-0">
                    {dl.output_file && (
                      <button
                        type="button"
                        onClick={() => onOpenFolder(dl.output_file)}
                        className="text-zinc-400 hover:text-white flex items-center gap-1"
                        title={t('Show in folder')}
                      >
                        <FolderOpen size={12} /> {t('Folder')}
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onDeleteHistory(dl.download_id)}
                      className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                      title={t('Remove from history')}
                    >
                      <Trash2 size={12} /> {t('Delete')}
                    </button>
                  </div>
                </div>
                {dl.error && <span className="text-[10px] text-red-400 font-mono">{dl.error}</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="border-t-2 border-zinc-800 pt-3 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500 flex items-center gap-1.5">
            <Clapperboard size={11} className="text-[#9146FF]" />
            {t('Twitch Clips')}
          </span>
          <div className="flex items-center gap-2">
            {clipDeleteError && (
              <span className="text-[10px] font-mono text-red-400">{clipDeleteError}</span>
            )}
            {twitchClips.length > 0 && (
              <label
                className="flex items-center gap-1.5 text-[9px] font-mono text-zinc-500 cursor-pointer hover:text-zinc-300"
                title={t('Select all clips')}
              >
                <input
                  type="checkbox"
                  checked={allClipsSelected}
                  onChange={toggleAllClips}
                  className="shrink-0"
                  style={vodCheckboxStyle('#fafafa')}
                />
                {t('Select all')}
              </label>
            )}
            {selectedClipIds.size > 0 && (
              <button
                type="button"
                onClick={() => void handleBulkDeleteClips()}
                className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
                title={t('Delete selected clips')}
              >
                <Trash2 size={12} /> {t('Delete {count}', { count: selectedClipIds.size })}
              </button>
            )}
            <button
              type="button"
              onClick={loadTwitchClips}
              className={`text-zinc-500 hover:text-white transition-colors ${twitchClipsLoading ? 'animate-spin' : ''}`}
              title={t('Refresh Twitch clip history')}
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </div>
        {twitchClips.length === 0 ? (
          <div className="text-center text-zinc-600 font-mono text-xs py-4 border-2 border-dashed border-zinc-800">
            {t('NO TWITCH CLIPS YET — use the CLIP button in a Twitch preview.')}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {twitchClips.map((c) => (
              <div key={c.id} className="flex flex-col gap-1.5">
              <div className="border-2 border-zinc-800 bg-zinc-950 p-2 flex items-center gap-3">
                <input
                  type="checkbox"
                  checked={selectedClipIds.has(c.id)}
                  onChange={() => toggleClipSelection(c.id)}
                  aria-label={t('Select clip')}
                  className="shrink-0"
                  style={vodCheckboxStyle('#9146FF')}
                />
                {c.thumbnail_url ? (
                  <img
                    src={c.thumbnail_url}
                    alt=""
                    className="shrink-0 w-16 h-9 object-cover border border-zinc-800 bg-zinc-900"
                    onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
                  />
                ) : (
                  <Clapperboard size={14} className="shrink-0 text-[#9146FF]" />
                )}
                <div className="flex flex-col gap-0.5 min-w-0 flex-1">
                  <span className="text-xs font-mono text-zinc-300 truncate">
                    {c.channel}
                    {c.vod_id ? ` · ${t('VOD {id}', { id: c.vod_id })}` : ` · ${t('live')}`}
                    {c.duration_sec ? ` · ${Math.round(c.duration_sec)}s` : ''}
                  </span>
                  <span className="text-[10px] font-mono text-zinc-500 truncate">
                    {c.offset_sec != null ? `${t('offset {time}', { time: formatHmsFull(c.offset_sec) })} · ` : ''}
                    {new Date(c.created_at).toLocaleString()}
                  </span>
                </div>
                {/^https?:\/\//.test(c.url) ? (
                  <>
                    <a
                      href={c.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-zinc-400 hover:text-white flex items-center gap-1 text-[10px] font-mono uppercase tracking-wider shrink-0"
                      title={t('Open Twitch clip editor')}
                    >
                      <ExternalLink size={12} /> {t('Editor')}
                    </a>
                    <a
                      href={c.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={t('Open in browser')}
                      title={t('Open in browser')}
                      className="text-zinc-500 hover:text-white p-1 shrink-0"
                    >
                      <ExternalLink size={11} />
                    </a>
                  </>
                ) : (
                  <span className="text-zinc-700 text-[10px] font-mono uppercase tracking-wider shrink-0">
                    {t('—')}
                  </span>
                )}
                {onDownloadClip && (
                  <button
                    type="button"
                    onClick={() => onDownloadClip(c)}
                    className="text-[#53fc18] hover:text-white flex items-center gap-1 text-[10px] font-mono uppercase tracking-wider shrink-0"
                    title={t('Download clip')}
                  >
                    <Download size={12} /> {t('Download')}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => toggleClipChat(c)}
                  className="text-[#1f8fff] hover:text-white flex items-center gap-1 text-[10px] font-mono uppercase tracking-wider shrink-0"
                  title={t('Clip chat history')}
                >
                  <MessageSquare size={12} /> {t('Chat')}
                </button>
              </div>
              {clipChatOpen === c.id && (
                <div className="border-2 border-zinc-800 bg-zinc-900/60 p-2 flex flex-col gap-1 max-h-56 overflow-y-auto custom-scrollbar">
                  {clipChatLoading && (
                    <p className="text-[10px] font-mono text-zinc-500">{t('Loading chat history...')}</p>
                  )}
                  {clipChatError && (
                    <p className="text-[10px] font-mono text-red-400">{clipChatError}</p>
                  )}
                  {!clipChatLoading && !clipChatError && clipChat && clipChat.messages.length === 0 && (
                    <p className="text-[10px] font-mono text-zinc-500">{t('No archived chat for this clip.')}</p>
                  )}
                  {!clipChatLoading && !clipChatError && clipChat && clipChat.messages.length > 0 && (
                    <>
                      {clipChat.messages.map((m) => (
                        <p
                          key={`c:${m.video_id}:${m.offset_sec}:${m.username}:${m.text}`}
                          className="text-[10px] leading-snug text-zinc-200 break-words"
                        >
                          <span className="text-zinc-400 font-mono mr-1">{formatArchiveOffset(m.offset_sec)}</span>
                          <span className="font-bold" style={{ color: resolveChatColor(m.color, m.username, m.platform) }}>{m.username}:</span> {m.text}
                          {typeof m.spam_count === 'number' && m.spam_count > 1 && (
                            <span
                              className="text-[9px] font-mono text-zinc-500 ml-1"
                              title={t('{count} identical messages collapsed', { count: m.spam_count })}
                            >
                              ×{m.spam_count}
                            </span>
                          )}
                        </p>
                      ))}
                      {clipChat.truncated && (
                        <p className="text-[9px] font-mono text-zinc-600">
                          {t('History cut at the archive cap — showing the first {shown} of {total} messages.', {
                            shown: clipChat.messages.length,
                            total: clipChat.total,
                          })}
                        </p>
                      )}
                    </>
                  )}
                </div>
              )}
              </div>
            ))}
          </div>
        )}
      </div>
      </>
      )}
    </div>
  );
}
