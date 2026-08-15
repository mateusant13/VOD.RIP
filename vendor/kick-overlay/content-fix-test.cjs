// Behavioral harness for the kick-overlay content.js fixes (injectStyles,
// ensureYtHls TTL/backoff, yt fatal-error fallback, yt URL reload). Runs the
// real content.js source with a stubbed DOM/chrome environment and asserts on
// observable behavior. `node content-fix-test.js` (no frameworks).
'use strict';
const fs = require('fs');
const assert = require('assert');

const swCalls = []; // chrome.runtime.sendMessage recorder
const storage = { ko: undefined };
const framePosts = []; // postMessage sent to the fake yt frame
const appends = []; // elements appended to document.head
let fakeFrame = null;

const el = (tag) => ({
  tag,
  id: '',
  textContent: '',
  innerHTML: '',
  style: {},
  dataset: {},
  className: '',
  isConnected: true,
  contentWindow: null,
  classList: { add() {}, remove() {}, contains: () => false },
  appendChild() {},
  remove() {},
  addEventListener() {},
  setAttribute() {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
  getClientRects: () => [],
  querySelector: () => null,
  querySelectorAll: () => [],
});

const doc = {
  head: { appendChild: (x) => appends.push(x) },
  documentElement: el('html'),
  body: el('body'),
  hidden: false,
  hasFocus: () => true,
  fullscreenElement: null,
  createElement: (t) => el(t),
  getElementById: (id) => (id === 'ko-yt' ? fakeFrame : null),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
};

const chromeStub = {
  runtime: {
    sendMessage: (m, cb) => {
      swCalls.push(m);
      if (m && m.type === 'ko-yt-play') {
        const rr = { url: 'https://hls.example/u1' };
        setTimeout(() => cb && cb(rr), 0);
      } else {
        cb && cb({ ok: true });
      }
    },
    lastError: null,
    onMessage: { addListener() {} },
    getURL: (p) => 'chrome-extension://test/' + p,
  },
  storage: {
    local: {
      get: (k, cb) => { void k; setTimeout(() => cb && cb({}), 0); },
      set: (o, cb) => { storage.ko = o['ko.v2']; cb && cb(); },
    },
    onChanged: { addListener() {} },
  },
  action: {
    setBadgeText() {},
    setBadgeBackgroundColor() {},
  },
  tabs: { query: () => Promise.resolve([]) },
};
global.chrome = chromeStub;
global.window = global; // content.js binds window listeners — alias to global
global.document = doc;
global.location = { pathname: '/rodil', search: '', href: 'https://www.twitch.tv/rodil', hash: '' };
const msgListeners = [];
global.addEventListener = (t, l) => { if (t === 'message') msgListeners.push(l); };
global.removeEventListener = () => {};
// kick.com is 403-gated from this IP; the content script catches fetch
// failures internally, so a rejecting stub keeps the boot probe deterministic.
global.fetch = () => Promise.reject(new Error('stub-fetch'));

const src = fs.readFileSync('content.js', 'utf8');
// Run in a Function scope so top-level consts/functions are accessible.
const scope = {};
const fn = new Function('window', 'document', 'location', 'navigator', src + '\n;return {KO, probe, apply, ensureYtHls, injectStyles, teardown, setPlayer};');
const api = fn(global, doc, global.location, global.navigator);
const KO = api.KO;

const settle = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await settle(400); // let init() + first fire(apply) settle

  // ---- A: injectStyles actually injects the <style> -------------------------
  const styleEl = appends.find((e) => e.id === 'ko-style');
  assert.ok(styleEl, 'ko-style must be appended to document.head');
  assert.ok(!styleEl.textContent.includes('appendChild(st)'), 'CSS must not contain the stray appendChild text');
  assert.ok(styleEl.textContent.includes('#ko-wrap{position:fixed'), 'CSS body present');
  console.log('A injectStyles: style appended, no stray JS in CSS — OK');

  // ---- C: ensureYtHls TTL + backoff -----------------------------------------
  KO.slug = 'rodil';
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  KO.ytHlsFailed = false;
  KO.ytHlsFailedAt = 0;
  const before = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  await api.ensureYtHls();
  assert.strictEqual(KO.ytHlsUrl, 'https://hls.example/u1', 'mint sets the url');
  assert.strictEqual(KO.ytHlsFailed, false, 'mint clears the failed flag');
  await api.ensureYtHls(); // immediate second call
  let after = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(after - before, 1, 'fresh mint must not re-hit the background (TTL)');
  // failure backoff: failed 5s ago -> no retry
  KO.ytHlsFailed = true; KO.ytHlsFailedAt = Date.now() - 5000; KO.ytHlsUrl = null;
  await api.ensureYtHls();
  after = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(after - before, 1, 'hard-fail backoff must not re-hit the background');
  // backoff expired -> retry clears the flag
  KO.ytHlsFailedAt = Date.now() - 31000;
  await api.ensureYtHls();
  after = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(after - before, 2, 'expired backoff must re-mint');
  assert.strictEqual(KO.ytHlsFailed, false, 'successful retry clears the failed flag');
  console.log('C ensureYtHls: TTL dedupe + failure backoff + retry — OK');

  // ---- B/D: probe youtube branch — fallback + URL reload --------------------
  const wrap = {
    style: { display: 'none' },
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
    isConnected: true,
    offsetWidth: 100, offsetHeight: 60,
  };
  fakeFrame = { ...el('iframe'), contentWindow: { postMessage: (m) => framePosts.push(m) } };
  KO.wrap = wrap;
  KO.player = 'youtube';
  KO.kickSlug = 'rodil';
  KO.ytState.ready = true;
  KO.ytState.live = false;
  KO.ytEmbedAt = Date.now();
  KO.ytHlsUrl = 'https://hls.example/u1';
  KO.ytHlsAt = Date.now();
  KO.ytHlsFailed = false;
  KO.ytHlsFailedAt = 0;
  KO.ytLoadedUrl = 'https://hls.example/u0'; // frame holds a STALE url

  await api.probe();
  const loadMsg = framePosts.find((m) => m.__koKick && m.__koKick.t === 'load');
  assert.ok(loadMsg, 'probe must hand the refreshed url to the frame');
  assert.strictEqual(loadMsg.__koKick.url, 'https://hls.example/u1', 'load carries the NEW url');
  assert.strictEqual(KO.ytLoadedUrl, 'https://hls.example/u1', 'ytLoadedUrl tracks the handed url');
  assert.strictEqual(wrap.style.display, 'block', 'ready+loaded layer must be shown (transition grace)');
  assert.strictEqual(KO.player, 'youtube', 'no fallback while the layer is recoverable');
  console.log('D probe: refreshed url reloaded into the live frame, layer shown — OK');

  // fatal error after the frame's one reload -> fallback to kick
  framePosts.length = 0;
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now();
  KO.ytLoadedUrl = 'https://hls.example/u1';
  await api.probe();
  assert.strictEqual(KO.player, 'kick', 'ytHlsFailed must hand over to kick');
  assert.ok(!framePosts.some((m) => m.__koKick && m.__koKick.t === 'load'), 'no load sent after fallback');
  console.log('B probe: fatal hls error (ytHlsFailed) reaches the kick fallback — OK');

  // ---- G: kick frame ready only from the actual frame -----------------------
  // The kick message listener was registered at eval on `window` (=global);
  // assert the guard directly on the registered source.
  const kickHandlerStart = src.indexOf("window.addEventListener('message', (ev) => {", 0);
  const kickHandlerEnd = src.indexOf('// kick.com-identical seek UX', kickHandlerStart);
  const kickSrc = src.slice(kickHandlerStart, kickHandlerEnd);
  assert.ok(kickSrc.includes("if (m.t === 'ready')"), 'ready branch present');
  assert.ok(kickSrc.includes('ev.source !== KO.kickFrame.contentWindow'), 'kick ready guarded by frame source check');
  console.log('G kick ready: source-guarded against non-frame windows — OK (source assertion)');

  // ---- teardown resets the new fields ---------------------------------------
  KO.ytHlsUrl = 'u'; KO.ytHlsAt = 1; KO.ytHlsFailed = true; KO.ytHlsFailedAt = 2; KO.ytLoadedUrl = 'u';
  KO.kickFrame = null; KO.wrap = null;
  api.teardown();
  assert.strictEqual(KO.ytHlsUrl, null);
  assert.strictEqual(KO.ytHlsAt, 0);
  assert.strictEqual(KO.ytHlsFailed, false);
  assert.strictEqual(KO.ytHlsFailedAt, 0);
  assert.strictEqual(KO.ytLoadedUrl, null);
  console.log('teardown: yt mint bookkeeping reset — OK');

  console.log('\nALL CONTENT.JS FIX CHECKS PASSED');
  process.exit(0);
})().catch((e) => { console.error('HARNESS FAIL:', e); process.exit(1); });
