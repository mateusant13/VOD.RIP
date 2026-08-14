// Kick Overlay — content script (runs on https://www.twitch.tv/*).
//
// While enabled and the streamer is live on Kick or YouTube too, overlays
// the streamer's REAL Kick or YouTube stream over the Twitch player so
// Twitch ads become invisible and inaudible (Twitch keeps playing
// underneath, muted + PAUSED — never rendering while covered).
//
// Sources: Kick = the SAME engine kick.com uses (Amazon IVS web player,
// amazon-ivs-player) playing the same playback_url the VOD.RIP downloader
// uses (full HD, all IVS renditions). The IVS player runs in an extension
// page iframe (player.html) so the wasm worker is same-origin and the
// playback_url fetch rides on host_permissions. YouTube = the official
// live_stream embed (youtube.com/embed/live_stream?channel=<UC...>, real
// YT player, native controls incl. seek-back + LIVE chip — the "back to
// live" option). The channel handle is resolved to its UC... id by the
// service worker.
//
// ONE player rendering at all times (user mandate 2026-08-13): the hidden
// players are PAUSED (not just covered) — kick frame, yt iframe, and the
// native Twitch player all pause while another layer is shown; switching
// resumes instantly (buffers are kept; kick/yt seek back to the live edge
// on return). The Kick player also offers seek-back within the live window
// + a LIVE button.
//
// Manual player switches NEVER wait on the Twitch player state: clicking
// KICK/YOUTUBE/TWITCH always takes effect immediately (the Twitch-live
// gate was removed 2026-08-13 — it was destroying the kick player on
// Twitch ad transitions and making the switch appear broken).
'use strict';

const KEY = 'ko.v2';
const POLL_MS = 20000;   // live-status re-check while enabled
const RECT_MS = 400;     // geometry + pause/mute enforcement tick
const SPA_MS = 900;      // Twitch SPA pathname poll (no reload on channel nav)
const HIDE_TICKS = 3;    // consecutive ticks without a Twitch player before hiding
const MAX_RECONNECT = 3; // kick fatal retries (fresh playback_url each time)

// Diagnostics: the [ko] console lines are ALSO mirrored to a local listener
// (127.0.0.1:9234) so the extension's real-browser state can be read without
// F12. The content script forwards through the SW, which beacons with a
// no-cors fetch (neither CORS- nor CSP-blocked, no host_permission needed).
// ponytail: debug-only channel; remove once YT validation is done.
function diag(ev, data) {
  try {
    chrome.runtime.sendMessage({ __koDiag: { ev, data: data || {} } }, () => void chrome.runtime.lastError);
  } catch {
    /* beacon must never break the overlay */
  }
}

// Twitch routes that look like /<slug> but are not channels.
const NOT_CHANNEL = new Set([
  'directory', 'downloads', 'friends', 'gift', 'jobs', 'login', 'notifications',
  'p', 'prime', 'settings', 'subscriptions', 'turbo', 'events', 'search',
  'wallet', 'moderation', 'popout', 'videos', 'clips', 'team', 'tags',
]);

const KO = {
  enabled: false,
  player: 'kick', // 'kick' | 'youtube' | 'twitch' — switching never rebuilds players
  mappings: {},   // twitchSlug -> kickSlug (string) | {kick, yt, ytId}
  slug: null,
  kickSlug: null,
  ytRaw: '',
  ytId: null,
  activeUrl: null, // kick playback_url currently loaded in the frame
  wrap: null,      // overlay container — persists across switches
  kickFrame: null, // <iframe src=player.html> (IVS engine, same as kick.com)
  kickWin: null,   // kickFrame.contentWindow (set on first ready message)
  kickReady: false,
  kickState: null, // last {state, paused, muted, volume, pos, lat, q, qcount} from the frame
  lastKickSt: 0,   // epoch ms of the last st message (frame-death watchdog)
  pendingUrl: null,// playback_url queued until the frame reports ready
  kickVol: 1,
  kickMuted: false, // user's kick mute choice (persisted; hidden-mute is separate)
  ytVol: 100,       // user's youtube volume (persisted, applied on show)
  ytState: { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 },
  ytUnlock: false,
  bridgeInjected: false,
  twLive: false,       // sticky: Twitch channel confirmed live on this page
  twWasPlaying: false, // Twitch was playing when we paused it → resume on switch back
  seeking: false,
  reconnectCount: 0,
  pollTimer: null,
  rectTimer: null,
  spaTimer: null,
  muted: new Set(), // twitch videos we muted (restored)
  hideTicks: 0,
  stallTicks: 0,
  lastTickT: 0,
  lastPath: location.pathname,
};

// ---- storage ----------------------------------------------------------------

function applyVols(vols) {
  vols = vols || {};
  const kv = vols.kick || {};
  const yv = vols.yt || {};
  if (typeof kv.v === 'number') KO.kickVol = kv.v;
  if (typeof kv.m === 'boolean') KO.kickMuted = kv.m;
  if (typeof yv.v === 'number') KO.ytVol = yv.v;
}

function loadState() {
  return new Promise((res) => {
    chrome.storage.local.get(KEY, (o) => {
      const s = (o && o[KEY]) || {};
      KO.enabled = s.enabled === undefined ? true : !!s.enabled;
      KO.player = s.player === 'twitch' ? 'twitch' : s.player === 'youtube' ? 'youtube' : 'kick';
      KO.mappings = s.mappings && typeof s.mappings === 'object' ? s.mappings : {};
      applyVols(s.vols);
      res();
    });
  });
}

function saveState() {
  return new Promise((res) => {
    chrome.storage.local.set(
      {
        [KEY]: {
          enabled: KO.enabled,
          mappings: KO.mappings,
          player: KO.player,
          vols: {
            kick: { v: KO.kickVol, m: KO.kickMuted },
            yt: { v: KO.ytVol },
          },
        },
      },
      res,
    );
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes[KEY]) return;
  const s = changes[KEY].newValue || {};
  const prev = changes[KEY].oldValue || {};
  const pChanged =
    !!s.enabled !== !!prev.enabled ||
    (s.player || 'kick') !== (prev.player || 'kick') ||
    JSON.stringify(s.mappings || {}) !== JSON.stringify(prev.mappings || {});
  KO.enabled = !!s.enabled;
  KO.player = s.player === 'twitch' ? 'twitch' : s.player === 'youtube' ? 'youtube' : 'kick';
  KO.mappings = s.mappings && typeof s.mappings === 'object' ? s.mappings : {};
  applyVols(s.vols);
  if (pChanged) apply(); // hot toggle / remap / player switch — no page reload
});

// ---- helpers ----------------------------------------------------------------

function currentSlug() {
  const seg = location.pathname.split('/').filter(Boolean);
  if (!seg.length) return null;
  const s = decodeURIComponent(seg[0]).toLowerCase();
  if (!/^[a-z0-9_]{2,25}$/.test(s) || NOT_CHANNEL.has(s)) return null;
  return s;
}

// Largest visible page <video> that is not ours — the active Twitch player.
function twitchVideo() {
  const vids = [...document.querySelectorAll('video')].filter((v) => v.getClientRects().length);
  let best = null;
  let bestArea = 0;
  for (const v of vids) {
    const r = v.getBoundingClientRect();
    const a = r.width * r.height;
    if (a > bestArea) {
      bestArea = a;
      best = v;
    }
  }
  return best;
}

function twitchIsLive(v) {
  return !!(v && !v.paused && v.readyState >= 2 && v.currentTime > 0);
}

// Sticky "Twitch is live" — survives OUR pause (overlay shown pauses the
// native player; a paused live must not be treated as offline). Used for
// badges/status only — it no longer gates any player switch.
function updateTwLiveSticky(v) {
  if (twitchIsLive(v)) {
    KO.twLive = true;
  } else if (!v || v.readyState < 1) {
    if (!v) KO.twLive = false;
  }
  // else: video exists but paused by us → keep sticky value
}

// Kick stream source — THE full-HD playback_url (same endpoint the VOD.RIP
// downloader uses; all IVS renditions incl. 1080p+; v2 exposes it TOP-LEVEL).
async function kickPlaybackUrl(slug) {
  const enc = encodeURIComponent(slug);
  const probes = [
    `https://kick.com/api/v2/channels/${enc}`,
    `https://kick.com/api/v1/channels/${enc}`,
  ];
  for (const url of probes) {
    try {
      const r = await fetch(url, { credentials: 'omit' });
      if (!r.ok) continue;
      const d = await r.json();
      const ls = d && d.livestream;
      if (d && d.playback_url) return { live: true, url: d.playback_url };
      if (ls && ls.playback_url) return { live: true, url: ls.playback_url };
      if (!d || (!d.playback_url && !ls)) return { live: false }; // explicitly offline
    } catch {
      // transient — try next endpoint / next poll tick
    }
  }
  return { live: false };
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeBackgroundColor({ color: color || [0, 0, 0, 0] });
    chrome.action.setBadgeText({ text: text || '' });
  } catch {
    /* badge is cosmetic */
  }
}

// ---- YouTube bridge ---------------------------------------------------------

function injectBridge() {
  if (KO.bridgeInjected) return;
  KO.bridgeInjected = true;
  const s = document.createElement('script');
  s.src = chrome.runtime.getURL('yt-bridge.js');
  (document.head || document.documentElement).appendChild(s);
}

function ytCmd(cmd, arg) {
  try {
    window.postMessage({ __koYtCmd: cmd, __koYtArg: arg }, '*');
  } catch {
    /* ignore */
  }
}

function resolveYtChannel(raw) {
  return new Promise((res) => {
    try {
      chrome.runtime.sendMessage({ type: 'ko-resolve-yt', value: raw }, (r) => {
        res((r && r.id) || null);
      });
    } catch {
      res(null);
    }
  });
}

async function ensureYtId() {
  if (KO.ytId) return true;
  if (!KO.ytRaw) return false;
  KO.ytId = await resolveYtChannel(KO.ytRaw);
  if (KO.ytId) {
    const m = KO.mappings[KO.slug];
    if (m && typeof m === 'object') {
      m.ytId = KO.ytId;
      saveState();
    }
  }
  return !!KO.ytId;
}

function ensureYtIframe() {
  injectBridge();
  if (document.getElementById('ko-yt')) return;
  const iframe = document.createElement('iframe');
  iframe.id = 'ko-yt';
  iframe.src =
    'https://www.youtube.com/embed/live_stream?channel=' +
    encodeURIComponent(KO.ytId) +
    '&autoplay=1&mute=1&controls=1&playsinline=1&enablejsapi=1&origin=' +
    encodeURIComponent(location.origin);
  iframe.setAttribute('allow', 'autoplay; fullscreen; encrypted-media');
  iframe.setAttribute('allowfullscreen', '');
  KO.wrap.appendChild(iframe);
}

function enableYtUnlock() {
  if (KO.ytUnlock) return;
  KO.ytUnlock = true;
  const unlock = () => {
    KO.ytUnlock = false;
    document.removeEventListener('pointerdown', unlock, true);
    if (KO.player !== 'youtube' || !KO.wrap || KO.wrap.style.display === 'none') return;
    ytCmd('unmute');
    ytCmd('play');
  };
  document.addEventListener('pointerdown', unlock, true);
}

let lastYtProbe = 0;
window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (!d || !d.__koYt) return;
  if (d.t === 'ready') {
    KO.ytState.ready = true;
    ytCmd('setVolume', KO.ytVol); // restore the user's last yt volume on a fresh embed
    throttledYtProbe();
  } else if (d.t === 'status') {
    const prevLive = KO.ytState.live;
    KO.ytState.playing = !!d.playing;
    KO.ytState.muted = !!d.muted;
    KO.ytState.live = !!d.live;
    KO.ytState.dur = d.dur || 0;
    KO.ytState.ct = d.ct || 0;
    KO.ytState.vq = d.vq || '';
    if (!d.muted && typeof d.volume === 'number' && d.volume >= 0 && KO.ytVol !== d.volume) {
      KO.ytVol = d.volume; // remember the user's yt volume slider
      saveState();
    }
    if (KO.player === 'youtube') throttledYtProbe();
    if (prevLive && !KO.ytState.live && KO.player === 'youtube') throttledYtProbe();
    if (KO.player === 'youtube' && KO.ytState.live && KO.ytState.muted) enableYtUnlock();
  } else if (d.t === 'error') {
    KO.ytState.error = d.c || 0;
    if (KO.player === 'youtube') throttledYtProbe();
  }
});

function throttledYtProbe() {
  const now = Date.now();
  if (now - lastYtProbe < 1000) return;
  lastYtProbe = now;
  probe();
}

// ---- Kick frame bridge (IVS — the engine kick.com uses) ---------------------

function kickSend(o) {
  if (!KO.kickWin) return;
  try {
    KO.kickWin.postMessage({ __koKick: o }, '*');
  } catch {
    /* frame gone */
  }
}

function kickFrame() {
  if (KO.kickFrame && KO.kickFrame.isConnected) return KO.kickFrame;
  if (!KO.wrap) mount();
  const fr = document.createElement('iframe');
  fr.id = 'ko-ivs';
  fr.src = chrome.runtime.getURL('player.html');
  fr.setAttribute('allow', 'autoplay; fullscreen');
  fr.setAttribute('allowfullscreen', '');
  KO.wrap.appendChild(fr);
  KO.kickFrame = fr;
  KO.kickWin = null;
  KO.kickReady = false;
  KO.kickState = null;
  fr.addEventListener('load', () => {
    KO.kickWin = fr.contentWindow;
  });
  return fr;
}

window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (!d || !d.__koKick) return;
  if (KO.kickWin && ev.source !== KO.kickWin) return;
  const m = d.__koKick;
  if (m.t === 'ready') {
    KO.kickWin = ev.source;
    KO.kickReady = true;
    KO.lastKickSt = Date.now();
    if (KO.pendingUrl) {
      kickSend({ t: 'load', url: KO.pendingUrl });
      KO.pendingUrl = null;
    }
  } else if (m.t === 'st') {
    KO.kickState = m.st;
    KO.lastKickSt = Date.now();
    if (KO.player === 'kick') updateKickBar();
  } else if (m.t === 'ev') {
    if (m.e === 'error') {
      console.error('[ko] kick (IVS) error', m.d || '');
      diag('ivs_error', { msg: String(m.d || '').slice(0, 140), code: m.code || 0 });
      if (KO.enabled && KO.player === 'kick' && KO.activeUrl) reconnect();
    } else if (m.e === 'rebuffering') {
      diag('ivs_rebuffer', {});
    }
  }
});

// Attach the current playback_url to the IVS frame. Same-url loads are
// no-ops (switching players never re-attaches). The frame is created once
// and the player is re-created only when the url actually changes.
function ensureKick(url) {
  if (!KO.wrap) mount();
  if (KO.activeUrl === url && KO.kickFrame && KO.kickReady) {
    if (KO.kickState && KO.kickState.state !== 'Playing') kickSend({ t: 'play' });
    return;
  }
  KO.activeUrl = url;
  KO.reconnectCount = 0;
  kickFrame();
  KO.pendingUrl = url;
  if (KO.kickReady) {
    kickSend({ t: 'load', url });
    KO.pendingUrl = null;
  }
}

// ---- mute/pause model -------------------------------------------------------
// One player renders at a time: with an overlay shown, every Twitch video is
// muted AND paused (no decode, no composite); the hidden overlay players
// (kick frame + yt iframe) are paused too. In twitch mode the overlay
// players are paused and Twitch resumes (only if WE paused it — a user-set
// pause survives).

function syncMute() {
  const overlayShown = KO.player !== 'twitch' && !!KO.wrap && KO.wrap.style.display !== 'none';
  for (const v of document.querySelectorAll('video')) {
    if (overlayShown && !v.muted) {
      v.muted = true;
      KO.muted.add(v);
    } else if (!overlayShown && KO.muted.has(v) && v.muted) {
      v.muted = false;
      KO.muted.delete(v);
    }
  }
}

function unmuteAll() {
  for (const v of KO.muted) {
    try {
      v.muted = false;
    } catch {
      /* detached */
    }
  }
  KO.muted.clear();
}

function pauseTwitchForOverlay() {
  const v = twitchVideo();
  if (!v) return;
  if (!v.paused) {
    KO.twWasPlaying = true;
    v.pause();
  }
}

function resumeTwitchIfOurs() {
  const v = twitchVideo();
  if (v && KO.twWasPlaying && v.paused) {
    v.play().catch(() => {});
  }
  KO.twWasPlaying = false;
}

// The overlay must sit ABOVE the Twitch player but BELOW page UI that
// overlaps the player (user profile cards, badge hovercards, menus).
// Measured live (2026-08-13): the player lives in main.twilight-main
// (z-index 1, stacking context) → persistent-player (z 1) → video; the
// viewer card renders INSIDE main (right-column z 1 → chat wrapper z 10 →
// card z 10). A body-level overlay with max z-index covers the whole main
// context — card included — which is why profile clicks vanished under
// the overlay. Fix: append the wrap AND the floating buttons INTO main and
// use z-index 5 / 6 — above the player (1), below the chat/card wrapper
// (10). ponytail: if Twitch reworks these z-indexes, re-measure the
// wrapper values; the anchor selector is layout-agnostic ('main').
function overlayAnchor() {
  return document.querySelector('main.twilight-main, main') || document.body;
}

function ensureAttached(el) {
  if (el && !el.isConnected) overlayAnchor().appendChild(el);
}

// ---- overlay lifecycle ------------------------------------------------------

function mount() {
  if (KO.wrap) return;
  const wrap = document.createElement('div');
  wrap.id = 'ko-wrap';
  wrap.style.display = 'none';
  wrap.classList.add('ko-kick');
  const bar = document.createElement('div');
  bar.id = 'ko-bar';
  bar.innerHTML =
    '<span id="ko-badge">KICK</span>' +
    '<button id="ko-play" title="Play/Pause">\u275A\u275A</button>' +
    '<button id="ko-mute" title="Mute">Mute</button>' +
    '<input id="ko-vol" type="range" min="0" max="100" value="100" title="Volume" />' +
    '<input id="ko-seek" type="range" min="0" max="1" value="0" title="Seek (live window)" />' +
    '<button id="ko-live" title="Back to the live edge" style="display:none">LIVE</button>' +
    '<span id="ko-time">LIVE</span>' +
    '<span style="flex:1"></span>' +
    '<button id="ko-fs" title="Fullscreen">\u26F6</button>';
  wrap.appendChild(bar);
  const rc = document.createElement('div');
  rc.id = 'ko-reconnecting';
  rc.textContent = 'RECONNECTING\u2026';
  rc.style.display = 'none';
  wrap.appendChild(rc);
  overlayAnchor().appendChild(wrap);
  KO.wrap = wrap;
  KO.hideTicks = 0;

  let hotTimer = null;
  const updateBar = () => {
    const playing = KO.kickState && KO.kickState.state === 'Playing';
    bar.querySelector('#ko-play').textContent = playing ? '\u275A\u275A' : '\u25B6';
    const silent = KO.kickMuted || KO.kickVol === 0;
    bar.querySelector('#ko-mute').textContent = silent ? 'Unmute' : 'Mute';
    bar.querySelector('#ko-vol').value = String(Math.round((silent ? 0 : KO.kickVol) * 100));
  };
  window.addEventListener('mousemove', (e) => {
    if (!KO.wrap) return;
    const r = KO.wrap.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) return;
    KO.wrap.classList.add('ko-hot');
    clearTimeout(hotTimer);
    hotTimer = setTimeout(() => {
      if (KO.wrap) KO.wrap.classList.remove('ko-hot');
    }, 2600);
  });
  bar.querySelector('#ko-play').addEventListener('click', () => {
    const playing = KO.kickState && KO.kickState.state === 'Playing';
    kickSend(playing ? { t: 'pause' } : { t: 'play' });
  });
  bar.querySelector('#ko-mute').addEventListener('click', () => {
    KO.kickMuted = !KO.kickMuted;
    if (!KO.kickMuted && KO.kickVol === 0) KO.kickVol = 1;
    kickSend({ t: 'mute', m: KO.kickMuted });
    if (!KO.kickMuted) kickSend({ t: 'volume', v: KO.kickVol });
    updateBar();
    saveState(); // remember the user's kick mute choice
  });
  const vol = bar.querySelector('#ko-vol');
  vol.addEventListener('input', () => {
    KO.kickVol = Number(vol.value) / 100;
    KO.kickMuted = vol.value === '0';
    kickSend({ t: 'volume', v: KO.kickVol });
    kickSend({ t: 'mute', m: KO.kickMuted });
    updateBar();
  });
  vol.addEventListener('change', () => {
    saveState(); // remember the user's kick volume slider
  });
  const seek = bar.querySelector('#ko-seek');
  seek.addEventListener('input', () => {
    KO.seeking = true;
    kickSend({ t: 'seek', s: Number(seek.value) });
  });
  seek.addEventListener('change', () => {
    KO.seeking = false;
  });
  const live = bar.querySelector('#ko-live');
  live.addEventListener('click', () => kickSend({ t: 'seekToLive' }));
  bar.querySelector('#ko-fs').addEventListener('click', () => {
    if (document.fullscreenElement === KO.wrap) document.exitFullscreen().catch(() => {});
    else if (KO.wrap) KO.wrap.requestFullscreen().catch(() => {});
  });
  startRectLoop();
}

function teardown() {
  stopRectLoop();
  if (KO.kickFrame) {
    try {
      KO.kickFrame.remove();
    } catch {
      /* already gone */
    }
    KO.kickFrame = null;
  }
  if (KO.wrap) {
    KO.wrap.remove();
    KO.wrap = null;
  }
  ytCmd('destroy');
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytUnlock = false;
  KO.kickWin = null;
  KO.kickReady = false;
  KO.kickState = null;
  KO.pendingUrl = null;
  KO.activeUrl = null;
  KO.reconnectCount = 0;
  KO.hideTicks = 0;
  KO.stallTicks = 0;
  KO.lastTickT = 0;
  KO.lastKickSt = 0;
  KO.twLive = false;
  KO.twWasPlaying = false;
  unmuteAll();
  for (const b of ['ko-twitchbtn', 'ko-kickbtn', 'ko-ytbtn']) {
    const el = document.getElementById(b);
    if (el) el.style.display = 'none';
  }
}

function showWrap() {
  if (!KO.wrap) return;
  KO.wrap.style.display = 'block';
  KO.hideTicks = 0;
  syncMute();
}

function hideWrap() {
  if (!KO.wrap) return;
  KO.wrap.style.display = 'none';
  if (KO.kickFrame && KO.kickState && KO.kickState.state === 'Playing') {
    kickSend({ t: 'pause' }); // one rendering: hidden kick player PAUSED
    kickSend({ t: 'mute', m: true });
  }
  if (KO.ytState.ready && KO.ytState.playing) ytCmd('pause');
  ytCmd('mute');
  syncMute();
}

function showKickLayer() {
  if (!KO.wrap) mount();
  const wasHidden = KO.wrap.style.display === 'none';
  KO.wrap.classList.remove('ko-yt');
  KO.wrap.classList.add('ko-kick');
  showWrap();
  if (KO.ytState.ready && KO.ytState.playing) ytCmd('pause');
  ytCmd('mute');
  if (KO.kickFrame) {
    kickSend({ t: 'mute', m: KO.kickMuted });
    kickSend({ t: 'volume', v: KO.kickVol });
    kickSend({ t: 'play' });
    // Freshest edge ONLY when re-showing the layer after a hide — probe()
    // re-shows every poll and a per-poll seek would stall live playback.
    if (wasHidden) kickSend({ t: 'seekToLive' });
  }
  syncMute();
}

function showYtLayer() {
  if (!KO.wrap) mount();
  KO.wrap.classList.remove('ko-kick');
  KO.wrap.classList.add('ko-yt');
  showWrap();
  if (KO.kickFrame && KO.kickState && KO.kickState.state === 'Playing') {
    kickSend({ t: 'pause' }); // one rendering: hidden kick player PAUSED
    kickSend({ t: 'mute', m: true });
  }
  ytCmd('play');
  ytCmd('setVolume', KO.ytVol); // the user's last yt volume follows the player switch
  ytCmd('unmute');
  setTimeout(() => {
    // Muted autoplay fallback: unmute on the first user gesture.
    if (KO.player === 'youtube' && KO.ytState.muted) enableYtUnlock();
  }, 500);
  syncMute();
}

// kick fatal — reconnect IN PLACE: the wrap keeps covering Twitch (muted +
// paused) the whole time, so the user never sees the underlying player or
// an ad. Each attempt fetches a FRESH playback_url (IVS tokens rotate).
async function reconnect() {
  if (KO.reconnectCount >= MAX_RECONNECT) {
    teardown();
    setBadge('KICK OFF', '#6b7280');
    return;
  }
  KO.reconnectCount++;
  setBadge('RECONNECT', '#d97706');
  if (KO.wrap) {
    const rc = KO.wrap.querySelector('#ko-reconnecting');
    if (rc) rc.style.display = 'flex';
  }
  console.log(`[ko] kick reconnect attempt ${KO.reconnectCount}/${MAX_RECONNECT}`);
  diag('reconnect', { n: KO.reconnectCount, max: MAX_RECONNECT });
  const k = await kickPlaybackUrl(KO.kickSlug);
  if (!k.live || !k.url) {
    teardown();
    setBadge('KICK OFF', '#6b7280');
    return;
  }
  await new Promise((r) => setTimeout(r, 2000)); // back off, then re-attach
  if (!KO.enabled || KO.player !== 'kick') return;
  ensureKick(k.url);
  if (KO.wrap) {
    const rc = KO.wrap.querySelector('#ko-reconnecting');
    if (rc) rc.style.display = 'none';
  }
}

// Kick bar: seek slider within the live window + LIVE button when behind.
// Position/latency come from the IVS frame's ~1s state messages.
function updateKickBar() {
  if (!KO.wrap || KO.player !== 'kick') return;
  const st = KO.kickState;
  if (!st) return;
  const pos = st.pos || 0;
  const lat = st.lat;
  const liveEdge = isFinite(lat) && lat >= 0 ? pos + lat : null;
  const max = Math.max(1, Math.ceil(liveEdge || pos));
  const seek = KO.wrap.querySelector('#ko-seek');
  const live = KO.wrap.querySelector('#ko-live');
  if (seek) {
    if (!KO.seeking) seek.value = String(Math.min(max, Math.max(0, Math.floor(pos))));
    seek.max = String(max);
  }
  const behind = liveEdge !== null && lat > 8;
  if (live) live.style.display = behind ? 'block' : 'none';
  const t = Math.floor(pos);
  const hh = String(Math.floor(t / 3600)).padStart(2, '0');
  const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0');
  const ss = String(t % 60).padStart(2, '0');
  const time = KO.wrap.querySelector('#ko-time');
  if (time) time.textContent = `LIVE \u00B7 ${hh}:${mm}:${ss}`;
  const badgeEl = KO.wrap.querySelector('#ko-badge');
  if (badgeEl) {
    const qn = st.q && st.q.name;
    badgeEl.textContent = qn ? `KICK \u00B7 LIVE \u00B7 ${qn}` : 'KICK \u00B7 LIVE';
  }
}

function setPlayer(p) {
  if (p === KO.player) return;
  KO.player = p;
  saveState().then(() => apply()); // apply() only toggles layers/pause/mute
}

// ---- decision loop ----------------------------------------------------------

async function probe() {
  const slug = currentSlug();
  if (!slug) {
    setBadge('');
    teardown();
    return;
  }
  if (slug !== KO.slug) {
    KO.slug = slug;
    const m = KO.mappings[slug];
    KO.kickSlug = typeof m === 'string' ? m : (m && m.kick) || slug; // same-handle fallback
    KO.ytRaw = m && typeof m === 'object' ? m.yt || '' : '';
    KO.ytId = m && typeof m === 'object' ? m.ytId || null : null;
    teardown();
  }
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    return;
  }
  // NOTE (2026-08-13): no Twitch-live gate here anymore. It used to
  // teardown() the whole overlay whenever the native player paused or
  // swapped elements during ad transitions, which made the manual KICK
  // switch appear broken (clicks did nothing until Twitch happened to be
  // playing). Manual switches always take effect; kick liveness is gated
  // by kickPlaybackUrl(), youtube by the embed's own live state.
  updateTwLiveSticky(twitchVideo());

  if (KO.player === 'twitch') {
    // Native player; overlay players paused (rect loop keeps them so).
    if (KO.wrap) hideWrap();
    const kl = await kickPlaybackUrl(KO.kickSlug);
    const yl = KO.ytState.live;
    setBadge(kl.live ? 'KICK' : yl ? 'YT' : 'TW', kl.live ? '#059669' : yl ? '#ff0000' : '#6b7280');
    updateSwitchButtons();
    return;
  }

  if (KO.player === 'kick') {
    // Already playing on a live url? Keep it — IVS playback_urls rotate on
    // every API call and a re-attach would reset the stream to the live edge.
    // The stall watchdog reconnects with a FRESH url when the current one
    // goes stale (8s frozen). This branch is network-free → the KICK button
    // switch is instant.
    if (KO.activeUrl && KO.kickState && KO.kickState.state === 'Playing') {
      showKickLayer();
      setBadge('KICK', '#059669');
      updateSwitchButtons();
      return;
    }
    const k = await kickPlaybackUrl(KO.kickSlug);
    diag('kick_probe', { slug: KO.kickSlug, live: k.live, url: k.url ? 'yes' : 'no' });
    if (k.live && k.url) {
      ensureKick(k.url);
      showKickLayer();
      setBadge('KICK', '#059669');
      updateSwitchButtons();
      return;
    }
    console.log('[ko] kick offline or unreachable', KO.kickSlug, JSON.stringify(k));
    diag('kick_offline', { slug: KO.kickSlug, live: k.live, url: k.url ? 'yes' : 'no' });
    setBadge('KICK OFF', '#6b7280');
    if (KO.wrap) hideWrap();
    updateSwitchButtons();
    return;
  }

  // youtube mode
  if (!KO.ytRaw) {
    setBadge('YT?', '#6b7280'); // no mapping — map the channel in the popup
    if (KO.wrap) hideWrap();
    updateSwitchButtons();
    return;
  }
  await ensureYtId();
  if (!KO.ytId) {
    setBadge('YT?', '#6b7280'); // could not resolve handle → check the popup value
    if (KO.wrap) hideWrap();
    updateSwitchButtons();
    return;
  }
  ensureYtIframe();
  if (KO.ytState.live) {
    showYtLayer();
    setBadge('YT', '#ff0000');
    updateSwitchButtons();
    return;
  }
  setBadge('YT OFF', '#6b7280');
  if (KO.wrap) hideWrap();
  updateSwitchButtons();
}

async function apply() {
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    return;
  }
  const slug = currentSlug();
  if (!slug) {
    setBadge('');
    teardown();
    return;
  }
  if (slug !== KO.slug) {
    KO.slug = slug;
    const m = KO.mappings[slug];
    KO.kickSlug = typeof m === 'string' ? m : (m && m.kick) || slug;
    KO.ytRaw = m && typeof m === 'object' ? m.yt || '' : '';
    KO.ytId = m && typeof m === 'object' ? m.ytId || null : null;
    teardown();
  }
  await probe();
}

function startWatchers() {
  KO.spaTimer = setInterval(() => {
    if (location.pathname !== KO.lastPath) {
      KO.lastPath = location.pathname;
      apply();
    }
  }, SPA_MS);
  KO.pollTimer = setInterval(() => {
    if (KO.enabled) probe();
  }, POLL_MS);
  // The boot probe can race the Twitch player's start — re-probe the
  // moment the player starts playing, throttled. Video events don't
  // bubble; capture catches them.
  let lastPlayingProbe = 0;
  document.addEventListener(
    'playing',
    () => {
      if (!KO.enabled) return;
      const now = Date.now();
      if (now - lastPlayingProbe < 3000) return;
      lastPlayingProbe = now;
      probe();
    },
    true,
  );
}

function startRectLoop() {
  stopRectLoop();
  KO.rectTimer = setInterval(() => {
    if (!KO.wrap) {
      stopRectLoop();
      return;
    }
    ensureAttached(KO.wrap); // Twitch may re-render main — re-parent if dropped
    const tv = twitchVideo();
    updateTwLiveSticky(tv);
    const overlayShown = KO.player !== 'twitch' && KO.wrap.style.display !== 'none';
    if (overlayShown) {
      if (tv) {
        const r = tv.getBoundingClientRect();
        const s = KO.wrap.style;
        s.left = `${r.left}px`;
        s.top = `${r.top}px`;
        s.width = `${r.width}px`;
        s.height = `${r.height}px`;
        if (KO.wrap.style.display === 'none') showWrap();
        pauseTwitchForOverlay(); // one rendering: Twitch pauses under the overlay
      } else {
        // Debounce the hide: ad transitions / player re-layouts can briefly
        // drop the video from the tree — hiding on a single frame would blink.
        KO.hideTicks++;
        if (KO.hideTicks >= HIDE_TICKS) hideWrap();
      }
      syncMute();
      updateKickBar();
      // Stall watchdog: kick stream frozen >8s while shown, visible, and
      // playing — IVS tokens can go stale silently (no error event). Force
      // a fresh playback_url via the normal reconnect budget. Hidden tabs
      // throttle the frame's 1s state timer, so frozen pos there is NOT a
      // stall (the stream keeps playing under the muted-audio exemption).
      if (!document.hidden && KO.player === 'kick' && KO.kickState && KO.kickState.state === 'Playing') {
        const t = KO.kickState.pos || 0;
        if (Math.abs(t - KO.lastTickT) < 0.05) {
          KO.stallTicks++;
        } else {
          KO.stallTicks = 0;
          KO.lastTickT = t;
        }
        if (KO.stallTicks > 20) {
          KO.stallTicks = 0;
          if (KO.reconnectCount < MAX_RECONNECT) {
            console.log('[ko] kick stalled (8s frozen) — reconnecting');
            diag('stall', { ct: Math.floor(t) });
            reconnect();
          }
        }
      }
      // Frame-death watchdog: the bridge went silent while shown. Rebuild
      // the frame (same url — IVS reloads it). Hidden tabs throttle the
      // frame's 1s state timer, so only run while the tab is visible.
      if (!document.hidden && KO.player === 'kick' && KO.kickFrame && KO.lastKickSt && Date.now() - KO.lastKickSt > 30000) {
        console.log('[ko] kick bridge silent — rebuilding frame');
        KO.kickFrame.remove();
        KO.kickFrame = null;
        KO.kickWin = null;
        KO.kickReady = false;
        KO.kickState = null;
        KO.pendingUrl = KO.activeUrl;
        kickFrame();
      }
    } else {
      // twitch mode: overlay players stay paused; resume Twitch if ours.
      if (KO.kickFrame && KO.kickState && KO.kickState.state === 'Playing') {
        kickSend({ t: 'pause' });
        kickSend({ t: 'mute', m: true });
      }
      if (KO.ytState.ready && KO.ytState.playing) ytCmd('pause');
      resumeTwitchIfOurs();
      syncMute();
    }
  }, RECT_MS);
}

function stopRectLoop() {
  if (KO.rectTimer) {
    clearInterval(KO.rectTimer);
    KO.rectTimer = null;
  }
}

function injectStyles() {
  if (document.getElementById('ko-style')) return;
  const st = document.createElement('style');
  st.id = 'ko-style';
  st.textContent =
    '#ko-wrap{position:fixed;z-index:5;pointer-events:none;background:#000;overflow:hidden;}' +
    '#ko-wrap #ko-ivs{width:100%;height:100%;border:0;display:block;pointer-events:none;}' +
    '#ko-wrap.ko-yt iframe{width:100%;height:100%;border:0;display:block;pointer-events:auto;}' +
    '#ko-bar{position:absolute;left:0;right:0;bottom:0;pointer-events:auto;opacity:0;transition:opacity .18s ease;' +
    'display:flex;align-items:center;gap:8px;padding:9px 12px;color:#fff;' +
    'background:linear-gradient(0deg,rgba(0,0,0,.85),rgba(0,0,0,0));font:12px/1 system-ui,sans-serif;}' +
    '#ko-wrap.ko-yt #ko-bar{display:none;}' + // YT has native controls incl. LIVE chip
    '#ko-wrap.ko-hot #ko-bar{opacity:1;}' +
    '#ko-reconnecting{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;' +
    'background:rgba(0,0,0,.82);color:#fff;font:700 15px system-ui,sans-serif;letter-spacing:.04em;pointer-events:none;}' +
    '#ko-bar button{background:transparent;border:0;color:#fff;cursor:pointer;font:inherit;padding:3px 7px;border-radius:4px;white-space:nowrap;}' +
    '#ko-bar button:hover{background:rgba(255,255,255,.18);}' +
    '#ko-badge{font-weight:700;color:#53fc18;white-space:nowrap;}' +
    '#ko-time{color:#cfcfcf;white-space:nowrap;}' +
    '#ko-vol{width:60px;height:4px;accent-color:#9147ff;cursor:pointer;}' +
    '#ko-seek{flex:1;height:4px;accent-color:#53fc18;cursor:pointer;min-width:40px;}' +
    '#ko-live{font-weight:700;color:#000;background:#ff0000;border-radius:10px;padding:1px 9px;}' +
    '#ko-live:hover{background:#ff4d4d;}' +
    '#ko-twitchbtn,#ko-kickbtn,#ko-ytbtn{position:fixed;z-index:6;pointer-events:auto;display:none;' +
    'background:#9147ff;color:#fff;border:0;border-radius:14px;padding:6px 14px;' +
    'font:700 13px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.5);}' +
    '#ko-twitchbtn:hover,#ko-kickbtn:hover,#ko-ytbtn:hover{background:#a970ff;}' +
    '#ko-kickbtn{background:#53fc18;color:#000;}' +
    '#ko-ytbtn{background:#ff0000;color:#fff;}';
  (document.head || document.documentElement).appendChild(st);
}

// Floating switch buttons: show the OTHER configured platforms' buttons.
function buildSwitchButtons() {
  const anchor = overlayAnchor();
  if (!document.getElementById('ko-twitchbtn')) {
    const b = document.createElement('button');
    b.id = 'ko-twitchbtn';
    b.textContent = 'TWITCH';
    b.title = 'Show the native Twitch player (the other player pauses)';
    b.addEventListener('click', () => setPlayer('twitch'));
    anchor.appendChild(b);
  }
  if (!document.getElementById('ko-kickbtn')) {
    const b = document.createElement('button');
    b.id = 'ko-kickbtn';
    b.textContent = 'KICK';
    b.title = 'Show the Kick player (no Twitch ads)';
    b.addEventListener('click', () => setPlayer('kick'));
    anchor.appendChild(b);
  }
  if (!document.getElementById('ko-ytbtn')) {
    const b = document.createElement('button');
    b.id = 'ko-ytbtn';
    b.textContent = 'YOUTUBE';
    b.title = 'Show the YouTube player (no Twitch ads)';
    b.addEventListener('click', () => {
      setPlayer('youtube');
      // Unmapped: open the popup so the user can paste the channel —
      // the click is a user gesture, so openPopup() is allowed (127+).
      if (!KO.ytRaw && !KO.ytId) {
        try {
          chrome.action.openPopup();
        } catch {
          /* older Chrome — badge 'YT?' is shown instead */
        }
      }
    });
    anchor.appendChild(b);
  }
}

// Pin the active switch buttons to the Twitch player (bottom-right row).
function updateSwitchButtons() {
  const ids = [];
  if (KO.player !== 'twitch') ids.push('ko-twitchbtn');
  if (KO.player !== 'kick') ids.push('ko-kickbtn');
  if (KO.player !== 'youtube') ids.push('ko-ytbtn'); // always visible — unmapped click opens the popup
  const tv = twitchVideo();
  const shown = [];
  let totalW = 0;
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    ensureAttached(el); // survive Twitch main re-renders
    el.style.display = 'none';
    if (!tv) continue;
    el.style.display = 'block';
    totalW += el.offsetWidth + 8;
    shown.push(el);
  }
  if (!tv || !shown.length) return;
  const r = tv.getBoundingClientRect();
  let x = r.right - totalW - 12;
  for (const el of shown) {
    el.style.left = `${Math.max(8, x)}px`;
    el.style.top = `${Math.max(8, r.bottom - el.offsetHeight - 12)}px`;
    x += el.offsetWidth + 8;
  }
}

// ---- test / automation hook -------------------------------------------------
// The page world can dispatch CustomEvent('kick-overlay:set', {detail:{...}})
// to flip the toggle or remap the current channel (used by smoke tests).
window.addEventListener('kick-overlay:set', (e) => {
  const d = e.detail || {};
  if (typeof d.enabled === 'boolean') {
    KO.enabled = d.enabled;
    saveState().then(() => apply());
  }
  if (d.kickSlug && KO.slug) {
    const m = KO.mappings[KO.slug];
    KO.mappings[KO.slug] = typeof m === 'object' ? { ...m, kick: d.kickSlug } : { kick: d.kickSlug };
    KO.kickSlug = d.kickSlug;
    saveState().then(() => apply());
  }
  if (d.ytChannel && KO.slug) {
    const m = KO.mappings[KO.slug];
    KO.mappings[KO.slug] = typeof m === 'object' ? { ...m, yt: d.ytChannel } : { yt: d.ytChannel };
    KO.ytRaw = d.ytChannel;
    KO.ytId = null;
    saveState().then(() => apply());
  }
});
window.addEventListener('kick-overlay:status', () => {
  const v = twitchVideo();
  window.dispatchEvent(
    new CustomEvent('kick-overlay:status-reply', {
      detail: {
        enabled: KO.enabled,
        player: KO.player,
        slug: KO.slug,
        kickSlug: KO.kickSlug,
        ytRaw: KO.ytRaw,
        ytId: KO.ytId,
        mounted: !!KO.wrap,
        wrapShown: !!(KO.wrap && KO.wrap.style.display !== 'none'),
        kickPlaying: !!(KO.kickState && KO.kickState.state === 'Playing' && (KO.kickState.pos || 0) > 0),
        kickMuted: KO.kickMuted,
        yt: { ...KO.ytState },
        twitchLive: KO.twLive,
        twitchPlaying: twitchIsLive(v),
        twitchPausedByUs: KO.twWasPlaying,
        twitchMuted: [...document.querySelectorAll('video')].every((x) => x.muted),
      },
    }),
  );
});

// ---- boot -------------------------------------------------------------------

(async function init() {
  await loadState();
  // Persist the resolved defaults (enabled: true) so the popup's toggle
  // always agrees with the content script.
  if (Object.keys(KO.mappings).length === 0) {
    await saveState();
  }
  injectStyles();
  buildSwitchButtons();
  startWatchers();
  setInterval(updateSwitchButtons, RECT_MS);
  diag('boot', { url: location.href.slice(0, 70), slug: currentSlug(), enabled: KO.enabled, player: KO.player, kickSlug: KO.kickSlug, hidden: document.hidden });
  // Heartbeat: full overlay state every 8s while the page is open.
  setInterval(() => {
    const tv = twitchVideo();
    diag('hb', {
      slug: KO.slug,
      player: KO.player,
      wrapShown: !!(KO.wrap && KO.wrap.style.display !== 'none'),
      hidden: document.hidden,
      focused: document.hasFocus(),
      tw: tv
        ? { rs: tv.readyState, paused: tv.paused, ct: Math.floor(tv.currentTime || 0), muted: tv.muted, err: tv.error ? tv.error.code : 0 }
        : null,
      kick: KO.kickState
        ? {
            state: KO.kickState.state,
            paused: KO.kickState.paused,
            ct: Math.floor(KO.kickState.pos || 0),
            muted: KO.kickState.muted,
            q: (KO.kickState.q && KO.kickState.q.name) || '',
            err: 0,
          }
        : null,
      yt: { ...KO.ytState },
      twLive: KO.twLive,
    });
  }, 8000);
  apply();
})();
