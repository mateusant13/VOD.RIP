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
const PAGE_DELAY_MS = 500;          // pacing between pages (checkpoint safety)
const MAX_PAGES = 500;              // hard cap per list (500*200 = 100k users)
const MAX_RETRIES = 3;              // consecutive transient failures

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
 * kind: 'followers' | 'following'. Returns { users, nextMaxId, incomplete? }.
 */
async function fetchPage(kind, uid, maxId, session, signal) {
  const params = new URLSearchParams({ count: String(PAGE_SIZE), search_surface: 'follow_list_page' });
  if (maxId) params.set('max_id', maxId);
  const path = `/api/v1/friendships/${uid}/${kind}/?${params.toString()}`;
  const body = await apiFetch(path, session, { signal });
  const users = Array.isArray(body.users) ? body.users : [];
  const next = body.next_max_id;
  const hasMore = !!(next && body.big_list !== false);
  return { users, nextMaxId: hasMore ? next : null };
}

/**
 * Fetch the COMPLETE list (all pages) — no account may be missing.
 * Returns Map<username, meta>. Throws IgApiError on terminal failure.
 */
export async function fetchAllUsers(kind, uid, session, { signal, onProgress } = {}) {
  const out = new Map();
  let maxId = null;
  let retries = 0;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    try {
      const { users, nextMaxId } = await fetchPage(kind, uid, maxId, session, signal);
      retries = 0;
      for (const u of users) {
        if (!u || !u.username) continue;
        out.set(u.username, {
          pk: String(u.pk || ''),
          username: u.username,
          full_name: u.full_name || '',
          is_private: !!u.is_private,
          is_verified: !!u.is_verified,
          profile_pic_url: u.profile_pic_url || '',
        });
      }
      if (onProgress) onProgress(out.size);
      if (!nextMaxId) break;
      maxId = nextMaxId;
      await sleep(PAGE_DELAY_MS);
    } catch (err) {
      if (signal && signal.aborted) throw err;
      if (err.code === 'rate-limited' || err.code === 'network') {
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
