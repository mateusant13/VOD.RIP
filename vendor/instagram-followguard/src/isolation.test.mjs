// IG FollowGuard — account-isolation snapshot policy tests.
'use strict';
import { test } from 'node:test';
import assert from 'node:assert/strict';

globalThis.chrome = {
  alarms: { clear: async () => {}, create: async () => {}, onAlarm: { addListener() {} } },
  storage: { local: { get: async () => ({}), set: async () => {}, remove: async () => {} } },
  runtime: {
    getURL: (p) => `chrome-extension://test/${p}`,
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: { addListener() {} },
  },
  notifications: { create: async () => {}, clear: async () => {}, onClicked: { addListener() {} } },
  tabs: { create: async () => {}, query: async () => [], sendMessage: async () => ({ pong: true }), remove: async () => {}, get: async () => { throw new Error('gone'); } },
};

const { processSyncSnapshot } = await import('./background.js');

const meta = (pk, name = '') => ({
  pk: String(pk), username: 'u', full_name: name, is_private: false, is_verified: false, profile_pic_url: '',
});

test('account switch: stored uid A, session uid B => no notify, fresh baseline', () => {
  const prev = { victim: meta(1, 'Victim') };
  const followers = new Map([['victim', meta(1)]]);
  const following = new Map();
  const snap = processSyncSnapshot({
    storedSnapshotUid: 'A',
    currentUid: 'B',
    prev,
    followers,
    following,
    history: {},
  });
  assert.equal(snap.events.length, 0, 'skip diff on account switch');
  assert.equal(snap.notifyable.length, 0);
  assert.equal(snap.snapshotUid, 'B');
  assert.equal(snap.freshBaseline, true);
});

test('same uid with complete prev still produces notifyable unfollows', () => {
  const prev = { a: meta(1), b: meta(2) };
  const followers = new Map([['b', meta(2)]]);
  const following = new Map([['a', meta(1)], ['b', meta(2)]]);
  const snap = processSyncSnapshot({
    storedSnapshotUid: '42',
    currentUid: '42',
    prev,
    followers,
    following,
    history: {},
  });
  assert.equal(snap.events.length, 1);
  assert.equal(snap.events[0].username, 'a');
  assert.equal(snap.notifyable.length, 1);
  assert.equal(snap.freshBaseline, false);
});
