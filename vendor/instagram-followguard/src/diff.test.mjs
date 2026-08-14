// IG FollowGuard — unit tests for the follow-diff + event logic (pure, no chrome).
'use strict';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { diffAndRecord, mergeEvents } from './diff.mjs';

const meta = (pk, name = '') => ({ pk: String(pk), username: 'u', full_name: name, is_private: false, is_verified: false, profile_pic_url: '' });
const map = (o) => new Map(Object.entries(o));

test('first sync baseline: no events, history seeded', () => {
  const prev = {};
  const next = map({ a: meta(1), b: meta(2) });
  const { events, newHistory } = diffAndRecord(prev, next, map({ a: meta(1), b: meta(2) }), {}, 1000);
  assert.equal(events.length, 0);
  assert.equal(newHistory.a.firstFollowedAt, 1000);
  assert.equal(newHistory.a.unfollowedAt, null);
});

test('unfollow detected with stillFollowing flag', () => {
  const prev = { a: meta(1), b: meta(2), c: meta(3) };
  const next = map({ b: meta(2) });
  const following = map({ a: meta(1), b: meta(2) }); // user still follows a, unfollowed c
  const { events, newHistory } = diffAndRecord(prev, next, following, {}, 2000);
  assert.equal(events.length, 2);
  const a = events.find((e) => e.username === 'a');
  const c = events.find((e) => e.username === 'c');
  assert.equal(a.stillFollowing, true, 'we still follow a -> notifiable');
  assert.equal(c.stillFollowing, false, 'we no longer follow c -> not notified');
  assert.equal(a.detectedAt, 2000);
  assert.equal(newHistory.a.unfollowedAt, 2000);
  assert.equal(newHistory.a.unfollowCount, 1);
  assert.equal(newHistory.c.unfollowCount, 1);
});

test('idempotence: identical snapshots produce no events', () => {
  const prev = { a: meta(1), b: meta(2) };
  const next = map({ a: meta(1), b: meta(2) });
  const { events } = diffAndRecord(prev, next, map({ a: meta(1), b: meta(2) }), {}, 3000);
  assert.equal(events.length, 0);
});

test('re-follow after unfollow resets unfollowedAt, counts accumulate', () => {
  let history = {};
  const snap1 = { a: meta(1) };
  const r1 = diffAndRecord(snap1, map({}), map({ a: meta(1) }), history, 1000); // a leaves
  history = r1.newHistory;
  assert.equal(history.a.unfollowCount, 1);
  const r2 = diffAndRecord({}, map({ a: meta(1) }), map({ a: meta(1) }), history, 2000); // a returns
  history = r2.newHistory;
  assert.equal(history.a.unfollowedAt, null, 'unfollowedAt cleared on re-follow');
  assert.equal(history.a.unfollowCount, 1, 'count preserved, not reset');
  assert.equal(history.a.firstFollowedAt, 2000, 'first recorded follow is the re-follow');
  const r3 = diffAndRecord({ a: meta(1) }, map({}), map({ a: meta(1) }), history, 3000); // a leaves again
  assert.equal(r3.newHistory.a.unfollowCount, 2);
  assert.equal(r3.newHistory.a.unfollowedAt, 3000);
});

test('mergeEvents caps at 100, newest first', () => {
  const stored = Array.from({ length: 100 }, (_, i) => ({ username: `old${i}`, detectedAt: 99 - i }));
  const fresh = Array.from({ length: 12 }, (_, i) => ({ username: `new${i}`, detectedAt: 1000 + i }));
  const merged = mergeEvents(fresh, stored, 100);
  assert.equal(merged.length, 100);
  assert.equal(merged[0].username, 'new11', 'newest first');
  assert.equal(merged[99].username, 'old87', 'oldest kept is the 88th stored (12 oldest dropped)');
  assert.ok(fresh.every((f) => merged.some((m) => m.username === f.username)), 'all fresh events kept');
  assert.ok(merged.every((e, i) => i === 0 || merged[i - 1].detectedAt >= e.detectedAt), 'sorted desc');
});

test('mergeEvents empty stored keeps fresh', () => {
  const merged = mergeEvents([{ username: 'a' }], [], 100);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].username, 'a');
});
