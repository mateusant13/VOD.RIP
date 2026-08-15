// IG FollowGuard — service worker.
// Owns: full followers/following sync (complete lists), not-following-back
// diffing, unfollow detection vs the previous snapshot, notifications,
// periodic alarms. Reads ONLY follow/follower relationships.
'use strict';

import { readSession, fetchAllUsers, IgApiError, buildResume, apiFetch, transientRetry, __setTransport } from './ig_api.mjs';
import { diffAndRecord, mergeEvents } from './diff.mjs';

const K = {
  settings: 'igf.settings',
  state: 'igf.state',
  followers: 'igf.followers',
  following: 'igf.following',
  prevFollowers: 'igf.prevFollowers',
  history: 'igf.followHistory',
  events: 'igf.unfollowEvents',
};
const EVENTS_MAX = 100;
const SYNC_ALARM = 'igf-sync';
const DEFAULT_SETTINGS = {
  // No username here: the logged-in profile is resolved at runtime from the
  // session cookie (ds_user_id -> /api/v1/users/{pk}/info/). Never hardcode.
  refreshMinutes: 60,
  notificationsEnabled: true,
  autoSync: true,
};

const emptyState = () => ({
  status: 'idle', // idle | syncing | ok | error
  trigger: null,
  lastSyncAt: null,
  lastDurationMs: null,
  error: null,
  ownUsername: null,
  ownUserId: null,
  followersCount: 0,
  followingCount: 0,
  notFollowingBackCount: 0,
  incomplete: false, // true when a list was truncated by the page cap
});

async function getSettings() {
  const o = await chrome.storage.local.get(K.settings);
  return { ...DEFAULT_SETTINGS, ...(o[K.settings] || {}) };
}

async function saveSettings(s) {
  const clean = { ...s };
  delete clean.username; // legacy hardcoded field — never persist/use it
  await chrome.storage.local.set({ [K.settings]: clean });
}

async function getState() {
  const o = await chrome.storage.local.get(K.state);
  return { ...emptyState(), ...(o[K.state] || {}) };
}

async function setState(patch) {
  const o = await chrome.storage.local.get(K.state);
  await chrome.storage.local.set({ [K.state]: { ...emptyState(), ...(o[K.state] || {}), ...patch } });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function nowIso() {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Sync checkpoints: persist per-page progress so a failed/restarted sync
// RESUMES from the last page instead of re-fetching everything from scratch
// (the "fez tudo do scratch" complaint). Keyed by uid — never resumes across
// accounts; TTL-guarded; only the contiguous page prefix is ever resumed.
// ---------------------------------------------------------------------------

// 'igf.resume.' namespace (v2): the pre-fix build wrote `seq: page` — a
// restarted loop counter clobbered old checkpoints, so storage can hold
// contiguous labels with TORN content. Bumping the prefix invalidates any
// such partials on upgrade (a torn checkpoint would resume an incomplete
// list). Old keys are never read again (harmless orphans).
const PART_META = 'igf.resume.meta';
const PART_PREFIX = 'igf.resume.';
const partKey = (kind, uid, seq) => `${PART_PREFIX}${kind}.${uid}.${seq}`;

// Serializes the PART_META read-modify-write across the two parallel sync
// branches — without it, a lost key in the index truncates the resume prefix
// and the unindexed value key is never cleaned by clearPartials.
let metaLock = Promise.resolve();

// Best-effort: a failing checkpoint write only costs resume granularity.
function savePagePart(kind, uid, seq, maxId, users) {
  const key = partKey(kind, uid, seq);
  metaLock = metaLock.then(async () => {
    await chrome.storage.local.set({ [key]: { maxId, at: Date.now(), users } });
    const o = await chrome.storage.local.get(PART_META);
    const meta = o[PART_META] || { keys: [] };
    if (!meta.keys.includes(key)) meta.keys.push(key);
    await chrome.storage.local.set({ [PART_META]: meta });
  });
  return metaLock;
}

async function readPartials(uid) {
  try {
    const o = await chrome.storage.local.get(PART_META);
    const meta = o[PART_META];
    if (!meta || !meta.keys.length) return { following: null, followers: null };
    const values = await chrome.storage.local.get(meta.keys);
    return buildResume(uid, meta, values);
  } catch {
    return { following: null, followers: null };
  }
}

async function clearPartials() {
  try {
    const o = await chrome.storage.local.get(PART_META);
    const meta = o[PART_META];
    if (meta && meta.keys.length) await chrome.storage.local.remove([...meta.keys, PART_META]);
  } catch { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Sync: fetch the COMPLETE following + followers lists, diff, notify.
// ---------------------------------------------------------------------------

let runningSync = null;

// --- Page-context transport ---
// The sync's HTTP requests run on an instagram.com tab via content_proxy.js:
// same-origin fetch with the browser's real headers and native cookies —
// byte-indistinguishable from the web app's own requests (see
// content_proxy.js). One tab is used for the whole sync; a tab WE opened is
// closed when the sync ends ("fecha a aba q tu abriu"), the user's own tab
// is never touched.
//
// Tabs opened BEFORE an extension reload have no content_proxy listener
// (content scripts inject only on load) — pinging selects only tabs that can
// actually serve, and the transport self-heals mid-sync (a closed/navigated
// tab re-ensures a fresh one instead of retrying into a dead listener).
let syncTabId = null;   // tab the active sync is using
let openedTabId = null; // tab we created for the sync (must be closed after)

async function pingProxy(tabId) {
  try {
    const r = await chrome.tabs.sendMessage(tabId, { igf: 'ping' });
    return !!(r && r.pong);
  } catch {
    return false;
  }
}

async function waitForProxy(tabId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await pingProxy(tabId)) return true;
    await sleep(400);
  }
  return false;
}

async function ensureIgTab() {
  // Prefer an existing IG tab whose content script ANSWERS — a pre-reload
  // tab (no listener) is skipped, never navigated.
  const tabs = await chrome.tabs.query({ url: 'https://www.instagram.com/*' });
  for (const t of tabs) {
    if (await pingProxy(t.id)) {
      syncTabId = t.id;
      return;
    }
  }
  // Reuse our own still-open tab, else open a fresh one (content script is
  // guaranteed after the page loads; ping until it answers).
  if (openedTabId != null) {
    try {
      await chrome.tabs.get(openedTabId);
      syncTabId = openedTabId;
      return;
    } catch { openedTabId = null; }
  }
  const t = await chrome.tabs.create({ url: 'https://www.instagram.com/', active: false });
  openedTabId = t.id;
  syncTabId = t.id;
  await waitForProxy(t.id, 10000);
}

function releaseIgTab() {
  if (openedTabId != null) {
    chrome.tabs.remove(openedTabId).catch(() => {});
    openedTabId = null;
  }
  syncTabId = null;
}

// Runs fn with the page-context transport installed: an IG tab is ensured
// (opened only when none exists AND the user asked — never on a timer), every
// request inside fn goes through content_proxy.js, and the tab/transport are
// released after. EVERY path that talks to IG must go through this — a
// SW-originated fetch advertises sec-fetch-site: none with forged headers,
// the exact fingerprint the page transport removes.
//
// Serialized with a promise lock (same pattern as metaLock for checkpoints):
// withPageTransport installs a MODULE-GLOBAL transport, so a concurrent
// caller's teardown must never land between two of our fetches — e.g.
// igf-get-own resolving the user mid-sync used to null the transport under
// sync()'s feet (internal-error + full backoff burn). The lock makes
// install/run/teardown atomic per caller; queueing is short in practice
// (get-own only takes this path while ownUsername is unset, and the sync
// sets it during its own resolution phase).
let transportLock = Promise.resolve();
function withPageTransport(fn) {
  const run = transportLock.then(async () => {
    await ensureIgTab();
    __setTransport(pageTransport);
    try {
      return await fn();
    } finally {
      __setTransport(null);
      releaseIgTab();
    }
  });
  // Keep the chain alive even if this caller's fn rejects.
  transportLock = run.catch(() => {});
  return run;
}

async function pageTransport(path, _session, _signal) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (syncTabId == null) await ensureIgTab();
    try {
      const r = await chrome.tabs.sendMessage(syncTabId, { igf: 'fetch', path });
      if (!r || !r.ok) {
        throw new IgApiError('network', r && r.error === 'timeout'
          ? 'O Instagram não respondeu em 20s.'
          : 'Falha de rede ao falar com o Instagram.');
      }
      return { status: r.status, text: r.text };
    } catch (e) {
      // Tab lost its listener (closed/navigated mid-sync) — heal so the
      // retry hits a live tab instead of looping into the dead one.
      syncTabId = null;
      await ensureIgTab();
    }
  }
  throw new IgApiError('network', 'Falha de rede ao falar com o Instagram.');
}

const TRANSIENT_CODES = new Set(['http', 'network', 'rate-limited']);
const RETRY_ALARM = 'igf-sync-retry';
// Escalating auto-retry delays (min): a fixed 5-min retry loop for hours is a
// bot cadence; a human checks back later, then much later. retryN is
// persisted and reset on success; cap at 2h.
const RETRY_DELAYS_MIN = [5, 15, 45, 120];

// After a TRANSIENT failure (never login/checkpoint/gate/limit), schedule a
// one-shot retry. Checkpoints make the auto-resume safe — it picks up from
// the last persisted page instead of re-fetching from page 1.
function scheduleErrorRetry(code) {
  if (!TRANSIENT_CODES.has(code)) return;
  getState().then(async (st) => {
    const n = Math.min(Number(st.retryN) || 0, RETRY_DELAYS_MIN.length - 1);
    await chrome.alarms.create(RETRY_ALARM, { delayInMinutes: RETRY_DELAYS_MIN[n] });
    await setState({ retryN: n + 1 });
  }).catch(() => {});
}

async function sync(trigger) {
  if (runningSync) return runningSync;
  const run = (async () => {
    const t0 = Date.now();
    try {
      // Cancel any pending auto-retry — a fresh manual/alarm attempt supersedes it.
      await chrome.alarms.clear(RETRY_ALARM);
      await setState({ status: 'syncing', trigger: trigger || 'manual', error: null, syncProgress: null });
      return await withPageTransport(async () => {
      const session = await readSession().catch((err) => err);
      if (session instanceof IgApiError) {
        await setState({ status: 'error', error: session.message, errorCode: session.code, trigger: trigger || 'manual' });
        scheduleErrorRetry(session.code);
        return { ok: false, error: session.message };
      }
      const settings = await getSettings();
      const st0 = await getState();
      let uid = session.uid;
      let username = st0.ownUsername || null; // runtime-resolved, never hardcoded
      if (uid && !username) {
        username = (await resolveOwnUser(null, session, uid)).username;
      } else if (!uid) {
        if (!username) {
          throw new IgApiError('not-logged-in', 'Não encontrei seu ID de usuário. Abra instagram.com logado.');
        }
        const info = await resolveOwnUser(username, session);
        uid = info.uid;
        username = info.username;
      }
      if (uid) await setState({ ownUserId: String(uid) });
      if (username) await setState({ ownUsername: username });

      // Both lists in PARALLEL (a sequential full-following-then-full-followers
      // walk doubles the wall time on large accounts). Progress is persisted
      // per page so the dashboard can show live counts; each page is also
      // checkpointed so a failure/restart resumes instead of restarting.
      const partials = await readPartials(uid);
      const [following, followers] = await Promise.all([
        fetchAllUsers('following', uid, session, {
          resume: partials.following,
          onProgress: ({ kind, fetched }) => setState({ syncProgress: { phase: kind, fetched } }),
          onPart: ({ seq, maxId, users }) => savePagePart('following', uid, seq, maxId, users),
        }),
        fetchAllUsers('followers', uid, session, {
          resume: partials.followers,
          onProgress: ({ kind, fetched }) => setState({ syncProgress: { phase: kind, fetched } }),
          onPart: ({ seq, maxId, users }) => savePagePart('followers', uid, seq, maxId, users),
        }),
      ]);

      const stored = await chrome.storage.local.get([K.prevFollowers, K.history]);
      const prev = stored[K.prevFollowers] || {};
      const history = stored[K.history] || {};

      // --- persist current lists (followers first, then following) ---
      await chrome.storage.local.set({
        [K.followers]: Object.fromEntries(followers),
        [K.following]: Object.fromEntries(following),
      });

      // --- diff: who left, who arrived (pure logic) ---
      const now = Date.now();
      const { events, newHistory } = diffAndRecord(prev, followers, following, history, now);

      // --- prepend events (newest first), cap at EVENTS_MAX ---
      const storedEvents = (await chrome.storage.local.get(K.events))[K.events] || [];
      const allEvents = mergeEvents(events, storedEvents, EVENTS_MAX);

      await chrome.storage.local.set({
        [K.prevFollowers]: Object.fromEntries(followers),
        [K.history]: newHistory,
        [K.events]: allEvents,
      });
      await clearPartials(); // sync complete — no resume needed anymore

      // --- notifications for people we still follow who stopped following us ---
      const notifyable = events.filter((e) => e.stillFollowing);
      if (notifyable.length && settings.notificationsEnabled) {
        try {
          await notifyUnfollows(notifyable);
        } catch {
          // Notifications are best-effort — a failed create must not mark a
          // COMPLETED sync as errored.
        }
      }

      const notFollowingBack = [...following.keys()].filter((u) => !followers.has(u));
      await setState({
        status: 'ok',
        lastSyncAt: nowIso(),
        lastDurationMs: Date.now() - t0,
        error: null,
        errorCode: null,
        syncProgress: null,
        followersCount: followers.size,
        followingCount: following.size,
        notFollowingBackCount: notFollowingBack.length,
        incomplete: false,
        retryN: 0,
      });
      return { ok: true, following: following.size, followers: followers.size, notFollowingBack: notFollowingBack.length, newEvents: events.length };
      });
    } catch (err) {
      const msg = err instanceof IgApiError ? err.message : String(err && err.message || err).slice(0, 200);
      const code = err instanceof IgApiError ? err.code : null;
      await setState({ status: 'error', error: msg, syncProgress: null, errorCode: code });
      scheduleErrorRetry(code); // transient only — login/checkpoint/gate never auto-retry
      return { ok: false, error: msg };
    } finally {
      runningSync = null;
    }
  })();
  runningSync = run;
  return runningSync;
}

/** Resolve own user id + username (uid from ds_user_id cookie, else by username). */
async function resolveOwnUser(username, session, knownUid) {
  if (knownUid) {
    // /api/v1/users/{pk}/info/ echoes the profile (no username needed).
    // Classified + retried: a transient blip on the sync's FIRST step used to
    // kill the whole sync with zero retries (raw fetch, no classification).
    const body = await transientRetry(() => apiFetch(`/api/v1/users/${knownUid}/info/`, session));
    if (body && body.user && body.user.username) {
      return { uid: knownUid, username: body.user.username };
    }
    throw new IgApiError('http', 'Instagram respondeu com uma resposta inesperada.');
  }
  if (username) {
    const params = new URLSearchParams({ username });
    const body = await transientRetry(() => apiFetch(`/api/v1/users/web_profile_info/?${params.toString()}`, session));
    if (body && body.data && body.data.user && body.data.user.username) {
      return { uid: String(body.data.user.id), username: body.data.user.username };
    }
    throw new IgApiError('http', 'Instagram respondeu com uma resposta inesperada.');
  }
  throw new IgApiError('not-logged-in', 'Não encontrei seu ID de usuário. Abra instagram.com logado.');
}

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

export async function notifyUnfollows(events) {
  const N = events.length;
  if (N <= 5) {
    for (const e of events) {
      await chrome.notifications.create(`igf-uf-${e.username}-${e.detectedAt}`, {
        type: 'basic',
        iconUrl: chrome.runtime.getURL('images/icon128.png'),
        title: `${e.username} deixou de te seguir`,
        message: e.fullName ? `${e.fullName} — você ainda segue esta conta.` : 'Você ainda segue esta conta.',
        priority: 1,
      });
    }
    return;
  }
  await chrome.notifications.create(`igf-uf-summary-${Date.now()}`, {
    type: 'basic',
    iconUrl: chrome.runtime.getURL('images/icon128.png'),
    title: `${N} pessoas deixaram de te seguir`,
    message: `Abre o IG FollowGuard para ver as últimas ${Math.min(N, EVENTS_MAX)}.`,
    priority: 1,
  });
}

// ---------------------------------------------------------------------------
// Alarms + lifecycle
// ---------------------------------------------------------------------------

async function scheduleAlarm() {
  const s = await getSettings();
  await chrome.alarms.clear(SYNC_ALARM);
  if (s.autoSync) {
    await chrome.alarms.create(SYNC_ALARM, { periodInMinutes: Math.max(1, Number(s.refreshMinutes) || 60) });
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== SYNC_ALARM && alarm.name !== RETRY_ALARM) return;
  // Background syncs run only while an IG tab exists — the requests must
  // LOOK like page requests (see pageTransport). No IG tab open = nobody is
  // on Instagram = skip; the next panel open / alarm with a tab handles it.
  try {
    const tabs = await chrome.tabs.query({ url: 'https://www.instagram.com/*' });
    if (!tabs.length) return;
  } catch {
    return;
  }
  sync(alarm.name === SYNC_ALARM ? 'alarm' : 'retry');
});

chrome.runtime.onInstalled.addListener(async (details) => {
  await scheduleAlarm();
  if (details.reason === 'install') {
    await sync('install');
  }
});

chrome.runtime.onStartup.addListener(async () => {
  await scheduleAlarm();
  const s = await getSettings();
  if (s.autoSync) sync('startup');
});

// ---------------------------------------------------------------------------
// Messages (dashboard popup / panel -> background)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || !msg.type || !msg.type.startsWith('igf-')) return false;
  if (msg.type === 'igf-sync') {
    sync(msg.trigger || 'manual').then(sendResponse);
    return true;
  }
  if (msg.type === 'igf-get-own') {
    // Logged-in profile, resolved from the session at runtime (never hardcoded).
    (async () => {
      const st = await getState();
      if (st.ownUsername) {
        sendResponse({ ok: true, username: st.ownUsername, uid: st.ownUserId ? String(st.ownUserId) : null });
        return;
      }
      const session = await readSession().catch((err) => err);
      if (session instanceof IgApiError) {
        sendResponse({ ok: false, error: session.message });
        return;
      }
      let uid = session.uid;
      let username = null;
      try {
        // Same page-context transport as sync() — the user-resolution request
        // must NOT be a SW fetch (fingerprint). The FAB/panel on an IG page
        // reuses the user's own tab; nothing is closed.
        username = await withPageTransport(async () => {
          if (uid) {
            return (await resolveOwnUser(null, session, uid)).username;
          }
          throw new IgApiError('not-logged-in', 'Não encontrei seu ID de usuário. Abra instagram.com logado.');
        });
      } catch (err) {
        sendResponse({
          ok: false,
          error: err instanceof IgApiError ? err.message : String(err && err.message || err),
        });
        return;
      }
      if (uid) await setState({ ownUserId: String(uid) });
      if (username) await setState({ ownUsername: username });
      sendResponse({ ok: !!(uid && username), username: username || null, uid: uid ? String(uid) : null });
    })();
    return true;
  }
  if (msg.type === 'igf-settings-update') {
    (async () => {
      const s = await getSettings();
      const next = { ...s, ...(msg.settings || {}) };
      await saveSettings(next);
      await scheduleAlarm();
      sendResponse({ ok: true, settings: next });
    })();
    return true;
  }
  if (msg.type === 'igf-reset-history') {
    (async () => {
      await chrome.storage.local.remove([K.prevFollowers, K.history, K.events]);
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (msg.type === 'igf-open-profile') {
    chrome.tabs.create({ url: `https://www.instagram.com/${encodeURIComponent(msg.username)}/` });
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

chrome.notifications.onClicked.addListener((id) => {
  const m = id.match(/^igf-uf-(.+?)-\d+$/);
  if (m) {
    chrome.tabs.create({ url: `https://www.instagram.com/${encodeURIComponent(m[1])}/` });
    chrome.notifications.clear(id);
  }
});

// Initial alarm (SW may restart without onInstalled).
scheduleAlarm().catch(() => {});

// A SW restart mid-sync would leave status='syncing' forever (the UI shows an
// endless spinner). On every SW start, clear any stale syncing state — a live
// sync can't coexist with a restart, so this is always safe.
(async () => {
  try {
    const st = await getState();
    if (st.status === 'syncing') {
      await setState({ status: 'idle', syncProgress: null });
    }
  } catch { /* storage unavailable — next sync overwrites anyway */ }
})();

// One-time cleanup: drop pre-v2 checkpoint keys ('igf.part.*' — torn-content
// hazard written by the build that restarted the page counter at 0). The new
// namespace is 'igf.resume.'; old keys are never read, just garbage.
chrome.storage.local.get(null).then((all) => {
  const stale = Object.keys(all).filter((k) => k.startsWith('igf.part.'));
  if (stale.length) chrome.storage.local.remove(stale);
}).catch(() => {});
