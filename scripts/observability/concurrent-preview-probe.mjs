/**
 * concurrent-preview-probe.mjs - open N previews AT ONCE and count how many play.
 *
 * WHY THIS EXISTS: every other preview harness in this repo opens ONE preview,
 * measures it, closes it, and moves on. The failure the owner photographed is
 * SIX previews open simultaneously, all stuck on "Carregando transmissao ao
 * vivo...", and that case was never measured until this file.
 *
 * MEASURED 2026-10-16, frozen build, VODRIP_NO_DAEMONS UNSET (see AGENTS.md -
 * that flag invalidates any latency measurement), six previews at once:
 *
 *   t+0.6s..t+19.5s   videos=6 ready=0 playing=0     <- all six spinners, 20 s
 *   t+22.6s           videos=5 ready=1 playing=1
 *   t+44.4s           videos=5 ready=5 playing=1
 *
 * THE METRIC IS ready, NOT playing. Opening a preview pauses every other one by
 * design (LivePlayerPopup joins the shared preview-pause bus), so playing=1 with
 * six open is CORRECT and the first version of this probe wrongly called it a
 * failure. What the owner photographed is the SPINNER - "Carregando transmissao
 * ao vivo..." - which is readyState < 2. So the number to move is the time until
 * all N are ready: 20-26 s in the run above, against a 3 s target.
 *   t+44.4s           videos=5 ready=5 playing=1   (47 s, only ONE plays)
 *   request log:      20+ panel/twitch/...?limit=2000 PENDING at capture end
 *
 * That run motivated routing the panel poll through the shared budget lane
 * (src/components/PreviewChatPanel.tsx, _panelLane). Re-run THIS file after a
 * rebuild to confirm all six play; the numbers above are the before arm.
 *
 * USAGE (app must already be running and healthy):
 *   $env:BASE_URL="http://127.0.0.1:7897/"; $env:N="6"; $env:TIMEOUT="45000"
 *   node scripts/observability/concurrent-preview-probe.mjs
 *
 * N       previews to open simultaneously (default 6)
 * TIMEOUT ms to wait for all of them to reach playing (default 45000)
 *
 * Reports: a progress line every 3 s (videos / readyState>=2 / actually
 * playing), the final verdict, and the last 24 /api/preview requests with
 * their start, end and status - PENDENTE forever is the starvation signature.
 */
import { chromium } from 'playwright';
const EXE = 'C:/Users/Administrador/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:7897/';
const N = Number(process.env.N || 6);        // previews to open AT ONCE
const TIMEOUT = Number(process.env.TIMEOUT || 45000);
const ALL = ['link-download','channels','transcribe','live-subtitles','live-preview','live-chat','clip-editor','twitch-clip','archive-search','channel-explore','local-file','ai-ask'];
const browser = await chromium.launch({ headless: true, executablePath: EXE, args: ['--autoplay-policy=no-user-gesture-required','--mute-audio','--disable-dev-shm-usage','--no-sandbox'] });
const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage();
page.on('pageerror', e => console.log('PAGEERROR', String(e).slice(0,160)));
await page.route('**/api/plugins', (route) => route.abort());
await page.addInitScript((ids) => {
  try {
    localStorage.setItem('vodrip.plugins', JSON.stringify(Object.fromEntries(ids.map((i) => [i, true]))));
    localStorage.setItem('vodrip.onboardingDone', '1');
    localStorage.setItem('vodrip.firstTime.cookieInstall', '1');
  } catch (e) {}
  const T = { reqs: [], t0: performance.now() };
  window.__TAP = T;
  const of = window.fetch;
  window.fetch = function (...a) {
    const u = String(a[0] && a[0].url ? a[0].url : a[0]);
    const rec = { t: performance.now() - T.t0, url: u.includes('/api/') ? '/api/' + u.split('/api/')[1] : u, done: null };
    T.reqs.push(rec);
    return of.apply(this, a).then(r => { rec.done = performance.now() - T.t0; rec.status = r.status; return r; });
  };
}, ALL);
await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(7000);
for (let k = 0; k < 3; k++) {
  const wiz = page.getByRole('button', { name: /Tudo|Só o essencial/ });
  if (await wiz.count()) { await wiz.first().click().catch(()=>{}); await page.waitForTimeout(800); }
  const save = page.getByRole('button', { name: /Salvar e continuar/ });
  if (await save.count()) { await save.click().catch(()=>{}); await page.waitForTimeout(800); }
}
await page.waitForTimeout(4000);
await page.getByRole('button', { name: /CANAIS/ }).first().click();
await page.waitForTimeout(6000);
const rows = await page.$$eval('[data-channel-row]', els => els.map(e => e.textContent.slice(0,40)));
let idx = rows.findIndex(r => /srdogg/i.test(r)); if (idx < 0) idx = 0;
await page.locator('[data-channel-row]').nth(idx).locator('div[role=button]').first().click();
await page.waitForFunction(() => document.querySelectorAll('button[title^="Prévia"]').length > 3, null, { timeout: 90000 });
await page.waitForTimeout(1500);
const total = await page.$$eval('button[title^="Prévia"]', b => b.length);
console.log('botoes Previa disponiveis: ' + total + ' | vou abrir ' + N + ' DE UMA VEZ');
// instrument: count playable videos
await page.evaluate(() => {
  const T = window.__TAP;
  T.play = [];
  const iv = setInterval(() => {
    const vs = [...document.querySelectorAll('video')];
    T.play = vs.map(v => ({ rs: v.readyState, t: performance.now() - T.t0, paused: v.paused, ct: +(v.currentTime || 0).toFixed(1) }));
  }, 250);
  T._iv = iv;
});
const t0 = Date.now();
// CLICK ALL AT ONCE
await page.evaluate((n) => {
  const bs = [...document.querySelectorAll('button[title^="Prévia"]')].slice(0, n);
  bs.forEach(b => b.click());
  window.__TAP.clickAt = performance.now() - window.__TAP.t0;
}, N);
let result = null;
for (let i = 0; i < Math.ceil(TIMEOUT / 500); i++) {
  await page.waitForTimeout(500);
  const st = await page.evaluate(() => {
    const vs = [...document.querySelectorAll('video')];
    const playing = vs.filter(v => v.readyState >= 2 && !v.paused && v.currentTime > 0.05);
    const ready = vs.filter(v => v.readyState >= 2);
    return { videos: vs.length, ready: ready.length, playing: playing.length };
  });
  if (i % 6 === 0) console.log('  t+' + ((Date.now()-t0)/1000).toFixed(1) + 's videos=' + st.videos + ' ready=' + st.ready + ' playing=' + st.playing);
  // SUCCESS = every preview reached readyState >= 2 (the spinner is gone).
  // It is NOT "every preview is playing": opening a preview pauses every other
  // one on purpose - LivePlayerPopup joins the shared preview-pause bus
  // ("opening a live pauses every other preview", LivePlayerPopup.tsx:337, with
  // a test at LivePlayerPopup.test.tsx:521). Exactly ONE playing is the DESIGN,
  // so a playing>=N verdict measures the wrong thing and would never pass.
  if (st.ready >= N) { result = { ok: true, ms: Date.now() - t0, ...st }; break; }
  if (i === Math.ceil(TIMEOUT/500) - 1) result = { ok: false, ms: Date.now() - t0, ...st };
}
console.log('');
console.log('=== RESULTADO: ' + N + ' previas DE UMA VEZ ===');
console.log('  TODAS PRONTAS (readyState>=2, sem spinner): ' + (result.ok ? 'SIM' : 'NAO') + '  em ' + result.ms + ' ms');
console.log('  videos=' + result.videos + ' ready=' + result.ready + ' playing=' + result.playing + ' (alvo ready ' + N + ')');
console.log('  nota: playing=1 e o DESIGN (preview-pause bus); o alvo medido e ready=' + N + '.');
const reqs = await page.evaluate(() => window.__TAP.reqs.map(r => ({ p: r.url.replace(/^.*\/api\/preview\//,'').slice(0,44), s: r.t, e: r.done, st: r.status })));
console.log('');
console.log('=== requisicoes (ms desde o load) ===');
// EARLIEST requests, not the latest. The previous version printed slice(-24),
// which in a 70 s run showed only the calm tail where everything completes in
// ~10 ms - the exact opposite of the window that matters. What holds sockets
// during the first seconds is the question, so print the first 40 by start time.
const first = [...reqs].sort((a, b) => a.s - b.s).slice(0, 40);
console.log('');
console.log('=== as PRIMEIRAS 40 requisicoes (a janela que decide) ===');
for (const r of first) {
  const dur = r.e === null ? '  pendente' : String(Math.round(r.e - r.s)).padStart(6) + ' ms';
  console.log('  @' + String(Math.round(r.s)).padStart(7) + '  ' + dur + '  [' + r.st + ']  ' + r.p);
}
const stillPending = reqs.filter(r => r.e === null).length;
console.log('  pendentes no fim: ' + stillPending + ' de ' + reqs.length);
// ── RESOURCE TIMING: attribute the seconds to a PHASE ─────────────────────
// The fetch tap above records only start/end, which cannot separate "waited
// for a socket" from "the server was slow". Resource Timing can, is
// same-origin here (UI and API share 127.0.0.1:7897), so the full breakdown
// is exposed without touching hls.js internals:
//   queue  = fetchStart - startTime      (waiting for a connection slot)
//   server = responseStart - requestStart (backend thinking)
//   total  = duration
// Whichever dominates names the phase, and that decides which fix is real.
const rt = await page.evaluate(() => {
  const out = [];
  for (const e of performance.getEntriesByType('resource')) {
    if (!/\/api\/(preview|channel|channels)/.test(e.name)) continue;
    out.push({
      n: e.name.replace(/^.*\/api\//, '/api/').split('?')[0].slice(0, 46),
      start: Math.round(e.startTime),
      fetchStart: Math.round(e.fetchStart),
      reqStart: Math.round(e.requestStart),
      resStart: Math.round(e.responseStart),
      resEnd: Math.round(e.responseEnd),
      dur: Math.round(e.duration),
      size: e.transferSize || 0,
    });
  }
  return out;
});
console.log('');
// FOUR phases, not two. The first version of this printed only
// fetchStart-startTime as "queue" and responseStart-requestStart as "server",
// and reported the queue as 0 ms on every request - which was wrong and nearly
// became another wrong conclusion. A /api/preview/session with total 10211 ms
// and server 839 ms left 9372 ms unaccounted for, and a 1 KB response cannot
// spend 9 s transferring: the wait sits in fetchStart -> requestStart, which is
// CONNECTION ACQUISITION, the phase the browser 6-per-origin limit shows up in.
// It was simply not printed. All four now are:
//   sched = fetchStart   - startTime     (scheduler / not-yet-started)
//   conn  = requestStart - fetchStart    (waiting for a free connection) <- the queue
//   srv   = responseStart- requestStart  (backend thinking)
//   xfer  = responseEnd  - responseStart (transfer)
console.log('=== RESOURCE TIMING: as 18 mais lentas, 4 fases ===');
const byDur = [...rt].sort((a, b) => b.dur - a.dur).slice(0, 18);
console.log('  conn(ms)  srv(ms)  xfer(ms)  total(ms)  inicio(ms)  tam   url');
for (const e of byDur) {
    const conn = e.reqStart - e.fetchStart;
    const srv = e.resStart - e.reqStart;
    const xfer = e.resEnd - e.resStart;
    console.log(
      '  ' + String(conn).padStart(8) + '  ' + String(srv).padStart(7) +
      '  ' + String(xfer).padStart(8) + '  ' + String(e.dur).padStart(9) +
      '  ' + String(e.start).padStart(10) +
      '  ' + String(Math.round(e.size / 1024)).padStart(5) + 'K  ' + e.n,
    );
  }
const totQueue = rt.reduce((a, e) => a + Math.max(0, e.reqStart - e.fetchStart), 0);
const totServer = rt.reduce((a, e) => a + Math.max(0, e.resStart - e.reqStart), 0);
const totXfer = rt.reduce((a, e) => a + Math.max(0, e.resEnd - e.resStart), 0);
const totDur = rt.reduce((a, e) => a + e.dur, 0);
console.log('  ---------------------------------------------');
console.log('  requisicoes medidas: ' + rt.length);
console.log('  soma CONN (fila): ' + totQueue + ' ms');
console.log('  soma SERVIDOR:    ' + totServer + ' ms');
  console.log('  soma TRANSFER:    ' + totXfer + ' ms');
console.log('  soma TOTAL:    ' + totDur + ' ms');
// per-phase totals for the preview-relevant subset
const pv = rt.filter((e) => /preview|hls/.test(e.n));
console.log('  --- so preview/hls (' + pv.length + ') ---');
console.log('    CONN:     ' + pv.reduce((a, e) => a + Math.max(0, e.reqStart - e.fetchStart), 0) + ' ms');
console.log('    SERVIDOR: ' + pv.reduce((a, e) => a + Math.max(0, e.resStart - e.reqStart), 0) + ' ms');
// where the time sits relative to the click
const clickAt = await page.evaluate(() => window.__TAP.clickAt || 0);
console.log('    click em @' + Math.round(clickAt) + ' ms');
const pvAfterClick = pv.filter((e) => e.start >= clickAt);
if (pvAfterClick.length) {
  const first = pvAfterClick.sort((a, b) => a.start - b.start)[0];
  console.log('    primeira req preview pos-clique @' + Math.round(first.start) + ' (delta ' + Math.round(first.start - clickAt) + ' ms)');
}

await browser.close();