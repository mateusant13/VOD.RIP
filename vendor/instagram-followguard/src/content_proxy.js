// IG FollowGuard — page-context API proxy (SOTA humanization).
//
// Executes the sync's HTTP requests from THIS tab via same-origin fetch, so
// every request carries the browser's REAL request headers (sec-fetch-site:
// same-origin, real User-Agent, sec-ch-ua, accept-language, referer) and
// native cookies. A fetch from the service worker would advertise
// `sec-fetch-site: none`, a forged cookie header and a fabricated UA — a
// fingerprint no real browser ever produces. Through this proxy the request
// is byte-identical to the web app's own: same origin, same headers, same
// session, same browser profile.
//
// Security: allowlisted paths only (the follow-relationship reads the sync
// needs — never DMs, never posts), and messages are accepted ONLY from this
// extension's own contexts (sender.id check); the page itself cannot reach
// this listener.
'use strict';

(() => {
  const IG_WEB_APP_ID = '936619743392459'; // www.instagram.com web client app id
  const ALLOWED_PATH = /^\/api\/v1\/(friendships\/[^/]+\/(following|followers)\/|users\/[^/]+\/info\/|users\/web_profile_info\/)/;
  const TIMEOUT_MS = 20000; // same per-request timeout policy as the SW

  const readCookie = (name) => {
    const m = document.cookie.split('; ').find((c) => c.startsWith(name + '='));
    return m ? m.slice(name.length + 1) : null;
  };

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || sender.id !== chrome.runtime.id) return;
    if (msg.igf === 'ping') { sendResponse({ pong: true }); return; }
    if (msg.igf !== 'fetch') return;
    if (!ALLOWED_PATH.test(msg.path || '')) {
      sendResponse({ ok: false, error: 'forbidden' });
      return;
    }
    // Only headers the web app itself sends on these calls; everything else
    // (UA, sec-fetch-*, accept-language, referer, cookies) comes from the
    // browser natively — never fabricated.
    const headers = {
      'x-ig-app-id': IG_WEB_APP_ID,
      'x-requested-with': 'XMLHttpRequest',
    };
    const csrf = readCookie('csrftoken');
    if (csrf) headers['x-csrftoken'] = csrf;
    const claim = readCookie('x-ig-www-claim');
    if (claim) headers['x-ig-www-claim'] = claim;

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    fetch(`https://www.instagram.com${msg.path}`, {
      method: 'GET',
      credentials: 'include',
      headers,
      signal: ctrl.signal,
    })
      .then((r) => r.text().then((text) => sendResponse({ ok: true, status: r.status, text })))
      .catch((e) => {
        // Abort = IG held the connection past the timeout (gate behavior);
        // anything else = blocked/offline (e.g. an adblocker). Both are
        // transient in the SW's classification.
        sendResponse({ ok: false, error: e && e.name === 'AbortError' ? 'timeout' : 'network' });
      })
      .finally(() => clearTimeout(timer));
    return true; // async sendResponse
  });
})();
