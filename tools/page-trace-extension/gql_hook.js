// MAIN-world hook: capture the clip editor's real GQL + history.state.
// Isolated-world tracing never sees fetch bodies, which is why the previous
// trace log could not explain the 30s-default / 99% hang.
(() => {
  if (window.__vodripGqlHook) return;
  window.__vodripGqlHook = true;

  const sentOps = new Set();
  const send = (event, data = {}) => {
    // MAIN world has no chrome.runtime. Isolated trace.js relays this.
    try {
      window.postMessage({ source: 'vodrip-trace', event, data }, '*');
    } catch { /* ignore */ }
  };

  const CLIP_HINT = /clip|rawMedia|raw_media|renderJob|render_job|offsetSeconds|clipOffset|startOffset|endOffset|durationSeconds|CreateClip|createClip/i;
  const KEEP = new Set([
    'operationName', 'vodID', 'vodId', 'broadcastID', 'offsetSeconds',
    'clipOffsets', 'startOffset', 'endOffset', 'durationSeconds', 'duration',
    'rawMediaID', 'rawMediaId', 'title', 'slug', 'id', 'clipID', 'clipSlug',
    'sourceURL', 'status', 'error', 'message',
  ]);

  const pick = (value, acc, depth) => {
    if (value == null || depth > 8) return acc;
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 12)) pick(item, acc, depth + 1);
      return acc;
    }
    if (typeof value !== 'object') return acc;
    for (const [key, child] of Object.entries(value)) {
      if (KEEP.has(key) || /offset|duration|clip|vod|rawMedia|slug/i.test(key)) {
        if (child && typeof child === 'object' && !Array.isArray(child)) {
          acc[key] = pick(child, {}, depth + 1);
        } else if (Array.isArray(child)) {
          acc[key] = child.slice(0, 8).map((item) => (
            item && typeof item === 'object' ? pick(item, {}, depth + 1) : item
          ));
        } else if (typeof child === 'string') {
          acc[key] = child.length > 160 ? `${child.slice(0, 160)}…` : child;
        } else if (typeof child === 'number' || typeof child === 'boolean' || child == null) {
          acc[key] = child;
        }
      } else {
        pick(child, acc, depth + 1);
      }
    }
    return acc;
  };

  const namesOf = (payload) => {
    const names = [];
    const walk = (node) => {
      if (!node) return;
      if (Array.isArray(node)) { node.forEach(walk); return; }
      if (typeof node !== 'object') return;
      if (typeof node.operationName === 'string') names.push(node.operationName);
      walk(node.extensions);
    };
    walk(payload);
    return names;
  };

  const clipRelated = (payload, raw) => (
    CLIP_HINT.test(raw) || namesOf(payload).some((name) => CLIP_HINT.test(name))
  );

  const parseBody = (body) => {
    if (body == null) return null;
    if (typeof body === 'string') {
      try { return JSON.parse(body); } catch { return { raw: body.slice(0, 240) }; }
    }
    if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
      const text = body.toString();
      try { return JSON.parse(text); } catch { return { raw: text.slice(0, 240) }; }
    }
    return null;
  };

  const reportGql = (direction, payload, extra = {}) => {
    const raw = (() => {
      try { return JSON.stringify(payload); } catch { return ''; }
    })();
    const names = namesOf(payload);
    const related = clipRelated(payload, raw);
    if (!related) {
      for (const name of names) {
        if (sentOps.has(name)) continue;
        sentOps.add(name);
        send('gql_op', { operationName: name });
      }
      return;
    }
    send('gql', {
      direction,
      operationName: names[0] || extra.operationName || null,
      operations: names.slice(0, 8),
      fields: pick(payload, {}, 0),
      ...extra,
    });
  };

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = String(
      (init && init.method) || (input && input.method) || 'GET',
    ).toUpperCase();
    if (/gql\.twitch\.tv/i.test(url) && method === 'POST') {
      try {
        reportGql('req', parseBody(init && init.body));
      } catch { /* never break Twitch */ }
    }
    const result = origFetch.apply(this, arguments);
    if (/gql\.twitch\.tv/i.test(url) && method === 'POST' && result && typeof result.then === 'function') {
      result.then((res) => {
        try {
          res.clone().text().then((text) => {
            try { reportGql('res', JSON.parse(text), { status: res.status }); }
            catch { /* non-JSON */ }
          }).catch(() => {});
        } catch { /* ignore */ }
      }).catch(() => {});
    }
    return result;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__vodripUrl = String(url || '');
    this.__vodripMethod = String(method || 'GET').toUpperCase();
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (/gql\.twitch\.tv/i.test(this.__vodripUrl || '') && this.__vodripMethod === 'POST') {
      try { reportGql('req', parseBody(body), { via: 'xhr' }); } catch { /* ignore */ }
      this.addEventListener('load', () => {
        try { reportGql('res', JSON.parse(this.responseText || 'null'), { via: 'xhr', status: this.status }); }
        catch { /* ignore */ }
      });
    }
    return origSend.apply(this, arguments);
  };

  const clipOffsetsFromHistory = (state) => {
    try {
      const raw = JSON.stringify(state || {});
      const match = raw.match(/"clipOffsets"\s*:\s*\{[^}]{0,240}\}/);
      return match ? match[0] : (raw.includes('clipOffsets') ? 'present' : null);
    } catch {
      return 'unreadable';
    }
  };

  const reportHistory = (how, state, url) => {
    const offsets = clipOffsetsFromHistory(state);
    if (!offsets && how !== 'load') return;
    send('history', {
      how,
      url: String(url || location.href).slice(0, 240),
      clipOffsets: offsets,
    });
  };

  const origReplace = history.replaceState;
  const origPush = history.pushState;
  history.replaceState = function (state, title, url) {
    try { reportHistory('replaceState', state, url); } catch { /* ignore */ }
    return origReplace.apply(this, arguments);
  };
  history.pushState = function (state, title, url) {
    try { reportHistory('pushState', state, url); } catch { /* ignore */ }
    return origPush.apply(this, arguments);
  };
  window.addEventListener('popstate', (event) => {
    try { reportHistory('popstate', event.state, location.href); } catch { /* ignore */ }
  });
  reportHistory('load', history.state, location.href);
})();
