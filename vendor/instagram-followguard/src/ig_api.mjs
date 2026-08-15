// IG FollowGuard — Instagram web API client (followers/following).
// Talks to the SAME internal API the logged-in web app uses, with the
// session cookies of the user's real profile (chrome.cookies). Reads only
// follow/follower relationships — never DMs, never posts.
'use strict';

export const IG_WEB_APP_ID = '936619743392459'; // www.instagram.com web client app id
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const PAGE_SIZE = 24;               // web-app follow-list page size — the web UI requests 24 per scroll; 200/page in a burst is the scraper fingerprint that got the friend's account gated
// Humanized pacing base (ms/page). Fixed-interval loops are the #1 bot
// fingerprint; the web app paces by the user's SCROLL — variable gaps and
// pauses. pageDelayMs is the BASE: real gaps are randomized around it and
// ~10% of pauses are 2-5.5× longer ("reading" the list). count=24 × ~2.6s
// avg ≈ a real person scrolling the dialog; 1048 accounts take ~2min (the
// web app itself would take as long).
const PAGE_DELAY_MS = 2000;
const FETCH_TIMEOUT_MS = 20000;     // per-request timeout — a throttled IG connection hangs forever without one
const MAX_PAGES = 500;              // hard cap per list (500*24 = 12k users)
const MAX_RETRIES = 5;              // consecutive transient failures
// Backoff per error class: http/network are soft transients; rate-limited and
// gate responses need MINUTES (short retries just re-enter the gate).
const RETRY_BACKOFF_MS = {
  http: [30e3, 60e3, 120e3, 240e3, 240e3],
  network: [30e3, 60e3, 120e3, 240e3, 240e3],
  'rate-limited': [60e3, 120e3, 180e3, 300e3, 300e3],
};
export const RESUME_TTL_MS = 60 * 60 * 1000; // discard checkpoints older than 1h
// Internal test seams — real values are far too slow for unit tests.
let retryBaseMs = 5000;
let fetchTimeoutMs = FETCH_TIMEOUT_MS;
let pageDelayMs = PAGE_DELAY_MS;
export function __setRetryBaseMsForTests(v) { retryBaseMs = v; }
export function __setFetchTimeoutMsForTests(v) { fetchTimeoutMs = v; }
export function __setPageDelayMsForTests(v) { pageDelayMs = v; }

// Transport indirection. Production (background.js) swaps in a page-context
// transport: the fetch is executed by content_proxy.js on an instagram.com
// TAB, same-origin, so requests carry the browser's REAL headers
// (sec-fetch-site: same-origin, real UA, sec-ch-ua, accept-language,
// referer) and native cookies. A service-worker fetch would advertise
// `sec-fetch-site: none` + a forged cookie header + a fabricated UA — a
// fingerprint no browser ever produces. With the page transport the request
// is byte-identical to the web app's own. The default transport exists for
// tests and runs only when no transport is installed.
let transport = null;
export function __setTransport(fn) { transport = fn; }

// Humanized inter-page pause: never a fixed interval. Short gaps (0.6-1.8×
// base) most of the time, ~10% of pauses are 2-5.5× base — the "hesitating /
// reading" behavior of a real person scrolling a list.
function humanPauseMs() {
  if (pageDelayMs <= 0) return 0;
  const r = Math.random();
  if (r < 0.1) return Math.round(pageDelayMs * (2 + Math.random() * 3.5));
  return Math.round(pageDelayMs * (0.6 + Math.random() * 1.2));
}
function backoffFor(code, retries) {
  if (retryBaseMs !== 5000) return retryBaseMs; // test seam
  const seq = RETRY_BACKOFF_MS[code] || RETRY_BACKOFF_MS.http;
  return seq[Math.min(retries - 1, seq.length - 1)];
}

export class IgApiError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code; // not-logged-in | checkpoint | rate-limited | http | network | aborted
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Read the Instagram session cookies of the user's real profile. */
export async function readSession() {
  const all = await chrome.cookies.getAll({ domain: '.instagram.com' });
  const get = (name) => {
    const c = all.find((x) => x.name === name);
    return c ? c.value : null;
  };
  const sessionid = get('sessionid');
  if (!sessionid) {
    throw new IgApiError('not-logged-in', 'Nenhuma sessão do Instagram encontrada. Abra instagram.com logado e tente de novo.');
  }
  return {
    sessionid,
    uid: get('ds_user_id'),
    csrftoken: get('csrftoken'),
    cookieHeader: all
      .filter((c) => c.domain.includes('instagram.com') || c.domain === '.instagram.com')
      .map((c) => `${c.name}=${c.value}`)
      .join('; '),
  };
}

// fetch with a hard per-request timeout (an IG rate gate holds connections
// instead of answering — without this the sync hangs forever). Composes the
// caller's abort signal manually (AbortSignal.any needs Chrome 116+; the
// manifest allows 102).
function timedFetch(url, opts, signal) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), fetchTimeoutMs);
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener('abort', () => ctrl.abort(), { once: true });
  }
  return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => clearTimeout(timer));
}

export async function apiFetch(path, session, { signal } = {}) {
  const headers = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'pt-BR,pt;q=0.9,en;q=0.8',
    'content-type': 'application/x-www-form-urlencoded',
    'cookie': session.cookieHeader,
    'origin': 'https://www.instagram.com',
    'referer': 'https://www.instagram.com/',
    'user-agent': UA,
    'x-csrftoken': session.csrftoken || '',
    'x-ig-app-id': IG_WEB_APP_ID,
    'x-ig-www-claim': '0',
    'x-requested-with': 'XMLHttpRequest',
  };
  let status, text;
  try {
    if (transport) {
      // Page-context transport (content_proxy.js on an IG tab). Same-origin
      // fetch → browser-real headers + native cookies. The tab's proxy aborts
      // at the same 20s policy; the SW-side race is a belt for a dead tab.
      let timer;
      const timeoutP = new Promise((_, rej) => {
        timer = setTimeout(
          () => rej(new IgApiError('network', `O Instagram não respondeu em ${Math.round(fetchTimeoutMs / 1000)}s.`)),
          fetchTimeoutMs,
        );
      });
      try {
        const r = await Promise.race([transport(path, session, signal), timeoutP]);
        status = r.status;
        text = String(r.text ?? '');
      } finally {
        clearTimeout(timer);
      }
    } else {
      // Production guard: in Chrome, an IG request must NEVER be a SW fetch
      // (sec-fetch-site: none + forged headers = fingerprint). All production
      // paths install the page transport via withPageTransport(); this branch
      // exists for unit tests (no chrome global) and fails loudly otherwise.
      if (typeof chrome !== 'undefined') {
        throw new IgApiError('http', 'IGF interno: requisição sem transporte de página.');
      }
      const res = await timedFetch(`https://www.instagram.com${path}`, {
        method: 'GET',
        headers,
        credentials: 'include',
      }, signal);
      status = res.status;
      text = await res.text().catch(() => ''); // a text() failure is also transient
    }
  } catch (err) {
    // Timeout abort OR raw network failure (offline/DNS/reset). Both are
    // transient and must reach the retry path — 'network' used to be dead
    // because raw TypeErrors have no .code and died without a retry.
    if (err instanceof IgApiError) throw err; // page transport already classified
    if (err && err.name === 'AbortError') {
      throw new IgApiError('network', `O Instagram não respondeu em ${Math.round(fetchTimeoutMs / 1000)}s.`);
    }
    throw new IgApiError('network', 'Falha de rede ao falar com o Instagram.');
  }
  let body = null;
  if (!text) throw new IgApiError('network', 'Falha de rede ao falar com o Instagram.');
  try {
    body = JSON.parse(text);
  } catch {
    body = null; // non-JSON (login wall / challenge / gate HTML)
  }
  if (status === 429) throw new IgApiError('rate-limited', 'Instagram limitou as requisições (429).');
  if (status === 401) throw new IgApiError('not-logged-in', 'Sessão do Instagram expirou. Faça login e tente de novo.');
  if (status === 403) throw new IgApiError('checkpoint', 'Instagram pediu verificação (403).');
  if (status < 200 || status >= 300) {
    // 4xx/5xx with an actionable fail body beats the generic HTTP message.
    const why = body && (body.message || body.error_type);
    const low = String(why || '').toLowerCase();
    if (low.includes('login_required')) throw new IgApiError('not-logged-in', 'Sessão do Instagram expirou. Faça login e tente de novo.');
    if (low.includes('checkpoint')) throw new IgApiError('checkpoint', 'Instagram pediu verificação. Abra instagram.com no navegador.');
    throw new IgApiError('http', `HTTP ${status} em ${path.split('?')[0]}`);
  }
  if (!body || body.status === 'fail') {
    const why = (body && (body.message || body.error_type || body.feedback_title)) || '';
    const low = String(why).toLowerCase();
    if (low.includes('login_required')) {
      throw new IgApiError('not-logged-in', 'Sessão do Instagram expirou. Faça login e tente de novo.');
    }
    if (low.includes('checkpoint') || low.includes('challenge') || low.includes('confirmar que é você')) {
      throw new IgApiError('checkpoint', 'Instagram pediu verificação. Abra instagram.com no navegador.');
    }
    // Throttle family: rate_limit_error, "wait a few minutes", pt-BR "aguarde",
    // and the BARE {"status":"fail"} with no message — IG's most common
    // throttling response, previously mislabeled as the friend's exact
    // "resposta inválida" dead end. Now a proper rate-limited state: the
    // 5-min auto-retry resumes from the last checkpoint when the gate lifts.
    if (low.includes('rate_limit_error') || low.includes('rate limit') ||
        low.includes('few minutes') || low.includes('try again') ||
        low.includes('aguarde') || low.includes('alguns minutos') ||
        (body && !why)) {
      // (body && !why): BARE {"status":"fail"} — IG's most common throttling
      // shape. body=null (HTML) must NOT land here — it falls to the sniff
      // below.
      throw new IgApiError('rate-limited', 'O Instagram limitou as requisições temporariamente. A extensão vai tentar de novo sozinha em alguns minutos, retomando de onde parou — não precisa fazer nada.');
    }
    if (low.includes('feedback_required') || low.includes('action_blocked') ||
        low.includes('spam') || low.includes('automático') || low.includes('sinalizada')) {
      // Bot gate — retrying it is provably useless and deepens the gate.
      const title = (body && body.feedback_title) || 'Sua conta foi temporariamente limitada.';
      throw new IgApiError('feedback-required', `${title} Aguarde algumas horas e tente de novo, ou abra o Instagram.`);
    }
    // Non-JSON 200: the raw body is a login wall, challenge or gate HTML page
    // masquerading as JSON — this was the friend's exact "resposta inválida"
    // dead end (gate HTML after a hang). Sniff before the generic message.
    if (!body) {
      const t = text.slice(0, 4000).toLowerCase();
      if (t.includes('accounts/login') || t.includes('login_required')) {
        throw new IgApiError('not-logged-in', 'Sessão do Instagram expirou. Faça login e tente de novo.');
      }
      if (t.includes('checkpoint') || t.includes('challenge') || t.includes('confirmar que é você')) {
        throw new IgApiError('checkpoint', 'Instagram pediu verificação. Abra instagram.com no navegador.');
      }
      if (t.includes('feedback_required') || t.includes('temporarily limited') || t.includes('temporariamente limitada') ||
          t.includes('action_blocked') || t.includes('sinalizada') || t.includes('spam') ||
          t.includes('tentamos processar') || t.includes('automatizado') || t.includes('bloqueada')) {
        throw new IgApiError('feedback-required', 'Sua conta foi temporariamente limitada. Aguarde algumas horas e tente de novo, ou abra o Instagram.');
      }
    }
    // Unclassified fallback — carry a diagnostic snippet so a recurring error
    // can be identified from the panel text (status + first chars of body).
    const snippet = text.slice(0, 100).replace(/\s+/g, ' ').trim();
    throw new IgApiError('http', `Instagram respondeu: resposta inválida (HTTP ${status} · "${snippet}")`);
  }
  return body;
}

// Run an IG request with the SAME transient-retry policy as the list walk.
// Used by resolveOwnUser — the sync's first step must not die on a blip with
// zero retries (it used to: raw fetch, no classification, no timeout).
export async function transientRetry(fn) {
  let retries = 0;
  for (;;) {
    try {
      return await fn();
    } catch (err) {
      if (err && (err.code === 'network' || err.code === 'http')) {
        retries += 1;
        if (retries > MAX_RETRIES) throw err;
        await sleep(backoffFor(err.code, retries));
        continue;
      }
      throw err;
    }
  }
}

/**
 * Fetch ONE page of a follow list.
 * kind: 'followers' | 'following'.
 * Returns { users, nextMaxId, usersPresent } — usersPresent distinguishes a
 * genuinely empty list (users: []) from a malformed response without a
 * users array (which would silently truncate the list — the "no account may
 * be missing" invariant).
 */
async function fetchPage(kind, uid, maxId, session, signal) {
  const params = new URLSearchParams({ count: String(PAGE_SIZE), search_surface: 'follow_list_page' });
  if (maxId) params.set('max_id', maxId);
  const path = `/api/v1/friendships/${uid}/${kind}/?${params.toString()}`;
  const body = await apiFetch(path, session, { signal });
  const usersPresent = Array.isArray(body.users);
  const users = usersPresent ? body.users : [];
  const next = body.next_max_id;
  const hasMore = !!(next && body.big_list !== false);
  return { users, nextMaxId: hasMore ? next : null, usersPresent };
}

/**
 * Fetch the COMPLETE list (all pages) — no account may be missing.
 * Options:
 *   resume  { maxId, users } — continue from a persisted checkpoint instead
 *           of re-fetching the whole list (a failed/restarted sync resumes,
 *           never "from scratch"). maxId chains from the persisted pages, so
 *           the merged result is exactly what a from-scratch run would get.
 *   onPart  ({ seq, maxId, users }) — called after each page that has a next
 *           page; the caller persists the checkpoint (best-effort).
 * Returns Map<username, meta>. Throws IgApiError on terminal failure.
 */
export async function fetchAllUsers(kind, uid, session, { signal, onProgress, resume, onPart } = {}) {
  const out = new Map();
  let maxId = null;
  let seq = 0; // monotonic across runs — a restarted counter would clobber old checkpoints
  let retries = 0;
  let completed = false;
  const pick = (u) => ({
    pk: String(u.pk || ''),
    username: u.username,
    full_name: u.full_name || '',
    is_private: !!u.is_private,
    is_verified: !!u.is_verified,
    profile_pic_url: u.profile_pic_url || '',
  });
  if (resume && resume.maxId && Array.isArray(resume.users)) {
    seq = typeof resume.nextSeq === 'number' ? resume.nextSeq : 0;
    for (const u of resume.users) {
      if (!u || !u.username) continue;
      out.set(u.username, pick(u));
    }
    maxId = resume.maxId;
  }
  for (let page = 0; page < MAX_PAGES; page += 1) {
    try {
      const { users, nextMaxId, usersPresent } = await fetchPage(kind, uid, maxId, session, signal);
      // Malformed response (no users array at all) would silently TRUNCATE
      // the list — from scratch OR resumed (a seeded resume has out.size>0,
      // so the guard must not depend on it). A genuine empty list always
      // carries users: [] (usersPresent=true), so no false positive. Thrown
      // as 'http' → retried as transient, then surfaces as sync error with
      // checkpoints intact (next attempt resumes, never completes short).
      // NOTE: the retry budget (retries) must reset AFTER this guard — a
      // page that "succeeds" at fetchPage but fails validation here would
      // otherwise reset the counter every attempt and retry forever.
      if (!usersPresent && users.length === 0) {
        throw new IgApiError('http', 'Instagram respondeu com uma resposta inesperada.');
      }
      for (const u of users) {
        if (!u || typeof u.username !== 'string' || !u.username) {
          // IG never sends null/empty entries. A malformed page must NOT let
          // the cursor advance past it — silent skip = missing accounts +
          // mass fake unfollows. Fail into the retry path instead.
          throw new IgApiError('http', 'Instagram respondeu com uma página de dados inválida.');
        }
        out.set(u.username, pick(u));
      }
      retries = 0; // page produced usable data — reset the consecutive-failure budget
      if (onProgress) onProgress({ kind, fetched: out.size });
      if (onPart && users.length && nextMaxId) {
        try {
          // Await before advancing: a not-awaited checkpoint write can still
          // be in flight when the caller clears partials after success,
          // resurrecting a stale key past clearPartials.
          await onPart({ seq: seq++, maxId: nextMaxId, users: users.map(pick) });
        } catch {
          // checkpoint persistence is best-effort — resume granularity only
        }
      }
      if (!nextMaxId) {
        completed = true;
        break;
      }
      maxId = nextMaxId;
      await sleep(humanPauseMs());
    } catch (err) {
      if (signal && signal.aborted) throw err;
      // network/http are transient; rate-limited/checkpoint/login/feedback/limit
      // are terminal — a gate must NOT be retried in a loop (useless + deepens
      // it); the caller's 5-min auto-retry alarm handles "try again later".
      if (err.code === 'network' || err.code === 'http') {
        retries += 1;
        if (retries > MAX_RETRIES) throw err;
        await sleep(backoffFor(err.code, retries));
        continue;
      }
      throw err;
    }
  }
  if (!completed) {
    // >100k users or a looping cursor — completing short would diff against
    // the previous full snapshot and fire mass fake unfollows. Fail with
    // checkpoints intact; the next attempt resumes instead of re-fetching.
    throw new IgApiError('limit', `A lista passou de ${MAX_PAGES * PAGE_SIZE} contas — a sincronização não terminou (o progresso foi guardado; tente de novo).`);
  }
  return out;
}

/**
 * Merge persisted page-checkpoints into per-kind resume descriptors.
 * Pure (testable): meta/values come from chrome.storage, but the merge logic
 * lives here. Only the CONTIGUOUS prefix of pages is used — a gap means a
 * page was never persisted, and resuming past it would skip users.
 *
 * meta   { keys: [partKey, ...] }        — index of persisted pages
 * values { [partKey]: { maxId, at, users } } — the pages themselves
 * Returns { following: {maxId, users, nextSeq}|null, followers: ... }.
 * nextSeq = number of persisted pages — the next run MUST keep numbering
 * pages from there (a restarted page counter would overwrite old checkpoints
 * and a later resume would assemble an INCOMPLETE list).
 */
export function buildResume(uid, meta, values) {
  const out = { following: null, followers: null };
  if (!meta || !Array.isArray(meta.keys)) return out;
  const byKind = { following: {}, followers: {} };
  for (const k of meta.keys) {
    const parts = k.split('.');
    if (parts[1] !== 'resume') continue; // pre-v2 namespace (torn checkpoints) — never resume
    const kind = parts[2];
    const pk = parts[3];
    if (!byKind[kind] || pk !== String(uid)) continue; // unknown kind / other account
    const v = values && values[k];
    if (!v || !Array.isArray(v.users) || !v.maxId) continue;
    if (Date.now() - v.at > RESUME_TTL_MS) continue;   // stale checkpoint
    byKind[kind][Number(parts[4])] = v;
  }
  for (const kind of ['following', 'followers']) {
    const seqs = Object.keys(byKind[kind]).map(Number).sort((a, b) => a - b);
    const users = [];
    let maxId = null;
    let prev = -1;
    let nextSeq = 0;
    for (const s of seqs) {
      if (s !== prev + 1) break; // contiguous prefix only — never skip pages
      users.push(...byKind[kind][s].users);
      maxId = byKind[kind][s].maxId;
      prev = s;
      nextSeq = s + 1;
    }
    out[kind] = users.length ? { maxId, users, nextSeq } : null;
  }
  return out;
}
