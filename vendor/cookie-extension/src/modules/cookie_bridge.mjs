/**
 * VOD.RIP cookie bridge — pushes keep-listed cookies for kick.com,
 * youtube.com and twitch.tv to the local backend (http://127.0.0.1:7897).
 *
 * Privacy rule: only cookies whose NAME is in the per-platform keep-list ever
 * leave the browser, and only cookies from the three platform domains are
 * ever collected. The backend re-applies the same keep-list as an
 * authoritative second gate.
 *
 * Pure functions live here so the debounce/filter/payload logic can be
 * asserted from Node with a fake `chrome` shim (scripts/selftest-cookie-bridge.mjs).
 */

export const API_BASE = 'http://127.0.0.1:7897';
export const BRIDGE_DOMAINS = ['kick.com', 'youtube.com', 'twitch.tv'];

const API_BASE_KEY = 'vodrip_bridge_api_base';

/**
 * Bridge endpoint. Defaults to the VOD.RIP app port 7897; a per-install
 * override (chrome.storage.local) allows testing against another port
 * without touching the default.
 */
export const getApiBase = async () => {
  const stored = await chrome.storage.local.get(API_BASE_KEY);
  return stored[API_BASE_KEY] || API_BASE;
};

// MUST mirror backend/services/cookie_store.py KEEP_LISTS.
export const KEEP_LISTS = {
  kick: new Set(['auth_token', 'g_session']),
  youtube: new Set([
    'SID',
    '__Secure-1PAPISID',
    '__Secure-3PAPISID',
    'APISID',
    'SAPISID',
    'HSID',
    'SSID',
    '__Secure-1PSID',
    '__Secure-3PSID',
    'VISITOR_INFO1_LIVE',
  ]),
  twitch: new Set(['auth-token', 'sp']),
};

const DOMAIN_PLATFORMS = [
  ['kick.com', 'kick'],
  ['youtube.com', 'youtube'],
  ['twitch.tv', 'twitch'],
];

/** Map a cookie domain to a platform (bare host, leading dot, or subdomain). */
export const platformForDomain = (domain) => {
  const d = String(domain || '').trim().toLowerCase().replace(/^\./, '');
  if (!d) return null;
  for (const [suffix, platform] of DOMAIN_PLATFORMS) {
    if (d === suffix || d.endsWith(`.${suffix}`)) return platform;
  }
  return null;
};

export const isKept = (platform, name) =>
  Boolean(platform && KEEP_LISTS[platform]?.has(name));

/** Map a chrome.cookies.Cookie to the backend wire shape. */
const toPayloadCookie = (c) => {
  const payload = {
    name: c.name,
    domain: c.domain,
    path: c.path,
    secure: Boolean(c.secure),
    httpOnly: Boolean(c.httpOnly),
    value: c.value,
  };
  // Session cookies have no expirationDate — omit rather than send null.
  if (typeof c.expirationDate === 'number') payload.expirationDate = c.expirationDate;
  return payload;
};

/**
 * Filter raw cookies to the keep-listed payload list.
 * Drops foreign domains and non-kept names; returns [] when nothing qualifies.
 */
export const filterCookies = (cookies) => {
  const out = [];
  for (const c of cookies || []) {
    const platform = platformForDomain(c.domain);
    if (!platform || !isKept(platform, c.name)) continue;
    out.push(toPayloadCookie(c));
  }
  return out;
};

const TOKEN_KEY = 'vodrip_cookie_bridge_token';

/**
 * Per-install token: generated once with crypto.randomUUID, persisted in
 * chrome.storage.local. The backend pairs on the first POST that carries it.
 */
export const getToken = async () => {
  const stored = await chrome.storage.local.get(TOKEN_KEY);
  if (stored[TOKEN_KEY]) return stored[TOKEN_KEY];
  const token = crypto.randomUUID();
  await chrome.storage.local.set({ [TOKEN_KEY]: token });
  return token;
};

/** POST a payload list to the bridge endpoint; throws on non-2xx. */
export const postCookies = async (cookies) => {
  let token = await getToken();
  // Adopt the backend's paired token if it already has one: a locally minted
  // UUID would 403 forever once the bridge is paired (backend re-pairs on
  // mismatch, but converging here keeps the pair stable across re-installs).
  try {
    const res = await fetch(`${await getApiBase()}/api/session/cookies/token`);
    if (res.ok) {
      const body = await res.json();
      if (body && body.token) {
        token = body.token;
        await chrome.storage.local.set({ [TOKEN_KEY]: token });
      }
    }
  } catch { /* app unreachable — the POST below fails identically */ }
  const res = await fetch(`${await getApiBase()}/api/session/cookies`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, cookies }),
  });
  if (!res.ok) throw new Error(`cookie bridge POST failed: ${res.status}`);
  return res;
};

/**
 * Debounced push factory. `collect()` returns raw cookies, `post()` receives
 * the filtered payload. Repeated changes within `delayMs` collapse into one
 * POST; a push in flight is skipped (the next change re-schedules).
 */
export const createDebouncedPush = ({ collect, post, delayMs = 300 }) => {
  let timer = null;
  let inFlight = false;

  const fire = async () => {
    timer = null;
    if (inFlight) return;
    inFlight = true;
    try {
      const payload = filterCookies(await collect());
      if (payload.length > 0) await post(payload);
    } catch (err) {
      console.warn('[vodrip-bridge] push failed (backend offline?)', err);
    } finally {
      inFlight = false;
    }
  };

  const schedule = () => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(fire, delayMs);
  };
  return schedule;
};
