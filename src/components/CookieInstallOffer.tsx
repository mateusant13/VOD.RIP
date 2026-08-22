import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { apiPost } from '../hooks/useApiClient';
import { markSeen } from '../lib/firstTime';
import { useI18n } from '../i18n';
import { runSilentCookieExtensionInstall } from '../lib/cookieAutoInstall';

/**
 * First-run cookie-extension install offer (mounted in App.tsx).
 *
 * Shown while cookies are NOT paired and the 'cookieInstall' tutorial flag
 * has never been seen. "Instalar agora" POSTs /api/session/cookies/
 * auto-install (the backend spawns the Chrome automation in a background
 * thread and returns started:true) and polls GET /api/session/cookies/status
 * -> auto_install until the install settles. Dismissing marks the tutorial
 * seen; when the auto-install toggle is OFF the dismiss label is
 * "Não mostrar novamente" and it ALSO arms the toggle, so the offer is
 * governed by the settings flag from then on.
 */


type Phase = 'idle' | 'installing' | 'installed' | 'failed';

export default function CookieInstallOffer({
  open,
  toggleOn,
  onClose,
}: {
  open: boolean;
  /** auto_install_extension setting — undefined counts as ON. */
  toggleOn: boolean;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);

  useEffect(() => {
    if (open) {
      setPhase('idle');
      setError(null);
      busyRef.current = false;
    }
  }, [open]);

  if (!open) return null;

  const installNow = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setPhase('installing');
    setError(null);
    try {
      const res = await runSilentCookieExtensionInstall();
      if (!res.ok) {
        setError(res.error || t('cookieAuto.timeout'));
        setPhase('failed');
        return;
      }
      markSeen('cookieInstall');
      setPhase('installed');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '');
      setPhase('failed');
    } finally {
      busyRef.current = false;
    }
  };

  const dismiss = () => {
    markSeen('cookieInstall');
    if (!toggleOn) {
      // "Não mostrar novamente": arm the settings toggle too, so the offer
      // is governed by the flag from now on (a Tutorial reset re-shows it).
      void apiPost('/api/settings', { auto_install_extension: true }).catch(() => {});
    }
    onClose();
  };

  return (
    <div className="fixed inset-0 z-[21000] flex items-center justify-center bg-black/70 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t('cookieAuto.title')}
        className="w-full max-w-md border-2 border-zinc-700 bg-zinc-950 p-5 flex flex-col gap-4 shadow-2xl"
      >
        <h2 className="text-[13px] font-black uppercase tracking-widest text-zinc-200">
          {t('cookieAuto.title')}
        </h2>

        {phase === 'idle' || phase === 'installing' ? (
          <p className="text-[11px] font-mono text-zinc-400 leading-relaxed">
            {phase === 'installing' ? t('cookieAuto.installing') : t('cookieAuto.body')}
          </p>
        ) : null}

        {phase === 'installing' ? (
          <div className="flex items-center gap-2 text-[11px] font-mono text-emerald-400">
            <Loader2 size={14} className="animate-spin" />
            {t('cookieAuto.installing')}
          </div>
        ) : null}

        {phase === 'installed' ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-start gap-2 text-[11px] font-mono text-emerald-400 leading-relaxed">
              <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
              <span>{t('cookieAuto.installed')}</span>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="bg-zinc-100 text-black font-black uppercase py-2.5 text-[11px] border-2 border-zinc-100 hover:bg-zinc-300"
            >
              {t('cookieAuto.close')}
            </button>
          </div>
        ) : null}

        {phase === 'failed' ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-start gap-2 text-[11px] font-mono text-red-400 leading-relaxed">
              <XCircle size={15} className="shrink-0 mt-0.5" />
              <span>{error ? t('cookieAuto.failed', { error }) : t('cookieAuto.failedGeneric')}</span>
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => void installNow()}
                className="flex-1 bg-zinc-100 text-black font-black uppercase py-2 text-[11px] border-2 border-zinc-100 hover:bg-zinc-300"
              >
                {t('cookieAuto.retry')}
              </button>
              <button
                type="button"
                onClick={dismiss}
                className="flex-1 bg-zinc-900 text-zinc-200 font-black uppercase py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white"
              >
                {t('cookieAuto.close')}
              </button>
            </div>
          </div>
        ) : null}

        {phase === 'idle' ? (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void installNow()}
              className="flex-1 bg-zinc-100 text-black font-black uppercase py-2.5 text-[11px] border-2 border-zinc-100 hover:bg-zinc-300"
            >
              {t('cookieAuto.installNow')}
            </button>
            <button
              type="button"
              onClick={dismiss}
              className="flex-1 bg-zinc-900 text-zinc-200 font-black uppercase py-2.5 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white"
            >
              {toggleOn ? t('cookieAuto.later') : t('cookieAuto.never')}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
