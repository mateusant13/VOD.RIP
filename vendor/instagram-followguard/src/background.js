// IG FollowGuard — service worker.
// Owns: full followers/following sync (complete lists), not-following-back
// diffing, unfollow detection vs the previous snapshot, notifications,
// periodic alarms. Reads ONLY follow/follower relationships.
'use strict';

import { readSession, fetchAllUsers, IgApiError } from './ig_api.mjs';
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
// Sync: fetch the COMPLETE following + followers lists, diff, notify.
// ---------------------------------------------------------------------------

let runningSync = null;

async function sync(trigger) {
  if (runningSync) return runningSync;
  runningSync = (async () => {
    const t0 = Date.now();
    await setState({ status: 'syncing', trigger: trigger || 'manual', error: null });
    const session = await readSession().catch((err) => err);
    if (session instanceof IgApiError) {
      await setState({ status: 'error', error: session.message, trigger: trigger || 'manual' });
      runningSync = null;
      return { ok: false, error: session.message };
    }
    const settings = await getSettings();
    const st0 = await getState();
    let uid = session.uid;
    let username = st0.ownUsername || null; // runtime-resolved, never hardcoded
    try {
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

      const following = await fetchAllUsers('following', uid, session, { onProgress: () => {} });
      const followers = await fetchAllUsers('followers', uid, session, { onProgress: () => {} });

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

      // --- notifications for people we still follow who stopped following us ---
      const notifyable = events.filter((e) => e.stillFollowing);
      if (notifyable.length && settings.notificationsEnabled) {
        await notifyUnfollows(notifyable);
      }

      const notFollowingBack = [...following.keys()].filter((u) => !followers.has(u));
      await setState({
        status: 'ok',
        lastSyncAt: nowIso(),
        lastDurationMs: Date.now() - t0,
        error: null,
        followersCount: followers.size,
        followingCount: following.size,
        notFollowingBackCount: notFollowingBack.length,
        incomplete: false,
      });
      runningSync = null;
      return { ok: true, following: following.size, followers: followers.size, notFollowingBack: notFollowingBack.length, newEvents: events.length };
    } catch (err) {
      const msg = err instanceof IgApiError ? err.message : String(err && err.message || err).slice(0, 200);
      await setState({ status: 'error', error: msg });
      runningSync = null;
      return { ok: false, error: msg };
    }
  })();
  return runningSync;
}

/** Resolve own user id + username (uid from ds_user_id cookie, else by username). */
async function resolveOwnUser(username, session, knownUid) {
  if (knownUid) {
    // /api/v1/users/{pk}/info/ echoes the profile (no username needed).
    const res = await fetch(`https://www.instagram.com/api/v1/users/${knownUid}/info/`, {
      method: 'GET',
      headers: {
        'accept': 'application/json',
        'cookie': session.cookieHeader,
        'referer': 'https://www.instagram.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'x-csrftoken': session.csrftoken || '',
        'x-ig-app-id': '936619743392459',
      },
      credentials: 'include',
    });
    const body = await res.json();
    if (body && body.user && body.user.username) {
      return { uid: knownUid, username: body.user.username };
    }
  }
  if (username) {
    const params = new URLSearchParams({ username });
    const res = await fetch(`https://www.instagram.com/api/v1/users/web_profile_info/?${params.toString()}`, {
      method: 'GET',
      headers: {
        'accept': 'application/json',
        'cookie': session.cookieHeader,
        'referer': 'https://www.instagram.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'x-ig-app-id': '936619743392459',
      },
      credentials: 'include',
    });
    const body = await res.json();
    if (body && body.data && body.data.user) {
      return { uid: body.data.user.id, username: body.data.user.username };
    }
    throw new IgApiError('http', 'Não consegui resolver seu usuário no Instagram.');
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

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === SYNC_ALARM) sync('alarm');
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
        if (uid) {
          username = (await resolveOwnUser(null, session, uid)).username;
        } else {
          throw new IgApiError('not-logged-in', 'Não encontrei seu ID de usuário. Abra instagram.com logado.');
        }
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
