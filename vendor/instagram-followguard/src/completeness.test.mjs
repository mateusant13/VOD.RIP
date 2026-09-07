// IG FollowGuard — completeness-oracle + transport-classification regressions.
// Root-cause suite for the "só 20 pessoas que não me seguiam" bug: a truncated
// follow list must NEVER be accepted, persisted, or diffed as complete.
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  fetchAllUsers, apiFetch, IgApiError,
  __setRetryBaseMsForTests, __setFetchTimeoutMsForTests, __setPageDelayMsForTests, __setTransport,
} from './ig_api.mjs';
import { mergeEvents } from './diff.mjs';

__setRetryBaseMsForTests(1);
__setFetchTimeoutMsForTests(5);
__setPageDelayMsForTests(0);

const UID = '123';
const SESSION = { cookieHeader: 'a=1; b=2', csrftoken: 'tok' };
const u = (n) => ({ username: 'user' + n, pk: String(n) });
const many = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => u(from + i));

// Page-transport stub: queue of {status?, body} payloads (status defaults 200).
function stubTransport(payloads) {
  const queue = payloads.slice();
  __setTransport(() => {
    const p = queue.shift() ?? { body: {} };
    return Promise.resolve({ status: p.status ?? 200, text: JSON.stringify(p.body) });
  });
  return queue;
}

const withTransport = async (fn) => {
  try { return await fn(); } finally { __setTransport(null); }
};

// --- (a) the oracle: IG says "done", declared count says otherwise ---

test('fetchAllUsers: short list with expectedCount rejects as incomplete', () => withTransport(async () => {
  stubTransport([{ body: { status: 'ok', users: many(1, 20) } }]); // gate page: 20 users, no cursor
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, { expectedCount: 1048 }),
    (err) => err instanceof IgApiError && err.code === 'incomplete',
    'a 20-of-1048 walk must never complete',
  );
}));

test('fetchAllUsers: count within slack completes', () => withTransport(async () => {
  // 1048 declared, 1030 fetched (slack = max(30, 53) = 53 → floor 995).
  stubTransport([{ body: { status: 'ok', users: many(1, 1030) } }]);
  const out = await fetchAllUsers('following', UID, SESSION, { expectedCount: 1048 });
  assert.equal(out.size, 1030);
}));

test('fetchAllUsers: full list matching expectedCount completes', () => withTransport(async () => {
  stubTransport([
    { body: { status: 'ok', users: many(1, 24), next_max_id: 'm1' } },
    { body: { status: 'ok', users: many(25, 40) } },
  ]);
  const out = await fetchAllUsers('following', UID, SESSION, { expectedCount: 40 });
  assert.equal(out.size, 40);
}));

// --- (b) big_list:false must not discard a live cursor ---

test('fetchAllUsers: big_list:false with next_max_id continues the walk', () => withTransport(async () => {
  stubTransport([
    { body: { status: 'ok', users: many(1, 24), next_max_id: 'm1', big_list: false } },
    { body: { status: 'ok', users: many(25, 48) } },
  ]);
  const out = await fetchAllUsers('following', UID, SESSION, { expectedCount: 48 });
  assert.equal(out.size, 48, 'cursor after big_list:false must be followed');
}));

// --- (c/d) resume + empty tail ---

test('fetchAllUsers: resumed walk ending short of expectedCount rejects', () => withTransport(async () => {
  stubTransport([{ body: { status: 'ok', users: [] } }]); // legit empty end-of-list
  const resume = { maxId: 'm0', nextSeq: 1, users: many(1, 20) };
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, { resume, expectedCount: 1048 }),
    (err) => err instanceof IgApiError && err.code === 'incomplete',
    'expired resume cursor + empty tail must not accept the prefix',
  );
}));

test('fetchAllUsers: same shape WITHOUT expectedCount still completes (legacy path)', () => withTransport(async () => {
  stubTransport([{ body: { status: 'ok', users: [] } }]);
  const resume = { maxId: 'm0', nextSeq: 1, users: [u(1)] };
  const out = await fetchAllUsers('following', UID, SESSION, { resume });
  assert.deepEqual([...out.keys()], ['user1']);
}));

// --- transport classification: an active gate must not be retried in-loop ---

test('apiFetch: 400 with rate_limit_error body classifies as rate-limited', () => withTransport(async () => {
  stubTransport([{ status: 400, body: { message: 'rate limit exceeded', error_type: 'rate_limit_error' } }]);
  await assert.rejects(
    apiFetch('/api/v1/friendships/123/following/', SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'rate-limited',
    'a 400 carrying the gate error_type must be terminal (no in-loop retry storm)',
  );
}));

test('apiFetch: plain 500 stays transient http', () => withTransport(async () => {
  stubTransport([{ status: 500, body: { message: 'server error' } }]);
  await assert.rejects(
    apiFetch('/api/v1/friendships/123/following/', SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'http',
  );
}));

test('apiFetch: 400 with feedback_title-only body classifies as feedback-required', () => withTransport(async () => {
  // Real gate shape: 400 whose only signal is feedback_title — must NOT fall
  // through to transient 'http' (the in-loop ladder would hammer the gate).
  stubTransport([{ status: 400, body: { feedback_title: 'Sua conta foi temporariamente limitada.' } }]);
  await assert.rejects(
    apiFetch('/api/v1/friendships/123/following/', SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'feedback-required',
  );
}));

test('apiFetch: 400 bare status:fail body classifies as rate-limited', () => withTransport(async () => {
  // Mirrors the 200-path bare-fail rule.
  stubTransport([{ status: 400, body: { status: 'fail' } }]);
  await assert.rejects(
    apiFetch('/api/v1/friendships/123/following/', SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'rate-limited',
  );
}));

// --- (f) mergeEvents: no caller mutation, dedupe newest-first ---

test('mergeEvents does not mutate the caller array', () => {
  const fresh = [{ username: 'a', detectedAt: 2 }, { username: 'b', detectedAt: 1 }];
  const snapshot = fresh.map((e) => ({ ...e }));
  mergeEvents(fresh, [], 100);
  assert.deepEqual(fresh, snapshot, 'input order preserved after merge');
});

test('mergeEvents dedupes by username keeping the newest', () => {
  const fresh = [{ username: 'a', detectedAt: 500 }];
  const stored = [{ username: 'a', detectedAt: 100 }, { username: 'b', detectedAt: 90 }];
  const merged = mergeEvents(fresh, stored, 100);
  assert.equal(merged.length, 2);
  assert.equal(merged.find((e) => e.username === 'a').detectedAt, 500);
});

// --- (e) fetchListComplete: 'incomplete' propagates without segment-pausing ---

globalThis.chrome = {
  alarms: { clear: async () => {}, create: async () => {}, onAlarm: { addListener() {} } },
  storage: { local: { get: async () => ({}), set: async () => {}, remove: async () => {} } },
  runtime: {
    id: 'test',
    getURL: (p) => `chrome-extension://test/${p}`,
    getManifest: () => ({ version: '0.6.1' }),
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: { addListener() {} },
  },
  notifications: { create: async () => {}, clear: async () => {}, onClicked: { addListener() {} } },
  tabs: { create: async () => {}, query: async () => [], sendMessage: async () => ({ pong: true }), remove: async () => {}, get: async () => { throw new Error('gone'); } },
};

const { fetchListComplete, resolveOwnUser } = await import('./background.js');

test('fetchListComplete: incomplete rejects immediately (auto-retry owns resume)', async () => {
  let segmentAlarms = 0;
  const readPartials = async () => ({ following: null, followers: null });
  const fetchFn = async () => { throw new IgApiError('incomplete', 'short'); };
  const origCreate = chrome.alarms.create;
  chrome.alarms.create = async (name) => { if (name === 'igf-sync-continue') segmentAlarms += 1; };
  try {
    await assert.rejects(
      fetchListComplete('following', UID, SESSION, () => ({}), readPartials, { fetchFn }),
      (err) => err.code === 'incomplete',
    );
  } finally {
    chrome.alarms.create = origCreate;
  }
  assert.equal(segmentAlarms, 0, 'incomplete must not trigger a segment pause');
});

// --- resolveOwnUser: the oracle's count fields must survive a partial info body ---

test('resolveOwnUser: info without follower_count falls back to web_profile_info', async () => {
  await withTransport(async () => {
    __setTransport((path) => {
      if (path.startsWith('/api/v1/users/123/info/')) {
        return Promise.resolve({ status: 200, text: JSON.stringify({ status: 'ok', user: { username: 'x', following_count: 1048 } }) });
      }
      if (path.startsWith('/api/v1/users/web_profile_info/')) {
        return Promise.resolve({ status: 200, text: JSON.stringify({ status: 'ok', data: { user: { username: 'x', edge_followed_by: { count: 77 }, edge_follow: { count: 1048 } } } }) });
      }
      return Promise.resolve({ status: 404, text: '{}' });
    });
    const res = await resolveOwnUser('x', SESSION, '123');
    assert.deepEqual(
      { uid: res.uid, followerCount: res.followerCount, followingCount: res.followingCount },
      { uid: '123', followerCount: 77, followingCount: 1048 },
    );
  });
});
