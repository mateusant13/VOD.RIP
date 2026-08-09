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
