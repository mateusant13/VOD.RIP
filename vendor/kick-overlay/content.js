// Kick Overlay — content script (runs on https://www.twitch.tv/*).
//
// While enabled and the streamer is live on BOTH platforms, embeds the
// REAL Kick player (player.kick.com/<slug>, the official embed with Kick's
// own IVS player + controls) pinned exactly over the Twitch player. Twitch
// keeps playing underneath, muted — so Twitch ads are invisible and
// inaudible without touching Twitch's own requests. The embedded player is
// Kick's real one (play/pause, volume, quality, fullscreen) — no
// reimplementation. A floating TWITCH button switches back to the native
// Twitch player; the popup holds the same option.
//
// Smoothness: the embed handles its own stream lifecycle (no hls.js, no
// token management, no teardown/reload cycles). The rect loop tolerates
// brief Twitch player re-layouts (ad transitions) with a hide-debounce so
// the overlay never blinks.
'use strict';

const KEY = 'ko.v2';
const POLL_MS = 20000;   // Kick live-status re-check while enabled
const RECT_MS = 400;     // overlay geometry + mute enforcement tick
const SPA_MS = 900;      // Twitch SPA pathname poll (no reload on channel nav)
const HIDE_TICKS = 3;    // consecutive ticks without a Twitch player before hiding

// Twitch routes that look like /<slug> but are not channels.
const NOT_CHANNEL = new Set([
  'directory', 'downloads', 'friends', 'gift', 'jobs', 'login', 'notifications',
  'p', 'prime', 'settings', 'subscriptions', 'turbo', 'events', 'search',
  'wallet', 'moderation', 'popout', 'videos', 'clips', 'team', 'tags',
]);

const KO = {
  enabled: false,
  player: 'kick', // 'kick' (overlay) | 'twitch' (native)
  mappings: {},   // twitchSlug -> kickSlug
  slug: null,
  kickSlug: null,
  wrap: null,     // overlay container holding the real Kick player iframe
  twitchBtn: null, // floating TWITCH switch (kick-player mode)
  pollTimer: null,
  rectTimer: null,
  spaTimer: null,
  muted: new Set(), // twitch videos we muted (restored on teardown)
  hideTicks: 0,
  lastPath: location.pathname,
};

// ---- storage ----------------------------------------------------------------

function loadState() {
  return new Promise((res) => {
    chrome.storage.local.get(KEY, (o) => {
      const s = (o && o[KEY]) || {};
      // Default ON (user mandate 2026-08-13): the overlay must protect the
      // first ad, not wait for a manual popup toggle. Explicit OFF respected.
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

// Largest visible page <video> that is not in our overlay — the Twitch player.
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

// Kick channel API — v2 livestream.playback_url first, v1 fallback. Both
// endpoints are anonymous and reflect any Origin (verified). Used ONLY for
// the live-status poll (the player itself is the real embed).
async function kickIsLive(slug) {
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
      const ls = d && (d.livestream || null);
      if (d && !d.livestream) return false; // v2 explicitly offline
      if (ls && (ls.playback_url || ls.is_live)) return true;
      if (d && d.playback_url) return true;
      return false;
    } catch {
      // transient — try next endpoint / next poll tick
    }
  }
  return false;
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeBackgroundColor({ color: color || [0, 0, 0, 0] });
    chrome.action.setBadgeText({ text: text || '' });
  } catch {
    /* badge is cosmetic */
  }
}

// ---- overlay lifecycle ------------------------------------------------------

function mount() {
  if (KO.wrap) return;
  const wrap = document.createElement('div');
  wrap.id = 'ko-wrap';
  wrap.style.display = 'none';
  const f = document.createElement('iframe');
  f.id = 'ko-frame';
  f.setAttribute('allow', 'autoplay; fullscreen; encrypted-media');
  f.setAttribute('allowfullscreen', '');
  f.setAttribute('scrolling', 'no');
  f.setAttribute('frameborder', '0');
  (document.body || document.documentElement).appendChild(wrap);
  wrap.appendChild(f);
  KO.wrap = wrap;
}

function teardown() {
  if (KO.wrap) {
    KO.wrap.remove();
    KO.wrap = null;
  }
  if (KO.twitchBtn) KO.twitchBtn.style.display = 'none';
  KO.hideTicks = 0;
  for (const v of KO.muted) {
    try {
      v.muted = false;
    } catch {
      /* detached */
    }
  }
  KO.muted.clear();
}

function showWrap() {
  if (!KO.wrap) return;
  KO.wrap.style.display = 'block';
  KO.hideTicks = 0;
}

function hideWrap() {
  if (KO.wrap) KO.wrap.style.display = 'none';
}

function startRectLoop() {
  stopRectLoop();
  KO.rectTimer = setInterval(() => {
    // Suppress all Twitch audio while the overlay is mounted; restore on teardown.
    for (const v of document.querySelectorAll('video')) {
      if (!v.muted) {
        v.muted = true;
        KO.muted.add(v);
      }
    }
    if (!KO.wrap) {
      stopRectLoop();
      return;
    }
    const tv = twitchVideo();
    if (tv) {
      const r = tv.getBoundingClientRect();
      const s = KO.wrap.style;
      s.left = `${r.left}px`;
      s.top = `${r.top}px`;
      s.width = `${r.width}px`;
      s.height = `${r.height}px`;
      if (KO.wrap.style.display === 'none') KO.hideTicks = 0;
      showWrap();
      // Floating TWITCH switch pinned to the player (kick-player mode).
      if (KO.twitchBtn) {
        KO.twitchBtn.style.display = 'block';
        KO.twitchBtn.style.left = `${Math.max(8, r.right - KO.twitchBtn.offsetWidth - 12)}px`;
        KO.twitchBtn.style.top = `${Math.max(8, r.bottom - KO.twitchBtn.offsetHeight - 12)}px`;
      }
    } else {
      // Debounce the hide: ad transitions / player re-layouts can briefly
      // drop the video from the tree — hiding on a single frame would blink.
      KO.hideTicks++;
      if (KO.hideTicks >= HIDE_TICKS) {
        hideWrap();
        if (KO.twitchBtn) KO.twitchBtn.style.display = 'none';
      }
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
    '#ko-wrap{position:fixed;z-index:2147483647;overflow:hidden;background:#000;}' +
    '#ko-frame{width:100%;height:100%;border:0;display:block;}' +
    '#ko-twitchbtn{position:fixed;z-index:2147483646;display:none;' +
    'background:#9147ff;color:#fff;border:0;border-radius:14px;padding:6px 14px;' +
    'font:700 13px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.5);}' +
    '#ko-twitchbtn:hover{background:#a970ff;}';
  (document.head || document.documentElement).appendChild(st);
}

// Floating TWITCH switch button shown in kick-player mode.
function buildTwitchBtn() {
  const b = document.createElement('button');
  b.id = 'ko-twitchbtn';
  b.textContent = 'TWITCH';
  b.title = 'Show the native Twitch player (Kick player hides)';
  b.addEventListener('click', () => {
    KO.player = 'twitch';
    saveState().then(() => apply());
  });
  (document.body || document.documentElement).appendChild(b);
  KO.twitchBtn = b;
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
    // SPA channel navigation — hot swap in place.
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
  if (!twitchIsLive(twitchVideo())) {
    setBadge('TW', '#6b7280'); // Twitch offline — nothing to cover
    teardown();
    return;
  }
  if (!(await kickIsLive(KO.kickSlug))) {
    setBadge('KICK OFF', '#6b7280');
    hideWrap();
    return;
  }
  setBadge('KICK', '#059669');
  mount();
  const f = KO.wrap.querySelector('#ko-frame');
  const want = `https://player.kick.com/${encodeURIComponent(KO.kickSlug)}?autoplay=true&muted=false`;
  if (f.src !== want) f.src = want; // remap in place — no reload of the overlay
  showWrap();
}

async function apply() {
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    if (KO.twitchBtn) KO.twitchBtn.style.display = 'none';
    return;
  }
  if (KO.player === 'twitch') {
    // Twitch-player option: no overlay, Twitch audio untouched; the
    // floating KICK button... native player is shown; overlay unmounted.
    setBadge('TW', '#6b7280');
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
    if (KO.enabled && KO.player === 'kick') probe();
  }, POLL_MS);
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
        twitchLive: twitchIsLive(v),
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
  buildTwitchBtn();
  startWatchers();
  apply();
})();
