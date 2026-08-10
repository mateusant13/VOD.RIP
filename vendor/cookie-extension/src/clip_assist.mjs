// VOD.RIP clip assistant — content script for Twitch pages.
//
// The VOD.RIP app opens a Twitch URL carrying vodrip_* query params
// (vodrip_clip=1 + vodrip_start/end/title). Two flows:
//   - clips.twitch.tv/create?vodID=...&offsetSeconds=... — the legacy editor
//     URL opens Twitch's clip editor DIRECTLY (logged in); this script fills
//     the title and clicks Save Clip.
//   - twitch.tv/videos/<id>?t=... — this script waits for the player, clicks
//     its Clip button to open the editor overlay, fills the title and clicks
//     Publish.
// Everything runs inside Twitch's own page, so the editor's GQL mutation uses
// the session cookie + integrity the site itself generates — no API token
// scopes needed (a browser-login token never carries clip scopes anyway).
//
// ponytail: the editor's DOM is Twitch's private React tree and changes without
// notice; every selector below is a candidate list with graceful fallbacks
// (status panel tells the user what to finish by hand). Upgrade path: drive
// the range handles precisely once the live editor DOM is confirmed; until
// then the site's default clip window around offsetSeconds is used, and the
// panel shows the requested start → end.
(async () => {
  if (window.top !== window) return; // the player lives in the top frame

  // clips.twitch.tv/create SPA-redirects (e.g. to /clips/500) and drops the
  // query — stash the params at document_start so the flow below can still
  // read them after the redirect.
  if (location.hostname === 'clips.twitch.tv' && /vodrip_(clip|diag)=1/.test(location.search)) {
    try {
      sessionStorage.setItem('vodrip_clip_params', location.search);
    } catch { /* storage blocked */ }
  }

  const rawSearch = location.hostname === 'clips.twitch.tv'
    ? (sessionStorage.getItem('vodrip_clip_params') || location.search)
    : location.search;
  const params = new URLSearchParams(rawSearch.replace(/^\?/, ''));
  if (params.get('vodrip_diag') !== '1' && params.get('vodrip_clip') !== '1') return;

  const startSec = Math.max(0, Math.floor(Number(params.get('vodrip_start')) || 0));
  const endSec = Math.max(startSec, Math.floor(Number(params.get('vodrip_end')) || 0));
  // VOD total length (the app sends vodrip_dur) — lets the editor-range
  // helper nudge the window off the VOD's last frame instead of failing.
  const durSec = Number(params.get('vodrip_dur')) || 0;

  // document_start on clips.twitch.tv may run before <html> exists.
  if (!document.documentElement) {
    await new Promise((resolve) =>
      document.addEventListener('DOMContentLoaded', resolve, { once: true }),
    );
  }

  const panel = document.createElement('div');
  panel.setAttribute('data-vodrip-clip-assist', '');
  Object.assign(panel.style, {
    position: 'fixed',
    top: '16px',
    right: '16px',
    zIndex: '2147483647',
    background: 'rgba(10,10,14,0.94)',
    color: '#f4f4f5',
    font: '12px/1.5 Consolas, monospace',
    padding: '10px 12px',
    borderRadius: '6px',
    border: '1px solid #9146FF',
    boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
    maxWidth: '340px',
    whiteSpace: 'pre-wrap',
  });
  document.documentElement.appendChild(panel);

  const setStatus = (text, kind = 'info') => {
    panel.style.borderColor =
      kind === 'ok' ? '#53fc18' : kind === 'err' ? '#f87171' : '#9146FF';
    panel.textContent = `VOD.RIP clip\n${text}`;
  };

  const fmtHms = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    const m = Math.floor(s / 60);
    const r = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return `${m}:${pad(r)}`;
  };
  // No user title -> the VOD/live title (twitch.tv/videos pages set
  // document.title to "<stream title> - Twitch" server-side). NEVER a
  // "VOD.RIP …" default — the user requires the live's title verbatim.
  const title =
    (params.get('vodrip_title') || '').trim() ||
    (document.title || '').replace(/\s*-\s*Twitch\s*$/i, '').trim() ||
    'Clip';
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  // Debugging event sequence: every flow step is POSTed to the app's
  // clip-events sink (same API base as the cookie bridge) so a clip attempt
  // can be replayed end to end from the app log. Fire-and-forget — logging
  // must never break the flow. The dynamic import stays lazy so a bridge
  // module failure can't take the clip flow down with it.
  const note = (event, data = {}) => {
    try {
      import('./modules/cookie_bridge.mjs')
        .then(({ getApiBase }) => getApiBase())
        .then((base) =>
          fetch(base + '/api/debug/clip-events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ src: 'ext', event, data }),
          }).catch(() => {}),
        )
        .catch(() => {});
    } catch { /* ignore */ }
  };
  // The published clip is recorded into the app's clip history (so the app
  // shows a download button for it). Fire-and-forget, same posture as note();
  // Twitch's site published it, the backend never saw it — the browser
  // path's equivalent of a server-side history write.
  const recordPublishedClip = (clipUrl) => {
    try {
      const path = (() => {
        try { return new URL(clipUrl).pathname; } catch { return ''; }
      })();
      // Only a /<login>/clip/<slug> URL carries the channel; a /videos/N URL
      // has none (split[1] would be 'videos'), so fall back to the param.
      const ch = params.get('broadcasterLogin') || (() => {
        const segs = path.split('/').filter(Boolean);
        const clipIdx = segs.indexOf('clip');
        return clipIdx > 0 ? segs[clipIdx - 1] : undefined;
      })();
      const vodMatch = (location.pathname || '').match(/^\/videos\/(\d+)/);
      const start = Number(params.get('vodrip_start')) || 0;
      const end = Number(params.get('vodrip_end')) || 0;
      import('./modules/cookie_bridge.mjs')
        .then(({ getApiBase }) => getApiBase())
        .then((base) =>
          fetch(base + '/api/twitch/clips/record', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              url: clipUrl,
              title,
              channel: ch,
              vod_id: params.get('vodID') || (vodMatch ? vodMatch[1] : undefined),
              offset_sec: end > 0 ? Math.floor(end) : undefined,
              duration_sec: end > start ? Math.round(end - start) : undefined,
            }),
          }).catch(() => {}),
        )
        .catch(() => {});
    } catch { /* ignore */ }
  };
  // User window rule: after the flow ends (success OR failure) the editor
  // tab closes itself. The BACKGROUND holds the delay: content-script
  // timers freeze in hidden/throttled tabs (Chrome Memory Saver), so the
  // close message is sent immediately and the service worker waits.
  // vodrip_close=0 keeps the tab open. Default: close.
  const closeAfterFlow = (delayMs = 1200) => {
    note('ext_close', {
      mode: (params.get('vodrip_close') || '1') === '0' ? 'stay-open' : 'close',
      delayMs,
    });
    if ((params.get('vodrip_close') || '1') === '0') {
      // Stay-open mode: still stop the preview video so the tab is idle
      // (the looping editor preview was the GPU hog).
      try { document.querySelector('video')?.pause(); } catch { /* ignore */ }
      return;
    }
    try {
      const p = chrome.runtime.sendMessage({ type: 'vodrip-close-tab', delayMs });
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch {
      try { window.close(); } catch { /* ignore */ }
    }
  };
  note('ext_start', {
    hostname: location.hostname,
    startSec,
    endSec,
    title,
    diag: params.get('vodrip_diag') === '1',
    closeMode: (params.get('vodrip_close') || '1') === '0' ? 'stay-open' : 'close',
  });
  // React-fiber access lives in the page's MAIN world: content scripts
  // (isolated world) CANNOT see the __reactFiber$ expando React puts on DOM
  // nodes, and inline script tags get blocked by the page CSP/Trusted Types
  // (proven live 2026-08-09). The background injects the helper via
  // chrome.scripting with world:'MAIN' — it drives the fiber drag + poll
  // and posts the result back (vodrip-range-res).
  const injectMainHelper = async () => {
    if (window.__vodripMainInjected) return 'already';
    window.__vodripMainInjected = true;
    try { await chrome.runtime.sendMessage({ type: 'vodrip-inject-main' }).catch(() => {}); } catch { /* ignore */ }
    try {
      const m = await chrome.storage.local.get('vodrip_sw_inject_msg');
      return m.vodrip_sw_inject_msg ? 'delivered' : 'not-delivered';
    } catch {
      return 'storage-unreadable';
    }
  };
  const rangeViaMainWorld = (startSec, endSec, durSec) =>
    new Promise((resolve) => {
      const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
      const onMsg = (ev) => {
        const d = ev.data;
        if (d && d.source === 'vodrip-range-res' && d.nonce === nonce) {
          clearTimeout(timer);
          window.removeEventListener('message', onMsg);
          resolve(d);
        }
      };
      const timer = setTimeout(() => {
        window.removeEventListener('message', onMsg);
        resolve({ ok: false, reason: 'helper não respondeu (injeção MAIN falhou?)' });
      }, 12000);
      window.addEventListener('message', onMsg);
      try {
        window.postMessage({ source: 'vodrip-range-req', nonce, start: startSec, end: endSec, dur: durSec }, '*');
      } catch (err) {
        clearTimeout(timer);
        window.removeEventListener('message', onMsg);
        resolve({ ok: false, reason: String(err) });
      }
    });
  async function waitFor(fn, timeoutMs, intervalMs = 500) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const value = fn();
      if (value) return value;
      if (Date.now() > deadline) return null;
      await sleep(intervalMs);
    }
  }
  /** Set a React-controlled input's value the way the site's own handlers see it. */
  const setReactValue = (el, value) => {
    const proto =
      el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const click = (el) => {
    el.click();
    return el;
  };
  const find = (selectors) => {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      if (el) return el;
    }
    return null;
  };

  /** Parse an editor clock "3:20" (m:ss) or "1:02:05" (h:mm:ss) → seconds. */
  const parseClock = (s) => {
    const m = String(s || '').trim().match(/^(\d+):(\d{1,2})(?::(\d{1,2}))?$/);
    if (!m) return null;
    return +m[1] * 60 + +m[2] + (+m[3] || 0);
  };

  /**
   * Drive the clip editor's REAL window control. The editor has NO start/end
   * time inputs (proven on the live page 2026-08-09): the clip window is a
   * draggable slider (`[role=slider]`, aria-valuetext "3:00 to 3:20") whose
   * React internals expose onLeftDrag/onRightDrag callbacks that take
   * {startOffset, endOffset} — the same contract a real handle drag fires.
   * We invoke the callbacks, then poll the slider's valuetext to report what
   * the editor ACTUALLY accepted (it clamps to the VOD + the site's
   * 5..60s window length limits). No seek: the callbacks set the window
   * offsets directly (the editor accepted a 500–520s window while its
   * aria-valuemax was still 95.3). Returns '' on confirmed success, else a
   * human-readable reason (the caller refuses to save on failure).
   */
  const setEditorRange = async (scope, startSec, endSec, durSec) => {
    const len = endSec - startSec;
    // Twitch's own hard rule (the user's ground truth): the editor only
    // accepts 5..60s windows, and its slider spans at most ~1:30 around the
    // anchor. Refuse early instead of fighting the editor for a doomed range.
    if (len < 5 || len > 60) {
      note('ext_range_refused', { startSec, endSec, len });
      setStatus(
        `Trecho de ${Math.round(len)}s fora do limite da Twitch (5..60s) — ajuste o clipe no app e tente de novo.`,
        'err',
      );
      return 'fora-do-limite';
    }
    // The slider renders late (after the title input/dialog) — 45s budget.
    const slider = await waitFor(
      () => (scope || document).querySelector('[role="slider"]'),
      45000,
      700,
    );
    if (!slider) return 'controle do editor (slider) não encontrado';
    // The fiber drag must run in the page's MAIN world (the isolated world
    // cannot see the __reactFiber$ expando); the helper polls the
    // valuetext and reports what the editor ACTUALLY accepted.
    const res = await rangeViaMainWorld(startSec, endSec, durSec);
    note('ext_range', {
      ok: !!res.ok,
      targetStart: startSec,
      targetEnd: endSec,
      durSec,
      valuetext: res.ok ? (res.valuetext || null) : null,
      reason: res.ok ? null : (res.reason || null),
    });
    if (!res.ok) {
      const reason = res.reason || 'falha';
      setStatus(
        `Clique de ${Math.round(len)}s (de ${fmtHms(startSec)} a ${fmtHms(endSec)}) NÃO posicionado no editor (${reason}) — nada foi salvo. Ajuste o trecho no editor e salve você mesmo.`,
        'err',
      );
      return reason;
    }
    setStatus(
      `Clique de ${Math.round(len)}s (de ${fmtHms(startSec)} a ${fmtHms(endSec)}) confirmado no editor (${res.valuetext}).`,
      'ok',
    );
    return '';
  };

  // vodrip_diag=1 — census the editor DOM for selector maintenance, then
  // stop (never auto-publishes). Panel text is read back via UIA.
  if (params.get('vodrip_diag') === '1') {
    (async () => {
      const injectResult = await injectMainHelper();
      // Wait for the REAL window control: the editor renders [role=slider]
      // well after the title input/dialog appear; probing early reports
      // everything missing (seen live 2026-08-09).
      await waitFor(
        () => document.querySelector('[role="slider"]'),
        45000,
        1000,
      );
      const census = [];
      census.push('inject-result: ' + injectResult);
      const probe = (label, sel) => {
        const el = document.querySelector(sel);
        census.push(`${label}: ${el ? 'FOUND value=' + (el.value != null ? JSON.stringify(el.value) : '(no value)') : 'missing'}`);
      };
      probe('title-input', 'input[data-a-target="clip-editor-title-input"], input[data-a-target="tw-input"]');
      const inputDump = [...document.querySelectorAll('input, textarea')]
        .slice(0, 12)
        .map((i) => {
          const attrs = [];
          ['id', 'name', 'aria-label', 'aria-labelledby', 'placeholder', 'data-a-target', 'class'].forEach((a) => {
            const v = i.getAttribute(a);
            if (v) attrs.push(a + '=' + JSON.stringify(v.slice(0, 40)));
          });
          return '<' + i.tagName.toLowerCase() + (i.type ? ' type=' + i.type : '') + (attrs.length ? ' ' + attrs.join(' ') : '') + '>';
        });
      census.push('inputs: ' + (inputDump.join(' | ') || '(none)'));
      const targets = [...document.querySelectorAll('[data-a-target]')]
        .map((el) => el.getAttribute('data-a-target'))
        .filter((t) => t && /clip|editor|range|handle|start|end|time|title|slider/i.test(t))
        .slice(0, 30);
      census.push('targets: ' + (targets.join(' | ') || '(none)'));
      const handles = [...document.querySelectorAll('[class*="handle" i], [class*="range" i], [class*="timeline" i], [class*="scrubber" i]')]
        .slice(0, 12)
        .map((el) => String(el.className).slice(0, 90));
      census.push('handles: ' + (handles.join(' | ') || '(none)'));
      const roles = [...document.querySelectorAll('[role]')]
        .map((el) => el.getAttribute('role'))
        .filter((r) => r && r !== 'dialog')
        .slice(0, 12);
      census.push('roles: ' + (roles.join(' | ') || '(none)'));
      const rangeEl = document.querySelector('[class*="ScRange" i]');
      census.push('range-el: ' + (rangeEl ? rangeEl.outerHTML.slice(0, 400) : '(none)'));
      const rangeInput = document.querySelector('[class*="tw-range" i], input[type="range"]');
      census.push('range-input: ' + (rangeInput ? rangeInput.outerHTML.slice(0, 300) : '(none)'));
      const timeEl = [...document.querySelectorAll('*')].find(
        (el) => /^\s*\d+:\d{2}\s*\/\s*\d+:\d{2}\s*$/.test((el.textContent || '')) && el.children.length <= 4,
      );
      census.push('time-el: ' + (timeEl ? timeEl.outerHTML.slice(0, 300) : '(none)'));
      const timeParent = timeEl && timeEl.parentElement;
      census.push('time-parent: ' + (timeParent ? timeParent.outerHTML.slice(0, 300) : '(none)'));
      const timeGp = timeParent && timeParent.parentElement;
      census.push('time-grandparent: ' + (timeGp ? timeGp.outerHTML.slice(0, 300) : '(none)'));
      const styled = [...document.querySelectorAll('[style]')]
        .filter((el) => /left|transform|width|margin/i.test(el.getAttribute('style') || ''))
        .slice(0, 10)
        .map((el) => String(el.className).slice(0, 70) + ' => ' + String(el.getAttribute('style')).slice(0, 70));
      census.push('styled: ' + (styled.join(' | ') || '(none)'));
      const btns = [...document.querySelectorAll('button')]
        .slice(0, 25)
        .map((b) => (b.getAttribute('aria-label') || b.innerText || '').trim().slice(0, 40))
        .filter(Boolean);
      census.push('buttons: ' + (btns.join(' | ') || '(none)'));
      const slider = document.querySelector('[role="slider"]');
      census.push(
        'slider: ' +
          (slider
            ? 'FOUND valuetext=' + (slider.getAttribute('aria-valuetext') || '?') + ' now=' + (slider.getAttribute('aria-valuenow') || '?')
            : 'missing'),
      );
      let hasDrag = false;
      if (slider) {
        const fiberKey2 = Object.keys(slider).find((k) => k.startsWith('__reactFiber'));
        census.push('fiber-keys: ' + (fiberKey2 || '(none)'));
        if (fiberKey2) {
          let n = slider[fiberKey2];
          const propKeys = new Set();
          for (let i = 0; n && i < 40; i++) {
            const p = n.memoizedProps || {};
            Object.keys(p).forEach((k) => propKeys.add(k));
            if (typeof p.onLeftDrag === 'function' && typeof p.onRightDrag === 'function') {
              hasDrag = true;
              break;
            }
            n = n.return;
          }
          census.push('fiber-props: ' + ([...propKeys].slice(0, 30).join(' | ') || '(none)'));
        }
      }
      census.push('drag-handles: ' + (hasDrag ? 'OK (onLeftDrag/onRightDrag)' : 'missing'));
      // Window test: prove the fiber drag + poll from the isolated world
      // WITHOUT publishing (diag never saves). vodrip_start/vodrip_end are
      // the target window in seconds.
      const tStart = Number(params.get('vodrip_start') || '0');
      const tEnd = Number(params.get('vodrip_end') || '0');
      let windowTest = 'no range requested';
      if (tEnd > tStart) {
        // Probe the helper first (echo) — isolates injection failure from
        // drag failure.
        const echo = await new Promise((resolve) => {
          const nonce = 'echo' + Math.random().toString(36).slice(2);
          const timer = setTimeout(() => resolve('no-echo'), 3000);
          const onMsg = (ev) => {
            const d = ev.data;
            if (d && d.source === 'vodrip-range-res' && d.nonce === nonce) {
              clearTimeout(timer);
              window.removeEventListener('message', onMsg);
              resolve('alive:' + (d.ok ? 'ok' : d.reason));
            }
          };
          window.addEventListener('message', onMsg);
          try { window.postMessage({ source: 'vodrip-range-req', nonce, start: 0, end: 0 }, '*'); }
          catch (err) { resolve('postMessage-threw:' + err); }
        });
        census.push('helper-echo: ' + echo);
        const res = await rangeViaMainWorld(tStart, tEnd, durSec);
        windowTest = res.ok
          ? `MOVED to ${res.valuetext} (target ${fmtHms(tStart)}→${fmtHms(tEnd)})`
          : `NOT CONFIRMED (${res.reason || '?'})`;
      }
      census.push('window-test: ' + windowTest);
      const inputs = [...document.querySelectorAll('input')];
      const withLabels = inputs
        .map((i) => (i.getAttribute('aria-label') || '').trim())
        .filter((l) => l);
      census.push('aria-labels: ' + (withLabels.join(' | ') || '(none)'));
      const values = inputs.map((i) => i.value || '').filter(Boolean);
      census.push('input-values: ' + (values.join(' | ') || '(none)'));
      census.push('doc-title: ' + document.title);
      setStatus('DIAG CENSUS\n' + census.join('\n'), 'info');
      // GPU: the looping editor preview is the hog — stop it right away.
      try { document.querySelector('video')?.pause(); } catch { /* ignore */ }
    })().catch((err) => {
      setStatus('DIAG FAIL: ' + (err && err.message ? err.message : String(err)), 'err');
    });
    return;
  }
  setStatus(`Preparando clique de ${Math.round(endSec - startSec)}s…`);

  // clips.twitch.tv/create — the legacy URL opens Twitch's clip editor
  // DIRECTLY (it reads vodID + offsetSeconds, which is the clip END) when
  // logged in; only the title + Save Clip remain. The twitch.tv/videos/*
  // flow below is the player route (Clip button → editor overlay).
  if (location.hostname === 'clips.twitch.tv') {
    (async () => {
      injectMainHelper();
      const editorInput = await waitFor(
        () =>
          find([
            'input[data-a-target="tw-input"]',
            'input[placeholder*="title" i]',
            'textarea[placeholder*="title" i]',
          ]),
        45000,
        800,
      );
      if (!editorInput) {
        const errorPage = /something went wrong|ocorreu um problema|algo deu errado/i.test(
          document.body ? document.body.innerText : '',
        );
        note('ext_error', { step: 'editor-input', reason: errorPage ? 'twitch-error-page' : 'not-loaded' });
        setStatus(
          errorPage
            ? 'A Twitch redirecionou para a página de erro — você está logado na Twitch nesta aba? Faça login e tente de novo.'
            : 'Editor não carregou — recarregue a página.',
          'err',
        );
        return;
      }
      setReactValue(editorInput, title);
      note('ext_title', { title, flow: 'create' });
      setStatus(`Título preenchido: ${title}`);
      const rangeErr = await setEditorRange(document, startSec, endSec, durSec);
      if (rangeErr) {
        setStatus(
          `Clique de ${Math.round(endSec - startSec)}s (de ${fmtHms(startSec)} a ${fmtHms(endSec)}) NÃO posicionado no editor (${rangeErr}) — nada foi salvo. Ajuste o trecho no editor e salve você mesmo.`,
          'err',
        );
        closeAfterFlow(2000);
        return;
      }
      setStatus(`Salvando clique de ${Math.round(endSec - startSec)}s…`);
      // Wait for the Save button to become enabled (the editor loads the
      // VOD frame preview first; clicking too early is a silent no-op).
      const save = await waitFor(
        () => {
          const b = [...document.querySelectorAll('button')].find((x) =>
            /save clip|salvar clip/i.test((x.innerText || '').trim()),
          );
          return b && !b.disabled ? b : null;
        },
        30000,
        800,
      );
      if (!save) {
        note('ext_error', { step: 'save-btn', reason: 'missing' });
        setStatus('Botão Save Clip não encontrado — clique você mesmo.', 'err');
        closeAfterFlow();
        return;
      }
      note('ext_save_clicked', { startSec, endSec, title });
      await sleep(1200);
      click(save);
      // Success = the SPA navigates to /<slug>, or the editor reaches its
      // post-publish state ("Copiar Link"). /create and /clips/* are the
      // editor itself and never a success navigation.
      const slugRe = /^\/(?!create$)(?!clips(?:\/|$))[A-Za-z][A-Za-z0-9_-]+$/;
      const published = await waitFor(() => {
        if (location.pathname.match(slugRe)) return `https://clips.twitch.tv${location.pathname}`;
        const copyBtn = [...document.querySelectorAll('button')].find((b) =>
          /copiar link|copy link/i.test((b.innerText || '').trim()),
        );
        return copyBtn ? 'copy' : null;
      }, 60000, 800);
      if (published) {
        let clipUrl = published === 'copy' ? '' : published;
        try {
          const shareInput = find(['[data-a-target="clip-share-url"]', 'input[readonly]', 'textarea[readonly]']);
          if (shareInput) clipUrl = (shareInput.value || '').trim() || clipUrl;
        } catch { /* ignore */ }
        note('ext_published', { url: clipUrl || null, via: published === 'copy' ? 'copy-link' : 'navigation' });
        recordPublishedClip(clipUrl || '');
        setStatus(
          clipUrl
            ? `Clip publicado ✓\n${title}\n${clipUrl}`
            : `Clip publicado ✓\n${title}`,
          'ok',
        );
        closeAfterFlow(1500);
        return;
      }
      setStatus(
        'Save clicado, aguardando processamento… ' +
          (title ? `\n${title}` : '') + '\n(se não aparecer em instantes, confira se o clipe foi criado)',
        'err',
      );
      closeAfterFlow(2000);
    })().catch((err) => {
      note('ext_error', { step: 'crash', msg: (err && err.message ? err.message : String(err)) });
      setStatus('Falha inesperada: ' + (err && err.message ? err.message : String(err)), 'err');
    });
    return;
  }

  (async () => {
    injectMainHelper();
    // 1. Player — needs real duration before we can seek.
    const video = await waitFor(() => {
      const v = document.querySelector('video');
      return v && v.readyState >= 1 && v.duration > 0 ? v : null;
    }, 40000, 800);
    if (!video) {
      setStatus('Player não carregou — recarregue a página.', 'err');
      return;
    }
    // 2. Player's Clip button — wait for it to be ENABLED. Twitch disables
    // it while an ad plays or the VOD is still buffering; clicking a
    // disabled button is a silent no-op. Localized ("Clipe") with no stable
    // data-a-target, so match by text inside the player controls.
    setStatus(`Player pronto — aguardando botão de clipe habilitar em ${fmtHms(startSec)}…`);
    const clipBtn = await waitFor(
      () => {
        const cands = [...document.querySelectorAll('button')].filter((b) => {
          const t = (b.innerText || '').trim();
          return /^\s*clip/i.test(t) && b.offsetParent !== null &&
            !b.disabled && b.getAttribute('aria-disabled') !== 'true' &&
            !!b.closest('[data-a-target="player-controls"], [class*="player"]');
        });
        if (cands.length) return cands[0];
        const byAny = [...document.querySelectorAll('button')].filter((b) =>
          /^\s*clip/i.test((b.innerText || '').trim()) && b.offsetParent !== null &&
          !b.disabled && b.getAttribute('aria-disabled') !== 'true',
        );
        if (byAny.length) return byAny[0];
        return null;
      },
      120000,
      1000,
    );
    if (!clipBtn) {
      note('ext_error', { step: 'clip-btn', reason: 'never-enabled' });
      setStatus('Botão Clip não habilitou (anúncio em reprodução?) — tente de novo mais tarde.', 'err');
      closeAfterFlow(2000);
      return;
    }
    try {
      video.currentTime = startSec; // the ?t= already seeks; enforce it
    } catch {
      /* ignore seek errors — the URL hash did the work */
    }
    await sleep(1500);
    click(clipBtn);
    // Player controls listen on pointer events — fire the full sequence so
    // the editor opens even when the site ignores synthetic `click`.
    try {
      const opts = { bubbles: true, cancelable: true, button: 0, view: window };
      clipBtn.dispatchEvent(new PointerEvent('pointerdown', opts));
      clipBtn.dispatchEvent(new PointerEvent('pointerup', opts));
      clipBtn.dispatchEvent(new MouseEvent('mousedown', opts));
      clipBtn.dispatchEvent(new MouseEvent('mouseup', opts));
      clipBtn.dispatchEvent(new MouseEvent('click', opts));
    } catch { /* older engines: click() above already ran */ }
    await sleep(2000);

    // 3. Editor overlay — a real dialog carrying clip/title chrome, NOT
    // just any tw-input (the search box is one and would false-positive).
    setStatus('Editor abrindo…');
    let editor = await waitFor(
      () => {
        const dialog = [...document.querySelectorAll('[role="dialog"]')].find((d) =>
          /clip|t[ií]tulo|title/i.test((d.innerText || '').slice(0, 600)),
        );
        if (dialog) return dialog;
        return find(['[data-a-target="clip-editor"]']);
      },
      10000,
      700,
    );
    if (!editor) {
      // The button's own aria-label advertises the shortcut: alt+x.
      try {
        const kd = new KeyboardEvent('keydown', { key: 'x', code: 'KeyX', altKey: true, bubbles: true });
        const ku = new KeyboardEvent('keyup', { key: 'x', code: 'KeyX', altKey: true, bubbles: true });
        const target = document.activeElement || document.body;
        target.dispatchEvent(kd);
        target.dispatchEvent(ku);
      } catch { /* ignore */ }
      editor = await waitFor(
        () => {
          const dialog = [...document.querySelectorAll('[role="dialog"]')].find((d) =>
            /clip|t[ií]tulo|title/i.test((d.innerText || '').slice(0, 600)),
          );
          if (dialog) return dialog;
          return find(['[data-a-target="clip-editor"]']);
        },
        15000,
        700,
      );
    }
    if (!editor) {
      note('ext_error', { step: 'editor-dialog', reason: 'missing' });
      setStatus(
        'Não consegui abrir o editor (faça login na Twitch nesta aba e tente de novo).\nTítulo: ' +
          (title || '(vazio)') +
          `\nClique: ${Math.round(endSec - startSec)}s (de ${fmtHms(startSec)} a ${fmtHms(endSec)})`,
        'err',
      );
      closeAfterFlow(2000);
      return;
    }
    await sleep(1500); // let the editor settle
    const rangeErr = await setEditorRange(editor, startSec, endSec, durSec);
    if (rangeErr) {
      setStatus(
        `Clique de ${Math.round(endSec - startSec)}s (de ${fmtHms(startSec)} a ${fmtHms(endSec)}) NÃO posicionado no editor (${rangeErr}) — nada foi publicado. Ajuste o trecho no editor e publique você mesmo.`,
        'err',
      );
      closeAfterFlow(2000);
      return;
    }

    // 4. Title (always non-empty — the URL default covers missing vodrip_title).
    const input =
      (editor.querySelector('input[data-a-target="tw-input"], [data-a-target="clip-editor-title-input"], input[placeholder*="title" i], textarea[placeholder*="title" i], input[aria-label*="title" i]')) ||
      find([
        '[data-a-target="clip-editor-title-input"]',
        'input[placeholder*="title" i]',
        'textarea[placeholder*="title" i]',
        'input[aria-label*="title" i]',
      ]);
    if (input) {
      setReactValue(input, title);
      note('ext_title', { title, flow: 'videos' });
      setStatus(`Título preenchido: ${title}`);
    } else {
      note('ext_error', { step: 'title-input', reason: 'missing' });
      setStatus('Campo de título não encontrado — preencha manualmente.', 'err');
    }

    // 5. Publish.
    const publish = await waitFor(
      () => {
        const inEditor = [...editor.querySelectorAll('button')].find((b) =>
          /save clip|publish|salvar clip|publicar/i.test((b.innerText || '').trim()),
        );
        if (inEditor) return inEditor;
        return find([
          '[data-a-target="clip-publish-button"]',
          'button[data-a-target*="publish" i]',
          'button[aria-label*="publish" i]',
          'button[class*="publish" i]',
        ]);
      },
      10000,
      500,
    );
    if (!publish) {
      note('ext_error', { step: 'publish-btn', reason: 'missing' });
      setStatus('Botão Publish não encontrado — clique você mesmo.', 'err');
      closeAfterFlow();
      return;
    }
    note('ext_publish_clicked', { startSec, endSec, title });
    setStatus(`Publicando clip ${fmtHms(startSec)} → ${fmtHms(endSec)}…`);
    await sleep(1200);
    click(publish);
    // Success = the editor reaches its post-publish state ("Copiar Link" /
    // "Copy Link"), or the site navigates to /<channel>/clip/<slug>.
    const published = await waitFor(() => {
      const copyBtn = [...document.querySelectorAll('button')].find((b) =>
        /copiar link|copy link/i.test((b.innerText || '').trim()),
      );
      if (copyBtn) return 'copy';
      return location.pathname.match(/\/clip\//) ? 'nav' : null;
    }, 60000, 800);
    if (published) {
      // The share box next to "Copiar Link" holds the clip URL.
      let clipUrl = '';
      try {
        const shareInput = (editor && editor.querySelector('[data-a-target="clip-share-url"], input[readonly], textarea[readonly]')) ||
          find(['[data-a-target="clip-share-url"]', 'input[readonly]', 'textarea[readonly]']);
        if (shareInput) clipUrl = (shareInput.value || '').trim();
      } catch { /* ignore */ }
      if (!clipUrl) clipUrl = location.href;
      note('ext_published', { url: clipUrl, via: published === 'copy' ? 'copy-link' : 'navigation' });
      recordPublishedClip(clipUrl);
      setStatus(
        clipUrl
          ? `Clip publicado ✓\n${title}\n${clipUrl}`
          : `Clip publicado ✓\n${title}`,
        'ok',
      );
      closeAfterFlow(1500);
    } else {
      setStatus(
        'Publish clicado — aguardando processamento…\n' + (title || ''),
        'err',
      );
      closeAfterFlow(2000);
    }
  })().catch((err) => {
    setStatus('Falha inesperada: ' + (err && err.message ? err.message : String(err)), 'err');
  });
})().catch(() => {
  /* top-level: panel setup failed — nothing useful to do */
});
