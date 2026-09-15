'use strict';
// Kick player bridge — hosts the SAME engine kick.com uses (Amazon IVS
// web player, amazon-ivs-player) inside the extension page, so the wasm
// worker is same-origin (no CORS/importScripts traps) and playback_url
// fetches ride on the extension's host_permissions. Controls are driven
// over postMessage by the content script; state is reported back ~1/s.
const V = document.getElementById('v');
const params = new URLSearchParams(location.search);
const HLS_MODE = params.get('m') === 'hls';
const PLAYER_TOKEN = params.get('token');
// The embed's parent page. Historically twitch.tv only; since 0.8.2 the
// same bridge also runs over youtube.com pages (the overlay's YT layer).
// The actual origin is LEARNED from the first token-valid message — the
// shared secret (PLAYER_TOKEN, minted per page by the content script) is
// what authenticates the parent, not a hardcoded host — so replies never
// need '*'. Until a valid message arrives, twitch.tv is the default.
const PARENT_ORIGINS = new Set([
  'https://www.twitch.tv',
  'https://www.youtube.com',
  'https://m.youtube.com',
  'https://youtube.com',
  'https://youtu.be',
]);
let PARENT_ORIGIN = 'https://www.twitch.tv';
const beacon = (ev, data) => {
  try {
    chrome.runtime.sendMessage({ __koDiag: { ev, data } }, () => void chrome.runtime.lastError);
  } catch {
    /* beacon must never break the player */
  }
};
window.addEventListener('error', (e) => beacon('ivs_page_err', { msg: String((e && e.message) || e).slice(0, 150), src: (e && e.filename ? e.filename.slice(-40) : '') }));
window.addEventListener('unhandledrejection', (e) => beacon('ivs_page_err', { rej: String((e && e.reason) || '').slice(0, 150) }));
const post = (m) => {
  if (!PLAYER_TOKEN) return;
  try {
    parent.postMessage({ __koKick: { ...m, _koToken: PLAYER_TOKEN } }, PARENT_ORIGIN);
  } catch {
    /* parent gone */
  }
};

// Keyboard relay (0.8.6, pain 2): when focus lives INSIDE the extension-page
// iframe (after the user clicks the player — the host's document-level
// keydown capture never sees those presses, because the event targets the
// chrome-extension:// frame's own document), forward the overlay-relevant
// keys up so the content script issues the same seek/fullscreen it would for
// a keypress on the Twitch page. Only the keys the host's onOverlayKeydown
// whitelists (f / arrowleft / arrowright) are relayed, so the native YT
// controls (space = play/pause, and every other key) are left untouched.
// 'f' maps to the host's fullscreen toggle. preventDefault keeps the frame
// from also acting on the arrow (e.g. scrolling the page body).
const RELAY_KEYS = new Set(['f', 'arrowleft', 'arrowright']);
window.addEventListener('keydown', (ev) => {
  const k = String(ev.key).toLowerCase();
  if (!RELAY_KEYS.has(k) || ev.ctrlKey || ev.metaKey || ev.altKey || ev.shiftKey) return;
  const t = ev.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  try { ev.preventDefault(); } catch { /* ignore */ }
  post({ t: 'key', k });
});

if (HLS_MODE) {
  // ---- HLS engine (YouTube layer) ------------------------------------------
  // Same bridge protocol as IVS: {t:'load'|'play'|'pause'|'mute'|'volume'|
  // 'seek'|'seekToLive'|'getState'} in, {t:'ready'} + ~1/s {t:'st'} + {t:'ev'}
  // out. hls.js plays the innertube hlsManifestUrl; native controls visible.
  V.controls = true; // native controls give the yt layer the embed-era UX
  // 0.8.6 pain-1: the yt rewind window is TUNED, not whatever the playlist
  // grants — liveBackBufferLength anchors the ~20s seekable window the bar,
  // the "LIVE -Ns" pill and the edge-clamp all promise (0.8.5 left it at the
  // hls.js default, so the window was shorter than the UI implied).
  const h = new Hls({ maxBufferLength: 30, liveSyncDurationCount: 3, liveBackBufferLength: 20 });
  let currentUrl = null;
  let reloaded = false;
  const st = () => {
    let dur = 0;
    try { dur = V.duration || 0; } catch { /* not ready */ }
    if (!isFinite(dur) || dur < 0 || dur >= 1e15) dur = 0;
    let pos = V.currentTime || 0;
    if (!isFinite(pos) || pos < 0 || pos >= 1e15) pos = 0;
    let lat = 0;
    try {
      if (V.seekable && V.seekable.length) lat = V.seekable.end(V.seekable.length - 1) - pos;
    } catch { /* not ready */ }
    if (!isFinite(lat) || lat < 0) lat = 0;
    const levels = h.levels || [];
    let q = null;
    if (h.currentLevel >= 0 && levels[h.currentLevel]) {
      const l = levels[h.currentLevel];
      q = { name: l.height ? `${l.height}p` : `${l.width || ''}w`, w: l.width, h: l.height };
    }
    const qualities = levels.map((l, i) => ({
      id: i,
      name: l.height ? `${l.height}p` : `${l.width || ''}w`,
      w: l.width,
      h: l.height,
    }));
    // A drained live stream ends the element and then sits paused forever, so
    // 'Ended' gets its own name: the content side maps an unknown state to
    // NOT-live, which is what stops a dead YouTube layer from pinning a black
    // box over the deleted Twitch player.
    const state = V.ended ? 'Ended' : V.paused ? (V.readyState === 0 ? 'Idle' : 'Paused') : 'Playing';
    return {
      state,
      paused: V.paused,
      muted: V.muted,
      volume: V.volume,
      pos,
      lat,
      dur,
      q,
      qcount: qualities.length,
      qualities,
    };
  };
  const sendSt = () => post({ t: 'st', st: st() });
  window.addEventListener('message', (ev) => {
    if (ev.source !== window.parent || !PARENT_ORIGINS.has(ev.origin)) return;
    const m = ev.data && ev.data.__koKick;
    if (!m || m._koToken !== PLAYER_TOKEN) return;
    PARENT_ORIGIN = ev.origin; // answers follow the page that owns us
    switch (m.t) {
      case 'load':
        try {
          currentUrl = m.url;
          reloaded = false;
          h.loadSource(m.url);
          h.attachMedia(V);
          V.play().catch(() => { /* autoplay policy — unlock via gesture */ });
        } catch (e) {
          post({ t: 'ev', e: 'error', d: String(e) });
        }
        break;
      case 'play':
        V.play().catch(() => { /* ignore */ });
        break;
      case 'pause':
        V.pause();
        break;
      case 'mute':
        V.muted = !!m.m;
        break;
      case 'volume':
        if (Number.isFinite(m.v)) V.volume = Math.max(0, Math.min(1, m.v));
        break;
      case 'seek':
        if (Number.isFinite(m.s) && m.s >= 0 && m.s < 1e15) {
          // Clamp into the actual seekable range (the live window), never
          // past the live edge — an un-clamped currentTime there silently
          // snaps back to the buffer head on the next 'st' (0.8.5: arrow
          // overshoot left the playhead visibly jittering instead of live).
          let lo = 0, hi = m.s;
          try {
            if (V.seekable && V.seekable.length) {
              lo = V.seekable.start(0);
              hi = V.seekable.end(V.seekable.length - 1);
            }
          } catch { /* not ready */ }
          V.currentTime = Math.max(lo, Math.min(hi, m.s));
        }
        break;
      case 'seekToLive':
        try {
          if (V.seekable && V.seekable.length) V.currentTime = V.seekable.end(V.seekable.length - 1);
        } catch { /* ignore */ }
        break;
      case 'quality':
        if (m.q === 'auto') h.currentLevel = -1;
        else if (Number.isInteger(m.q) && m.q >= 0 && m.q < h.levels.length) h.currentLevel = m.q;
        break;
      // Vertical quality menu (0.8.2): the REAL hls.js levels, not a static
      // guess list. 'levels' mirrors the {t:'st'} qualities shape; the menu
      // adds its own Auto row.
      case 'getLevels':
        post({
          t: 'levels',
          levels: (h.levels || []).map((l, i) => ({
            id: i,
            name: l.height ? `${l.height}p` : `${l.width || ''}w`,
            w: l.width,
            h: l.height,
          })),
        });
        break;
      case 'setLevel':
        if (m.l === 'auto') h.currentLevel = -1;
        else if (Number.isInteger(m.l) && m.l >= 0 && m.l < (h.levels || []).length) h.currentLevel = m.l;
        sendSt(); // the checkmark follows the engine, not the click
        break;
      case 'getState':
        sendSt();
        break;
    }
  });
  h.on(Hls.Events.ERROR, (_e, d) => {
    beacon('hls_err', { type: d.type, details: d.details, fatal: !!d.fatal });
    if (d.fatal) {
      post({ t: 'ev', e: 'error', d: String(d.details || d.type) });
      // One reload per url (stale manifest on live switches); then the
      // content script's fallback (kick) takes over.
      if (currentUrl && !reloaded) {
        reloaded = true;
        setTimeout(() => {
          h.loadSource(currentUrl);
          h.attachMedia(V);
        }, 1500);
      }
    }
  });
  setInterval(sendSt, 1000);
  post({ t: 'ready' });
} else {
  const IVS = window.IVSPlayerModule;
  beacon('ivs_page', { mod: typeof IVS, create: typeof (IVS || {}).create });
  const create = IVS.create;
  const ET = IVS.PlayerEventType || {};
  const p = create({
    wasmWorker: chrome.runtime.getURL('ivs/amazon-ivs-wasmworker.min.js'),
    wasmBinary: chrome.runtime.getURL('ivs/amazon-ivs-wasmworker.min.wasm'),
    logLevel: 'warn',
  });
  beacon('ivs_boot', { ver: p.getVersion(), worker: chrome.runtime.getURL('ivs/amazon-ivs-wasmworker.min.js').slice(-44) });
  p.attachHTMLVideoElement(V);
  p.setAutoplay(true);
  p.setMuted(true);

// A position to apply once the next load() completes (kick-style rewind:
// switch to the DVR url, then seek within the loaded broadcast).
let pendingSeek = null;
let pendingTries = 0; // seek retry budget (1/s) — give up instead of looping

function sendSt() {
  let q = null;
  try {
    const qq = p.getQuality();
    if (qq) q = { name: qq.name, w: qq.width, h: qq.height };
  } catch {
    /* not ready */
  }
  let qualities = [];
  try {
    qualities = p.getQualities().map((quality, id) => ({
      id,
      name: quality.name,
      w: quality.width,
      h: quality.height,
    }));
  } catch {
    /* not ready */
  }
  let dur = 0;
  try {
    dur = p.getDuration() || 0;
  } catch {
    /* not ready */
  }
  // Sanitize everything that reaches the bar: a poisoned seek (or IVS
  // quirks) can report MAX_SAFE_INTEGER / NaN positions — the content
  // script must never format Infinity or set an insane input range.
  let pos = 0;
  try {
    pos = p.getPosition();
  } catch {
    /* not ready */
  }
  if (!isFinite(pos) || pos < 0 || pos >= 1e15) pos = 0;
  let lat = 0;
  try {
    lat = p.getLiveLatency();
  } catch {
    /* not ready */
  }
  if (!isFinite(lat)) lat = 0;
  if (!isFinite(dur) || dur < 0 || dur >= 1e15) dur = 0;
  // Retry a pending rewind seek every poll until it lands, capped — IVS
  // drops a seekTo issued before the loaded media is seekable (first
  // Playing can fire with an empty seekable range), but a target that
  // never becomes reachable must give up instead of seeking forever
  // (that "video replays every second" bug — each seek restarts playback).
  if (pendingSeek !== null && pendingSeek < 1e15) {
    if (pendingTries-- > 0) {
      if (pos < pendingSeek - 1) {
        try {
          p.seekTo(pendingSeek);
        } catch {
          pendingSeek = null; // outside the timeline — give up quietly
        }
      } else {
        pendingSeek = null; // landed
      }
    } else {
      pendingSeek = null; // capped — stop hammering
    }
  }
  post({
    t: 'st',
    st: {
      state: p.getState(),
      paused: p.isPaused(),
      muted: p.isMuted(),
      volume: p.getVolume(),
      pos,
      lat,
      dur,
      q,
      qcount: qualities.length,
      qualities,
    },
  });
}

window.addEventListener('message', (ev) => {
  if (ev.source !== window.parent || !PARENT_ORIGINS.has(ev.origin)) return;
  const m = ev.data && ev.data.__koKick;
  if (!m || m._koToken !== PLAYER_TOKEN) return;
  PARENT_ORIGIN = ev.origin; // answers follow the page that owns us
  switch (m.t) {
    case 'load':
      try {
        pendingSeek = null;
        p.load(m.url);
        if (Number.isFinite(m.seekTo) && m.seekTo > 0 && m.seekTo < 1e15) {
          pendingSeek = m.seekTo;
          pendingTries = 30;
        }
        p.play();
      } catch (e) {
        post({ t: 'ev', e: 'error', d: String(e) });
      }
      break;
    case 'play':
      try { p.play(); } catch (e) { /* ignore */ }
      break;
    case 'pause':
      try { p.pause(); } catch (e) { /* ignore */ }
      break;
    case 'mute':
      try { p.setMuted(!!m.m); } catch (e) { /* ignore */ }
      break;
    case 'volume':
      try { p.setVolume(m.v); } catch (e) { /* ignore */ }
      break;
    case 'seekToLive':
      try {
        const lat = p.getLiveLatency();
        if (isFinite(lat) && lat > 0) p.seekTo(p.getPosition() + lat);
        else p.play(); // already at the edge — no MAX-hack (that poisoned pos)
      } catch (e) { /* ignore */ }
      break;
    case 'quality':
      try {
        if (m.q === 'auto') {
          p.setAutoQualityMode(true);
        } else if (Number.isInteger(m.q)) {
          const qualities = p.getQualities();
          const selected = qualities[m.q];
          if (selected) {
            p.setAutoQualityMode(false);
            p.setQuality(selected);
          }
        }
      } catch (e) { /* quality menu is best-effort */ }
      break;
    // Vertical quality menu (0.8.2): IVS's real quality ladder, same
    // {id,name,w,h} shape the 'st' message carries.
    case 'getLevels': {
      let levels = [];
      try {
        levels = p.getQualities().map((quality, id) => ({
          id,
          name: quality.name,
          w: quality.width,
          h: quality.height,
        }));
      } catch (e) { /* not ready yet */ }
      post({ t: 'levels', levels });
      break;
    }
    case 'setLevel':
      try {
        if (m.l === 'auto') {
          p.setAutoQualityMode(true);
        } else if (Number.isInteger(m.l) && m.l >= 0) {
          const selected = p.getQualities()[m.l];
          if (selected) {
            p.setAutoQualityMode(false);
            p.setQuality(selected);
          }
        }
      } catch (e) { /* quality menu is best-effort */ }
      sendSt(); // the checkmark follows the engine, not the click
      break;
    case 'getState':
      sendSt();
      break;
  }
});

try {
  p.addEventListener(ET.ERROR || 'PlayerError', (e) => {
    const info = { d: (e && e.message) || 'unknown', code: (e && e.code) || 0 };
    post({ t: 'ev', e: 'error', d: info.d, code: info.code });
    beacon('ivs_err', info);
  });
  p.addEventListener(ET.REBUFFERING || 'PlayerRebuffering', () => post({ t: 'ev', e: 'rebuffering' }));
} catch {
  /* event wiring is best-effort; state polling still reports errors via getState */
}
setInterval(sendSt, 1000);
post({ t: 'ready' });
} // /else (IVS engine)
