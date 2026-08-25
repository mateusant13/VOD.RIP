import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, ExternalLink, FolderOpen, Loader2, ShieldCheck, ShieldOff } from 'lucide-react';
import { apiGet, apiPost } from '../hooks/useApiClient';
import InfoHint from './InfoHint';
import ExtensionWaitOverlay from './ExtensionWaitOverlay';
import { useI18n } from '../i18n';
import { runSilentCookieExtensionInstall } from '../lib/cookieAutoInstall';

/**
 * Cookie Bridge consent + diagnostics.
 *
 * This section IS the consent point for the local browser extension: it
 * explains what gets sent where, shows pairing/platform state, and the
 * enable/disable toggle is the kill switch (backend returns 403 for ingest
 * while disabled).
 *
 * Install flow: primary path is silent auto-install (UIA, invisible to the
 * user). Manual drag-and-drop remains as fallback in Settings.
 */

interface PlatformBridgeStatus {
  count: number;
  lastGrabAt: string | null;
  expiredCount: number;
}

export interface BridgeStatus {
  paired: boolean;
  enabled: boolean;
  platforms: Record<string, PlatformBridgeStatus>;
}

interface ExtSource {
  extension_dir: string;
  ready: boolean;
  version: string | null;
}

interface OpenResult {
  launched: boolean;
  browser: string | null;
  url: string | null;
  /** True when a browser runs but its window could not be driven. */
  blocked?: boolean;
}

const PLATFORM_LABELS: Record<string, string> = {
  youtube: 'YouTube',
  twitch: 'Twitch',
  kick: 'Kick',
};

const formatGrabTime = (iso: string | null): string => {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

export default function CookieBridgeSection({
  onStatusChange,
}: {
  /** Reports each fetched bridge status so parents can reorder content. */
  onStatusChange?: (status: BridgeStatus) => void;
}) {
  const [status, setStatus] = useState<BridgeStatus | null>(null);
  const [token, setToken] = useState('');
  const [ext, setExt] = useState<ExtSource | null>(null);
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState(false);
  const [silentInstalling, setSilentInstalling] = useState(false);
  const [waitOpen, setWaitOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { t } = useI18n();

  const refresh = useCallback(async () => {
    try {
      const s = await apiGet<BridgeStatus>('/api/session/cookies/status');
      setStatus(s);
      onStatusChange?.(s);
      setError(null);
    } catch {
      setError(t('Cookie Bridge API unreachable.'));
    }
    try {
      const t_ = await apiGet<{ token: string }>('/api/session/cookies/token');
      setToken(t_.token);
    } catch {
      /* keep last known */
    }
    try {
      const src = await apiGet<ExtSource>('/api/session/cookies/extension/source');
      setExt(src);
    } catch {
      setExt(null);
    }
  }, [onStatusChange, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);


  const clearStored = async () => {
    if (!status || busy || clearing || !token) return;
    if (!window.confirm(t('Delete all stored bridge cookies from this machine?'))) return;
    setClearing(true);
    setError(null);
    try {
      const res = await apiPost<{ cleared: number }>(
        '/api/session/cookies/clear',
        {},
        { headers: { 'X-Cookie-Bridge-Token': token } },
      );
      if (res.cleared > 0) {
        await refresh();
      }
    } catch {
      setError(t('Could not clear stored cookies — backend unreachable?'));
    }
    setClearing(false);
  };

  const toggle = async () => {
    if (!status || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiPost<{ enabled: boolean }>(
        status.enabled ? '/api/session/cookies/disable' : '/api/session/cookies/enable',
        {},
      );
      setStatus({ ...status, enabled: res.enabled });
    } catch {
      setError(t('Could not toggle Cookie Bridge — backend unreachable?'));
    }
    setBusy(false);
  };


  const silentInstall = async () => {
    if (silentInstalling || opening) return;
    setSilentInstalling(true);
    setError(null);
    try {
      const res = await runSilentCookieExtensionInstall();
      if (!res.ok) {
        setError(
          res.error
            ? t('cookieAuto.failed', { error: res.error })
            : t('cookieAuto.failedGeneric'),
        );
      } else {
        await refresh();
      }
    } catch {
      setError(t('Could not reach the backend to start install.'));
    }
    setSilentInstalling(false);
  };

  const openManager = async () => {
    if (opening) return;
    setOpening(true);
    setError(null);
    // Open the browser tab FIRST, then reveal the folder — the new tab lands
    // focused and the Explorer window pops behind it. Each failure is handled
    // independently — a failed reveal must not block the checklist, a failed
    // open keeps the existing manual-install hint.
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
    // Waiting-mode overlay takes over from here: it polls the status endpoint
    // and guides the user through the install until cookies land.
    setWaitOpen(true);
    setOpening(false);
  };

  const handleWaitStatus = useCallback(
    (s: BridgeStatus) => {
      setStatus(s);
      onStatusChange?.(s);
    },
    [onStatusChange],
  );

  const revealFolder = async () => {
    setError(null);
    try {
      await apiPost<{ ok: boolean }>('/api/session/cookies/extension/reveal', {});
    } catch {
      setError(t('Extension folder not available yet.'));
    }
  };

  const copyToken = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (non-secure context) — user can select manually */
    }
  };

  const enabled = status?.enabled ?? true;
  const platforms = status?.platforms ?? {};

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className={`text-[11px] font-mono ${status?.paired ? 'text-emerald-500' : 'text-zinc-400'}`}>
          {status?.paired ? t('● paired') : t('○ not paired')}
        </span>
        <button
          type="button"
          onClick={() => void toggle()}
          disabled={!status || busy}
          className={`flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-black uppercase border-2 transition-colors disabled:opacity-50 ${
            enabled
              ? 'bg-emerald-950 text-emerald-400 border-emerald-900 hover:border-emerald-500'
              : 'bg-red-950 text-red-400 border-red-900 hover:border-red-500'
          }`}
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : enabled ? <ShieldCheck size={13} /> : <ShieldOff size={13} />}
          {enabled ? t('Enabled') : t('Disabled')}
        </button>
      </div>

      <div className="flex items-start gap-1.5">
        <p className="text-[11px] text-zinc-300 font-mono leading-relaxed">
          {t('Local-only cookie sync — nothing leaves this machine.')}
        </p>
        <InfoHint text={t('Sends keep-listed session cookies (Kick auth_token, YouTube SID family, Twitch auth-token) from your browser to the local VOD.RIP app on 127.0.0.1 only. Disabling blocks all cookie ingestion.')} />
      </div>

      {error ? <p className="text-[11px] text-red-400 font-mono">{error}</p> : null}

      <button
        type="button"
        onClick={() => void clearStored()}
        disabled={!status || busy || clearing || !token || !enabled}
        className="self-start bg-zinc-900 text-red-400 font-black uppercase px-3 py-2 text-[11px] border-2 border-red-900 hover:border-red-500 disabled:opacity-50"
      >
        {clearing ? <Loader2 size={13} className="animate-spin inline" /> : null}
        {t('Apagar cookies armazenados')}
      </button>

      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] font-mono">
        {Object.keys(platforms).length > 0 ? (
          Object.entries(platforms).map(([platform, st]) => (
            <span key={platform} className={st.count > 0 ? 'text-zinc-300' : 'text-zinc-400'}>
              {PLATFORM_LABELS[platform] ?? platform}: {st.count}
              {st.lastGrabAt ? ` · ${formatGrabTime(st.lastGrabAt)}` : ''}
              {st.expiredCount > 0 ? (
                <span className="text-amber-500"> · {t('{count} expired', { count: st.expiredCount })}</span>
              ) : null}
            </span>
          ))
        ) : (
          <span className="text-zinc-400">{t('no cookies stored')}</span>
        )}
      </div>

      {token ? (
        <div className="flex items-center gap-2">
          <input
            type="text"
            readOnly
            value={token}
            onFocus={(e) => e.target.select()}
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
          />
          <button
            type="button"
            onClick={() => void copyToken()}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white flex items-center gap-1.5 shrink-0"
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
            {copied ? t('Copied') : t('Copy')}
          </button>
        </div>
      ) : null}

      {ext?.ready ? (
        <div className="flex flex-col gap-2 border-t-2 border-zinc-800 pt-2.5">
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => void silentInstall()}
              disabled={silentInstalling || opening}
              className="flex items-center gap-1.5 bg-zinc-100 text-black font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-100 hover:bg-zinc-300 disabled:opacity-50"
            >
              {silentInstalling ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}
              {t('cookieAuto.installNow')}
            </button>
            <button
              type="button"
              onClick={() => void openManager()}
              disabled={opening || silentInstalling}
              className="flex items-center gap-1.5 bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50"
            >
              {opening ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}
              {t('cookieBridge.manualOpen')}
            </button>
            <button
              type="button"
              onClick={() => void revealFolder()}
              title={t('Opens the cookie-extension folder — grab the VOD.RIP-cookies folder inside')}
              className="flex items-center gap-1.5 bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white"
            >
              <FolderOpen size={13} />
              {t('Show folder')}
            </button>
            {ext.version ? (
              <span className="text-[11px] text-zinc-400 font-mono ml-auto">v{ext.version}</span>
            ) : null}
          </div>
          <p className="text-[11px] text-zinc-400 font-mono leading-relaxed">
            {t('cookieBridge.silentHint')}
            <br />
            <span className="text-zinc-300 break-all">{ext.extension_dir}</span>
          </p>
        </div>
      ) : (
        <p className="text-[11px] text-zinc-400 font-mono">
          {t('Extension package not installed — restart the app to refresh it, then this flow appears here.')}
        </p>
      )}

      <ExtensionWaitOverlay
        open={waitOpen}
        extensionDir={ext?.extension_dir ?? ''}
        onClose={() => setWaitOpen(false)}
        onStatus={handleWaitStatus}
      />
    </div>
  );
}
