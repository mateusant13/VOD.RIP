// IG FollowGuard — auto-continue across MAX_PAGES segments.
'use strict';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { IgApiError } from './ig_api.mjs';

const store = {};
globalThis.chrome = {
  alarms: { clear: async () => {}, create: async () => {}, onAlarm: { addListener() {} } },
  storage: {
    onChanged: { addListener() {} },
    local: {
      get: async (keys) => {
        if (keys == null) return { ...store };
        if (typeof keys === 'string') return { [keys]: store[keys] };
        const o = {};
        for (const k of keys) if (k in store) o[k] = store[k];
        return o;
      },
      set: async (obj) => { Object.assign(store, obj); },
      remove: async (keys) => {
        for (const k of (Array.isArray(keys) ? keys : [keys])) delete store[k];
      },
    },
  },
  runtime: {
    getURL: (p) => `chrome-extension://test/${p}`,
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: { addListener() {} },
  },
  tabs: { query: async () => [], create: async () => {}, sendMessage: async () => ({ pong: true }), remove: async () => {}, get: async () => { throw new Error('gone'); }, update: async () => {} },
  notifications: { create: async () => {}, clear: async () => {}, onClicked: { addListener() {} } },
};

const { fetchListComplete, __setSegmentPauseMsForTests } = await import('./background.js');

test('fetchListComplete resumes after limit without user action', async () => {
  __setSegmentPauseMsForTests(() => 0);
  let calls = 0;
  const fetchFn = async () => {
    calls += 1;
    if (calls === 1) throw new IgApiError('limit', 'cap');
    return new Map([['alice', { username: 'alice', pk: '1' }]]);
  };
  const readPartialsFn = async () => ({ followers: { users: [{ username: 'bob', pk: '2' }], maxId: 'x', nextSeq: 1 }, following: null });
  const listOpts = (kind, resume) => ({ resume });
  const out = await fetchListComplete('followers', '99', {}, listOpts, readPartialsFn, { fetchFn });
  assert.equal(calls, 2);
  assert.equal(out.get('alice').username, 'alice');
});

test('fetchListComplete propagates non-limit errors', async () => {
  __setSegmentPauseMsForTests(() => 0);
  const fetchFn = async () => { throw new IgApiError('rate-limited', 'wait'); };
  await assert.rejects(
    () => fetchListComplete('followers', '1', {}, () => ({}), async () => ({ followers: null }), { fetchFn }),
    (err) => err instanceof IgApiError && err.code === 'rate-limited',
  );
});

test('fetchListComplete does not loop on repeated cursor limit', async () => {
  __setSegmentPauseMsForTests(() => 0);
  let calls = 0;
  const fetchFn = async () => {
    calls += 1;
    throw new IgApiError('limit', 'cursor repeat');
  };
  const readPartialsFn = async () => ({
    followers: { users: [{ username: 'bob', pk: '2' }], maxId: 'x', nextSeq: 1 },
    following: null,
  });
  await assert.rejects(
    () => fetchListComplete('followers', '99', {}, (k, r) => ({ resume: r }), readPartialsFn, { fetchFn }),
    (err) => err instanceof IgApiError && err.code === 'limit',
  );
  assert.equal(calls, 2, 'must stop after one no-progress limit');
});
