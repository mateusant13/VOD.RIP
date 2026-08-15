// IG FollowGuard — Instagram web API client (followers/following).
// Talks to the SAME internal API the logged-in web app uses, with the
// session cookies of the user's real profile (chrome.cookies). Reads only
// follow/follower relationships — never DMs, never posts.
'use strict';

export const IG_WEB_APP_ID = '936619743392459'; // www.instagram.com web client app id
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const PAGE_SIZE = 200;              // max users per page the web API accepts
const PAGE_DELAY_MS = 250;          // pacing between pages (checkpoint safety)
const MAX_PAGES = 500;              // hard cap per list (500*200 = 100k users)
const MAX_RETRIES = 3;              // consecutive transient failures
export const RESUME_TTL_MS = 60 * 60 * 1000; // discard checkpoints older than 1h

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

async function apiFetch(path, session, { signal } = {}) {
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
  const res = await fetch(`https://www.instagram.com${path}`, {
    method: 'GET',
    headers,
    credentials: 'include',
    signal,
  });
  let body = null;
  const text = await res.text();
  try {
    body = JSON.parse(text);
  } catch {
    body = null; // non-JSON (login wall / challenge HTML)
  }
  if (res.status === 429) throw new IgApiError('rate-limited', 'Instagram limitou as requisições (429).');
  if (res.status === 403) throw new IgApiError('checkpoint', 'Instagram pediu verificação (403).');
  if (!res.ok) throw new IgApiError('http', `HTTP ${res.status} em ${path.split('?')[0]}`);
  if (!body || body.status === 'fail') {
    const why = (body && (body.message || body.error_type)) || 'resposta inválida';
    if (String(why).includes('login_required')) {
      throw new IgApiError('not-logged-in', 'Sessão do Instagram expirou. Faça login e tente de novo.');
    }
    if (String(why).includes('checkpoint') || res.status === 403) {
      throw new IgApiError('checkpoint', 'Instagram pediu verificação. Abra instagram.com no navegador.');
    }
    throw new IgApiError('http', `Instagram respondeu: ${why}`);
  }
  return body;
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
  let retries = 0;
  const pick = (u) => ({
    pk: String(u.pk || ''),
    username: u.username,
    full_name: u.full_name || '',
    is_private: !!u.is_private,
    is_verified: !!u.is_verified,
    profile_pic_url: u.profile_pic_url || '',
  });
  if (resume && resume.maxId && Array.isArray(resume.users)) {
    for (const u of resume.users) {
      if (!u || !u.username) continue;
      out.set(u.username, pick(u));
    }
    maxId = resume.maxId;
  }
  for (let page = 0; page < MAX_PAGES; page += 1) {
    try {
      const { users, nextMaxId, usersPresent } = await fetchPage(kind, uid, maxId, session, signal);
      retries = 0;
      // Malformed empty response (no users array at all) on an empty partial
      // result would "complete" with zero accounts — treat it as transient.
      if (out.size === 0 && users.length === 0 && !nextMaxId && !usersPresent) {
        throw new IgApiError('http', 'Instagram respondeu com uma resposta inesperada.');
      }
      for (const u of users) {
        if (!u || !u.username) continue;
        out.set(u.username, pick(u));
      }
      if (onProgress) onProgress({ kind, fetched: out.size });
      if (onPart && users.length && nextMaxId) {
        onPart({ seq: page, maxId: nextMaxId, users: users.map(pick) }).catch(() => {});
      }
      if (!nextMaxId) break;
      maxId = nextMaxId;
      await sleep(PAGE_DELAY_MS);
    } catch (err) {
      if (signal && signal.aborted) throw err;
      // rate-limited/network/http are transient (IG occasionally serves a 200
      // with HTML or a fail body); checkpoint/login errors are terminal.
      if (err.code === 'rate-limited' || err.code === 'network' || err.code === 'http') {
        retries += 1;
        if (retries > MAX_RETRIES) throw err;
        await sleep(5000 * retries);
        continue;
      }
      throw err;
    }
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
 * Returns { following: {maxId, users}|null, followers: ... }.
 */
export function buildResume(uid, meta, values) {
  const out = { following: null, followers: null };
  if (!meta || !Array.isArray(meta.keys)) return out;
  const byKind = { following: {}, followers: {} };
  for (const k of meta.keys) {
    const parts = k.split('.');
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
    for (const s of seqs) {
      if (s !== prev + 1) break; // contiguous prefix only — never skip pages
      users.push(...byKind[kind][s].users);
      maxId = byKind[kind][s].maxId;
      prev = s;
    }
    out[kind] = users.length ? { maxId, users } : null;
  }
  return out;
}
