// Kick Overlay — content script (runs on https://www.twitch.tv/*).
//
// Runs ONLY on https://www.twitch.tv/* (see manifest). It overlays the
// streamer's Kick or YouTube stream onto the Twitch player page; kick.com
// and youtube.com themselves are not modified by this content script.
//
// While enabled and the streamer is live on Kick or YouTube too, overlays the
// streamer's REAL Kick or YouTube stream over the Twitch player. The native
// Twitch player is paused and muted while either overlay player is active.
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
const YT_EMBED_GRACE = 8000; // yt embed must init (onReady) within this or fall back to kick
const SPA_MS = 900;      // Twitch SPA pathname poll (no reload on channel nav)
const HIDE_TICKS = 3;    // consecutive ticks without a Twitch player before hiding
const MAX_RECONNECT = 3; // kick fatal retries (fresh playback_url each time)

// Diagnostics stay inside the extension's service-worker console. Only the
// event name is logged there; payloads are intentionally not forwarded.
function diag(ev, data) {
  try {
    chrome.runtime.sendMessage({ __koDiag: { ev, data: data || {} } }, () => void chrome.runtime.lastError);
  } catch {
    /* beacon must never break the overlay */
  }
}

// Fire-and-forget async entry points (probe/apply/reconnect/init) must
// never reject into the console or the chrome://extensions error badge.
// Any failure becomes a diag event instead.
function fire(fn) {
  try {
    const p = fn();
    if (p && typeof p.catch === 'function') {
      p.catch((e) => diag('guard', { err: String((e && e.message) || e).slice(0, 120) }));
    }
  } catch (e) {
    diag('guard', { err: String((e && e.message) || e).slice(0, 120) });
  }
}

// Twitch routes that look like /<slug> but are not channels.
const NOT_CHANNEL = new Set([
  'directory', 'downloads', 'friends', 'gift', 'jobs', 'login', 'notifications',
  'p', 'prime', 'settings', 'subscriptions', 'turbo', 'events', 'search',
  'wallet', 'moderation', 'popout', 'videos', 'clips', 'team', 'tags',
]);

// Build marker for the diag stream — lets us tell which code a tab runs
// (content scripts of pre-reload tabs survive extension reloads).
const KO_VER = '0.8.0';
const KO = {
  playerPreference: 'kick',
  enabled: false,
  player: 'kick', // 'kick' | 'youtube' | 'twitch' — switching never rebuilds players
  mappings: {},   // twitchSlug -> kickSlug (string) | {kick, yt, ytId}
  slug: null,
  kickSlug: null,
  ytRaw: '',
  ytId: null,
  activeUrl: null, // kick playback_url currently loaded in the frame
  wrap: null,      // overlay container — persists across switches
  statusChip: null, // #ko-status pill — a SIBLING of the wrap (survives hideWrap)
  kickFrame: null, // <iframe src=player.html> (IVS engine, same as kick.com)
  kickWin: null,   // kickFrame.contentWindow (set on first ready message)
  kickReady: false,
  kickState: null, // last {state, paused, muted, volume, pos, lat, q, qcount} from the frame
  lastKickSt: 0,   // epoch ms of the last st message (frame-death watchdog)
  lastYtSt: 0,     // epoch ms of the last yt st message (yt frame-death watchdog)
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
  ytVol: 1,         // user's YouTube volume in the player bridge's 0..1 range
  ytMuted: false,   // user's YouTube mute choice (persisted)
  ytState: { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 },
  ytHlsUrl: null,   // last minted HLS manifest url (refreshed under the background cache TTL)
  ytHlsAt: 0,       // epoch ms of the successful mint
  ytHlsFailed: false, // last mint (or a fatal frame error) failed — fall back to kick
  ytHlsFailedAt: 0,   // epoch ms of the failure (retry backoff)
  ytLoadedUrl: null, // url currently handed to the yt frame (re-load on change)
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
  if (typeof kv.v === 'number') KO.kickVol = Math.max(0, Math.min(1, kv.v));
  if (typeof kv.m === 'boolean') KO.kickMuted = kv.m;
  if (typeof yv.v === 'number') {
    const volume = yv.v > 1 ? yv.v / 100 : yv.v;
    KO.ytVol = Math.max(0, Math.min(1, volume));
  }
  if (typeof yv.m === 'boolean') KO.ytMuted = yv.m;
}

function loadState() {
  return new Promise((res) => {
    chrome.storage.local.get(KEY, (o) => {
      void chrome.runtime.lastError;
      const s = (o && o[KEY]) || {};
      KO.enabled = s.enabled === undefined ? true : !!s.enabled;
      KO.player = s.player === 'twitch' ? 'twitch' : s.player === 'youtube' ? 'youtube' : 'kick';
      KO.playerPreference = KO.player;
      KO.mappings = s.mappings && typeof s.mappings === 'object' ? s.mappings : {};
      applyVols(s.vols);
      res();
    });
  });
}

function saveState(persistPlayer = false) {
  return new Promise((res) => {
    if (persistPlayer) KO.playerPreference = KO.player;
    chrome.storage.local.set(
      {
        [KEY]: {
          enabled: KO.enabled,
          mappings: KO.mappings,
          // The YouTube fallback changes KO.player for this session only.
          player: KO.playerPreference,
          vols: {
            kick: { v: KO.kickVol, m: KO.kickMuted },
            yt: { v: KO.ytVol, m: KO.ytMuted },
          },
        },
      },
      () => {
        void chrome.runtime.lastError;
        res();
      },
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
  const nextPlayer = s.player === 'twitch' ? 'twitch' : s.player === 'youtube' ? 'youtube' : 'kick';
  // A volume/mapping write during the session-only fallback carries the
  // preferred player back to storage; do not let that self-write undo the
  // active fallback. A different incoming player is a real popup switch.
  if (KO.player === KO.playerPreference || nextPlayer !== KO.playerPreference) KO.player = nextPlayer;
  KO.playerPreference = nextPlayer;
  KO.mappings = s.mappings && typeof s.mappings === 'object' ? s.mappings : {};
  applyVols(s.vols);
  if (pChanged) fire(apply); // hot toggle / remap / player switch — no page reload
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

// Badge texts are clipped by Chrome at 4 chars — every badge is one of the
// normalized <=4-char tokens below, with the PHASE carried by the color:
//   KICK green = Playing | KICK amber = connecting (url/ready pending)
//   KICK gray  = failed/offline | YT red = live | YT amber = minting/loading
//   YT gray    = failed | TW/OFF gray | '' clear.
// The #ko-status pill is driven from the SAME call (single choke point) so
// the chip and the badge can never disagree.
const CHIP_STATES = {
  'KICK|#059669': ['KICK', '#53fc18'],
  'KICK|#d97706': ['KICK…', '#fbbf24'],
  'KICK|#6b7280': ['KICK ✕', '#f87171'],
  'YT|#ff0000': ['YT', '#ff0000'],
  'YT|#d97706': ['YT…', '#fbbf24'],
  'YT|#6b7280': ['YT ✕', '#f87171'],
};

// Pin the chip to the top-right of the player rect; lastRect keeps the pin
// after ko-delete-twitch (viewport-primed when no rect was ever measured).
function positionStatusChip() {
  const c = KO.statusChip;
  if (!c || c.style.display === 'none') return;
  const r = twitchAnchorRect() || KO.lastRect || { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
  const right = typeof r.right === 'number' ? r.right : r.left + r.width;
  c.style.left = `${Math.max(0, right - (c.offsetWidth || 0) - 8)}px`;
  c.style.top = `${r.top + 8}px`;
}

function updateStatusChip(text, color) {
  const c = KO.statusChip;
  if (!c) return;
  const st = CHIP_STATES[text + '|' + color];
  // Chip only exists for mapped channels with an overlay player active —
  // twitch mode / unmapped / disabled pages get no pill.
  if (!st || !KO.enabled || !KO.slug || !KO.mappings[KO.slug] || KO.player === 'twitch') {
    if (c.style.display !== 'none') c.style.display = 'none';
    return;
  }
  c.textContent = st[0];
  c.style.color = st[1];
  if (c.style.display === 'none') c.style.display = 'block';
  positionStatusChip();
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeBackgroundColor({ color: color || [0, 0, 0, 0] });
    chrome.action.setBadgeText({ text: text || '' });
  } catch {
    /* badge is cosmetic */
  }
  updateStatusChip(text, color);
}

// ---- YouTube bridge ---------------------------------------------------------

// YouTube layer — the extension's OWN HLS player (player.html?m=hls). The
// live_stream embed cannot initialize on bot-gated/cookieless sessions: the
// server bakes an error config ("Erro 153") into the page and the player
// never issues an innertube request, so no pot in the URL can fix it. The
// background mints the stream via an anonymous MWEB player API call (the
// yt-dlp recipe) and the HLS frame speaks the same __koKick protocol as IVS.
const PLAYER_ORIGIN = new URL(chrome.runtime.getURL('player.html')).origin;

function ytSend(o) {
  const f = document.getElementById('ko-yt');
  const token = f && f.dataset.koToken;
  if (!f || !f.contentWindow || !token) return;
  try {
    f.contentWindow.postMessage({ __koKick: { ...o, _koToken: token } }, PLAYER_ORIGIN);
  } catch {
    /* frame gone */
  }
}

function ytCmd(cmd, arg) {
  switch (cmd) {
    case 'play': return ytSend({ t: 'play' });
    case 'pause': return ytSend({ t: 'pause' });
    case 'mute': return ytSend({ t: 'mute', m: arg === undefined ? true : !!arg });
    case 'unmute': return ytSend({ t: 'mute', m: false });
    case 'setVolume': return ytSend({ t: 'volume', v: arg });
    case 'seekToLive': return ytSend({ t: 'seekToLive' });
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

// Mint the live HLS manifest through the background (MWEB player API call —
// Chrome transport, anonymous). The background caches the URL per video id
// for 5 min; content refreshes under that TTL so a re-mint re-resolves the
// live video id (stream restarts) without hammering innertube, and failures
// retry after a short backoff. The old one-shot flags stuck forever — a
// single failed mint ("sts not found", SW cold start, transient network)
// killed the yt layer for the whole page session (observed in the diag log:
// yt_hls err -> yt_fallback, then never retried).
const YT_HLS_TTL_MS = 4 * 60 * 1000; // refresh before the background's 5-min cache expires
const YT_HLS_RETRY_MS = 30 * 1000;   // hard-fail backoff (transient bot-gate/network)
async function ensureYtHls() {
  const now = Date.now();
  if (KO.ytHlsUrl && now - (KO.ytHlsAt || 0) < YT_HLS_TTL_MS) return; // fresh mint
  if (KO.ytHlsFailed && now - (KO.ytHlsFailedAt || 0) < YT_HLS_RETRY_MS) return; // backoff
  try {
    const r = await new Promise((res) => {
      try {
        chrome.runtime.sendMessage({ type: 'ko-yt-play', channelRef: KO.ytId || KO.ytRaw }, (rr) => res(rr || {}));
      } catch {
        res({});
      }
    });
    if (r && r.url) {
      KO.ytHlsUrl = r.url;
      KO.ytHlsAt = now;
      KO.ytHlsFailed = false;
      diag('yt_hls', { url: r.url.slice(0, 60) });
    } else {
      KO.ytHlsFailed = true;
      KO.ytHlsFailedAt = now;
      diag('yt_hls', { err: (r && r.error) || 'no-url' });
    }
  } catch (e) {
    KO.ytHlsFailed = true;
    KO.ytHlsFailedAt = now;
    diag('yt_hls', { err: String(e).slice(0, 120) });
  }
}

function ensureYtIframe() {
  if (document.getElementById('ko-yt')) return;
  if (!KO.wrap) mount();
  if (!KO.ytHlsUrl) return;
  const iframe = document.createElement('iframe');
  iframe.id = 'ko-yt';
  const token = crypto.randomUUID();
  iframe.dataset.koToken = token;
  iframe.src = chrome.runtime.getURL(`player.html?m=hls&token=${encodeURIComponent(token)}`);
  iframe.setAttribute('allow', 'autoplay; fullscreen; encrypted-media');
  iframe.allowFullscreen = true;
  KO.wrap.appendChild(iframe);
  KO.pendingYtUrl = KO.ytHlsUrl;
  KO.ytEmbedAt = Date.now();
  diag('yt_embed', { id: KO.ytId.slice(0, 8), hls: !!KO.ytHlsUrl });
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
  const d = ev.data;
  if (!d || !d.__koKick || ev.origin !== PLAYER_ORIGIN) return;
  const f = document.getElementById('ko-yt');
  if (!f || ev.source !== f.contentWindow || d.__koKick._koToken !== f.dataset.koToken) return; // the yt (hls) frame only
  const m = d.__koKick;
  if (m.t === 'ready') {
    KO.ytWin = ev.source;
    KO.ytState.ready = true;
    if (!ytFirstInfo) {
      ytFirstInfo = true;
      diag('yt_ready', {});
    }
    ytCmd('setVolume', KO.ytVol); // restore the user's last yt volume on a fresh frame
    if (KO.pendingYtUrl) {
      ytSend({ t: 'load', url: KO.pendingYtUrl });
      KO.ytLoadedUrl = KO.pendingYtUrl;
      KO.pendingYtUrl = null;
    }
    return;
  }
  if (m.t === 'st' && m.st) {
    KO.lastYtSt = Date.now(); // yt frame-death watchdog clock
    const st = m.st;
    const prevLive = KO.ytState.live;
    // hls.js state mapping — a paused-but-loaded video is still live.
    KO.ytState.playing = st.state === 'Playing';
    KO.ytState.muted = !!st.muted;
    KO.ytState.live = st.state === 'Playing' || st.state === 'Buffering' || st.state === 'Paused';
    KO.ytState.dur = st.dur || 0;
    KO.ytState.ct = st.pos || 0;
    KO.ytState.vq = (st.q && st.q.name) || '';
    KO.ytState.qualities = Array.isArray(st.qualities) ? st.qualities : [];
    updatePlayUI();
    updateKickVolUI();
    renderQualityMenu();
    if (typeof st.volume === 'number' && st.volume >= 0 && !st.muted && KO.ytVol !== st.volume) {
      KO.ytVol = st.volume; // remember the user's yt volume slider
      saveState();
    }
    if (KO.player === 'youtube') throttledYtProbe();
    if (prevLive && !KO.ytState.live && KO.player === 'youtube') throttledYtProbe();
    if (KO.player === 'youtube' && KO.ytState.live && KO.ytState.muted) enableYtUnlock();
    return;
  }
  if (m.t === 'ev' && m.e === 'error') {
    console.log('[ko] yt (hls) error', m.d || '');
    diag('yt_hls_err', { msg: String(m.d || '').slice(0, 140) });
    // The frame already did its one reload per url (player-bridge) — a
    // second fatal error means the URL is dead. Mark the layer failed so
    // the next probe falls back to kick (never leave the native Twitch
    // player with ads visible) and re-mints after the backoff.
    KO.ytHlsFailed = true;
    KO.ytHlsFailedAt = Date.now();
    throttledYtProbe();
  }
});

function throttledYtProbe() {
  const now = Date.now();
  if (now - lastYtProbe < 1000) return;
  lastYtProbe = now;
  fire(probe);
}

// ---- Kick frame bridge (IVS — the engine kick.com uses) ---------------------

function kickSend(o) {
  const f = KO.kickFrame;
  const token = f && f.dataset.koToken;
  if (!KO.kickWin || !token) return;
  try {
    KO.kickWin.postMessage({ __koKick: { ...o, _koToken: token } }, PLAYER_ORIGIN);
  } catch {
    /* frame gone */
  }
}

function kickFrame() {
  if (KO.kickFrame && KO.kickFrame.isConnected) return KO.kickFrame;
  if (!KO.wrap) mount();
  const fr = document.createElement('iframe');
  const token = crypto.randomUUID();
  fr.id = 'ko-ivs';
  fr.dataset.koToken = token;
  fr.src = chrome.runtime.getURL(`player.html?token=${encodeURIComponent(token)}`);
  fr.setAttribute('allow', 'autoplay; fullscreen');
  fr.allowFullscreen = true;
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
  if (!d || !d.__koKick || ev.origin !== PLAYER_ORIGIN) return;
  const ytf = document.getElementById('ko-yt');
  if (ytf && ev.source === ytf.contentWindow) return; // the yt (hls) frame — handled by the yt listener
  if (!KO.kickFrame || ev.source !== KO.kickFrame.contentWindow) return;
  const m = d.__koKick;
  if (m.t === 'ready') {
    // Only the actual player frame may declare ready — before the first
    // ready message KO.kickWin is null and the source guard below would
    // accept ANY window (page-world spoof would hijack kickSend).
    if (!KO.kickFrame || ev.source !== KO.kickFrame.contentWindow) return;
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
      console.log('[ko] kick (IVS) error', m.d || '');
      diag('ivs_error', { msg: String(m.d || '').slice(0, 140), code: m.code || 0 });
      if (!KO.enabled || KO.player !== 'kick' || !KO.activeUrl) return;
      // DVR errors → go live once (kick-identical: replay failure returns to
      // the edge), never a reconnect storm. Live errors → budgeted reconnect.
      if (KO.kickOnDvr) kickBackToLive();
      else fire(reconnect);
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
  const lat = isFinite(st.lat) && st.lat >= 0 ? st.lat : 0;
  const edge = max - lat; // newest frame actually available
  // kick semantics: a click within 30s of the edge means "go live"; farther
  // back seeks the DVR url, clamped to edge-30s so a mis-click into the
  // latency buffer can't request a future seek.
  const t = Math.min(target, Math.max(0, edge - 30));
  if (edge - t <= 30) {
    kickBackToLive();
    return;
  }
  kickStartDvr(t);
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

// ---- playback ownership -----------------------------------------------------
// Exactly one player may run at a time. When a visible Kick or YouTube layer
// owns the overlay, pause and mute every native Twitch video; resume only a
// Twitch video that this overlay paused when the layer is hidden or removed.
function syncMute() {
  const overlaySelected = KO.player !== 'twitch' && !!KO.wrap
    && (KO.wrap.style.display !== 'none' || KO.twDeleted);
  for (const v of document.querySelectorAll('video')) {
    if (overlaySelected) {
      if (!v.paused) {
        v.pause();
        KO.twWasPlaying = true;
      }
      if (!v.muted) {
        v.muted = true;
        KO.muted.add(v);
      }
    } else if (KO.muted.has(v) && v.muted) {
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

function resumeTwitchIfOurs() {
  if (!KO.twWasPlaying) return;
  KO.twWasPlaying = false;
  const v = twitchVideo();
  if (v) v.play().catch(() => { /* autoplay policy */ });
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

// #ko-status pill — always-visible phase indicator. Appended to overlayAnchor
// as a SIBLING of #ko-wrap (never a wrap child) so it survives hideWrap and
// keeps reporting the failure state when the layer is gone. Created by
// probe() after the enabled check; removed by teardown().
function ensureStatusChip() {
  if (KO.statusChip) return;
  const c = document.createElement('div');
  c.id = 'ko-status';
  c.style.cssText =
    'position:fixed;z-index:6;pointer-events:none;display:none;' +
    'font:700 12px system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
    'background:rgba(0,0,0,.65);border:1px solid rgba(255,255,255,.18);' +
    'border-radius:999px;padding:3px 10px;white-space:nowrap;color:#fff;';
  c.style.display = 'none'; // cssText already carries it; explicit for clarity
  overlayAnchor().appendChild(c);
  KO.statusChip = c;
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
  fs: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M16.188 12.25v3.938H12.25V19H19v-6.75zM7.75 16.188H3.813V12.25H1V19h6.75zM3.813 7.75V3.813H7.75V1H1v6.75zm8.437-3.937h3.938V7.75H19V1h-6.75z"/></svg>',
  settings: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="m8.1 1-.4 2a7 7 0 0 0-1.6.9L4.2 2.8 2.8 4.2l1.1 1.9a7 7 0 0 0-.9 1.6l-2 .4v2l2 .4a7 7 0 0 0 .9 1.6l-1.1 1.9 1.4 1.4 1.9-1.1a7 7 0 0 0 1.6.9l.4 2h2l.4-2a7 7 0 0 0 1.6-.9l1.9 1.1 1.4-1.4-1.1-1.9a7 7 0 0 0 .9-1.6l2-.4v-2l-2-.4a7 7 0 0 0-.9-1.6l1.1-1.9-1.4-1.4-1.9 1.1a7 7 0 0 0-1.6-.9l-.4-2zM9.1 7a3 3 0 1 1 0 6 3 3 0 0 1 0-6"/></svg>',
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
  const isYt = KO.player === 'youtube';
  const muted = isYt ? (KO.ytState.muted || KO.ytVol === 0) : (KO.kickMuted || KO.kickVol === 0);
  const v = muted ? 0 : (isYt ? KO.ytVol : KO.kickVol);
  const fillEl = KO.wrap.querySelector('#ko-volfill');
  const thumbEl = KO.wrap.querySelector('#ko-volthumb');
  const muteBtn = KO.wrap.querySelector('#ko-mute');
  if (fillEl) fillEl.style.width = (v * 100) + '%';
  if (thumbEl) thumbEl.style.left = (v * 100) + '%';
  if (muteBtn) {
    const want = v === 0 ? 'muted' : 'sound';
    if (muteBtn.dataset.st !== want) {
      muteBtn.dataset.st = want;
      muteBtn.innerHTML = want === 'muted' ? KO_SVG.muted : KO_SVG.sound;
    }
  }
}

function updatePlayUI() {
  if (!KO.wrap) return;
  const playing = KO.player === 'youtube'
    ? KO.ytState.playing
    : !!(KO.kickState && KO.kickState.state === 'Playing');
  const playBtn = KO.wrap.querySelector('#ko-play');
  if (!playBtn) return;
  const want = playing ? 'pause' : 'play';
  if (playBtn.dataset.st !== want) {
    playBtn.dataset.st = want;
    playBtn.innerHTML = want === 'pause' ? KO_SVG.pause : KO_SVG.play;
  }
}

function renderQualityMenu() {
  const menu = KO.wrap && KO.wrap.querySelector('#ko-quality-menu');
  if (!menu) return;
  const state = KO.player === 'youtube' ? KO.ytState : KO.kickState;
  const qualities = Array.isArray(state && state.qualities) ? state.qualities : [];
  menu.textContent = '';
  const add = (label, value, selected) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.role = 'menuitemradio';
    button.textContent = label;
    button.setAttribute('aria-checked', String(selected));
    button.dataset.quality = value === 'auto' ? 'auto' : String(value);
    button.addEventListener('click', () => {
      const q = button.dataset.quality;
      const numeric = q === 'auto' ? 'auto' : Number(q);
      const message = { t: 'quality', q: numeric };
      if (KO.player === 'youtube') ytSend(message);
      else kickSend(message);
      menu.style.display = 'none';
    });
    menu.appendChild(button);
  };
  add('Auto', 'auto', !state || !state.q);
  qualities.forEach((quality, id) => {
    const label = quality.name || (quality.h ? `${quality.h}p` : `${quality.w || ''}w`);
    add(label, Number.isInteger(quality.id) ? quality.id : id, !!state && state.q && state.q.name === label);
  });
}

// Track recent pointer activity for diagnostics; controls remain visible.
function setupHotBar(wrap) {
  teardownHotBar();
  KO.hotWrap = wrap;
  const armHot = () => {
    if (!KO.hotWrap) return;
    KO.hotWrap.classList.add('ko-hot');
    clearTimeout(KO.hotTimer);
    KO.hotTimer = setTimeout(() => {
      if (KO.hotWrap) KO.hotWrap.classList.remove('ko-hot');
    }, 2600);
  };
  KO._hotArm = armHot;
  wrap.addEventListener('pointermove', armHot);
  wrap.addEventListener('pointerdown', armHot);
  wrap.addEventListener('mouseenter', armHot);
  armHot();
}

function teardownHotBar() {
  clearTimeout(KO.hotTimer);
  KO.hotTimer = null;
  if (KO.hotWrap && KO._hotArm) {
    KO.hotWrap.removeEventListener('pointermove', KO._hotArm);
    KO.hotWrap.removeEventListener('pointerdown', KO._hotArm);
    KO.hotWrap.removeEventListener('mouseenter', KO._hotArm);
  }
  KO.hotWrap = null;
  KO._hotArm = null;
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
    '<div id="ko-g2" class="ko-g"><div id="ko-quality-wrap"><button id="ko-settings" class="ko-icn" title="Quality" aria-label="Quality"></button><div id="ko-quality-menu" role="menu"></div></div><button id="ko-fs" class="ko-icn" title="Fullscreen" aria-label="Fullscreen"></button></div>' +
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
    const playing = KO.player === 'youtube'
      ? KO.ytState.playing
      : !!(KO.kickState && KO.kickState.state === 'Playing');
    if (KO.player === 'youtube') ytCmd(playing ? 'pause' : 'play');
    else kickSend(playing ? { t: 'pause' } : { t: 'play' });
  });
  const muteBtn = bar.querySelector('#ko-mute');
  muteBtn.innerHTML = KO_SVG.sound;
  const fsBtn = bar.querySelector('#ko-fs');
  fsBtn.innerHTML = KO_SVG.fs;
  const qualityBtn = bar.querySelector('#ko-settings');
  const qualityMenu = bar.querySelector('#ko-quality-menu');
  qualityBtn.innerHTML = KO_SVG.settings;
  qualityBtn.addEventListener('click', () => {
    renderQualityMenu();
    qualityMenu.style.display = qualityMenu.style.display === 'flex' ? 'none' : 'flex';
  });
  muteBtn.addEventListener('click', () => {
    if (KO.player === 'youtube') {
      const muted = !KO.ytState.muted;
      KO.ytMuted = muted;
      if (!muted && KO.ytVol === 0) KO.ytVol = 1;
      ytCmd('mute', muted);
      if (!muted) ytCmd('setVolume', KO.ytVol);
    } else {
      KO.kickMuted = !KO.kickMuted;
      if (!KO.kickMuted && KO.kickVol === 0) KO.kickVol = 1;
      kickSend({ t: 'mute', m: KO.kickMuted });
      if (!KO.kickMuted) kickSend({ t: 'volume', v: KO.kickVol });
    }
    updateKickVolUI();
    saveState(); // remember the user's player mute choice
  });
  const volwrap = bar.querySelector('#ko-volwrap');
  const volTrack = bar.querySelector('#ko-voltrack');
  const volFrac = (e) => {
    const r = volTrack.getBoundingClientRect();
    return r.width ? Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) : 0;
  };
  let volDrag = false;
  const setVolFrom = (e) => {
    const v = volFrac(e);
    if (KO.player === 'youtube') {
      KO.ytVol = v;
      KO.ytMuted = v === 0;
      ytCmd('setVolume', v);
      ytCmd('mute', KO.ytMuted);
    } else {
      KO.kickVol = v;
      KO.kickMuted = v === 0;
      kickSend({ t: 'volume', v: KO.kickVol });
      kickSend({ t: 'mute', m: KO.kickMuted });
    }
    updateKickVolUI();
  };
  volwrap.addEventListener('pointerdown', (e) => {
    if (e.target === muteBtn || e.target.closest('#ko-mute')) return;
    e.preventDefault();
    volDrag = true;
    try {
      volwrap.setPointerCapture(e.pointerId);
    } catch {
      /* capture unsupported */
    }
    setVolFrom(e);
  });
  volwrap.addEventListener('pointermove', (e) => {
    if (!volDrag) return;
    if (e.buttons === 0) {
      // capture lost: release happened outside — still persist
      volDrag = false;
      saveState();
      return;
    }
    setVolFrom(e);
  });
  volwrap.addEventListener('pointerup', () => {
    volDrag = false;
    saveState();
  });
  volwrap.addEventListener('pointercancel', () => {
    volDrag = false;
  });
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
      : pos >= dur - 30
        ? 'LIVE'
        : '-' + fmtDur(dur - pos, dur >= 3600);
    hov.style.left = Math.max(14, Math.min(r.width - 14, frac * r.width)) + 'px';
    hov.style.display = 'block';
  };
  sb.addEventListener('pointermove', (e) => {
    if (dragging) {
      if (e.buttons === 0) {
        // capture lost: release happened outside the bar — end the drag
        endDrag(e);
        return;
      }
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
  bar.querySelector('#ko-fs').addEventListener('click', toggleOverlayFullscreen);
  startRectLoop();
  setupHotBar(wrap);
}

function teardown() {
  teardownHotBar();
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
  if (KO.statusChip) {
    try {
      KO.statusChip.remove();
    } catch {
      /* already gone */
    }
    KO.statusChip = null;
  }
  // the frame dies with the removed iframe; no ytCmd('destroy') exists
  KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
  KO.ytUnlock = false;
  KO.ytWin = null;
  KO.lastYtSt = 0;
  KO.ytHlsUrl = null;
  KO.ytHlsAt = 0;
  KO.ytHlsFailed = false;
  KO.ytHlsFailedAt = 0;
  KO.ytLoadedUrl = null;
  KO.pendingYtUrl = null;
  KO.ytEmbedAt = 0;
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

// probe()'s wrap-hide sites: with the Twitch player deleted, hiding the wrap
// shows a blank page (there is no native player left) — keep the loading-state
// wrap up and let the chip carry the phase instead.
function hideWrapForProbe() {
  if (KO.twDeleted && KO.player !== 'twitch') return;
  if (KO.wrap) hideWrap();
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
  ytCmd('mute', KO.ytMuted);
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
    setBadge('KICK', '#6b7280');
    return;
  }
  KO.reconnectCount++;
  setBadge('KICK', '#d97706');
  if (KO.wrap) {
    const rc = KO.wrap.querySelector('#ko-reconnecting');
    if (rc) rc.style.display = 'flex';
  }
  console.log(`[ko] kick reconnect attempt ${KO.reconnectCount}/${MAX_RECONNECT}`);
  diag('reconnect', { n: KO.reconnectCount, max: MAX_RECONNECT });
  const k = await kickPlaybackUrl(KO.kickSlug);
  if (!k.live || !k.url) {
    teardown();
    setBadge('KICK', '#6b7280');
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
  const liveVisible = !!(st && (st.state === 'Playing' || st.state === 'Buffering' || KO.kickOnDvr));
  KO.wrap.classList.toggle('ko-offline', !liveVisible);
  renderQualityMenu();
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
  // Keep the hidden status state coherent for diagnostics. The persistent
  // top-left LIVE label is intentionally not rendered.
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
  updatePlayUI();
  updateKickVolUI();
}

function setPlayer(p) {
  if (p === KO.player) return;
  KO.player = p;
  saveState(true).then(() => apply()); // apply() only toggles layers/pause/mute
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
  ensureStatusChip(); // always-visible phase pill (survives hideWrap)
  // NOTE (2026-08-13): no Twitch-live gate here anymore. It used to
  // teardown() the whole overlay whenever the native player paused or
  // swapped elements during ad transitions, which made the manual KICK
  // switch appear broken (clicks did nothing until Twitch happened to be
  // playing). Manual switches always take effect; kick liveness is gated
  // by kickPlaybackUrl(), youtube by the embed's own live state.
  updateTwLiveSticky(twitchVideo());

  if (KO.player === 'twitch') {
    // Native player; overlay players paused (rect loop keeps them so).
    hideWrapForProbe();
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
      setBadge('KICK', '#6b7280');
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
      setBadge('KICK', '#d97706'); // url still loading — connecting phase
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
      setBadge('KICK', '#d97706'); // fresh url attached — frame still loading
      return;
    }
    console.log('[ko] kick offline or unreachable', KO.kickSlug, JSON.stringify(k));
    diag('kick_offline', { slug: KO.kickSlug, live: k.live, url: k.url ? 'yes' : 'no' });
    // Honest offline badge: after a yt→kick fallback the layer the user
    // actually chose is the failed one — say so instead of a KICK that
    // implies their own kick choice.
    setBadge(KO.ytHlsFailed ? 'YT' : 'KICK', '#6b7280');
    hideWrapForProbe();
    return;
  }

  // youtube mode
  if (!KO.ytRaw) {
    setBadge('YT', '#6b7280'); // no mapping — map the channel in the popup
    hideWrapForProbe();
    return;
  }
  await ensureYtId();
  if (!KO.ytId) {
    setBadge('YT', '#6b7280'); // could not resolve handle → check the popup value
    hideWrapForProbe();
    return;
  }
  await ensureYtHls();
  ensureYtIframe();
  // The HLS layer never initialized: the manifest mint failed (ytHlsFailed —
  // also set by the frame's fatal error handler after its one reload) or the
  // frame never reached ready within YT_EMBED_GRACE. Hand over to kick ONLY
  // when a REAL kick mapping exists (kickSlug differs from the twitch slug):
  // KO.kickSlug always falls back to the twitch slug, so a same-handle
  // "mapping" would bounce to a kick probe of the same channel that is
  // offline → wrap hidden → native Twitch visible. The handover must NOT
  // persist: the user's stored player choice stays 'youtube' (storage keeps
  // the user's choice; only the in-memory layer switches).
  if (
    KO.kickSlug && KO.kickSlug !== KO.slug &&
    (KO.ytHlsFailed || (KO.ytEmbedAt && !KO.ytState.ready && Date.now() - KO.ytEmbedAt > YT_EMBED_GRACE))
  ) {
    diag('yt_fallback', {
      kick: KO.kickSlug,
      fail: !!KO.ytHlsFailed,
      embedMs: KO.ytEmbedAt ? Date.now() - KO.ytEmbedAt : 0,
    });
    KO.player = 'kick'; // no saveState() — storage keeps the user's choice
    fire(apply);
    return;
  }
  // The mint refreshed (TTL or post-failure re-mint) while the frame was
  // alive: hand the NEW url to the frame — the old one is dead (stream
  // restart) and would otherwise sit black forever.
  if (KO.ytState.ready && KO.ytHlsUrl && KO.ytLoadedUrl !== KO.ytHlsUrl) {
    ytSend({ t: 'load', url: KO.ytHlsUrl });
    KO.ytLoadedUrl = KO.ytHlsUrl;
  }
  if (KO.ytState.live || (KO.ytState.ready && KO.ytHlsUrl && KO.ytLoadedUrl === KO.ytHlsUrl)) {
    // live, or ready with the current url handed over and starting to load
    // (kick-style transition grace: show now, hls.js goes live in a moment;
    // a dead url posts a fatal error that flips ytHlsFailed → fallback).
    showYtLayer();
    setBadge('YT', '#ff0000');
    return;
  }
  // Not live yet: amber while minting/loading (retry after the 30s backoff),
  // gray when the mint failed and no real kick mapping exists to fall back to.
  setBadge('YT', KO.ytHlsFailed ? '#6b7280' : '#d97706');
  hideWrapForProbe();
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

function toggleOverlayFullscreen() {
  if (!KO.wrap || KO.player === 'twitch' || KO.wrap.style.display === 'none') return;
  if (document.fullscreenElement === KO.wrap) document.exitFullscreen().catch(() => {});
  else KO.wrap.requestFullscreen().catch((err) => diag('fullscreen', { err: String(err).slice(0, 100) }));
}

function onOverlayKeydown(e) {
  if (String(e.key).toLowerCase() !== 'f' || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const target = e.target;
  if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
  if (!KO.wrap || KO.player === 'twitch' || KO.wrap.style.display === 'none') return;
  e.preventDefault();
  e.stopImmediatePropagation();
  toggleOverlayFullscreen();
}

function startWatchers() {
  KO.spaTimer = setInterval(() => {
    if (location.pathname !== KO.lastPath) {
      KO.lastPath = location.pathname;
      fire(apply);
    }
  }, SPA_MS);
  KO.pollTimer = setInterval(() => {
    if (KO.enabled) fire(probe);
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
      fire(probe);
    },
    true,
  );
  document.addEventListener('keydown', onOverlayKeydown, true);
}

function startRectLoop() {
  stopRectLoop();
  KO.rectTimer = setInterval(() => {
    try {
      rectTick();
    } catch (err) {
      // A per-tick crash would leave the wrap frozen at a stale rect (the
      // "mini player" symptom) — log it once and keep the loop alive.
      if (!KO.rectErrShown) {
        KO.rectErrShown = true;
        diag('rect_err', { m: String((err && err.message) || err).slice(0, 120) });
      }
    }
  }, RECT_MS);
}

// The <video> element rect jitters during boot/ad transitions (Twitch
// renders it at a transient smaller height — observed 1340x401 while the
// player card stayed 1340x751). The player CARD (.video-ref / the
// video-player wrapper) is layout-stable, so anchor the overlay to it;
// fall back to the video rect when no card is found (e.g. the video is a
// sidebar hover-preview).
function twitchAnchorRect() {
  const tv = twitchVideo();
  if (!tv) return null;
  let el = tv;
  for (let i = 0; i < 5 && el; i++) {
    const cls = (el.className || '').toString();
    if (cls.includes('video-ref') || cls.includes('video-player')) {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 ? r : tv.getBoundingClientRect();
    }
    el = el.parentElement;
  }
  return tv.getBoundingClientRect();
}

function rectTick() {
  if (!KO.wrap) {
    stopRectLoop();
    return;
  }
    ensureAttached(KO.wrap); // Twitch may re-render main — re-parent if dropped
    const tv = twitchVideo();
    updateTwLiveSticky(tv);
    const overlaySelected = KO.player !== 'twitch' && !!KO.wrap;
    const overlayShown = overlaySelected && KO.wrap.style.display !== 'none';
    if (overlayShown) {
      if (tv) {
        const prev = KO.lastRect;
        const r = twitchAnchorRect() || tv.getBoundingClientRect();
        const vw = window.innerWidth || 0;
        const vh = window.innerHeight || 0;
        let left = r.left;
        let top = r.top;
        let width = r.width;
        let height = r.height;
        if (vw > 0 && vh > 0) {
          left = Math.max(0, Math.min(left, vw - 1));
          top = Math.max(0, Math.min(top, vh - 1));
          width = Math.min(width, vw - left);
          height = Math.min(height, vh - top);
        }
        KO.lastRect = { left, top, width, height };
        const s = KO.wrap.style;
        s.left = `${left}px`;
        s.top = `${top}px`;
        s.width = `${width}px`;
        s.height = `${height}px`;
        // One-shot anomaly dump: the applied rect and the rendered box
        // disagree (the "mini player" symptom) — capture the constraint
        // source. Rate-limited 30s; renders fresh ground truth.
        if (Math.abs(KO.wrap.offsetHeight - Math.round(r.height)) > 2 && r.height > 40) {
          if (!KO.rectFitAt || Date.now() - KO.rectFitAt > 30000) {
            KO.rectFitAt = Date.now();
            const cs = getComputedStyle(KO.wrap);
            let chain = [];
            let pel = KO.wrap.parentElement;
            for (let i = 0; i < 4 && pel; i++, pel = pel.parentElement) {
              const cr = pel.getBoundingClientRect();
              chain.push(pel.tagName + '.' + String(pel.className || '').split(' ')[0] + ' h=' + Math.round(cr.height) + ' ' + getComputedStyle(pel).position);
            }
            diag('rect_fit', {
              applied: { w: Math.round(r.width), h: Math.round(r.height) },
              box: { w: KO.wrap.offsetWidth, h: KO.wrap.offsetHeight },
              styleAttr: (KO.wrap.getAttribute('style') || '').slice(0, 160),
              csHeight: cs.height,
              csMaxH: cs.maxHeight,
              csPos: cs.position,
              wrapCount: document.querySelectorAll('#ko-wrap').length,
              parentChain: chain,
            });
          }
        }
        // Ground truth when the page video population is unusual (a second
        // visible video — hover preview, clips card — or a large shrink):
        // the wrap should mirror the picked card rect exactly.
        if (!KO.rectDiagAt || Date.now() - KO.rectDiagAt > 10000) {
          const all = [...document.querySelectorAll('video')]
            .filter((v) => v.getClientRects().length)
            .map((v) => {
              const rr = v.getBoundingClientRect();
              return { w: Math.round(rr.width), h: Math.round(rr.height), rs: v.readyState, p: v.paused };
            });
          if (all.length > 1 || (prev && r.height < prev.height * 0.7)) {
            KO.rectDiagAt = Date.now();
            diag('rect_pop', {
              picked: { w: Math.round(r.width), h: Math.round(r.height) },
              all,
              wrap: { w: Math.round(KO.wrap.offsetWidth), h: Math.round(KO.wrap.offsetHeight) },
            });
          }
        }
      } else if (KO.twDeleted) {
        // User deleted the Twitch player from the popup: keep the overlay
        // pinned at its last rect (the whole point is seeing the overlay
        // WITHOUT the Twitch player underneath). Re-apply the rect every
        // tick — the delete handler primes lastRect with the viewport when
        // no player rect was ever measured, so the wrap covers the page.
        KO.hideTicks = 0;
        const lr = KO.lastRect;
        if (lr) {
          const s = KO.wrap.style;
          s.left = `${lr.left}px`;
          s.top = `${lr.top}px`;
          s.width = `${lr.width}px`;
          s.height = `${lr.height}px`;
        }
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
        KO.kickOnDvr = false;
        if (KO.wrap) KO.wrap.classList.add('ko-offline');
        KO.pendingUrl = KO.activeUrl;
        kickFrame();
      }
      // yt frame-death watchdog (mirror of the kick one): the bridge went
      // silent while shown. Remove #ko-yt and reset the ready state — the
      // next probe recreates the frame. Hidden tabs throttle the frame's
      // 1s state timer, so only run while the tab is visible.
      if (!document.hidden && KO.player === 'youtube' && KO.ytState.ready && KO.lastYtSt && Date.now() - KO.lastYtSt > 30000) {
        console.log('[ko] yt bridge silent — rebuilding frame');
        const yf = document.getElementById('ko-yt');
        if (yf) yf.remove();
        KO.ytState = { ready: false, playing: false, muted: true, live: false, dur: 0, ct: 0, error: 0 };
        KO.ytWin = null;
        KO.lastYtSt = 0;
        fire(probe); // recreate now instead of waiting for the 20s poll
      }
    } else if (overlaySelected) {
      syncMute();
      if (!KO.twDeleted && KO.wrap.style.display === 'none') resumeTwitchIfOurs();
    } else {
      if (KO.kickFrame && KO.kickState && KO.kickState.state === 'Playing') {
        kickSend({ t: 'pause' });
        kickSend({ t: 'mute', m: true });
      }
      if (KO.ytState.ready && KO.ytState.playing) ytCmd('pause');
      resumeTwitchIfOurs();
      syncMute();
    }
    // The chip lives outside the wrap — keep it pinned to the player rect
    // on every tick, whether the wrap is shown or hidden.
    positionStatusChip();
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
    '#ko-wrap.ko-kick #ko-yt{display:none;}' + // one rendering: the inactive player is hidden, not covered
    '#ko-wrap.ko-yt #ko-ivs{display:none;}' +
    '#ko-wrap.ko-yt iframe{width:100%;height:100%;border:0;display:block;pointer-events:auto;}' +
    '#ko-bar{position:absolute;left:0;right:0;bottom:0;pointer-events:auto;opacity:1;transition:opacity .18s ease;' +
    'display:flex;flex-direction:row;align-items:center;justify-content:space-between;padding:22px 10px 6px;color:#fff;' +
    'background:linear-gradient(0deg,rgba(0,0,0,.8),rgba(0,0,0,0));' +
    'font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}' +
    '#ko-wrap:not(.ko-hot) #ko-bar{opacity:1;}' +
    '#ko-wrap.ko-yt #ko-bar{display:flex;}' +
    '#ko-wrap.ko-yt #ko-top{display:none;}' + // stale LIVE pill in yt mode
    '#ko-wrap.ko-offline #ko-top{display:none;}' + // hide LIVE pill when kick is offline/not playing
    '#ko-reconnecting{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;' +
    'background:rgba(0,0,0,.82);color:#fff;font:700 15px system-ui,sans-serif;letter-spacing:.04em;pointer-events:none;}' +
    '.ko-g{display:flex;flex-direction:row;align-items:center;}' +
    '#ko-quality-wrap{position:relative;display:flex;align-items:center;}' +
    '#ko-quality-menu{display:none;position:absolute;right:0;bottom:42px;min-width:110px;padding:4px;' +
    'background:rgba(17,17,17,.96);border:1px solid rgba(255,255,255,.2);border-radius:6px;z-index:3;}' +
    '#ko-quality-menu button{display:block;width:100%;padding:7px 10px;border:0;background:transparent;color:#fff;' +
    'font:600 12px system-ui,sans-serif;text-align:left;cursor:pointer;border-radius:4px;}' +
    '#ko-quality-menu button:hover{background:rgba(255,255,255,.12);}' +
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
    '#ko-seekbar{position:absolute;top:6px;left:10px;right:10px;height:14px;cursor:pointer;touch-action:none;}' +
    '#ko-track{position:absolute;left:0;right:0;bottom:0;height:4px;border-radius:2px;background:rgba(146,158,166,.5);}' +
    '#ko-seekbar:hover #ko-track{height:6px;bottom:-1px;}' +
    '#ko-fill{position:absolute;left:0;top:0;bottom:0;background:#53fc18;border-radius:2px;}' +
    '.ko-prog{position:absolute;left:0;right:0;bottom:0;height:4px;transform-origin:left;}' +
    '#ko-loaded{background:rgba(255,255,255,.5);}' +
    '#ko-loadind{background:rgba(255,255,255,.3);}' +
    '#ko-thumb{position:absolute;top:50%;width:16px;height:16px;border-radius:50%;background:#53fc18;' +
    'transform:translate(-50%,-50%);display:none;}' +
    '#ko-seekbar:hover #ko-thumb,#ko-seekbar.ko-drag #ko-thumb{display:block;}' +
    '#ko-hov{position:absolute;top:-20px;left:0;transform:translateX(-50%);background:rgba(0,0,0,.78);' +
    'border-radius:6px;padding:4px 6px;font-size:12px;font-weight:700;color:#fff;' +
    'font-variant-numeric:tabular-nums;white-space:nowrap;display:none;pointer-events:none;z-index:2;}' +
    '#ko-top{display:none;position:absolute;top:0;left:0;right:0;pointer-events:none;flex-direction:row;' +
    'align-items:center;gap:12px;padding:10px 14px;color:#fff;' +
    'background:linear-gradient(180deg,rgba(0,0,0,.7),rgba(0,0,0,0));' +
    'font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}' +
    '#ko-livebadge{pointer-events:auto;display:flex;flex-direction:row;align-items:center;gap:6px;' +
    'background:transparent;border:0;color:#fff;cursor:default;padding:0;}' +
    '#ko-live-dot{width:10px;height:10px;border-radius:50%;background:#53fc18;}' +
    '#ko-live-txt{font-size:14px;font-weight:600;white-space:nowrap;}' +
    '#ko-elapsed{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;}';
  (document.head || document.documentElement).appendChild(st);
}

// ---- popup actions ----------------------------------------------------------
// ko-delete-twitch: remove the native Twitch player element so the user can
// SEE that the visible video is the overlay's, not Twitch's. Restored on
// page reload (Twitch rebuilds its player). The overlay stays pinned at the
// last player rect. Twitch's SPA re-renders the player element, so while a
// non-twitch layer is active a MutationObserver re-applies the delete.
let twDeleteObserver = null;

function killTwitchVideo(v) {
  if (!v) return;
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

function ensureTwDeleteObserver() {
  if (twDeleteObserver) return;
  twDeleteObserver = new MutationObserver((muts) => {
    // Only re-apply while an overlay layer is active — in twitch mode the
    // user explicitly chose the native player (their problem if it is gone).
    if (!KO.twDeleted || KO.player === 'twitch') return;
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (!n || n.nodeType !== 1) continue;
        if (n.tagName === 'VIDEO') {
          killTwitchVideo(n);
          continue;
        }
        // Re-renders may wrap the player — scan the added subtree shallowly.
        if (n.querySelectorAll) {
          for (const v of n.querySelectorAll('video')) killTwitchVideo(v);
        }
      }
    }
  });
  twDeleteObserver.observe(document.body, { childList: true, subtree: true });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'ko-delete-twitch') {
    KO.twDeleted = true;
    const v = twitchVideo();
    if (v) killTwitchVideo(v);
    // Twitch SPA re-renders the player element — re-apply the delete while a
    // non-twitch layer is active (the observer survives the element swap).
    ensureTwDeleteObserver();
    // Prime the pin: when no player rect was ever measured (deleted before
    // the first tick), the overlay covers the whole page instead of floating.
    if (!KO.lastRect) {
      const ar = twitchAnchorRect();
      if (ar && ar.width > 0 && ar.height > 0) {
        KO.lastRect = { left: ar.left, top: ar.top, width: ar.width, height: ar.height };
      }
    }
    // The point of the button is SEEING the overlay without the Twitch
    // player — if the current player is 'twitch' the overlay would stay
    // hidden and the click would look dead. Force an overlay player on.
    if (KO.player === 'twitch') {
      KO.player = 'kick';
      saveState(true);
    }
    // Youtube layer not shown (mint pending/failed)? Keep the wrap visible
    // in its loading state instead of hiding — the chip shows YT…/YT ✕ and
    // the black layer covers the deleted player slot (probe()'s hide sites
    // respect twDeleted via hideWrapForProbe()).
    if (KO.player === 'youtube' && KO.wrap && KO.wrap.style.display === 'none') {
      if (KO.lastRect) {
        const s = KO.wrap.style;
        s.left = `${KO.lastRect.left}px`;
        s.top = `${KO.lastRect.top}px`;
        s.width = `${KO.lastRect.width}px`;
        s.height = `${KO.lastRect.height}px`;
      }
      showWrap();
    }
    apply();
    diag('tw_delete', {});
    sendResponse({ ok: true });
  }
});

// ---- test / automation hook -------------------------------------------------
// to flip the toggle or remap the current channel (used by smoke tests).
window.addEventListener('kick-overlay:set', (e) => {
  const d = e.detail || {};
  if (typeof d.enabled === 'boolean') {
    KO.enabled = d.enabled;
    saveState().then(() => fire(apply));
  }
  if (d.kickSlug && KO.slug) {
    const m = KO.mappings[KO.slug];
    KO.mappings[KO.slug] = typeof m === 'object' ? { ...m, kick: d.kickSlug } : { kick: d.kickSlug };
    KO.kickSlug = d.kickSlug;
    saveState().then(() => fire(apply));
  }
  if (d.ytChannel && KO.slug) {
    const m = KO.mappings[KO.slug];
    KO.mappings[KO.slug] = typeof m === 'object' ? { ...m, yt: d.ytChannel } : { yt: d.ytChannel };
    KO.ytRaw = d.ytChannel;
    KO.ytId = null;
    saveState().then(() => fire(apply));
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
    // legacy mappings can be a plain string (mappings[slug] = 'foo'); keep it
    const base = typeof m === 'string' ? { kick: m } : m && typeof m === 'object' ? { ...m } : { kick: setupSlug };
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
    saveState(Boolean(qPlayer)).then(() => fire(apply));
  }
  // Persist the resolved defaults (enabled: true) so the popup's toggle
  // always agrees with the content script.
  if (Object.keys(KO.mappings).length === 0) {
    await saveState();
  }
  injectStyles();
  startWatchers();
  diag('boot', { ver: KO_VER, url: location.href.slice(0, 70), slug: currentSlug(), enabled: KO.enabled, player: KO.player, kickSlug: KO.kickSlug, hidden: document.hidden });
  // Heartbeat: full overlay state every 8s while the page is open.
  setInterval(() => {
    const tv = twitchVideo();
    diag('hb', {
      ver: KO_VER,
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
      wr: KO.wrap ? { w: Math.round(KO.wrap.offsetWidth), h: Math.round(KO.wrap.offsetHeight) } : null,
      vr: tv ? { w: Math.round(tv.getBoundingClientRect().width), h: Math.round(tv.getBoundingClientRect().height) } : null,
    });
  }, 8000);
  fire(apply);
})().catch((e) => diag('guard', { err: String((e && e.message) || e).slice(0, 120) }));
