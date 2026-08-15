// IG FollowGuard — dashboard (popup + injected panel share this).
'use strict';

const TABS = { nonFollowers: 'nonFollowers', events: 'events', mutual: 'mutual' };
const PAGE = 60;
const R = 60 * 1000;

const $ = (id) => document.getElementById(id);

const el = {
  pill: () => $('pill'),
  pillText: () => $('pill-text'),
  lastSync: () => $('last-sync'),
  refresh: () => $('refresh'),
  cardK: () => $('card-k'),
  cardF: () => $('card-f'),
  cardM: () => $('card-m'),
  tabs: () => $('tabs'),
  search: () => $('search'),
  list: () => $('list'),
  error: () => $('error'),
  errText: () => $('err-text'),
  errBtn: () => $('err-btn'),
  meta: () => $('meta'),
  interval: () => $('interval'),
  notif: () => $('notif'),
  openIg: () => $('open-ig'),
};

let state = { status: 'idle' };
let settings = {};
let followers = {};
let following = {};
let events = [];
let tab = TABS.nonFollowers;
let query = '';
let shown = 0;
let live = [];

function relTime(iso) {
  if (!iso) return 'nunca';
  const d = Date.now() - new Date(iso).getTime();
  if (d < R) return 'agora';
  const m = Math.floor(d / R);
  if (m < 60) return `há ${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `há ${h} h`;
  const days = Math.floor(h / 24);
  return `há ${days} d`;
}

function avatarImg(u) {
  if (u.profile_pic_url) {
    // No direct src: fbcdn serves `Cross-Origin-Resource-Policy: same-origin`
    // for no-referrer/cross-site requests, which the browser enforces on <img>
    // (net::ERR_BLOCKED_BY_RESPONSE.NotSameOrigin — the old placeholder bug).
    // hydrateAvatars() fetches the pic with an instagram.com referrer (CDN
    // then answers `cross-origin`) and swaps in a same-origin blob URL.
    const esc = String(u.profile_pic_url).replace(/&/g, '&amp;').replace(/"/g, '&quot;');
    return `<img class="avatar" data-pic="${esc}" alt="">`;
  }
  return placeholder(u);
}
function placeholder(u) {
  const letter = (u.username || '?')[0].toUpperCase();
  return `<span class="avatar" style="display:inline-flex;align-items:center;justify-content:center;font-weight:700;color:#fff;background:linear-gradient(135deg,#feda75,#d62976,#962fbf,#4f5bd5)">${letter}</span>`;
}

// Resolve profile pics as same-origin blob URLs (deduped per page instance).
// LRU-capped: the Map holds only the most-recent 512 URLs, so pathological
// scrolling on huge accounts can't grow the string Map unboundedly.
// ponytail: evicted blobs are NOT revoked — revoking a URL that is still in
// the DOM (re-rendered items) would break a visible avatar; blobs are freed
// anyway when the document (popup/panel iframe) is destroyed.
const PIC_CACHE_MAX = 512;
const picCache = new Map(); // url -> Promise<blobUrl>
function hydrateAvatars(root) {
  for (const img of root.querySelectorAll('img.avatar[data-pic]')) {
    const url = img.dataset.pic;
    img.removeAttribute('data-pic');
    let p = picCache.get(url);
    if (!p) {
      p = fetch(url, { referrer: 'https://www.instagram.com/', credentials: 'omit' })
        .then((r) => {
          if (!r.ok) throw new Error('http ' + r.status);
          return r.blob();
        })
        .then((b) => URL.createObjectURL(b))
        .catch((err) => { picCache.delete(url); throw err; });
      picCache.set(url, p);
      if (picCache.size > PIC_CACHE_MAX) {
        const oldest = picCache.keys().next().value; // Map = insertion order
        picCache.delete(oldest);
      }
    }
    p.then((blobUrl) => { if (img.isConnected) img.src = blobUrl; })
      .catch(() => {
        if (img.isConnected) {
          const item = img.closest('.item');
          img.outerHTML = placeholder(item && item.dataset ? { username: item.dataset.u } : {});
        }
      });
  }
}

function itemHtml(u) {
  const tags = [];
  if (u.is_private) tags.push('<span class="tag private">privado</span>');
  if (u.is_verified) tags.push('<span class="tag verified">✓</span>');
  const name = u.full_name ? ` — ${u.full_name}` : '';
  return `
    <div class="item" data-u="${u.username}">
      ${avatarImg(u)}
      <div class="who">
        <b><a href="https://www.instagram.com/${encodeURIComponent(u.username)}/" target="_blank" rel="noopener">${u.username}</a></b>
        <span title="${u.full_name}">${u.full_name || ''}</span>
      </div>
      ${tags.join('')}
    </div>`;
}

function eventHtml(e) {
  const tags = [];
  if (e.stillFollowing) tags.push('<span class="tag unfollowed">você segue</span>');
  const name = e.fullName ? ` — ${e.fullName}` : '';
  return `
    <div class="item" data-u="${e.username}">
      ${avatarImg({ username: e.username, profile_pic_url: e.profilePicUrl })}
      <div class="who">
        <b><a href="https://www.instagram.com/${encodeURIComponent(e.username)}/" target="_blank" rel="noopener">${e.username}</a></b>
        <span title="${e.fullName}">${e.fullName || ''}</span>
      </div>
      ${tags.join('')}
      <time>${relTime(new Date(e.detectedAt).toISOString())}</time>
    </div>`;
}

function computeLists() {
  const fKeys = new Set(Object.keys(followers));
  const gKeys = Object.keys(following);
  const nonFollowers = gKeys.filter((u) => !fKeys.has(u));
  const mutual = gKeys.filter((u) => fKeys.has(u));
  return { nonFollowers, mutual };
}

function renderHeader() {
  const pill = el.pill();
  pill.className = 'pill';
  const t = el.pillText();
  switch (state.status) {
    case 'syncing':
      pill.classList.add('sync');
      if (state.syncProgress) {
        const p = state.syncProgress;
        const label = p.phase === 'followers' ? 'seguidores' : 'seguindo';
        t.textContent = `sincronizando… ${label}: ${Number(p.fetched || 0).toLocaleString('pt-BR')}`;
      } else {
        t.textContent = 'sincronizando…';
      }
      break;
    case 'ok': pill.classList.add('ok'); t.textContent = 'atualizado'; break;
    case 'error': pill.classList.add('err'); t.textContent = 'erro'; break;
    case 'idle': t.textContent = 'aguardando'; break;
    default: t.textContent = state.status;
  }
  el.lastSync().textContent = `última: ${relTime(state.lastSyncAt)}`;
  el.refresh().disabled = state.status === 'syncing';
  el.cardK().querySelector('.count').textContent = state.notFollowingBackCount;
  el.cardF().querySelector('.count').textContent = state.followersCount;
  el.cardM().querySelector('.count').textContent = state.followingCount;
}

function renderError() {
  if (state.status === 'error' && state.error) {
    el.error().style.display = 'block';
    el.errText().textContent = state.error;
    const isLogin = /sessão|login|verificação/i.test(state.error);
    el.errBtn().textContent = isLogin ? 'Abrir Instagram' : 'Tentar de novo';
    el.errBtn().onclick = isLogin ? openInstagram : () => sendSync();
  } else {
    el.error().style.display = 'none';
  }
}

function renderTabs() {
  const { nonFollowers, mutual } = computeLists();
  const nEvents = events.length;
  const tabs = [
    { key: TABS.nonFollowers, label: `Não seguem de volta (${nonFollowers.length})` },
    { key: TABS.events, label: `Deixaram de seguir (${nEvents})` },
    { key: TABS.mutual, label: `Seguem de volta (${mutual.length})` },
  ];
  el.tabs().innerHTML = tabs
    .map((t) => `<button class="tab ${t.key === tab ? 'active' : ''}" data-tab="${t.key}">${t.label}</button>`)
    .join('');
  el.tabs().querySelectorAll('.tab').forEach((b) => {
    b.onclick = () => { tab = b.dataset.tab; shown = 0; render(); };
  });
}

function renderList() {
  const { nonFollowers, mutual } = computeLists();
  const q = query.trim().toLowerCase();
  let pool;
  if (tab === TABS.events) {
    pool = events.filter((e) => !q || e.username.toLowerCase().includes(q) || (e.fullName || '').toLowerCase().includes(q));
  } else {
    const keys = tab === TABS.nonFollowers ? nonFollowers : mutual;
    const map = tab === TABS.nonFollowers ? following : following;
    pool = keys
      .map((u) => map[u])
      .filter(Boolean)
      .filter((u) => !q || u.username.toLowerCase().includes(q) || (u.full_name || '').toLowerCase().includes(q));
  }
  live = pool;
  const slice = pool.slice(0, shown + PAGE);
  if (slice.length === 0) {
    el.list().innerHTML = '<div class="empty">Nada aqui' + (query ? ' para essa busca' : '') + '.</div>';
    return;
  }
  el.list().innerHTML =
    slice.map(tab === TABS.events ? eventHtml : itemHtml).join('') +
    (pool.length > slice.length ? '<button class="more">Mostrar mais</button>' : '');
  const more = el.list().querySelector('.more');
  if (more) more.onclick = () => { shown += PAGE; renderList(); };
  el.list().querySelectorAll('.item').forEach((it) => {
    it.onclick = (ev) => {
      if (ev.target.closest('a')) return;
      openProfile(it.dataset.u);
    };
  });
  hydrateAvatars(el.list());
}

function renderMeta() {
  el.meta().textContent = state.ownUsername ? `@${state.ownUsername} · listas completas` : '';
}

function renderSettings() {
  el.interval().value = String(settings.refreshMinutes || 60);
  el.notif().checked = settings.notificationsEnabled !== false;
}

function render() {
  renderHeader();
  renderError();
  renderTabs();
  renderList();
  renderMeta();
  renderSettings();
}

function sendSync() {
  chrome.runtime.sendMessage({ type: 'igf-sync', trigger: 'manual' });
}

// Events stored before profilePicUrl existed: backfill the pic from the
// current follow lists (the user is in at least one of them while relevant).
function enrichEvents(list) {
  return list.map((e) => {
    if (e.profilePicUrl) return e;
    const pic =
      (following[e.username] && following[e.username].profile_pic_url) ||
      (followers[e.username] && followers[e.username].profile_pic_url) ||
      '';
    return pic ? { ...e, profilePicUrl: pic } : e;
  });
}

function openInstagram() {
  chrome.tabs.create({ url: 'https://www.instagram.com/' });
}

function openProfile(username) {
  chrome.tabs.create({ url: `https://www.instagram.com/${encodeURIComponent(username)}/` });
}

async function load() {
  const o = await chrome.storage.local.get([
    'igf.state', 'igf.settings', 'igf.followers', 'igf.following', 'igf.unfollowEvents',
  ]);
  state = o['igf.state'] || { status: 'idle' };
  settings = o['igf.settings'] || {};
  followers = o['igf.followers'] || {};
  following = o['igf.following'] || {};
  events = enrichEvents(o['igf.unfollowEvents'] || []);
  render();
  // Panel: auto-sync when data is stale (popup relies on the manual button).
  if (document.body.classList.contains('panel')) {
    const staleMs = (settings.refreshMinutes || 60) * 60 * 1000;
    if (!state.lastSyncAt || Date.now() - new Date(state.lastSyncAt).getTime() > staleMs) {
      sendSync();
    }
  }
  // Announce the dashboard is live (the injected panel listens; harmless
  // when this page runs as the toolbar popup — posting to self, no receiver).
  try {
    parent.postMessage({ type: 'igf-panel-ready' }, '*');
  } catch { /* never break the UI */ }
}

// --- events ----------------------------------------------------------------
el.refresh().onclick = sendSync;
el.cardK().onclick = () => { tab = TABS.nonFollowers; shown = 0; render(); };
el.cardF().onclick = () => { tab = TABS.mutual; shown = 0; render(); };
el.cardM().onclick = () => { tab = TABS.mutual; shown = 0; render(); };
el.search().addEventListener('input', (e) => { query = e.target.value; shown = 0; renderList(); });
el.interval().addEventListener('change', async (e) => {
  await chrome.runtime.sendMessage({
    type: 'igf-settings-update',
    settings: { refreshMinutes: Number(e.target.value) },
  });
});
el.notif().addEventListener('change', async (e) => {
  await chrome.runtime.sendMessage({
    type: 'igf-settings-update',
    settings: { notificationsEnabled: e.target.checked },
  });
});
el.openIg().onclick = openInstagram;

// CSP-safe avatar fallback: a failed <img> becomes the letter placeholder.
// (Inline onerror= handlers are blocked by the extension CSP.)
document.addEventListener('error', (ev) => {
  const t = ev.target;
  if (!t || t.tagName !== 'IMG' || !t.classList || !t.classList.contains('avatar')) return;
  const item = t.closest && t.closest('.item');
  t.outerHTML = placeholder(item && item.dataset ? { username: item.dataset.u } : {});
}, true);

// Panel-only: the inline close script was removed from panel.html (extension
// CSP blocks inline scripts -> console errors); wired here, guarded for the
// popup which has no #close button.
const closeBtn = $('close');
if (closeBtn) closeBtn.addEventListener('click', () => parent.postMessage({ type: 'igf-close-panel' }, '*'));

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  if (changes['igf.state']) state = changes['igf.state'].newValue || { status: 'idle' };
  if (changes['igf.settings']) settings = changes['igf.settings'].newValue || {};
  if (changes['igf.followers']) followers = changes['igf.followers'].newValue || {};
  if (changes['igf.following']) following = changes['igf.following'].newValue || {};
  if (changes['igf.unfollowEvents']) events = enrichEvents(changes['igf.unfollowEvents'].newValue || []);
  render();
});

load();
