import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, ShieldAlert, Undo2 } from 'lucide-react';
import { useI18n } from '../i18n';
import {
  learnedAt,
  parkedState,
  readParkedSnapshot,
  reasonKeyFor,
  releaseParkedChannel,
  skippedOf,
  type ParkedOutcome,
  type ParkedSnapshot,
} from '../channelPark';

/**
 * The parked-channels panel - the visible half of the learned channel state.
 *
 * Lives in the channels tab, above the channel list, because that is where the
 * owner looks at channels. A parked channel is otherwise INVISIBLE: the walk
 * skips it before drawing a governor token, so nothing in the channel row ever
 * changes and a channel that gained a /streams tab stays skipped forever with
 * no way back. That is the gap this panel closes.
 *
 * Three things it must never do:
 *
 *  - claim a channel is HEALTHY because nothing is parked. An unreachable
 *    backend and a genuine empty table are different facts, and the second one
 *    is the only one that may render the empty state (`parkedState`);
 *  - present a stored code as a reason this build understands. A row whose code
 *    the backend marked `known: false` is listed so it can be released, with the
 *    catch-all phrase, not dressed up in a sentence we invented;
 *  - let a release read as a FIX. Releasing clears the memory so the next cycle
 *    asks again - if the condition still holds, it is learned again - and that
 *    is said next to the button, not only in a comment.
 */

/** One row: which channel, which tab, why, since when, and the escape hatch. */
function ParkedRow({
  row,
  busy,
  onRelease,
}: {
  row: ParkedOutcome;
  busy: boolean;
  onRelease: (row: ParkedOutcome) => void;
}) {
  const { t } = useI18n();
  const tab = row.tab || t('channelPark.tab');
  const counted = skippedOf(row);
  // Shown ONLY when a skip was actually counted. The walk deliberately does not
  // drive this counter (test_ytdlp_outcomes.py pins "the walk counts nothing on
  // its own"), so a 0 here means "no observation was ever taken", not "this
  // saved nothing" - rendering a confident "0 requests skipped" would be a
  // silent zero wearing a number.
  const skipped = counted !== null && counted > 0 ? counted : null;
  // The localised phrase is DERIVED FROM THE STORED CODE, never the reverse: the
  // code is the contract, the sentence is a translation of it, and a wording
  // change can therefore never break a stored row.
  const reason = t(reasonKeyFor(row.outcome_code), { tab });
  // Only a park the backend vouched for may use the park colour. A row it could
  // not vouch for is rendered in muted zinc, so "we do not know" never looks
  // like "we know and it is parked".
  const known = row.known && row.permanent;
  const chip = known ? 'text-amber-300 border-amber-500/60' : 'text-zinc-400 border-zinc-600';
  const stamp = learnedAt(row.first_seen);

  return (
    <li
      data-parked-row
      data-parked-code={row.outcome_code}
      data-parked-known={known ? 'true' : 'false'}
      className="flex flex-col gap-1 border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 min-w-0"
    >
      <div className="flex items-center gap-1.5 min-w-0 flex-wrap">
        {known ? (
          <ShieldAlert size={12} className="shrink-0 text-amber-400" aria-hidden />
        ) : (
          <ShieldAlert size={12} className="shrink-0 text-zinc-500" aria-hidden />
        )}
        <span
          data-parked-channel
          className="text-ui-sm font-mono font-bold text-zinc-100 truncate min-w-0"
        >
          @{row.channel}
        </span>
        <span className="text-ui-xs font-mono text-zinc-500 shrink-0">/</span>
        <span data-parked-tab className="text-ui-xs font-mono text-zinc-400 shrink-0">
          {tab}
        </span>
        <span
          data-parked-code-chip
          title={row.outcome_code}
          className={`text-ui-xs font-mono border px-1 shrink-0 ${chip}`}
        >
          {row.outcome_code}
        </span>
        <button
          type="button"
          data-parked-release
          onClick={() => onRelease(row)}
          disabled={busy}
          aria-label={t('channelPark.release')}
          title={t('channelPark.release')}
          className="ml-auto shrink-0 flex items-center gap-1 border border-zinc-600 hover:border-amber-400 hover:bg-amber-500/10 px-1.5 py-0.5 text-ui-xs font-mono font-bold uppercase tracking-wider text-zinc-300 hover:text-amber-300 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? (
            <Loader2 size={10} className="animate-spin" aria-hidden />
          ) : (
            <Undo2 size={10} aria-hidden />
          )}
          {t('channelPark.release')}
        </button>
      </div>
      <p
        data-parked-reason
        className={`text-ui-xs font-mono leading-relaxed ${known ? 'text-amber-200/90' : 'text-zinc-400'}`}
      >
        {reason}
      </p>
      <p data-parked-learned className="text-ui-xs font-mono text-zinc-500 leading-relaxed">
        {stamp ? t('channelPark.learnedAt', { when: stamp }) : row.outcome_code}
        {skipped === null ? '' : ` · ${t('channelPark.skipped', { count: skipped })}`}
      </p>
    </li>
  );
}

export default function ChannelParkedOutcomes({
  platform = 'youtube',
}: {
  platform?: string;
}) {
  const { t } = useI18n();
  // `null` while the first read is in flight, so the panel can tell "loading"
  // from "the read failed" - two facts a single `[]` would have merged.
  const [snapshot, setSnapshot] = useState<ParkedSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [noticeKind, setNoticeKind] = useState<'ok' | 'none' | 'fail'>('none');
  // One read per mount, and a re-read after a release. The ref keeps a release
  // from racing the mount read and overwriting a fresher snapshot.
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    const next = await readParkedSnapshot(platform);
    if (!mounted.current) return;
    setSnapshot(next);
    setLoading(false);
  }, [platform]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
    };
  }, [refresh]);

  const onRelease = useCallback(
    async (row: ParkedOutcome) => {
      const key = `${row.channel}/${row.tab}`;
      setBusyKey(key);
      setNotice(null);
      setNoticeKind('none');
      try {
        const res = await releaseParkedChannel(row.channel, row.tab, platform);
        if (!mounted.current) return;
        // The backend reports what THIS call released. A second press on an
        // already-released channel answers 0, and that must read as a no-op -
        // never as a successful release of something that was not parked.
        if (res.released > 0) {
          setNotice(t('channelPark.released'));
          setNoticeKind('ok');
        } else {
          setNotice(t('channelPark.nothingToRelease'));
          setNoticeKind('none');
        }
      } catch {
        if (!mounted.current) return;
        setNotice(t('channelPark.releaseFailed'));
        setNoticeKind('fail');
      } finally {
        if (mounted.current) setBusyKey(null);
        await refresh();
      }
    },
    [platform, refresh, t],
  );

  // `loading` wins over the snapshot only while there is nothing to show yet, so
  // a re-read triggered by a release does not blank the list under the cursor.
  const state = loading && snapshot === null ? 'loading' : parkedState(snapshot);
  const rows = snapshot?.parked ?? [];

  return (
    <section
      data-channel-parked
      className="flex flex-col gap-1.5 min-w-0 border border-zinc-800 px-2 py-1.5"
    >
      <div className="flex items-baseline gap-1.5 min-w-0 flex-wrap">
        <p className="text-ui-xs font-mono font-bold uppercase tracking-wide text-zinc-300">
          {t('channelPark.title')}
        </p>
        {state === 'rows' && (
          <span data-parked-count className="text-ui-xs font-mono text-amber-400">
            {String(rows.length)}
          </span>
        )}
      </div>

      {state === 'loading' && (
        <p data-parked-loading className="text-ui-xs font-mono text-zinc-500 flex items-center gap-1">
          <Loader2 size={10} className="animate-spin" aria-hidden />
          {t('channelPark.loading')}
        </p>
      )}

      {/* NOT the empty state. The read failed, so nothing is claimed about the
          owner's channels - which is the opposite of "nothing is parked". */}
      {state === 'unavailable' && (
        <p data-parked-unavailable className="text-ui-xs font-mono text-amber-400/90 leading-relaxed">
          {t('channelPark.unavailable')}
        </p>
      )}

      {/* A real answer: the read succeeded and there is genuinely nothing. */}
      {state === 'empty' && (
        <p data-parked-empty className="text-ui-xs font-mono text-zinc-500 leading-relaxed">
          {t('channelPark.empty')}
        </p>
      )}

      {state === 'rows' && (
        <>
          <p className="text-ui-xs font-mono text-zinc-500 leading-relaxed">
            {t('channelPark.subtitle')}
          </p>
          <ul data-parked-list className="flex flex-col gap-1 min-w-0 list-none p-0 m-0">
            {rows.map((row) => (
              <ParkedRow
                key={`${row.channel}/${row.tab}`}
                row={row}
                busy={busyKey === `${row.channel}/${row.tab}`}
                onRelease={onRelease}
              />
            ))}
          </ul>
          {/* Stated where the button is, not only in a comment: a release asks
              again, and the same condition coming back is the expected
              outcome, not a bug. */}
          <p
            data-parked-reversible
            className="text-ui-xs font-mono text-amber-400/90 leading-relaxed"
          >
            {t('channelPark.reversible')}
          </p>
        </>
      )}

      {notice && (
        <p
          data-parked-notice
          data-parked-notice-kind={noticeKind}
          role="status"
          className={`text-ui-xs font-mono leading-relaxed ${
            noticeKind === 'fail' ? 'text-red-400' : 'text-zinc-400'
          }`}
        >
          {notice}
        </p>
      )}
    </section>
  );
}
