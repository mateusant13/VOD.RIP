
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
}: Props) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
          Queue
        </span>
        <button onClick={onRefresh} className="text-zinc-500 hover:text-white transition-colors">
          <RefreshCw size={14} />
        </button>
      </div>

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
        />
      </div>

      {recentDownloads.length > 0 && (
        <div className="border-t-2 border-zinc-800 pt-3 flex flex-col gap-2">
          <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
            Recent
          </span>
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
            />
          </div>
        </div>
      )}

      <div className="border-t-2 border-zinc-800 pt-3 flex flex-col gap-2">
        <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
          History
        </span>
        <div className="flex flex-col gap-2 max-h-[160px] overflow-y-auto pr-1 custom-scrollbar">
          {historyDownloads.length === 0 ? (
            <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
              NO COMPLETED DOWNLOADS YET.
            </div>
          ) : historyDownloads.map((dl) => (
              <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-950 p-2 flex flex-col gap-1.5">
                <div className="flex justify-between items-center gap-2">
                  <div className="flex items-center gap-2 min-w-0">
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
          ))}
        </div>
      </div>
    </div>
  );
}
