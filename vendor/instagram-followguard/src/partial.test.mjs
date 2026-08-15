// IG FollowGuard — buildResume checkpoint-merge unit tests.
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildResume, RESUME_TTL_MS } from './ig_api.mjs';

const UID = '123';
const mk = (users, maxId, at = Date.now()) => ({ maxId, at, users });
const u = (n) => ({ username: 'user' + n, pk: String(n) });

test('buildResume: no meta -> both null', () => {
  assert.deepEqual(buildResume(UID, null, {}), { following: null, followers: null });
  assert.deepEqual(buildResume(UID, { keys: [] }, {}), { following: null, followers: null });
});

test('buildResume: contiguous pages merge in order', () => {
  const meta = { keys: ['igf.part.following.123.0', 'igf.part.following.123.1'] };
  const values = {
    'igf.part.following.123.0': mk([u(1), u(2)], 'm1'),
    'igf.part.following.123.1': mk([u(3)], 'm2'),
  };
  const r = buildResume(UID, meta, values);
  assert.equal(r.following.maxId, 'm2');
  assert.deepEqual(r.following.users.map((x) => x.username), ['user1', 'user2', 'user3']);
  assert.equal(r.followers, null);
});

test('buildResume: gap truncates to contiguous prefix', () => {
  const meta = { keys: ['igf.part.following.123.0', 'igf.part.following.123.1', 'igf.part.following.123.3'] };
  const values = {
    'igf.part.following.123.0': mk([u(1)], 'm1'),
    'igf.part.following.123.1': mk([u(2)], 'm2'),
    'igf.part.following.123.3': mk([u(4)], 'm4'),
  };
  const r = buildResume(UID, meta, values);
  assert.equal(r.following.maxId, 'm2');
  assert.deepEqual(r.following.users.map((x) => x.username), ['user1', 'user2']);
});

test('buildResume: pages out of order still merge by seq', () => {
  const meta = { keys: ['igf.part.following.123.2', 'igf.part.following.123.0', 'igf.part.following.123.1'] };
  const values = {
    'igf.part.following.123.0': mk([u(1)], 'm1'),
    'igf.part.following.123.1': mk([u(2)], 'm2'),
    'igf.part.following.123.2': mk([u(3)], 'm3'),
  };
  const r = buildResume(UID, meta, values);
  assert.deepEqual(r.following.users.map((x) => x.username), ['user1', 'user2', 'user3']);
});

test('buildResume: other account ignored', () => {
  const meta = { keys: ['igf.part.following.999.0'] };
  const values = { 'igf.part.following.999.0': mk([u(1)], 'm1') };
  assert.equal(buildResume(UID, meta, values).following, null);
});

test('buildResume: stale checkpoint discarded (TTL)', () => {
  const meta = { keys: ['igf.part.following.123.0'] };
  const values = { 'igf.part.following.123.0': mk([u(1)], 'm1', Date.now() - RESUME_TTL_MS - 1000) };
  assert.equal(buildResume(UID, meta, values).following, null);
});

test('buildResume: page without maxId skipped', () => {
  const meta = { keys: ['igf.part.following.123.0'] };
  const values = { 'igf.part.following.123.0': { at: Date.now(), users: [u(1)] } };
  assert.equal(buildResume(UID, meta, values).following, null);
});

test('buildResume: unknown kind ignored', () => {
  const meta = { keys: ['igf.part.other.123.0'] };
  const values = { 'igf.part.other.123.0': mk([u(1)], 'm1') };
  assert.deepEqual(buildResume(UID, meta, values), { following: null, followers: null });
});
