import { useEffect, useState } from 'react';
import { AlertTriangle, BookOpen, ExternalLink, X } from 'lucide-react';
import { apiGet, apiPost } from '../hooks/useApiClient';
import { useI18n } from '../i18n';

/**
 * Any-tab banner for the YouTube bot-gate freeze.
 *
 * When yt_gate's cooldown is active AND the cookie extension is not paired,
 * YouTube archive work is silently frozen and requeued — tell the user to
 * install the extension instead of waiting blind. Signal source is the same
 * GET /api/session/cookies/status CookieBridgeSection polls (two extra
 * fields); the install actions reuse its extension/open + extension/reveal
 * endpoints. Dismiss is session-scoped (sessionStorage) and re-arms when the
 * gate lifts, so a future gate episode nags again.
 */

const POLL_INTERVAL_MS = 30_000;
const DISMISS_KEY = 'botGate.dismissed';

interface GateStatus {
  paired: boolean;
  youtube_gate_active: boolean;
  youtube_gate_remaining_sec: number;
}

interface OpenResult {
  launched: boolean;
  browser: string | null;
  url: string | null;
  /** True when a browser runs but its window could not be driven. */
  blocked?: boolean;
}

export default function BotGateBanner({
  onOpenInstructions,
}: {
  /** Switches the app to the Settings tab, where CookieBridgeSection lives. */
  onOpenInstructions: () => void;
}) {
  const [gate, setGate] = useState<GateStatus | null>(null);
  const [dismissed, setDismissed] = useState(() => sessionStorage.getItem(DISMISS_KEY) === '1');
  const [error, setError] = useState<string | null>(null);
  const [installing, setInstalling] = useState(false);
  const { t } = useI18n();

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const s = await apiGet<GateStatus>('/api/session/cookies/status');
        if (!alive) return;
        setGate(s);
        // Gate lifted → re-arm the dismiss so the NEXT gate episode nags again.
        if (!s.youtube_gate_active) {
          sessionStorage.removeItem(DISMISS_KEY);
          setDismissed(false);
        }
      } catch {
        /* backend unreachable — keep last known state, banner stays hidden */
      }
    };
    void poll();
    const id = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const dismiss = () => {
    sessionStorage.setItem(DISMISS_KEY, '1');
    setDismissed(true);
  };

  const installNow = async () => {
    if (installing) return;
    setInstalling(true);
    setError(null);
    // Same order as CookieBridgeSection.openManager: browser tab first, then
    // the Explorer reveal. Each failure handled independently.
    const openRes = await apiPost<OpenResult>('/api/session/cookies/extension/open', {}).catch(() => null);
    if (openRes === null) {
      setError(t('Could not reach the backend to open the browser tab.'));
    } else if (!openRes.launched) {
      setError(
        openRes.blocked
          ? t('Browser window could not be focused — open chrome://extensions manually and drop the folder.')
          : t('No Chromium browser found — open chrome://extensions manually and drop the folder.'),
      );
    }
    void apiPost<{ ok: boolean }>('/api/session/cookies/extension/reveal', {}).catch(() => null);
    setInstalling(false);
  };

  if (!gate || dismissed || !gate.youtube_gate_active || gate.paired) return null;

  const minutes = Math.max(1, Math.ceil(gate.youtube_gate_remaining_sec / 60));

  return (
    <div className="fixed inset-x-0 top-0 z-[20000] flex justify-center pointer-events-none">
      <div className="pointer-events-auto mx-3 mt-3 max-w-2xl bg-zinc-950 border-2 border-amber-500 text-zinc-100 shadow-2xl">
        <div className="flex items-start gap-3 px-4 py-3">
          <AlertTriangle size={18} className="text-amber-400 shrink-0 mt-0.5" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="text-[12px] font-bold leading-snug">{t('botGate.banner')}</p>
            <p className="mt-0.5 text-[11px] font-mono text-amber-400">
              {t('botGate.waiting', { min: minutes })}
            </p>
            {error ? <p className="mt-1 text-[11px] text-red-400 font-mono">{error}</p> : null}
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={onOpenInstructions}
                className="flex items-center gap-1.5 bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-1.5 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white"
              >
                <BookOpen size={13} />
                {t('botGate.openInstructions')}
              </button>
              <button
                type="button"
                onClick={() => void installNow()}
                disabled={installing}
                className="flex items-center gap-1.5 bg-amber-500 text-black font-black uppercase px-3 py-1.5 text-[11px] border-2 border-amber-500 hover:bg-amber-400 hover:border-amber-400 disabled:opacity-50"
              >
                <ExternalLink size={13} />
                {t('botGate.installNow')}
              </button>
            </div>
          </div>
          <button
            type="button"
            onClick={dismiss}
            aria-label={t('botGate.dismiss')}
            title={t('botGate.dismiss')}
            className="text-zinc-400 hover:text-white shrink-0 p-0.5"
          >
            <X size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}
