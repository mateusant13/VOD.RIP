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
// notice; candidate selectors have graceful fallbacks and the status panel
// reports actionable failures. The MAIN-world fiber callbacks are the source
// of truth for the selected absolute VOD range; the visible clock is rewritten
// to the confirmed 5–60 second duration so users never read VOD positions as
// clip lengths.
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
  const vodDurationSec = Number(params.get('vodrip_dur')) || 0;
  // The title the editor gets: the ORIGINAL one — vodrip_title (the app
  // sends the VOD's GraphQL title) else document.title minus the " - Twitch"
  // suffix. NEVER a "VOD.RIP …" default, and never a page-title artifact
  // ("Criar clipe - Twitch") — an unusable fallback stays '' so the write
  // is skipped rather than typing a fake title.
  const title =
    (params.get('vodrip_title') || '').trim() ||
    (document.title || '').replace(/\s*-\s*Twitch\s*$/i, '').trim() ||
    '';
  const clipTitle = /^(criar clipe|create clip|clips?|twitch)$/i.test(title) ? '' : title;

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
  // Ground-truth editor position: the playback-time display the user
  // identified (strong "17:00 / 1:30" inside the player controls). Unlike
  // the slider valuetext (window-relative 0..90s), this shows the
  // VOD-absolute position the editor is actually at — a "17:00 / 1:30"
  // here means the window sits at 17:00 of the VOD, NOT a 17-minute
  // duration. Always read + log it when attempting a clip. PRIMARY locator
  // is the user's exact structural path (the <strong> inside the editor's
  // time display); the loose 'main strong' regex is the fallback.
  const playbackTimeText = () => {
    try {
      const viaPath = document.evaluate(
        '//*[@id="root"]/div[1]/div[1]/div/div/main/div/div[1]/div[2]/div/div/div[1]/div[3]/strong',
        document,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null,
      ).singleNodeValue;
      const t = viaPath && viaPath.textContent ? viaPath.textContent.trim() : '';
      const original = viaPath && viaPath.dataset.vodripOriginalClock;
      if (original) return original;
      if (t) return t;
      const el = [...document.querySelectorAll('main strong')].find((x) =>
        /^\s*\d+:\d{2}\s*\/\s*\d+:\d{2}\s*$/.test((x.textContent || '').trim()),
      );
      return el ? el.textContent.trim() : null;
    } catch { return null; }
  };
  // Twitch renders the selected clip as VOD-absolute m:ss values (for
  // example "12:31 / 1:30"). Users read those as clip input values, so keep
  // the real slider contract in seconds on-screen and hide duplicate setter
  // labels that repeat the absolute clock.
  const CLOCK_RE = /^\s*\d{1,2}:\d{2}\s*\/\s*\d{1,2}:\d{2}\s*$/;
  const clockNodes = () => {
    const out = [];
    try {
      const viaPath = document.evaluate(
        '//*[@id="root"]/div[1]/div[1]/div/div/main/div/div[1]/div[2]/div/div/div[1]/div[3]/strong',
        document,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null,
      ).singleNodeValue;
      if (viaPath) out.push(viaPath);
    } catch { /* ignore */ }
    for (const s of document.querySelectorAll('strong')) {
      if (CLOCK_RE.test((s.textContent || '').trim()) && !out.includes(s)) out.push(s);
    }
    return out;
  };
  const clockVisible = () => clockNodes().length > 0;
  const hideAbsoluteTimeControls = () => {
    for (const button of document.querySelectorAll('button, [role="button"]')) {
      if (!/ajustar momento de (início|encerramento)|set (clip )?(start|end)|set (the )?(start|end) time/i.test(
        (button.innerText || '').trim(),
      )) continue;
      button.style.display = 'none';
      button.setAttribute('aria-hidden', 'true');
    }
  };
  const secondsifyClock = () => {
    const w = selectedWindow();
    if (!w || !Number.isFinite(w.dur) || w.dur < 5) return;
    const duration = Math.max(5, Math.min(60, Math.round(w.dur)));
    for (const clock of clockNodes()) {
      if (!clock.dataset.vodripOriginalClock) {
        clock.dataset.vodripOriginalClock = (clock.textContent || '').trim();
      }
      clock.textContent = `${duration}s`;
      clock.setAttribute('aria-label', `Duração do clipe: ${duration} segundos`);
      clock.dataset.vodripSecondsClock = '1';
    }
    hideAbsoluteTimeControls();
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  // Debugging event sequence: every flow step is POSTed to the app's
  // clip-events sink (same API base as the cookie bridge) so a clip attempt
  // can be replayed end to end from the app log. Fire-and-forget — logging
  // must never break the flow. The POST runs in the BACKGROUND service
  // worker, NOT here: a direct fetch from this content script is
  // cross-origin (clips.twitch.tv -> http://127.0.0.1) and the browser
  // kills it on the CORS preflight (the backend had no CORS middleware —
  // that is why zero ext_* events ever reached the sink). The worker
  // fetches with the extension's own origin + host_permission
  // http://127.0.0.1/* — no preflight at all.
  const note = (event, data = {}) => {
    try {
      const p = chrome.runtime.sendMessage({ type: 'vodrip_note', event, data });
      if (p && typeof p.catch === 'function') p.catch(() => {});
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
      const p = chrome.runtime.sendMessage({
        type: 'vodrip_record',
        payload: {
          url: clipUrl,
          title,
          channel: ch,
          vod_id: params.get('vodID') || (vodMatch ? vodMatch[1] : undefined),
          offset_sec: end > 0 ? Math.floor(end) : undefined,
          duration_sec: end > start ? Math.round(end - start) : undefined,
        },
      });
      if (p && typeof p.catch === 'function') p.catch(() => {});
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
    playback: playbackTimeText(),
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
  const rangeViaMainWorld = (startSec, endSec, vodDurationSec) =>
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
        window.postMessage({ source: 'vodrip-range-req', nonce, start: startSec, end: endSec, dur: vodDurationSec }, '*');
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
  const click = (el) => {
    el.click();
    return el;
  };
  /** Set a React-controlled input's value the way the site's own handlers see it. */
  const setReactValue = (el, value) => {
    const proto =
      el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const find = (selectors) => {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      if (el) return el;
    }
    return null;
  };

  /**
   * Dismiss the editor's portrait-layout modal — "Notamos que você não
   * editou o layout em retrato" / "You haven't edited the portrait layout".
   * Twitch shows it on EVERY save when the portrait layout was never
   * edited; it sat in front of the post-publish state, so the publish
   * watcher timed out and NO clip was ever created. The modal opens right
   * after Save and first shows the editor processing the clip ("Salvando
   * clipe... N%" + layout-version tabs); its "skip" button ("Salvar sem
   * editar" / "Save without editing" / "Continuar sem editar" / "Skip (the)
   * (portrait) layout") only becomes clickable once processing finishes —
   * observed live at >5min for a 34-min VOD (2026-08-10). Phase 1 waits
   * through the editor's processing state; phase 2 polls the modal button
   * with a long budget.
   * Fallback: the modal's OWN confirm button (/salvar clip|save
   * clip/i) scoped to the dialog element so the page's Save button is
   * never clicked twice; a page-wide skip-button search covers a modal
   * rendered without role=dialog (the skip text is unique to the modal).
   * Always logs ext_modal. Called AFTER click(save)/click(publish), BEFORE
   * the publish watcher.
   */
  const dismissPortraitModal = async () => {
    // Real modal text (seen live 2026-08-10): the pt-BR intro + the dialog's
    // layout-version tabs ("Versão em retrato"/"Versão em paisagem").
    const modalTextRe = /layout em retrato|portrait layout|não editou o layout|haven'?t edited the layout|em retrato|em paisagem|portrait|landscape|sem editar|without editing/i;
    const skipBtnRe = /salvar sem editar|save without editing|continuar sem editar|skip(?:ping)? (?:the )?(?:portrait )?layout/i;
    const confirmBtnRe = /^(salvar clipe|save clip|criar clipe|create clip)$/i;
    const findDialog = () =>
      [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')].find((el) =>
        modalTextRe.test((el.innerText || '').slice(0, 600)),
      ) || null;
    const findSkipPageWide = () =>
      [...document.querySelectorAll('button, [role="button"]')].find((b) =>
        skipBtnRe.test((b.innerText || '').trim()),
      ) || null;
    const bodyHasModalText = () =>
      /em retrato|em paisagem|salvar sem editar|save without editing|layout em retrato|portrait layout/i.test(
        (document.body ? document.body.innerText : '').slice(0, 8000),
      );
    let dialog = null;
    let modalText = null;
    let skipBtn = null;
    let confirmBtn = null;
    // Phase 1 — detect the modal while Twitch may still be processing.
    const detectDeadline = Date.now() + 120000;
    const bodyHasProcessingText = () =>
      /salvando clipe|saving clip|processando|processing/i.test(
        (document.body ? document.body.innerText : '').slice(0, 8000),
      ) ||
      [...document.querySelectorAll('[role="progressbar"], [role="status"]')]
        .some((el) => /salvando|saving|processando|processing/i.test(el.innerText || ''));
    while (!dialog && !skipBtn && Date.now() <= detectDeadline) {
      dialog = findDialog();
      skipBtn = findSkipPageWide();
      if (!skipBtn) await sleep(400);
    }
    if (dialog) modalText = (dialog.innerText || '').trim().slice(0, 120);
    if (!dialog && !skipBtn && !bodyHasModalText() && !bodyHasProcessingText()) {
      // No modal at all (e.g. the editor remembered the layout) — the
      // publish watcher proceeds immediately.
      note('ext_modal', { action: 'none', modalText: null });
      return;
    }
    // Phase 2 — the modal is present; wait for its button (long budget:
    // the editor's "Salvando clipe..." processing must finish first).
    const deadline = Date.now() + 480000;
    while (Date.now() <= deadline) {
      if (dialog) {
        const btns = [...dialog.querySelectorAll('button, [role="button"]')];
        skipBtn = skipBtn || btns.find((b) => skipBtnRe.test((b.innerText || '').trim()));
        confirmBtn = confirmBtn || btns.find((b) => confirmBtnRe.test((b.innerText || '').trim()));
        if (skipBtn || confirmBtn) break;
      }
      skipBtn = skipBtn || findSkipPageWide();
      if (skipBtn) break;
      await sleep(500);
    }
    if (skipBtn) {
      click(skipBtn);
      note('ext_modal', { action: 'dismissed', modalText, via: dialog ? 'dialog' : 'page' });
      return;
    }
    if (confirmBtn) {
      click(confirmBtn);
      note('ext_modal', { action: 'save-in-modal', modalText });
      return;
    }
    // Diagnostic census when nothing was found — the next fix needs to know
    // what the live DOM actually looked like (roles? button texts?).
    const census = {
      dialogs: [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')]
        .slice(0, 5)
        .map((d) => (d.innerText || '').trim().slice(0, 160)),
      buttons: [...document.querySelectorAll('button, [role="button"]')]
        .slice(0, 40)
        .map((b) => (b.innerText || '').trim().slice(0, 40))
        .filter(Boolean),
      bodyHasModalText: bodyHasModalText(),
    };
    note('ext_modal', { action: 'none', modalText, census });
  };

  /** Parse an editor clock "3:20" (m:ss) or "1:02:05" (h:mm:ss) → seconds. */
  const parseClock = (s) => {
    const m = String(s || '').trim().match(/^(\d+):(\d{1,2})(?::(\d{1,2}))?$/);
    if (!m) return null;
    return +m[1] * 60 + +m[2] + (+m[3] || 0);
  };

  /**
   * Twitch's clip editor displays the selected window in absolute VOD
   * seconds. The fiber drag callbacks use those same absolute values;
   * the 90-second preview chunk is only a viewport around the selection.
   */
  const EDITOR_WINDOW_SEC = 90;
  const editorChunkStart = (endSec) => Math.max(0, endSec - EDITOR_WINDOW_SEC);

  /**
   * Drive the editor's real window control. React exposes
   * onLeftDrag/onRightDrag callbacks on the slider; their offsets are
   * absolute VOD seconds, as confirmed by the live 12:31 -> 12:48 run.
   */
  const setEditorRange = async (scope, startSec, endSec, vodDurationSec) => {
    const len = endSec - startSec;
    // Twitch's native viewport is 90s, but VOD.RIP's clip contract is 5..60s.
    if (len < 5 || len > 60) {
      note('ext_range_refused', { startSec, endSec, len });
      setStatus(
        `Trecho de ${Math.round(len)}s fora do limite (5..60s) — ajuste o clipe no app e tente de novo.`,
        'err',
      );
      return 'fora-do-limite';
    }
    // The title can render before Twitch mounts the real range control.
    // Wait for that control instead of falling back with the native window untouched.
    const slider = await waitFor(() => document.querySelector('[role="slider"]'), 45000, 500);
    if (!slider) return 'controle do editor (slider) não encontrado';
    const res = await rangeViaMainWorld(startSec, endSec, vodDurationSec);
    note('ext_range', {
      ok: !!res.ok,
      targetStart: startSec,
      targetEnd: endSec,
      editorChunkStart: editorChunkStart(endSec),
      editorStart: startSec,
      editorEnd: endSec,
      vodDurationSec,
      playback: playbackTimeText(),
      clockVisible: clockVisible(),
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
      `Clique de ${Math.round(len)}s confirmado no editor.`,
      'ok',
    );
    mountDurationBadge();
    return '';
  };

  // -------------------------------------------------------------------
  // Duration badge — the user reads the editor's "12:31 to 12:48"
  // (VOD-absolute m:ss from the slider's aria-valuetext) as a ~12-minute
  // clip; the actual window is 17s. This big banner makes the SECONDS the
  // unmissable thing: large green "Duração: Ns" with the window in small
  // text beneath, pinned center-top of the viewport (user-mandated
  // 2026-08-10). It is mounted OUTSIDE React's tree (child of <html>) so
  // re-renders never remove it, and renders immediately on range-set
  // success (no tick). Updates are driven by a MutationObserver on the
  // slider's aria-valuetext — both the extension's own onLeftDrag/
  // onRightDrag drags and the user's handle drags land there — and a 1s
  // liveness tick re-finds a React-replaced slider (the attribute observer
  // dies with the old node) and removes the chip once the slider is gone
  // (save → SPA navigation, editor closed, leave). pointer-events:none so
  // it never blocks Twitch's own controls.
  // -------------------------------------------------------------------
  let durationBadge = null;
  let durationBadgeObs = null;
  let durationBadgeTick = null;

  const fmtClock = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(r)}` : `${m}:${pad(r)}`;
  };
  const parseValuetext = (vt) => {
    const parts = String(vt || '').trim().split(/\s*to\s*/i);
    if (parts.length !== 2) return null;
    const a = parseClock(parts[0]);
    const b = parseClock(parts[1]);
    if (a == null || b == null) return null;
    return { start: a, end: b, dur: b - a };
  };
  const selectedWindow = (scope = document) => {
    const slider = (scope || document).querySelector('[role="slider"]') ||
      document.querySelector('[role="slider"]');
    return slider ? parseValuetext(slider.getAttribute('aria-valuetext')) : null;
  };
  const confirmSelectedWindow = (scope, start, end) => waitFor(() => {
    const w = selectedWindow(scope);
    const len = end - start;
    return w &&
      Math.abs(w.start - start) <= 3 &&
      Math.abs(w.end - end) <= 3 &&
      Math.abs(w.dur - len) <= 1 &&
      w.dur >= 5 && w.dur <= 60
      ? w
      : null;
  }, 5000, 250);
  const renderDurationBadge = () => {
    const slider = document.querySelector('[role="slider"]');
    if (!slider) return false;
    const w = parseValuetext(slider.getAttribute('aria-valuetext'));
    if (!w) return true; // valuetext unreadable — keep the last known text
    // USER-MANDATED (2026-08-10): minutes-shaped values must NEVER appear
    // on the editor screen — the badge and native clock show seconds only.
    secondsifyClock();
    durationBadge.querySelector('[data-vodrip-badge-sec]').textContent = `Duração: ${Math.max(5, Math.min(60, Math.round(w.dur)))}s`;
    return true;
  };
  const mountDurationBadge = () => {
    if (durationBadge) { renderDurationBadge(); return durationBadge; }
    durationBadge = document.createElement('div');
    durationBadge.setAttribute('data-vodrip-duration-badge', '');
    Object.assign(durationBadge.style, {
      position: 'fixed',
      top: '14px',
      left: '50%',
      transform: 'translateX(-50%)',
      zIndex: '2147483646', // one below the assist panel — never over it
      background: 'rgba(10,10,14,0.97)',
      color: '#f4f4f5',
      fontFamily: 'Consolas, monospace',
      padding: '10px 20px',
      borderRadius: '12px',
      border: '3px solid #53fc18',
      boxShadow: '0 0 28px rgba(83,252,24,0.5), 0 4px 24px rgba(0,0,0,0.65)',
      pointerEvents: 'none',
      textAlign: 'center',
      whiteSpace: 'nowrap',
      lineHeight: '1.15',
    });
    // Static inner structure (no user data) — seconds only, large.
    durationBadge.innerHTML =
      '<div data-vodrip-badge-sec style="font-size:30px;font-weight:800;color:#53fc18;letter-spacing:1px;">Duração: —</div>';
    document.documentElement.appendChild(durationBadge);
    const observeSlider = () => {
      const slider = document.querySelector('[role="slider"]');
      if (!durationBadgeObs || !slider) return;
      durationBadgeObs.disconnect();
      durationBadgeObs.observe(slider, { attributes: true, attributeFilter: ['aria-valuetext'] });
    };
    durationBadgeObs = new MutationObserver(renderDurationBadge);
    observeSlider();
    renderDurationBadge();
    window.addEventListener('resize', renderDurationBadge);
    // Liveness tick: React may replace the slider node entirely (the
    // attribute observer dies with the old node) — re-find + re-observe;
    // slider gone (save → SPA navigated, editor closed) → remove the chip.
    durationBadgeTick = setInterval(() => {
      if (!document.querySelector('[role="slider"]')) {
        unmountDurationBadge();
        return;
      }
      observeSlider();
      renderDurationBadge();
    }, 1000);
    const onUnload = () => unmountDurationBadge();
    window.addEventListener('beforeunload', onUnload);
    window.addEventListener('pagehide', onUnload);
    return durationBadge;
  };
  const unmountDurationBadge = () => {
    if (durationBadgeTick) { clearInterval(durationBadgeTick); durationBadgeTick = null; }
    if (durationBadgeObs) { durationBadgeObs.disconnect(); durationBadgeObs = null; }
    if (durationBadge) { durationBadge.remove(); durationBadge = null; }
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
        const res = await rangeViaMainWorld(tStart, tEnd, vodDurationSec);
        windowTest = res.ok
          ? `MOVED to ${res.valuetext} (target ${fmtHms(tStart)}→${fmtHms(tEnd)}) debug=${JSON.stringify(res.debug || {})}`
          : `NOT CONFIRMED (${res.reason || '?'})`;
        // The diag page doubles as the badge's DOM probe: a successful
        // window test mounts the same duration-first chip the real flow
        // mounts, so the census below can assert its presence + text.
        if (res.ok) mountDurationBadge();
      }
      census.push('window-test: ' + windowTest);
      const badge = document.querySelector('[data-vodrip-duration-badge]');
      census.push('duration-badge: ' + (badge ? badge.textContent : 'missing'));
      const inputs = [...document.querySelectorAll('input')];
      const withLabels = inputs
        .map((i) => (i.getAttribute('aria-label') || '').trim())
        .filter((l) => l);
      census.push('aria-labels: ' + (withLabels.join(' | ') || '(none)'));
      const values = inputs.map((i) => i.value || '').filter(Boolean);
      census.push('input-values: ' + (values.join(' | ') || '(none)'));
      census.push('doc-title: ' + document.title);
      // The playback-time strong the user identified ("17:00 / 1:30") — the
      // VOD-absolute position; always surfaced in the diag census.
      census.push('playback-strong: ' + (playbackTimeText() || '(none)'));
      census.push('visible-clock: ' + (clockNodes().map((el) => (el.textContent || '').trim()).join(' | ') || '(none)'));
      // Sink the census so Main can read the REAL editor DOM from the app
      // log (GET /api/debug/clip-events) after the user opens the diag URL.
      // Truncate each line: the backend rejects data > 8KB, and the panel
      // keeps the full text for UIA reads.
      note('ext_diag', {
        census: census.map((l) => (l.length > 240 ? l.slice(0, 240) + '…' : l)),
      });
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
      // Fill the title with the ORIGINAL one (vodrip_title from the app =
      // the VOD's GraphQL title, else document.title). The editor REQUIRES
      // a title ("Adicione um título (obrigatório)") — leaving it empty
      // blocks the save. Never typed when the fallback is a page-title
      // artifact (clipTitle ''). The wait above doubles as the
      // editor-ready + error-page check.
      if (clipTitle) {
        setReactValue(editorInput, clipTitle);
        note('ext_title', { title: clipTitle, value: editorInput.value || '', flow: 'create' });
      }
      const rangeErr = await setEditorRange(document, startSec, endSec, vodDurationSec);
      if (rangeErr) {
        // Never click Save without a confirmed range. Leave the editor open
        // for an explicit user correction instead of publishing an unknown
        // duration.
        note('ext_range_fallback', { reason: rangeErr, startSec, endSec, len: endSec - startSec });
        setStatus(
          'Não consegui confirmar o trecho automaticamente.\n' +
            `Não vou clicar em Save sem confirmar ${Math.round(endSec - startSec)}s. ` +
            `Ajuste o clipe no editor para ${fmtHms(startSec)}–${fmtHms(endSec)} e tente novamente.`,
          'err',
        );
        closeAfterFlow(8000);
        return;
      }
        const confirmedWindow = await confirmSelectedWindow(document, startSec, endSec);
        if (!confirmedWindow) {
          note('ext_error', {
            step: 'range-before-save',
            reason: 'slider-changed-before-save',
            targetStart: startSec,
            targetEnd: endSec,
            valuetext: selectedWindow()?.start != null
              ? `${selectedWindow().start} to ${selectedWindow().end}`
              : null,
          });
          setStatus(
            `O trecho mudou antes do save — nada foi salvo. Ajuste para ${Math.round(endSec - startSec)}s e tente novamente.`,
            'err',
          );
          return;
        }
        note('ext_range_confirmed', {
          startSec,
          endSec,
          durationSec: confirmedWindow.dur,
          valuetext: confirmedWindow.start + ' to ' + confirmedWindow.end,
        });
        setStatus(`Salvando clique de ${Math.round(endSec - startSec)}s…`);
        // Wait for the Save button to become enabled (the editor loads the
        // VOD frame preview first; clicking too early is a silent no-op).
        const save = await waitFor(
          () => {
            // Exact label match: "Salvar clipe" (create, pt-BR census),
            // "Save Clip", "Criar clipe" (/videos publish label), "Create
            // Clip". NEVER substring — "Sempre criar clipes em novas abas"
            // (the toggle) contains "criar clipe" and would swallow the click.
            const b = [...document.querySelectorAll('button')].find((x) =>
              /^(save clip|salvar clipe|criar clipe|create clip)$/i.test((x.innerText || '').trim()),
            );
            return b && !b.disabled ? b : null;
          },
          90000,
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
        // The portrait-layout modal blocks EVERY save — dismiss it before
        // the publish watcher below starts (else the watcher times out and
        // no clip is ever created). Same guard on the /videos publish path.
        await dismissPortraitModal();
      // Success = the SPA navigates to /<slug>, or the editor reaches its
      // post-publish state ("Copiar Link"). /create and /clips/* are the
      // editor itself and never a success navigation. Save is reached only
      // after the selected window was confirmed above.
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
    const rangeErr = await setEditorRange(editor, startSec, endSec, vodDurationSec);
    if (rangeErr) {
      setStatus(
        `Clique de ${Math.round(endSec - startSec)}s (de ${fmtHms(startSec)} a ${fmtHms(endSec)}) NÃO posicionado no editor (${rangeErr}) — nada foi publicado. Ajuste o trecho no editor e publique você mesmo.`,
        'err',
      );
      closeAfterFlow(2000);
      return;
    }
    const confirmedWindow = await confirmSelectedWindow(editor, startSec, endSec);
    if (!confirmedWindow) {
      note('ext_error', {
        step: 'range-before-publish',
        reason: 'slider-changed-before-publish',
        targetStart: startSec,
        targetEnd: endSec,
      });
      setStatus(
        `O trecho mudou antes do publish — nada foi publicado. Ajuste para ${Math.round(endSec - startSec)}s e tente novamente.`,
        'err',
      );
      closeAfterFlow(2000);
      return;
    }
    note('ext_range_confirmed', {
      startSec,
      endSec,
      durationSec: confirmedWindow.dur,
      valuetext: confirmedWindow.start + ' to ' + confirmedWindow.end,
    });

    // 4. Title — the editor REQUIRES one ("Adicione um título
    // (obrigatório)"); fill it with the ORIGINAL title (vodrip_title from
    // the app, else document.title minus " - Twitch"). Never typed when
    // the fallback is a page-title artifact (clipTitle '').
    if (clipTitle) {
      const titleInput =
        (editor && editor.querySelector(
          'input[data-a-target="tw-input"], [data-a-target="clip-editor-title-input"], ' +
            'input[placeholder*="title" i], textarea[placeholder*="title" i], input[aria-label*="title" i]',
        )) ||
        find([
          'input[data-a-target="tw-input"]',
          '[data-a-target="clip-editor-title-input"]',
          'input[placeholder*="title" i]',
          'textarea[placeholder*="title" i]',
          'input[aria-label*="title" i]',
        ]);
      if (titleInput) {
        setReactValue(titleInput, clipTitle);
        note('ext_title', { title: clipTitle, value: titleInput.value || '', flow: 'videos' });
      }
    }

    // 5. Publish.
    const publish = await waitFor(
      () => {
        const inEditor = [...editor.querySelectorAll('button')].find((b) =>
          /save clip|publish|salvar clip|publicar|criar clip|create clip/i.test((b.innerText || '').trim()),
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
    // Same portrait-layout modal guard as the /create save flow (above).
    await dismissPortraitModal();
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
