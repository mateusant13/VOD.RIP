// IG FollowGuard — isolated-world bridge for friendship actions on instagram.com.
// friendship_hook.js runs in MAIN world (manifest); this file forwards postMessage
// events to the extension background.
'use strict';

(() => {
  if (window.__igfFriendshipWatch) return;
  window.__igfFriendshipWatch = true;

  window.addEventListener('message', (ev) => {
    if (ev.source !== window || !ev.data || ev.data.type !== 'igf-friendship') return;
    const action = String(ev.data.action || '').trim();
    const pk = String(ev.data.pk || '').trim();
    if (!action || !pk) return;
    try {
      chrome.runtime.sendMessage({ type: 'igf-friendship-action', action, pk });
    } catch { /* SW asleep — next action will retry */ }
  });
})();
