// IG FollowGuard — in-page dashboard on instagram.com.
// Floating button (bottom-right) opens the FollowGuard panel (extension
// iframe inside a shadow root — full isolation from the page).
'use strict';

(() => {
  if (document.getElementById('igf-root')) return;

  const host = document.createElement('div');
  host.id = 'igf-root';
  host.style.cssText = 'all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;';

  const shadow = host.attachShadow({ mode: 'open' });

  const fab = document.createElement('button');
  fab.type = 'button';
  fab.title = 'IG FollowGuard — quem não te segue de volta';
  fab.setAttribute('aria-label', fab.title);
  fab.style.cssText =
    'all:initial;display:block;width:52px;height:52px;padding:0;border:0;border-radius:50%;' +
    'cursor:pointer;background:linear-gradient(135deg,#feda75,#d62976,#962fbf,#4f5bd5);' +
    'box-shadow:0 4px 16px rgba(0,0,0,.45);';

  const icon = document.createElement('img');
  icon.src = chrome.runtime.getURL('images/icon128.png');
  icon.alt = '';
  icon.style.cssText = 'all:initial;display:block;width:52px;height:52px;border-radius:50%;';
  fab.appendChild(icon);

  let panel = null;
  let panelReady = false;

  const setBadge = (count) => {
    const label = `IG FollowGuard — ${count} não seguem de volta`;
    fab.title = label;
    fab.setAttribute('aria-label', label);
  };
  // Live badge: read the latest state (content scripts may use chrome.storage).
  const refreshBadge = async () => {
    try {
      const o = await chrome.storage.local.get('igf.state');
      const st = o['igf.state'] || {};
      setBadge(typeof st.notFollowingBackCount === 'number' ? st.notFollowingBackCount : '–');
    } catch { /* badge is best-effort */ }
  };
  refreshBadge();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes['igf.state']) refreshBadge();
  });

  const togglePanel = () => {
    if (!panel) {
      panel = document.createElement('iframe');
      panel.src = chrome.runtime.getURL('panel.html');
      panel.title = 'IG FollowGuard';
      panel.style.cssText =
        'all:initial;position:fixed;right:18px;bottom:78px;width:372px;height:560px;' +
        'border:1px solid #2c2f35;border-radius:12px;background:#121316;' +
        'box-shadow:0 10px 40px rgba(0,0,0,.55);z-index:2147483647;';
      shadow.appendChild(panel);
    } else {
      panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    }
  };
  fab.addEventListener('click', togglePanel);

  window.addEventListener('message', (ev) => {
    if (!ev.data) return;
    if (ev.data.type === 'igf-close-panel' && panel) {
      panel.style.display = 'none';
    } else if (ev.data.type === 'igf-panel-ready' && !panelReady) {
      panelReady = true;
      try {
        const label = `${fab.title} · painel OK`;
        fab.title = label;
        fab.setAttribute('aria-label', label);
      } catch { /* best-effort */ }
    }
  });

  shadow.appendChild(fab);
  (document.body || document.documentElement).appendChild(host);
})();
