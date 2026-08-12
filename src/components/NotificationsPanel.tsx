import { useEffect, useMemo, useState } from 'react';
import { Bell, CheckCircle2, CircleAlert, Loader2 } from 'lucide-react';
import PlatformVodIcon from './PlatformVodIcon';
import { isRetryJob, type ArchiveJobRow } from './QueueTab';
import { useI18n } from '../i18n';

const JOBS_POLL_MS = 3000;
const JOBS_MAX_ROWS = 80;

const PLATFORM_ICON_NAME: Record<string, string> = {
  youtube: 'YouTube',
  twitch: 'Twitch',
  kick: 'Kick',
};

type StatusFilter = 'all' | 'active' | 'done' | 'failed' | 'retrying';
type KindFilter = 'all' | 'transcribe' | 'chat' | 'events' | 'ingest';

async function fetchJobs(): Promise<ArchiveJobRow[]> {
  const res = await fetch('/api/archive/jobs?limit=200', { signal: AbortSignal.timeout(8000) });
  if (!res.ok) return [];
  const data = (await res.json().catch(() => null)) as { jobs?: ArchiveJobRow[] } | null;
  return Array.isArray(data?.jobs) ? data.jobs : [];
}

export default function NotificationsPanel() {
  const { t } = useI18n();
  const [jobs, setJobs] = useState<ArchiveJobRow[]>([]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [kindFilter, setKindFilter] = useState<KindFilter>('all');

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const rows = await fetchJobs();
        if (!cancelled) setJobs(rows);
      } catch {
        /* next tick */
      } finally {
        if (!cancelled) timer = window.setTimeout(poll, JOBS_POLL_MS);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  const kindLabel = (kind: string) => {
    switch (kind) {
      case 'transcribe': return t('progress.kind.transcribe');
      case 'chat': return t('progress.kind.chat');
      case 'events': return t('progress.kind.events');
      case 'ingest': return t('progress.kind.ingest');
      default: return kind;
    }
  };
  const statusLabel = (j: ArchiveJobRow) => {
    // Retry (queued with attempts > 0) takes precedence over the queued case.
    if (isRetryJob(j)) return t('progress.status.retrying');
    switch (j.status) {
      case 'queued': return t('progress.status.queued');
      case 'running': return t('progress.status.running');
      case 'done': return t('progress.status.done');
      case 'failed': return t('progress.status.failed');
      default: return j.status;
    }
  };

  const filtered = useMemo(() => {
    return jobs.filter((j) => {
      if (kindFilter !== 'all' && j.kind !== kindFilter) return false;
      if (statusFilter === 'active') return j.status === 'queued' || j.status === 'running';
      if (statusFilter === 'done') return j.status === 'done';
      // 'failed' is FINAL failure only — retries (queued, attempts>0) are not.
      if (statusFilter === 'failed') return j.status === 'failed';
      if (statusFilter === 'retrying') return isRetryJob(j);
      return true;
    }).slice(0, JOBS_MAX_ROWS);
  }, [jobs, kindFilter, statusFilter]);

  const activeCount = jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
  const doneCount = jobs.filter((j) => j.status === 'done').length;
  const finishedCount = jobs.filter((j) => j.status === 'done' || j.status === 'failed').length;

  const chip = (id: string, label: string, active: boolean, onClick: () => void) => (
    <button
      key={id}
      type="button"
      onClick={onClick}
      className={`px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest border-2 transition-colors ${
        active ? 'border-white bg-white text-black' : 'border-zinc-800 text-zinc-500 hover:text-white hover:border-zinc-500'
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Bell size={14} className="text-zinc-400 shrink-0" />
          <span className="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
            {t('Notifications')}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[9px] font-mono text-zinc-500 shrink-0">
          <span className="text-[#53fc18]">{activeCount} {t('in progress')}</span>
          <span>·</span>
          <span>{doneCount} {t('completed')}</span>
          <button
            type="button"
            disabled={finishedCount === 0}
            onClick={() => {
              void fetch('/api/archive/jobs/clear', { method: 'POST' })
                .then(() => fetchJobs())
                .then(setJobs)
                .catch(() => {});
            }}
            className="ml-2 px-2 py-0.5 border-2 border-zinc-700 text-zinc-400 hover:text-white hover:border-white uppercase tracking-widest font-bold disabled:opacity-40 disabled:hover:text-zinc-400 disabled:hover:border-zinc-700 disabled:cursor-default"
            title={t('Clear notifications')}
          >
            {t('Clear notifications')}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {chip('s-all', t('All'), statusFilter === 'all', () => setStatusFilter('all'))}
        {chip('s-active', t('In progress'), statusFilter === 'active', () => setStatusFilter('active'))}
        {chip('s-done', t('Completed'), statusFilter === 'done', () => setStatusFilter('done'))}
        {chip('s-fail', t('Failed'), statusFilter === 'failed', () => setStatusFilter('failed'))}
        {chip('s-retry', t('progress.status.retrying'), statusFilter === 'retrying', () => setStatusFilter('retrying'))}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {chip('k-all', t('Any work'), kindFilter === 'all', () => setKindFilter('all'))}
        {chip('k-tr', t('progress.kind.transcribe'), kindFilter === 'transcribe', () => setKindFilter('transcribe'))}
        {chip('k-ch', t('progress.kind.chat'), kindFilter === 'chat', () => setKindFilter('chat'))}
        {chip('k-ev', t('progress.kind.events'), kindFilter === 'events', () => setKindFilter('events'))}
        {chip('k-in', t('progress.kind.ingest'), kindFilter === 'ingest', () => setKindFilter('ingest'))}
      </div>

      {filtered.length === 0 ? (
        <div className="text-center text-zinc-600 font-mono text-xs py-8 border-2 border-dashed border-zinc-800">
          {t('No background jobs match these filters')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {filtered.map((j) => {
            const pct = Math.min(100, Math.max(0, Math.round((j.progress || 0) * 100)));
            const running = j.status === 'running';
            const retrying = isRetryJob(j);
            return (
              <div key={j.id} className="border-2 border-zinc-800 bg-zinc-950/80 p-2.5 flex flex-col gap-1.5">
                <div className="flex justify-between items-center gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <PlatformVodIcon platform={PLATFORM_ICON_NAME[j.platform] ?? j.platform} className="w-3.5 h-3.5 shrink-0" />
                    <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider shrink-0">
                      {kindLabel(j.kind)}
                    </span>
                    <span className="text-[10px] font-mono text-zinc-300 truncate" title={j.title || j.video_id}>
                      {j.title || j.video_id}
                    </span>
                  </div>
                  <span className={`text-[10px] font-mono shrink-0 flex items-center gap-1 ${
                    j.status === 'running' ? 'text-[#53fc18]' :
                    j.status === 'failed' ? 'text-red-400' :
                    retrying ? 'text-amber-400' :
                    j.status === 'done' ? 'text-zinc-400' : 'text-zinc-500'
                  }`}>
                    {j.status === 'running' ? <Loader2 size={11} className="animate-spin" /> : null}
                    {j.status === 'done' ? <CheckCircle2 size={11} /> : null}
                    {j.status === 'failed' ? <CircleAlert size={11} /> : null}
                    {statusLabel(j)}
                  </span>
                </div>
                {running && (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 rounded-sm bg-zinc-800 overflow-hidden" role="progressbar" aria-valuenow={pct}>
                      <div className="h-full bg-[#53fc18] transition-[width] duration-300" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-[9px] font-mono text-zinc-500 tabular-nums shrink-0">{pct}%</span>
                  </div>
                )}
                {j.status === 'failed' && j.error && (
                  <span className="text-[10px] text-red-400 font-mono truncate" title={j.error}>{j.error}</span>
                )}
                {retrying && j.error && (
                  <span className="text-[10px] text-amber-300/70 font-mono truncate" title={j.error}>{j.error}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
