import { ShieldAlert } from 'lucide-react';
import { useI18n } from '../i18n';
import { parkVariant } from '../captionsPark';

/**
 * The parked-captions notice.
 *
 * Replaces the generic "No subtitles available for this video." when — and only
 * when — the backend says this specific video is parked behind a YouTube age
 * gate. Two things it must never do:
 *
 *  - read as a permanent verdict. The park is released the moment an
 *    authenticated YouTube session exists, so the copy says "paused" and says
 *    what clears it;
 *  - hide the backend's own words. The reason is rendered verbatim below the
 *    localized sentence: the localized text is a translation of the condition,
 *    the verbatim reason is the authoritative fact.
 */
export default function CaptionsParkedNotice({
  reason,
  onOpenCookieBridge,
}: {
  /** Backend's `captions_parked_reason`, verbatim. Non-empty by contract. */
  reason: string;
  /** Switches the app to Settings, where CookieBridgeSection lives. */
  onOpenCookieBridge?: () => void;
}) {
  const { t } = useI18n();
  const variant = parkVariant(reason);
  const body =
    variant === 'no-session'
      ? t('captionsPark.noSession')
      : variant === 'rejected-session'
        ? t('captionsPark.rejectedSession')
        : t('captionsPark.unknown');

  return (
    <div
      data-captions-parked
      className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 px-4 py-4"
    >
      <div className="flex items-center gap-1.5 text-amber-400">
        <ShieldAlert size={13} className="shrink-0" aria-hidden />
        <p className="text-ui-sm font-mono font-bold uppercase tracking-wide text-center">
          {t('captionsPark.title')}
        </p>
      </div>
      <p className="text-ui-sm font-mono text-zinc-300 text-center leading-relaxed" data-captions-park-body>
        {body}
      </p>
      {/* The park is reversible — say so before the user reads the notice as
          "this video is gone". */}
      <p
        className="text-ui-xs font-mono text-amber-400/90 text-center leading-relaxed"
        data-captions-park-reversible
      >
        {t('captionsPark.reversible')}
      </p>
      <p
        className="text-ui-xs font-mono text-zinc-500 text-center leading-relaxed break-words"
        data-captions-park-reason
      >
        {reason}
      </p>
      {onOpenCookieBridge && (
        <button
          type="button"
          onClick={onOpenCookieBridge}
          className="mt-1 border border-amber-500/60 hover:border-amber-400 hover:bg-amber-500/10 px-2 py-1 text-ui-xs font-mono font-bold uppercase tracking-wider text-amber-300"
        >
          {t('captionsPark.openSettings')}
        </button>
      )}
    </div>
  );
}
