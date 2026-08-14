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
  return null;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== 'ko-resolve-yt') return;
  const url = normalizeYtUrl(msg.value);
  if (!url) {
    sendResponse({ id: null, error: 'bad-url' });
    return;
  }
  fetch(url, { credentials: 'include' })
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
