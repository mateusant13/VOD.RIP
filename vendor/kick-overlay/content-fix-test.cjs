// Behavioral harness for the kick-overlay content.js fixes (injectStyles,
// ensureYtHls TTL/backoff, yt fallback GATE + non-persisting handover, URL
// reload, #ko-status chip + <=4-char badge tokens, sticky tw_delete via
// MutationObserver, yt frame-death watchdog). Runs the real content.js source
// with a stubbed DOM/chrome environment and asserts on observable behavior.
// `node content-fix-test.cjs` (no frameworks).
'use strict';
const fs = require('fs');
const assert = require('assert');

const manifest = JSON.parse(fs.readFileSync('manifest.json', 'utf8'));
assert.ok(manifest.host_permissions.includes('https://kick.com/*'), 'manifest permits Kick API fetches');
const CS_MATCHES = manifest.content_scripts && manifest.content_scripts[0] && manifest.content_scripts[0].matches;
assert.ok(Array.isArray(CS_MATCHES), 'manifest has content_scripts[0].matches');
assert.ok(CS_MATCHES.includes('https://www.twitch.tv/*'), 'content script runs on www.twitch.tv');
assert.ok(CS_MATCHES.includes('https://www.youtube.com/*'), 'content script runs on www.youtube.com');
assert.ok(CS_MATCHES.includes('https://m.youtube.com/*'), 'content script runs on m.youtube.com');
assert.ok(CS_MATCHES.includes('https://youtu.be/*'), 'content script runs on youtu.be');
const WAR = manifest.web_accessible_resources && manifest.web_accessible_resources[0] && manifest.web_accessible_resources[0].matches;
assert.ok(Array.isArray(WAR), 'manifest has web_accessible_resources[0].matches');
assert.ok(WAR.includes('https://www.youtube.com/*'), 'player.html is web-accessible from www.youtube.com');
assert.ok(WAR.includes('https://m.youtube.com/*'), 'player.html is web-accessible from m.youtube.com');
assert.ok(WAR.includes('https://youtu.be/*'), 'player.html is web-accessible from youtu.be');
assert.strictEqual(manifest.version, '0.8.6', 'manifest version is 0.8.6');
const swCalls = []; // chrome.runtime.sendMessage recorder
const badgeTexts = []; // chrome.action.setBadgeText recorder (clipping check)
const badge = []; // paired {text,color} records — the 0.8.2 status contract
let lastBadgeColor = null;
const runtimeMsgListeners = []; // chrome.runtime.onMessage recorders
const moInstances = []; // MutationObserver instances (tw-delete re-apply)
const fakeVideos = []; // page-world <video> population (twitch player)
let fakeFrame = null;
const storage = { ko: undefined }; // the single 'ko.v2' record
let storageChangedListener = null; // chrome.storage.onChanged listener
const framePosts = []; // postMessage sent to the fake yt frame
const appends = []; // elements appended to document.head

// Live id registry. content.js re-finds the YouTube frame with
// document.getElementById('ko-yt') and the Twitch player via querySelector, so
// a mounted frame must be findable — and forgetable on remove(). The eager
// dual-mount and the no-teardown class-flip contracts both hinge on this.

const byId = new Map();
const unregister = (n) => {
  if (n && n.id && byId.get(n.id) === n) byId.delete(n.id);
  (n.children || []).forEach(unregister);
};

// Set-backed classList: 0.8.2 reports state through classes (ko-kick/ko-yt/
const mkClassList = (owner) => {
  const set = new Set();
  const sync = () => { owner.className = [...set].join(' '); };
  return {
    _set: set,
    add(...c) { c.forEach((x) => set.add(x)); sync(); },
    remove(...c) { c.forEach((x) => set.delete(x)); sync(); },
    contains: (c) => set.has(c),
    toggle(c, force) {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      sync();
      return on;
    },
  };
};

const el = (tag) => {
  let id = '';
  const node = {
    tag,
    tagName: (tag || 'div').toUpperCase(),
    nodeType: 1,
    innerHTML: '',
    style: {},
    dataset: {},
    className: '',
    attrs: {},
    isConnected: true,
    removed: 0,
    contentWindow: null,
    children: [],
    listeners: {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      unregister(c);
      return c;
    },
    remove() { this.removed = (this.removed || 0) + 1; this.isConnected = false; unregister(this); },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener() {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
    getClientRects: () => [],
    querySelector: () => el('div'), // mount() wires buttons from inner queries
    querySelectorAll: () => [],
    click() { (this.listeners.click || []).forEach((f) => f({ target: this, preventDefault() {}, stopPropagation() {} })); },
  };
  node.classList = mkClassList(node);
  Object.defineProperty(node, 'id', {
    get: () => id,
    set: (v) => { id = String(v); if (id) byId.set(id, node); },
    enumerable: true,
  });
  let text = '';
  Object.defineProperty(node, 'textContent', {
    get: () => text,
    set: (v) => { text = String(v); node.children.length = 0; }, // rebuild-from-empty
    enumerable: true,
  });
  return node;
};

// A page <video> (the Twitch player). remove() also drops it from the DOM
// population, mirroring reality (twitchVideo() must stop seeing it).
const videoEl = (id) => ({
  ...el('video'),
  id,
  paused: true,
  readyState: 4,
  currentTime: 10,
  muted: false,
  getClientRects: () => [{}],
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
  pause() { this.paused = true; },
  play() { this.paused = false; return Promise.resolve(); },
  remove() {
    this.removed = (this.removed || 0) + 1;
    this.isConnected = false;
    const i = fakeVideos.indexOf(this);
    if (i >= 0) fakeVideos.splice(i, 1);
  },
});

// Page-level selectors the content script queries on the HOST page (YouTube's
// player element, the watch-page channel-identity metadata). Driven per test.
const pageSel = new Map();
const setPageSel = (sel, node) => { if (node === null) pageSel.delete(sel); else pageSel.set(sel, node); };
const clearPageSel = () => pageSel.clear();

const doc = {
  head: { appendChild: (x) => appends.push(x) },
  documentElement: el('html'),
  body: el('body'),
  hidden: false,
  hasFocus: () => true,
  fullscreenElement: null,
  createElement: (t) => el(t),
  getElementById: (id) => (id === 'ko-yt' && fakeFrame ? fakeFrame : byId.get(id) || null),
  querySelector: (sel) => pageSel.get(sel) || null,
  querySelectorAll: (sel) => (sel === 'video' ? fakeVideos : []),
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
    onMessage: { addListener: (l) => runtimeMsgListeners.push(l) },
    getURL: (p) => 'chrome-extension://test/' + p,
  },
  storage: {
    local: {
      // Single mutable object: tests write it to simulate another tab or the
      // popup pushing a new settings table.
      get: (k, cb) => setTimeout(() => cb && cb(storage.ko === undefined ? {} : { [k]: storage.ko }), 0),
      set: (o, cb) => { storage.ko = o['ko.v2']; cb && cb(); },
    },
    // FIRST-WINS: the harness loads content.js twice (main api + mountApi at
    // section K). Tests that fire the listener drive the MAIN module's state
    // (KO from line ~220) — the second load must not hijack the handle.
    onChanged: { addListener: (l) => { if (!storageChangedListener) storageChangedListener = l; } },
  },
  action: {
    // setBadge() always sets the colour BEFORE the text, so pairing the last
    // colour with the emitted text records the exact call.
    setBadgeText(o) { badgeTexts.push(o && o.text); badge.push({ text: o && o.text, color: lastBadgeColor }); },
    setBadgeBackgroundColor({ color }) { lastBadgeColor = color; },
  },
  tabs: { query: () => Promise.resolve([]) },
};
global.chrome = chromeStub;
global.window = global; // content.js binds window listeners — alias to global
global.document = doc;
global.innerWidth = 1280;
global.innerHeight = 720;
global.location = { pathname: '/rodil', search: '', href: 'https://www.twitch.tv/rodil', hash: '' };
const msgListeners = [];
global.addEventListener = (t, l) => { if (t === 'message') msgListeners.push(l); };
global.removeEventListener = () => {};
global.MutationObserver = class {
  constructor(cb) { this.cb = cb; moInstances.push(this); }
  observe(target, opts) { this.target = target; this.opts = opts; }
  disconnect() {}
};
// rectTick's one-shot anomaly dump reads computed styles; the harness never
// gives the wrap an offsetHeight, so the dump stays cold — the stub exists so a
// future fixture that does set one cannot throw.
global.getComputedStyle = () => ({ height: '', maxHeight: '', position: 'static' });
// kick.com is 403-gated from this IP; the content script catches fetch
// failures internally, so a rejecting stub keeps the boot probe deterministic.
global.fetch = () => Promise.reject(new Error('stub-fetch'));

const src = fs.readFileSync('content.js', 'utf8');
assert.ok(src.includes("const KO_VER = '0.8.6'"), 'content KO_VER stays 0.8.6, aligned with manifest (P3d — a half-bump would drift green)');
const bridgeSrc = fs.readFileSync('player-bridge.js', 'utf8');
// Run in a Function scope so top-level consts/functions are accessible.
const scope = {};
const fn = new Function('window', 'document', 'location', 'navigator', src + '\n;return {KO, probe, apply, ensureYtHls, injectStyles, teardown, setPlayer, saveState, ' +
  'loadState, rectTick, rectFromHost, currentSlug, resolveYtPageKick, ensureYtIframe, ensureKick, showKickLayer, showYtLayer, hideWrap, hideWrapForProbe, ' +
  'renderQualityMenu, mount, updateKickBar, seekOverlayStep, onOverlayKeydown};');
const api = fn(global, doc, global.location, global.navigator);
const KO = api.KO;

const settle = (ms) => new Promise((r) => setTimeout(r, ms));
// The last badge emission. setBadge() ALWAYS sets the colour first, so the
// pair is the exact call (0.8.2 carries the phase in the colour, not in text).
const lastBadge = () => badge[badge.length - 1] || { text: null, color: null };

(async () => {
  await settle(400); // let init() + first fire(apply) settle

  // ---- A: injectStyles actually injects the <style> -------------------------
  const styleEl = appends.find((e) => e.id === 'ko-style');
  assert.ok(styleEl, 'ko-style must be appended to document.head');
  assert.ok(!styleEl.textContent.includes('appendChild(st)'), 'CSS must not contain the stray appendChild text');
  assert.ok(styleEl.textContent.includes('#ko-wrap{position:fixed'), 'CSS body present');
  // 0.8.1 lineage deliberately removed the persistent top-left LIVE chip (#ko-top);
  // 0.8.2 hides the bar on inactivity. Assertions ported from the 0.8.0 chip/always-visible era.
  assert.ok(!src.includes('ensureStatusChip') && !src.includes("'#ko-top{"), 'no status chip in 0.8.1+ lineage');
  // 0.8.6 (owner pain 3): the modern return-to-live control is NOT the dead
  // #ko-top chip — the shipped clickable LIVE pill is the #ko-golive button
  // created in mount() (markup asserts its own presence/click later in P3).
  assert.ok(src.includes("<button id=\"ko-golive\""), 'mount ships the clickable #ko-golive LIVE pill');
  assert.ok(styleEl.textContent.includes('#ko-golive{'), 'LIVE pill has visible styling');
  assert.ok(styleEl.textContent.includes('#ko-bar{position:absolute;left:0;right:0;bottom:0;pointer-events:auto;opacity:1;'), 'control bar defaults visible');
  assert.ok(styleEl.textContent.includes('#ko-wrap:not(.ko-hot) #ko-bar{opacity:0;pointer-events:none;}'), 'bar auto-hides after inactivity (2.6s hot timer)');
  assert.ok(!styleEl.textContent.includes('#ko-wrap:not(.ko-hot) #ko-bar{opacity:1;}'), 'stale always-visible bar rule purged');
  assert.ok(styleEl.textContent.includes('#ko-quality-menu{display:none;'), 'quality menu CSS present');
  assert.ok(styleEl.textContent.includes('flex-direction:column;'), 'quality menu is vertical (column)');
  assert.ok(styleEl.textContent.includes('#ko-quality-wrap.ko-open #ko-quality-menu{display:flex;}'), 'quality menu opens via ko-on class');
  assert.ok(styleEl.textContent.includes('#ko-seekbar{position:absolute;top:6px;'), 'seek bar contained inside bar');
  assert.ok(!styleEl.textContent.includes('top:-28px'), 'seek bar no longer extends above bar');
  assert.ok(src.includes("document.addEventListener('keydown', onOverlayKeydown, true)"), 'F key capture listener present');
  assert.ok(src.includes('e.stopImmediatePropagation()'), 'overlay F handler wins over Twitch fullscreen');
  assert.ok(src.includes('iframe.allowFullscreen = true') && src.includes('fr.allowFullscreen = true'), 'both iframe fullscreen permissions present');
  assert.ok(src.includes('if (!v.paused)') && src.includes('v.pause();'), 'native Twitch video is paused under overlay');
  assert.ok(bridgeSrc.includes("case 'quality':") && bridgeSrc.includes('setAutoQualityMode'), 'quality command reaches both player engines');
  console.log('A injectStyles: style appended, bar/seek/offline CSS — OK');

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

  // ---- B/D: probe youtube branch — fallback gate + URL reload ---------------
  const wrap = {
    style: { display: 'none' },
    // Real set-backed classList: showKickLayer/showYtLayer report the active
    // layer through ko-kick/ko-yt, and the class-flip contract asserts on it.
    classList: null,
    appendChild() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
    isConnected: true,
    offsetWidth: 100, offsetHeight: 60,
  };
  wrap.classList = mkClassList(wrap);
  wrap.classList.add('ko-kick');
  fakeFrame = { ...el('iframe'), contentWindow: { postMessage: (m) => framePosts.push(m) } };
  fakeFrame.dataset.koToken = 'test-token';
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

  badge.length = 0;
  await api.probe();
  const bD = lastBadge();
  assert.strictEqual(bD.text, 'YT', 'the running yt layer badges YT');
  assert.strictEqual(bD.color, '#ff0000', 'red = the layer is live/loaded and showing');
  const loadMsg = framePosts.find((m) => m.__koKick && m.__koKick.t === 'load');
  assert.ok(loadMsg, 'probe must hand the refreshed url to the frame');
  assert.strictEqual(loadMsg.__koKick._koToken, 'test-token', 'player commands must carry the frame token');
  assert.strictEqual(loadMsg.__koKick.url, 'https://hls.example/u1', 'load carries the NEW url');
  assert.strictEqual(KO.ytLoadedUrl, 'https://hls.example/u1', 'ytLoadedUrl tracks the handed url');
  assert.strictEqual(wrap.style.display, 'block', 'ready+loaded layer must be shown (transition grace)');
  assert.strictEqual(KO.player, 'youtube', 'no fallback while the layer is recoverable');
  console.log('D probe: refreshed url reloaded into the live frame, layer shown — OK');

  // fatal error after the frame's one reload — same-handle kickSlug: the
  // fallback gate must KEEP youtube mode (a same-handle kick is no real
  // kick mapping — its probe would find the same offline channel).
  framePosts.length = 0;
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now();
  KO.ytLoadedUrl = 'https://hls.example/u1';
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytEmbedAt = Date.now() - 10000; // embed grace expired too
  badge.length = 0;
  await api.probe();
  const bFail = lastBadge();
  assert.strictEqual(bFail.text, 'YT', 'a failed yt layer badges YT (0.8.1+ has no in-page chip)');
  assert.strictEqual(bFail.color, '#6b7280', 'gray = the mint/embed failed, not merely connecting');
  assert.strictEqual(KO.player, 'youtube', 'same-handle kickSlug must NOT hand over to kick');
  assert.ok(!framePosts.some((m) => m.__koKick && m.__koKick.t === 'load'), 'no load sent after fatal error');
  assert.strictEqual(wrap.style.display, 'none', 'failed yt layer hides the wrap (the badge carries the state)');
  // A REAL kick mapping (kickSlug != slug) DOES hand over — in memory only.
  storage.ko = undefined;
  KO.kickSlug = 'realkick';
  global.fetch = () => Promise.resolve({
    ok: true,
    json: async () => ({ livestream: { playback_url: 'https://ivs.example/live', id: 's1' } }),
  });
  await api.probe();
  await settle(50); // the fire(apply) handover probe settles
  assert.strictEqual(KO.player, 'kick', 'real kick mapping hands over to kick');
  assert.ok(!(storage.ko && storage.ko.player === 'kick'), 'handover must not persist to storage');
  assert.strictEqual(wrap.style.display, 'block', 'kick layer shown after the handover');
  global.fetch = () => Promise.reject(new Error('stub-fetch'));
  KO.kickSlug = 'rodil';
  KO.kickFrame = null;
  KO.kickWin = null;
  KO.activeUrl = null;
  console.log('B probe: fallback gate — same-handle stays youtube, real mapping hands over w/o persisting — OK');

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
  KO.lastYtSt = 123;
  // The wrap/frames are 0.8.2's only in-page state surface; teardown must drop
  // them while leaving the iframe-identity counter alone (frameSeq only ever
  // moves forward — the status hook compares it across a flip).
  KO.wrap = el('div');
  KO.kickFrame = el('iframe');
  const seqBeforeTeardown = KO.frameSeq;
  api.teardown();
  assert.strictEqual(KO.ytHlsUrl, null);
  assert.strictEqual(KO.ytHlsAt, 0);
  assert.strictEqual(KO.ytHlsFailed, false);
  assert.strictEqual(KO.ytHlsFailedAt, 0);
  assert.strictEqual(KO.ytLoadedUrl, null);
  assert.strictEqual(KO.lastYtSt, 0, 'teardown resets the yt silence clock');
  assert.strictEqual(KO.wrap, null, 'teardown drops the wrap — state now reports through the badge');
  assert.strictEqual(KO.kickFrame, null, 'teardown drops the IVS frame');
  assert.strictEqual(KO.frameSeq, seqBeforeTeardown, 'teardown never rewinds the frame-identity counter');
  console.log('teardown: yt mint bookkeeping + yt watchdog clock + wrap/frame reset — OK');

  // ---- H: yt→kick fallback gate ---------------------------------------------
  KO.slug = 'rodil';
  KO.player = 'youtube';
  KO.kickSlug = 'rodil';            // same-handle fallback — no real kick mapping
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.mappings['rodil'] = { yt: '@x' };
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now();    // inside the 30s backoff window
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  KO.ytEmbedAt = Date.now() - 10000; // embed grace expired too
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytLoadedUrl = null;
  KO.twDeleted = false;
  KO.wrap = null;                   // youtube-only path mounts the wrap on demand
  storage.ko = undefined;
  fakeFrame = null;
  fakeVideos.length = 0;
  const playsBefore = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  badge.length = 0;
  await api.probe();
  assert.strictEqual(KO.player, 'youtube', 'no real kick mapping -> stay in youtube mode (no kick bounce)');
  assert.ok(KO.wrap, 'youtube branch mounts the wrap');
  assert.strictEqual(KO.wrap.style.display, 'none', 'failed mint hides the wrap');
  assert.ok(!(storage.ko && storage.ko.player === 'kick'), 'no persisted handover');
  KO.playerPreference = 'youtube';
  KO.player = 'kick';
  await api.saveState();
  assert.strictEqual(storage.ko.player, 'youtube', 'session fallback must not leak into persisted player choice');
  KO.player = 'kick';
  KO.playerPreference = 'youtube';
  const currentMappings = KO.mappings;
  assert.ok(storageChangedListener, 'storage listener must be registered');
  storageChangedListener({
    'ko.v2': {
      oldValue: { enabled: true, player: 'youtube', mappings: currentMappings },
      newValue: { enabled: true, player: 'youtube', mappings: currentMappings },
    },
  }, 'local');
  assert.strictEqual(KO.player, 'kick', 'preferred-player self-writes must not undo a session fallback');
  KO.player = 'youtube';
  const playsAfterFail = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(playsAfterFail, playsBefore, 'backoff must prevent a mint hit during the gate probe');
  // The reported failure case: the wrap is hidden, but the badge still
  // reports WHICH layer failed (0.8.1+ has no in-page chip — the state
  // surface is chrome.action, so a hidden wrap is never a silent failure).
  const bH = lastBadge();
  assert.strictEqual(bH.text, 'YT', 'the failed yt layer badges YT, not KICK');
  assert.strictEqual(bH.color, '#6b7280', 'gray = failed, distinct from amber connecting');
  assert.strictEqual(KO.wrap.style.display, 'none', 'wrap stays hidden while the layer is dead');
  // 30s backoff expires -> the probe loop re-enters the youtube branch and re-mints.
  KO.ytHlsFailedAt = Date.now() - 31000;
  badge.length = 0;
  await api.probe();
  assert.strictEqual(KO.player, 'youtube', 're-mint keeps youtube mode');
  assert.strictEqual(KO.ytHlsUrl, 'https://hls.example/u1', 'backoff-expired re-mint succeeded');
  assert.strictEqual(KO.ytHlsFailed, false, 'successful re-mint clears the failed flag');
  const bH2 = lastBadge();
  assert.strictEqual(bH2.text, 'YT', 'the badge keeps the YT token across the re-mint');
  assert.strictEqual(bH2.color, '#d97706', 'amber = minted and connecting (never red/failed again)');
  console.log('H fallback gate: same-handle stays youtube, no persist, backoff re-mint, badge YT gray->amber — OK');

  // ---- H4: P4 — re-picking youtube (same stored value) after a session-only
  // yt->kick fallback with an EXPIRED backoff is a genuine retry (owner pain
  // 4: "Troquei para YouTube e continuou mostrando a Twitch"). storage keeps
  // player:'youtube' (the fallback never persists) and the popup re-writes the
  // SAME value, so neither the stored-value gate nor :366 fires. The retry
  // arm must clear the stale mint evidence and push KO.player back to youtube
  // so fire(apply) mints instead of re-probing kick. Real kick mapping.
  KO.slug = 'rodil';
  KO.player = 'kick';
  KO.playerPreference = 'youtube';     // stored pref is youtube; fallback is in-memory
  KO.kickSlug = 'realkick';            // real mapping — the fallback gate was armed
  KO.mappings['rodil'] = { kick: 'realkick', yt: '@x' };
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now() - 31000; // EXPIRED backoff — same window ensureYtHls honours
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  KO.ytEmbedAt = 0;                    // disarm embed-stalled so no handover re-fires
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytLoadedUrl = null;
  KO.wrap = null;
  fakeFrame = null;
  fakeVideos.length = 0;
  storage.ko = undefined;
  const pH4Before = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  const h4Mappings = KO.mappings;
  assert.ok(storageChangedListener, 'storage listener must be registered');
  // popup re-pick: popup.js stamps fresh.playerAt=Date.now() on every player
  // change, so a same-value write arrives with a NEW playerAt vs the stored
  // one — that delta is the retry intent (0.8.6 replaced the 30s backoff
  // window with this immediate marker).
  storageChangedListener({
    'ko.v2': {
      oldValue: { enabled: true, player: 'youtube', playerAt: 1000, mappings: h4Mappings },
      newValue: { enabled: true, player: 'youtube', playerAt: 2000, mappings: h4Mappings },
    },
  }, 'local');
  // Async fire(apply) -> probe() -> probeYtLayer -> ensureYtHls mint.
  await settle(80);
  const pH4After = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(KO.ytHlsFailed, false, 'retry intent with expired backoff must clear the stale mint failure');
  assert.strictEqual(KO.player, 'youtube', 'retry intent must return the active layer to youtube (not keep kicking)');
  assert.strictEqual(pH4After - pH4Before, 1, 'the re-pick re-mints exactly once (fresh mint, not the old pendings)');
  assert.ok(KO.ytHlsUrl, 'the re-mint produced a fresh url');
  KO.player = 'kick';
  KO.playerPreference = 'kick';
  storage.ko = undefined;
  console.log('H4 P4 retry: same-value youtube re-pick after expired-backoff fallback clears evidence + re-mints — OK');

  // ---- H5: (a) an erase-echo (a write WITHOUT playerAt while a fallback is
  // live) must NOT re-mint — it is a content/volume echo, not a retry; the
  // `typeof s.playerAt === 'number'` guard is what keeps
  // undefined-undefined or a dropped marker from false-positive. (b) the
  // same-value re-pick must ALSO clear an EMBED-STALL (embedStalled hands
  // KO.player to kick with ytHlsFailed still false) — the 0.8.6 gate keys on
  // KO.player==='kick' + the playerAt delta, not on ytHlsFailed.
  KO.slug = 'rodil';
  KO.player = 'kick';
  KO.playerPreference = 'youtube';
  KO.kickSlug = 'realkick';
  KO.mappings['rodil'] = { kick: 'realkick', yt: '@x' };
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now();
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  KO.ytEmbedAt = 0;
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytLoadedUrl = null;
  KO.wrap = null;
  fakeFrame = null;
  fakeVideos.length = 0;
  storage.ko = undefined;
  // (a) erase-echo: same stored player AND playerAt all the way through.
  const h5Before = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  // note: the erase-echo newValue carries NO playerAt key at all.
  storageChangedListener({
    'ko.v2': {
      oldValue: { enabled: true, player: 'youtube', playerAt: 3000, mappings: KO.mappings },
      newValue: { enabled: true, player: 'youtube', mappings: KO.mappings },
    },
  }, 'local');
  await settle(80);
  const h5After = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(KO.ytHlsFailed, true, 'an erase-echo (no playerAt) must NOT clear the stale failure');
  assert.strictEqual(KO.player, 'kick', 'an erase-echo is not a retry — layer stays on kick (fallback intact)');
  assert.strictEqual(h5After - h5Before, 0, 'an erase-echo must NOT mint');
  // (b) embed-stall same-value re-pick MUST re-mint (ytHlsFailed=false but
  // KO.player===kick from embedStalled). The 0.8.6 retry arm keys on
  // KO.player==='kick' + the playerAt delta — NOT on ytHlsFailed — so a
  // pathologically stalled embed that never set ytHlsFailed still gets its
  // fresh mint attempt on an explicit popup re-pick. Note the embed stays
  // "not ready" in the harness, so the post-fire probe legitimately
  // re-evaluates embedStalled and re-handovers; the pin is that a FRESH mint
  // was attempted (the gate fired), which is what the fix must guarantee.
  KO.player = 'kick';
  KO.ytHlsFailed = false; // embed-stall trigger, NOT a failed mint
  KO.ytEmbedAt = Date.now() - 200000; // embed dead > YT_EMBED_GRACE → embedStalled
  // directly arm the live state so probe() lands the fallback as embed-stalled
  storage.ko = { enabled: true, player: 'youtube', playerAt: 4000, mappings: KO.mappings };
  await api.probe();
  assert.strictEqual(KO.player, 'kick', 'embed-stall hands the layer to kick');
  const h5bBefore = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  storageChangedListener({
    'ko.v2': {
      oldValue: { enabled: true, player: 'youtube', playerAt: 4000, mappings: KO.mappings },
      newValue: { enabled: true, player: 'youtube', playerAt: 5000, mappings: KO.mappings },
    },
  }, 'local');
  await settle(80);
  const h5bAfter = swCalls.filter((m) => m && m.type === 'ko-yt-play').length;
  assert.strictEqual(h5bAfter - h5bBefore, 1, 'the embed-stall re-pick fires a fresh mint (the 0.8.6 gate fires without ytHlsFailed)');
  KO.player = 'kick';
  KO.playerPreference = 'kick';
  storage.ko = undefined;
  console.log('H5 P4 retry: erase-echo no-mint (playerAt guard) + embed-stall same-value re-mint — OK');

  // ---- I: badge state transitions (the 0.8.2 status surface) ---------------
  KO.mappings['rodil'] = { kick: 'realkick', yt: '@x' };
  KO.ytHlsFailed = false;
  KO.ytHlsFailedAt = 0;
  // KICK Playing -> green KICK
  KO.player = 'kick';
  KO.kickSlug = 'realkick';
  KO.activeUrl = 'https://ivs.example/live';
  KO.kickUrlT = Date.now();
  KO.kickEverPlayed = true;
  KO.kickState = { state: 'Playing', paused: false, muted: false, pos: 10, lat: 2, dur: 120, ts: Date.now() };
  badge.length = 0;
  await api.probe();
  const bKick = lastBadge();
  assert.strictEqual(bKick.text, 'KICK', 'KICK badge while kick Playing');
  assert.strictEqual(bKick.color, '#059669', 'green = kick playback confirmed Playing');
  assert.strictEqual(KO.wrap.style.display, 'block', 'kick layer shown');
  assert.ok(KO.wrap.classList.contains('ko-kick') && !KO.wrap.classList.contains('ko-yt'), 'kick layer is the rendering one');
  // KICK connecting (url/ready pending) -> amber KICK
  KO.kickState = { state: 'Idle', paused: true, muted: true, pos: 0, lat: 0, dur: 0, ts: Date.now() };
  KO.kickUrlT = Date.now(); // fresh url inside the transition grace
  badge.length = 0;
  await api.probe();
  const bConn = lastBadge();
  assert.strictEqual(bConn.text, 'KICK', 'connecting keeps the KICK token (the phase is the color)');
  assert.strictEqual(bConn.color, '#d97706', 'amber = url attached but IVS not Playing yet');
  // KICK failed/offline -> gray, on the layer the user actually chose
  KO.activeUrl = null;
  assert.ok(kickSrc.includes('ev.origin !== PLAYER_ORIGIN'), 'kick messages must use the extension-frame origin');
  KO.kickUrlT = 0;
  KO.kickState = null;
  KO.kickEverPlayed = false;
  badge.length = 0;
  await api.probe();
  const bOff = lastBadge();
  assert.strictEqual(bOff.text, 'KICK', 'offline kick badges KICK while the yt mint is healthy');
  assert.strictEqual(bOff.color, '#6b7280', 'gray = offline/failed');
  assert.strictEqual(KO.wrap.style.display, 'none', 'wrap hidden when kick offline');
  // The same offline probe with a FAILED yt mint must blame YT, not KICK.
  KO.ytHlsFailed = true;
  badge.length = 0;
  await api.probe();
  const bBlame = lastBadge();
  assert.strictEqual(bBlame.text, 'YT', 'after a yt→kick fallback the offline badge names the failed layer');
  assert.strictEqual(bBlame.color, '#6b7280', 'still gray (offline/failed)');
  KO.ytHlsFailed = false;
  // YT live -> red YT
  KO.player = 'youtube';
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.ytHlsUrl = 'https://hls.example/u1';
  KO.ytHlsAt = Date.now();
  KO.ytHlsFailed = false;
  KO.ytState = { ready: true, playing: true, muted: false, live: true, dur: 0, ct: 0, error: 0 };
  KO.ytLoadedUrl = 'https://hls.example/u1';
  badge.length = 0;
  await api.probe();
  const bYt = lastBadge();
  assert.strictEqual(bYt.text, 'YT', 'YT badge while yt live');
  assert.strictEqual(bYt.color, '#ff0000', 'red = the live YouTube layer (kick green vs yt red)');
  assert.strictEqual(KO.wrap.style.display, 'block', 'yt layer shown');
  assert.ok(KO.wrap.classList.contains('ko-yt') && !KO.wrap.classList.contains('ko-kick'), 'yt layer is the rendering one');
  // YT minting/loading (not live yet) -> amber YT
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytEmbedAt = Date.now(); // embed grace not expired
  badge.length = 0;
  await api.probe();
  const bYt2 = lastBadge();
  assert.strictEqual(bYt2.text, 'YT', 'YT token while minting/loading');
  assert.strictEqual(bYt2.color, '#d97706', 'amber = loading, gray would mean failed');
  // twitch mode -> overlay hidden, badge peeks which fallback is available
  KO.player = 'twitch';
  badge.length = 0;
  await api.probe();
  const bTw = lastBadge();
  assert.strictEqual(bTw.text, 'TW', 'twitch mode badges TW when neither kick nor yt is live');
  assert.strictEqual(bTw.color, '#6b7280', 'gray = nothing to hand over to');
  assert.strictEqual(KO.wrap.style.display, 'none', 'wrap hidden in twitch mode');
  KO.player = 'youtube';
  console.log('I badge states: KICK green/amber/gray (blame-aware) + YT red/amber + TW peek — OK');

  // ---- J: badge texts all fit Chrome's 4-char clip --------------------------
  const seen = new Set(badgeTexts);
  for (const t of seen) {
    assert.ok(typeof t === 'string' && t.length <= 4, `badge text '${t}' must fit the 4-char clip`);
    assert.ok(['', 'KICK', 'YT', 'TW', 'OFF'].includes(t), `badge '${t}' must be a normalized token`);
  }
  assert.ok(!seen.has('KICK OFF') && !seen.has('RECONNECT') && !seen.has('YT OFF') && !seen.has('YT?'), 'no legacy clipped badge tokens');
  console.log('J badge clipping: every emitted badge <=4 chars, normalized tokens only — OK');

  // ---- K: ko-delete-twitch stickiness ----------------------------------------
  KO.slug = 'rodil';
  KO.player = 'youtube';
  KO.kickSlug = 'rodil';            // same-handle — gate keeps youtube mode
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.mappings['rodil'] = { yt: '@x' };
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now();    // backoff — the mint will not retry mid-test
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  KO.ytEmbedAt = Date.now() - 10000;
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytLoadedUrl = null;
  KO.twDeleted = false;
  KO.lastRect = null;
  KO.kickFrame = null;
  KO.kickWin = null;
  fakeFrame = null;
  fakeVideos.length = 0;
  const delVideo = videoEl('tw-main-video');
  fakeVideos.push(delVideo);
  await api.probe(); // youtube branch mounts the wrap; failed mint + not deleted -> hidden
  assert.ok(KO.wrap, 'wrap mounted in youtube mode');
  assert.strictEqual(KO.wrap.style.display, 'none', 'failed mint hides the wrap before the delete');
  assert.strictEqual(delVideo.removed, 0, 'probe alone never touches the twitch video');

  const delListener = runtimeMsgListeners[0];
  assert.ok(delListener, 'popup action listener registered');
  badge.length = 0;
  await new Promise((res) => delListener({ type: 'ko-delete-twitch' }, {}, () => res()));
  await settle(50); // let the handler's apply() settle
  assert.strictEqual(KO.twDeleted, true);
  assert.strictEqual(delVideo.removed, 1, 'delete removes the current twitch video');
  assert.ok(!KO.lastRect || KO.lastRect.width !== 1280 || KO.lastRect.height !== 720, 'tw-delete must not prime full viewport lastRect');
  assert.strictEqual(KO.wrap.style.display, 'block', 'wrap kept visible in the loading state');
  assert.ok(!KO.wrap.style.width || KO.wrap.style.width !== '1280px', 'wrap not forced to viewport width without a measured rect');
  // hideWrapForProbe() respects twDeleted (hiding would reveal a blank page),
  // so the badge — not the wrap — is what tells the user the layer is dead.
  const bK = lastBadge();
  assert.strictEqual(bK.text, 'YT', 'the yt failure is still reported with the wrap up');
  assert.strictEqual(bK.color, '#6b7280', 'gray = failed mint, not the amber loading state');

  // SPA re-render: a NEW video element appears -> the observer re-applies the delete.
  assert.ok(moInstances.length >= 1, 'tw-delete MutationObserver registered');
  const mo = moInstances[moInstances.length - 1];
  const reVideo = videoEl('tw-main-video-rendered');
  fakeVideos.push(reVideo);
  mo.cb([{ addedNodes: [reVideo] }]);
  assert.strictEqual(reVideo.removed, 1, 'observer re-applies the delete on SPA re-render');

  // Observer is idle while the user is in twitch mode.
  KO.player = 'twitch';
  const twVideo = videoEl('tw-main-video-2');
  fakeVideos.push(twVideo);
  mo.cb([{ addedNodes: [twVideo] }]);
  assert.strictEqual(twVideo.removed, 0, "observer no-ops while player === 'twitch'");
  KO.player = 'youtube';
  console.log('K tw_delete: video removed, wrap kept up without viewport priming, observer re-applies — OK');

  // ---- L: yt frame-death watchdog -------------------------------------------
  KO.player = 'youtube';
  KO.ytState = { ready: true, playing: true, muted: false, live: true, dur: 0, ct: 0, error: 0 };
  KO.lastYtSt = Date.now() - 31000; // bridge silent > 30s
  KO.ytWin = { postMessage() {} };
  KO.ytHlsUrl = 'https://hls.example/u1';
  KO.ytHlsAt = Date.now();
  KO.ytHlsFailed = false;
  KO.kickSlug = 'rodil'; // same-handle — gate keeps youtube
  KO.wrap = KO.wrap || wrap;
  KO.wrap.style.display = 'block'; // watchdog only runs while the layer is shown
  fakeFrame = { ...el('iframe'), id: 'ko-yt' };
  api.rectTick();
  assert.strictEqual(fakeFrame.removed, 1, 'watchdog removes the dead yt frame');
  assert.strictEqual(KO.ytState.ready, false, 'watchdog resets ready');
  assert.strictEqual(KO.ytWin, null, 'watchdog clears ytWin');
  assert.strictEqual(KO.lastYtSt, 0, 'watchdog resets the silence clock');
  // A recent st heartbeat keeps the frame alive.
  KO.ytState = { ready: true, playing: true, muted: false, live: true, dur: 0, ct: 0, error: 0 };
  KO.ytWin = { postMessage() {} };
  KO.lastYtSt = Date.now();
  fakeFrame = { ...el('iframe'), id: 'ko-yt' };
  api.rectTick();
  assert.strictEqual(fakeFrame.removed, 0, 'recent st heartbeat keeps the frame');
  console.log('L yt watchdog: 30s silence removes the frame, resets ready/ytWin — OK');

  // ---- M/N/O/P: kick overlay UI fixes (fs icon, bar hot, badge offline, no viewport prime) ----
  const makeNode = (tag) => {
    const node = {
      tagName: (tag || 'div').toUpperCase(),
      nodeType: 1,
      id: '',
      innerHTML: '',
      textContent: '',
      style: { display: '' },
      dataset: {},
      children: [],
      attrs: {},
      _text: '',
      classList: {
        _s: new Set(),
        add(...a) { a.forEach((x) => this._s.add(x)); },
        remove(...a) { a.forEach((x) => this._s.delete(x)); },
        contains(x) { return this._s.has(x); },
        toggle(x, force) { const want = force === undefined ? !this._s.has(x) : !!force; if (want) this._s.add(x); else this._s.delete(x); return want; },
      },
      appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
      remove() { this.removed = (this.removed || 0) + 1; },
      _ev: {},
      addEventListener(type, fn) { this._ev[type] = fn; },
      removeEventListener(type, fn) { if (this._ev[type] === fn) delete this._ev[type]; },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
      querySelector(sel) {
        if (!sel || sel[0] !== '#') return null;
        const want = sel.slice(1);
        const all = [];
        const walk = (n) => { all.push(n); n.children.forEach(walk); };
        walk(this);
        return all.find((n) => n.id === want) || null;
      },
      querySelectorAll() { return []; },
      getBoundingClientRect: () => ({ left: 10, top: 20, width: 400, height: 300 }),
      isConnected: true,
      offsetWidth: 400,
      offsetHeight: 300,
    };
    Object.defineProperty(node, 'innerHTML', {
      set(html) {
        node._html = html;
        node.children = [];
        const re = /<(button|div|span)[^>]*id="([^"]+)"/g;
        let m;
        while ((m = re.exec(html))) {
          const child = makeNode(m[1]);
          child.id = m[2];
          node.appendChild(child);
        }
      },
      get() { return node._html || ''; },
    });
    // textContent is DOM-faithful: assigning '' (renderQualityMenu's
    // re-render reset) drops children; reads aggregate the subtree.
    Object.defineProperty(node, 'textContent', {
      set(v) { node._text = String(v); node.children.length = 0; },
      get() {
        if (node.children.length) return node.children.map((c) => c.textContent).join('');
        return node._text;
      },
    });
    return node;
  };

  const mountDoc = {
    head: doc.head,
    body: makeNode('body'),
    documentElement: doc.documentElement,
    hidden: false,
    hasFocus: () => true,
    fullscreenElement: null,
    createElement: (t) => makeNode(t),
    getElementById: () => null,
    querySelector: (sel) => (sel === 'main' ? mountDoc.body : null),
    querySelectorAll: (sel) => (sel === 'video' ? fakeVideos : []),
    addEventListener() {},
    removeEventListener() {},
  };
  const mountApi = new Function('window', 'document', 'location', 'navigator', src + '\n;return {KO, mount, updateKickBar, teardown, injectStyles, rectTick, syncMute, setupHotBar, teardownHotBar, renderQualityMenu};')(
    global, mountDoc, global.location, global.navigator,
  );
  const mKO = mountApi.KO;
  mountApi.injectStyles();
  mountApi.mount();
  assert.ok(mKO.wrap, 'mount creates wrap');
  const fsNode = mKO.wrap.querySelector('#ko-fs');
  assert.ok(fsNode && fsNode.innerHTML.includes('viewBox'), 'fullscreen button receives the fs SVG icon');
  assert.ok(mKO.wrap.classList.contains('ko-hot'), 'wrap starts hot so the control bar is visible');
  fakeFrame = { contentWindow: { postMessage() {} }, dataset: { koToken: 'message-token' } };
  mountDoc.getElementById = (id) => (id === 'ko-yt' ? fakeFrame : null);
  msgListeners.forEach((listener) => listener({
    source: fakeFrame.contentWindow,
    origin: new URL(chromeStub.runtime.getURL('player.html')).origin,
    data: { __koKick: { t: 'ready', _koToken: 'message-token' } },
  }));
  fakeVideos.length = 0;
  const nativeVideo = { ...videoEl('native'), play() { this.paused = false; return Promise.resolve(); } };
  fakeVideos.push(nativeVideo);
  nativeVideo.paused = false;
  nativeVideo.muted = false;
  mKO.player = 'youtube';
  mKO.wrap.style.display = 'block';
  mountApi.syncMute();
  assert.strictEqual(nativeVideo.paused, true, 'overlay pauses Twitch playback');
  assert.strictEqual(nativeVideo.muted, true, 'overlay mutes Twitch playback');
  mKO.wrap.style.display = 'none';
  mountApi.rectTick();
  assert.strictEqual(nativeVideo.paused, false, 'hidden failed overlay resumes Twitch playback');
  assert.strictEqual(nativeVideo.muted, false, 'hidden failed overlay unmutes Twitch playback');
  fakeVideos.length = 0;
  console.log('M fs icon: mount assigns KO_SVG.fs to #ko-fs — OK');

  // ---- P3: the LIVE return-to-live pill (owner pain 3 — "no clickable LIVE
  // button"). A <button id="ko-golive"> lives in #ko-bar and is shown by
  // updateKickBar() only when the playhead sits behind the edge (lat ≥ 1
  // arrow step) or on the DVR url; clicking jumps to live via the bridge.
  const p3posts = [];
  const fakeFrameKick = { ...el('iframe'), id: 'ko-ivs', dataset: { koToken: 'p3kick' }, contentWindow: { postMessage: (m) => p3posts.push(m) } };
  fakeFrame = { ...el('iframe'), id: 'ko-yt', dataset: { koToken: 'p3yt' }, contentWindow: { postMessage: (m) => p3posts.push(m) } };
  mountApi.mount();
  assert.ok(mKO.wrap, 'mountApi exposed for the pill test');
  const goliveP3 = mKO.wrap.querySelector('#ko-golive');
  assert.ok(goliveP3, 'mount creates the #ko-golive pill');
  mountDoc.getElementById = (id) => (id === 'ko-yt' ? fakeFrame : id === 'ko-ivs' ? fakeFrameKick : null);
  mKO.player = 'youtube';
  mKO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 40, dur: 60, lat: 7 };
  mKO.wrap.style.display = 'block';
  mKO.ytHlsUrl = 'https://hls.example/p3';
  mountApi.updateKickBar();
  assert.ok(goliveP3.classList.contains('ko-show'), 'kt leg: pill shows while lat ≥ ARROW_SEEK_SEC');
  assert.ok(goliveP3.textContent.match(/LIVE\s*−\s*7s/), 'kt leg: pill labelled with the live throttle (LIVE −7s at lat=7)');
  // click → ytCmd('seekToLive') → post {t:'seekToLive'}
  goliveP3._ev.click();
  const p3ytPosts = p3posts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive');
  assert.strictEqual(p3ytPosts.length, 1, 'yt leg: pill click posts seekToLive to the yt frame');
  assert.strictEqual(p3ytPosts[0].__koKick._koToken, 'p3yt', 'yt leg: seekToLive carries the yt frame token');
  // at the edge (lat < 1 step) the pill hides for yt
  mKO.ytState.lat = 0;
  mountApi.updateKickBar();
  assert.ok(!goliveP3.classList.contains('ko-show'), 'yt leg: pill hides at the live edge');
  // kick leg on DVR → clicking reloads the live url (kickBackToLive loads activeUrl)
  mKO.player = 'kick';
mKO.kickState = { state: 'Playing', pos: 10, lat: 18, dur: 3600, qualities: [], ts: Date.now() };
  mKO.kickOnDvr = true;
  mKO.activeUrl = 'https://ivs.example/live-p3';
  mKO.kickFrame = fakeFrameKick;
  mKO.kickWin = fakeFrameKick.contentWindow;
  mKO.wrap.style.display = 'block';
  mountApi.updateKickBar();
  assert.ok(goliveP3.classList.contains('ko-show'), 'kick leg: pill shows while lat ≥ ARROW_SEEK_SEC (and on DVR)');
  assert.ok(goliveP3.textContent.match(/LIVE\s*−\s*18s/), 'kick leg: pill labelled with the live throttle (LIVE −18s at lat=18)');
  p3posts.length = 0;
  goliveP3._ev.click();
  const p3kickPosts = p3posts.filter((m) => m && m.__koKick && m.__koKick.t === 'load');
  assert.strictEqual(p3kickPosts.length, 1, 'kick leg DVR: pill click loads the live url to go live (kickBackToLive)');
  assert.strictEqual(p3kickPosts[0].__koKick.url, 'https://ivs.example/live-p3', 'kick leg: reload targets activeUrl');
  // kick leg at edge (not DVR, lat 0) hides the pill
  mKO.kickOnDvr = false;
  mKO.kickState = { state: 'Playing', pos: 100, lat: 0, dur: 3600, qualities: [] , ts: Date.now() };
  mountApi.updateKickBar();
  assert.ok(!goliveP3.classList.contains('ko-show'), 'kick leg: pill hides when actually live');
  fakeFrame = null;
  p3posts.length = 0;
  console.log('P3: LIVE pill shows behind live / on DVR, hides at the edge, click jumps to live on both legs — OK');

  const styleEl2 = appends.find((e) => e.id === 'ko-style');
  assert.ok(styleEl2.textContent.includes('#ko-wrap:not(.ko-hot) #ko-bar{opacity:0;pointer-events:none;}'), 'bar auto-hides once the wrap goes cold');
  assert.ok(!styleEl2.textContent.includes('#ko-wrap:not(.ko-hot) #ko-bar{opacity:1;}'), 'stale always-visible bar rule purged');
  // The CSS keys on a class, so the contract only holds if the class cycles:
  // mount starts the wrap hot, and pointer activity re-arms it after it cools.
  assert.ok(mKO.wrap.classList.contains('ko-hot'), 'a fresh mount is hot (bar visible)');
  mKO.wrap.classList.remove('ko-hot');
  assert.ok(!mKO.wrap.classList.contains('ko-hot'), 'the wrap cools on the 2.6s timer');
  assert.ok(typeof mKO.wrap._ev.pointermove === 'function', 'pointer activity is wired on the wrap');
  mKO.wrap._ev.pointermove();
  assert.ok(mKO.wrap.classList.contains('ko-hot'), 'pointermove re-arms the hot class');
  mKO.wrap._ev.pointerdown();
  mKO.wrap._ev.mouseenter();
  assert.ok(mKO.wrap.classList.contains('ko-hot'), 'pointerdown/mouseenter keep it hot');
  mountApi.teardownHotBar();
  mKO.wrap.classList.remove('ko-hot');
  assert.ok(!mKO.wrap.classList.contains('ko-hot'), 'after teardown nothing re-arms the bar');
  console.log('N bar autohide: :not(.ko-hot) rule + hot class cycles on pointer activity — OK');

  const badgeWrap = {
    style: { display: 'block' },
    classList: null,
    querySelector: (sel) => {
      // 0.8.1+ has no LIVE badge element: the live/offline signal is a class
      // on the wrap. #ko-play is the only control updateKickBar still touches
      // through updatePlayUI/updateKickVolUI.
      if (sel === '#ko-play') return { dataset: {}, innerHTML: '' };
      return null;
    },
  };
  badgeWrap.classList = mkClassList(badgeWrap);
  badgeWrap.classList.add('ko-kick');
  KO.wrap = badgeWrap;
  KO.player = 'kick';
  KO.kickOnDvr = false;
  KO.kickState = null;
  api.updateKickBar();
  assert.ok(badgeWrap.classList.contains('ko-offline'), 'offline kickState hides LIVE badge via ko-offline');
  KO.kickState = { state: 'Playing', pos: 1, lat: 1, dur: 10 , ts: Date.now() };
  api.updateKickBar();
  assert.ok(!badgeWrap.classList.contains('ko-offline'), 'playing kickState shows LIVE badge');
  KO.kickOnDvr = true;
  KO.kickState = { state: 'Paused', pos: 1, lat: 1, dur: 10 , ts: Date.now() };
  api.updateKickBar();
  assert.ok(!badgeWrap.classList.contains('ko-offline'), 'DVR mode keeps badge visible');
  console.log('O badge offline: ko-offline toggles with kick playback state — OK');

  KO.lastRect = null;
  const delListener2 = runtimeMsgListeners[0];
  await new Promise((res) => delListener2({ type: 'ko-delete-twitch' }, {}, () => res()));
  assert.ok(!KO.lastRect || KO.lastRect.width !== global.innerWidth || KO.lastRect.height !== global.innerHeight, 'tw-delete without video does not prime viewport lastRect');
  console.log('P tw-delete: no viewport lastRect priming — OK');

  mountApi.teardown();

  // ---- Q: rectFromHost resolves the host player rect (twitch vs YT) --------
  const qRectFn = api.rectFromHost;
  const savedLoc = { ...global.location };
  try {
    Object.assign(global.location, { hostname: 'www.youtube.com', pathname: '/watch', search: '?v=Pu7xsoh-K6k' });
    setPageSel('#movie_player', el('div'));
    const player = byId.get('mp-sel') || (byId.has('mp-sel') ? null : null);
    // setPageSel stores raw nodes, so reach into the pageSel map.
    const mp = pageSel.get('#movie_player');
    assert.ok(mp, '#movie_player selectable on a YT watch page');
    mp.getBoundingClientRect = () => ({ left: 8, top: 9, width: 640, height: 360 });
    const r1 = qRectFn();
    assert.strictEqual(r1.left, 8, 'rectFromHost uses #movie_player first on www.youtube.com');
    assert.strictEqual(r1.width, 640, '#movie_player width used');
    // Fallback player (embed players expose .html5-video-player, not #movie_player).
    pageSel.delete('#movie_player');
    setPageSel('.html5-video-player', el('div'));
    const hvp = pageSel.get('.html5-video-player');
    hvp.getBoundingClientRect = () => ({ left: 100, top: 200, width: 1280, height: 720 });
    const r2 = qRectFn();
    assert.strictEqual(r2.width, 1280, 'falls back to .html5-video-player');
    // No player -> null; zero-size rect -> null.
    pageSel.delete('.html5-video-player');
    assert.strictEqual(qRectFn(), null, 'no YT player -> null');
    setPageSel('#movie_player', el('div'));
    const z = pageSel.get('#movie_player');
    z.getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0 });
    assert.strictEqual(qRectFn(), null, 'zero-size rect -> null');
  } finally {
    Object.assign(global.location, savedLoc);
    pageSel.clear();
  }
  // Twitch branch: rect comes from the twitch video element.
  Object.assign(global.location, { hostname: 'www.twitch.tv', pathname: '/rodil', search: '' });
  fakeVideos.length = 0;
  const twV = videoEl('tw-main-video');
  fakeVideos.push(twV);
  assert.deepStrictEqual(qRectFn(), { left: 0, top: 0, width: 100, height: 60 }, 'twitch anchor rect = video rect (no anchor class)');
  fakeVideos.length = 0;
  console.log('Q rectFromHost: #movie_player -> .html5-video-player -> null/zero-size -> twitch video rect — OK');

  // ---- R: resolveYtPageKick tier order + host-aware currentSlug ------------
  // The YT-page identity helpers read <meta itemprop=channelId>, <link
  // itemprop=url>, <meta itemprop=name> and /watch?v= via pageSel + location.
  const R = api.resolveYtPageKick;
  const R_setYt = (opts) => {
    pageSel.clear();
    Object.assign(global.location, {
      hostname: 'www.youtube.com', pathname: '/watch', search: '?v=zzzzzz',
    });
    if (opts.cid) {
      const m = el('meta'); m.getAttribute = (k) => (k === 'content' ? opts.cid : null);
      setPageSel('meta[itemprop="channelId"]', m);
    }
    if (opts.handle) {
      const lk = el('link'); lk.getAttribute = (k) => (k === 'href' ? '/@' + opts.handle : null);
      setPageSel('link[itemprop="url"]', lk);
    }
    if (opts.name) {
      const nm = el('meta'); nm.getAttribute = (k) => (k === 'content' ? opts.name : null);
      setPageSel('meta[itemprop="name"]', nm);
    }
  };
  // Tier 1: ytId exact match (case-insensitive).
  KO.mappings = { gaules: { yt: 'Gaules', ytId: 'UCgXYe0X202GT0lxXH6pGXZg' } };
  R_setYt({ cid: 'UCGXYE0X202GT0LXXH6PGXZG' });
  assert.strictEqual(R().via, 'ytId', 'tier 1: cid matches ytId (case-insensitive)');
  assert.strictEqual(R().slug, 'gaules', 'tier 1 resolves the owning table row');
  // Tier 2: yt handle/name match on the page.
  KO.mappings = { jb: { yt: '@JBSniperPRIME', ytId: 'UCother' } };
  R_setYt({ cid: 'UCJJJJJJJJJJJJJJJJJJJJJJ', handle: 'jbsniperprime' });
  assert.strictEqual(R().via, 'yt', 'tier 2: page handle matches a stored yt value');
  assert.strictEqual(R().slug, 'jb', 'tier 2 resolves the row');
  // Tier 3a: stored kick slug matches the page handle.
  KO.mappings = { sp: { kick: 'spreen', yt: 'SpreenDMC' } };
  R_setYt({ cid: 'UCAAAAAAAAAAAAAAAAAAAAAA', handle: 'spreen' });
  assert.strictEqual(R().via, 'handle', 'tier 3a: stored kick slug === page handle');
  assert.strictEqual(R().slug, 'sp', 'tier 3a resolves the row');
  // Tier 3b: no stored row, same-handle doctrine.
  KO.mappings = {};
  R_setYt({ cid: 'UCAAAAAAAAAAAAAAAAAAAAAA', handle: 'spreen' });
  const t3b = R();
  assert.strictEqual(t3b.via, 'handle', 'tier 3b: synthetic same-handle');
  assert.strictEqual(t3b.slug, 'host:yt:@spreen', 'tier 3b synthetic host-scoped slug');
  // Tier 4: videoId fallback (watch page, no metadata).
  KO.mappings = {};
  R_setYt({});
  assert.strictEqual(R().via, 'videoId', 'tier 4: watch videoId fallback');
  assert.strictEqual(R().slug, 'host:yt:zzzzzz', 'tier 4 host:yt:<videoId> slug');
  // Tier 5: ytId fallback (metadata UC only, untracked channel). A non-watch
  // page (channel page) carries channelId but no video id.
  KO.mappings = {};
  R_setYt({ cid: 'UCDDDDDDDDDDDDDDDDDDDDDD' });
  Object.assign(global.location, { hostname: 'www.youtube.com', pathname: '/channel/UCDDDDDDDDDDDDDDDDDDDDDD', search: '' });
  const t5 = R();
  assert.strictEqual(t5 && t5.via, 'ytId-fallback', 'tier 5: bare ytId fallback');
  assert.strictEqual(t5.slug, 'host:yt:UCDDDDDDDDDDDDDDDDDDDDDD', 'tier 5 host:yt:<UC> slug');
  // Unknown /@handle page -> synthetic same-handle (still a valid identity).
  R_setYt({ handle: 'xyz' });
  Object.assign(global.location, { pathname: '/@xyz', search: '' });
  pageSel.delete('meta[itemprop="channelId"]');
  assert.strictEqual(R().via, 'handle', '@handle page resolves same-handle even for an unmapped handle');
  assert.strictEqual(R().slug, 'host:yt:@xyz', '@handle synthetic slug');
  // YouTube HOME page: no cid, no handle, no video -> no overlay identity.
  pageSel.clear();
  Object.assign(global.location, { hostname: 'www.youtube.com', pathname: '/', search: '' });
  assert.strictEqual(R(), null, 'youtube home: no cid/handle/videoId -> null');
  // host-aware currentSlug.
  R_setYt({ handle: 'gaules' });
  Object.assign(global.location, { pathname: '/@gaules', search: '' });
  assert.strictEqual(api.currentSlug(), 'host:yt:@gaules', 'YT handle page -> host-scoped slug');
  Object.assign(global.location, { pathname: '/watch', search: '?v=Pu7xsoh-K6k' });
  pageSel.clear();
  assert.strictEqual(api.currentSlug(), 'host:yt:pu7xsoh-k6k', 'YT watch page -> host:yt:videoId slug');
  Object.assign(global.location, savedLoc);
  pageSel.clear();
  setPageSel('main', el('div'));
  Object.assign(global.location, { hostname: 'www.twitch.tv', pathname: '/Gaules', search: '' });
  assert.strictEqual(api.currentSlug(), 'gaules', 'twitch channel path -> plain slug');
  Object.assign(global.location, { pathname: '/' });
  assert.strictEqual(api.currentSlug(), null, 'twitch root -> null');
  Object.assign(global.location, savedLoc);
  pageSel.clear();
  console.log('R resolveYtPageKick: ytId>yt>handle>videoId>ytId-fallback tiers + host-aware currentSlug — OK');

  // ---- S: probe binds the table row; a fresh mappings object re-resolves --
  KO.mappings = { srow: { yt: '@s', ytId: 'UCSSSSSSSSSSSSSSSSSSSSSS' } };
  KO.enabled = true;
  KO.player = 'youtube';
  KO.slug = 'srow';
  KO.ytPageId = null;
  KO.ytBoundMappings = null;
  KO.activeUrl = null;
  KO.ytHlsUrl = null;
  KO.ytHlsFailed = false;
  KO.wrap = null;
  KO.kickFrame = null;
  KO.kickWin = null;
  fakeVideos.length = 0;
  Object.assign(global.location, { hostname: 'www.youtube.com', pathname: '/watch', search: '?v=abcdef' });
  pageSel.clear();
  const sMeta = el('meta'); sMeta.getAttribute = (k) => (k === 'content' ? 'UCSSSSSSSSSSSSSSSSSSSSSS' : null);
  setPageSel('meta[itemprop="channelId"]', sMeta);
  const sRowBind = KO.mappings;
  await api.probe();
  assert.strictEqual(KO.slug, 'srow', 'probe binds the table row key as slug');
  assert.strictEqual(KO.ytPageTarget && KO.ytPageTarget.slug, 'srow', 'ytPageTarget records the resolved row');
  assert.ok(KO.ytBoundMappings === sRowBind, 'ytBoundMappings === the table identity the row was bound from');
  // A fresh mappings object (storage.onChanged replaces it) must force a re-resolve.
  KO.mappings = { srow: { yt: '@s', ytId: 'UCSSSSSSSSSSSSSSSSSSSSSS' }, alt: { yt: '@alt' } };
  assert.notStrictEqual(KO.mappings, sRowBind, 'fresh mappings object changes identity');
  Object.assign(global.location, { pathname: '/watch', search: '?v=abcdef' });
  await api.probe();
  assert.ok(KO.ytBoundMappings === KO.mappings, 're-probe re-binds to the new table identity');
  assert.strictEqual(KO.slug, 'srow', 're-resolve still lands on the ytId row');
  Object.assign(global.location, savedLoc);
  pageSel.clear();
  console.log('S probe binding: row key + ytBoundMappings identity re-resolution — OK');

// ---- T: eager dual-mount class-flip preserves iframe identity -----------
  // Drive the real storage.onChanged path: a player switch (youtube<->kick)
  // must swap ko-yt/ko-kick on the SAME wrap and the SAME iframe objects,
  // with no teardown (the eager dual mount keeps both players ready).
  await api.teardown();
  KO.wrap = null;
  fakeFrame = null;
  fakeVideos.length = 0;
  Object.assign(global.location, { hostname: 'www.twitch.tv', pathname: '/rodil', search: '' });
  KO.enabled = true;
  KO.player = 'kick';
  KO.slug = 'rodil';
  KO.kickSlug = 'realkick';
  KO.mappings = { rodil: { kick: 'realkick', yt: '@x' } };
  KO.ytRaw = '@x';
  KO.ytId = 'UCRRRRRRRRRRRRRRRRRRRRRR';
  const T_VIDEO = videoEl('tw-main-video');
  fakeVideos.push(T_VIDEO);
  // A LIVE kick (fetch resolves a playback_url) so the layer actually mounts
  // a frame whose identity we can assert survives a flip.
  const realFetch = global.fetch;
  global.fetch = () => Promise.resolve({
    ok: true, json: () => Promise.resolve({
      livestream: { playback_url: 'https://ivs.example/live-flip', id: 's1' },
    }),
  });
  KO.ytHlsUrl = 'https://hls.example/live-flip';
  KO.ytHlsAt = Date.now();
  KO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 5, dur: 60, lat: 2 };
  fakeFrame = { ...el('iframe'), id: 'ko-yt', dataset: { koToken: 't' }, contentWindow: { postMessage() {} } };
  // Mount a kick layer, then flip to youtube via a storage player change.
  await api.probe();
  assert.ok(KO.wrap, 'flip: wrap is mounted after the kick probe');
  assert.ok(KO.kickFrame, 'flip: kick frame is mounted (live url)');
  assert.ok(KO.wrap.classList.contains('ko-kick') && !KO.wrap.classList.contains('ko-yt'), 'flip: starts on the kick layer');
  const tWrap = KO.wrap;
  const tKickFrame = KO.kickFrame;
  const tSeq = KO.frameSeq;
  const fireFlip = (player) => {
    // Mirror the popup's write timing: the listener's session-fallback guard
    // compares the incoming player against the CURRENT preference, so the
    // preference must still hold the pre-flip player when the event lands.
    KO.playerPreference = KO.player;
    // Mirror chrome.storage.onChanged: enabled+player+mappings drive apply().
    console.log('T diag fireFlip enter: listener=', !!storageChangedListener, 'target=', player, 'pref-before=', KO.playerPreference);
    const _r = storageChangedListener(
      { 'ko.v2': {
        newValue: { enabled: true, player, mappings: KO.mappings, vols: undefined },
        oldValue: { enabled: true, player: KO.player, mappings: KO.mappings, vols: undefined },
      } },
      'local',
    );
    void _r;
  };
  fireFlip('youtube'); // pChanged (player differs) -> fire(apply)
  console.log('T diag sync-after-fireFlip: player=', KO.player, 'pref=', KO.playerPreference);
  await settle(50);
  console.log('T diag flip-yt: player=', KO.player, 'pref=', KO.playerPreference, 'wrap=', !!KO.wrap, 'classes=', KO.wrap && KO.wrap.classList.value, 'ytState=', JSON.stringify(KO.ytState), 'ytHlsUrl=', KO.ytHlsUrl, 'ytHlsFailed=', KO.ytHlsFailed, 'ytEmbedAt=', KO.ytEmbedAt);
  assert.strictEqual(KO.wrap, tWrap, 'flip to youtube keeps the SAME wrap (no teardown)');
  assert.ok(KO.wrap.classList.contains('ko-yt') && !KO.wrap.classList.contains('ko-kick'), 'flip: class swapped to ko-yt');
  if (tKickFrame) assert.strictEqual(KO.kickFrame, tKickFrame, 'flip keeps the SAME kick iframe object');
  assert.strictEqual(tKickFrame.removed, 0, 'kick iframe never removed across a flip');
  const ytYf = document.getElementById('ko-yt');
  // Flip back to kick.
  fireFlip('kick');
  await settle(50);
  assert.strictEqual(KO.wrap, tWrap, 'flip back to kick keeps the SAME wrap');
  assert.ok(KO.wrap.classList.contains('ko-kick') && !KO.wrap.classList.contains('ko-yt'), 'flip back: class swapped back to ko-kick');
  assert.strictEqual(tKickFrame.removed, 0, 'second flip still does not remove the kick iframe');
  if (ytYf) assert.strictEqual(ytYf.removed, 0, 'yt iframe never removed across flips');
  assert.strictEqual(KO.frameSeq, tSeq, 'frameSeq unchanged across class flips (no rebuild)');
  fakeVideos.length = 0;
  await api.teardown();
  global.fetch = realFetch; // restore the rejecting stub for the offline cases
  Object.assign(global.location, savedLoc);
  pageSel.clear();
  console.log('T class-flip: eager dual mount flips ko-kick/ko-yt on SAME wrap + iframe identity, no teardown — OK');

  // ---- U: first-run kickMuted:true + no-clobber of a stored choice ---------
  KO.kickMuted = false;
  storage.ko = undefined;
  await api.loadState();
  assert.strictEqual(KO.kickMuted, true, 'first run defaults kickMuted:true (owner: no audio leaks)');
  KO.kickMuted = true;
  storage.ko = { vols: { kick: { v: 0.4, m: false } } };
  await api.loadState();
  assert.strictEqual(KO.kickMuted, false, 'a stored muted:false choice is NOT clobbered');
  assert.strictEqual(KO.kickVol, 0.4, 'stored kick volume applied');
  storage.ko = undefined;
  console.log('U mute: first-run kickMuted:true; stored muted:false preserved — OK');

  // ---- V: levels -> vertical quality menu -> setLevel ----------------------
  // Reuse the mountApi's real id-tree: renderQualityMenu reads #ko-quality-menu.
  // K's mountApi.teardown() nulled mKO.wrap — re-mount to rebuild the id tree.
  mountApi.mount();
  const vWrap = mKO.wrap; // makeNode-based, querySelector resolves real ids
  mKO.player = 'kick';
  mKO.kickState = { state: 'Playing', pos: 10, qualities: [{ name: '1080p', id: 0 , ts: Date.now() }, { h: 720, id: 1 }] };
  // The quality menu only renders when the bar is open; drive renderQualityMenu directly.
  mountApi.renderQualityMenu();
  const qMenu = vWrap.querySelector('#ko-quality-menu');
  assert.ok(qMenu, 'quality menu node exists after render');
  const labels = qMenu.children.map((c) => (c.textContent || c.innerHTML || '').trim());
  assert.ok(labels.includes('Auto'), 'Auto entry present');
  assert.ok(labels.includes('1080p'), '1080p entry present');
  assert.ok(labels.includes('720p'), '720p (h-based label) present');
  assert.strictEqual(qMenu.children.length, 3, 'three entries, no stacking after one render');
  // Re-render must not stack.
  mountApi.renderQualityMenu();
  mountApi.renderQualityMenu();
  assert.strictEqual(qMenu.children.length, 3, 're-render does not stack entries');
  // Only Auto is aria-checked initially.
  const checked = qMenu.children.filter((c) => c.getAttribute('aria-checked') === 'true').length;
  assert.strictEqual(checked, 1, 'exactly one aria-checked (Auto) initially');
  console.log('V quality: vertical menu Auto/1080p/720p, no re-render stacking, single check — OK');

  // ---- W: black-box regression — kick offline + twDeleted -------------------
  // Repro shape: kick is live over a deleted Twitch player (twDeleted pins
  // the wrap at the last rect), then the kick stream ENDS. The probe's
  // offline branch is terminal — nothing is loading — so the wrap's #000
  // background must not stay up: that is the empty black box over the page.
  await api.teardown();
  Object.assign(global.location, { hostname: 'www.twitch.tv', pathname: '/rodil', search: '' });
  pageSel.clear();
  fakeVideos.length = 0;
  KO.mappings = { rodil: { kick: 'realkick', yt: '@x' } };
  KO.enabled = true;
  KO.player = 'kick';
  KO.slug = 'rodil';
  KO.kickSlug = 'realkick';
  KO.ytRaw = '@x';
  KO.ytId = 'UCWKKKKKKKKKKKKKKKKKKKKK';
  KO.twDeleted = true;
  KO.lastRect = { left: 10, top: 20, width: 400, height: 300 };
  KO.activeUrl = null;
  KO.kickUrlT = 0;
  KO.kickState = null;
  KO.kickOnDvr = false;
  KO.reconnectCount = 0;
  KO.kickEverPlayed = false;
  KO.ytHlsFailed = false;
  KO.hideTicks = 0;
  const realFetchW = global.fetch;
  global.fetch = () => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ livestream: { playback_url: 'https://ivs.example/dying', id: 'w1' } }),
  });
  api.mount();
  await api.probe(); // kick LIVE: the layer mounts and the wrap covers the deleted-player slot
  assert.strictEqual(KO.wrap.style.display, 'block', 'a live kick keeps the wrap up under twDeleted');
  global.fetch = realFetchW; // the broadcast ended: the API now answers offline
  KO.kickUrlT = Date.now() - 30000; // load grace expired — the loading branches no longer apply
  badge.length = 0;
  await api.probe();
  const bW = lastBadge();
  assert.strictEqual(bW.text, 'KICK', 'the offline badge still reports the chosen layer');
  assert.strictEqual(bW.color, '#6b7280', 'gray = offline');
  assert.strictEqual(KO.wrap.style.display, 'none', 'kick offline + twDeleted HIDES the wrap — no display without a playable source');
  api.rectTick();
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'none', 'the rect loop must not hold/re-show the wrap over a dead kick stream');
  // Controls — the pin stays intact for states that DO have a source:
  KO.wrap.style.display = 'block';
  KO.kickUrlT = Date.now(); // url inside the load grace is still loading
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', 'a loading kick keeps its pinned wrap');
  assert.strictEqual(KO.wrap.style.width, '400px', 'the last rect is still re-applied while pinned');
  KO.kickUrlT = 0;
  // 0.8.5 contract: the transient arms are only display-worthy while the poll
  // loop still confirms the source. A genuine reconnect-in-flight has that —
  // the last confirming probe stamped the clock (the confirming branch re-
  // anchors it every 20s). The stopped-clock case is pinned in Y2 below.
  KO.kickProbeAt = Date.now();
  KO.reconnectCount = 1; // a reconnect in flight shows the RECONNECTING chip
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', 'a reconnect in flight keeps the wrap up');
  KO.reconnectCount = 0;
  KO.player = 'youtube'; // K's contract: the yt loading placeholder pin is untouched
  KO.ytProbeAt = Date.now(); // the last youtube probe found it loading (0.8.4 clock)
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', 'the youtube twDeleted placeholder pin is unchanged');
  KO.player = 'kick';
  KO.twDeleted = false;
  KO.lastRect = null;
  fakeVideos.length = 0;
  console.log('W black box: kick offline + twDeleted hides the wrap, no re-show across rectTick, live/loading pins intact — OK');

  // ---- X: the twDeleted keep-up gate is PER-PLAYER (owner 0.8.3: youtube
  // "dead" + players bugged on switching). 0.8.3 asked kickLayerLive() even
  // while KO.player === 'youtube': a dead yt mint under a deleted Twitch
  // player kept the black wrap pinned forever (the keep-up branch re-shows /
  // holds whatever the gate does not hide). Invariant: never displayed
  // without a playable source FOR THE ACTIVE PLAYER.
  await api.teardown();
  Object.assign(global.location, { hostname: 'www.twitch.tv', pathname: '/rodil', search: '' });
  pageSel.clear();
  fakeVideos.length = 0;
  KO.mappings = { rodil: { kick: 'rodil', yt: '@x' } }; // same-handle kick: no handover
  KO.enabled = true;
  KO.player = 'youtube';
  KO.slug = 'rodil';
  KO.kickSlug = 'rodil';
  KO.ytRaw = '@x';
  KO.ytId = 'UCXXXXXXXXXXXXXXXXXXXXXX';
  KO.twDeleted = true;
  KO.lastRect = { left: 10, top: 20, width: 400, height: 300 };
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now();
  KO.ytEmbedAt = Date.now() - 10000;
  KO.ytProbeAt = 0;
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, lat: 0, error: 0 };
  api.mount();
  KO.wrap.style.display = 'block'; // the loading-state keep-up left it up
  api.rectTick();
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'none', '0.8.3 defect: a DEAD youtube layer under twDeleted kept the blank wrap pinned — the keep-up gate asked only about kick');
  // A youtube layer still probing (mint healthy) keeps its placeholder pin.
  KO.ytHlsFailed = false;
  KO.ytProbeAt = Date.now();
  KO.wrap.style.display = 'block';
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', 'a minting/loading yt layer keeps the pinned placeholder (probe-grace)');
  // A live youtube layer pins too (the placeholder becomes the player) — with
  // the evidence a live layer actually has: a bridge that is still talking.
  KO.ytProbeAt = 0;
  KO.ytState.live = true;
  KO.lastYtSt = Date.now();
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', 'a live yt layer with a fresh bridge is pinned under twDeleted');
  console.log('X per-player keep-up: dead yt hides, loading/live yt pins — OK');

  // ---- X3: the owner's 0.8.4 black box (symptom 1). The bridge posts its
  // state every second, so KO.ytState.live is a STICKY last-known value that
  // outlives the source: a drained/ended element, a killed page, or a frame
  // whose tab was hidden all keep reporting the last transient state. 0.8.4
  // trusted it, so a dead yt layer under twDeleted stayed a pinned black box
  // forever — nothing ever cleared it. 0.8.5 requires the bridge to be FRESH
  // (within 3 polls, the window the frame-death watchdog uses for hidden-tab
  // throttling) or the probe clock to still be running.
  KO.ytProbeAt = 0;
  KO.ytState.live = true;
  KO.lastYtSt = Date.now() - 61000; // 3 * POLL_MS + 1s: the bridge went silent
  KO.wrap.style.display = 'block';
  api.rectTick();
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'none', '0.8.4 defect: a stale live flag with a silent bridge pinned the black wrap forever');
  // Inside the freshness window the same flag still pins (throttled tab).
  KO.lastYtSt = Date.now() - 55000;
  KO.wrap.style.display = 'block';
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', 'a throttled-but-fresh bridge (window = 3 polls, not 30s) still pins the layer');
  console.log('X3 stale live flag: silent bridge stops pinning a dead yt layer — OK');

  // ---- X2: a MANUAL switch to youtube is a retry request (owner 0.8.3
  // symptom 2: "switching players leaves players bugged"). 0.8.3 carried the
  // stale ytHlsFailed evidence across the flip, so the probe re-entered the
  // youtube branch, hit the 30s mint backoff with the flag still set, and
  // silently handed the user straight back to kick. User intent on an
  // explicit switch is "try again": the switch sites clear the evidence so
  // this probe re-mints, and only a FRESH failure may hand over.
  await api.teardown();
  Object.assign(global.location, { hostname: 'www.twitch.tv', pathname: '/rodil', search: '' });
  fakeVideos.length = 0;
  KO.enabled = true;
  KO.player = 'kick';
  KO.playerPreference = 'kick';
  KO.slug = 'rodil';
  KO.kickSlug = 'realkick'; // a REAL kick mapping: the handover gate is armed
  KO.mappings = { rodil: { kick: 'realkick', yt: '@x' } };
  KO.ytRaw = '@x';
  KO.ytId = 'UCXXXXXXXXXXXXXXXXXXXXXX';
  KO.twDeleted = false;
  KO.ytHlsFailed = true;
  KO.ytHlsFailedAt = Date.now(); // inside the 30s backoff window
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  storage.ko = undefined;
  storageChangedListener(
    { 'ko.v2': {
      newValue: { enabled: true, player: 'youtube', mappings: KO.mappings },
      oldValue: { enabled: true, player: 'kick', mappings: KO.mappings },
    } },
    'local',
  );
  assert.strictEqual(KO.player, 'youtube', 'the popup flip sets youtube mode');
  assert.strictEqual(KO.ytHlsFailed, false, 'a manual switch to youtube must drop the stale mint failure');
  await settle(60); // the flip's apply() -> probe() -> re-mint round-trip
  assert.strictEqual(KO.player, 'youtube', '0.8.3 defect: the flip back to youtube handed straight back to kick on stale failure evidence');
  assert.strictEqual(KO.ytHlsUrl, 'https://hls.example/u1', 'the switch retried the mint and got a fresh url');
  KO.player = 'kick';
  KO.playerPreference = 'kick';
  storage.ko = undefined;
  console.log('X2 manual switch: youtube flip re-mints instead of handing back to kick — OK');

  // ---- Y: a kick stream the 20s poll confirmed LIVE is never hidden by the
  // 400ms loop (owner 0.8.3: "kick UI gone while the channel IS live").
  // 0.8.3 anchored keep-up to kickUrlT (a single url-attach timestamp): a
  // first frame slower than 25s — normal on IVS url rotations — made the
  // gate hide a genuinely-live layer, fighting every showKickLayer.
  KO.twDeleted = true; // the keep-up gate under test only runs on this path
  KO.lastRect = { left: 10, top: 20, width: 400, height: 300 };
  api.mount();
  KO.player = 'kick';
  KO.ytState.live = false;
  KO.activeUrl = 'https://ivs.example/slow';
  KO.kickUrlT = Date.now() - 30000;   // the 25s url grace is spent
  KO.kickProbeAt = Date.now() - 2000; // last poll confirmed the channel LIVE
  KO.kickState = { state: 'Buffering', pos: 0, lat: 0 , ts: Date.now() };
  KO.kickEverPlayed = false;
  KO.reconnectCount = 0;
  KO.kickOnDvr = false;
  KO.wrap.style.display = 'block';
  api.rectTick();
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'block', '0.8.3 defect: a live-confirmed kick still loading its first frame was hidden by the 400ms loop mid-stream');
  // Invariant stands: a url the poll loop stopped re-confirming (offline
  // answer zeroes kickProbeAt; two missed polls hide) is not display-worthy.
  KO.kickProbeAt = Date.now() - 60000;
  api.rectTick();
  assert.strictEqual(KO.wrap.style.display, 'none', 'a kick not reconfirmed live within the poll loop is not displayed');
  // Back to Playing: the next probe re-shows WITHOUT a page reload.
  KO.kickState = { state: 'Playing', pos: 5, lat: 2 , ts: Date.now() };
  badge.length = 0;
  await api.probe();
  assert.strictEqual(KO.wrap.style.display, 'block', 'a kick back to Playing re-shows the wrap without a reload');
  assert.strictEqual(lastBadge().color, '#059669', 'green KICK badge after the recovery');
  assert.ok(KO.kickProbeAt > 0, 'a Playing probe re-anchors the keep-up clock');
  console.log('Y kick keep-up: live-confirmed load pinned, unreconfirmed hidden, Playing re-shows — OK');

  // ---- Z: mouse activity re-arms the control bar over the SHOWN wrap
  // (owner 0.8.3 symptom 4: "the UI never comes back once the mouse moves
  // off"). The wrap is pointer-events:none (page UI stays clickable under
  // it), so pointer events can NEVER reach the wrap's own listeners —
  // 0.8.2/0.8.3 armed the hot bar only at mount time. The arm must live at
  // document level with a hit-test on the wrap rect.
  mountApi.teardown();
  const docEvents = {};
  mountDoc.addEventListener = (t, f) => { (docEvents[t] = docEvents[t] || []).push(f); };
  mountDoc.removeEventListener = (t, f) => { if (docEvents[t]) docEvents[t] = docEvents[t].filter((x) => x !== f); };
  mountApi.mount();
  mKO.wrap.style.display = 'block';
  mKO.wrap.classList.remove('ko-hot');
  // Wrap rect in this stub world: left 10, top 20, 400x300.
  (docEvents.pointermove || []).forEach((f) => f({ clientX: 200, clientY: 150 }));
  assert.ok(mKO.wrap.classList.contains('ko-hot'), '0.8.3 defect: a mouse move over the visible overlay never re-armed the hot bar — the wrap listens for events it cannot receive');
  mKO.wrap.classList.remove('ko-hot');
  (docEvents.pointermove || []).forEach((f) => f({ clientX: 900, clientY: 500 }));
  assert.ok(!mKO.wrap.classList.contains('ko-hot'), 'a pointer move OUTSIDE the wrap rect does not re-arm (hit-test, not blanket)');
  mKO.wrap.style.display = 'none';
  mKO.wrap.classList.remove('ko-hot');
  (docEvents.pointermove || []).forEach((f) => f({ clientX: 200, clientY: 150 }));
  assert.ok(!mKO.wrap.classList.contains('ko-hot'), 'a hidden wrap does not re-arm');
  mountApi.teardownHotBar();
  (docEvents.pointermove || []).forEach((f) => f({ clientX: 200, clientY: 150 }));
  console.log('Z hover re-arm: document-level pointermove over the wrap rect re-arms the bar; outside/hidden do not — OK');

  // ---- P1a: yt HLS — arrow-right at/over the live edge sends seekToLive,
  // not an over-shoot seek that snaps back (owner pain 1: arrows don't
  // return to live). seekOverlayStep(+ARROW_SEEK_SEC) from within the window
  // seeks; once the next step would cross ct+lat it goes seekToLive.
  KO.wrap = wrap;
  KO.player = 'youtube';
  KO.kickFrame = null; KO.kickWin = null;
  fakeFrame = { ...el('iframe'), contentWindow: { postMessage: (m) => framePosts.push(m) } };
  fakeFrame.dataset.koToken = 'p1yt';
  doc.getElementById = (id) => (id === 'ko-yt' ? fakeFrame : null);
  KO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 40, dur: 60, lat: 7 }; // edge = 47
  framePosts.length = 0;
  // within window: 40+5=45 < 47 -> plain seek
  api.seekOverlayStep(5);
  let p1ytSeek = framePosts.map((m) => m && m.__koKick && m.__koKick.t === 'seek' && m.__koKick.s);
  assert.strictEqual(p1ytSeek.length, 1, 'yt: arrow-right from inside the window seeks');
  assert.strictEqual(p1ytSeek[0], 45, 'yt: seeks to ct+delta while still inside the window');
  // next step: with lat < step (lat=2, edge=42) a 5s arrow over-shoots the
  // edge (40+5=45 >= 42) -> seekToLive, not an over-shoot seek.
  KO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 40, dur: 60, lat: 2 };
  framePosts.length = 0;
  api.seekOverlayStep(5);
  const p1ytLive = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive').length;
  assert.strictEqual(p1ytLive, 1, 'yt: arrow-right at/over the live edge sends seekToLive (no over-shoot seek)');
  console.log('P1a yt edge: arrow-right to the live edge becomes seekToLive, inside window seeks — OK');

  // ---- P1b: kick — on the LIVE url an edge-ward (forward) press sends the
  // cheap seekToLive directly, never the full live-url reload; only a real
  // DVR rewind may reload the live url. Owner pain 1 (kick kick).
  KO.player = 'kick';
  KO.kickOnDvr = false;              // on the live url
  KO.activeUrl = 'https://ivs.example/live-p1b';
  KO.kickState = { state: 'Playing', pos: 100, lat: 1, dur: 200 , ts: Date.now() };
  KO.kickFrame = fakeFrame; KO.kickWin = fakeFrame.contentWindow; // kickSend target
  framePosts.length = 0;
  api.seekOverlayStep(5);
  const p1bSeekLive = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive').length;
  const p1bReload = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'load').length;
  assert.strictEqual(p1bSeekLive, 1, 'kick: forward press on the live url sends seekToLive');
  assert.strictEqual(p1bReload, 0, 'kick: forward press on the live url must NOT reload (0.8.5: it hit kickBackToLive->load)');
  // same at the edge (already live, lat tiny): still cheap seekToLive, no reload
  KO.kickState = { state: 'Playing', pos: 199, lat: 0, dur: 200 , ts: Date.now() };
  framePosts.length = 0;
  api.seekOverlayStep(5);
  const p1bEdge = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive').length;
  const p1bEdgeReload = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'load').length;
  assert.strictEqual(p1bEdge, 1, 'kick: already at the live edge, forward press still only seekToLive');
  assert.strictEqual(p1bEdgeReload, 0, 'kick: already live, forward press never reloads');
  // On the DVR url a deep rewind stays a DVR seek; the returning forward
  // press near the DVR tail reloads live (kick semantics, acceptable) — the
  // clamp is not lost.
  KO.kickOnDvr = true; KO.kickDvrUrl = 'https://dvr.example/p1b';
  KO.kickState = { state: 'Playing', pos: 10, lat: 2, dur: 200 , ts: Date.now() };
  framePosts.length = 0;
  api.seekOverlayStep(-5); // 10-5=5, far from edge -> DVR load/seeks
  const p1bDvr = framePosts.filter((m) => m && m.__koKick && (m.__koKick.t === 'load' || m.__koKick.t === 'seek')).length;
  assert.strictEqual(p1bDvr, 1, 'kick DVR: back-seek routes to the DVR path (load/seek), not seekToLive');
  console.log('P1b kick edge: live-url forward press = seekToLive only (no reload); DVR rewind preserved — OK');

  // P1b clean-room variant: self-contained (explicit kickOnDvr=false +
  // activeUrl + kickState with a fresh st.ts, NO reliance on clobbered state
  // from earlier sections) and proves the 0.8.5 reload cliff is gone even
  // with kickDur UNSET — a clean long-running stream fixture (pos+lat riding
  // the edge). On 0.8.5, kickSeekTo computed max = Math.ceil(pos+lat) = the
  // edge, so a forward press past edge−30 fell into kickBackToLive → live-url
  // 'load'; post-fix the live-url forward branch short-circuits to seekToLive.
  // Also an erase-echo remains inert here because this path never enters the
  // retry arm (no playerAt involved).
  KO.kickOnDvr = false;
  KO.activeUrl = 'https://ivs.example/live-p1b-clean';
  KO.kickState = { state: 'Playing', pos: 100, lat: 1, ts: Date.now() }; // no dur → kickDur unset (=0)
  KO.kickFrame = fakeFrame; KO.kickWin = fakeFrame.contentWindow;
  framePosts.length = 0;
  api.seekOverlayStep(5);
  const p1bCleanLive = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive').length;
  const p1bCleanReload = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'load').length;
  assert.strictEqual(p1bCleanLive, 1, 'clean-room (kickDur unset): forward live-url press sends exactly one seekToLive');
  assert.strictEqual(p1bCleanReload, 0, 'clean-room (kickDur unset): NO live-url reload (0.8.5 cliff path is dead)');

  // ---- P1c: display-agnostic keydown gate — a HIDDEN wrap (transient rect
  // miss hides it up to 20s) must still answer arrows. 0.8.5 gate bailed on
  // display:none = dead arrows. Drive onOverlayKeydown directly with the
  // wrap hidden; a kick seek/seekToLive must be issued.
  KO.player = 'kick';
  KO.kickOnDvr = false;
  KO.activeUrl = 'https://ivs.example/live-p1c';
  KO.kickState = { state: 'Playing', pos: 100, lat: 1, dur: 120 , ts: Date.now() };
  KO.kickFrame = fakeFrame; KO.kickWin = fakeFrame.contentWindow;
  KO.wrap.style.display = 'none';    // hidden, yet selected
  framePosts.length = 0;
  api.onOverlayKeydown({
    key: 'ArrowRight', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
    target: wrap, preventDefault() {}, stopImmediatePropagation() {},
  });
  const p1cLive = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive').length;
  assert.strictEqual(p1cLive, 1, 'hidden-but-selected wrap still answers ArrowRight (seekToLive), not dead');
  // judge P2: the re-arm must be layer-correct — showKickLayer sends play, so
  // the re-shown frame is NOT frozen black. p1cLive === 1 is the dedupe pin:
  // without it showKickLayer's wasHidden seekToLive + the ArrowRight step
  // would put TWO seekToLive on the queue.
  const p1cPlay = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'play').length;
  assert.strictEqual(p1cPlay, 1, 'hidden kick re-arm sends play (showKickLayer, not bare showWrap)');
  // Left arrow on a hidden yt wrap must also seek.
  KO.player = 'youtube';
  KO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 40, dur: 60, lat: 7 };
  KO.kickFrame = null; KO.kickWin = null;
  fakeFrame = { ...el('iframe'), contentWindow: { postMessage: (m) => framePosts.push(m) } };
  fakeFrame.dataset.koToken = 'p1cy';
  doc.getElementById = (id) => (id === 'ko-yt' ? fakeFrame : null);
  framePosts.length = 0;
  api.onOverlayKeydown({
    key: 'ArrowLeft', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
    target: wrap, preventDefault() {}, stopImmediatePropagation() {},
  });
  const p1cSeek = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seek' && m.__koKick.s === 35).length;
  assert.strictEqual(p1cSeek, 1, 'hidden yt wrap still seeks back (ArrowLeft -> seek 35), not dead');
  KO.wrap.style.display = 'block';
  fakeFrame = null;
  framePosts.length = 0;
  console.log('P1c hidden gate: overlay answers arrows while the wrap is hidden (display-agnostic) — OK');

  // ---- P2a: bridge relays iframe-focus overlay keys (pain 2 — "player not
  // keyboard-controllable"). The extension-page bridge (both HLS and IVS
  // branches) addEventListener('keydown') for f/arrows, preventDefaults, and
  // posts {t:'key',k} up; native YT keys like space are untouched.
  assert.ok(bridgeSrc.includes("const RELAY_KEYS = new Set(['f', 'arrowleft', 'arrowright']);"), 'bridge declares the f/arrow relay whitelist');
  assert.ok(bridgeSrc.includes('window.addEventListener(\'keydown\', (ev) => {'), 'bridge has ONE shared keydown relay (both engines)');
  assert.ok(bridgeSrc.includes('post({ t: \'key\', k });'), 'bridge forwards the key upward as {t:key,k}');
  assert.ok(bridgeSrc.includes('try { ev.preventDefault(); } catch'), 'bridge preventDefaults the relayed key');
  // It must NOT blanket-preventDefault (native space play/pause stays intact).
  assert.ok(!bridgeSrc.includes("new Set(['f', 'arrowleft', 'arrowright', ' '])"), 'relay leaves space (native play/pause) alone');
  console.log('P2a bridge relay: bridge posts {t:key} for f/arrows via one shared listener, keeps native space — OK');

  // ---- P2b: yt host listener routes a relayed {t:'key'} to the same
  // dispatch. Focus sits in the yt frame -> bridge sends {t:'key',k:'arrowleft'}
  // -> host dispatchOverlayKey seeks back (ko-yt frame post).
  KO.player = 'youtube';
  KO.wrap = wrap;
  KO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 40, dur: 60, lat: 7 };
  KO.kickFrame = null; KO.kickWin = null;
  fakeFrame = { ...el('iframe'), id: 'ko-yt', dataset: { koToken: 'p2yt' }, contentWindow: { postMessage: (m) => framePosts.push(m) } };
  doc.getElementById = (id) => (id === 'ko-yt' ? fakeFrame : null);
  framePosts.length = 0;
  msgListeners.forEach((listener) => listener({
    source: fakeFrame.contentWindow,
    origin: new URL(chromeStub.runtime.getURL('player.html')).origin,
    data: { __koKick: { t: 'key', k: 'arrowleft', _koToken: 'p2yt' } },
  }));
  const p2bSeek = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seek' && m.__koKick.s === 35).length;
  assert.strictEqual(p2bSeek, 1, 'yt host: relayed arrowleft seeks back (seek 35) — iframe-focus arrows now work');
  fakeFrame = null;
  framePosts.length = 0;

  // ---- P2c: kick host routes a relayed {t:'key'} on the live url to the
  // cheap seekToLive (arrowright pressed while focus sits in the IVS frame).
  KO.player = 'kick';
  KO.kickOnDvr = false;
  KO.activeUrl = 'https://ivs.example/live-p2c';
  KO.kickState = { state: 'Playing', pos: 100, lat: 1, dur: 200 , ts: Date.now() };
  fakeFrame = { ...el('iframe'), id: 'ko-ivs', dataset: { koToken: 'p2ck' }, contentWindow: { postMessage: (m) => framePosts.push(m) } };
  KO.kickFrame = fakeFrame; KO.kickWin = fakeFrame.contentWindow;
  doc.getElementById = () => null; // no ko-yt in this world -> kick listener owns it
  framePosts.length = 0;
  msgListeners.forEach((listener) => listener({
    source: fakeFrame.contentWindow,
    origin: new URL(chromeStub.runtime.getURL('player.html')).origin,
    data: { __koKick: { t: 'key', k: 'arrowright', _koToken: 'p2ck' } },
  }));
  const p2cLive = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seekToLive').length;
  assert.strictEqual(p2cLive, 1, 'kick host: relayed arrowright on the live url seeks to live — iframe-focus arrows now work');
  fakeFrame = null;
  framePosts.length = 0;

  // ---- P2d: the overlay wrap is keyboard-focusable (Tab) and a single
  // overlay key press dispatches EXACTLY ONE seek (pain 2 / P3a). Key
  // handling is unified in the document capture (onOverlayKeydown) + the
  // iframe relay — there is no dead bubble-phase wrap listener (0.8.6 P3a
  // removed it); the pin is a dispatch-count: one synthetic ArrowLeft through
  // the real capture handler yields one {t:'seek'} on the frame queue.
  mountApi.teardown();
  mountApi.mount();
  assert.ok(mKO.wrap, 'remount for the focus test');
  assert.strictEqual(mKO.wrap.tabIndex, 0, 'wrap is Tab-focusable (tabIndex 0), not untabbable -1');
  assert.ok(typeof mKO.wrap._ev.keydown === 'undefined', 'no bubble-phase keydown listener (P3a: unified capture dispatch)');
  // Dispatch-count: one press via the document capture handler (what a
  // page-focus ArrowLeft hits) must enqueue exactly one seek, not two.
  KO.player = 'youtube';
  KO.wrap = wrap;
  KO.ytState = { ready: true, live: true, playing: true, muted: false, ct: 40, dur: 60, lat: 7 };
  KO.kickFrame = null; KO.kickWin = null;
  fakeFrame = { ...el('iframe'), id: 'ko-yt', dataset: { koToken: 'p2dyt' }, contentWindow: { postMessage: (m) => framePosts.push(m) } };
  doc.getElementById = (id) => (id === 'ko-yt' ? fakeFrame : null);
  framePosts.length = 0;
  api.onOverlayKeydown({
    key: 'ArrowLeft', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
    target: wrap, preventDefault() {}, stopImmediatePropagation() {},
  });
  const p2dSeek = framePosts.filter((m) => m && m.__koKick && m.__koKick.t === 'seek' && m.__koKick.s === 35).length;
  assert.strictEqual(p2dSeek, 1, 'one ArrowLeft press -> EXACTLY ONE seek post (single dispatch per press)');
  assert.strictEqual(framePosts.filter((m) => m && m.__koKick && (m.__koKick.t === 'seek' || m.__koKick.t === 'seekToLive')).length, 1, 'no duplicate seek dispatch on one press');
  fakeFrame = null;
  framePosts.length = 0;
  mountApi.teardown();
  console.log('P2d wrap focus: tabIndex=0 + single dispatch per press (no dead bubble listener) — OK');

  console.log('\nALL CONTENT.JS FIX CHECKS PASSED');
  process.exit(0);
})().catch((e) => { console.error('HARNESS FAIL:', e); process.exit(1); });
