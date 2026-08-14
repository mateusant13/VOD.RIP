// Kick Overlay — service worker.
// Resolves a YouTube channel URL (@handle / channel/UC... / bare UC...) to
// its UC... channel id by fetching the channel page (host_permission grants
// the cross-origin fetch the content script does not have). The live embed
// (youtube.com/embed/live_stream) only accepts the UC... id, not handles.
'use strict';

const YT_ID_RE = /UC[0-9A-Za-z_-]{22}/;

function normalizeYtUrl(value) {
  let v = (value || '').trim();
  if (!v) return null;
  if (YT_ID_RE.test(v) && v.startsWith('UC')) return `https://www.youtube.com/channel/${v}`;
  const m = v.match(/youtube\.com\/(@[^/?#]+|channel\/UC[0-9A-Za-z_-]{22}|c\/[^/?#]+|user\/[^/?#]+)/i);
  if (m) return `https://www.youtube.com/${m[1]}`;
  if (v.startsWith('@')) return `https://www.youtube.com/${v}`;
  // Bare handle without @ (e.g. "JBSniperPRIME") — the popup hint says
  // "URL, @handle or UC…", but users type the plain name; resolve it anyway.
  if (/^[0-9A-Za-z._-]{2,64}$/.test(v)) return `https://www.youtube.com/@${v}`;
  return null;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.__koDiag) {
    // Diagnostics relay: the content script's [ko] state mirrored to the
    // local diag listener (http://127.0.0.1:9234). An extension-origin
    // no-cors fetch is neither CORS- nor CSP-blocked, so no host_permission
    // is needed. The server answers with an opaque 204; failures are
    // swallowed on purpose.
    // ponytail: debug-only channel; remove once the kick black-screen is
    // root-caused (2026-08-13).
    try {
      const e = encodeURIComponent(String(msg.__koDiag.ev || 'ev'));
      const d = encodeURIComponent(JSON.stringify(msg.__koDiag.data || {}));
      fetch(`http://127.0.0.1:9234/d?e=${e}&d=${d}`, { mode: 'no-cors' }).catch(() => {});
    } catch {
      /* relay must never break the extension */
    }
    sendResponse({ ok: true });
    return false;
  }
  if (!msg || msg.type !== 'ko-resolve-yt') return;
  const url = normalizeYtUrl(msg.value);
  if (!url) {
    sendResponse({ id: null, error: 'bad-url' });
    return;
  }
  // credentials 'omit': the anonymous page carries the UC id (curl-verified);
  // the logged-in variant can redirect to the consent wall and break the match.
  fetch(url, { credentials: 'omit' })
    .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((html) => {
      const m =
        html.match(/"externalId":"(UC[0-9A-Za-z_-]{22})"/) ||
        html.match(/"channelId":"(UC[0-9A-Za-z_-]{22})"/) ||
        html.match(/"browseId":"(UC[0-9A-Za-z_-]{22})"/);
      sendResponse({ id: m ? m[1] : null, error: m ? null : 'not-found' });
    })
    .catch((e) => sendResponse({ id: null, error: String(e).slice(0, 80) }));
  return true; // async sendResponse
});
