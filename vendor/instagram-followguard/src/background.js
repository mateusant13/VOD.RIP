// IG FollowGuard — service worker.
// Owns: full followers/following sync (complete lists), not-following-back
// diffing, unfollow detection vs the previous snapshot, notifications,
// periodic alarms. Reads ONLY follow/follower relationships.
'use strict';

import { readSession, fetchAllUsers, IgApiError, buildResume, apiFetch, transientRetry, jitteredPauseMs, __setTransport } from './ig_api.mjs';
import { diffAndRecord, mergeEvents, detectNewFollowers, applyManualUnfollow, applyFriendshipAction } from './diff.mjs';

const K = {
  settings: 'igf.settings',
  state: 'igf.state',
  followers: 'igf.followers',
  following: 'igf.following',
  prevFollowers: 'igf.prevFollowers',
  history: 'igf.followHistory',
  events: 'igf.unfollowEvents',
  newFollowers: 'igf.newFollowerEvents',
  snapshotUid: 'igf.snapshotUid',
};
const EVENTS_MAX = 100;
const SYNC_ALARM = 'igf-sync';
const DEFAULT_SETTINGS = {
  // No username here: the logged-in profile is resolved at runtime from the
  // session cookie (ds_user_id -> /api/v1/users/{pk}/info/). Never hardcode.
  refreshMinutes: 180,
  notificationsEnabled: true,
  // Hunt precaution (owner registry: "no auto-sync/refetch without owner OK"):
  // background sync is OFF unless the user turns it on deliberately. With it
  // off, the ONLY IG contact is an explicit ↻ click.
  autoSync: false,
  consentAt: null,
};

/** Account-isolation policy for prevFollowers snapshots (exported for tests). */
export function evaluateSnapshotPolicy(storedSnapshotUid, currentUid) {
  const cur = currentUid != null && currentUid !== '' ? String(currentUid) : null;
  if (!cur) {
    return { skipDiff: false, skipNotify: false, migrateUid: false };
  }
  const stored = storedSnapshotUid != null && storedSnapshotUid !== ''
    ? String(storedSnapshotUid) : null;
  if (stored === null) {
    // Legacy snapshot without uid — diff once, notify once skipped, tag uid.
    return { skipDiff: false, skipNotify: true, migrateUid: true };
  }
  if (stored !== cur) {
    return { skipDiff: true, skipNotify: true, migrateUid: true };
  }
  return { skipDiff: false, skipNotify: false, migrateUid: true };
}

/** Pure sync snapshot step — diff, baseline, and notify eligibility (tests). */
export function processSyncSnapshot({
  storedSnapshotUid,
  currentUid,
  prev,
  followers,
  following,
  history,
  now = Date.now(),
}) {
  const policy = evaluateSnapshotPolicy(storedSnapshotUid, currentUid);
  const fMap = followers instanceof Map ? followers : new Map(Object.entries(followers || {}));
  const gMap = following instanceof Map ? following : new Map(Object.entries(following || {}));
  const hist = history || {};
  let events = [];
  let newHistory = hist;
  if (!policy.skipDiff) {
    const diff = diffAndRecord(prev || {}, fMap, gMap, hist, now);
    events = diff.events;
    newHistory = diff.newHistory;
  }
  const notifyable = policy.skipNotify ? [] : events.filter((e) => e.stillFollowing);
  return {
    events,
    newHistory: policy.skipDiff ? hist : newHistory,
    notifyable,
    snapshotUid: curUid(currentUid),
    freshBaseline: policy.skipDiff,
  };
}


/** Dynamic manual-sync cooldown (ms) — scales with list size.
 * Anti-footgun design (owner: the user is a layperson and must not be able to
 * spam the account into a gate): after a SUCCESS the wait is list-scaled with
 * a deliberately high ceiling for huge accounts (40k+ users -> ~2h), because
 * nothing a layperson does changes faster than the lists themselves. A short
 * 5-min floor still covers fresh accounts so early retry-happiness is capped.
 */
export function manualSyncCooldownMs(followersCount = 0, followingCount = 0) {
  const total = Math.max(0, Number(followersCount) || 0) + Math.max(0, Number(followingCount) || 0);
  const minutes = Math.min(120, Math.max(10, 10 + Math.floor(total / 300)));
  return minutes * 60 * 1000;
}

/** Pure helper — blocks manual re-sync shortly after a successful full sync. */
export function manualSyncCooldownInfo(lastSyncAt, opts = {}, now = Date.now()) {
  const followersCount = opts.followersCount ?? 0;
  const followingCount = opts.followingCount ?? 0;
  const freeRefreshPending = !!opts.freeRefreshPending;
  if (!lastSyncAt) return { blocked: false, waitMs: 0, waitMinutes: 0, nextSyncAt: null, freeRefresh: false };
  if (freeRefreshPending) {
    return { blocked: false, waitMs: 0, waitMinutes: 0, nextSyncAt: null, freeRefresh: true };
  }
  const elapsed = now - new Date(lastSyncAt).getTime();
  const waitMs = manualSyncCooldownMs(followersCount, followingCount);
  if (elapsed >= waitMs) return { blocked: false, waitMs: 0, waitMinutes: 0, nextSyncAt: null, freeRefresh: false };
  const remainMs = waitMs - elapsed;
  const waitMinutes = Math.max(1, Math.ceil(remainMs / 60000));
  return {
    blocked: true,
    waitMs: remainMs,
    waitMinutes,
    nextSyncAt: new Date(now + remainMs).toISOString(),
    freeRefresh: false,
  };
}

/** Reuse window for the `following` walk (7 days). Long enough that a normal
 * weekly check-in costs one following walk per week instead of one per sync;
 * short enough that even a fully unwatched account is re-walked regularly. */
export const FOLLOWING_REUSE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * Pure reuse gate: may sync() skip the `following` walk and trust the stored
 * map? Exported as the tested decision seam.
 *
 * WHY this is safe where a generic cache would not be: the `following` map is
 * already kept current for every change the USER makes in an IG tab —
 * friendship_hook.js / friendship_watch.js intercept our own follow /
 * unfollow / remove_follower calls and recordFriendshipAction() rewrites
 * K.following live. The drift a reuse window can still hide is therefore
 * narrow: following done where no extension tab could watch it (mobile app,
 * another browser) AND a declared count Instagram has not yet updated.
 *
 * Every signal must be present and agree, or we walk (fail closed):
 *   1. same account (snapshotUid === currentUid, both non-null) — a fresh
 *      baseline or a switched account has no trustworthy stored map;
 *   2. a prior SUCCESSFUL walk timestamp (followingWalkedAt) — legacy state
 *      without the field, or a state whose last walk never completed, walks;
 *   3. declared followingCount === stored map size — EXACT, no slack. IG says
 *      20003 and we hold 20000 → something changed unwatched → walk. A
 *      missing/unparseable declared count is unverifiable → walk;
 *   4. age of that walk <= maxAgeMs.
 * A non-positive / non-finite maxAgeMs disables reuse rather than trusting a
 * negative window.
 */
export function shouldReuseFollowingList({
  snapshotUid,
  currentUid,
  followingWalkedAt,
  declaredFollowingCount,
  storedFollowingSize,
  now = Date.now(),
  maxAgeMs = FOLLOWING_REUSE_MAX_AGE_MS,
} = {}) {
  const cur = curUid(currentUid);
  const stored = curUid(snapshotUid);
  if (!cur || !stored || stored !== cur) return false;

  // Number(null)===0 would let a missing declared count satisfy an EMPTY
  // stored map; a count we never got is unverifiable -> walk (fail closed).
  if (declaredFollowingCount == null || declaredFollowingCount === '') return false;
  const declared = Number(declaredFollowingCount);
  const size = Number(storedFollowingSize);
  if (!Number.isFinite(declared) || declared < 0) return false;
  if (!Number.isInteger(size) || size < 0) return false;
  if (declared !== size) return false;

  if (typeof maxAgeMs !== 'number' || !Number.isFinite(maxAgeMs) || maxAgeMs <= 0) return false;
  if (followingWalkedAt == null || followingWalkedAt === '') return false;
  const walkedAt = new Date(followingWalkedAt).getTime();
  const t = Number(now);
  if (!Number.isFinite(walkedAt) || !Number.isFinite(t)) return false;
  return t - walkedAt <= maxAgeMs;
}

/** Apply a user-initiated unfollow to persisted maps (tests + runtime). */
export function recordManualUnfollowMaps(followingObj, followersObj, { pk, username } = {}) {
  const result = applyManualUnfollow(followingObj, followersObj, { pk, username });
  if (!result) return null;
  return {
    followingObj: Object.fromEntries(result.following),
    removedUsername: result.removedUsername,
    followingCount: result.followingCount,
    followersCount: result.followersCount,
    notFollowingBackCount: result.notFollowingBackCount,
  };
}
function curUid(uid) {
  return uid != null && uid !== '' ? String(uid) : null;
}


const emptyState = () => ({
  status: 'idle', // idle | syncing | ok | error
  trigger: null,
  lastSyncAt: null,
  lastAttemptAt: null, // EVERY sync attempt (success or failure) — cooldown ref for the error path
  lastDurationMs: null,
  error: null,
  ownUsername: null,
  ownUserId: null,
  followersCount: 0,
  followingCount: 0,
  notFollowingBackCount: 0,
  incomplete: false, // true when a list was truncated by the page cap
  // ISO timestamp of the last sync that WALKED `following` to completion
  // (never stamped by a reuse or a failure) — the reuse gate's age signal.
  followingWalkedAt: null,
  // true when the last completed sync reused the stored `following` map
  // instead of walking it — drives the one-line dashboard hint.
  followingReused: false,
  freeManualRefresh: false, // one free manual refresh after each successful sync
});

async function getSettings() {
  const o = await chrome.storage.local.get(K.settings);
  const s = { ...DEFAULT_SETTINGS, ...(o[K.settings] || {}) };
  // Hunt V5 (O-6): migrate pre-0.6.4 stored cadences (30/60 min) to the 3h
  // floor AT THE BOUNDARY, so the UI select, the alarm, and storage can
  // never disagree (the select has no <option> below 180 anymore).
  const rm = Number(s.refreshMinutes);
  if (!Number.isFinite(rm) || rm < 180) s.refreshMinutes = 180;
  return s;
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

// Serialized read-modify-write: the sync branches, alarms and message
// handlers all patch the same key; an unlocked RMW loses concurrent updates
// (a lost retryN silently breaks backoff escalation). Same chain pattern as
// transportLock — a failed write must not poison the chain.
let stateLock = Promise.resolve();
async function setState(patch) {
  const run = stateLock.then(async () => {
    const o = await chrome.storage.local.get(K.state);
    await chrome.storage.local.set({ [K.state]: { ...emptyState(), ...(o[K.state] || {}), ...patch } });
  });
  stateLock = run.catch(() => {});
  return run;
}
// Read-modify-write as ONE locked step: fn gets the freshly merged state and
// returns the patch to persist (fn must be SYNC — an async fn would run its
// reads outside the lock). Same merge semantics and chain-poison guard as
// setState.
async function patchState(fn) {
  const run = stateLock.then(async () => {
    const o = await chrome.storage.local.get(K.state);
    const cur = { ...emptyState(), ...(o[K.state] || {}) };
    await chrome.storage.local.set({ [K.state]: { ...cur, ...fn(cur) } });
  });
  stateLock = run.catch(() => {});
  return run;
}


export async function deleteAllData() {
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((k) => k.startsWith('igf.'));
  if (keys.length) await chrome.storage.local.remove(keys);
  await chrome.storage.local.set({
    [K.settings]: { ...DEFAULT_SETTINGS },
    [K.state]: emptyState(),
  });
}

/** Export every igf.* key for backup / reinstall migration. */
export async function exportBackup() {
  const all = await chrome.storage.local.get(null);
  const data = {};
  for (const [k, v] of Object.entries(all)) {
    if (k.startsWith('igf.')) data[k] = v;
  }
  return { schema: 1, exportedAt: Date.now(), data };
}

/** Restore igf.* keys from a prior exportBackup() payload. */
export async function importBackup(raw) {
  if (!raw || typeof raw !== 'object' || !raw.data || typeof raw.data !== 'object') {
    throw new Error('Backup inválido — arquivo corrompido ou formato antigo.');
  }
  const entries = Object.entries(raw.data).filter(([k]) => k.startsWith('igf.'));
  if (!entries.length) {
    throw new Error('Backup vazio — nenhum dado do IG FollowGuard.');
  }
  const all = await chrome.storage.local.get(null);
  const stale = Object.keys(all).filter((k) => k.startsWith('igf.'));
  if (stale.length) await chrome.storage.local.remove(stale);
  await chrome.storage.local.set(Object.fromEntries(entries));
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
// True when at least one instagram.com tab is open. Every automatic sync
// entrypoint (alarm, startup, SW-restart resume) MUST gate on this: the sync
// transport runs on an IG tab, and ensureIgTab() would otherwise OPEN one on
// its own — an auto-opened instagram.com is exactly what we never do.
async function hasOpenIgTab() {
  try {
    const tabs = await chrome.tabs.query({ url: 'https://www.instagram.com/*' });
    return tabs.length > 0;
  } catch {
    return false;
  }
}

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
  const run = metaLock.then(async () => {
    await chrome.storage.local.set({ [key]: { maxId, at: Date.now(), users } });
    try {
      const o = await chrome.storage.local.get(PART_META);
      const meta = o[PART_META] || { keys: [] };
      if (!meta.keys.includes(key)) meta.keys.push(key);
      await chrome.storage.local.set({ [PART_META]: meta });
    } catch (err) {
      // The value write succeeded but the index failed — an unindexed key
      // is invisible to clearPartials and would leak a full page payload
      // forever. Drop it with the page.
      await chrome.storage.local.remove(key).catch(() => {});
      throw err;
    }
  });
  // The chain survives a failed write: without the .catch, ONE storage
  // rejection (quota / SW teardown) poisoned every later checkpoint for the
  // SW's lifetime — fetchAllUsers swallows onPart errors, so the walk kept
  // going with checkpointing silently dead (transportLock already uses this
  // pattern correctly).
  metaLock = run.catch(() => {});
  return run;
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

function usersToObj(users) {
  const o = {};
  if (!Array.isArray(users)) return o;
  for (const u of users) {
    if (u && u.username) o[u.username] = u;
  }
  return o;
}

function countNotFollowingBack(followingObj, followersObj) {
  const fKeys = new Set(Object.keys(followersObj || {}));
  return Object.keys(followingObj || {}).filter((u) => !fKeys.has(u)).length;
}

/**
 * Restore the last-known-good lists (and the counter derived from them) after
 * a follow-list walk failed. Exported as the tested seam for the rollback.
 */
export async function restoreListsAfterFailedWalk(prewalk) {
  const pf = (prewalk && prewalk[K.followers]) || {};
  const pg = (prewalk && prewalk[K.following]) || {};
  await chrome.storage.local.set({ [K.followers]: pf, [K.following]: pg });
  await patchState(() => ({
    followingCount: Object.keys(pg).length,
    followersCount: Object.keys(pf).length,
    notFollowingBackCount: countNotFollowingBack(pg, pf),
  }));
  return { followingCount: Object.keys(pg).length, followersCount: Object.keys(pf).length };
}

function makeListProgressTracker() {
  const counts = { following: 0, followers: 0 };
  let publishChain = Promise.resolve();
  return {
    counts,
    flush() { return publishChain; },
    onProgress({ kind: k, fetched, users }) {
      counts[k] = fetched;
      const patch = { [k === 'following' ? K.following : K.followers]: usersToObj(users) };
      publishChain = publishChain.then(async () => {
        await chrome.storage.local.set(patch);
        const o = await chrome.storage.local.get([K.followers, K.following]);
        const followersObj = o[K.followers] || {};
        const followingObj = o[K.following] || {};
        await setState({
          syncProgress: {
            phase: k,
            fetched,
            followingFetched: counts.following,
            followersFetched: counts.followers,
          },
          followingCount: Object.keys(followingObj).length,
          followersCount: Object.keys(followersObj).length,
          notFollowingBackCount: countNotFollowingBack(followingObj, followersObj),
        });
      }).catch(() => {});
      return publishChain;
    },
  };
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
let pinnedForeignTabId = null; // user-owned IG tab we marked non-discardable
let openedTabId = null; // tab we created for the sync (must be closed after)

async function pingProxy(tabId) {
  try {
    const r = await chrome.tabs.sendMessage(tabId, { igf: 'ping' });
    // Version token: a tab whose content script predates an extension
    // reload answers the ping but runs the OLD build's proxy code. Treat a
    // mismatch (or a pre-token proxy, which sends no version) as dead so
    // ensureIgTab opens a fresh tab instead of driving stale code.
    const self = chrome.runtime.getManifest().version;
    return !!(r && r.pong && r.version === self);
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

async function pinSyncTab(active, { hint = false } = {}) {
  if (syncTabId == null) return;
  try {
    if (active) {
      await chrome.tabs.update(syncTabId, { autoDiscardable: false });
      if (openedTabId == null) pinnedForeignTabId = syncTabId;
    } else if (pinnedForeignTabId === syncTabId) {
      await chrome.tabs.update(syncTabId, { autoDiscardable: true }).catch(() => {});
      pinnedForeignTabId = null;
    }
    // Banner only during real sync runs — not igf-get-own tab ensure.
    if (!active || hint) {
      await chrome.tabs.sendMessage(syncTabId, { igf: 'sync-hint', active: !!active });
    }
  } catch { /* tab gone or listener not ready */ }
}

async function ensureIgTab() {
  // Prefer an existing IG tab whose content script ANSWERS — a pre-reload
  // tab (no listener) is skipped, never navigated.
  const tabs = await chrome.tabs.query({ url: 'https://www.instagram.com/*' });
  // Prefer the tab the user is actually looking at: freshest session, and
  // the sync banner lands where the user can see it.
  tabs.sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0));
  for (const t of tabs) {
    if (await pingProxy(t.id)) {
      syncTabId = t.id;
      await pinSyncTab(true);
      return;
    }
  }
  // Reuse our own still-open tab, else open a fresh one (content script is
  // guaranteed after the page loads; ping until it answers).
  if (openedTabId != null) {
    try {
      await chrome.tabs.get(openedTabId);
      syncTabId = openedTabId;
      await pinSyncTab(true);
      return;
    } catch { openedTabId = null; }
  }
  const t = await chrome.tabs.create({ url: 'https://www.instagram.com/', active: false });
  openedTabId = t.id;
  syncTabId = t.id;
  await waitForProxy(t.id, 10000);
  await pinSyncTab(true);
}

function releaseIgTab() {
  const tab = syncTabId;
  pinSyncTab(false).finally(() => {
    // Never close tabs — user may be browsing; only drop our borrow state.
    openedTabId = null;
    if (pinnedForeignTabId === tab) pinnedForeignTabId = null;
    syncTabId = null;
  });
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

async function pageTransport(path, _session, signal) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (signal && signal.aborted) throw new IgApiError('aborted', 'Sincronização interrompida.');
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

// 'incomplete' = the completeness oracle rejected a short list: transient —
// checkpoints survive and the escalating auto-retry resumes the walk.
const TRANSIENT_CODES = new Set(['http', 'network', 'rate-limited', 'incomplete']);
const RETRY_ALARM = 'igf-sync-retry';
const CONTINUE_ALARM = 'igf-sync-continue';
const SEGMENT_PAUSE_MS = 90_000; // pause between 12k-user chunks (~500 pages)
// Escalating auto-retry delays (min): a fixed 5-min retry loop for hours is a
// bot cadence; a human checks back later, then much later. retryN is
// persisted and reset on success; cap at 2h.
const RETRY_DELAYS_MIN = [5, 15, 45, 120];

// After a TRANSIENT failure (never login/checkpoint/gate/limit), schedule a
// one-shot retry. Checkpoints make the auto-resume safe — it picks up from
// the last persisted page instead of re-fetching from page 1.
function segmentPauseMs() {
  const base = SEGMENT_PAUSE_MS;
  return Math.round(base * (0.9 + Math.random() * 0.35)); // ~81–124s
}

let segmentPauseMsForTests = null;
export function __setSegmentPauseMsForTests(fn) { segmentPauseMsForTests = fn; }

/**
 * Fetch a full list, auto-continuing across MAX_PAGES segments without user action.
 * On segment cap (IgApiError code=limit), pauses then resumes from persisted checkpoints.
 */
const MAX_SEGMENT_PAUSES = 30; // safety cap (~30 × 12k users per list)

export async function fetchListComplete(kind, uid, session, listOpts, readPartialsFn, { fetchFn = fetchAllUsers } = {}) {
  let segmentPauses = 0;
  let lastFetched = -1;
  while (true) {
    try {
      const partials = await readPartialsFn(uid);
      const resume = partials[kind];
      return await fetchFn(kind, uid, session, listOpts(kind, resume));
    } catch (err) {
      if (!(err instanceof IgApiError) || err.code !== 'limit') throw err;
      const partials = await readPartialsFn(uid);
      const resume = partials[kind];
      const fetched = resume && Array.isArray(resume.users) ? resume.users.length : 0;
      // Repeated cursor / no progress — terminal (don't pause-loop forever).
      if (fetched === lastFetched) throw err;
      lastFetched = fetched;
      segmentPauses += 1;
      if (segmentPauses >= MAX_SEGMENT_PAUSES) {
        throw new IgApiError(
          'limit',
          `Lista muito grande — sincronização parou após ${fetched.toLocaleString('pt-BR')} contas (progresso guardado; tente de novo mais tarde).`,
        );
      }
      const pauseMs = segmentPauseMsForTests ? segmentPauseMsForTests() : segmentPauseMs();
      const partialsNow = await readPartialsFn(uid);
      const resumeNow = partialsNow[kind];
      const followingFetched = partialsNow.following && Array.isArray(partialsNow.following.users)
        ? partialsNow.following.users.length : 0;
      const followersFetched = partialsNow.followers && Array.isArray(partialsNow.followers.users)
        ? partialsNow.followers.users.length : 0;
      await setState({
        syncProgress: {
          phase: kind,
          fetched,
          followingFetched,
          followersFetched,
          segmentPause: true,
          resumeAt: Date.now() + pauseMs,
        },
      });
      await chrome.alarms.clear(CONTINUE_ALARM);
      await chrome.alarms.create(CONTINUE_ALARM, { delayInMinutes: Math.max(0.05, pauseMs / 60000) });
      await sleep(pauseMs);
      await chrome.alarms.clear(CONTINUE_ALARM);
      const partialsAfter = await readPartialsFn(uid);
      const followingAfter = partialsAfter.following && Array.isArray(partialsAfter.following.users)
        ? partialsAfter.following.users.length : 0;
      const followersAfter = partialsAfter.followers && Array.isArray(partialsAfter.followers.users)
        ? partialsAfter.followers.users.length : 0;
      await setState({ syncProgress: { phase: kind, fetched, followingFetched: followingAfter, followersFetched: followersAfter } });
    }
  }
}

function scheduleErrorRetry(code) {
  if (!TRANSIENT_CODES.has(code)) return;
  // retryN read-modify-write must live inside the lock or overlapping
  // schedules restart the escalation ladder. The retry alarm is armed only
  // after the increment lands, so a failed write leaves no orphan timer.
  let n = 0;
  patchState((st) => {
    n = Math.min(Number(st.retryN) || 0, RETRY_DELAYS_MIN.length - 1);
    return { retryN: n + 1 };
  })
    .then(() => chrome.alarms.create(RETRY_ALARM, { delayInMinutes: RETRY_DELAYS_MIN[n] }))
    .catch(() => {});
}

async function sync(trigger) {
  if (runningSync) return runningSync;
  const run = (async () => {
    const t0 = Date.now();
    const trig = trigger || 'manual';
    try {
      // Hunt V4: triggers must never grant consent for the user. Consent is
      // recorded ONLY on an explicit user action (a manual sync from the
      // dashboard — the button IS the consent gesture). Automatic triggers
      // without consent skip entirely.
      const settings0 = await getSettings();
      if (trig === 'manual') {
        const stCd = await getState();
        // Hunt V2 (r2): the gate applies to BOTH terminal statuses, with the
        // right anchor and scale for each. After a FAILURE the anchor is
        // lastAttemptAt (stamped at every sync start AND in the catch) and
        // the scale is the minimal 5 min (counters 0): reopen-spam cannot
        // restart walks, and one network hiccup never locks the user out for
        // the PREVIOUS success's (up to 45 min) cooldown. After SUCCESS the
        // full list-scaled cooldown applies, with the one free-refresh
        // escape hatch consumed here and re-granted only by a real success.
        const failed = stCd.status === 'error';
        const anchor = failed ? stCd.lastAttemptAt : stCd.lastSyncAt;
        const hadFree = !failed && !!stCd.freeManualRefresh;
        const cd = manualSyncCooldownInfo(anchor, {
          followersCount: failed ? 0 : stCd.followersCount,
          followingCount: failed ? 0 : stCd.followingCount,
          freeRefreshPending: hadFree,
        });
        if (cd.blocked) {
          return {
            ok: false,
            skipped: 'cooldown',
            waitMinutes: cd.waitMinutes,
            nextSyncAt: cd.nextSyncAt,
          };
        }
        if (hadFree) await setState({ freeManualRefresh: false });
      }
      if (!settings0.consentAt && trig !== 'manual') {
        return { ok: false, skipped: 'no-consent' };
      }
      if (!settings0.consentAt && trig === 'manual') {
        await saveSettings({ ...settings0, consentAt: Date.now() });
      }
      // Cancel any pending auto-retry — a fresh manual/alarm attempt supersedes it.
      await chrome.alarms.clear(RETRY_ALARM);
      await chrome.alarms.clear(CONTINUE_ALARM);
      await setState({ status: 'syncing', trigger: trig, error: null, syncProgress: null, lastAttemptAt: nowIso() });
      await pinSyncTab(true, { hint: true });
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
      if (!uid && !username) {
        throw new IgApiError('not-logged-in', 'Não encontrei seu ID de usuário. Abra instagram.com logado.');
      }
      // ALWAYS resolve the own profile, even when uid+username are known:
      // besides confirming identity, this is the completeness oracle — the
      // declared follower_count/following_count is the only way to detect a
      // list Instagram returned truncated-but-"completed" (the "só 20
      // pessoas" bug). One extra request per sync, through the same
      // humanized page transport.
      const info = uid
        ? await resolveOwnUser(null, session, uid)
        : await resolveOwnUser(username, session);
      uid = info.uid;
      username = info.username;
      if (uid) await setState({ ownUserId: String(uid) });
      if (username) await setState({ ownUsername: username });

      // Lists run SEQUENTIALLY — parallel walks roughly double request pressure
      // on Instagram and are easier to flag as automation. Progress is persisted
      // per page; each page is checkpointed so a failure/restart resumes.
      const listAbort = new AbortController();
      const progress = makeListProgressTracker();
      const expected = {
        following: Number.isFinite(info.followingCount) ? info.followingCount : null,
        followers: Number.isFinite(info.followerCount) ? info.followerCount : null,
      };
      // NOTE (reuse): when `following` is reused there is deliberately NO
      // following-side completeness check to run — the walk never happened, so
      // there is nothing to compare against the declared count. The gate below
      // already demanded declaredFollowingCount === stored map size, which is
      // the same signal the oracle would have checked, so this is not a hole.
      const listOpts = (kind, resume) => ({
        signal: listAbort.signal,
        resume,
        expectedCount: expected[kind],
        onProgress: (payload) => progress.onProgress(payload),
        onPart: ({ seq, maxId, users }) => savePagePart(kind, uid, seq, maxId, users),
      });
      // The progress tracker persists the growing lists under the
      // authoritative keys for live dashboard progress. If the walk then
      // fails, those keys hold a TRUNCATED list — snapshot them first and
      // restore on failure, so a short list never survives a failed sync as
      // the displayed baseline (a SW death mid-walk can still leave them
      // short until the next completed sync; the diff/notify path reads
      // prevFollowers, which only a COMPLETE walk ever writes).
      const prewalk = await chrome.storage.local.get([K.followers, K.following, K.snapshotUid]);
      // --- bounded `following` reuse (see shouldReuseFollowingList) ---
      // The following walk is the one list whose data is already authoritative
      // while it sits in storage: our own follow/unfollow/remove_follower
      // actions are intercepted live by friendship_hook.js and applied to
      // K.following by recordFriendshipAction(). So a 20k account that syncs
      // several times a day does not have to re-walk ~830 pages of `following`
      // every time — ONLY when the gate's signals all agree.
      // `followers` is NEVER reused or shortened: other people's actions have
      // no local signal, so it is walked in full on every sync.
      const partials0 = await readPartials(uid);
      const reuseFollowing = !partials0.following && !partials0.followers && shouldReuseFollowingList({
        // Fresh baseline / switched account => no snapshotUid (or a different
        // one) => gate fails closed, exactly as rule 1 requires.
        snapshotUid: prewalk[K.snapshotUid] ?? null,
        currentUid: uid,
        followingWalkedAt: st0.followingWalkedAt ?? null,
        declaredFollowingCount: info.followingCount,
        storedFollowingSize: Object.keys(prewalk[K.following] || {}).length,
      });
      // Resume partials present (either list) means the last sync never
      // finished cleanly: walk BOTH lists so the state stops being inherited,
      // and let the existing checkpoint resume do its job for `following`.
      let following;
      let followers;
      try {
        if (reuseFollowing) {
          // No `following` request at all — the stored object IS the walk
          // result, in the exact Map shape fetchAllUsers returns.
          following = new Map(Object.entries(prewalk[K.following] || {}));
        } else {
          following = await fetchListComplete('following', uid, session, listOpts, readPartials);
        }
        // ALWAYS walked, in full, first page to last cursor.
        followers = await fetchListComplete('followers', uid, session, listOpts, readPartials);
      } catch (err) {
        listAbort.abort();
        await progress.flush().catch(() => {}); // no tracker write may land after the restore
        await restoreListsAfterFailedWalk(prewalk);
        throw err;
      }

      const stored = await chrome.storage.local.get([K.prevFollowers, K.history, K.snapshotUid]);
      const snapshot = processSyncSnapshot({
        storedSnapshotUid: stored[K.snapshotUid] ?? null,
        currentUid: uid,
        prev: stored[K.prevFollowers] || {},
        followers,
        following,
        history: stored[K.history] || {},
      });

      // The final lists, baseline, history and events all describe ONE
      // snapshot and MUST land together: a SW death between the list write
      // and the baseline write left the next sync diffing a NEW list
      // against an OLD prev — mass duplicate unfollow events.
      const now = Date.now();

      // --- prepend events (newest first), cap at EVENTS_MAX ---
      const storedEvents = (await chrome.storage.local.get(K.events))[K.events] || [];
      const allEvents = snapshot.freshBaseline
        ? storedEvents
        : mergeEvents(snapshot.events, storedEvents, EVENTS_MAX);

      const prevFollowersObj = stored[K.prevFollowers] || {};
      const hasPrevSnapshot = Object.keys(prevFollowersObj).length > 0;
      const storedNewFollowers = (await chrome.storage.local.get(K.newFollowers))[K.newFollowers] || [];
      let allNewFollowers;
      if (snapshot.freshBaseline || !hasPrevSnapshot) {
        allNewFollowers = [];
      } else {
        const newFollowerBatch = detectNewFollowers(prevFollowersObj, followers, now);
        allNewFollowers = mergeEvents(newFollowerBatch, storedNewFollowers, EVENTS_MAX);
      }
      // One-time: clear bogus "everyone is new" flood from first sync with empty prev.
      const FIX_KEY = 'igf.fixNewFollowersBaseline';
      const fixDone = (await chrome.storage.local.get(FIX_KEY))[FIX_KEY];
      if (!fixDone) {
        const followerKeys = new Set(followers.keys());
        if (
          storedNewFollowers.length > 0
          && storedNewFollowers.length >= followerKeys.size
          && storedNewFollowers.every((e) => followerKeys.has(e.username))
        ) {
          allNewFollowers = [];
        }
        await chrome.storage.local.set({ [FIX_KEY]: true });
      }

      const persist = {
        [K.followers]: Object.fromEntries(followers),
        [K.prevFollowers]: Object.fromEntries(followers),
        [K.history]: snapshot.newHistory,
        [K.events]: allEvents,
        [K.newFollowers]: allNewFollowers,
      };
      // Reuse: the stored map is byte-for-byte what we just used as `following`
      // (built from it, and nothing wrote K.following during this sync) — so
      // skip the 20k-key rewrite. It is the whole point of the reuse path.
      if (!reuseFollowing) persist[K.following] = Object.fromEntries(following);
      if (snapshot.snapshotUid) persist[K.snapshotUid] = snapshot.snapshotUid;
      await chrome.storage.local.set(persist);
      await clearPartials(); // sync complete — no resume needed anymore
      await chrome.alarms.clear(CONTINUE_ALARM);

      // --- notifications for people we still follow who stopped following us ---
      const notifyable = snapshot.notifyable;
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
        // The oracle only proves completeness when BOTH declared counts were
        // available; without them the walk is unverified — say so honestly
        // instead of claiming "listas completas".
        incomplete: expected.following == null || expected.followers == null,
        retryN: 0,
        freeManualRefresh: true,
        followingReused: reuseFollowing,
        // Only a REAL completed walk re-stamps the age signal. On reuse the
        // original timestamp must survive untouched, or the 7-day window would
        // slide forward forever and the list would never be re-walked.
        ...(reuseFollowing ? {} : { followingWalkedAt: nowIso() }),
      });
      return { ok: true, following: following.size, followers: followers.size, notFollowingBack: notFollowingBack.length, newEvents: snapshot.events.length };
      });
    } catch (err) {
      const msg = err instanceof IgApiError ? err.message : String(err && err.message || err).slice(0, 200);
      const code = err instanceof IgApiError ? err.code : null;
      const incomplete = code === 'limit' || code === 'incomplete';
      await setState({ status: 'error', error: msg, syncProgress: null, errorCode: code, incomplete, lastAttemptAt: nowIso() });
      scheduleErrorRetry(code); // transient only — login/checkpoint/gate never auto-retry
      return { ok: false, error: msg };
    } finally {
      runningSync = null;
    }
  })();
  runningSync = run;
  return runningSync;
}

/**
 * Resolve own user id + username + DECLARED follower/following counts.
 * The counts feed fetchAllUsers' completeness oracle — a list that finishes
 * materially short of the declared count is truncated, never accepted.
 */
export async function resolveOwnUser(username, session, knownUid) {
  if (knownUid) {
    // /api/v1/users/{pk}/info/ echoes the profile (no username needed).
    // Classified + retried: a transient blip on the sync's FIRST step used to
    // kill the whole sync with zero retries (raw fetch, no classification).
    const body = await transientRetry(() => apiFetch(`/api/v1/users/${knownUid}/info/`, session));
    const u = body && body.user;
    if (u && u.username) {
      // Declared counts are the oracle's only input — Number.isFinite
      // already maps a missing key to null (falsy counts stay honest).
      let fBy = Number.isFinite(u.follower_count) ? u.follower_count : null;
      let f = Number.isFinite(u.following_count) ? u.following_count : null;
      if (fBy == null || f == null) {
        // /users/{pk}/info/ often omits the counts — without them the oracle
        // is silently inert and a truncated walk is accepted. ONE
        // web_profile_info lookup fills ONLY the missing side(s);
        // best-effort: on failure keep what was found (the sync then honestly
        // reports incomplete instead of dying on its first step).
        try {
          const params = new URLSearchParams({ username: u.username });
          const web = await transientRetry(() =>
            apiFetch(`/api/v1/users/web_profile_info/?${params.toString()}`, session));
          const du = web && web.data && web.data.user;
          if (du) {
            if (fBy == null) {
              const wfBy = du.edge_followed_by && Number(du.edge_followed_by.count);
              if (Number.isFinite(wfBy)) fBy = wfBy;
            }
            if (f == null) {
              const wf = du.edge_follow && Number(du.edge_follow.count);
              if (Number.isFinite(wf)) f = wf;
            }
          }
        } catch { /* keep partial counts — incomplete beats a dead sync */ }
      }
      return {
        uid: knownUid,
        username: u.username,
        followerCount: fBy,
        followingCount: f,
      };
    }
    throw new IgApiError('http', 'Instagram respondeu com uma resposta inesperada.');
  }
  if (username) {
    const params = new URLSearchParams({ username });
    const body = await transientRetry(() => apiFetch(`/api/v1/users/web_profile_info/?${params.toString()}`, session));
    const du = body && body.data && body.data.user;
    if (du && du.username) {
      const fBy = du.edge_followed_by && Number(du.edge_followed_by.count);
      const f = du.edge_follow && Number(du.edge_follow.count);
      return {
        uid: String(du.id),
        username: du.username,
        followerCount: Number.isFinite(fBy) ? fBy : null,
        followingCount: Number.isFinite(f) ? f : null,
      };
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
  // 'igf-sum-' prefix: the click handler's /^igf-uf-(.+?)-\d+$/ must NOT
  // match the summary (it used to open instagram.com/summary/ — a 404).
  await chrome.notifications.create(`igf-sum-${Date.now()}`, {
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

async function resumeInterruptedSync() {
  try {
    if (runningSync) return;
    const meta = (await chrome.storage.local.get(PART_META))[PART_META];
    if (!meta || !meta.keys || !meta.keys.length) return;
    sync('resume');
  } catch { /* best-effort */ }
}

async function scheduleAlarm() {
  const s = await getSettings();
  await chrome.alarms.clear(SYNC_ALARM);
  if (s.autoSync) {
    await chrome.alarms.create(SYNC_ALARM, { periodInMinutes: Math.max(180, Number(s.refreshMinutes) || 180) });
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== SYNC_ALARM && alarm.name !== RETRY_ALARM && alarm.name !== CONTINUE_ALARM) return;
  // Background syncs run only while an IG tab exists — the requests must
  // LOOK like page requests (see pageTransport). No IG tab open = nobody is
  // on Instagram = skip; the next panel open / alarm with a tab handles it.
  try {
    const tabs = await chrome.tabs.query({ url: 'https://www.instagram.com/*' });
    if (!tabs.length) {
      // No IG tab = no page transport. A one-shot retry consumed here would
      // be silently lost until the next periodic alarm (never, if autoSync
      // is off) — re-arm it cheaply: a later wake-up, zero IG traffic.
      if (alarm.name === RETRY_ALARM || alarm.name === CONTINUE_ALARM) {
        await chrome.alarms.create(alarm.name, { delayInMinutes: 15 });
      }
      return;
    }
  } catch {
    return;
  }
  const trig = alarm.name === SYNC_ALARM ? 'alarm' : (alarm.name === CONTINUE_ALARM ? 'continue' : 'retry');
  sync(trig);
});

chrome.runtime.onInstalled.addListener(async (details) => {
  await scheduleAlarm();
  // Hunt V4 (O-4): installing is NOT consent. consentAt stays null until the
  // user's first explicit sync from the dashboard. Nothing automatic touches
  // Instagram before that, and no tab is opened on install either.
});

chrome.runtime.onStartup.addListener(async () => {
  await scheduleAlarm();
  const meta = (await chrome.storage.local.get(PART_META))[PART_META];
  const hasPartials = !!(meta && meta.keys && meta.keys.length);
  if (hasPartials) {
    // Resume is an automatic sync: same tab gate as alarms — without it
    // ensureIgTab() would open instagram.com by itself on browser boot.
    if (await hasOpenIgTab()) await resumeInterruptedSync();
    return;
  }
  const s = await getSettings();
  if (s.autoSync && s.consentAt && (await hasOpenIgTab())) sync('startup');
});


async function recordFriendshipAction({ action, pk, username } = {}) {
  const act = String(action || '').trim();
  if (!act || !pk) return { ok: false, skipped: 'invalid' };
  const stored = await chrome.storage.local.get([
    K.following, K.followers, K.history, K.events, K.newFollowers, K.state,
  ]);
  const result = applyFriendshipAction(
    act,
    stored[K.following] || {},
    stored[K.followers] || {},
    stored[K.history] || {},
    { pk, username },
  );
  if (!result) return { ok: false, skipped: 'unknown' };
  const persist = {
    [K.following]: result.followingObj,
    [K.followers]: result.followersObj,
  };
  if (result.historyObj) persist[K.history] = result.historyObj;
  if (result.newFollowers && result.newFollowers.length) {
    const storedNew = stored[K.newFollowers] || [];
    persist[K.newFollowers] = mergeEvents(result.newFollowers, storedNew, EVENTS_MAX);
  }
  await chrome.storage.local.set(persist);
  await setState({
    followingCount: result.followingCount,
    followersCount: result.followersCount,
    notFollowingBackCount: result.notFollowingBackCount,
  });
  return { ok: true, action: act, username: result.username };
}

// ---------------------------------------------------------------------------
// Messages (dashboard popup / panel -> background)
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!sender || sender.id !== chrome.runtime.id) return false; // defense in depth: only own contexts
  if (!msg || !msg.type || !msg.type.startsWith('igf-')) return false;
  if (msg.type === 'igf-sync') {
    sync(msg.trigger || 'manual').then(sendResponse);
    return true;
  }
  if (msg.type === 'igf-get-own') {
    // Logged-in profile, resolved from the session at runtime (never hardcoded).
    // Hunt V3 (half-2): this handler runs on EVERY IG tab the FAB lives
    // on, and the old path used withPageTransport(), which auto-OPENS
    // instagram.com via ensureIgTab() when no tab answers — an
    // extension-opened tab is an automation fingerprint and a per-tab
    // identity probe. So resolve ONLY from storage here; if unresolved,
    // ask the user to open IG once. (sync() keeps its own transport +
    // tab policy.)
    (async () => {
      const st = await getState();
      if (st.ownUsername) {
        sendResponse({ ok: true, username: st.ownUsername, uid: st.ownUserId ? String(st.ownUserId) : null });
        return;
      }
      sendResponse({ ok: false, error: 'Abra instagram.com uma vez para o FollowGuard identificar seu perfil.' });
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
  if (msg.type === 'igf-delete-all') {
    (async () => {
      await deleteAllData();
      await scheduleAlarm();
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (msg.type === 'igf-export-backup') {
    (async () => {
      try {
        const backup = await exportBackup();
        sendResponse({ ok: true, backup });
      } catch (err) {
        sendResponse({ ok: false, error: String(err && err.message || err) });
      }
    })();
    return true;
  }
  if (msg.type === 'igf-import-backup') {
    (async () => {
      try {
        await importBackup(msg.backup);
        await scheduleAlarm();
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: String(err && err.message || err) });
      }
    })();
    return true;
  }
  if (msg.type === 'igf-friendship-action') {
    recordFriendshipAction({ action: msg.action, pk: msg.pk, username: msg.username }).then(sendResponse);
    return true;
  }
  if (msg.type === 'igf-manual-unfollow') {
    recordFriendshipAction({ action: 'unfollow', pk: msg.pk, username: msg.username }).then(sendResponse);
    return true;
  }
  return false;
});

chrome.notifications.onClicked.addListener((id) => {
  if (id.startsWith('igf-sum-')) {
    // Summary toast: open the dashboard (the list of who left), not a profile.
    chrome.tabs.create({ url: chrome.runtime.getURL('popup.html') });
    chrome.notifications.clear(id);
    return;
  }
  const m = id.match(/^igf-uf-(.+?)-\d+$/);
  if (m) {
    chrome.tabs.create({ url: `https://www.instagram.com/${encodeURIComponent(m[1])}/` });
    chrome.notifications.clear(id);
  }
});

// Initial alarm (SW may restart without onInstalled).
scheduleAlarm().catch(() => {});

// SW restart: resume checkpoints when possible; otherwise clear stale spinner.
(async () => {
  try {
    const meta = (await chrome.storage.local.get(PART_META))[PART_META];
    const hasPartials = !!(meta && meta.keys && meta.keys.length);
    if (hasPartials) {
      // Same gate as onStartup: no IG tab open = nobody is on Instagram,
      // and resume would auto-open a tab via ensureIgTab(). Skip; the next
      // alarm or panel open handles it.
      if (await hasOpenIgTab()) await resumeInterruptedSync();
      return;
    }
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
