// IG FollowGuard — in-page dashboard on instagram.com.
// Shows ONLY on the logged-in user's OWN profile (resolved at runtime from the
// session — never hardcoded), anchored next to the header settings gear.
// The floating button opens the FollowGuard panel (extension iframe inside a
// shadow root — full isolation from the page).
'use strict';

(() => {
  if (window.__igfContent) return; // SPA: never duplicate on re-injection
  window.__igfContent = true;

  let own = null;        // { username, uid } resolved from the SW (session)
  let host = null;       // injected host element (FAB + panel shadow)
  let panel = null;
  let panelReady = false;
  let lastPath = null;

  const isOwnProfilePath = (pathname) => {
    if (!own || !own.username) return false;
    const p = pathname.replace(/\/+$/, '').toLowerCase();
    return p === '/' + String(own.username).toLowerCase();
  };

  const setBadge = (count) => {
    if (!host || !host.shadowRoot) return;
    const fab = host.shadowRoot.querySelector('button');
    if (fab) {
      const label = `IG FollowGuard — ${count} não seguem de volta`;
      fab.title = label;
      fab.setAttribute('aria-label', label);
    }
  };

  const refreshBadge = async () => {
    if (!host) return;
    try {
      const o = await chrome.storage.local.get('igf.state');
      const st = o['igf.state'] || {};
      setBadge(typeof st.notFollowingBackCount === 'number' ? st.notFollowingBackCount : '–');
    } catch { /* badge is best-effort */ }
  };

  const resolveOwn = async () => {
    try {
      const r = await chrome.runtime.sendMessage({ type: 'igf-get-own' });
      if (r && r.ok && r.username) return { username: r.username, uid: r.uid || null };
    } catch { /* SW unreachable (e.g. mid-reload); retried on the timer */ }
    return null;
  };

  // Own-profile header settings gear — user-provided structural hint: the div
  // that holds the "opções" gear next to "Editar perfil".
  const GEAR_XPATH =
    '/html/body/div[1]/div/div/div[2]/div/div/div[1]/div[2]/div[2]/section/main/div/div/header/div/section[2]/div[1]/div[1]/div[2]';

  const findGearDiv = () => {
    try {
      const node = document.evaluate(GEAR_XPATH, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
      if (node && node.getBoundingClientRect) {
        const r = node.getBoundingClientRect();
        if (r.width || r.height) return node;
      }
    } catch { /* layout changed — fall through */ }
    // Fallback: the gear's own label (PT-BR "opções"; never "Configurações").
    const gear = document.querySelector('[aria-label="opções"], [aria-label="Opções"]');
    if (gear) {
      const r = gear.getBoundingClientRect();
      if (r.width || r.height) return gear;
    }
    return null;
  };

  // Anchor the FAB to the right of the settings gear (12px gap).
  const FAB_SIZE = 40;
  const positionNearSettings = () => {
    if (!host) return false;
    const gearDiv = findGearDiv();
    if (gearDiv) {
      const r = gearDiv.getBoundingClientRect();
      // FAB's left edge = gear's right edge + 12px.
      const right = Math.max(8, Math.round(innerWidth - r.right - 12 - FAB_SIZE));
      const top = Math.max(8, Math.round(r.top + (r.height - FAB_SIZE) / 2));
      host.style.right = right + 'px';
      host.style.top = top + 'px';
      return true;
    }
    return false;
  };

  const buildFab = () => {
    host = document.createElement('div');
    host.id = 'igf-root';
    host.style.cssText = 'all:initial;position:fixed;right:24px;top:150px;z-index:2147483647;';
    const shadow = host.attachShadow({ mode: 'open' });

    const fab = document.createElement('button');
    fab.type = 'button';
    fab.title = 'IG FollowGuard — quem não te segue de volta';
    fab.setAttribute('aria-label', fab.title);
    fab.style.cssText =
      'all:initial;display:block;width:40px;height:40px;padding:0;border:0;border-radius:50%;' +
      'cursor:pointer;background:linear-gradient(135deg,#feda75,#d62976,#962fbf,#4f5bd5);' +
      'box-shadow:0 3px 12px rgba(0,0,0,.45);';

    const icon = document.createElement('img');
    icon.src = chrome.runtime.getURL('images/icon128.png');
    icon.alt = '';
    icon.style.cssText = 'all:initial;display:block;width:36px;height:36px;border-radius:50%;margin:2px;';
    fab.appendChild(icon);

    const togglePanel = () => {
      if (!panel) {
        panel = document.createElement('iframe');
        panel.src = chrome.runtime.getURL('panel.html');
        panel.title = 'IG FollowGuard';
        panel.style.cssText =
          'all:initial;position:fixed;width:372px;height:560px;border:1px solid #2c2f35;' +
          'border-radius:12px;background:#121316;box-shadow:0 10px 40px rgba(0,0,0,.55);' +
          'z-index:2147483647;';
        // Open below the FAB, sharing its right edge.
        const top = host.style.top ? parseInt(host.style.top, 10) : 150;
        panel.style.top = (top + FAB_SIZE + 12) + 'px';
        panel.style.right = (host.style.right ? parseInt(host.style.right, 10) : 24) + 'px';
        shadow.appendChild(panel);
      } else {
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
      }
    };
    fab.addEventListener('click', togglePanel);

    shadow.appendChild(fab);
    (document.body || document.documentElement).appendChild(host);
    refreshBadge();
    // Anchor next to the gear once the header renders (retry briefly).
    let tries = 0;
    const tryAnchor = () => {
      if (positionNearSettings() || ++tries > 20) return;
      setTimeout(tryAnchor, 250);
    };
    tryAnchor();
  };

  const removeFab = () => {
    if (host) {
      host.remove();
      host = null;
    }
    panel = null;
    panelReady = false;
  };

  const tick = () => {
    const p = location.pathname;
    if (p === lastPath) return;
    lastPath = p;
    if (isOwnProfilePath(p)) {
      if (!host) buildFab();
    } else {
      removeFab();
    }
  };

  const adoptOwn = (r) => {
    if (r) {
      own = r;
      tick();
    }
  };

  window.addEventListener('message', (ev) => {
    if (!ev.data || !host || !panel) return;
    if (ev.data.type === 'igf-close-panel') {
      panel.style.display = 'none';
    } else if (ev.data.type === 'igf-panel-ready' && !panelReady) {
      panelReady = true;
      try {
        const fab = host.shadowRoot && host.shadowRoot.querySelector('button');
        if (fab) {
          const label = `${fab.title} · painel OK`;
          fab.title = label;
          fab.setAttribute('aria-label', label);
        }
      } catch { /* best-effort */ }
    }
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes['igf.state']) {
      refreshBadge();
      if (!own) resolveOwn().then(adoptOwn);
    }
  });

  (async () => {
    own = await resolveOwn();
    tick();
    setInterval(tick, 750); // SPA navigation watcher (no reloads on IG)
    setInterval(() => { if (!own) resolveOwn().then(adoptOwn); }, 5000);
    setInterval(refreshBadge, 30000);
  })();
})();
