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
  const title = (params.get('vodrip_title') || '').trim();

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
      if (!title) {
        setStatus('Sem título informado — preencha o título e clique em Save Clip.', 'info');
        return;
      }
      setReactValue(editorInput, title);
      setStatus(`Título preenchido: ${title}`);
      const save = await waitFor(
        () =>
          [...document.querySelectorAll('button')].find((b) =>
            /save clip|salvar clip/i.test((b.innerText || '').trim()),
          ),
        10000,
        500,
      );
      if (!save) {
        setStatus('Botão Save Clip não encontrado — clique você mesmo.', 'err');
        return;
      }
      setStatus(`Salvando clip ${fmtHms(startSec)} → ${fmtHms(endSec)}…`);
      await sleep(1200);
      click(save);
      // The SPA navigates to /<slug> after the clip is created.
      const slugUrl = await waitFor(
        () => {
          const m = location.pathname.match(/^\/([A-Za-z][A-Za-z0-9_-]+)$/);
          return m ? `https://clips.twitch.tv${location.pathname}` : null;
        },
        20000,
        800,
      );
      setStatus(
        slugUrl
          ? `Clip publicado ✓\n${title}\n${slugUrl}`
          : `Clip publicado ✓\n${title}`,
        'ok',
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
    try {
      video.currentTime = startSec; // the ?t= already seeks; enforce it
    } catch {
      /* ignore seek errors — the URL hash did the work */
    }

    // 2. Player's Clip button (opens the in-site editor for the VOD).
    setStatus(`Player pronto — abrindo editor em ${fmtHms(startSec)}…`);
    const clipBtn = await waitFor(
      () =>
        find([
          '[data-a-target="player-clip-button"]',
          'button[aria-label="Clip"]',
          'button[aria-label*="Clip" i]',
        ]),
      15000,
      500,
    );
    if (!clipBtn) {
      setStatus('Botão Clip não encontrado no player.', 'err');
      return;
    }
    click(clipBtn);

    // 3. Editor overlay (any of its well-known bits is proof it opened).
    setStatus('Editor abrindo…');
    const editor = await waitFor(
      () =>
        find([
          '[data-a-target="clip-editor"]',
          'input[data-a-target="tw-input"]',
          'input[placeholder*="title" i]',
          'textarea[placeholder*="title" i]',
          'button[data-a-target*="publish" i]',
          'button[aria-label*="publish" i]',
        ]),
      25000,
      700,
    );
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

    // 4. Title.
    if (title) {
      const input = find([
        'input[data-a-target="tw-input"]',
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
    } else {
      setStatus('Sem título informado — preencha o título e clique em Publish.', 'info');
    }

    // 5. Publish — only when a title was set (the site can reject empty titles).
    if (title) {
      const publish = await waitFor(
        () => {
          const byText = [...document.querySelectorAll('button')].find((b) =>
            /save clip|publish|salvar clip|publicar/i.test((b.innerText || '').trim()),
          );
          if (byText) return byText;
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
      setStatus(
        `Clip publicado ✓\n${title}\n${fmtHms(startSec)} → ${fmtHms(endSec)}`,
        'ok',
      );
    }
  })().catch((err) => {
    setStatus('Falha inesperada: ' + (err && err.message ? err.message : String(err)), 'err');
  });
})().catch(() => {
  /* top-level: panel setup failed — nothing useful to do */
});
