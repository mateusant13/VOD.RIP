import getAllCookies from './modules/get_all_cookies.mjs';
import saveToFile from './modules/save_to_file.mjs';
import {
  BRIDGE_DOMAINS,
  createDebouncedPush,
  getApiBase,
  postCookies,
} from './modules/cookie_bridge.mjs';

// Defensive: never surface an uncaught error in the SW console. Every known
// failure mode has its own try/catch; this is the last net for browser
// quirks and extension-API throws (degrades to a yellow warning instead of
// the red "background.js:0 (função anônima)" unhandled error).
self.addEventListener('unhandledrejection', (e) => {
  e.preventDefault();
  console.warn('[vodrip] unhandled rejection (non-fatal):', e.reason);
});
self.addEventListener('error', (e) => {
  e.preventDefault();
  console.warn('[vodrip] uncaught error (non-fatal):', e.error || e.message);
});

/**
 * Update icon badge counter on active page
 */
const updateBadgeCounter = async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) {
      return;
    }
    const { id: tabId, url: urlString } = tab;
    if (!urlString) {
      chrome.action.setBadgeText({ tabId, text: '' });
      return;
    }
    const url = new URL(urlString);
    const cookies = await getAllCookies({
      url: url.href,
      partitionKey: { topLevelSite: url.origin },
    });
    chrome.action.setBadgeText({ tabId, text: cookies.length.toFixed() });
  } catch {
    // tab may have closed between query and badge write — ignore
  }
};

chrome.cookies.onChanged.addListener(updateBadgeCounter);
chrome.tabs.onUpdated.addListener(updateBadgeCounter);
chrome.tabs.onActivated.addListener(updateBadgeCounter);
chrome.windows.onFocusChanged.addListener(updateBadgeCounter);

// ---------------------------------------------------------------------------
// VOD.RIP cookie bridge — keep-listed cookies for kick/youtube/twitch are
// pushed to the local backend on change (300ms debounce). Only the platform
// domains are ever queried; only keep-listed names are ever sent
// (cookie_bridge.mjs filterCookies is the single gate).
// ---------------------------------------------------------------------------
const collectBridgeCookies = async () => {
  const cookies = [];
  for (const domain of BRIDGE_DOMAINS) {
    try {
      // domain match includes subdomains (www., m., leading-dot cookies)
      cookies.push(...(await chrome.cookies.getAll({ domain })));
    } catch {
      // permission revoked or browser quirk — skip this domain
    }
  }
  return cookies;
};

const pushBridgeCookies = createDebouncedPush({
  collect: collectBridgeCookies,
  post: postCookies,
  delayMs: 300,
});
chrome.cookies.onChanged.addListener(pushBridgeCookies);

// A normal navigation is the one safe browser-side freshness signal: it lets
// the bridge immediately capture cookies rotated by the platform without
// reloading or opening a user tab on its own.
chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete' || !tab.url) return;
  try {
    const host = new URL(tab.url).hostname.toLowerCase();
    if (BRIDGE_DOMAINS.some((domain) => host === domain || host.endsWith(`.${domain}`))) {
      pushBridgeCookies();
    }
  } catch {
    // chrome:// and malformed URLs are not bridge pages
  }
});

// ---------------------------------------------------------------------------
// Passive cycle: onChanged only fires while the user browses. A fresh install
// or browser start may already hold platform cookies, and cookies rotate with
// the user logged in — so push once at install/startup, then re-push on a
// 10-minute alarm. The shared debounce collapses any overlap with onChanged
// bursts; the backend upsert is idempotent, so duplicate pushes are harmless.
// ---------------------------------------------------------------------------
const HEARTBEAT_ALARM = 'vodrip-cookie-heartbeat';
const HEARTBEAT_PERIOD_MIN = 10;

const armHeartbeat = () => {
  chrome.alarms.create(HEARTBEAT_ALARM, {
    periodInMinutes: HEARTBEAT_PERIOD_MIN,
  });
};

// ---------------------------------------------------------------------------
// Zero-window version reload. The service worker reloads itself in place via
// chrome.runtime.reload() when the backend has a newer staged copy. It never
// opens, reloads, or otherwise touches user tabs.
// ---------------------------------------------------------------------------
const RELOAD_CHECK_ALARM = 'vodrip-reload-check';
const RELOAD_CHECK_PERIOD_MIN = 0.5;

const armReloadCheck = () => {
  chrome.alarms.create(RELOAD_CHECK_ALARM, {
    periodInMinutes: RELOAD_CHECK_PERIOD_MIN,
  });
};

/** Fresh-SW confirmation: clear the directive only for the matching version. */
const confirmReloadDone = (version) => {
  (async () => {
    try {
      await fetch(`${await getApiBase()}/api/extension/reload-done`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version }),
      });
    } catch {
      // backend offline — the directive stays; the next check retries
    }
  })();
};

// A pending reload is only honored after TWO consecutive status polls
// report the SAME target. The staged folder the user has loaded unpacked
// is overwritten in place by builds; a reload sighted while that write is
// in flight makes Chrome re-fetch background.js from a half-written tree
// and the SW registration dies with "Service worker registration failed.
// Status code: 10" (kErrorNetwork — script fetch failed) until a manual
// reload, because Chrome stops retrying after ~3 backoff attempts. Two
// sightings (alarm period 30s, persisted in chrome.storage so SW idle
// restarts don't reset the count) mean the folder was complete for 30+
// seconds before the reload.
const RELOAD_SEEN_KEY = 'vodrip_reload_target_seen';
const readReloadSeen = async () =>
  ((await chrome.storage.local.get(RELOAD_SEEN_KEY))[RELOAD_SEEN_KEY] ?? null);
const clearReloadSeen = () => chrome.storage.local.remove(RELOAD_SEEN_KEY);

/** Check the directive without opening or reloading any browser page. */
export const checkReloadDirective = async () => {
  let body;
  try {
    const res = await fetch(`${await getApiBase()}/api/extension/status`);
    if (!res.ok) return;
    body = await res.json();
  } catch {
    return; // backend offline — the alarm retries
  }
  if (!body || !body.reloadTo) {
    await clearReloadSeen();
    return;
  }
  const manifestVersion = chrome.runtime.getManifest().version;
  if (body.reloadTo === manifestVersion) {
    await clearReloadSeen();
    confirmReloadDone(manifestVersion);
    return;
  }
  if ((await readReloadSeen()) === body.reloadTo) {
    await clearReloadSeen();
    chrome.runtime.reload();
    return;
  }
  await chrome.storage.local.set({ [RELOAD_SEEN_KEY]: body.reloadTo });
};

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) pushBridgeCookies();
  if (alarm.name === RELOAD_CHECK_ALARM) checkReloadDirective();
});

// Update notification
chrome.runtime.onInstalled.addListener(({ previousVersion, reason }) => {
  armHeartbeat();
  armReloadCheck();
  pushBridgeCookies(); // initial push — don't wait for the first cookie change
  if (reason === 'update') {
    const currentVersion = chrome.runtime.getManifest().version;
    chrome.notifications.create('updated', {
      type: 'basic',
      title: 'VOD RIP Get Cookies',
      message: `Updated from ${previousVersion} to ${currentVersion}`,
      iconUrl: '/images/icon128.png',
    }, () => { /* consume chrome.runtime.lastError (icon missing on some builds) */ });
  }
});

chrome.runtime.onStartup.addListener(() => {
  armHeartbeat();
  armReloadCheck();
  pushBridgeCookies();
});

// Boot-time directive check: a fresh SW (after chrome.runtime.reload())
// must confirm and clear the directive WITHOUT waiting for the first alarm;
// a normal boot with no directive is a single no-op fetch.
checkReloadDirective();

// TODO: use offscreen API to integrate implementation in chrome and firefox
// Save file message listener for firefox
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const { type, target, data } = message || {};
  if (target !== 'background' || type !== 'save') return undefined;
  const { text, name, format, saveAs } = data || {};
  saveToFile(text, name, format, saveAs)
    .then(() => sendResponse('done'))
    .catch((err) => {
      console.warn('[vodrip] save failed:', err);
      sendResponse('error');
    });
  return true;
});

// Clip-assist MAIN-world helper injection: the content script (isolated
// world) cannot see the page's __reactFiber$ expando, and inline script
// tags get blocked by the page CSP/Trusted Types. chrome.scripting with
// world:'MAIN' bypasses the page CSP and runs the helper in React's world.
// The helper drives the editor's window via onLeftDrag/onRightDrag and
// answers vodrip-range-req messages with the confirmed valuetext.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === 'vodrip-inject-main') {
    const tabId = sender && sender.tab && sender.tab.id;
    // Delivery proof for the content script: the sendResponse channel is
    // unreliable next to the legacy async onMessage listener, so the helper
    // reads this storage marker instead (kept minimal on purpose).
    chrome.storage.local.set({ vodrip_sw_inject_msg: Date.now() }).catch(() => {});
    if (tabId == null) { sendResponse({ ok: false, err: 'no-tab' }); return; }
    chrome.scripting
      .executeScript({
        target: { tabId },
        world: 'MAIN',
        func: () => {
          if (window.__vodripRangeReady) return;
          window.__vodripRangeReady = true;
          const pc = (s) => {
            const value = String(s || '').trim();
            const h = /^(\d+):(\d{1,2}):(\d{2})$/.exec(value);
            if (h) return +h[1] * 3600 + +h[2] * 60 + +h[3];
            const m = /^(\d+):(\d{2})$/.exec(value);
            if (m) return +m[1] * 60 + +m[2];
            if (/^\d{1,3}$/.test(value)) return +value;
            return null;
          };
          const urlOffset = () => {
            try {
              const n = Number(new URLSearchParams(location.search).get('offsetSeconds'));
              return Number.isFinite(n) && n > 0 ? n : null;
            } catch {
              return null;
            }
          };
          const historyOffsets = () => {
            try {
              const raw = JSON.stringify(history.state || {});
              const s = raw.match(/"startOffset"\s*:\s*(-?\d+(?:\.\d+)?)/);
              const e = raw.match(/"endOffset"\s*:\s*(-?\d+(?:\.\d+)?)/);
              if (!s || !e) return null;
              return { startOffset: +s[1], endOffset: +e[1] };
            } catch {
              return null;
            }
          };
          const writeClipOffsets = (s, e) => {
            const patch = (obj) => {
              if (!obj || typeof obj !== 'object') return false;
              let hit = false;
              if (obj.clipOffsets && typeof obj.clipOffsets === 'object') {
                obj.clipOffsets.startOffset = s;
                obj.clipOffsets.endOffset = e;
                hit = true;
              }
              for (const val of Object.values(obj)) {
                if (val && typeof val === 'object') hit = patch(val) || hit;
              }
              return hit;
            };
            try {
              const st = history.state ? JSON.parse(JSON.stringify(history.state)) : {};
              if (!patch(st)) st.clipOffsets = { startOffset: s, endOffset: e };
              history.replaceState(st, '', location.href);
              return true;
            } catch {
              return false;
            }
          };
          const toRelative = (start, end, native) => {
            const requestedDur = end - start;
            const uiEnd = (native && native.b > 0 && native.b <= 93) ? native.b : 90;
            const anchor = urlOffset() || end;
            const origin = Math.max(0, anchor - uiEnd);
            let relEnd = end - origin;
            let relStart = start - origin;
            if (relEnd > uiEnd) {
              relStart -= (relEnd - uiEnd);
              relEnd = uiEnd;
            }
            if (relStart < 0) relStart = 0;
            const minLen = Math.min(5, uiEnd);
            if (relEnd - relStart < minLen) relStart = Math.max(0, relEnd - minLen);
            return {
              relStart: Math.round(relStart),
              relEnd: Math.round(relEnd),
              origin,
              uiEnd,
              requestedDur,
              anchor,
            };
          };
          window.addEventListener('message', (ev) => {
            const d = ev.data;
            if (!d || d.source !== 'vodrip-range-req') return;
            const { nonce, start, end } = d;
            const send = (p) => {
              try { window.postMessage(Object.assign({ source: 'vodrip-range-res', nonce }, p), '*'); } catch { /* ignore */ }
            };
            const slider = document.querySelector('[role="slider"]');
            if (!slider) { send({ ok: false, reason: 'slider não encontrado' }); return; }
            const fk = Object.keys(slider).find((k) => k.startsWith('__reactFiber'));
            if (!fk) { send({ ok: false, reason: 'fiber não exposto' }); return; }
            let n = slider[fk];
            let drag = null;
            for (let i = 0; n && i < 40; i++) {
              const p = n.memoizedProps || {};
              if (typeof p.onLeftDrag === 'function' && typeof p.onRightDrag === 'function') {
                drag = p;
                break;
              }
              n = n.return;
            }
            if (!drag) { send({ ok: false, reason: 'controle de trecho não acessível' }); return; }
            const TOL = 2;
            const LEN_TOL = 1;
            const readWindow = () => {
              const vt = (slider.getAttribute('aria-valuetext') || '').trim();
              const parts = vt.split(/\s*to\s*/i);
              if (parts.length !== 2) return null;
              const a = pc(parts[0]);
              const b = pc(parts[1]);
              return a != null && b != null ? { a, b, vt } : null;
            };
            if (!(end > start)) {
              const cur = readWindow();
              send({
                ok: true,
                ping: true,
                valuetext: cur ? cur.vt : (slider.getAttribute('aria-valuetext') || ''),
                history: historyOffsets(),
              });
              return;
            }
            const native = readWindow();
            const mapped = toRelative(start, end, native);
            const relStart = mapped.relStart;
            const relEnd = mapped.relEnd;
            const matches = (w, s, e) => w &&
              Math.abs(w.a - s) <= TOL && Math.abs(w.b - e) <= TOL &&
              Math.abs((w.b - w.a) - (e - s)) <= LEN_TOL &&
              w.b - w.a >= 5 && w.b - w.a <= 60;
            const attempt = async (s, e) => {
              try {
                drag.onLeftDrag({ startOffset: s, endOffset: e });
              } catch (err) {
                return { ok: false, reason: 'arraste inicial falhou: ' + (err && err.message || err) };
              }
              const leftDeadline = Date.now() + 8000;
              let left = null;
              while (Date.now() <= leftDeadline) {
                left = readWindow();
                if (left && Math.abs(left.a - s) <= TOL) break;
                await new Promise((r) => setTimeout(r, 250));
              }
              if (!left || Math.abs(left.a - s) > TOL) {
                return { ok: false, reason: 'editor não confirmou início "' + (left ? left.vt : '(sem leitura)') + '"' };
              }
              try {
                drag.onRightDrag({ startOffset: s, endOffset: e });
              } catch (err) {
                return { ok: false, reason: 'arraste final falhou: ' + (err && err.message || err) };
              }
              const deadline = Date.now() + 8000;
              let w = null;
              while (Date.now() <= deadline) {
                w = readWindow();
                if (matches(w, s, e)) {
                  writeClipOffsets(s, e);
                  const vid = document.querySelector('video');
                  if (vid) {
                    try { vid.currentTime = s; } catch { /* ignore */ }
                    vid.dataset.vodripClampStart = String(s);
                    vid.dataset.vodripClampEnd = String(e);
                    if (vid.dataset.vodripRangeClamp !== '1') {
                      vid.dataset.vodripRangeClamp = '1';
                      const clamp = () => {
                        const a = Number(vid.dataset.vodripClampStart);
                        const b = Number(vid.dataset.vodripClampEnd);
                        if (!Number.isFinite(a) || !Number.isFinite(b) || !(b > a)) return;
                        if (vid.currentTime > b + 0.05) {
                          try { vid.currentTime = b; } catch { /* ignore */ }
                          try { if (!vid.paused) vid.pause(); } catch { /* ignore */ }
                        } else if (vid.currentTime < a - 0.2) {
                          try { vid.currentTime = a; } catch { /* ignore */ }
                        }
                      };
                      vid.addEventListener('timeupdate', clamp);
                      vid.addEventListener('seeking', clamp);
                    }
                  }
                  const histDeadline = Date.now() + 2500;
                  while (Date.now() <= histDeadline) {
                    const ho = historyOffsets();
                    if (ho && Math.abs(ho.startOffset - s) <= TOL && Math.abs(ho.endOffset - e) <= TOL) break;
                    writeClipOffsets(s, e);
                    await new Promise((r) => setTimeout(r, 200));
                  }
                  return {
                    ok: true,
                    valuetext: w.vt,
                    debug: {
                      relStart: s,
                      relEnd: e,
                      requestedDur: mapped.requestedDur,
                      rawEnd: mapped.uiEnd,
                      origin: mapped.origin,
                      anchor: mapped.anchor,
                      history: historyOffsets(),
                      videoDuration: vid && Number.isFinite(vid.duration) ? vid.duration : null,
                    },
                  };
                }
                await new Promise((r) => setTimeout(r, 250));
              }
              return { ok: false, reason: 'editor ajustou para "' + (w ? w.vt : '(sem leitura)') + '"' };
            };
            (async () => {
              const res = await attempt(relStart, relEnd);
              send(res);
            })();
          });
        },
      })
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, err: String((err && err.message) || err) }));
    return true; // async sendResponse
  }
  return undefined; // not ours — leave the channel to other listeners
});

// Clip-assist note/record relay: the content script (clip_assist.mjs) cannot
// POST to the local backend directly — its fetch from clips.twitch.tv /
// twitch.tv/videos is cross-origin to http://127.0.0.1 and the browser kills
// it on the CORS preflight. The service worker fetches with the extension's
// own origin + host_permission http://127.0.0.1/* — no preflight at all —
// so the clip flow's debug notes and published-clip records are relayed
// through here. Fire-and-forget both ways: never returns true, never
// touches sendResponse (the async legacy listener makes that channel
// unreliable anyway).
chrome.runtime.onMessage.addListener((message) => {
  if (!message || !message.type) return;
  // A content-script note means the user just opened the clip editor —
  // check for a pending reload directive NOW instead of waiting for the
  // 30s alarm (the new content script only runs after a self-reload).
  checkReloadDirective();
  if (message.type === 'vodrip_note') {
    const { event, data } = message;
    if (!event) return;
    (async () => {
      const res = await fetch(`${await getApiBase()}/api/debug/clip-events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ src: 'ext', event, data: data || {} }),
      });
      if (!res.ok) console.warn('[vodrip] note relay failed:', res.status);
    })().catch(() => { /* backend offline — logging is best-effort */ });
    return;
  }
  if (message.type === 'vodrip_record') {
    const payload = message.payload || {};
    (async () => {
      const res = await fetch(`${await getApiBase()}/api/twitch/clips/record`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) console.warn('[vodrip] record relay failed:', res.status);
    })().catch(() => { /* backend offline — next publish retries */ });
  }
});

// Clip-assist self-close: the content script (clip_assist.mjs) asks the
// background to close its own tab after the flow finishes — the user's
// window rule ("every spawned window is used for its purpose and closed
// after"). The SW holds the delay: content-script timers freeze in
// hidden/throttled tabs, SW timers do not. Only the clip-flow origins
// may request this.
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message && message.type === 'vodrip-close-tab') {
    const tabId = sender && sender.tab && sender.tab.id;
    if (tabId == null) return;
    let u;
    try {
      u = new URL(sender && sender.url);
    } catch {
      return;
    }
    const validOrigin =
      u.protocol === 'https:' &&
      ((u.hostname === 'clips.twitch.tv' && u.pathname.length > 1) ||
        (u.hostname === 'www.twitch.tv' && u.pathname.startsWith('/videos/')));
    if (!validOrigin) return;
    const delayMs = Number.isFinite(message.delayMs)
      ? Math.min(Math.max(message.delayMs, 0), 10000)
      : 1200;
    setTimeout(() => {
      try {
        chrome.tabs.remove(tabId);
      } catch (err) {
        console.warn('[vodrip] close tab failed:', err);
      }
    }, delayMs);
  }
});
