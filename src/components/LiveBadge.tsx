import { Eye } from 'lucide-react';

export interface LiveEntry {
  platform: string;
  is_live: boolean;
  title: string;
  url: string;
  headers: Record<string, string>;
  type: string;
}

export function LiveBadge({ entries }: { entries: LiveEntry[] }) {
  if (entries.length === 0) return null;
  return (
    <span
      title={entries.map((e) => `${e.platform}: ${e.title}`).join('\n')}
      className="flex items-center gap-1 rounded bg-red-900/50 px-1 text-[10px] text-red-200 font-bold shrink-0"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
      LIVE
    </span>
  );
}

export function LiveWatchButton({
  entries,
  onWatch,
  onShowPicker,
}: {
  entries: LiveEntry[];
  onWatch: (entry: LiveEntry) => void;
  onShowPicker?: () => void;
}) {
  if (entries.length === 0) return null;
  const single = entries.length === 1;
  return (
    <button
      type="button"
      title={
        single
          ? `Watch live: ${entries[0].platform}`
          : "Watch live (pick platform)"
      }
      onClick={(e) => {
        e.stopPropagation();
        if (single) onWatch(entries[0]);
        else onShowPicker?.();
      }}
      className="text-red-400 hover:text-red-200 p-0.5"
    >
      <Eye size={11} />
    </button>
  );
}
