const API = 'http://127.0.0.1:7897/api/debug/clip-events';
const TRACE_PREFIX = 'trace_';
const pending = new Map();

const pageKind = (url) => {
  try {
    const u = new URL(url);
    if (u.hostname === '127.0.0.1' && u.port === '5173') return 'vodrip';
    if (u.hostname.endsWith('twitch.tv')) return u.hostname === 'clips.twitch.tv' ? 'twitch-editor' : 'twitch';
  } catch { /* ignore malformed browser URLs */ }
  return null;
};

const safeUrl = (url) => {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return String(url || '').slice(0, 240);
  }
};

const send = async (event, data = {}) => {
  try {
    await fetch(API, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ src: 'ext', event: `${TRACE_PREFIX}${event}`, data }),
    });
  } catch { /* tracing must never affect the page */ }
};

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (!message || message.type !== 'trace') return;
  const tabId = sender.tab?.id ?? null;
  const tabUrl = sender.tab?.url || message.url || '';
  if (!pageKind(tabUrl)) return;
  void send(message.event || 'event', {
    ...message.data,
    traceId: message.traceId || null,
    tabId,
    page: pageKind(tabUrl),
    url: safeUrl(tabUrl),
  });
  respond?.({ ok: true });
  return true;
});

const clipNet = (url) => /gql\.twitch\.tv/i.test(url);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => {
    if (!clipNet(details.url)) return;
    const kind = pageKind(details.initiator || details.documentUrl || details.url);
    if (!kind) return;
    void send('network_error', {
      error: details.error,
      url: safeUrl(details.url),
      page: kind,
    });
  },
  { urls: ['https://gql.twitch.tv/*'] },
);
