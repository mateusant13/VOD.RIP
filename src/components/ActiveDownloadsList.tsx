import { memo, type ReactNode } from 'react';
import { FolderOpen, Pause, Play, StopCircle, Trash2 } from 'lucide-react';
import { platformAccentColor } from '../platformColors';

export type ActiveDownloadRow = {
  download_id: string;
  url: string;
  platform: string;
  status: string;
  progress: number;
  output_file: string;
  title?: string | null;
  error?: string | null;
};

const RESUMABLE_STATUSES = new Set(['Paused', 'Failed', 'Cancelled', 'Interrupted']);

type Props = {
  downloads: ActiveDownloadRow[];
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onCancel: (id: string) => void;
  onDelete: (id: string) => void;
  onOpenFolder: (path: string) => void;
  basename: (path: string) => string;
  platformIcon: (platform: string, className: string) => ReactNode;
  showCheckbox?: boolean;
  selectedIds?: Set<string>;
  onToggleSelect?: (id: string) => void;
};

function ActiveDownloadsListInner({
  downloads,
  onPause,
  onResume,
  onCancel,
  onDelete,
  onOpenFolder,
  basename,
  platformIcon,
  showCheckbox,
  selectedIds,
  onToggleSelect,
}: Props) {
  if (downloads.length === 0) {
    return (
      <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
        NO DOWNLOADS IN QUEUE.
      </div>
    );
  }

  return (
    <>
      {downloads.map((dl) => {
        const isResumable = RESUMABLE_STATUSES.has(dl.status);
        const isPaused = dl.status === 'Paused';
        const color = isResumable
          ? (dl.status === 'Failed' ? '#f87171' : '#fbbf24')
          : platformAccentColor(dl.platform);
        const barClass = isResumable || isPaused
          ? 'bg-yellow-500/70'
          : 'bg-gradient-to-r from-[#53fc18] via-[#9146FF] to-[#E03E3E]';
        const dlStatus = dl.status ?? '';
        const firstToken = (dlStatus.split(/\s+/, 1)[0] || '').toLowerCase();
        const phaseId =
          firstToken.startsWith('finalis') ? 'finalising'
          : firstToken.startsWith('remux') ? 'remuxing'
          : firstToken.startsWith('mux') ? 'muxing'
          : firstToken.startsWith('merg') ? 'merging'
          : firstToken.startsWith('encod') ? 'encoding'
          : '';
        const isPostProcess = !isResumable && phaseId !== '';
        const isFinalising = phaseId === 'finalising';
        const badgeText = isResumable
          ? (dl.progress > 0 ? `${dl.status} · ${dl.progress}%` : dl.status)
          : isPaused
            ? 'Paused'
            : (dl.progress > 0
                ? (isPostProcess ? dlStatus : `${dl.progress}%`)
                : dlStatus);
        const checked = showCheckbox && selectedIds?.has(dl.download_id);
        return (
          <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-900/40 p-3 flex flex-col gap-2">
            <div className="flex justify-between items-center gap-2">
              <div className="flex items-center gap-2 min-w-0">
                {showCheckbox ? (
                  <label
                    className="flex items-center gap-2 shrink-0 cursor-pointer"
                    onClick={(e) => {
                      e.preventDefault();
                      onToggleSelect?.(dl.download_id);
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={!!checked}
                      readOnly
                      tabIndex={-1}
                      className="accent-[#53fc18] shrink-0 pointer-events-none"
                    />
                    {platformIcon(dl.platform, 'w-4 h-4')}
                  </label>
                ) : (
                  platformIcon(dl.platform, 'w-4 h-4')
                )}
                <span className="text-xs font-mono text-zinc-300 truncate">
                  {dl.title || dl.url}
                </span>
              </div>
              <span
                className={`text-[10px] font-mono shrink-0 ${isPostProcess ? 'animate-pulse' : ''}`}
                style={{ color }}
              >
                {badgeText}
              </span>
            </div>
            <div className="w-full h-2 bg-zinc-800 border border-zinc-700 relative overflow-hidden">
              <div
                className={`h-full transition-all duration-300 ${barClass}`}
                style={{ width: `${Math.max(dl.progress, dl.status === 'Starting...' ? 2 : 0)}%` }}
              />
              {isFinalising && (
                <div
                  className="absolute inset-y-0 left-0 w-1/3 bg-gradient-to-r from-transparent via-white/20 to-transparent"
                  style={{ animation: 'shimmer 1.2s linear infinite' }}
                />
              )}
            </div>
            <div className="flex justify-between items-center text-[10px] text-zinc-500 font-mono gap-2">
              <span className="truncate">{basename(dl.output_file)}</span>
              <div className="flex items-center gap-2 shrink-0">
                {dl.output_file && !isResumable && (
                  <button
                    type="button"
                    onClick={() => onOpenFolder(dl.output_file)}
                    className="text-zinc-400 hover:text-white flex items-center gap-1"
                    title="Show in folder"
                  >
                    <FolderOpen size={12} /> Folder
                  </button>
                )}
                {isResumable ? (
                  <>
                    <button
                      type="button"
                      onClick={() => onResume(dl.download_id)}
                      className="text-zinc-400 hover:text-[#53fc18] flex items-center gap-1"
                      title="Resume download"
                    >
                      <Play size={12} /> Resume
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete(dl.download_id)}
                      className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                      title="Remove from queue"
                    >
                      <Trash2 size={12} /> Delete
                    </button>
                  </>
                ) : isPaused ? (
                  <>
                    <button
                      type="button"
                      onClick={() => onResume(dl.download_id)}
                      className="text-zinc-400 hover:text-white flex items-center gap-1"
                    >
                      <Play size={12} /> Resume
                    </button>
                    <button
                      type="button"
                      onClick={() => onCancel(dl.download_id)}
                      className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                    >
                      <StopCircle size={12} /> Cancel
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => onPause(dl.download_id)}
                      className="text-zinc-400 hover:text-yellow-300 flex items-center gap-1"
                    >
                      <Pause size={12} /> Pause
                    </button>
                    <button
                      type="button"
                      onClick={() => onCancel(dl.download_id)}
                      className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                    >
                      <StopCircle size={12} /> Cancel
                    </button>
                  </>
                )}
              </div>
            </div>
            {dl.error && (
              <span className="text-[10px] text-red-400 font-mono">{dl.error}</span>
            )}
          </div>
        );
      })}
    </>
  );
}

export const ActiveDownloadsList = memo(ActiveDownloadsListInner);
