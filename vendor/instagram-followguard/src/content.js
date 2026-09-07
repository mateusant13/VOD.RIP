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
  let lastPath = null;
  let syncBanner = null;
  let lastGearRect = null; // gear viewport rect at last successful mount

  const showSyncBanner = () => {
    if (!syncBanner) {
      syncBanner = document.createElement('div');
      syncBanner.id = 'igf-sync-banner';
      syncBanner.setAttribute('role', 'status');
      syncBanner.style.cssText =
        'position:fixed;top:0;left:0;right:0;z-index:2147483646;padding:10px 14px;' +
        'background:linear-gradient(90deg,#78350f,#92400e);color:#fef3c7;' +
        'font:600 12px/1.45 system-ui,sans-serif;text-align:center;' +
        'box-shadow:0 2px 12px rgba(0,0,0,.45);pointer-events:none;';
      syncBanner.innerHTML =
        '<strong style="color:#fde68a">Não feche esta aba do Instagram</strong> — ' +
        'o FollowGuard está sincronizando. Pode ficar em segundo plano.';
      (document.body || document.documentElement).appendChild(syncBanner);
    }
    syncBanner.style.display = 'block';
  };

  const hideSyncBanner = () => {
    if (syncBanner) syncBanner.style.display = 'none';
  };

  const isOwnProfilePath = (pathname) => {
    if (!own || !own.username) return false;
    const p = pathname.replace(/\/+$/, '').toLowerCase();
    return p === '/' + String(own.username).toLowerCase();
  };

  const setBadge = (count) => {
    if (!host || !host.shadowRoot) return;
    const fab = host.shadowRoot.querySelector('button');
    if (fab) {
      // '–' = never synced: never claim "0 não seguem de volta" without data.
      const label = count === '–'
        ? 'IG FollowGuard — sincronize para ver quem não te segue de volta'
        : `IG FollowGuard — ${count} não seguem de volta`;
      fab.title = label;
      fab.setAttribute('aria-label', label);
    }
  };

  const refreshBadge = async () => {
    if (!host) return;
    try {
      const o = await chrome.storage.local.get('igf.state');
      const st = o['igf.state'] || {};
      setBadge(st.lastSyncAt && typeof st.notFollowingBackCount === 'number' ? st.notFollowingBackCount : '–');
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
    // Fallback: the gear by its label. Exact PT-BR labels first, then any
    // button-like element whose aria-label reads as an options/settings entry
    // (localized variants: "opções", "Configurações", "Settings", ...).
    const candidates = document.querySelectorAll(
      '[aria-label="opções"], [aria-label="Opções"], [aria-label="Configurações"], [role=button][aria-label]',
    );
    const gear = Array.from(candidates).find((n) =>
      /opç|option|settings|config/i.test(n.getAttribute('aria-label') || ''));
    if (gear) {
      const r = gear.getBoundingClientRect();
      if (r.width || r.height) return gear;
    }
    return null;
  };

  // Anchor the FAB to the right of the settings gear (12px gap).
  const FAB_SIZE = 40;
  const mountHostNearGear = () => {
    if (!host) return false;
    const gearDiv = findGearDiv();
    if (!gearDiv || !gearDiv.parentElement) return false;
    const parent = gearDiv.parentElement;
    if (host.parentElement !== parent) {
      parent.insertBefore(host, gearDiv.nextSibling);
    }
    host.style.cssText =
      'all:initial;display:inline-flex;align-items:center;vertical-align:middle;' +
      'margin-left:12px;position:relative;z-index:2;flex:none;';
    lastGearRect = gearDiv.getBoundingClientRect();
    return true;
  };

  const positionPanelNearFab = (fab) => {
    if (!panel || !fab) return;
    const r = fab.getBoundingClientRect();
    const w = Math.min(372, Math.max(280, innerWidth - 16));
    const h = Math.min(560, Math.max(320, innerHeight - r.bottom - 20));
    panel.style.width = w + 'px';
    panel.style.height = h + 'px';
    panel.style.top = Math.min(r.bottom + 12, innerHeight - h - 8) + 'px';
    panel.style.right = Math.max(8, innerWidth - r.right) + 'px';
    panel.style.left = 'auto';
  };

  const buildFab = () => {
    host = document.createElement('div');
    host.id = 'igf-root';
    host.style.cssText = 'all:initial;display:inline-flex;align-items:center;margin-left:12px;position:relative;z-index:2;';
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
          'all:initial;position:fixed;border:1px solid #2c2f35;' +
          'border-radius:12px;background:#121316;box-shadow:0 10px 40px rgba(0,0,0,.55);' +
          'z-index:2147483647;';
        document.body.appendChild(panel);
        positionPanelNearFab(fab);
      } else {
        const opening = panel.style.display === 'none';
        panel.style.display = opening ? 'block' : 'none';
        if (opening) positionPanelNearFab(fab);
      }
    };
    fab.addEventListener('click', togglePanel);

    shadow.appendChild(fab);
    refreshBadge();
    // Mount in the profile header (scrolls with the page — not viewport-sticky).
    let tries = 0;
    const tryAnchor = () => {
      if (mountHostNearGear() || ++tries > 20) return;
      setTimeout(tryAnchor, 250);
    };
    tryAnchor();
  };

  const removeFab = () => {
    if (host) {
      host.remove();
      host = null;
    }
    if (panel) {
      panel.remove();
      panel = null;
    }
  };

  // True when the anchor gear vanished or its viewport rect moved >2px since
  // the last mount (scroll/resize/SPA re-layout). Cheap: one XPath probe.
  const gearRectMoved = () => {
    const gear = findGearDiv();
    if (!gear) return false; // gear gone entirely — leave the FAB where it is
    const r = gear.getBoundingClientRect();
    const moved = lastGearRect &&
      (Math.abs(r.left - lastGearRect.left) > 2 || Math.abs(r.top - lastGearRect.top) > 2);
    lastGearRect = r;
    return !!moved;
  };

  const tick = () => {
    const p = location.pathname;
    if (isOwnProfilePath(p)) {
      if (!host) {
        buildFab();
      } else if (p !== lastPath || gearRectMoved()) {
        mountHostNearGear(); // SPA nav or layout shift: re-anchor next to the gear
      }
    } else if (host) {
      removeFab();
    }
    lastPath = p;
  };

  const adoptOwn = (r) => {
    if (r) {
      own = r;
      tick();
    }
  };

  const extOrigin = `chrome-extension://${chrome.runtime.id}`;
  window.addEventListener('message', (ev) => {
    if (ev.origin !== extOrigin) return;
    if (!ev.data || !host || !panel) return;
    if (ev.data.type === 'igf-close-panel' && panel) panel.style.display = 'none';
    // 'igf-panel-ready' needs no handling: the FAB title stays clean (the old
    // " · painel OK" suffix was pure screen-reader noise).
  });

  // Re-anchor on viewport resize — the profile header reflows with the width.
  window.addEventListener('resize', () => {
    if (host) mountHostNearGear();
  });

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.igf !== 'sync-hint') return;
    if (msg.active) showSyncBanner();
    else hideSyncBanner();
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
    // Retry own-resolution with CAPPED exponential backoff (5s → 60s max),
    // never a fixed 5s loop — a fixed-cadence request stream while the user
    // is unresolved is itself a fingerprint, and hammering a gate deepens it.
    let ownRetryMs = 5000;
    const scheduleOwnRetry = () => {
      if (own) return;
      setTimeout(async () => {
        if (own) return;
        let r = null;
        try { r = await resolveOwn(); } catch { r = null; }
        if (r) {
          ownRetryMs = 5000;
          adoptOwn(r);
          return;
        }
        ownRetryMs = Math.min(ownRetryMs * 2, 60000);
        scheduleOwnRetry();
      }, ownRetryMs);
    };
    scheduleOwnRetry();
    setInterval(refreshBadge, 30000);
  })();
})();
