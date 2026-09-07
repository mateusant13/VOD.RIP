// IG FollowGuard — dashboard (popup + injected panel share this).
'use strict';

const TABS = {
  nonFollowers: 'nonFollowers',
  fans: 'fans',
  mutual: 'mutual',
  events: 'events',
  newFollowers: 'newFollowers',
};
const PAGE = 60;
const INITIAL_VISIBLE = 12;
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
  syncHint: () => $('sync-hint'),
  errText: () => $('err-text'),
  errBtn: () => $('err-btn'),
  meta: () => $('meta'),
  interval: () => $('interval'),
  notif: () => $('notif'),
  openIg: () => $('open-ig'),
  toolbar: () => $('toolbar'),
};

let state = { status: 'idle' };
let settings = {};
let followers = {};
let following = {};
let events = [];
let newFollowers = [];
let history = {};
let tab = TABS.nonFollowers;
let filterType = 'all';
let query = '';
let shown = 0;
let lastOwnKey = '';
let listsCache = null;
let listsFollowersRef = null;
let listsFollowingRef = null;
let poolCache = null;
let poolCacheSig = '';
let listRenderedCount = 0;
let renderRaf = 0;
let listObserver = null;
let pauseTick = null;
let cooldownNoticeMs = 0;
let cooldownTick = null;

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function relTime(iso) {
  if (!iso) return 'nunca';
  const d = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(d)) return 'nunca'; // NaN / Invalid Date
  if (d < R) return 'agora'; // future timestamps clamp to "agora" too
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
    const pic = esc(u.profile_pic_url);
    return `<img class="avatar" data-pic="${pic}" alt="">`;
  }
  return placeholder(u);
}
function placeholder(u) {
  const letter = esc((u.username || '?')[0].toUpperCase());
  return `<span class="avatar" style="display:inline-flex;align-items:center;justify-content:center;font-weight:700;color:#fff;background:linear-gradient(135deg,#feda75,#d62976,#962fbf,#4f5bd5)">${letter}</span>`;
}

// Resolve profile pics as same-origin blob URLs (deduped per page instance).
// LRU-capped: the Map holds only the most-recent 512 URLs, so pathological
// scrolling on huge accounts can't grow the string Map unboundedly. Failed
// fetches are cached too (failedPicUrls, ~10 min TTL) so a dead URL is not
// refetched from the CDN on every render.
// ponytail: evicted blobs are NOT revoked — revoking a URL that is still in
// the DOM (re-rendered items) would break a visible avatar; blobs are freed
// anyway when the document (popup/panel iframe) is destroyed.
const PIC_CACHE_MAX = 512;
const picCache = new Map(); // url -> Promise<blobUrl>
const FAILED_PIC_TTL_MS = 10 * 60 * 1000;
const failedPicUrls = new Map(); // url -> expiry timestamp (Date.now() + TTL)
function isAllowedPicUrl(raw) {
  try {
    const u = new URL(raw);
    if (u.protocol !== 'https:') return false;
    const h = u.hostname.toLowerCase();
    return h === 'instagram.com' || h.endsWith('.instagram.com')
      || h.endsWith('.cdninstagram.com') || h.endsWith('.fbcdn.net');
  } catch {
    return false;
  }
}
function hydrateAvatars(root) {
  for (const img of root.querySelectorAll('img.avatar[data-pic]')) {
    const url = img.dataset.pic;
    img.removeAttribute('data-pic');
    if (!isAllowedPicUrl(url)) {
      const item = img.closest('.item');
      img.outerHTML = placeholder(item && item.dataset ? { username: item.dataset.u } : {});
      continue;
    }
    const failedUntil = failedPicUrls.get(url);
    if (failedUntil && Date.now() < failedUntil) {
      const item = img.closest('.item');
      img.outerHTML = placeholder(item && item.dataset ? { username: item.dataset.u } : {});
      continue;
    }
    let p = picCache.get(url);
    if (!p) {
      p = fetch(url, { referrer: 'https://www.instagram.com/', credentials: 'omit' })
        .then((r) => {
          if (!r.ok) throw new Error('http ' + r.status);
          return r.blob();
        })
        .then((b) => URL.createObjectURL(b))
        .catch((err) => {
          picCache.delete(url);
          failedPicUrls.set(url, Date.now() + FAILED_PIC_TTL_MS);
          throw err;
        });
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

function matchesTypeFilter(u) {
  if (filterType === 'verified') return !!u.is_verified;
  if (filterType === 'private') return !!u.is_private;
  return true;
}
function itemHtml(u) {
  const tags = [];
  if (u.is_private) tags.push('<span class="tag private">privado</span>');
  if (u.is_verified) tags.push('<span class="tag verified">✓</span>');
  const user = esc(u.username);
  const full = esc(u.full_name || '');
  return `
    <div class="item" data-u="${user}">
      ${avatarImg(u)}
      <div class="who">
        <b><a href="#" class="profile-open" data-profile="${user}">${user}</a></b>
        <span title="${full}">${full}</span>
      </div>
      ${tags.join('')}
    </div>`;
}

function eventHtml(e) {
  const tags = [];
  if (e.stillFollowing) tags.push('<span class="tag unfollowed">você segue</span>');
  const user = esc(e.username);
  const full = esc(e.fullName || '');
  return `
    <div class="item" data-u="${user}">
      ${avatarImg({ username: e.username, profile_pic_url: e.profilePicUrl })}
      <div class="who">
        <b><a href="#" class="profile-open" data-profile="${user}">${user}</a></b>
        <span title="${full}">${full}</span>
      </div>
      ${tags.join('')}
      <time>${relTime(new Date(e.detectedAt).toISOString())}</time>
    </div>`;
}

function invalidateListCaches() {
  listsCache = null;
  poolCache = null;
  poolCacheSig = '';
  listRenderedCount = 0;
  resetListObserver();
}

function computeLists() {
  const fKeys = new Set(Object.keys(followers));
  const gKeys = Object.keys(following);
  const nonFollowers = gKeys.filter((u) => !fKeys.has(u));
  const mutual = gKeys.filter((u) => fKeys.has(u));
  const fans = Object.keys(followers).filter((u) => !following[u]);
  return { nonFollowers, mutual, fans };
}

function getLists() {
  if (listsCache && listsFollowersRef === followers && listsFollowingRef === following) return listsCache;
  listsFollowersRef = followers;
  listsFollowingRef = following;
  listsCache = computeLists();
  return listsCache;
}


function manualSyncCooldownMsLocal() {
  const total = (Number(state.followersCount) || 0) + (Number(state.followingCount) || 0);
  const minutes = Math.min(45, Math.max(5, 5 + Math.floor(total / 500)));
  return minutes * 60 * 1000;
}

function manualCooldownRemaining() {
  if (state.status !== 'ok' || !state.lastSyncAt) return 0;
  if (state.freeManualRefresh) return 0;
  const elapsed = Date.now() - new Date(state.lastSyncAt).getTime();
  return Math.max(0, manualSyncCooldownMsLocal() - elapsed);
}

function formatCooldownWait(ms) {
  const min = Math.max(1, Math.ceil(ms / 60000));
  return min === 1 ? '1 minuto' : `${min} minutos`;
}
function liveCounts() {
  const fKeys = Object.keys(followers);
  const gKeys = Object.keys(following);
  const fSet = new Set(fKeys);
  const notBack = gKeys.filter((u) => !fSet.has(u)).length;
  return { followers: fKeys.length, following: gKeys.length, notBack };
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
        const g = Number(p.followingFetched ?? 0);
        const f = Number(p.followersFetched ?? 0);
        const phase = p.phase === 'followers' ? '2/2 seguidores' : (p.phase === 'following' ? '1/2 seguindo' : 'listas');
        if (p.segmentPause && p.resumeAt) {
          const sec = Math.max(0, Math.ceil((p.resumeAt - Date.now()) / 1000));
          t.textContent = `pausa… ${phase}: seguindo ${g.toLocaleString('pt-BR')} · seguidores ${f.toLocaleString('pt-BR')} — ${sec}s`;
        } else {
          t.textContent = `sincronizando… ${phase}: seguindo ${g.toLocaleString('pt-BR')} · seguidores ${f.toLocaleString('pt-BR')}`;
        }
      } else {
        t.textContent = 'sincronizando…';
      }
      break;
    case 'ok': pill.classList.add('ok'); t.textContent = 'atualizado'; break;
    case 'error':
      pill.classList.add('err');
      t.textContent = state.incomplete ? 'incompleto' : 'erro';
      break;
    case 'idle': t.textContent = 'aguardando'; break;
    default: t.textContent = state.status;
  }
  el.lastSync().textContent = `última: ${relTime(state.lastSyncAt)}`;
  const cdMs = manualCooldownRemaining();
  const canRefresh = state.status !== 'syncing' && (cdMs <= 0 || !!state.freeManualRefresh);
  el.refresh().disabled = !canRefresh;
  el.refresh().title = state.freeManualRefresh
    ? 'Sincronizar agora (1 atualização gratuita após a última sync)'
    : (cdMs > 0
      ? `Próxima sincronização manual em ${formatCooldownWait(cdMs)}`
      : 'Sincronizar agora');
  const live = state.status === 'syncing' ? liveCounts() : null;
  el.cardK().querySelector('.count').textContent = live ? live.notBack : state.notFollowingBackCount;
  el.cardF().querySelector('.count').textContent = live ? live.followers : state.followersCount;
  el.cardM().querySelector('.count').textContent = live ? live.following : state.followingCount;
}

function renderSyncHint() {
  const hint = el.syncHint();
  if (!hint) return;
  if (cooldownNoticeMs > 0) {
    hint.style.display = 'block';
    hint.innerHTML =
      '<strong>Sincronização recente</strong> — a próxima manual fica disponível em ' +
      formatCooldownWait(cooldownNoticeMs) +
      '. O intervalo automático (abaixo) não muda.';
    return;
  }
  if (state.status === 'syncing') {
    hint.style.display = 'block';
    const p = state.syncProgress;
    if (p && p.segmentPause) {
      hint.innerHTML =
        '<strong>Pausa entre blocos</strong> — a sincronização continua sozinha em instantes. ' +
        'Não feche a aba do Instagram.';
    } else {
      hint.innerHTML =
        '<strong>Não feche a aba do Instagram</strong> enquanto sincroniza. ' +
        'A aba pode ficar em segundo plano — não precisa estar em foco.';
    }
  } else {
    hint.style.display = 'none';
  }
}

function renderError() {
  if (state.status === 'error' && state.error) {
    el.error().style.display = 'block';
    el.errText().textContent = state.error;
    // Background persists an errorCode with every error state; classify the
    // action from it. Regex on message text only as a fallback for state
    // persisted by older versions without errorCode.
    const code = state.errorCode;
    let action; // 'open' | 'retry' | 'none'
    if (code === 'not-logged-in' || code === 'checkpoint' || code === 'feedback-required') {
      action = 'open';
    } else if (code === 'rate-limited' || code === 'limit') {
      action = 'none'; // message already says when to retry — no button noise
    } else if (code) {
      action = 'retry';
    } else {
      // Legacy persisted state (pre-errorCode): fall back to text heuristics.
      action = /sessão|login|verificação|limitada|feedback|aguarde|temporariamente/i.test(state.error)
        ? 'open'
        : 'retry';
    }
    const btn = el.errBtn();
    if (action === 'open') {
      btn.style.display = '';
      btn.textContent = 'Abrir Instagram';
      btn.onclick = openInstagram;
    } else if (action === 'retry') {
      btn.style.display = '';
      btn.textContent = 'Tentar de novo';
      btn.onclick = () => sendSync();
    } else {
      btn.style.display = 'none';
      btn.onclick = null;
    }
  } else {
    el.error().style.display = 'none';
  }
}

function renderTabs() {
  const { nonFollowers, mutual, fans } = getLists();
  const tabs = [
    { key: TABS.nonFollowers, label: `Não seguem (${nonFollowers.length})` },
    { key: TABS.fans, label: `Te seguem (${fans.length})` },
    { key: TABS.mutual, label: `Mútuos (${mutual.length})` },
    { key: TABS.events, label: `Deixaram (${events.length})` },
    { key: TABS.newFollowers, label: `Novos (${newFollowers.length})` },
  ];
  el.tabs().innerHTML = tabs
    .map((t) => `<button class="tab ${t.key === tab ? 'active' : ''}" data-tab="${t.key}">${t.label}</button>`)
    .join('');
  el.tabs().querySelectorAll('.tab').forEach((b) => {
    b.onclick = () => { tab = b.dataset.tab; shown = 0; invalidateListCaches(); render(); };
  });
  const meta = el.meta();
  if (meta && state.ownUsername) {
    // state.incomplete is set by the background when a list was truncated by
    // the page cap — the pill already shows "incompleto"; echo it in the meta.
    meta.textContent = state.incomplete
      ? `@${state.ownUsername} · lista incompleta — sincronize de novo`
      : `@${state.ownUsername} · listas completas`;
  }
}

function renderToolbar() {
  const bar = el.toolbar();
  if (!bar) return;
  const chips = [
    { key: 'all', label: 'Todos' },
    { key: 'verified', label: 'Verificados' },
    { key: 'private', label: 'Privados' },
  ];
  bar.innerHTML = `
    <div class="chip-row">
      ${chips.map((c) => `<button type="button" class="chip ${filterType === c.key ? 'active' : ''}" data-filter="${c.key}">${c.label}</button>`).join('')}
    </div>`;
  bar.querySelectorAll('.chip').forEach((b) => {
    b.onclick = () => { filterType = b.dataset.filter; shown = 0; invalidateListCaches(); renderToolbar(); renderList(); };
  });
}

async function exportBackupFile() {
  // SW gone / popup closed mid-call: surface it, never an unhandled rejection.
  const res = await chrome.runtime.sendMessage({ type: 'igf-export-backup' })
    .catch(() => null);
  if (!res || !res.ok || !res.backup) {
    alert((res && res.error) || 'Não foi possível exportar o backup.');
    return;
  }
  const name = `igfollowguard-backup-${new Date().toISOString().slice(0, 10)}.json`;
  const blob = new Blob([JSON.stringify(res.backup, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  try {
    await chrome.downloads.download({ url, filename: name, saveAs: false });
  } catch {
    alert('Não foi possível baixar o backup.');
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

async function importBackupFile(file) {
  if (!file) return;
  let backup;
  try {
    backup = JSON.parse(await file.text());
  } catch {
    alert('Arquivo inválido — escolha um .json exportado pelo IG FollowGuard.');
    return;
  }
  if (!confirm('Substituir os dados atuais desta instalação pelo backup?')) return;
  const res = await chrome.runtime.sendMessage({ type: 'igf-import-backup', backup })
    .catch(() => null);
  if (!res || !res.ok) {
    alert((res && res.error) || 'Falha ao importar backup.');
    return;
  }
  await load().catch(() => {});
}

function buildPool() {
  const sig = `${tab}|${filterType}|${query}|${Object.keys(followers).length}|${Object.keys(following).length}|${events.length}|${newFollowers.length}`;
  if (poolCache && poolCacheSig === sig) return poolCache;
  const { nonFollowers, mutual, fans } = getLists();
  const q = query.trim().toLowerCase();
  let pool;
  if (tab === TABS.events) {
    pool = events.filter((e) => !q || e.username.toLowerCase().includes(q) || (e.fullName || '').toLowerCase().includes(q));
  } else if (tab === TABS.newFollowers) {
    pool = newFollowers.filter((e) => !q || e.username.toLowerCase().includes(q) || (e.fullName || '').toLowerCase().includes(q));
  } else {
    let keys;
    let map;
    if (tab === TABS.nonFollowers) { keys = nonFollowers; map = following; }
    else if (tab === TABS.fans) { keys = fans; map = followers; }
    else { keys = mutual; map = following; }
    pool = keys
      .map((u) => map[u])
      .filter(Boolean)
      .filter((u) => matchesTypeFilter(u))
      .filter((u) => !q || u.username.toLowerCase().includes(q) || (u.full_name || '').toLowerCase().includes(q));
  }
  poolCacheSig = sig;
  poolCache = pool;
  return pool;
}

function wireListItems(root) {
  root.querySelectorAll('a.profile-open').forEach((a) => {
    a.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openProfile(a.dataset.profile);
    };
  });
  root.querySelectorAll('.item').forEach((it) => {
    it.onclick = (ev) => {
      if (ev.target.closest('a, button')) return;
      openProfile(it.dataset.u);
    };
  });
}

function resetListObserver() {
  if (listObserver) {
    listObserver.disconnect();
    listObserver = null;
  }
}

function ensureListObserver(listEl) {
  resetListObserver();
  listObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        shown += PAGE;
        renderList();
        break;
      }
    }
  }, { root: listEl, rootMargin: '120px', threshold: 0 });
}

function renderList() {
  const pool = buildPool();
  const target = shown === 0 ? INITIAL_VISIBLE : shown;
  const slice = pool.slice(0, target);
  const listEl = el.list();
  if (slice.length === 0) {
    listEl.innerHTML = '<div class="empty">Nada aqui' + (query ? ' para essa busca' : '') + '.</div>';
    listRenderedCount = 0;
    resetListObserver();
    return;
  }
  const htmlFn = (tab === TABS.events || tab === TABS.newFollowers) ? eventHtml : itemHtml;
  const hasMore = pool.length > slice.length;
  if (listRenderedCount === 0 || listRenderedCount > slice.length) {
    listEl.innerHTML = slice.map(htmlFn).join('') + (hasMore ? '<div class="sentinel" aria-hidden="true"></div>' : '');
    listRenderedCount = slice.length;
    wireListItems(listEl);
    hydrateAvatars(listEl);
  } else if (slice.length > listRenderedCount) {
    const sentinel = listEl.querySelector('.sentinel');
    if (sentinel) sentinel.remove();
    const tmp = document.createElement('div');
    tmp.innerHTML = slice.slice(listRenderedCount).map(htmlFn).join('');
    while (tmp.firstChild) listEl.appendChild(tmp.firstChild);
    if (hasMore) {
      const s = document.createElement('div');
      s.className = 'sentinel';
      s.setAttribute('aria-hidden', 'true');
      listEl.appendChild(s);
    }
    listRenderedCount = slice.length;
    wireListItems(listEl);
    hydrateAvatars(listEl);
  }
  shown = slice.length;
  if (hasMore) {
    ensureListObserver(listEl);
    const sentinel = listEl.querySelector('.sentinel');
    if (sentinel) listObserver.observe(sentinel);
  } else {
    resetListObserver();
  }
}

function renderSettings() {
  el.interval().value = String(settings.refreshMinutes || 60);
  el.notif().checked = settings.notificationsEnabled !== false;
}

function renderHeavy() {
  renderTabs();
  renderToolbar();
  renderList();
  renderSettings();
}

function scheduleRender() {
  if (renderRaf) return;
  renderRaf = requestAnimationFrame(() => {
    renderRaf = 0;
    render();
  });
}

function render() {
  renderHeader();
  renderSyncHint();
  renderError();
  if (pauseTick) {
    clearInterval(pauseTick);
    pauseTick = null;
  }
  if (state.status === 'syncing') {
    if (state.syncProgress && state.syncProgress.segmentPause) {
      pauseTick = setInterval(() => {
        renderHeader();
        renderSyncHint();
      }, 1000);
    }
    renderHeavy();
    return;
  }
  renderHeavy();
}

function showCooldownNotice(ms) {
  cooldownNoticeMs = ms;
  renderSyncHint();
  if (cooldownTick) clearInterval(cooldownTick);
  if (ms <= 0) return;
  cooldownTick = setInterval(() => {
    const left = manualCooldownRemaining();
    if (left <= 0) {
      cooldownNoticeMs = 0;
      clearInterval(cooldownTick);
      cooldownTick = null;
      render();
      return;
    }
    cooldownNoticeMs = left;
    renderSyncHint();
    renderHeader();
  }, 1000);
}

async function sendSync() {
  const cdMs = manualCooldownRemaining();
  if (cdMs > 0 && !state.freeManualRefresh) {
    showCooldownNotice(cdMs);
    return;
  }
  try {
    const res = await chrome.runtime.sendMessage({ type: 'igf-sync', trigger: 'manual' });
    if (res && res.skipped === 'cooldown') {
      showCooldownNotice((res.waitMinutes || 1) * 60000);
    }
  } catch { /* popup may close */ }
}

function eventPicUrl(e) {
  const u = e.username;
  return (
    (following[u] && following[u].profile_pic_url) ||
    (followers[u] && followers[u].profile_pic_url) ||
    (history[u] && history[u].profilePicUrl) ||
    e.profilePicUrl ||
    ''
  );
}

function enrichEvents(list) {
  return list.map((e) => {
    const pic = eventPicUrl(e);
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
  let o;
  try {
    o = await chrome.storage.local.get([
      'igf.state', 'igf.settings', 'igf.followers', 'igf.following', 'igf.followHistory', 'igf.unfollowEvents', 'igf.newFollowerEvents',
    ]);
  } catch {
    // storage.local.get rejecting (SW teardown races etc.) must not kill the
    // page with an unhandled rejection — render an explicit error state.
    o = null;
  }
  if (!o) {
    state = { status: 'error', error: 'Não foi possível carregar os dados. Tente de novo.', errorCode: 'network' };
    settings = {};
    followers = {};
    following = {};
    history = {};
    events = [];
    newFollowers = [];
    render();
    return;
  }
  state = o['igf.state'] || { status: 'idle' };
  settings = o['igf.settings'] || {};
  followers = o['igf.followers'] || {};
  following = o['igf.following'] || {};
  history = o['igf.followHistory'] || {};
  events = enrichEvents(o['igf.unfollowEvents'] || []);
  newFollowers = enrichEvents(o['igf.newFollowerEvents'] || []);
  render();
  // Popup + panel: kick sync when idle and data is missing or stale.
  if (state.status !== 'syncing') {
    const staleMs = (settings.refreshMinutes || 60) * 60 * 1000;
    const empty = !Object.keys(followers).length && !Object.keys(following).length;
    const stale = settings.consentAt && (!state.lastSyncAt || Date.now() - new Date(state.lastSyncAt).getTime() > staleMs);
    if (empty || stale) sendSync(); // manual trigger grants consent on first open
  }
  // Announce the dashboard is live (the injected panel listens; harmless
  // when this page runs as the toolbar popup — posting to self, no receiver).
  try {
    parent.postMessage({ type: 'igf-panel-ready' }, '*');
  } catch { /* never break the UI */ }
}

// --- events ----------------------------------------------------------------
el.refresh().onclick = sendSync;
el.cardK().onclick = () => { tab = TABS.nonFollowers; shown = 0; invalidateListCaches(); render(); };
el.cardF().onclick = () => { tab = TABS.fans; shown = 0; invalidateListCaches(); render(); };
el.cardM().onclick = () => { tab = TABS.nonFollowers; shown = 0; invalidateListCaches(); render(); };
el.search().addEventListener('input', (e) => { query = e.target.value; shown = 0; invalidateListCaches(); renderHeavy(); });
el.interval().addEventListener('change', async (e) => {
  // SW restarting / popup closing mid-call: swallow, never an unhandled rejection.
  await chrome.runtime.sendMessage({
    type: 'igf-settings-update',
    settings: { refreshMinutes: Number(e.target.value) },
  }).catch(() => {});
});
el.notif().addEventListener('change', async (e) => {
  await chrome.runtime.sendMessage({
    type: 'igf-settings-update',
    settings: { notificationsEnabled: e.target.checked },
  }).catch(() => {});
});
el.openIg().addEventListener('click', (e) => { e.preventDefault(); openInstagram(); });

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
  if (changes['igf.state']) {
    const newState = changes['igf.state'].newValue || { status: 'idle' };
    const ownKey = `${newState.ownUserId || ''}:${newState.ownUsername || ''}`;
    if (lastOwnKey && ownKey !== lastOwnKey) {
      shown = 0;
      query = '';
      const search = el.search();
      if (search) search.value = '';
    }
    lastOwnKey = ownKey;
    state = newState;
  }
  if (changes['igf.settings']) settings = changes['igf.settings'].newValue || {};
  if (changes['igf.followers']) { followers = changes['igf.followers'].newValue || {}; invalidateListCaches(); }
  if (changes['igf.following']) { following = changes['igf.following'].newValue || {}; invalidateListCaches(); }
  if (changes['igf.followHistory']) history = changes['igf.followHistory'].newValue || {};
  if (changes['igf.unfollowEvents']) events = enrichEvents(changes['igf.unfollowEvents'].newValue || []);
  else if (changes['igf.followers'] || changes['igf.following'] || changes['igf.followHistory']) events = enrichEvents(events);
  if (changes['igf.newFollowerEvents']) newFollowers = enrichEvents(changes['igf.newFollowerEvents'].newValue || []);
  else if (changes['igf.followers'] || changes['igf.following'] || changes['igf.followHistory']) newFollowers = enrichEvents(newFollowers);
  render();
});

const exportBackupBtn = $('export-backup');
if (exportBackupBtn) exportBackupBtn.addEventListener('click', () => exportBackupFile());

const importBackupInput = $('import-backup');
if (importBackupInput) {
  importBackupInput.addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    await importBackupFile(file);
  });
}

const deleteBtn = $('delete-data');
if (deleteBtn) {
  deleteBtn.addEventListener('click', async () => {
    if (!confirm('Apagar todos os dados do IG FollowGuard neste navegador?')) return;
    await chrome.runtime.sendMessage({ type: 'igf-delete-all' }).catch(() => {});
    await load();
  });
}


export { esc, itemHtml };
if (!globalThis.__IGF_SKIP_UI_BOOT__) {
  load().catch(() => {
    // Boot last resort: something blew up beyond load()'s own catch — surface
    // it in the UI instead of dying with an unhandled rejection.
    state = { status: 'error', error: 'Não foi possível carregar o painel. Tente de novo.', errorCode: 'network' };
    try { render(); } catch { /* DOM not ready — nothing more we can do */ }
  });
}
