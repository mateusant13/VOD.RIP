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
// scopes needed (the backend Helix path needs editor:manage:clips, which the
// browser-login token never carries).
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
  if (location.hostname === 'clips.twitch.tv' && location.search.includes('vodrip_clip=1')) {
    try {
      sessionStorage.setItem('vodrip_clip_params', location.search);
    } catch { /* storage blocked */ }
  }

  const rawSearch = location.hostname === 'clips.twitch.tv'
    ? (sessionStorage.getItem('vodrip_clip_params') || location.search)
    : location.search;
  const params = new URLSearchParams(rawSearch.replace(/^\?/, ''));
  if (params.get('vodrip_clip') !== '1') return;

  const startSec = Math.max(0, Math.floor(Number(params.get('vodrip_start')) || 0));
  const endSec = Math.max(startSec, Math.floor(Number(params.get('vodrip_end')) || 0));

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
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h ? `${h}h${pad(m)}m${pad(r)}s` : m ? `${m}m${pad(r)}s` : `${r}s`;
  };
  // No user title -> a deterministic default so the clip still auto-publishes
  // with zero interaction (the app's "Open in browser" button never requires
  // a title).
  const title = (params.get('vodrip_title') || '').trim() || `VOD.RIP ${fmtHms(startSec)}`;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
  setStatus(`Preparando clip em ${fmtHms(startSec)}…`);

  // clips.twitch.tv/create — the legacy URL opens Twitch's clip editor
  // DIRECTLY (it reads vodID + offsetSeconds, which is the clip END) when
  // logged in; only the title + Save Clip remain. The twitch.tv/videos/*
  // flow below is the player route (Clip button → editor overlay).
  if (location.hostname === 'clips.twitch.tv') {
    (async () => {
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
        setStatus(
          errorPage
            ? 'A Twitch redirecionou para a página de erro — você está logado na Twitch nesta aba? Faça login e tente de novo.'
            : 'Editor não carregou — recarregue a página.',
          'err',
        );
        return;
      }
      setReactValue(editorInput, title);
      setStatus(`Título preenchido: ${title}`);
      setStatus(`Salvando clip ${fmtHms(startSec)} → ${fmtHms(endSec)}…`);
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
        setStatus('Botão Save Clip não encontrado — clique você mesmo.', 'err');
        return;
      }
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
        setStatus(
          clipUrl
            ? `Clip publicado ✓\n${title}\n${clipUrl}`
            : `Clip publicado ✓\n${title}`,
          'ok',
        );
        return;
      }
      setStatus(
        'Save clicado, aguardando processamento… ' +
          (title ? `\n${title}` : '') + '\n(se não aparecer em instantes, confira se o clipe foi criado)',
        'err',
      );
    })().catch((err) => {
      setStatus('Falha inesperada: ' + (err && err.message ? err.message : String(err)), 'err');
    });
    return;
  }

  (async () => {
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
      setStatus('Botão Clip não habilitou (anúncio em reprodução?) — tente de novo mais tarde.', 'err');
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
      setStatus(
        'Não consegui abrir o editor (faça login na Twitch nesta aba e tente de novo).\nTítulo: ' +
          (title || '(vazio)') +
          `\nTrecho: ${fmtHms(startSec)} → ${fmtHms(endSec)}`,
        'err',
      );
      return;
    }
    await sleep(1500); // let the editor settle

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
      setStatus(`Título preenchido: ${title}`);
    } else {
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
      setStatus('Botão Publish não encontrado — clique você mesmo.', 'err');
      return;
    }
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
      setStatus(
        clipUrl
          ? `Clip publicado ✓\n${title}\n${clipUrl}`
          : `Clip publicado ✓\n${title}`,
        'ok',
      );
    } else {
      setStatus(
        'Publish clicado — aguardando processamento…\n' + (title || ''),
        'err',
      );
    }
  })().catch((err) => {
    setStatus('Falha inesperada: ' + (err && err.message ? err.message : String(err)), 'err');
  });
})().catch(() => {
  /* top-level: panel setup failed — nothing useful to do */
});
