// Kick Overlay — content script (runs on https://www.twitch.tv/*).
//
// While enabled and the streamer is live on BOTH platforms, plays the
// streamer's Kick livestream in a fixed-position <video> pinned exactly
// over the Twitch player. Twitch keeps playing underneath, muted — so
// Twitch ads are invisible and inaudible without touching Twitch's own
// requests. The Kick player has its own control bar (play/pause, mute +
// volume, fullscreen, live badge with resolution) and a "TWITCH" switch —
// the player option flips between Kick and Twitch players from the bar, a
// floating KICK button, or the popup. Toggling (toolbar / storage change)
// and Twitch SPA navigation hot-swap the player in place; no page reload
// is ever needed.
//
// Kick HLS is AWS IVS: master m3u8 from playback.live-video.net (needs the
// DNR CORS rule in rules.json — the IVS token's origin allowlist is
// kick.com-family only), variants/segments already serve ACAO:*.
'use strict';

const KEY = 'ko.v2'; // v2: default ON (2026-08-13) — v1's auto-saved enabled:false would override the new default
const POLL_MS = 20000;   // Kick live-status re-check while enabled
const RECT_MS = 400;     // overlay geometry + mute enforcement tick
const SPA_MS = 900;      // Twitch SPA pathname poll (no reload on channel nav)

// Twitch routes that look like /<slug> but are not channels.
const NOT_CHANNEL = new Set([
  'videos', 'clips', 'directory', 'settings', 'search', 'subscribe',
  'about', 'careers', 'jobs', 'download', 'wallet', 'inventory',
  'rewards', 'moderation', 'dashboard', 'popout', 'login', 'signup',
  'turbo', 'prime', 'gift', 'events', 'friends', 'notifications', 'p',
]);

const KO = {
  enabled: false,
  player: 'kick', // which player to show: 'kick' (overlay) | 'twitch' (native)
  mappings: {}, // twitchSlug -> kickSlug
  state: 'idle', // idle | probing | active
  slug: null, // current twitch channel slug
  kickSlug: null,
  activeUrl: null,
  hls: null,
  video: null, // our overlay <video>
  wrap: null, // overlay container (video + control bar)
  kickBtn: null, // floating KICK switch button (twitch-player mode)
  userPaused: false, // user paused the Kick player via the control bar
  pollTimer: null,
  rectTimer: null,
  spaTimer: null,
  muted: new Set(), // twitch videos we muted (restored on teardown)
  lastPath: location.pathname,
};

// ---- storage ----------------------------------------------------------------

function loadState() {
  return new Promise((res) => {
    chrome.storage.local.get(KEY, (o) => {
      const s = (o && o[KEY]) || {};
      // Default ON (user mandate 2026-08-13): the overlay must protect the
      // first ad, not wait for a manual popup toggle. An explicit OFF is
      // still respected (s.enabled === false).
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

// Kick channel API — v2 livestream.playback_url first, v1 fallback (some
// channels, e.g. yoda, only expose playback_url on the v1 endpoint).
// Both endpoints are anonymous and reflect any Origin (verified).
async function kickPlaybackUrl(slug) {
  const enc = encodeURIComponent(slug);
  const probes = [
    { url: `https://kick.com/api/v2/channels/${enc}`, v2: true },
    { url: `https://kick.com/api/v1/channels/${enc}`, v2: false },
  ];
  for (const p of probes) {
    try {
      const r = await fetch(p.url, { credentials: 'omit' });
      if (!r.ok) continue;
      const d = await r.json();
      if (p.v2) {
        const ls = d && d.livestream;
        if (ls && ls.playback_url) return { live: true, url: ls.playback_url };
        if (!ls) return { live: false }; // explicitly offline — no v1 probe
      } else if (d && d.playback_url) {
        return { live: true, url: d.playback_url };
      } else {
        return { live: false };
      }
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

// ---- overlay lifecycle ------------------------------------------------------

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
  KO.userPaused = false;
  KO.state = 'idle';
  KO.activeUrl = null;
  for (const v of KO.muted) {
    try {
      v.muted = false;
    } catch {
      /* detached */
    }
  }
  KO.muted.clear();
}

function tryPlay() {
  const v = KO.video;
  if (!v || !KO.hls) return;
  const p = v.play();
  if (!p) return;
  p.catch(() => {
    // Autoplay-with-sound blocked (no media engagement yet): run muted,
    // unmute on the user's first click anywhere.
    v.muted = true;
    v.play().catch(() => {});
    const unlock = () => {
      if (!KO.video) return;
      KO.video.muted = false;
      KO.video.play().catch(() => {});
      document.removeEventListener('pointerdown', unlock, true);
    };
    document.addEventListener('pointerdown', unlock, true);
  });
}

function activate(url) {
  if (KO.state === 'active' && KO.activeUrl === url) return;
  if (KO.state === 'active') teardown();
  KO.state = 'probing';
  KO.activeUrl = url;
  setBadge('…', '#2563eb');

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
  KO.userPaused = false;

  // Control-bar interactivity. The wrap itself is pointer-events:none (clicks
  // pass through to Twitch); only the bar opts back in.
  let hotTimer = null;
  const updateBar = () => {
    const play = bar.querySelector('#ko-play');
    play.textContent = v.paused ? '\u25B6' : '\u275A\u275A';
    const mute = bar.querySelector('#ko-mute');
    const silent = v.muted || v.volume === 0;
    mute.textContent = silent ? 'Unmute' : 'Mute';
    bar.querySelector('#ko-vol').value = String(Math.round((silent ? 0 : v.volume) * 100));
  };
  window.addEventListener('mousemove', (e) => {
    if (!KO.wrap || KO.state !== 'active') return;
    const r = KO.wrap.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) return;
    KO.wrap.classList.add('ko-hot');
    clearTimeout(hotTimer);
    hotTimer = setTimeout(() => {
      if (KO.wrap) KO.wrap.classList.remove('ko-hot');
    }, 2600);
  });
  bar.querySelector('#ko-play').addEventListener('click', () => {
    if (v.paused) {
      KO.userPaused = false;
      v.play().catch(() => {});
    } else {
      KO.userPaused = true;
      v.pause();
    }
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
    if (document.fullscreenElement === KO.wrap) {
      document.exitFullscreen().catch(() => {});
    } else if (KO.wrap) {
      KO.wrap.requestFullscreen().catch(() => {});
    }
  });
  bar.querySelector('#ko-switch').addEventListener('click', () => {
    KO.player = 'twitch';
    saveState().then(() => apply());
  });
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

  let fatalHandled = false;
  const hls = new Hls({
    liveDurationInfinity: true,
    backBufferLength: 30,
    maxBufferLength: 30,
    manifestLoadingTimeOut: 15000,
  });
  KO.hls = hls;
  hls.on(Hls.Events.ERROR, (_e, data) => {
    if (!data.fatal || fatalHandled) return;
    fatalHandled = true;
    // Likely an expired IVS token or stream ended — tear down and let the
    // poll loop re-probe with a fresh API token.
    teardown();
    setBadge('RETRY', '#d97706');
    setTimeout(() => {
      if (KO.enabled) probe();
    }, 5000);
  });
  hls.on(Hls.Events.MANIFEST_PARSED, (_e, data) => {
    KO.state = 'active';
    setBadge('KICK', '#059669');
    let res = '';
    if (data && data.levels && data.levels.length) {
      const lv = data.levels[0];
      if (lv && lv.width && lv.height) res = ` \u00B7 ${lv.width}\u00D7${lv.height}`;
    }
    const badgeEl = KO.wrap && KO.wrap.querySelector('#ko-badge');
    if (badgeEl) badgeEl.textContent = `KICK \u00B7 LIVE${res}`;
    tryPlay();
  });
  hls.on(Hls.Events.MEDIA_ATTACHED, () => tryPlay());
  hls.loadSource(url);
  hls.attachMedia(v);
  startRectLoop();
}

function sync() {
  const v = KO.video;
  if (!v || !KO.hls) return;
  if (twitchIsLive(twitchVideo())) {
    if (v.paused && !KO.userPaused) tryPlay();
  } else if (!v.paused) {
    v.pause(); // user paused Twitch (or Twitch switched scenes) — follow
    KO.userPaused = false;
  }
}

function startRectLoop() {
  stopRectLoop();
  KO.rectTimer = setInterval(() => {
    if (!KO.wrap && !KO.kickBtn) {
      stopRectLoop();
      return;
    }
    if (KO.state === 'active' && KO.wrap) {
      // Suppress all Twitch audio while the overlay is live; restore on teardown.
      for (const v of document.querySelectorAll('video')) {
        if (v === KO.video) continue;
        if (!v.muted) {
          v.muted = true;
          KO.muted.add(v);
        }
      }
      const tv = twitchVideo();
      if (tv) {
        const r = tv.getBoundingClientRect();
        const s = KO.wrap.style;
        s.left = `${r.left}px`;
        s.top = `${r.top}px`;
        s.width = `${r.width}px`;
        s.height = `${r.height}px`;
        s.display = 'block';
      } else {
        KO.wrap.style.display = 'none';
      }
      sync();
    } else if (KO.player === 'twitch' && KO.kickBtn) {
      // Twitch-player mode: keep the floating KICK switch pinned to the player.
      const tv = twitchVideo();
      if (tv) {
        const r = tv.getBoundingClientRect();
        KO.kickBtn.style.display = 'block';
        KO.kickBtn.style.left = `${Math.max(8, r.right - KO.kickBtn.offsetWidth - 12)}px`;
        KO.kickBtn.style.top = `${Math.max(8, r.bottom - KO.kickBtn.offsetHeight - 12)}px`;
      } else {
        KO.kickBtn.style.display = 'none';
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
    '#ko-kickbtn{position:fixed;z-index:2147483646;pointer-events:auto;display:none;' +
    'background:#9147ff;color:#fff;border:0;border-radius:14px;padding:6px 14px;' +
    'font:700 13px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.5);}' +
    '#ko-kickbtn:hover{background:#a970ff;}';
  (document.head || document.documentElement).appendChild(st);
}

// Floating KICK switch button shown in Twitch-player mode.
function buildKickBtn() {
  const b = document.createElement('button');
  b.id = 'ko-kickbtn';
  b.textContent = 'KICK';
  b.title = 'Use the Kick player (no Twitch ads)';
  b.addEventListener('click', () => {
    KO.player = 'kick';
    saveState().then(() => apply());
  });
  (document.body || document.documentElement).appendChild(b);
  KO.kickBtn = b;
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
    // slug on Kick (rodil -> kick.com/rodil) so the overlay works out of
    // the box; an explicit popup mapping still overrides.
    KO.kickSlug = KO.mappings[slug] || slug;
    teardown();
  }
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    return;
  }
  if (!KO.kickSlug) {
    setBadge('MAP?', '#b45309');
    teardown();
    return;
  }
  if (!twitchIsLive(twitchVideo())) {
    setBadge('TW', '#6b7280'); // Twitch offline — nothing to cover
    teardown();
    return;
  }
  const k = await kickPlaybackUrl(KO.kickSlug);
  if (!k.live || !k.url) {
    setBadge('KICK OFF', '#6b7280');
    teardown();
    return;
  }
  activate(k.url);
}

async function apply() {
  if (!KO.enabled) {
    setBadge('OFF', '#6b7280');
    teardown();
    if (KO.kickBtn) KO.kickBtn.style.display = 'none';
    return;
  }
  if (KO.player === 'twitch') {
    // Twitch-player option: no overlay, Twitch audio untouched; the
    // floating KICK button switches back to the Kick player.
    setBadge('TW', '#6b7280');
    teardown();
    startRectLoop(); // keeps the KICK button pinned to the player
    return;
  }
  if (KO.kickBtn) KO.kickBtn.style.display = 'none';
  const slug = currentSlug();
  if (!slug) {
    setBadge('', null);
    teardown();
    return;
  }
  if (slug !== KO.slug) {
    KO.slug = slug;
    KO.kickSlug = KO.mappings[slug] || slug; // same-handle fallback (see probe)
    teardown();
  }
  if (!KO.kickSlug) {
    setBadge('MAP?', '#b45309');
    teardown();
    return;
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
// to flip the toggle or remap the current channel without touching the
// popup (used by the smoke test; harmless in production).
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
  window.dispatchEvent(
    new CustomEvent('kick-overlay:status-reply', {
      detail: {
        enabled: KO.enabled,
        player: KO.player,
        state: KO.state,
        slug: KO.slug,
        kickSlug: KO.kickSlug,
        videoPlaying: !!(KO.video && !KO.video.paused && KO.video.currentTime > 0),
        videoMuted: !!(KO.video && KO.video.muted),
        twitchMuted: [...document.querySelectorAll('video')]
          .filter((v) => v !== KO.video)
          .every((v) => v.muted),
      },
    }),
  );
});

// ---- boot -------------------------------------------------------------------

(async function init() {
  await loadState();
  // Persist the resolved defaults (enabled: true) so the popup's toggle
  // always agrees with the content script. The same-handle fallback makes
  // an explicit mapping seed unnecessary — every channel works out of the
  // box (ponytail: popup mapping remains the override for different
  // handles).
  if (Object.keys(KO.mappings).length === 0) {
    await saveState();
  }
  injectStyles();
  buildKickBtn();
  startWatchers();
  apply();
})();
