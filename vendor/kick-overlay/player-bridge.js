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
const PARENT_ORIGIN = 'https://www.twitch.tv';
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

if (HLS_MODE) {
  // ---- HLS engine (YouTube layer) ------------------------------------------
  // Same bridge protocol as IVS: {t:'load'|'play'|'pause'|'mute'|'volume'|
  // 'seek'|'seekToLive'|'getState'} in, {t:'ready'} + ~1/s {t:'st'} + {t:'ev'}
  // out. hls.js plays the innertube hlsManifestUrl; native controls visible.
  V.controls = true; // native controls give the yt layer the embed-era UX
  const h = new Hls({ maxBufferLength: 30, liveSyncDurationCount: 3 });
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
    const state = V.paused ? (V.readyState === 0 ? 'Idle' : 'Paused') : 'Playing';
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
    if (ev.source !== window.parent || ev.origin !== PARENT_ORIGIN) return;
    const m = ev.data && ev.data.__koKick;
    if (!m || m._koToken !== PLAYER_TOKEN) return;
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
        if (Number.isFinite(m.s) && m.s >= 0 && m.s < 1e15) V.currentTime = m.s;
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
  if (ev.source !== window.parent || ev.origin !== PARENT_ORIGIN) return;
  const m = ev.data && ev.data.__koKick;
  if (!m || m._koToken !== PLAYER_TOKEN) return;
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
    case 'seek':
      try { p.seekTo(m.s); } catch (e) { /* outside window */ }
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
