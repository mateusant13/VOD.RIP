import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, Loader2 } from 'lucide-react';
import { apiGet } from '../hooks/useApiClient';
import { useI18n } from '../i18n';
import type { BridgeStatus } from './CookieBridgeSection';

/** Poll cadence for the cookie status endpoint while the overlay is open. */
export const POLL_INTERVAL_MS = 2000;
/** After this long without cookies landing, show the "still waiting" hint. */
export const STILL_WAITING_MS = 30_000;

/**
 * Waiting-mode overlay for the open-extensions flow.
 *
 * Opened right after the browser tab + folder reveal are triggered. Polls
 * GET /api/session/cookies/status until cookies land (paired + at least one
 * platform count > 0), then shows a success message and stops polling. Never
 * auto-closes — the user dismisses it.
 */
export default function ExtensionWaitOverlay({
  open,
  extensionDir,
  onClose,
  onStatus,
}: {
  open: boolean;
  /** Folder the user must drag onto chrome://extensions. */
  extensionDir: string;
  onClose: () => void;
  /** Reports every polled status so the parent keeps its inline counts fresh. */
  onStatus?: (status: BridgeStatus) => void;
}) {
  const { t } = useI18n();
  const [success, setSuccess] = useState(false);
  const [stillWaiting, setStillWaiting] = useState(false);

  // Reset per open — the same component instance survives close/reopen.
  useEffect(() => {
    if (!open) return;
    setSuccess(false);
    setStillWaiting(false);
  }, [open]);

  useEffect(() => {
    if (!open || success) return;
    let stopped = false;
    const poll = async () => {
      try {
        const s = await apiGet<BridgeStatus>('/api/session/cookies/status');
        if (stopped) return;
        onStatus?.(s);
        if (s.paired && Object.values(s.platforms).some((p) => p.count > 0)) {
          setSuccess(true);
        }
      } catch {
        /* transient backend hiccup — keep waiting; the hint covers no-progress */
      }
    };
    void poll(); // check immediately — the user may already be paired
    const interval = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    const hint = window.setTimeout(() => {
      if (!stopped) setStillWaiting(true);
    }, STILL_WAITING_MS);
    return () => {
      stopped = true;
      window.clearInterval(interval);
      window.clearTimeout(hint);
    };
  }, [open, success, onStatus]);

  if (!open) return null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t('Waiting for cookies')}
      className="fixed inset-0 z-[400] flex items-center justify-center bg-black/75 p-4"
    >
      <div
        className="bg-zinc-950 border-2 border-white p-5 font-mono text-sm flex flex-col gap-3 min-w-[22rem] max-w-[30rem]"
        style={{ boxShadow: '4px 4px 0px 0px #34d399' }}
      >
        <div className="flex items-center gap-2">
          {success ? (
            <CheckCircle2 size={14} className="text-emerald-400 shrink-0" />
          ) : (
            <Loader2 size={14} className="text-emerald-400 animate-spin shrink-0" />
          )}
          <p className="text-zinc-200 text-[10px] font-bold uppercase tracking-widest">
            {t('Waiting for cookies')}
          </p>
        </div>

        {success ? (
          <p role="status" className="text-emerald-400 text-xs leading-relaxed font-bold">
            {t('Cookies detected — you can close this.')}
          </p>
        ) : (
          <>
            <p className="text-zinc-400 text-xs leading-relaxed">
              {t('Install and enable the extension to start syncing cookies:')}
            </p>
            <p className="text-[11px] text-zinc-400 font-mono leading-relaxed">
              {t('Grab the')} <span className="text-zinc-200">VOD.RIP-cookies</span>
              {t('folder (the one marked “drag this folder above”) and drop it onto the extensions page (Developer mode ON):')}
              <br />
              <span className="text-zinc-300 break-all">{extensionDir}</span>
            </p>
            <ol className="text-[11px] font-mono text-zinc-400 list-decimal list-inside leading-relaxed">
              <li>
                {t('Toggle')} <span className="text-zinc-200">Developer mode</span>
                {t(' ON (top-right corner of the tab).')}
              </li>
              <li>{t('Drop the VOD.RIP-cookies folder onto the page.')}</li>
              <li>{t('Open the extension popup on Kick or YouTube once — cookies land here.')}</li>
            </ol>
          </>
        )}

        <div className="flex items-center justify-between gap-3 border-t-2 border-zinc-800 pt-3 mt-1">
          {success ? null : stillWaiting ? (
            <span className="text-[11px] font-mono text-amber-400">
              {t('Still waiting — check that Developer mode is ON and that the extension popup was opened on Kick or YouTube.')}
            </span>
          ) : (
            <span className="text-[11px] font-mono text-zinc-500">{t('Waiting for cookies…')}</span>
          )}
          <button
            type="button"
            onClick={onClose}
            className="ml-auto border-2 border-zinc-600 text-zinc-300 hover:border-white hover:text-white px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider"
          >
            {t('Close')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
