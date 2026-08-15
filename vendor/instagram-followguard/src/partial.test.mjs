// IG FollowGuard — checkpoint/resume + error-classification unit tests.
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildResume, RESUME_TTL_MS, fetchAllUsers, IgApiError,
  __setRetryBaseMsForTests, __setFetchTimeoutMsForTests, __setPageDelayMsForTests,
} from './ig_api.mjs';

// Test seams — real backoff/timeout/pacing would make these tests take minutes.
__setRetryBaseMsForTests(1);
__setFetchTimeoutMsForTests(5);
__setPageDelayMsForTests(0);

const UID = '123';
const P = 'igf.resume.'; // current (v2) checkpoint namespace
const mk = (users, maxId, at = Date.now()) => ({ maxId, at, users });
const u = (n) => ({ username: 'user' + n, pk: String(n) });
const SESSION = { cookieHeader: 'a=1; b=2', csrftoken: 'tok' };
// MAX_RETRIES=5 -> initial attempt + 5 retries = 6 payloads for transient shapes.
const RETRY_ROUNDS = 6;

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
  const meta = { keys: ['igf.part.following.123.0', 'igf.part.following.123.1'] };
  const values = {
    'igf.part.following.123.0': mk([u(1)], 'm1'),
    'igf.part.following.123.1': mk([u(2)], 'm2'),
  };
  assert.deepEqual(buildResume(UID, meta, values), { following: null, followers: null });
});

// --- fetchAllUsers: resume + malformed-response guard ---

function stubFetchQueue(payloads) {
  const queue = payloads.slice();
  globalThis.fetch = async () => {
    const p = queue.shift();
    return { ok: true, status: 200, text: async () => JSON.stringify(p) };
  };
}

test('fetchAllUsers: malformed {} with resume rejects (no truncated completion)', async () => {
  stubFetchQueue(Array(RETRY_ROUNDS).fill({}));
  const resume = { maxId: 'm0', nextSeq: 2, users: [u(1), u(2)] };
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, { resume }),
    (err) => err instanceof IgApiError && err.code === 'http',
    'malformed response must not complete a resumed sync with only the prefix'
  );
});

test('fetchAllUsers: malformed {} from scratch rejects (original guard)', async () => {
  stubFetchQueue(Array(RETRY_ROUNDS).fill({}));
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

// --- fetchAllUsers: timeout / network / gate classification ---

function stubFetchHang() {
  // Never resolves on its own — only the AbortController can end it, exactly
  // like a throttled IG connection holding the request open.
  globalThis.fetch = (_url, opts) => new Promise((_, reject) => {
    const s = opts && opts.signal;
    if (s) s.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
  });
}

test('fetchAllUsers: hung request aborts to network and retries (no infinite stall)', async () => {
  stubFetchHang();
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'network',
    'a hung fetch must fail into the retry path, never hang the sync'
  );
});

test('fetchAllUsers: raw network rejection classified network and retried', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'network'
  );
});

test('fetchAllUsers: feedback_required is terminal, no retry (one call)', async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return { ok: true, status: 200, text: async () => JSON.stringify({ status: 'fail', message: 'feedback_required', feedback_title: 'Sua conta foi temporariamente limitada.' }) };
  };
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'feedback-required'
  );
  assert.equal(calls, 1, 'bot gate must not be retried');
});

test('apiFetch: HTML login wall -> not-logged-in', async () => {
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    text: async () => '<!DOCTYPE html><html><body><a href="/accounts/login/">Entrar</a></body></html>',
  });
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'not-logged-in'
  );
});

test('apiFetch: gate HTML -> feedback-required (friend\'s exact dead end now actionable)', async () => {
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    text: async () => '<!DOCTYPE html><html><body><title>temporarily limited</title>feedback_required</body></html>',
  });
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'feedback-required'
  );
});

test('apiFetch: 401 -> not-logged-in', async () => {
  globalThis.fetch = async () => ({
    ok: false, status: 401,
    text: async () => JSON.stringify({ status: 'fail', message: 'login_required' }),
  });
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'not-logged-in'
  );
});

test('apiFetch: rate_limit_error body -> rate-limited', async () => {
  stubFetchQueue(Array(RETRY_ROUNDS).fill({ status: 'fail', message: 'rate_limit_error' }));
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'rate-limited'
  );
});

// --- fetchAllUsers: data-integrity guards ---

test('fetchAllUsers: page with invalid entries throws (cursor never advances past bad data)', async () => {
  stubFetchQueue(Array(RETRY_ROUNDS).fill({ status: 'ok', users: [{ username: 'ok', pk: '1' }, null], next_max_id: 'm1' }));
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'http',
    'a page containing null entries must fail, not silently drop users'
  );
});

test('fetchAllUsers: partial page with next_max_id continues and checkpoints', async () => {
  // The friend's page 6: 21 users WITH next_max_id — must continue, never stall.
  const partial = Array.from({ length: 21 }, (_, i) => u(100 + i));
  stubFetchQueue([
    { status: 'ok', users: partial, next_max_id: 'm6' },
    { status: 'ok', users: [u(999)] },
  ]);
  const parts = [];
  const out = await fetchAllUsers('following', UID, SESSION, {
    onPart: async ({ seq, maxId, users }) => { parts.push([seq, maxId, users.length]); },
  });
  assert.equal(out.size, 22);
  assert.deepEqual(parts, [[0, 'm6', 21]]);
});

test('fetchAllUsers: MAX_PAGES exhaustion throws limit (never completes short)', async () => {
  // 500 pages, each with a next_max_id -> the loop hits the cap. The sync must
  // FAIL (checkpoints intact), never diff/notify on a truncated list.
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return {
      ok: true, status: 200,
      text: async () => JSON.stringify({ status: 'ok', users: [{ username: 'u' + calls, pk: String(calls) }], next_max_id: 'm' + calls }),
    };
  };
  await assert.rejects(
    fetchAllUsers('following', UID, SESSION, {}),
    (err) => err instanceof IgApiError && err.code === 'limit'
  );
  assert.equal(calls, 500);
});
