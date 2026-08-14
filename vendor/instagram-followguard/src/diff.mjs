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
  return [...newEvents.reverse(), ...storedEvents].slice(0, max);
}
