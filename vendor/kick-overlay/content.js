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
  kickUrlT: 0,      // epoch ms of the current playback_url load (dead-url watchdog)
  kickEverPlayed: false, // current url reached Playing at least once (re-buffers ≠ dead url)
  kickDvrUrl: null, // replay source (sources.dvr) from the playback bootstrap
  kickOnDvr: false, // frame is playing the DVR url (replay mode)
  kickDur: 0,       // broadcast duration (IVS getDuration) — seek-bar range
  kickStreamStart: null, // v1/video created_at (ISO-Z, absolute) — bar max base
  kickDvrFetchedAt: 0, // when kickStreamStart was captured (elapsed grows)
  kickStreamId: null, // live stream id (v2 livestream.id) — DVR entry match
  twDeleted: false, // user deleted the Twitch player (popup) — restored on reload
  lastRect: null,   // last Twitch player rect (keeps the overlay pinned when deleted)
  dvrFetchedFor: null, // playback_url the DVR source was fetched for (once per url)
  ytVol: 100,       // user's youtube volume (persisted, applied on show)
  ytState: { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 },
  ytUnlock: false,
  twLive: false,       // sticky: Twitch channel confirmed live on this page
  twWasPlaying: false, // Twitch was playing when we paused it → resume on switch back
  seeking: false,
  reconnectCount: 0,
  stableSince: null, // epoch ms of the current uninterrupted Playing run (budget reset)
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
// Live iff the `livestream` object exists: kick's v2 channel endpoint ALSO
// returns a stale top-level playback_url for OFFLINE channels (proven with
// nyro — livestream:null + playback_url present → the url does not stream),
// which used to put a dead black player over Twitch.
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
      if (ls) {
        if (ls.playback_url) return { live: true, url: ls.playback_url, streamId: ls.id || null };
        if (d && d.playback_url) return { live: true, url: d.playback_url, streamId: ls.id || null };
      }
      return { live: false }; // livestream null (or url-less) → channel offline
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

// YouTube layer — driven DIRECTLY against the live_stream embed via the
// IFrame API postMessage protocol, no page-world script needed (a <script
// src="chrome-extension://…"> injection into the Twitch page is silently
// CSP-blocked — the old yt-bridge.js never ran, so the embed never got
// ready). Commands go to the iframe's contentWindow; the embed answers
// with 'infoDelivery' messages the content script receives on the top
// window (that is how the official IFrame API works — no same-origin
// access required). Native embed controls stay enabled.
const YT_FN = { play: 'playVideo', pause: 'pauseVideo', mute: 'mute', unmute: 'unMute', setVolume: 'setVolume', seekToLive: 'seekTo' };

function ytCmd(cmd, arg) {
  const f = document.getElementById('ko-yt');
  if (!f || !f.contentWindow) return;
  const fn = YT_FN[cmd];
  if (!fn) return;
  try {
    f.contentWindow.postMessage({ event: 'command', func: fn, args: cmd === 'seekToLive' ? [Number.MAX_SAFE_INTEGER] : [arg] }, '*');
  } catch {
    /* frame gone */
  }
}

function resolveYtChannel(raw) {
  return new Promise((res) => {
    try {
      chrome.runtime.sendMessage({ type: 'ko-resolve-yt', value: raw }, (r) => {
        diag('yt_resolve', { raw, id: (r && r.id) ? r.id.slice(0, 10) : null, err: (r && r.error) || null });
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
  if (document.getElementById('ko-yt')) return;
  if (!KO.wrap) mount(); // youtube-only path never mounted the wrap (kick paths did)
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
  diag('yt_embed', { id: KO.ytId.slice(0, 8) });
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
let ytFirstInfo = false;
window.addEventListener('message', (ev) => {
  const f = document.getElementById('ko-yt');
  if (!f || ev.source !== f.contentWindow) return;
  const d = ev.data;
  if (!d || typeof d !== 'object') return;
  // The embed announces itself (the IFrame API handshake).
  if (d.event === 'onReady' || d.event === 'initialDelivery') {
    if (!ytFirstInfo) {
      ytFirstInfo = true;
      diag('yt_ready', {});
    }
    KO.ytState.ready = true;
    ytCmd('setVolume', KO.ytVol); // restore the user's last yt volume on a fresh embed
    return;
  }
  if (d.event !== 'infoDelivery' || !d.info) return;
  const info = d.info;
  const st = info.playerState; // -1 unstarted, 0 ended, 1 playing, 2 paused, 3 buffering, 5 cued
  const prevLive = KO.ytState.live;
  // LIVE detection: a real stream is playing/buffering; the offline
  // placeholder sits at -1 (unstarted) and never leaves it.
  KO.ytState.playing = st === 1;
  KO.ytState.muted = !!info.muted;
  KO.ytState.live = st === 1 || st === 3;
  KO.ytState.dur = info.duration || 0;
  KO.ytState.ct = info.currentTime || 0;
  KO.ytState.vq = info.playbackQuality || '';
  if (typeof info.volume === 'number' && info.volume >= 0 && !info.muted && KO.ytVol !== info.volume) {
    KO.ytVol = info.volume; // remember the user's yt volume slider
    saveState();
  }
  if (KO.player === 'youtube') throttledYtProbe();
  if (prevLive && !KO.ytState.live && KO.player === 'youtube') throttledYtProbe();
  if (KO.player === 'youtube' && KO.ytState.live && KO.ytState.muted) enableYtUnlock();
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
    if (m.st && m.st.state === 'Playing') KO.kickEverPlayed = true;
    // Reconnect budget replenishes only after a stable Playing stretch —
    // a fresh url that plays 2s then errors must NOT reset the budget
    // (that was the endless reconnect loop: ensureKick reset it per try).
    if (m.st && m.st.state === 'Playing') {
      if (!KO.stableSince) KO.stableSince = Date.now();
      else if (Date.now() - KO.stableSince > 15000) KO.reconnectCount = 0;
    } else {
      KO.stableSince = null;
    }
    if (KO.kickOnDvr && m.st && m.st.state === 'Ended') kickBackToLive(); // kick: replay ended → go live
    if (KO.player === 'kick') updateKickBar();
  } else if (m.t === 'ev') {
    if (m.e === 'error') {
      console.error('[ko] kick (IVS) error', m.d || '');
      diag('ivs_error', { msg: String(m.d || '').slice(0, 140), code: m.code || 0 });
      if (!KO.enabled || KO.player !== 'kick' || !KO.activeUrl) return;
      // DVR errors → go live once (kick-identical: replay failure returns to
      // the edge), never a reconnect storm. Live errors → budgeted reconnect.
      if (KO.kickOnDvr) kickBackToLive();
      else reconnect();
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
  KO.kickUrlT = Date.now();
  KO.kickEverPlayed = false;
  KO.kickOnDvr = false;
  KO.kickDur = 0;
  KO.stableSince = null;
  kickFrame();
  kickFetchDvr(); // replay source (best-effort, once per url)
  KO.pendingUrl = url;
  if (KO.kickReady) {
    kickSend({ t: 'load', url });
    KO.pendingUrl = null;
  }
}

// DVR/replay source — the same path VOD.RIP's downloader uses: the channel's
// newest VOD entry (id == the live stream's id while live) → its video uuid →
// GET /api/v1/video/<uuid> → `source` (the broadcast's HLS master, served
// from stream.kick.com with Access-Control-Allow-Origin: *). The IVS player
// loads it like any other HLS master; rewinds seek within its timeline.
// Fetched once per playback_url. (The POST /api/v1/stream/<uuid>/playback
// bootstrap needs the page's playbackVideoId, which the overlay — running on
// the Twitch page — cannot see.)
async function kickFetchDvr() {
  if (!KO.activeUrl || KO.dvrFetchedFor === KO.activeUrl) return;
  KO.dvrFetchedFor = KO.activeUrl;
  try {
    const vids = await (await fetch(`https://kick.com/api/v2/channels/${encodeURIComponent(KO.kickSlug)}/videos`, { credentials: 'omit' })).json();
    const list = Array.isArray(vids) ? vids : [];
    const cur = KO.kickStreamId ? list.find((v) => v && v.id === KO.kickStreamId) : null;
    const item = cur || list[0];
    if (!item || !item.video || !item.video.uuid) {
      diag('dvr_fetch', { err: 'no vod entry', slug: KO.kickSlug, streamId: KO.kickStreamId });
      return;
    }
    const v1 = await (await fetch(`https://kick.com/api/v1/video/${item.video.uuid}`, { credentials: 'omit' })).json();
    const src = v1 && v1.source;
    if (typeof src === 'string' && src) {
      KO.kickDvrUrl = src;
      // created_at is ISO-8601 UTC ("...Z") — an ABSOLUTE timestamp, so the
      // full-broadcast elapsed (bar max) is wall-clock-safe on any TZ (the
      // naive "YYYY-MM-DD HH:MM:SS" on the v2 channel endpoint is not).
      KO.kickStreamStart = Date.parse(v1.created_at) || null;
      KO.kickDvrFetchedAt = Date.now();
      diag('dvr_fetch', { ok: true, uuid: item.video.uuid.slice(0, 8), host: (src.split('/')[2] || '').slice(0, 40) });
    } else {
      diag('dvr_fetch', { err: 'no source in v1/video', uuid: item.video.uuid.slice(0, 8) });
    }
  } catch (e) {
    diag('dvr_fetch', { err: String(e).slice(0, 120) });
  }
}

// ---- kick.com-identical seek UX ---------------------------------------------
// Kick's player: any back-seek ≤30s from the edge just goes live; farther
// back switches to the DVR (VOD) url of the same broadcast (sources.dvr),
// seeked to the target. "Go Live" switches back to the live url. We mirror
// that exactly — the live url is never back-sought.
function kickSeekTo(target) {
  const st = KO.kickState || {};
  const max = Math.max(1, KO.kickDur || Math.ceil((st.pos || 0) + (st.lat || 0)));
  if (max - target <= 30) {
    kickBackToLive();
    return;
  }
  kickStartDvr(target);
}

function kickStartDvr(target) {
  if (!KO.kickDvrUrl) {
    diag('dvr_rewind', { skip: 'no vod url' }); // bootstrap failed → nothing to rewind into
    return;
  }
  KO.kickOnDvr = true;
  kickSend({ t: 'load', url: KO.kickDvrUrl, seekTo: target });
  updateKickBar();
}

function kickBackToLive() {
  if (KO.kickOnDvr && KO.activeUrl) {
    KO.kickOnDvr = false;
    // A fresh load of the live url starts AT the live edge (kick.com's
    // goBackToLive does exactly this: switch to the live url, no seek).
    // Reset the url timer so the transition grace covers this reload.
    KO.kickUrlT = Date.now();
    kickSend({ t: 'load', url: KO.activeUrl });
  } else {
    kickSend({ t: 'seekToLive' });
  }
  updateKickBar();
}

// ---- mute/pause model -------------------------------------------------------
// One player renders at a time: the OVERLAY players (kick frame + yt iframe)
// pause when hidden. The native Twitch player is different (user mandate
// 2026-08-14): it KEEPS RUNNING, muted, while covered — so a Twitch ad plays
// out inaudibly and switching back lands on live content, not on a paused ad.
// syncMute() is what keeps it inaudible; nothing pauses it for the overlay.

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

// The Twitch player runs through the overlay (muted) so ads play out in the
// background; nothing to do here — syncMute() handles the audio.
function pauseTwitchForOverlay() {
  /* no-op: Twitch keeps playing, muted, under the overlay (ad-through) */
}

function resumeTwitchIfOurs() {
  /* no-op: the native player was never paused; syncMute() unmutes on switch */
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

// ---- kick.com-identical player chrome (mined 2026-08-14) ---------------------
// Markup + Tailwind classes copied from kick's real player chunk
// (0x2w0nqf7e3t1.js) + compiled CSS (assets.kick.com): seekbar root
// `group/seekbar absolute -top-7 left-0 h-5 w-full`, track `bg-subtle/50`
// (rgba(146,158,166,.5)), fill + thumb `bg-green-500`/`bg-primary-base`
// (#53fc18), loaded progress `bg-white/50` + indicator `bg-white/30`
// (scaleX), hover time pill `-top-10 rounded-md p-1 text-xs font-bold`,
// thumb hidden until hover (`lg:betterhover:group-hover/seekbar:block`),
// volume slider `w-[100px] h-[3px] bg-[#24272C]` + `bg-white` fill, root
// `bg-gradient-to-t from-black/80 to-black/0`, LIVE badge = OnlineIcon +
// `text-sm font-semibold`, live elapsed `text-xs font-bold tabular-nums`.
// Behavior mirrors the mined store: live → rewind = startPlayingDVR at
// min(click, edge−30s); DVR → ≤30s from edge = goBackToLive, else DVR seek;
// hover shows "LIVE" near the edge, a -countdown behind, the time in DVR.
const KO_SVG = {
  play: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M6.251 1H1.743v18h4.508l12.006-9z"/></svg>',
  pause: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M7.66 1H4.15v18h3.51zm8.19 0h-3.51v18h3.51z"/></svg>',
  sound: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M14.5 10A4.5 4.5 0 0 0 10 5.5v2.25A2.257 2.257 0 0 1 12.25 10 2.257 2.257 0 0 1 10 12.25v2.25a4.5 4.5 0 0 0 4.5-4.5"/><path d="M10 1v2.25A6.755 6.755 0 0 1 16.75 10 6.755 6.755 0 0 1 10 16.75V19c4.973 0 9-4.027 9-9s-4.027-9-9-9M1 5.5v9h2.25l4.5 4.5V1l-4.5 4.5z"/></svg>',
  muted: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 14.503c2.486 0 4.5-2.013 4.5-4.497v-.102l-4.489 3.53v1.069zm7.346-9.68L19 3.518l-1.384-1.753-1.777 1.394A9 9 0 0 0 10 1v2.249a6.73 6.73 0 0 1 4.016 1.338l-1.879 1.472A4.45 4.45 0 0 0 10 5.497v2.249L7.75 9.51v-8.5l-4.5 4.497H1v9.32l1.395 1.755zM7.75 19v-3.789l-2.126 1.664z"/><path d="M16.514 8.32c.146.539.236 1.101.236 1.686 0 3.721-3.026 6.745-6.75 6.745V19c4.973 0 9-4.025 9-8.994a8.7 8.7 0 0 0-.608-3.17l-1.878 1.472z"/></svg>',
  fs: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M16.188 12.25v3.938H12.25V19H19v-6.75zM7.75 16.188H3.813V12.25H1V19h6.75zM3.813 7.75V3.813H7.75V1H1v6.75zm8.437-3.937h3.938V7.75H19V1h-6.75z"/></svg>'
};
function fmtDur(sec, incH) {
  // kick's formatVideoDuration: [HH if ≥1h]+[MM]+[SS], 0-padded
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600);
  const mm = Math.floor((sec % 3600) / 60);
  const ss = sec % 60;
  const p = (n) => String(n).padStart(2, '0');
  const out = p(mm) + ':' + p(ss);
  return incH || hh > 0 ? p(hh) + ':' + out : out;
}
function setKickFill(frac) {
  if (!KO.wrap) return;
  const fillEl = KO.wrap.querySelector('#ko-fill');
  const thumbEl = KO.wrap.querySelector('#ko-thumb');
  if (fillEl) fillEl.style.width = (Math.max(0, Math.min(1, frac)) * 100) + '%';
  if (thumbEl) thumbEl.style.left = (Math.max(0, Math.min(1, frac)) * 100) + '%';
}
function updateKickVolUI() {
  if (!KO.wrap) return;
  const v = KO.kickMuted || KO.kickVol === 0 ? 0 : KO.kickVol;
  const fillEl = KO.wrap.querySelector('#ko-volfill');
  const thumbEl = KO.wrap.querySelector('#ko-volthumb');
  const muteBtn = KO.wrap.querySelector('#ko-mute');
  if (fillEl) fillEl.style.width = (v * 100) + '%';
  if (thumbEl) thumbEl.style.left = (v * 100) + '%';
  if (muteBtn) muteBtn.innerHTML = v === 0 ? KO_SVG.muted : KO_SVG.sound;
}

function mount() {
  if (KO.wrap) return;
  const wrap = document.createElement('div');
  wrap.id = 'ko-wrap';
  wrap.style.display = 'none';
  wrap.classList.add('ko-kick');
  const bar = document.createElement('div');
  bar.id = 'ko-bar';
  bar.innerHTML =
    '<div id="ko-g1" class="ko-g">' +
    '<button id="ko-play" class="ko-icn" title="Play/Pause"></button>' +
    '<div id="ko-volwrap" class="ko-volwrap">' +
    '<button id="ko-mute" class="ko-icn" title="Mute"></button>' +
    '<div id="ko-vols"><div id="ko-voltrack"><div id="ko-volfill"></div><div id="ko-volthumb"></div></div></div>' +
    '</div>' +
    '<span id="ko-cur" class="ko-t"></span>' +
    '<span class="ko-t ko-sep">/</span>' +
    '<span id="ko-total" class="ko-t"></span>' +
    '</div>' +
    '<div id="ko-g2" class="ko-g"><button id="ko-fs" class="ko-icn" title="Fullscreen"></button></div>' +
    '<div id="ko-seekbar">' +
    '<span id="ko-hov" class="ko-hov"></span>' +
    '<div id="ko-loaded" class="ko-prog"></div>' +
    '<div id="ko-loadind" class="ko-prog"></div>' +
    '<div id="ko-track"><div id="ko-fill"></div></div>' +
    '<div id="ko-thumb"></div>' +
    '</div>';
  wrap.appendChild(bar);
  const top = document.createElement('div');
  top.id = 'ko-top';
  top.innerHTML =
    '<button id="ko-livebadge" class="ko-livebadge"><span id="ko-live-dot" class="ko-live-dot"></span>' +
    '<span id="ko-live-txt" class="ko-live-txt">LIVE</span></button>' +
    '<span id="ko-elapsed" class="ko-elapsed"></span>';
  wrap.appendChild(top);
  const rc = document.createElement('div');
  rc.id = 'ko-reconnecting';
  rc.textContent = 'RECONNECTING\u2026';
  rc.style.display = 'none';
  wrap.appendChild(rc);
  overlayAnchor().appendChild(wrap);
  KO.wrap = wrap;
  KO.hideTicks = 0;

  const play = bar.querySelector('#ko-play');
  play.innerHTML = KO_SVG.play;
  play.addEventListener('click', () => {
    const playing = KO.kickState && KO.kickState.state === 'Playing';
    kickSend(playing ? { t: 'pause' } : { t: 'play' });
  });
  const muteBtn = bar.querySelector('#ko-mute');
  muteBtn.innerHTML = KO_SVG.sound;
  muteBtn.addEventListener('click', () => {
    KO.kickMuted = !KO.kickMuted;
    if (!KO.kickMuted && KO.kickVol === 0) KO.kickVol = 1;
    kickSend({ t: 'mute', m: KO.kickMuted });
    if (!KO.kickMuted) kickSend({ t: 'volume', v: KO.kickVol });
    updateKickVolUI();
    saveState(); // remember the user's kick mute choice
  });
  const volwrap = bar.querySelector('#ko-volwrap');
  const volTrack = bar.querySelector('#ko-voltrack');
  const volFrac = (e) => {
    const r = volTrack.getBoundingClientRect();
    return r.width ? Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) : 0;
  };
  volwrap.addEventListener('pointerdown', (e) => {
    if (e.target === muteBtn || e.target.closest('#ko-mute')) return;
    e.preventDefault();
    const v = volFrac(e);
    KO.kickVol = v;
    KO.kickMuted = v === 0;
    kickSend({ t: 'volume', v: KO.kickVol });
    kickSend({ t: 'mute', m: KO.kickMuted });
    updateKickVolUI();
  });
  volwrap.addEventListener('pointerup', () => saveState());
  const sb = bar.querySelector('#ko-seekbar');
  const hov = bar.querySelector('#ko-hov');
  let dragging = false;
  const fracFromEvent = (e) => {
    const r = sb.getBoundingClientRect();
    return r.width ? Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) : 0;
  };
  const showHover = (frac) => {
    const r = sb.getBoundingClientRect();
    const pos = frac * KO.kickDur;
    const dur = KO.kickDur || 1;
    hov.textContent = KO.kickOnDvr
      ? fmtDur(pos, dur >= 3600)
      : pos >= dur - 0.01 * dur
        ? 'LIVE'
        : '-' + fmtDur(dur - pos, dur >= 3600);
    hov.style.left = Math.max(14, Math.min(r.width - 14, frac * r.width)) + 'px';
    hov.style.display = 'block';
  };
  sb.addEventListener('pointermove', (e) => {
    if (dragging) {
      setKickFill(fracFromEvent(e));
      KO.seeking = true;
      return;
    }
    showHover(fracFromEvent(e));
  });
  sb.addEventListener('pointerleave', () => {
    if (!dragging) hov.style.display = 'none';
  });
  sb.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    dragging = true;
    KO.seeking = true;
    sb.classList.add('ko-drag');
    try {
      sb.setPointerCapture(e.pointerId);
    } catch {
      /* capture unsupported */
    }
    setKickFill(fracFromEvent(e));
    showHover(fracFromEvent(e));
  });
  const endDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    KO.seeking = false;
    sb.classList.remove('ko-drag');
    hov.style.display = 'none';
    kickSeekTo(fracFromEvent(e) * KO.kickDur);
  };
  sb.addEventListener('pointerup', endDrag);
  sb.addEventListener('pointercancel', () => {
    dragging = false;
    KO.seeking = false;
    sb.classList.remove('ko-drag');
    hov.style.display = 'none';
  });
  const badge = top.querySelector('#ko-livebadge');
  badge.addEventListener('click', () => {
    if (KO.kickOnDvr) kickBackToLive();
  });
  bar.querySelector('#ko-fs').addEventListener('click', () => {
    if (document.fullscreenElement === KO.wrap) document.exitFullscreen().catch(() => {});
    else if (KO.wrap) KO.wrap.requestFullscreen().catch(() => {});
  });
  startRectLoop();

  let hotTimer = null;
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
  KO.kickEverPlayed = false;
  KO.kickOnDvr = false;
  KO.kickDur = 0;
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
    if (wasHidden) { KO.kickOnDvr = false; kickSend({ t: 'seekToLive' }); }
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

// Kick bar: full-broadcast seek slider (kick-style DVR: rewinds switch to
// the replay url, ≤30s from the edge goes live) + VOLTAR AO VIVO pill when
// behind. Position/latency/duration come from the IVS frame's ~1s messages.
function updateKickBar() {
  if (!KO.wrap || KO.player !== 'kick') return;
  const st = KO.kickState;
  if (!st) return;
  const pos = st.pos || 0;
  const lat = st.lat;
  const liveEdge = isFinite(lat) && lat >= 0 ? pos + lat : null;
  const dur = st.dur > 0 ? st.dur : null;
  // Full-broadcast range when the stream start is known (kick.com's bar is
  // the whole broadcast, playhead riding the edge); window fallback before
  // the first DVR fetch. The max GROWS as the stream runs (wall-clock).
  // Cap at 48h and require finite values — poisoned/NaN player readings
  // (a bad seek, getLiveLatency() quirks) must never reach the bar.
  let max = dur || 0;
  if (Number.isFinite(KO.kickStreamStart) && KO.kickDvrFetchedAt) {
    const elapsed = (Date.now() - KO.kickStreamStart) / 1000;
    if (elapsed > max) max = elapsed;
  }
  if (!(max > 0)) max = Math.ceil(liveEdge || pos || 1);
  if (!isFinite(max) || max > 172800) max = 172800; // 48h ceiling
  KO.kickDur = max;
  const head = KO.kickOnDvr ? pos : Math.max(0, max - (isFinite(lat) && lat >= 0 ? lat : 0));
  if (!KO.seeking) setKickFill(max > 0 ? head / max : 0);
  // Loaded progress (kick: bg-white/50 full + bg-white/30 indicator, scaleX).
  // IVS reports no buffered range; live is always "loaded" to the edge, DVR
  // gets a short lookahead.
  const loadedSec = KO.kickOnDvr ? Math.min(max, pos + 5) : max;
  const lf = max > 0 ? loadedSec / max : 1;
  const loaded = KO.wrap.querySelector('#ko-loaded');
  const loadind = KO.wrap.querySelector('#ko-loadind');
  if (loaded) loaded.style.transform = 'scaleX(' + lf + ')';
  if (loadind) loadind.style.transform = 'scaleX(' + lf + ')';
  const cur = KO.wrap.querySelector('#ko-cur');
  const total = KO.wrap.querySelector('#ko-total');
  if (cur) cur.textContent = fmtDur(head, max >= 3600);
  if (total) total.textContent = fmtDur(max, max >= 3600);
  // Top-left: LIVE badge (green dot + "LIVE") or the clickable
  // "Voltar ao vivo" while replaying (kick's Status button); the live
  // elapsed ticks next to it.
  const dot = KO.wrap.querySelector('#ko-live-dot');
  const btxt = KO.wrap.querySelector('#ko-live-txt');
  const badge = KO.wrap.querySelector('#ko-livebadge');
  const elapsed = KO.wrap.querySelector('#ko-elapsed');
  if (KO.kickOnDvr) {
    if (dot) dot.style.background = '#3f4448';
    if (btxt) btxt.textContent = 'Voltar ao vivo';
    if (badge) badge.style.cursor = 'pointer';
    if (elapsed) elapsed.textContent = '';
  } else {
    if (dot) dot.style.background = '#53fc18';
    if (btxt) btxt.textContent = 'LIVE';
    if (badge) badge.style.cursor = 'default';
    if (elapsed && Number.isFinite(KO.kickStreamStart)) elapsed.textContent = fmtDur((Date.now() - KO.kickStreamStart) / 1000, max >= 3600);
  }
  const playBtn = KO.wrap.querySelector('#ko-play');
  if (playBtn) playBtn.innerHTML = st.state === 'Playing' ? KO_SVG.pause : KO_SVG.play;
  updateKickVolUI();
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
    return;
  }

  if (KO.player === 'kick') {
    // Dead-url watchdog: the frame loaded a url but never reached Playing
    // within 15s. Kick's v2 API hands out a stale top-level playback_url for
    // offline channels; without this the layer would sit black over Twitch
    // (the user's nyro report). Playing-then-frozen is the stall watchdog's
    // job (reconnect with a FRESH url) — this one covers never-played.
    if (
      KO.activeUrl &&
      !KO.kickEverPlayed &&
      KO.kickState &&
      KO.kickState.state !== 'Playing' &&
      KO.kickUrlT &&
      Date.now() - KO.kickUrlT > 15000
    ) {
      console.log('[ko] kick url never played — teardown', KO.kickSlug, KO.kickState.state);
      diag('kick_stall', { slug: KO.kickSlug, state: KO.kickState.state, ct: KO.kickState.ct });
      teardown();
      if (KO.wrap) hideWrap();
      setBadge('KICK OFF', '#6b7280');
      return;
    }
    // Already playing on a live url? Keep it — IVS playback_urls rotate on
    // every API call and a re-attach would reset the stream to the live edge.
    // The stall watchdog reconnects with a FRESH url when the current one
    // goes stale (8s frozen). This branch is network-free → the KICK button
    // switch is instant.
    if (KO.activeUrl && KO.kickState && KO.kickState.state === 'Playing') {
      showKickLayer();
      setBadge('KICK', '#059669');
      return;
    }
    // Transition grace: a url younger than 25s is still loading (fresh live
    // loads after a DVR replay take ~10s to first frames; the DVR→live
    // switch must NOT be interrupted by a mid-load url rotation — that was
    // the "reconnecting" loop: every 20s poll fetched a NEW playback_url
    // and restarted the load). Failure recovery is the watchdogs' job:
    // never-played (>15s) tears down, stuck-not-playing (>25s, below)
    // reconnects on budget, frozen-while-playing (>8s) reconnects on budget.
    if (KO.activeUrl && KO.kickUrlT && Date.now() - KO.kickUrlT < 25000) {
      showKickLayer();
      setBadge('KICK', '#059669');
      return;
    }
    // Stuck not-playing watchdog: the url loaded but the player never left
    // Idle/Ready/Buffering (e.g. a live reload after the replay that IVS
    // won't start). Budgeted like any reconnect — a stable Playing resets
    // the budget, so real transient failures self-heal without looping.
    if (KO.activeUrl && KO.kickState && KO.kickState.state !== 'Playing' && KO.reconnectCount < MAX_RECONNECT) {
      console.log('[ko] kick not-playing for 25s — reconnecting', KO.kickState.state);
      diag('kick_stuck', { state: KO.kickState.state, ct: KO.kickState.ct });
      reconnect();
      return;
    }
    const k = await kickPlaybackUrl(KO.kickSlug);
    diag('kick_probe', { slug: KO.kickSlug, live: k.live, url: k.url ? 'yes' : 'no' });
    if (k.live && k.url) {
      KO.kickStreamId = k.streamId || null;
      ensureKick(k.url);
      showKickLayer();
      setBadge('KICK', '#059669');
      return;
    }
    console.log('[ko] kick offline or unreachable', KO.kickSlug, JSON.stringify(k));
    diag('kick_offline', { slug: KO.kickSlug, live: k.live, url: k.url ? 'yes' : 'no' });
    setBadge('KICK OFF', '#6b7280');
    if (KO.wrap) hideWrap();
    return;
  }

  // youtube mode
  if (!KO.ytRaw) {
    setBadge('YT?', '#6b7280'); // no mapping — map the channel in the popup
    if (KO.wrap) hideWrap();
    return;
  }
  await ensureYtId();
  if (!KO.ytId) {
    setBadge('YT?', '#6b7280'); // could not resolve handle → check the popup value
    if (KO.wrap) hideWrap();
    return;
  }
  ensureYtIframe();
  if (KO.ytState.live) {
    showYtLayer();
    setBadge('YT', '#ff0000');
    return;
  }
  setBadge('YT OFF', '#6b7280');
  if (KO.wrap) hideWrap();
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
        KO.lastRect = tv.getBoundingClientRect();
        const r = KO.lastRect;
        const s = KO.wrap.style;
        s.left = `${r.left}px`;
        s.top = `${r.top}px`;
        s.width = `${r.width}px`;
        s.height = `${r.height}px`;
        if (KO.wrap.style.display === 'none') showWrap();
      } else if (KO.twDeleted) {
        // User deleted the Twitch player from the popup: keep the overlay
        // pinned at its last rect (the whole point is seeing the overlay
        // WITHOUT the Twitch player underneath).
        KO.hideTicks = 0;
        if (KO.wrap.style.display === 'none') showWrap();
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
    'display:flex;flex-direction:row;align-items:center;justify-content:space-between;padding:0 10px;color:#fff;' +
    'background:linear-gradient(0deg,rgba(0,0,0,.8),rgba(0,0,0,0));' +
    'font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}' +
    '#ko-wrap.ko-yt #ko-bar{display:none;}' + // YT has native controls incl. LIVE chip
    '#ko-wrap.ko-hot #ko-bar{opacity:1;}' +
    '#ko-reconnecting{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;' +
    'background:rgba(0,0,0,.82);color:#fff;font:700 15px system-ui,sans-serif;letter-spacing:.04em;pointer-events:none;}' +
    '.ko-g{display:flex;flex-direction:row;align-items:center;}' +
    '#ko-g1{gap:2px;}' +
    '.ko-icn{background:transparent;border:0;color:#fff;cursor:pointer;display:flex;align-items:center;' +
    'justify-content:center;width:40px;height:40px;border-radius:8px;padding:0;}' +
    '.ko-icn:hover{background:rgba(255,255,255,.1);}' +
    '.ko-icn svg{width:22px;height:22px;display:block;}' +
    '.ko-t{font-size:12px;font-weight:700;white-space:nowrap;color:#fff;font-variant-numeric:tabular-nums;}' +
    '#ko-g1 .ko-t{padding:0 6px;}' +
    '.ko-sep{opacity:.6;}' +
    '#ko-volwrap{position:relative;display:flex;align-items:center;height:40px;}' +
    '#ko-vols{display:none;position:absolute;left:100%;top:0;height:100%;align-items:center;' +
    'width:100px;padding:0 4px;cursor:pointer;}' +
    '#ko-volwrap:hover #ko-vols{display:flex;}' +
    '#ko-voltrack{position:relative;width:100%;height:3px;border-radius:999px;background:#24272c;}' +
    '#ko-volfill{position:absolute;left:0;top:0;bottom:0;border-radius:999px;background:#fff;}' +
    '#ko-volthumb{position:absolute;top:50%;width:16px;height:16px;border-radius:10px;background:#fff;' +
    'transform:translate(-50%,-50%);box-shadow:0 1px 3px rgba(0,0,0,.4);}' +
    '#ko-seekbar{position:absolute;top:-28px;left:0;right:0;height:20px;cursor:pointer;touch-action:none;}' +
    '#ko-track{position:absolute;left:0;right:0;bottom:0;height:4px;border-radius:2px;background:rgba(146,158,166,.5);}' +
    '#ko-seekbar:hover #ko-track{height:6px;bottom:-1px;}' +
    '#ko-fill{position:absolute;left:0;top:0;bottom:0;background:#53fc18;border-radius:2px;}' +
    '.ko-prog{position:absolute;left:0;right:0;bottom:0;height:4px;transform-origin:left;}' +
    '#ko-loaded{background:rgba(255,255,255,.5);}' +
    '#ko-loadind{background:rgba(255,255,255,.3);}' +
    '#ko-thumb{position:absolute;bottom:-6px;width:16px;height:16px;border-radius:50%;background:#53fc18;' +
    'transform:translateX(-50%);display:none;}' +
    '#ko-seekbar:hover #ko-thumb,#ko-seekbar.ko-drag #ko-thumb{display:block;}' +
    '#ko-hov{position:absolute;bottom:26px;left:0;transform:translateX(-50%);background:rgba(0,0,0,.78);' +
    'border-radius:6px;padding:4px 6px;font-size:12px;font-weight:700;color:#fff;' +
    'font-variant-numeric:tabular-nums;white-space:nowrap;display:none;pointer-events:none;z-index:2;}' +
    '#ko-top{position:absolute;top:0;left:0;right:0;pointer-events:none;display:flex;flex-direction:row;' +
    'align-items:center;gap:12px;padding:10px 14px;color:#fff;' +
    'background:linear-gradient(180deg,rgba(0,0,0,.7),rgba(0,0,0,0));' +
    'font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}' +
    '#ko-livebadge{pointer-events:auto;display:flex;flex-direction:row;align-items:center;gap:6px;' +
    'background:transparent;border:0;color:#fff;cursor:default;padding:0;}' +
    '#ko-live-dot{width:10px;height:10px;border-radius:50%;background:#53fc18;}' +
    '#ko-live-txt{font-size:14px;font-weight:600;white-space:nowrap;}' +
    '#ko-elapsed{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;}' +
  '  (document.head || document.documentElement).appendChild(st);'
}

// ---- popup actions ----------------------------------------------------------
// ko-delete-twitch: remove the native Twitch player element so the user can
// SEE that the visible video is the overlay's, not Twitch's. Restored on
// page reload (Twitch rebuilds its player). The overlay stays pinned at the
// last player rect.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'ko-delete-twitch') {
    KO.twDeleted = true;
    const v = twitchVideo();
    if (v) {
      try {
        v.pause();
      } catch {
        /* already gone */
      }
      try {
        v.remove();
      } catch {
        /* already gone */
      }
    }
    diag('tw_delete', {});
    sendResponse({ ok: true });
  }
});

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
  // URL-param setup (?koyt=<yt channel>&kokick=<slug>&koplayer=<player>):
  // write this channel's mapping by opening a twitch URL — no popup clicks
  // needed (used to pre-configure channels, e.g. jb_sniper→JBSniperPRIME).
  const qp = new URLSearchParams(location.search);
  const qYt = qp.get('koyt');
  const qKick = qp.get('kokick');
  const qPlayer = qp.get('koplayer');
  const setupSlug = currentSlug(); // KO.slug is still null at init
  if (setupSlug && (qYt || qKick || qPlayer)) {
    const m = KO.mappings[setupSlug];
    const base = m && typeof m === 'object' ? { ...m } : { kick: setupSlug };
    if (qKick) base.kick = qKick.toLowerCase();
    if (qYt) {
      base.yt = qYt;
      delete base.ytId;
    }
    if (qYt || qKick) {
      KO.mappings[setupSlug] = base;
      KO.kickSlug = (qKick || base.kick || setupSlug).toLowerCase();
      KO.ytRaw = qYt || base.yt || '';
      KO.ytId = null;
    }
    if (qPlayer && ['kick', 'youtube', 'twitch'].includes(qPlayer)) KO.player = qPlayer;
    saveState().then(() => apply());
  }
  // Persist the resolved defaults (enabled: true) so the popup's toggle
  // always agrees with the content script.
  if (Object.keys(KO.mappings).length === 0) {
    await saveState();
  }
  injectStyles();
  startWatchers();
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
      bar: KO.wrap
        ? {
            mode: KO.kickOnDvr ? 'dvr' : 'live',
            cur: (KO.wrap.querySelector('#ko-cur') || {}).textContent || '',
            total: (KO.wrap.querySelector('#ko-total') || {}).textContent || '',
            fillPct: Math.round(parseFloat((KO.wrap.querySelector('#ko-fill') || {}).style?.width || '0')),
            badge: (KO.wrap.querySelector('#ko-live-txt') || {}).textContent || '',
            vol: Math.round(((KO.kickMuted || KO.kickVol === 0) ? 0 : KO.kickVol) * 100),
            dur: Math.round(KO.kickDur || 0),
            hot: KO.wrap.classList.contains('ko-hot'),
          }
        : null,
    });
  }, 8000);
  apply();
})();
