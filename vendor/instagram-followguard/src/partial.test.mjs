// IG FollowGuard — checkpoint/resume unit tests.
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildResume, RESUME_TTL_MS, fetchAllUsers, IgApiError, __setRetryBaseMsForTests } from './ig_api.mjs';

const UID = '123';
const P = 'igf.resume.'; // current (v2) checkpoint namespace
const mk = (users, maxId, at = Date.now()) => ({ maxId, at, users });
const u = (n) => ({ username: 'user' + n, pk: String(n) });
const SESSION = { cookieHeader: 'a=1; b=2', csrftoken: 'tok' };

// --- buildResume ---

test('buildResume: no meta -> both null', () => {
  assert.deepEqual(buildResume(UID, null, {}), { following: null, followers: null });
  assert.deepEqual(buildResume(UID, { keys: [] }, {}), { following: null, followers: null });
});

test('buildResume: contiguous pages merge in order', () => {
  const meta = { keys: [`${P}following.123.0`, `${P}following.123.1`] };
  const values = {
    [`${P}following.123.0`]: mk([u(1), u(2)], 'm1'),
    [`${P}following.123.1`]: mk([u(3)], 'm2'),
  };
  const r = buildResume(UID, meta, values);
  assert.equal(r.following.maxId, 'm2');
  assert.deepEqual(r.following.users.map((x) => x.username), ['user1', 'user2', 'user3']);
  assert.equal(r.following.nextSeq, 2); // resume must continue numbering from here
  assert.equal(r.followers, null);
});

test('buildResume: nextSeq reflects persisted page count', () => {
  const meta = { keys: [`${P}following.123.0`, `${P}following.123.1`, `${P}following.123.2`] };
  const values = {
    [`${P}following.123.0`]: mk([u(1)], 'm1'),
    [`${P}following.123.1`]: mk([u(2)], 'm2'),
    [`${P}following.123.2`]: mk([u(3)], 'm3'),
  };
  const r = buildResume(UID, meta, values);
  assert.equal(r.following.nextSeq, 3);
});

test('buildResume: gap truncates to contiguous prefix', () => {
  const meta = { keys: [`${P}following.123.0`, `${P}following.123.1`, `${P}following.123.3`] };
  const values = {
    [`${P}following.123.0`]: mk([u(1)], 'm1'),
    [`${P}following.123.1`]: mk([u(2)], 'm2'),
    [`${P}following.123.3`]: mk([u(4)], 'm4'),
  };
  const r = buildResume(UID, meta, values);
  assert.equal(r.following.maxId, 'm2');
  assert.deepEqual(r.following.users.map((x) => x.username), ['user1', 'user2']);
  assert.equal(r.following.nextSeq, 2);
});

test('buildResume: pages out of order still merge by seq', () => {
  const meta = { keys: [`${P}following.123.2`, `${P}following.123.0`, `${P}following.123.1`] };
  const values = {
    [`${P}following.123.0`]: mk([u(1)], 'm1'),
    [`${P}following.123.1`]: mk([u(2)], 'm2'),
    [`${P}following.123.2`]: mk([u(3)], 'm3'),
  };
  const r = buildResume(UID, meta, values);
  assert.deepEqual(r.following.users.map((x) => x.username), ['user1', 'user2', 'user3']);
});

test('buildResume: other account ignored', () => {
  const meta = { keys: [`${P}following.999.0`] };
  const values = { [`${P}following.999.0`]: mk([u(1)], 'm1') };
  assert.equal(buildResume(UID, meta, values).following, null);
});

test('buildResume: stale checkpoint discarded (TTL)', () => {
  const meta = { keys: [`${P}following.123.0`] };
  const values = { [`${P}following.123.0`]: mk([u(1)], 'm1', Date.now() - RESUME_TTL_MS - 1000) };
  assert.equal(buildResume(UID, meta, values).following, null);
});

test('buildResume: page without maxId skipped', () => {
  const meta = { keys: [`${P}following.123.0`] };
  const values = { [`${P}following.123.0`]: { at: Date.now(), users: [u(1)] } };
  assert.equal(buildResume(UID, meta, values).following, null);
});

test('buildResume: unknown kind ignored', () => {
  const meta = { keys: [`${P}other.123.0`] };
  const values = { [`${P}other.123.0`]: mk([u(1)], 'm1') };
  assert.deepEqual(buildResume(UID, meta, values), { following: null, followers: null });
});

test('buildResume: pre-v2 namespace keys ignored (torn-checkpoint upgrade guard)', () => {
  // The committed pre-fix build wrote `seq: page` (restarting counter) and can
  // leave contiguous labels with torn content; resuming those would produce an
  // incomplete list. The v2 namespace bump must make them invisible.
  const meta = { keys: ['igf.part.following.123.0', 'igf.part.following.123.1'] };
  const values = {
    'igf.part.following.123.0': mk([u(1)], 'm1'),
    'igf.part.following.123.1': mk([u(2)], 'm2'),
  };
  assert.deepEqual(buildResume(UID, meta, values), { following: null, followers: null });
});

// --- fetchAllUsers + resume + malformed-response guard ---

function stubFetchQueue(payloads) {
  const queue = payloads.slice();
  globalThis.fetch = async () => {
    const p = queue.shift();
    return { ok: true, status: 200, text: async () => JSON.stringify(p) };
  };
}

test('fetchAllUsers: malformed {} with resume rejects (no truncated completion)', async () => {
  __setRetryBaseMsForTests(1);
  stubFetchQueue([{}, {}, {}, {}]); // initial + 3 fast retries, all malformed
  const resume = { maxId: 'm0', nextSeq: 2, users: [u(1), u(2)] };
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, { resume }),
    (err) => err instanceof IgApiError && err.code === 'http',
    'malformed response must not complete a resumed sync with only the prefix'
  );
});

test('fetchAllUsers: malformed {} from scratch rejects (original guard)', async () => {
  stubFetchQueue([{}, {}, {}, {}]);
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'http'
  );
});

test('fetchAllUsers: legit empty page after resume completes with prefix only', async () => {
  stubFetchQueue([{ status: 'ok', users: [] }]); // end of list, users present
  const resume = { maxId: 'm0', nextSeq: 1, users: [u(1)] };
  const out = await fetchAllUsers('following', UID, SESSION, { resume });
  assert.deepEqual([...out.keys()], ['user1']);
});

test('fetchAllUsers: happy path, onPart seq continues from resume.nextSeq', async () => {
  stubFetchQueue([
    { status: 'ok', users: [u(2), u(3)], next_max_id: 'm1' },
    { status: 'ok', users: [u(4)] },
  ]);
  const resume = { maxId: 'm0', nextSeq: 1, users: [u(1)] };
  const parts = [];
  const out = await fetchAllUsers('following', UID, SESSION, {
    resume,
    onPart: async ({ seq, maxId, users }) => { parts.push([seq, maxId, users.length]); },
  });
  // seeded 1 + page 2 users (page 2 has next_max_id -> checkpointed with seq 1)
  assert.deepEqual([...out.keys()], ['user1', 'user2', 'user3', 'user4']);
  assert.deepEqual(parts, [[1, 'm1', 2]]); // seq continues, never restarts at 0
});

test('fetchAllUsers: fresh run checkpoints from seq 0', async () => {
  stubFetchQueue([
    { status: 'ok', users: [u(1)], next_max_id: 'm1' },
    { status: 'ok', users: [u(2)] },
  ]);
  const parts = [];
  const out = await fetchAllUsers('following', UID, SESSION, {
    onPart: async ({ seq, maxId, users }) => { parts.push([seq, maxId, users.length]); },
  });
  assert.deepEqual([...out.keys()], ['user1', 'user2']);
  assert.deepEqual(parts, [[0, 'm1', 1]]);
});
