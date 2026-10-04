/**
 * Platform Requests — the rate governor, made visible.
 *
 * `backend/services/rate_budget.py` has been silently pacing every YouTube,
 * Twitch and Kick request since the governor landed: a learned per-platform
 * ceiling, split into an AUTO pool (background work) and a USER pool (your own
 * clicks) so a background storm can never spend the reserve you need. It is
 * the one feature whose whole job is to be invisible — until something looks
 * wrong, and there was nowhere to look.
 *
 * Three rules this panel follows, and why:
 *
 * 1. **No polling.** The governor regulates request cost; a panel that polls
 *    it would spend the thing it reports. One GET when the card is opened
 *    (this component only mounts inside an expanded SettingsCard), plus one
 *    per explicit Refresh click.
 * 2. **Nothing is recomputed here.** Every number is rendered straight from
 *    GET /api/archive/rate-budget. Deriving a rate in the UI would be a second
 *    source of truth that can disagree with the one making the decisions.
 * 3. **NULL is not 0.** `learning.seconds_since_event` is null when no event
 *    was ever recorded — that means "not measured", and it is rendered as
 *    such. A fabricated 0 reads as a clean window and poisons the statistics
 *    (the rule the whole backend follows; see rl_counter / yt_gate / kick_gate).
 *    Same for an unreachable endpoint: no values at all, rather than zeros.
 */
import { useCallback, useEffect, useState } from 'react';
import { Activity, Loader2, RefreshCw } from 'lucide-react';
import FieldCaption from './FieldCaption';
import { apiGet } from '../hooks/useApiClient';
import { useI18n } from '../i18n';
import type { RateBudgetPlatform, RateBudgetStatus } from '../types';

/** One decimal, no trailing ".0" — these numbers are read at a glance, not
 *  audited. '—' is reserved for "no value", never for zero. */
function rpm(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  return String(Math.round(n * 10) / 10);
}

const PLATFORM_NAMES: Record<string, string> = {
  youtube: 'YouTube',
  twitch: 'Twitch',
  kick: 'Kick',
};

/** "4s" / "12min" — coarse on purpose; this is an age, not a stopwatch. */
function ago(seconds: number): string {
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))}s`;
  return `${rpm(seconds / 60)}min`;
}

function PlatformRow({ p, backoffS }: { p: RateBudgetPlatform; backoffS: number | null }) {
  const { t } = useI18n();
  const name = PLATFORM_NAMES[p.platform] ?? p.platform;
  const learned = p.ceiling_rpm < p.default_ceiling_rpm;
  const autoPct = Math.round(p.auto_share * 100);
  const l = p.learning;

  return (
    <div className="flex flex-col gap-2 border-2 border-zinc-800 bg-zinc-950/40 p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 min-w-0 text-[11px] font-bold uppercase tracking-wider text-zinc-300">
          <Activity size={12} className="shrink-0" />
          <span className="truncate">{name}</span>
        </span>
        <span className="text-[11px] font-mono text-zinc-100 tabular-nums shrink-0">
          {t('{rpm} req/min', { rpm: rpm(p.ceiling_rpm) })}
        </span>
      </div>

      <p className="text-[11px] font-mono text-zinc-400">
        {t('Allowed right now: {rpm} requests per minute.', { rpm: rpm(p.ceiling_rpm) })}
      </p>
      {learned ? (
        <p className="text-[11px] font-mono text-amber-400">
          {t('Learned down from the {rpm} req/min default.', { rpm: rpm(p.default_ceiling_rpm) })}
        </p>
      ) : null}

      {/* AUTO — the pool that paces background work. */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <FieldCaption>{t('Background work (AUTO)')}</FieldCaption>
          <span className="text-[11px] font-mono text-zinc-300 tabular-nums">
            {t('{left} of {cap} left', { left: rpm(p.auto.tokens), cap: rpm(p.auto.capacity) })}
          </span>
        </div>
        <p className="text-[11px] font-mono text-zinc-500">
          {t('May use {pct}% of the ceiling — never more, so a background storm cannot spend your share.', { pct: autoPct })}
        </p>
        {p.auto.exhausted ? (
          <p className="text-[11px] font-mono text-amber-400">
            {backoffS != null && backoffS > 0
              ? t('Spent for now — background work retries in about {sec}s.', { sec: rpm(backoffS) })
              : t('Spent for now — background work is being held back.')}
          </p>
        ) : null}
      </div>

      {/* USER — the reserve. The whole point of the split. */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <FieldCaption>{t('Your actions (USER)')}</FieldCaption>
          <span className="text-[11px] font-mono text-emerald-400 tabular-nums">
            {t('{left} of {cap} left', { left: rpm(p.user.tokens), cap: rpm(p.user.capacity) })}
          </span>
        </div>
        <p className="text-[11px] font-mono text-zinc-500">
          {t('Gets all {rpm} req/min. Background work can never draw from this reserve.', { rpm: rpm(p.user.refill_rpm) })}
        </p>
      </div>

      {/* Learning — what the governor has actually observed. */}
      <div className="flex flex-col gap-1">
        <FieldCaption>{t('Learning')}</FieldCaption>
        {l.events > 0 ? (
          <>
            <p className="text-[11px] font-mono text-zinc-400">
              {l.seconds_since_event != null
                ? t('{count} limit events recorded · last one {ago} ago.', {
                    count: l.events,
                    ago: ago(l.seconds_since_event),
                  })
                /* events > 0 with a null age is not expected from the
                   endpoint, but if it ever happens the age is omitted
                   rather than rendered as "0s ago" — an invented age is a
                   fabricated clean window. */
                : t('{count} limit events recorded.', { count: l.events })}
            </p>
            {l.min_trip_rpm > 0 ? (
              <p className="text-[11px] font-mono text-zinc-500">
                {t('Lowest rate that ever tripped it: {rpm} req/min.', { rpm: rpm(l.min_trip_rpm) })}
              </p>
            ) : null}
          </>
        ) : (
          /* No event in this process. The ceiling may STILL have been lowered
             from saved history (prime_from_history) — that is a different
             fact from "no events", and conflating them would hide it. */
          <p className="text-[11px] font-mono text-zinc-500">
            {t('No rate-limit events recorded in this session.')}
          </p>
        )}
        {l.events === 0 && l.min_trip_rpm > 0 ? (
          <p className="text-[11px] font-mono text-amber-400">
            {t('Ceiling learned earlier from saved history (lowest recorded trip {rpm} req/min).', { rpm: rpm(l.min_trip_rpm) })}
          </p>
        ) : null}
        {l.events > 0 && l.ramp_pending ? (
          <p className="text-[11px] font-mono text-zinc-500">
            {t('A clean window has passed — the ceiling will step back up slightly on the next request.')}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export default function RateGovernorSection() {
  const [data, setData] = useState<RateBudgetStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { t } = useI18n();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setData(await apiGet<RateBudgetStatus>('/api/archive/rate-budget'));
      setError(null);
    } catch (err) {
      /* Drop the last-known values: a stale ceiling shown next to a live
         "could not reach" is worse than showing nothing at all. */
      setData(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  // Mount-only. No interval, no visibility poller — see the header note.
  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="flex flex-col gap-3">
      <p className="text-[11px] font-mono text-zinc-500">
        {t('How fast VOD.RIP may talk to YouTube, Twitch and Kick. The ceiling is learned from real rate-limit trips: it drops fast when a platform pushes back and creeps back up after clean minutes.')}
      </p>

      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          aria-label="refresh rate governor"
          onClick={() => void refresh()}
          disabled={loading}
          className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-1.5 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white flex items-center gap-1.5 disabled:opacity-50"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          {t('Refresh')}
        </button>
        {data ? (
          <span className="text-[11px] font-mono text-zinc-500">
            {t('Background work waits at most {sec}s for a free slot, then goes ahead anyway — it is never blocked.', {
              sec: rpm(data.max_auto_wait_s),
            })}
          </span>
        ) : null}
      </div>

      {error ? (
        <div role="alert" className="bg-red-950 text-red-400 border-2 border-red-900 px-3 py-2 text-[11px] font-mono">
          {t('Could not reach the rate governor ({msg}). No values are shown — an unreachable governor is not a governor reporting zero.', { msg: error })}
        </div>
      ) : null}

      {!error && !data ? (
        <div className="flex items-center gap-2 text-[11px] font-mono text-zinc-500">
          <Loader2 size={12} className="animate-spin" />
          {t('Reading the governor…')}
        </div>
      ) : null}

      {data ? (
        <>
          {data.platforms.map((p) => (
            <PlatformRow
              key={p.platform}
              p={p}
              backoffS={data.scheduler?.[p.platform]?.backoff_s ?? null}
            />
          ))}

          <div className="flex flex-col gap-1">
            <FieldCaption>{t('Recent throttles')}</FieldCaption>
            {data.recent_decisions && data.recent_decisions.length > 0 ? (
              <p className="text-[11px] font-mono text-zinc-400">
                {t('{count} background requests were held back. This list lives in memory only and clears when the app restarts.', {
                  count: data.recent_decisions.length,
                })}
              </p>
            ) : (
              <p className="text-[11px] font-mono text-zinc-500">{t('None held back right now.')}</p>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
