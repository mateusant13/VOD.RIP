import { useCallback, useEffect, useState } from 'react';
import { Loader2, Trash2 } from 'lucide-react';
import FieldCaption from './FieldCaption';
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
  cleaning,
  onClean,
}: {
  category: string;
  bytes: number;
  cleaning: boolean;
  onClean: (category: string) => void;
}) {
  const cleanable = (CLEANABLE as readonly string[]).includes(category);
  return (
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
  );
}

export default function DiskSection({ settings, setSettings }: Props) {
  const [usage, setUsage] = useState<DiskUsage | null>(null);
  const [status, setStatus] = useState<DiskStatus | null>(null);
  const [cleaning, setCleaning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFreed, setLastFreed] = useState<string | null>(null);
  const [whisperSaving, setWhisperSaving] = useState(false);
  const [whisperMsg, setWhisperMsg] = useState<string | null>(null);

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

  const activeWhisperModel = (settings.whisper_model ?? '').trim() || 'large-v3-turbo';

  const onSaveWhisper = useCallback(async () => {
    setWhisperSaving(true);
    setWhisperMsg(null);
    try {
      const updated = await apiPost<AppSettings>('/api/settings', {
        whisper_model: (settings.whisper_model ?? '').trim() || undefined,
        whisper_model_cache: (settings.whisper_model_cache ?? '').trim() || null,
      });
      setSettings(updated);
      setWhisperMsg('saved');
    } catch (err) {
      setWhisperMsg(err instanceof Error ? err.message : 'save failed');
    } finally {
      setWhisperSaving(false);
    }
  }, [settings.whisper_model, settings.whisper_model_cache, setSettings]);

  return (
    <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-3">
      <FieldCaption noWrap>Disk</FieldCaption>

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

      <div className="flex items-center gap-1.5 pt-1">
        <FieldCaption noWrap>Archive VODs Keep</FieldCaption>
        <input
          type="number"
          min={1}
          max={50}
          value={keepValue}
          onChange={(e) =>
            setSettings({
              ...settings,
              archive_vod_keep_count: Math.max(1, Math.min(50, parseInt(e.target.value) || 5)),
            })
          }
          aria-label="archive vods keep count"
          className="w-16 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1 px-2 focus:outline-none focus:border-white text-xs"
        />
        <span className="text-[9px] text-zinc-600 font-mono">oldest VODs kept per platform — applies on Save Settings</span>
      </div>

      <div className="flex flex-col gap-1.5 pt-1">
        <FieldCaption noWrap>Transcription Model</FieldCaption>
        <input
          type="text"
          value={settings.whisper_model ?? ''}
          onChange={(e) => setSettings({ ...settings, whisper_model: e.target.value })}
          placeholder="large-v3-turbo"
          aria-label="whisper model id"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1 px-2 focus:outline-none focus:border-white text-xs"
        />
        <input
          type="text"
          value={settings.whisper_model_cache ?? ''}
          onChange={(e) => setSettings({ ...settings, whisper_model_cache: e.target.value })}
          placeholder="%APPDATA%/VOD.RIP/whisper-models"
          aria-label="whisper model cache directory"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1 px-2 focus:outline-none focus:border-white text-xs"
        />
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={onSaveWhisper}
            disabled={whisperSaving}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50 flex items-center gap-1"
          >
            {whisperSaving ? <Loader2 size={10} className="animate-spin" /> : null}
            {whisperSaving ? '...' : 'Save'}
          </button>
          <span className="text-[9px] text-zinc-600 font-mono">active: {activeWhisperModel}</span>
          {whisperMsg ? <span className="text-[9px] text-emerald-700 font-mono">{whisperMsg}</span> : null}
        </div>
        <span className="text-[9px] text-zinc-600 font-mono">
          cache may point at a shared HF hub dir — already-downloaded models are reused without re-download
        </span>
      </div>

      {error ? <div className="text-[9px] text-red-500 font-mono">{error}</div> : null}
    </div>
  );
}
