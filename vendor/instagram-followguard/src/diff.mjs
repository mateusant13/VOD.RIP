// IG FollowGuard — pure follow-diff logic (no chrome APIs; unit-testable).
'use strict';

/**
 * Diff a fresh followers snapshot against the previous one and update the
 * per-user follow history.
 *
 * @param {Object} prev       previous followers map: username -> meta
 * @param {Map}    next       new followers map (username -> meta)
 * @param {Map}    following  current following map (username -> meta)
 * @param {Object} history    per-user follow history
 * @param {number} now        epoch ms
 * @returns {{events: Array, newHistory: Object}}
 *   events: unfollow detections, newest-first ordering applied by caller.
 */
export function diffAndRecord(prev, next, following, history, now) {
  const events = [];
  const newHistory = { ...history };
  for (const [u, meta] of Object.entries(prev)) {
    if (!next.has(u)) {
      const stillFollowing = following.has(u);
      const cur = stillFollowing ? following.get(u) : null;
      const h = newHistory[u] || {};
      const pk = String(meta.pk || h.pk || '');
      const fullName = meta.full_name || h.fullName || '';
      const profilePicUrl = (cur && cur.profile_pic_url) || meta.profile_pic_url || h.profilePicUrl || '';
      events.push({ username: u, pk, fullName, profilePicUrl, detectedAt: now, stillFollowing });
      newHistory[u] = {
        ...h,
        pk,
        fullName,
        profilePicUrl,
        firstFollowedAt: h.firstFollowedAt || null,
        lastFollowedAt: h.lastFollowedAt || null,
        unfollowedAt: now,
        unfollowCount: (h.unfollowCount || 0) + 1,
      };
    }
  }
  for (const [u, meta] of next) {
    const h = newHistory[u] || {};
    newHistory[u] = {
      ...h,
      pk: String(meta.pk || h.pk || ''),
      fullName: meta.full_name || h.fullName || '',
      profilePicUrl: meta.profile_pic_url || h.profilePicUrl || '',
      firstFollowedAt: h.firstFollowedAt || now,
      lastFollowedAt: now,
      unfollowedAt: null,
      unfollowCount: h.unfollowCount || 0,
    };
  }
  return { events, newHistory };
}

/**
 * Prepend new events (newest first), cap at max.
 */
export function mergeEvents(newEvents, storedEvents, max) {
  // Copy before reversing: background passes the same array it later
  // filters/returns — in-place reverse mutated the caller's data.
  const merged = [...newEvents].reverse();
  for (const e of storedEvents) merged.push(e);
  // Dedupe by username keeping the newest detectedAt: the same unfollow
  // re-detected after a baseline reset used to stack duplicate feed rows.
  const byUser = new Map();
  let anon = 0;
  for (const e of merged) {
    const k = e && e.username != null ? `u:${e.username}` : `x:${anon++}`;
    const prev = byUser.get(k);
    if (!prev || ((e.detectedAt || 0) > (prev.detectedAt || 0))) byUser.set(k, e);
  }
  // Map.set keeps the FIRST insertion slot, so a stored winner could render
  // above newer rows under clock skew — sort by detectedAt before capping.
  return [...byUser.values()].sort((a, b) => (b.detectedAt || 0) - (a.detectedAt || 0)).slice(0, max);
}

/**
 * Detect accounts that appeared in the followers list since the last snapshot.
 */
export function detectNewFollowers(prev, next, now) {
  const prevObj = prev || {};
  if (!Object.keys(prevObj).length) return [];
  const events = [];
  const nextMap = next instanceof Map ? next : new Map(Object.entries(next || {}));
  for (const [u, meta] of nextMap) {
    if (!prevObj[u]) {
      events.push({
        username: u,
        pk: String(meta.pk || ''),
        fullName: meta.full_name || meta.fullName || '',
        profilePicUrl: meta.profile_pic_url || meta.profilePicUrl || '',
        detectedAt: now,
      });
    }
  }
  return events;
}

/**
 * Remove one account from the local following map after the user unfollows on IG.
 * Returns null when nothing changed.
 */
export function applyManualUnfollow(following, followers, { pk, username } = {}) {
  const gMap = following instanceof Map ? following : new Map(Object.entries(following || {}));
  const fMap = followers instanceof Map ? followers : new Map(Object.entries(followers || {}));
  let key = username && gMap.has(username) ? username : null;
  if (!key && pk) {
    for (const [u, meta] of gMap) {
      if (meta && String(meta.pk || '') === String(pk)) { key = u; break; }
    }
  }
  if (!key || !gMap.has(key)) return null;
  gMap.delete(key);
  const notFollowingBack = [...gMap.keys()].filter((u) => !fMap.has(u)).length;
  return {
    following: gMap,
    removedUsername: key,
    followingCount: gMap.size,
    followersCount: fMap.size,
    notFollowingBackCount: notFollowingBack,
  };
}

function mapsFromInput(following, followers) {
  const gMap = following instanceof Map ? following : new Map(Object.entries(following || {}));
  const fMap = followers instanceof Map ? followers : new Map(Object.entries(followers || {}));
  return { gMap, fMap };
}

function recomputeCounts(gMap, fMap) {
  const notFollowingBackCount = [...gMap.keys()].filter((u) => !fMap.has(u)).length;
  return { followingCount: gMap.size, followersCount: fMap.size, notFollowingBackCount };
}

/** Resolve username key from pk using stored maps/history. */
export function resolveUsernameByPk(following, followers, history, { pk, username } = {}) {
  const pkStr = pk != null ? String(pk) : '';
  if (!pkStr) return username || null;
  if (username) return username;
  const { gMap, fMap } = mapsFromInput(following, followers);
  for (const [u, meta] of gMap) {
    if (meta && String(meta.pk) === pkStr) return u;
  }
  for (const [u, meta] of fMap) {
    if (meta && String(meta.pk) === pkStr) return u;
  }
  const hist = history || {};
  for (const [u, h] of Object.entries(hist)) {
    if (h && String(h.pk) === pkStr) return u;
  }
  return `id${pkStr}`;
}

function metaFromStores(gMap, fMap, hist, key, pk) {
  const existing = gMap.get(key) || fMap.get(key);
  if (existing) return { ...existing, pk: String(pk), username: key };
  const h = hist[key] || {};
  return {
    pk: String(pk),
    username: key,
    full_name: h.fullName || '',
    profile_pic_url: h.profilePicUrl || '',
    is_private: false,
    is_verified: false,
  };
}

function findFollowerKeyByPk(fMap, pk, fallbackKey) {
  const pkStr = String(pk);
  if (fallbackKey && fMap.has(fallbackKey)) return fallbackKey;
  for (const [u, meta] of fMap) {
    if (meta && String(meta.pk) === pkStr) return u;
  }
  return null;
}

function findFollowingKeyByPk(gMap, pk, fallbackKey) {
  const pkStr = String(pk);
  if (fallbackKey && gMap.has(fallbackKey)) return fallbackKey;
  for (const [u, meta] of gMap) {
    if (meta && String(meta.pk) === pkStr) return u;
  }
  return null;
}

/**
 * Apply a live friendship action detected on instagram.com.
 * Supported: unfollow, follow, remove_follower, approve, block.
 */
export function applyFriendshipAction(action, following, followers, history, { pk, username } = {}, now = Date.now()) {
  const { gMap, fMap } = mapsFromInput(following, followers);
  const hist = { ...(history || {}) };
  const key = resolveUsernameByPk(gMap, fMap, hist, { pk, username });

  switch (action) {
    case 'unfollow': {
      const r = applyManualUnfollow(gMap, fMap, { pk, username: key });
      if (!r) return null;
      return {
        followingObj: Object.fromEntries(r.following),
        followersObj: Object.fromEntries(fMap),
        historyObj: hist,
        username: r.removedUsername,
        ...recomputeCounts(r.following, fMap),
      };
    }
    case 'follow': {
      // A synthetic `id<pk>` key means the account is unknown to every store
      // — writing it would create a phantom dashboard row that can never be
      // reconciled by username. Skip; the next completed sync picks it up.
      if (!key || (!username && key === `id${pk}`)) return null;
      const gKey = findFollowingKeyByPk(gMap, pk, key);
      if (gKey) return null;
      const meta = metaFromStores(gMap, fMap, hist, key, pk);
      gMap.set(key, meta);
      const h = hist[key] || {};
      hist[key] = {
        ...h,
        pk: String(pk),
        fullName: meta.full_name || h.fullName || '',
        profilePicUrl: meta.profile_pic_url || h.profilePicUrl || '',
        firstFollowedAt: h.firstFollowedAt || now,
        lastFollowedAt: now,
        unfollowedAt: null,
        unfollowCount: h.unfollowCount || 0,
      };
      return {
        followingObj: Object.fromEntries(gMap),
        followersObj: Object.fromEntries(fMap),
        historyObj: hist,
        username: key,
        ...recomputeCounts(gMap, fMap),
      };
    }
    case 'remove_follower': {
      const fKey = findFollowerKeyByPk(fMap, pk, key);
      if (!fKey) return null;
      fMap.delete(fKey);
      return {
        followingObj: Object.fromEntries(gMap),
        followersObj: Object.fromEntries(fMap),
        historyObj: hist,
        username: fKey,
        ...recomputeCounts(gMap, fMap),
      };
    }
    case 'approve': {
      if (!key || (!username && key === `id${pk}`)) return null;
      const fKey = findFollowerKeyByPk(fMap, pk, key);
      if (fKey) return null;
      const meta = metaFromStores(gMap, fMap, hist, key, pk);
      fMap.set(key, meta);
      const h = hist[key] || {};
      hist[key] = {
        ...h,
        pk: String(pk),
        fullName: meta.full_name || h.fullName || '',
        profilePicUrl: meta.profile_pic_url || h.profilePicUrl || '',
        firstFollowedAt: h.firstFollowedAt || now,
        lastFollowedAt: now,
        unfollowedAt: null,
        unfollowCount: h.unfollowCount || 0,
      };
      return {
        followingObj: Object.fromEntries(gMap),
        followersObj: Object.fromEntries(fMap),
        historyObj: hist,
        username: key,
        newFollowers: [{
          username: key,
          pk: String(pk),
          fullName: meta.full_name || '',
          profilePicUrl: meta.profile_pic_url || '',
          detectedAt: now,
        }],
        ...recomputeCounts(gMap, fMap),
      };
    }
    case 'block': {
      const fKey = findFollowerKeyByPk(fMap, pk, key);
      const gKey = findFollowingKeyByPk(gMap, pk, key);
      if (!fKey && !gKey) return null;
      if (fKey) fMap.delete(fKey);
      if (gKey) gMap.delete(gKey);
      return {
        followingObj: Object.fromEntries(gMap),
        followersObj: Object.fromEntries(fMap),
        historyObj: hist,
        username: gKey || fKey || key,
        ...recomputeCounts(gMap, fMap),
      };
    }
    default:
      return null;
  }
}
