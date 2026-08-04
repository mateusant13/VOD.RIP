import { useCallback, useEffect, useState } from 'react';
import { Loader2, Trash2 } from 'lucide-react';
import FieldCaption from './FieldCaption';
import NumberField from './NumberField';
import { apiGet, apiPost } from '../hooks/useApiClient';
import { formatBytes } from '../formatters';
import type { AppSettings, DiskStatus, DiskUsage } from '../types';

type Props = {
  settings: AppSettings;
  setSettings: React.Dispatch<React.SetStateAction<AppSettings>>;
};

const CLEANABLE = ['archive_vods', 'whisper_models', 'preview_cache', 'update_temps'] as const;

const ROW_LABELS: Record<string, string> = {
  archive_vods: 'Archive VODs',
  whisper_models: 'Whisper Models',
  db: 'Database',
  logs: 'Logs',
  preview_cache: 'Preview Cache',
  update_temps: 'Update Temps',
};

function UsageRow({
  category,
  bytes,
  total,
  cleaning,
  onClean,
}: {
  category: string;
  bytes: number;
  total: number;
  cleaning: boolean;
  onClean: (category: string) => void;
}) {
  const cleanable = (CLEANABLE as readonly string[]).includes(category);
  const pct = total > 0 ? Math.max(0, Math.min(100, (bytes / total) * 100)) : 0;
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-2">
        <span className="flex-1 text-[10px] text-zinc-400 font-mono uppercase tracking-wide truncate">
          {ROW_LABELS[category] ?? category}
        </span>
        <span className="text-[10px] text-zinc-200 font-mono tabular-nums w-20 text-right">
          {formatBytes(bytes)}
        </span>
        {cleanable ? (
          <button
            type="button"
            aria-label={`clean ${category}`}
            onClick={() => onClean(category)}
            disabled={cleaning}
            className="bg-zinc-900 text-zinc-400 font-black uppercase px-2 py-1 text-[9px] border-2 border-zinc-700 hover:border-red-500 hover:text-red-400 shrink-0 flex items-center gap-1 disabled:opacity-40"
          >
            {cleaning ? <Loader2 size={10} className="animate-spin" /> : <Trash2 size={10} />}
            CLEAN
          </button>
        ) : (
          <span className="w-16 shrink-0" aria-hidden />
        )}
      </div>
      <div className="h-1 bg-zinc-800/80 overflow-hidden">
        <div className="h-full bg-zinc-500/70" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export default function DiskSection({ settings, setSettings }: Props) {
  const [usage, setUsage] = useState<DiskUsage | null>(null);
  const [status, setStatus] = useState<DiskStatus | null>(null);
  const [cleaning, setCleaning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFreed, setLastFreed] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [u, s] = await Promise.all([
        apiGet<DiskUsage>('/api/disk/usage'),
        apiGet<DiskStatus>('/api/disk/status'),
      ]);
      setUsage(u);
      setStatus(s);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Disk info failed');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onClean = useCallback(async (category: string) => {
    setCleaning(category);
    setLastFreed(null);
    try {
      const res = await apiPost<{ freed_bytes: number }>('/api/disk/cleanup', { category });
      setLastFreed(`${ROW_LABELS[category] ?? category}: freed ${formatBytes(res.freed_bytes)}`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cleanup failed');
    } finally {
      setCleaning(null);
    }
  }, [refresh]);

  const keepCount = settings.archive_vod_keep_count ?? 5;
  const keepValue = Number.isFinite(keepCount) ? Math.max(1, Math.min(50, keepCount)) : 5;

  return (
    <div className="flex flex-col gap-2">
      {status?.low ? (
        <div role="alert" className="bg-red-950 text-red-400 border-2 border-red-900 px-2 py-1.5 text-[10px] font-mono">
          LOW DISK SPACE — {formatBytes(status.free_bytes)} free. Run cleanups below or free space manually.
        </div>
      ) : null}

      <div className="flex flex-col gap-1">
        {(usage
          ? Object.entries(usage).filter(([cat]) => cat !== 'total')
          : Object.keys(ROW_LABELS).map((cat) => [cat, 0] as [string, number])
        ).map(([cat, bytes]) => (
          <UsageRow
            key={cat}
            category={cat}
            bytes={bytes as number}
            total={usage?.total ?? 0}
            cleaning={cleaning === cat}
            onClean={onClean}
          />
        ))}
      </div>

      {usage ? (
        <div className="flex items-center gap-2">
          <span className="flex-1 text-[10px] text-zinc-500 font-mono uppercase tracking-wide">Total</span>
          <span className="text-[10px] text-zinc-100 font-mono tabular-nums w-20 text-right">{formatBytes(usage.total)}</span>
          <span className="w-16 shrink-0" aria-hidden />
        </div>
      ) : null}

      <div className="flex items-center gap-1.5 pt-0.5">
        <span className="text-[9px] text-zinc-600 font-mono">FREE</span>
        <span className="text-[9px] text-zinc-400 font-mono tabular-nums">
          {status ? formatBytes(status.free_bytes) : '…'}
        </span>
        {lastFreed ? <span className="text-[9px] text-emerald-700 font-mono">{lastFreed}</span> : null}
      </div>

      <div className="flex flex-col gap-1 pt-1">
        <FieldCaption noWrap>Archive VODs Keep</FieldCaption>
        <div className="flex items-center gap-1.5">
          <div className="w-24 shrink-0">
            <NumberField
              ariaLabel="archive vods keep count"
              value={keepValue}
              min={1}
              max={50}
              step={1}
              onChange={(v) => setSettings({ ...settings, archive_vod_keep_count: v })}
            />
          </div>
          <span className="text-[9px] text-zinc-600 font-mono flex-1">oldest VODs kept per platform — applies on Save Settings</span>
        </div>
      </div>

      {error ? <div className="text-[9px] text-red-500 font-mono">{error}</div> : null}
    </div>
  );
}
