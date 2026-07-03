
import { FolderOpen, RefreshCw, Trash2 } from 'lucide-react';
import { ActiveDownloadsList } from './ActiveDownloadsList';
import PlatformVodIcon from './PlatformVodIcon';
import type { DownloadState } from '../types';

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
}: Props) {
  const queueAllSelected = queueDownloads.length > 0 && selectedQueueIds?.size === queueDownloads.length;
  const recentAllSelected = recentDownloads.length > 0 && selectedRecentIds?.size === recentDownloads.length;
  const historyAllSelected = historyDownloads.length > 0 && selectedHistoryIds?.size === historyDownloads.length;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
          Queue
        </span>
        <div className="flex items-center gap-2">
          {selectedQueueIds && selectedQueueIds.size > 0 && (
            <button
              type="button"
              onClick={onBulkDeleteQueue}
              className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
            >
              <Trash2 size={12} /> Delete {selectedQueueIds.size}
            </button>
          )}
          <button onClick={onRefresh} className="text-zinc-500 hover:text-white transition-colors">
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

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
              className="accent-[#53fc18]"
            />
            Select all
          </label>
        </div>
      )}

      <div className="flex flex-col gap-2 max-h-[240px] overflow-y-auto pr-1 custom-scrollbar">
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
              Recent
            </span>
            {selectedRecentIds && selectedRecentIds.size > 0 && (
              <button
                type="button"
                onClick={onBulkDeleteRecent}
                className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
              >
                <Trash2 size={12} /> Delete {selectedRecentIds.size}
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
                  className="accent-[#53fc18]"
                />
                Select all
              </label>
            </div>
          )}
          <div className="flex flex-col gap-2 max-h-[200px] overflow-y-auto pr-1 custom-scrollbar">
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
            History
          </span>
          {selectedHistoryIds && selectedHistoryIds.size > 0 && (
            <button
              type="button"
              onClick={onBulkDeleteHistory}
              className="text-[10px] text-red-400 hover:text-red-300 flex items-center gap-1 font-bold uppercase tracking-wider"
            >
              <Trash2 size={12} /> Delete {selectedHistoryIds.size}
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
                className="accent-[#53fc18]"
              />
              Select all
            </label>
          </div>
        )}
        <div className="flex flex-col gap-2 max-h-[160px] overflow-y-auto pr-1 custom-scrollbar">
          {historyDownloads.length === 0 ? (
            <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
              NO COMPLETED DOWNLOADS YET.
            </div>
          ) : historyDownloads.map((dl) => {
            const checked = selectedHistoryIds?.has(dl.download_id);
            return (
              <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-950 p-2 flex flex-col gap-1.5">
                <div className="flex justify-between items-center gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    {onToggleHistorySelection && (
                      <input
                        type="checkbox"
                        checked={!!checked}
                        onChange={() => onToggleHistorySelection(dl.download_id)}
                        onClick={(e) => e.stopPropagation()}
                        className="accent-[#53fc18] shrink-0"
                      />
                    )}
                    <PlatformVodIcon platform={dl.platform} className="w-4 h-4" />
                    <span className="text-xs font-mono text-zinc-300 truncate">
                      {dl.title || dl.url}
                    </span>
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
                        title="Show in folder"
                      >
                        <FolderOpen size={12} /> Folder
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onDeleteHistory(dl.download_id)}
                      className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                      title="Remove from history"
                    >
                      <Trash2 size={12} /> Delete
                    </button>
                  </div>
                </div>
                {dl.error && <span className="text-[10px] text-red-400 font-mono">{dl.error}</span>}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
