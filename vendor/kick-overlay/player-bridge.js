'use strict';
// Kick player bridge — hosts the SAME engine kick.com uses (Amazon IVS
// web player, amazon-ivs-player) inside the extension page, so the wasm
// worker is same-origin (no CORS/importScripts traps) and playback_url
// fetches ride on the extension's host_permissions. Controls are driven
// over postMessage by the content script; state is reported back ~1/s.
const V = document.getElementById('v');
const IVS = window.IVSPlayerModule;
const beacon = (ev, data) => {
  try {
    chrome.runtime.sendMessage({ __koDiag: { ev, data } }, () => void chrome.runtime.lastError);
  } catch {
    /* beacon must never break the player */
  }
};
window.addEventListener('error', (e) => beacon('ivs_page_err', { msg: String((e && e.message) || e).slice(0, 150), src: (e && e.filename ? e.filename.slice(-40) : '') }));
window.addEventListener('unhandledrejection', (e) => beacon('ivs_page_err', { rej: String((e && e.reason) || '').slice(0, 150) }));
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

const post = (m) => {
  try {
    parent.postMessage({ __koKick: m }, '*');
  } catch {
    /* parent gone */
  }
};

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
  let qcount = 0;
  try {
    qcount = p.getQualities().length;
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
    st: { state: p.getState(), paused: p.isPaused(), muted: p.isMuted(), volume: p.getVolume(), pos, lat, dur, q, qcount },
  });
}

window.addEventListener('message', (ev) => {
  if (ev.source !== window.parent) return;
  const m = ev.data && ev.data.__koKick;
  if (!m) return;
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
