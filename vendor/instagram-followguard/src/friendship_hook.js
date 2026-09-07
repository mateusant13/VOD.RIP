// IG FollowGuard — page-world fetch/XHR hook (loaded via friendship_watch.js).
// Must be web_accessible; runs in instagram.com page context, not extension world.
(() => {
  if (window.__igfFriendshipHook) return;
  window.__igfFriendshipHook = true;
  const RULES = [
    { action: 'unfollow', re: /\/api\/v1\/friendships\/(?:destroy|unfollow)\/([^/?#]+)/i },
    { action: 'follow', re: /\/api\/v1\/friendships\/create\/([^/?#]+)/i },
    { action: 'remove_follower', re: /\/api\/v1\/friendships\/remove_follower\/([^/?#]+)/i },
    { action: 'approve', re: /\/api\/v1\/friendships\/approve\/([^/?#]+)/i },
    { action: 'block', re: /\/api\/v1\/friendships\/block\/([^/?#]+)/i },
  ];
  const notify = (url) => {
    try {
      const s = String(url || '');
      for (const { action, re } of RULES) {
        const m = s.match(re);
        if (m && m[1]) {
          window.postMessage({ type: 'igf-friendship', action, pk: m[1] }, '*');
          return;
        }
      }
    } catch {}
  };
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function(input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      notify(url);
      return origFetch.apply(this, arguments);
    };
  }
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const open = XHR.prototype.open;
    XHR.prototype.open = function(method, url) {
      this.__igfUrl = url;
      return open.apply(this, arguments);
    };
    const send = XHR.prototype.send;
    XHR.prototype.send = function() {
      notify(this.__igfUrl);
      return send.apply(this, arguments);
    };
  }
})();
