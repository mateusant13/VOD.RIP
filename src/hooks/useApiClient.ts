/**
 * Shared API client — extracted from App.tsx, now reusable by ChannelExplorePopup.tsx
 * and any future component that needs backend access.
 *
 * ponytail: App.tsx and ChannelExplorePopup.tsx had separate apiPost/apiGet/apiDelete
 * with different retry/timeout behaviour. One client, imported everywhere.
 */

const API_BASE = '';
const API_TIMEOUT_MS = 60_000;
const API_RETRY_ATTEMPTS = 2;
const API_RETRY_BACKOFF_MS = 900;

const IS_DEV_UI = import.meta.env.DEV;
const TIMEOUT_HINT = IS_DEV_UI
  ? 'The backend is taking too long. Please wait a moment and try again.'
  : 'The app is taking too long. Please wait a moment and try again.';
const BACKEND_HINT = IS_DEV_UI
  ? 'Backend not running. Start the app with: npm run dev  (API on http://localhost:7897 + UI on :5173).'
  : 'API not reachable. Quit VOD.RIP from the tray and reopen the app.';

function formatApiDetail(detail: unknown): string {
  if (detail == null) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg?: string }).msg ?? item);
        }
        return String(item);
      })
      .filter(Boolean)
      .join('; ');
  }
  if (typeof detail === 'object') return JSON.stringify(detail);
  return String(detail);
}

function apiErrorMessage(res: Response, fallback: string, path?: string): string {
  const p = path ?? '';
  // Preview extract failures are 500 with a real message — not a dead backend.
  if ((res.status === 500 || res.status === 503) && p.includes('/api/preview/')) {
    return fallback || 'Preview failed — try again.';
  }
  if (res.status === 500 || res.status === 502 || res.status === 503) {
    return IS_DEV_UI
      ? 'Backend not running. Start the app with: npm run dev  (API on http://localhost:7897 + UI on :5173).'
      : 'API not reachable. Quit VOD.RIP from the tray and reopen the app.';
  }
  if (res.status === 404) {
    const p = path ?? '';
    const fb = String(fallback).toLowerCase();
    if (p.includes('/api/channel/clips') || fb === 'not found') {
      return IS_DEV_UI
        ? 'Clips API not on server — restart with npm run dev'
        : 'Clips API unavailable — quit VOD.RIP from the tray and reopen the app';
    }
  }
  if (res.status === 405) {
    return IS_DEV_UI
      ? 'API method not supported — restart with npm run dev'
      : 'API method not supported — reopen VOD.RIP';
  }
  return fallback;
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  let lastErr: unknown;
  // Retry network failures and our own timeout, but never retry a caller's
  // intentional cancellation. AbortSignal.timeout() throws a distinct
  // TimeoutError, and AbortSignal.any() lets us honour a caller-supplied
  // signal without losing that distinction.
  for (let i = 0; i <= API_RETRY_ATTEMPTS; i++) {
    const timeoutSignal = AbortSignal.timeout(API_TIMEOUT_MS);
    const signal = init?.signal
      ? AbortSignal.any([init.signal, timeoutSignal])
      : timeoutSignal;
    try {
      return await fetch(`${API_BASE}${path}`, { ...init, signal });
    } catch (err: unknown) {
      lastErr = err;
      // Caller-initiated aborts are not retries; surface them immediately.
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw err;
      }
      if (i >= API_RETRY_ATTEMPTS) break;
      await new Promise((resolve) =>
        window.setTimeout(resolve, API_RETRY_BACKOFF_MS * (i + 1))
      );
    }
  }
  const isTimeout =
    lastErr instanceof DOMException && (lastErr as DOMException).name === 'TimeoutError';
  throw new Error(isTimeout ? TIMEOUT_HINT : BACKEND_HINT);
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await apiFetch(path);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = formatApiDetail(err.detail) || `HTTP ${res.status}`;
    throw new Error(apiErrorMessage(res, detail, path));
  }
  return res.json();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = formatApiDetail(err.detail) || `HTTP ${res.status}`;
    throw new Error(apiErrorMessage(res, detail, path));
  }
  return res.json();
}

export async function apiDelete(path: string): Promise<void> {
  const res = await apiFetch(path, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = formatApiDetail(err.detail) || `HTTP ${res.status}`;
    throw new Error(apiErrorMessage(res, detail));
  }
}
