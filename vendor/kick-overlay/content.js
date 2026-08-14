// Kick Overlay — content script (runs on https://www.twitch.tv/*).
//
// While enabled and the streamer is live on BOTH platforms, plays the
// streamer's REAL Kick stream (the same playback_url the VOD.RIP downloader
// uses — full HD, all IVS renditions) in an overlay pinned exactly over the
// Twitch player. Twitch keeps playing underneath, muted — Twitch ads are
// invisible and inaudible without touching Twitch's own requests. The
// player is a thin hls.js shell with a minimal control bar; the STREAM is
// Kick's own (the embed player.kick.com is deliberately capped by Kick at
// low res + "Visit Kick for HD", so it is not used).
//
// Seamless player switch (user mandate 2026-08-13): the Kick player mounts
// ONCE per channel and is NEVER destroyed when switching kick<->twitch —
// the switch only toggles wrap visibility + mute state, so the live keeps
// playing without a single frame of rebuffer or blink. hls fatal errors
// reconnect IN PLACE (the wrap keeps covering Twitch the whole time) — no
// teardown flash, no Twitch peek-through.
'use strict';

const KEY = 'ko.v2';
const POLL_MS = 20000;   // Kick live-status re-check while enabled
const RECT_MS = 400;     // overlay geometry + mute enforcement tick
const SPA_MS = 900;      // Twitch SPA pathname poll (no reload on channel nav)
const HIDE_TICKS = 3;    // consecutive ticks without a Twitch player before hiding
const MAX_RECONNECT = 3; // hls fatal retries (fresh playback_url each time)

// Twitch routes that look like /<slug> but are not channels.
const NOT_CHANNEL = new Set([
  'directory', 'downloads', 'friends', 'gift', 'jobs', 'login', 'notifications',
  'p', 'prime', 'settings', 'subscriptions', 'turbo', 'events', 'search',
  'wallet', 'moderation', 'popout', 'videos', 'clips', 'team', 'tags',
]);

const KO = {
  enabled: false,
  player: 'kick', // 'kick' (overlay) | 'twitch' (native) — switching never rebuilds the player
  mappings: {},   // twitchSlug -> kickSlug
  slug: null,
  kickSlug: null,
  activeUrl: null, // playback_url currently attached
  hls: null,
  video: null,    // our overlay <video> (Kick stream)
  wrap: null,     // overlay container (video + control bar) — persists across switches
  twitchBtn: null, // floating TWITCH switch (kick mode)
  kickBtn: null,   // floating KICK switch (twitch mode)
  reconnectCount: 0,
  pollTimer: null,
  rectTimer: null,
  spaTimer: null,
  muted: new Set(), // twitch videos we muted (restored on teardown/switch)
  hideTicks: 0,
  lastPath: location.pathname,
};

// ---- storage ----------------------------------------------------------------

function loadState() {
  return new Promise((res) => {
    chrome.storage.local.get(KEY, (o) => {
      const s = (o && o[KEY]) || {};
      // Default ON (user mandate 2026-08-13). Explicit OFF respected.
      KO.enabled = s.enabled === undefined ? true : !!s.enabled;
      KO.player = s.player === 'twitch' ? 'twitch' : 'kick';
      KO.mappings = s.mappings && typeof s.mappings === 'object' ? s.mappings : {};
      res();
    });
  });
}

function saveState() {
  return new Promise((res) => {
    chrome.storage.local.set({ [KEY]: { enabled: KO.enabled, mappings: KO.mappings, player: KO.player } }, res);
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes[KEY]) return;
  const s = changes[KEY].newValue || {};
  KO.enabled = !!s.enabled;
  KO.player = s.player === 'twitch' ? 'twitch' : 'kick';
  KO.mappings = s.mappings && typeof s.mappings === 'object' ? s.mappings : {};
  apply(); // hot toggle / remap / player switch — no page reload
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
  const vids = [...document.querySelectorAll('video')].filter(
    (v) => v !== KO.video && v.getClientRects().length,
  );
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

// Kick stream source — THE full-HD playback_url (the same endpoint the
// VOD.RIP downloader uses; all IVS renditions incl. 1080p+). v2 exposes it
// TOP-LEVEL (d.playback_url); v1 is the fallback for the few channels that
// only expose it there. Anonymous, reflects any Origin (verified).
async function kickPlaybackUrl(slug) {
  const enc = encodeURIComponent(slug);
  const probes = [
    { url: `https://kick.com/api/v2/channels/${enc}`, top: true },
    { url: `https://kick.com/api/v1/channels/${enc}`, top: false },
  ];
  for (const p of probes) {
    try {
      const r = await fetch(p.url, { credentials: 'omit' });
      if (!r.ok) continue;
      const d = await r.json();
      const ls = d && d.livestream;
      const pu = p.top ? d.playback_url : d.playback_url;
      if (pu) return { live: true, url: pu };
      if (ls && ls.playback_url) return { live: true, url: ls.playback_url };
      if (p.top && !ls) return { live: false }; // v2 explicitly offline
      if (d && !d.playback_url && !ls) return { live: false };
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

// ---- mute model -------------------------------------------------------------
// kick mode: every Twitch video muted (ads inaudible), our Kick video loud.
// twitch mode: Twitch unmuted, our Kick video muted (keeps playing hidden).
// We only track videos WE muted, so a user-set Twitch mute survives teardown.

function syncMute() {
  const kickShown = KO.player === 'kick' && !!KO.wrap && KO.wrap.style.display !== 'none';
  for (const v of document.querySelectorAll('video')) {
    if (v === KO.video) continue;
    if (kickShown && !v.muted) {
      v.muted = true;
      KO.muted.add(v);
    } else if (!kickShown && KO.muted.has(v) && v.muted) {
      v.muted = false;
      KO.muted.delete(v);
    }
  }
  if (KO.video) KO.video.muted = !kickShown;
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
  if (KO.video) KO.video.muted = false;
}

// ---- overlay lifecycle ------------------------------------------------------

function mount() {
  if (KO.wrap) return;
  const wrap = document.createElement('div');
  wrap.id = 'ko-wrap';
  wrap.style.display = 'none';
  const v = document.createElement('video');
  v.setAttribute('playsinline', '');
  v.setAttribute('autoplay', '');
  wrap.appendChild(v);
  const bar = document.createElement('div');
  bar.id = 'ko-bar';
  bar.innerHTML =
    '<span id="ko-badge">KICK</span>' +
    '<button id="ko-play" title="Play/Pause">\u275A\u275A</button>' +
    '<button id="ko-mute" title="Mute">Mute</button>' +
    '<input id="ko-vol" type="range" min="0" max="100" value="100" title="Volume" />' +
    '<span id="ko-time">LIVE</span>' +
    '<span style="flex:1"></span>' +
    '<button id="ko-switch">TWITCH</button>' +
    '<button id="ko-fs" title="Fullscreen">\u26F6</button>';
  wrap.appendChild(bar);
  (document.body || document.documentElement).appendChild(wrap);
  KO.wrap = wrap;
  KO.video = v;
  KO.hideTicks = 0;

  let hotTimer = null;
  const updateBar = () => {
    bar.querySelector('#ko-play').textContent = v.paused ? '\u25B6' : '\u275A\u275A';
    const silent = v.muted || v.volume === 0;
    bar.querySelector('#ko-mute').textContent = silent ? 'Unmute' : 'Mute';
    bar.querySelector('#ko-vol').value = String(Math.round((silent ? 0 : v.volume) * 100));
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
    if (v.paused) v.play().catch(() => {});
    else v.pause();
  });
  bar.querySelector('#ko-mute').addEventListener('click', () => {
    v.muted = !v.muted;
    if (!v.muted && v.volume === 0) v.volume = 1;
    updateBar();
  });
  const vol = bar.querySelector('#ko-vol');
  vol.addEventListener('input', () => {
    v.volume = Number(vol.value) / 100;
    v.muted = vol.value === '0';
    updateBar();
  });
  bar.querySelector('#ko-fs').addEventListener('click', () => {
    if (document.fullscreenElement === KO.wrap) document.exitFullscreen().catch(() => {});
    else if (KO.wrap) KO.wrap.requestFullscreen().catch(() => {});
  });
  bar.querySelector('#ko-switch').addEventListener('click', () => setPlayer('twitch'));
  v.addEventListener('play', updateBar);
  v.addEventListener('pause', updateBar);
  v.addEventListener('volumechange', updateBar);
  v.addEventListener('timeupdate', () => {
    const t = Math.floor(v.currentTime || 0);
    const hh = String(Math.floor(t / 3600)).padStart(2, '0');
    const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0');
    const ss = String(t % 60).padStart(2, '0');
    bar.querySelector('#ko-time').textContent = `LIVE \u00B7 ${hh}:${mm}:${ss}`;
  });
  startRectLoop();
}

function teardown() {
  stopRectLoop();
  if (KO.hls) {
    try {
      KO.hls.destroy();
    } catch {
      /* already gone */
    }
    KO.hls = null;
  }
  if (KO.wrap) {
    KO.wrap.remove();
    KO.wrap = null;
    KO.video = null;
  }
  KO.activeUrl = null;
  KO.reconnectCount = 0;
  KO.hideTicks = 0;
  unmuteAll();
  if (KO.twitchBtn) KO.twitchBtn.style.display = 'none';
  if (KO.kickBtn) KO.kickBtn.style.display = 'none';
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
  syncMute();
}

// Attach the current playback_url to hls. If the SAME url is already
// attached, this is a no-op — switching players never re-attaches.
function ensurePlayer(url) {
  if (!KO.wrap) mount();
  if (KO.activeUrl === url && KO.hls && KO.video) {
    if (KO.video.paused) KO.video.play().catch(() => {});
    return;
  }
  KO.activeUrl = url;
  if (KO.hls) {
    try {
      KO.hls.destroy();
    } catch {
      /* already gone */
    }
    KO.hls = null;
  }
  KO.reconnectCount = 0;
  const hls = new Hls({
    liveDurationInfinity: true,
    backBufferLength: 30,
    maxBufferLength: 30,
    manifestLoadingTimeOut: 15000,
  });
  KO.hls = hls;
  hls.on(Hls.Events.ERROR, (_e, data) => {
    if (!data.fatal || !KO.enabled || KO.player !== 'kick') return;
    reconnect();
  });
  hls.on(Hls.Events.MANIFEST_PARSED, (_e, mdata) => {
    KO.reconnectCount = 0;
    const badgeEl = KO.wrap && KO.wrap.querySelector('#ko-badge');
    if (badgeEl) {
      const lv = mdata && mdata.levels && mdata.levels[0];
      const res = lv && lv.width && lv.height ? ` \u00B7 ${lv.width}\u00D7${lv.height}` : '';
      badgeEl.textContent = `KICK \u00B7 LIVE${res}`;
    }
    setBadge('KICK', '#059669');
    KO.video.play().catch(() => {
      // Autoplay-with-sound blocked: run muted, unmute on first click.
      KO.video.muted = true;
      KO.video.play().catch(() => {});
      const unlock = () => {
        if (!KO.video) return;
        KO.video.muted = false;
        KO.video.play().catch(() => {});
        document.removeEventListener('pointerdown', unlock, true);
      };
      document.addEventListener('pointerdown', unlock, true);
    });
  });
  hls.on(Hls.Events.MEDIA_ATTACHED, () => {
    KO.video.play().catch(() => {});
  });
  hls.loadSource(url);
  hls.attachMedia(KO.video);
}

// hls fatal — reconnect IN PLACE: the wrap keeps covering Twitch (muted)
// the whole time, so the user never sees the underlying player or an ad.
// Each attempt fetches a FRESH playback_url (IVS tokens rotate). If Kick
// reports offline, the stream ended — tear down and let the poll take over.
async function reconnect() {
  if (KO.reconnectCount >= MAX_RECONNECT) {
    teardown();
    setBadge('KICK OFF', '#6b7280');
    if (KO.kickBtn) KO.kickBtn.style.display = 'none';
    return;
  }
  KO.reconnectCount++;
  setBadge('RECONNECT', '#d97706');
  const k = await kickPlaybackUrl(KO.kickSlug);
  if (!k.live || !k.url) {
    teardown();
    setBadge('KICK OFF', '#6b7280');
    return;
  }
  await new Promise((r) => setTimeout(r, 2000)); // back off, then re-attach
  if (!KO.enabled || KO.player !== 'kick') return;
  ensurePlayer(k.url);
}

function setPlayer(p) {
  if (p === KO.player) return;
  KO.player = p;
  saveState().then(() => apply()); // apply() only toggles visibility/mute
}

// ---- decision loop ----------------------------------------------------------

async function probe() {
  const slug = currentSlug();
  if (!slug) {
    setBadge('', null);
    teardown();
    return;
  }
  if (slug !== KO.slug) {
    KO.slug = slug;
    // Same-handle fallback (2026-08-13): unmapped channels use the same
    // slug on Kick (rodil -> kick.com/rodil); popup mapping overrides.
    KO.kickSlug = KO.mappings[slug] || slug;
    teardown();
  }
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    return;
  }
  const liveTw = twitchIsLive(twitchVideo());
  const k = await kickPlaybackUrl(KO.kickSlug);

  if (KO.player === 'twitch') {
    // Native player visible; the Kick player (if mounted) keeps playing
    // hidden — switching back is instant. When kick is live, pre-attach
    // it even on first entry so the first switch never re-buffers.
    setBadge(k.live ? 'KICK' : 'KICK OFF', k.live ? '#059669' : '#6b7280');
    if (k.live && k.url) ensurePlayer(k.url);
    if (KO.wrap) hideWrap();
    if (KO.kickBtn) KO.kickBtn.style.display = k.live ? 'block' : 'none';
    syncMute();
    return;
  }

  if (!liveTw) {
    setBadge('TW', '#6b7280'); // Twitch offline — nothing to cover
    teardown();
    return;
  }
  if (!k.live || !k.url) {
    // Kick offline: nothing to overlay — watch Twitch native (unmuted).
    setBadge('KICK OFF', '#6b7280');
    teardown();
    return;
  }
  ensurePlayer(k.url);
  showWrap();
  if (KO.twitchBtn) KO.twitchBtn.style.display = 'block';
  syncMute();
}

async function apply() {
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    return;
  }
  const slug = currentSlug();
  if (!slug) {
    setBadge('', null);
    teardown();
    return;
  }
  if (slug !== KO.slug) {
    KO.slug = slug;
    KO.kickSlug = KO.mappings[slug] || slug;
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
  // The boot probe can race the Twitch player's start (10s+ delay before
  // readyState>=2/currentTime>0 — seen live). Re-probe the moment the
  // player starts playing, throttled. Video events don't bubble; capture
  // catches them.
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
    const kickShown = KO.player === 'kick';
    if (kickShown) {
      const tv = twitchVideo();
      if (tv) {
        const r = tv.getBoundingClientRect();
        const s = KO.wrap.style;
        s.left = `${r.left}px`;
        s.top = `${r.top}px`;
        s.width = `${r.width}px`;
        s.height = `${r.height}px`;
        if (KO.wrap.style.display === 'none') showWrap();
        syncMute();
      } else {
        // Debounce the hide: ad transitions / player re-layouts can briefly
        // drop the video from the tree — hiding on a single frame would blink.
        KO.hideTicks++;
        if (KO.hideTicks >= HIDE_TICKS) hideWrap();
      }
    } else {
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
    '#ko-wrap{position:fixed;z-index:2147483647;pointer-events:none;background:#000;overflow:hidden;}' +
    '#ko-wrap video{width:100%;height:100%;display:block;pointer-events:none;object-fit:contain;background:#000;}' +
    '#ko-bar{position:absolute;left:0;right:0;bottom:0;pointer-events:auto;opacity:0;transition:opacity .18s ease;' +
    'display:flex;align-items:center;gap:10px;padding:9px 12px;color:#fff;' +
    'background:linear-gradient(0deg,rgba(0,0,0,.85),rgba(0,0,0,0));font:12px/1 system-ui,sans-serif;}' +
    '#ko-wrap.ko-hot #ko-bar{opacity:1;}' +
    '#ko-bar button{background:transparent;border:0;color:#fff;cursor:pointer;font:inherit;padding:3px 7px;border-radius:4px;white-space:nowrap;}' +
    '#ko-bar button:hover{background:rgba(255,255,255,.18);}' +
    '#ko-badge{font-weight:700;color:#53fc18;white-space:nowrap;}' +
    '#ko-time{color:#cfcfcf;white-space:nowrap;}' +
    '#ko-vol{width:70px;height:4px;accent-color:#9147ff;cursor:pointer;}' +
    '#ko-switch{font-weight:700;color:#a970ff;border:1px solid #a970ff !important;border-radius:12px !important;padding:2px 10px !important;}' +
    '#ko-twitchbtn,#ko-kickbtn{position:fixed;z-index:2147483646;pointer-events:auto;display:none;' +
    'background:#9147ff;color:#fff;border:0;border-radius:14px;padding:6px 14px;' +
    'font:700 13px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.5);}' +
    '#ko-twitchbtn:hover,#ko-kickbtn:hover{background:#a970ff;}' +
    '#ko-kickbtn{background:#53fc18;color:#000;}';
  (document.head || document.documentElement).appendChild(st);
}

// Floating switch buttons: TWITCH shown in kick mode, KICK in twitch mode.
function buildSwitchButtons() {
  if (!KO.twitchBtn) {
    const b = document.createElement('button');
    b.id = 'ko-twitchbtn';
    b.textContent = 'TWITCH';
    b.title = 'Show the native Twitch player (Kick keeps playing hidden)';
    b.addEventListener('click', () => setPlayer('twitch'));
    (document.body || document.documentElement).appendChild(b);
    KO.twitchBtn = b;
  }
  if (!KO.kickBtn) {
    const b = document.createElement('button');
    b.id = 'ko-kickbtn';
    b.textContent = 'KICK';
    b.title = 'Show the Kick player (no Twitch ads)';
    b.addEventListener('click', () => setPlayer('kick'));
    (document.body || document.documentElement).appendChild(b);
    KO.kickBtn = b;
  }
}

// Pin the active switch button to the Twitch player (bottom-right).
function pinSwitchButton() {
  const btn = KO.player === 'kick' ? KO.twitchBtn : KO.kickBtn;
  const other = KO.player === 'kick' ? KO.kickBtn : KO.twitchBtn;
  if (other) other.style.display = 'none';
  if (!btn) return;
  const tv = twitchVideo();
  if (tv) {
    const r = tv.getBoundingClientRect();
    btn.style.display = 'block';
    btn.style.left = `${Math.max(8, r.right - btn.offsetWidth - 12)}px`;
    btn.style.top = `${Math.max(8, r.bottom - btn.offsetHeight - 12)}px`;
  } else {
    btn.style.display = 'none';
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
    KO.mappings[KO.slug] = d.kickSlug;
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
        mounted: !!KO.wrap,
        wrapShown: !!(KO.wrap && KO.wrap.style.display !== 'none'),
        videoPlaying: !!(KO.video && !KO.video.paused && KO.video.currentTime > 0),
        videoMuted: !!(KO.video && KO.video.muted),
        twitchLive: twitchIsLive(v),
        twitchMuted: [...document.querySelectorAll('video')]
          .filter((x) => x !== KO.video)
          .every((x) => x.muted),
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
  setInterval(pinSwitchButton, RECT_MS); // cheap: position the active switch button
  apply();
})();
