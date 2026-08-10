import getAllCookies from './modules/get_all_cookies.mjs';
import saveToFile from './modules/save_to_file.mjs';
import {
  BRIDGE_DOMAINS,
  createDebouncedPush,
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

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) pushBridgeCookies();
});

// Update notification
chrome.runtime.onInstalled.addListener(({ previousVersion, reason }) => {
  armHeartbeat();
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
  pushBridgeCookies();
});

// TODO: use offscreen API to integrate implementation in chrome and firefox
// Save file message listener for firefox
chrome.runtime.onMessage.addListener(async (message, sender, sendResponse) => {
  const { type, target, data } = message || {};
  if (target !== 'background') return;
  if (type === 'save') {
    const { text, name, format, saveAs } = data || {};
    try {
      await saveToFile(text, name, format, saveAs);
      sendResponse('done');
    } catch (err) {
      console.warn('[vodrip] save failed:', err);
      sendResponse('error');
    }
    return true;
  }
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
            const m = /^(\d+):(\d{2})$/.exec(s);
            return m ? +m[1] * 60 + +m[2] : null;
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
            try {
              drag.onLeftDrag({ startOffset: start, endOffset: end });
              drag.onRightDrag({ startOffset: start, endOffset: end });
            } catch (err) {
              send({ ok: false, reason: 'drag falhou: ' + (err && err.message || err) });
              return;
            }
            const deadline = Date.now() + 8000;
            const tick = () => {
              const vt = (slider.getAttribute('aria-valuetext') || '').trim();
              const parts = vt.split(/\s*to\s*/i);
              if (parts.length === 2) {
                const a = pc(parts[0]);
                const b = pc(parts[1]);
                if (a != null && b != null && Math.abs(a - start) <= 1 && Math.abs(b - end) <= 1) {
                  send({ ok: true, valuetext: vt });
                  return;
                }
              }
              if (Date.now() > deadline) {
                send({ ok: false, reason: 'editor ajustou para "' + vt + '"' });
                return;
              }
              setTimeout(tick, 250);
            };
            setTimeout(tick, 0);
          });
        },
      })
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, err: String((err && err.message) || err) }));
    return true; // async sendResponse
  }
  return undefined; // not ours — leave the channel to other listeners
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
    const u = sender && sender.url;
    if (!u || !/^https:\/\/(clips\.twitch\.tv|www\.twitch\.tv\/videos)/.test(u)) return;
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
