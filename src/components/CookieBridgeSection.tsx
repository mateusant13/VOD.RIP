import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, ExternalLink, FolderOpen, Loader2, ShieldCheck, ShieldOff } from 'lucide-react';
import { apiGet, apiPost } from '../hooks/useApiClient';

/**
 * Cookie Bridge consent + diagnostics.
 *
 * This section IS the consent point for the local browser extension: it
 * explains what gets sent where, shows pairing/platform state, and the
 * enable/disable toggle is the kill switch (backend returns 403 for ingest
 * while disabled).
 *
 * Install flow (drag-and-drop): the backend materializes the unpacked
 * extension folder next to the packaged crx; the user opens
 * chrome://extensions, toggles Developer mode, and drops the folder onto
 * the page. No admin, no policy, no certs — works on unmanaged Windows.
 */

interface PlatformBridgeStatus {
  count: number;
  lastGrabAt: string | null;
  expiredCount: number;
}

interface BridgeStatus {
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

export default function CookieBridgeSection() {
  const [status, setStatus] = useState<BridgeStatus | null>(null);
  const [token, setToken] = useState('');
  const [ext, setExt] = useState<ExtSource | null>(null);
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState(false);
  const [opened, setOpened] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await apiGet<BridgeStatus>('/api/session/cookies/status');
      setStatus(s);
      setError(null);
    } catch {
      setError('Cookie Bridge API unreachable.');
    }
    try {
      const t = await apiGet<{ token: string }>('/api/session/cookies/token');
      setToken(t.token);
    } catch {
      /* keep last known */
    }
    try {
      const src = await apiGet<ExtSource>('/api/session/cookies/extension/source');
      setExt(src);
    } catch {
      setExt(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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
      setError('Could not toggle Cookie Bridge — backend unreachable?');
    }
    setBusy(false);
  };

  const openManager = async () => {
    if (opening) return;
    setOpening(true);
    setError(null);
    try {
      const res = await apiPost<OpenResult>('/api/session/cookies/extension/open', {});
      setOpened(res.launched);
      if (!res.launched) {
        setError('No Chromium browser found — open chrome://extensions manually and drop the folder.');
      }
      // refresh once shortly after so freshly pushed cookies show in the counts
      setTimeout(() => void refresh(), 5000);
    } catch {
      setError('Could not reach the backend to open the browser tab.');
    }
    setOpening(false);
  };

  const revealFolder = async () => {
    setError(null);
    try {
      await apiPost<{ ok: boolean }>('/api/session/cookies/extension/reveal', {});
    } catch {
      setError('Extension folder not available yet.');
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
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className={`text-[9px] font-mono ${status?.paired ? 'text-emerald-600' : 'text-zinc-600'}`}>
          {status?.paired ? '● paired' : '○ not paired'}
        </span>
        <button
          type="button"
          onClick={() => void toggle()}
          disabled={!status || busy}
          className={`flex items-center gap-1 px-2 py-0.5 text-[10px] font-black uppercase border-2 transition-colors disabled:opacity-50 ${
            enabled
              ? 'bg-emerald-950 text-emerald-400 border-emerald-900 hover:border-emerald-500'
              : 'bg-red-950 text-red-400 border-red-900 hover:border-red-500'
          }`}
        >
          {busy ? <Loader2 size={11} className="animate-spin" /> : enabled ? <ShieldCheck size={11} /> : <ShieldOff size={11} />}
          {enabled ? 'Enabled' : 'Disabled'}
        </button>
      </div>

      <p className="text-[9px] text-zinc-600 font-mono leading-snug">
        Sends keep-listed session cookies (Kick auth_token, YouTube SID family, Twitch
        auth-token) from your browser to the local VOD.RIP app on 127.0.0.1 only.
        Nothing leaves this machine. Disabling blocks all cookie ingestion.
      </p>

      {error ? <p className="text-[9px] text-red-500 font-mono">{error}</p> : null}

      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] font-mono">
        {Object.keys(platforms).length > 0 ? (
          Object.entries(platforms).map(([platform, st]) => (
            <span key={platform} className={st.count > 0 ? 'text-zinc-500' : 'text-zinc-700'}>
              {PLATFORM_LABELS[platform] ?? platform}: {st.count}
              {st.lastGrabAt ? ` · ${formatGrabTime(st.lastGrabAt)}` : ''}
              {st.expiredCount > 0 ? (
                <span className="text-amber-500"> · {st.expiredCount} expired</span>
              ) : null}
            </span>
          ))
        ) : (
          <span className="text-zinc-700">no cookies stored</span>
        )}
      </div>

      {token ? (
        <div className="flex items-center gap-2">
          <input
            type="text"
            readOnly
            value={token}
            onFocus={(e) => e.target.select()}
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1 px-2 text-[10px] focus:outline-none focus:border-white"
          />
          <button
            type="button"
            onClick={() => void copyToken()}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white flex items-center gap-1 shrink-0"
          >
            {copied ? <Check size={11} /> : <Copy size={11} />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      ) : null}

      {ext?.ready ? (
        <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void openManager()}
              disabled={opening}
              className="flex items-center gap-1 bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50"
            >
              {opening ? <Loader2 size={11} className="animate-spin" /> : <ExternalLink size={11} />}
              Open extensions
            </button>
            <button
              type="button"
              onClick={() => void revealFolder()}
              className="flex items-center gap-1 bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white"
            >
              <FolderOpen size={11} />
              Show folder
            </button>
            {ext.version ? (
              <span className="text-[9px] text-zinc-600 font-mono ml-auto">v{ext.version}</span>
            ) : null}
          </div>
          <p className="text-[9px] text-zinc-600 font-mono leading-snug">
            Drag this folder onto the extensions page (Developer mode ON):
            <br />
            <span className="text-zinc-400 break-all">{ext.extension_dir}</span>
          </p>
          {opened ? (
            <ol className="text-[9px] font-mono text-zinc-500 list-decimal list-inside leading-snug">
              <li>
                Toggle <span className="text-zinc-300">Developer mode</span> ON (top-right corner of the tab).
              </li>
              <li>Drop the folder above onto the page.</li>
              <li>Open the extension popup on Kick or YouTube once — cookies land here.</li>
            </ol>
          ) : null}
        </div>
      ) : (
        <p className="text-[9px] text-zinc-600 font-mono">
          Extension package not installed — restart the app to refresh it, then this flow appears here.
        </p>
      )}
    </div>
  );
}
