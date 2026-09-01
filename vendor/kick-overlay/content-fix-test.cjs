// Behavioral harness for the kick-overlay content.js fixes (injectStyles,
// ensureYtHls TTL/backoff, yt fallback GATE + non-persisting handover, URL
// reload, #ko-status chip + <=4-char badge tokens, sticky tw_delete via
// MutationObserver, yt frame-death watchdog). Runs the real content.js source
// with a stubbed DOM/chrome environment and asserts on observable behavior.
// `node content-fix-test.cjs` (no frameworks).
'use strict';
const fs = require('fs');
const assert = require('assert');

const swCalls = []; // chrome.runtime.sendMessage recorder
const badgeTexts = []; // chrome.action.setBadgeText recorder (clipping check)
const runtimeMsgListeners = []; // chrome.runtime.onMessage recorders
const moInstances = []; // MutationObserver instances (tw-delete re-apply)
const storage = { ko: undefined };
let storageChangedListener = null;
const framePosts = []; // postMessage sent to the fake yt frame
const appends = []; // elements appended to document.head
const fakeVideos = []; // page-world <video> population (twitch player)
let fakeFrame = null;

const el = (tag) => ({
  tag,
  tagName: (tag || 'div').toUpperCase(),
  nodeType: 1,
  id: '',
  textContent: '',
  innerHTML: '',
  style: {},
  dataset: {},
  className: '',
  isConnected: true,
  removed: 0,
  contentWindow: null,
  classList: { add() {}, remove() {}, contains: () => false },
  appendChild() {},
  remove() { this.removed = (this.removed || 0) + 1; this.isConnected = false; },
  addEventListener() {},
  setAttribute() {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
  getClientRects: () => [],
  querySelector: () => el('div'), // mount() wires buttons from inner queries
  querySelectorAll: () => [],
});

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
  remove() {
    this.removed = (this.removed || 0) + 1;
    this.isConnected = false;
    const i = fakeVideos.indexOf(this);
    if (i >= 0) fakeVideos.splice(i, 1);
  },
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
      get: (k, cb) => { void k; setTimeout(() => cb && cb({}), 0); },
      set: (o, cb) => { storage.ko = o['ko.v2']; cb && cb(); },
    },
    onChanged: { addListener: (l) => { storageChangedListener = l; } },
  },
  action: {
    setBadgeText(o) { badgeTexts.push(o && o.text); },
    setBadgeBackgroundColor() {},
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
// kick.com is 403-gated from this IP; the content script catches fetch
// failures internally, so a rejecting stub keeps the boot probe deterministic.
global.fetch = () => Promise.reject(new Error('stub-fetch'));

const src = fs.readFileSync('content.js', 'utf8');
const bridgeSrc = fs.readFileSync('player-bridge.js', 'utf8');
// Run in a Function scope so top-level consts/functions are accessible.
const scope = {};
const fn = new Function('window', 'document', 'location', 'navigator', src + '\n;return {KO, probe, apply, ensureYtHls, injectStyles, teardown, setPlayer, saveState, rectTick, mount, updateKickBar};');
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
  assert.ok(styleEl.textContent.includes('#ko-wrap.ko-yt #ko-top{display:none;}'), 'stale LIVE pill hidden in yt mode');
  assert.ok(styleEl.textContent.includes('#ko-bar{position:absolute;left:0;right:0;bottom:0;pointer-events:auto;opacity:1;'), 'control bar defaults visible');
  assert.ok(styleEl.textContent.includes('#ko-wrap:not(.ko-hot) #ko-bar{opacity:1;}'), 'bar stays visible after inactivity');
  assert.ok(styleEl.textContent.includes('#ko-wrap.ko-offline #ko-top{display:none;}'), 'offline LIVE pill hidden');
  assert.ok(styleEl.textContent.includes('#ko-quality-menu{display:none;'), 'quality menu CSS present');
  assert.ok(styleEl.textContent.includes('#ko-top{display:none;'), 'persistent top-left LIVE label disabled');
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
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 60 }),
    isConnected: true,
    offsetWidth: 100, offsetHeight: 60,
  };
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

  await api.probe();
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
  await api.probe();
  assert.strictEqual(KO.player, 'youtube', 'same-handle kickSlug must NOT hand over to kick');
  assert.ok(!framePosts.some((m) => m.__koKick && m.__koKick.t === 'load'), 'no load sent after fatal error');
  assert.strictEqual(wrap.style.display, 'none', 'failed yt layer hides the wrap (chip carries the state)');
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
  KO.statusChip = { remove() {} };
  KO.kickFrame = null; KO.wrap = null;
  api.teardown();
  assert.strictEqual(KO.ytHlsUrl, null);
  assert.strictEqual(KO.ytHlsAt, 0);
  assert.strictEqual(KO.ytHlsFailed, false);
  assert.strictEqual(KO.ytHlsFailedAt, 0);
  assert.strictEqual(KO.ytLoadedUrl, null);
  assert.strictEqual(KO.lastYtSt, 0, 'teardown resets the yt silence clock');
  assert.strictEqual(KO.statusChip, null, 'teardown removes the status chip');
  console.log('teardown: yt mint bookkeeping + chip + yt watchdog clock reset — OK');

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
  // The reported failure case: wrap hidden, but the chip still reports state.
  assert.ok(KO.statusChip, 'chip created after the enabled check');
  assert.strictEqual(KO.statusChip.textContent, 'YT ✕', 'chip shows the failed yt state');
  assert.strictEqual(KO.statusChip.style.display, 'block', 'chip visible with the wrap hidden');
  assert.strictEqual(KO.statusChip.style.color, '#f87171', 'chip red for the failed yt state');
  // 30s backoff expires -> the probe loop re-enters the youtube branch and re-mints.
  KO.ytHlsFailedAt = Date.now() - 31000;
  await api.probe();
  assert.strictEqual(KO.player, 'youtube', 're-mint keeps youtube mode');
  assert.strictEqual(KO.ytHlsUrl, 'https://hls.example/u1', 'backoff-expired re-mint succeeded');
  assert.strictEqual(KO.ytHlsFailed, false, 'successful re-mint clears the failed flag');
  assert.strictEqual(KO.statusChip.textContent, 'YT…', 'chip flips to the connecting state');
  assert.strictEqual(KO.statusChip.style.color, '#fbbf24', 'chip amber while minting');
  console.log('H fallback gate: same-handle stays youtube, no persist, backoff re-mint, chip YT ✕/YT… — OK');

  // ---- I: #ko-status chip state transitions (driven from setBadge) ----------
  KO.mappings['rodil'] = { kick: 'realkick', yt: '@x' };
  KO.ytHlsFailed = false;
  KO.ytHlsFailedAt = 0;
  // KICK Playing -> green KICK
  KO.player = 'kick';
  KO.kickSlug = 'realkick';
  KO.activeUrl = 'https://ivs.example/live';
  KO.kickUrlT = Date.now();
  KO.kickEverPlayed = true;
  KO.kickState = { state: 'Playing', paused: false, muted: false, pos: 10, lat: 2, dur: 120 };
  await api.probe();
  assert.strictEqual(KO.statusChip.textContent, 'KICK', 'chip KICK while kick Playing');
  assert.strictEqual(KO.statusChip.style.color, '#53fc18', 'chip green while kick Playing');
  assert.strictEqual(KO.statusChip.style.display, 'block', 'chip visible');
  // KICK connecting (url/ready pending) -> amber KICK…
  KO.kickState = { state: 'Idle', paused: true, muted: true, pos: 0, lat: 0, dur: 0 };
  KO.kickUrlT = Date.now(); // fresh url inside the transition grace
  await api.probe();
  assert.strictEqual(KO.statusChip.textContent, 'KICK…', 'chip KICK… while kick connecting');
  assert.strictEqual(KO.statusChip.style.color, '#fbbf24', 'chip amber while kick connecting');
  // KICK failed/offline -> gray KICK ✕
  KO.activeUrl = null;
  assert.ok(kickSrc.includes('ev.origin !== location.origin'), 'kick messages must be page-origin restricted');
  KO.kickUrlT = 0;
  KO.kickState = null;
  KO.kickEverPlayed = false;
  await api.probe();
  assert.strictEqual(KO.statusChip.textContent, 'KICK ✕', 'chip KICK ✕ when kick offline');
  assert.strictEqual(KO.statusChip.style.color, '#f87171', 'chip red when kick offline');
  assert.strictEqual(KO.wrap.style.display, 'none', 'wrap hidden when kick offline');
  // YT live -> red YT
  KO.player = 'youtube';
  KO.ytRaw = '@x';
  KO.ytId = 'UCxxxxxxxxxxxxxxxxxxxxxx';
  KO.ytHlsUrl = 'https://hls.example/u1';
  KO.ytHlsAt = Date.now();
  KO.ytHlsFailed = false;
  KO.ytState = { ready: true, playing: true, muted: false, live: true, dur: 0, ct: 0, error: 0 };
  KO.ytLoadedUrl = 'https://hls.example/u1';
  await api.probe();
  assert.strictEqual(KO.statusChip.textContent, 'YT', 'chip YT while yt live');
  assert.strictEqual(KO.statusChip.style.color, '#ff0000', 'chip red while yt live');
  // YT minting/loading (not live yet) -> amber YT…
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytEmbedAt = Date.now(); // embed grace not expired
  await api.probe();
  assert.strictEqual(KO.statusChip.textContent, 'YT…', 'chip YT… while minting/loading');
  assert.strictEqual(KO.statusChip.style.color, '#fbbf24', 'chip amber while minting/loading');
  // twitch mode -> chip hidden
  KO.player = 'twitch';
  await api.probe();
  assert.strictEqual(KO.statusChip.style.display, 'none', 'chip hidden in twitch mode');
  KO.player = 'youtube';
  console.log('I status chip: KICK/KICK…/KICK ✕ + YT/YT…/YT ✕ transitions, hidden in twitch mode — OK');

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
  await new Promise((res) => delListener({ type: 'ko-delete-twitch' }, {}, () => res()));
  await settle(50); // let the handler's apply() settle
  assert.strictEqual(KO.twDeleted, true);
  assert.strictEqual(delVideo.removed, 1, 'delete removes the current twitch video');
  assert.ok(!KO.lastRect || KO.lastRect.width !== 1280 || KO.lastRect.height !== 720, 'tw-delete must not prime full viewport lastRect');
  assert.strictEqual(KO.wrap.style.display, 'block', 'wrap kept visible in the loading state');
  assert.ok(!KO.wrap.style.width || KO.wrap.style.width !== '1280px', 'wrap not forced to viewport width without a measured rect');
  assert.strictEqual(KO.statusChip.textContent, 'YT ✕', 'chip reports the yt failure with the wrap up');
  assert.strictEqual(KO.statusChip.style.display, 'block', 'chip visible with the wrap up');

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
      parentElement: null,
      className: '',
      classList: {
        _s: new Set(),
        add(...a) { a.forEach((x) => this._s.add(x)); },
        remove(...a) { a.forEach((x) => this._s.delete(x)); },
        contains(x) { return this._s.has(x); },
      },
      appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
      remove() { this.removed = (this.removed || 0) + 1; },
      _ev: {},
      addEventListener(type, fn) { this._ev[type] = fn; },
      removeEventListener(type, fn) { if (this._ev[type] === fn) delete this._ev[type]; },
      setAttribute() {},
      getAttribute() { return null; },
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
  const mountApi = new Function('window', 'document', 'location', 'navigator', src + '\n;return {KO, mount, updateKickBar, teardown, injectStyles};')(
    global, mountDoc, global.location, global.navigator,
  );
  const mKO = mountApi.KO;
  mountApi.injectStyles();
  mountApi.mount();
  assert.ok(mKO.wrap, 'mount creates wrap');
  const fsNode = mKO.wrap.querySelector('#ko-fs');
  assert.ok(fsNode && fsNode.innerHTML.includes('viewBox'), 'fullscreen button receives the fs SVG icon');
  assert.ok(mKO.wrap.classList.contains('ko-hot'), 'wrap starts hot so the control bar is visible');
  console.log('M fs icon: mount assigns KO_SVG.fs to #ko-fs — OK');

  const styleEl2 = appends.find((e) => e.id === 'ko-style');
  assert.ok(styleEl2.textContent.includes('opacity:1'), 'bar visible-by-default CSS present');
  console.log('N bar visibility: controls stay visible after inactivity — OK');

  const badgeWrap = {
    style: { display: 'block' },
    classList: {
      _s: new Set(['ko-kick']),
      add(...a) { a.forEach((x) => this._s.add(x)); },
      remove(...a) { a.forEach((x) => this._s.delete(x)); },
      contains(x) { return this._s.has(x); },
      toggle(cls, on) { on ? this.add(cls) : this.remove(cls); },
    },
    querySelector: (sel) => {
      if (sel === '#ko-live-dot') return { style: {} };
      if (sel === '#ko-live-txt') return { textContent: '' };
      if (sel === '#ko-livebadge') return { style: { cursor: '' } };
      if (sel === '#ko-elapsed') return { textContent: '' };
      if (sel === '#ko-play') return { dataset: {}, innerHTML: '' };
      return null;
    },
  };
  KO.wrap = badgeWrap;
  KO.player = 'kick';
  KO.kickOnDvr = false;
  KO.kickState = null;
  api.updateKickBar();
  assert.ok(badgeWrap.classList.contains('ko-offline'), 'offline kickState hides LIVE badge via ko-offline');
  KO.kickState = { state: 'Playing', pos: 1, lat: 1, dur: 10 };
  api.updateKickBar();
  assert.ok(!badgeWrap.classList.contains('ko-offline'), 'playing kickState shows LIVE badge');
  KO.kickOnDvr = true;
  KO.kickState = { state: 'Paused', pos: 1, lat: 1, dur: 10 };
  api.updateKickBar();
  assert.ok(!badgeWrap.classList.contains('ko-offline'), 'DVR mode keeps badge visible');
  console.log('O badge offline: ko-offline toggles with kick playback state — OK');

  KO.lastRect = null;
  const delListener2 = runtimeMsgListeners[0];
  await new Promise((res) => delListener2({ type: 'ko-delete-twitch' }, {}, () => res()));
  assert.ok(!KO.lastRect || KO.lastRect.width !== global.innerWidth || KO.lastRect.height !== global.innerHeight, 'tw-delete without video does not prime viewport lastRect');
  console.log('P tw-delete: no viewport lastRect priming — OK');

  mountApi.teardown();

  console.log('\nALL CONTENT.JS FIX CHECKS PASSED');
  process.exit(0);
})().catch((e) => { console.error('HARNESS FAIL:', e); process.exit(1); });
