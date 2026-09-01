import { apiGet, apiPost } from '../hooks/useApiClient';

export interface AutoInstallExtensionStatus {
  ok: boolean;
  installed: boolean;
  extension_id: string;
}

export interface AutoInstallStatus {
  state: 'idle' | 'running' | 'done' | 'error';
  installed: boolean;
  extensions?: Record<string, AutoInstallExtensionStatus>;
  error: string | null;
}

export interface AutoInstallResult {
  ok: boolean;
  started?: boolean;
  alreadyInstalled?: boolean;
  error?: string;
}

export interface BridgeStatusWithInstall {
  paired: boolean;
  auto_install?: AutoInstallStatus;
}

const POLL_MS = 2000;
const INSTALL_TIMEOUT_MS = 120_000;

/** Silent cookie-extension install — UIA driver, no visible chrome:// flow. */
export async function runSilentCookieExtensionInstall(): Promise<{
  ok: boolean;
  alreadyInstalled?: boolean;
  error?: string;
}> {
  const res = await apiPost<AutoInstallResult>('/api/session/cookies/auto-install', {
    include_kick_overlay: true,
  });
  if (!res.ok) {
    return { ok: false, error: res.error ?? 'install failed' };
  }
  if (res.alreadyInstalled) {
    return { ok: true, alreadyInstalled: true };
  }

  const deadline = Date.now() + INSTALL_TIMEOUT_MS;
  let last: BridgeStatusWithInstall | null = null;
  while (Date.now() < deadline) {
    try {
      last = await apiGet<BridgeStatusWithInstall>('/api/session/cookies/status');
    } catch {
      await new Promise((r) => setTimeout(r, POLL_MS));
      continue;
    }
    const state = last.auto_install?.state;
    if (state && state !== 'running') break;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }

  const st = last?.auto_install;
  if (st && (st.state === 'done' || st.installed)) {
    return { ok: true };
  }
  return { ok: false, error: st?.error ?? 'install timed out' };
}
