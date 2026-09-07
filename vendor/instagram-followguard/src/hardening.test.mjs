// IG FollowGuard — delete-all + HTML escaping tests.
'use strict';
import { test } from 'node:test';
import assert from 'node:assert/strict';

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
  notifications: { create: async () => {}, clear: async () => {}, onClicked: { addListener() {} } },
  tabs: { create: async () => {}, query: async () => [], sendMessage: async () => ({ pong: true }), remove: async () => {}, get: async () => { throw new Error('gone'); } },
};

globalThis.document = {
  getElementById: () => ({
    onclick: null,
    addEventListener() {},
    textContent: '',
    style: {},
    classList: { add() {}, remove() {}, toggle() {} },
  }),
  addEventListener() {},
  body: { classList: { contains: () => false } },
};
globalThis.parent = { postMessage() {} };

const { deleteAllData, manualSyncCooldownInfo, manualSyncCooldownMs, recordManualUnfollowMaps, restoreListsAfterFailedWalk } = await import('./background.js');

test('delete-all wipes every igf.* key incl. resume.* and resets defaults', async () => {
  Object.assign(store, {
    'igf.settings': { refreshMinutes: 30, notificationsEnabled: false, autoSync: false, consentAt: 123 },
    'igf.state': { status: 'ok', ownUsername: 'x' },
    'igf.followers': { a: 1 },
    'igf.following': { b: 1 },
    'igf.prevFollowers': { c: 1 },
    'igf.followHistory': { d: 1 },
    'igf.unfollowEvents': [{ username: 'z' }],
    'igf.snapshotUid': '99',
    'igf.resume.meta': { keys: ['igf.resume.followers.1.0'] },
    'igf.resume.followers.1.0': { users: [] },
    'other.key': 'keep',
  });
  await deleteAllData();
  const keys = Object.keys(store).filter((k) => k.startsWith('igf.'));
  assert.deepEqual(keys.sort(), ['igf.settings', 'igf.state']);
  assert.equal(store['igf.settings'].consentAt, null);
  assert.equal(store['igf.settings'].refreshMinutes, 60);
  assert.equal(store['igf.state'].status, 'idle');
  assert.equal(store['other.key'], 'keep');
});

globalThis.__IGF_SKIP_UI_BOOT__ = true;
const { itemHtml } = await import('./dashboard.js');

test('manual sync cooldown scales with follower count', () => {
  const small = manualSyncCooldownMs(200, 100);
  const big = manualSyncCooldownMs(20000, 15000);
  assert.equal(small, 5 * 60 * 1000);
  assert.ok(big > small);
  const last = new Date('2026-01-01T12:00:00.000Z').toISOString();
  const now = new Date('2026-01-01T12:04:00.000Z').getTime();
  const cd = manualSyncCooldownInfo(last, { followersCount: 200, followingCount: 100 }, now);
  assert.equal(cd.blocked, true);
  assert.equal(cd.waitMinutes, 1);
  const ok = manualSyncCooldownInfo(last, { followersCount: 200, followingCount: 100 }, now + small);
  assert.equal(ok.blocked, false);
});

test('free manual refresh bypasses cooldown', () => {
  const last = new Date('2026-01-01T12:00:00.000Z').toISOString();
  const now = new Date('2026-01-01T12:01:00.000Z').getTime();
  const cd = manualSyncCooldownInfo(last, { followersCount: 5000, followingCount: 5000, freeRefreshPending: true }, now);
  assert.equal(cd.blocked, false);
  assert.equal(cd.freeRefresh, true);
});

test('recordManualUnfollowMaps removes user and updates counts', () => {
  const following = { alice: { pk: '1', username: 'alice' }, bob: { pk: '2', username: 'bob' } };
  const followers = { carol: { pk: '3', username: 'carol' } };
  const patch = recordManualUnfollowMaps(following, followers, { pk: '2' });
  assert.ok(patch);
  assert.equal(patch.removedUsername, 'bob');
  assert.equal(patch.followingCount, 1);
  assert.equal(patch.notFollowingBackCount, 1);
  assert.equal(patch.followingObj.alice.username, 'alice');
  assert.equal(patch.followingObj.bob, undefined);
});

test('failed walk rolls the counter back to the last-known-good lists', async () => {
  // The progress tracker publishes the growing lists under the authoritative
  // igf.following / igf.followers keys. When the walk then dies those keys —
  // and igf.state — keep the TRUNCATED data, so the "não seguem de volta"
  // badge shows a number computed from a partial list as if it were final.
  const prewalk = {
    'igf.following': { a: { pk: '1', username: 'a' }, b: { pk: '2', username: 'b' }, c: { pk: '3', username: 'c' } },
    'igf.followers': { a: { pk: '1', username: 'a' } },
  };
  Object.assign(store, {
    'igf.following': { a: { pk: '1', username: 'a' } }, // mid-walk partial
    'igf.followers': {},
    'igf.state': { status: 'error', followingCount: 1, followersCount: 0, notFollowingBackCount: 1 },
  });
  await restoreListsAfterFailedWalk(prewalk);
  assert.deepEqual(Object.keys(store['igf.following']), ['a', 'b', 'c']);
  assert.deepEqual(Object.keys(store['igf.followers']), ['a']);
  assert.equal(store['igf.state'].followingCount, 3);
  assert.equal(store['igf.state'].followersCount, 1);
  assert.equal(store['igf.state'].notFollowingBackCount, 2, 'counter recomputed from restored lists');
});

test('itemHtml escapes full_name XSS payload in text and title', () => {
  const html = itemHtml({
    username: 'safeuser',
    full_name: '"><img src=x>',
    is_private: false,
    is_verified: false,
    profile_pic_url: '',
  });
  assert.match(html, /&quot;&gt;&lt;img src=x&gt;/);
  assert.doesNotMatch(html, /title=""><img/);
  assert.doesNotMatch(html, /<span title=""><img/);
});
