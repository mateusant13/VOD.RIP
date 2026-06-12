import { memo, type ReactNode } from 'react';
import { FolderOpen, Pause, Play, StopCircle } from 'lucide-react';

export type ActiveDownloadRow = {
  download_id: string;
  url: string;
  platform: string;
  status: string;
  progress: number;
  output_file: string;
  title?: string | null;
};

type Props = {
  downloads: ActiveDownloadRow[];
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onCancel: (id: string) => void;
  onOpenFolder: (path: string) => void;
  basename: (path: string) => string;
  platformIcon: (platform: string, className: string) => ReactNode;
};

function ActiveDownloadsListInner({
  downloads,
  onPause,
  onResume,
  onCancel,
  onOpenFolder,
  basename,
  platformIcon,
}: Props) {
  if (downloads.length === 0) {
    return (
      <div className="text-center text-zinc-600 font-mono text-xs py-6 border-2 border-dashed border-zinc-800">
        NO ACTIVE DOWNLOADS.
      </div>
    );
  }

  return (
    <>
      {downloads.map((dl) => {
        const isTw = dl.platform === 'Twitch';
        const isPaused = dl.status === 'Paused';
        const color = isPaused ? '#fbbf24' : (isTw ? '#9146FF' : '#53fc18');
        return (
          <div key={dl.download_id} className="border-2 border-zinc-800 bg-zinc-900/40 p-3 flex flex-col gap-2">
            <div className="flex justify-between items-center gap-2">
              <div className="flex items-center gap-2 min-w-0">
                {platformIcon(dl.platform, 'w-4 h-4')}
                <span className="text-xs font-mono text-zinc-300 truncate">
                  {dl.title || dl.url}
                </span>
              </div>
              <span className="text-[10px] font-mono shrink-0" style={{ color }}>
                {isPaused ? 'Paused' : (dl.progress > 0 ? `${dl.progress}%` : dl.status)}
              </span>
            </div>
            <div className="w-full h-2 bg-zinc-800 border border-zinc-700">
              <div
                className={`h-full transition-all duration-300 ${
                  isPaused ? 'bg-yellow-500/70' : 'bg-gradient-to-r from-[#53fc18] to-[#9146FF]'
                }`}
                style={{ width: `${Math.max(dl.progress, dl.status === 'Starting...' ? 2 : 0)}%` }}
              />
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
                {isPaused ? (
                  <button
                    type="button"
                    onClick={() => onResume(dl.download_id)}
                    className="text-zinc-400 hover:text-white flex items-center gap-1"
                  >
                    <Play size={12} /> Resume
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => onPause(dl.download_id)}
                    className="text-zinc-400 hover:text-yellow-300 flex items-center gap-1"
                  >
                    <Pause size={12} /> Pause
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onCancel(dl.download_id)}
                  className="text-zinc-500 hover:text-red-400 flex items-center gap-1"
                >
                  <StopCircle size={12} /> Cancel
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </>
  );
}

export const ActiveDownloadsList = memo(ActiveDownloadsListInner);
