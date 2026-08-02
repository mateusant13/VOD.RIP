// E2E: live popup DVR overhaul — quality ladder (360-1080, default 480),
// open-channel button, LIVE <-> REPLAY mode switching with a growing archive
// snapshot (re-snapshot extends the seek rail), and resize parity with the
// VOD preview panel. Fully offline: every API/media route is intercepted in
// the browser (the live session POST included, so the crafted response can
// carry archive_url); only the settings reset hits the real backend.
//
// Run: node e2e/repro-live-dvr.mjs   (requires dev server on :5176)
import { chromium } from 'playwright';
import { readFileSync } from 'node:fs';

const ADTEST = 'C:/tmp/adtest';
const live1 = readFileSync(`${ADTEST}/live1.ts`);
const live2 = readFileSync(`${ADTEST}/live2.ts`);

const PORT = 5176;
const ORIGIN = `http://localhost:${PORT}`;

// Six-variant master: 144p/1440p must be filtered out by the popup, leaving
// 360-1080 with ORIGINAL hls.levels indices; default = closest to 480 (idx 2).
const MASTER = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=256x144
${ORIGIN}/live/media-144.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
${ORIGIN}/live/media-360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=854x480
${ORIGIN}/live/media-480.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
${ORIGIN}/live/media-720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080
${ORIGIN}/live/media-1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=2560x1440
${ORIGIN}/live/media-1440.m3u8
`;

const segs = (n) => Array.from({ length: n }, (_, i) =>
  `#EXTINF:2.0,\n${ORIGIN}/live/seg${i}.ts\n`).join('');

// Live media playlist: NO #EXT-X-ENDLIST (live-capable preview), 5x2s = 10s.
const LIVE_MEDIA = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:0
${segs(5)}`;

// Replay snapshot: what open_replay_hls_proxy returns — ENDLIST-terminated.
// First snapshot 5x2s (10s); every re-snapshot 7x2s (14s) = the archive grew.
let snapCount = 0;
const snapshotPlaylist = () => {
  snapCount += 1;
  const n = snapCount === 1 ? 5 : 7;
  return `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:0
${segs(n)}#EXT-X-ENDLIST
`;
};

const browser = await chromium.launch({
  executablePath: 'C:/Users/Administrador/AppData/Local/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-win64/chrome-headless-shell.exe',
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await ctx.newPage();

const requested = { master: 0, ts: [], snapshots: [] };
page.on('request', (r) => {
  const u = r.url();
  if (u.includes('/master.m3u8')) requested.master++;
  if (u.includes('/live/') && u.includes('.ts')) requested.ts.push(u);
  if (u.includes('resource?id=replay-playlist')) requested.snapshots.push(u);
});

const check = (ok, label) => { console.log(`${label}: ${ok ? 'PASS' : 'FAIL'}`); return ok ? 0 : 1; };
let failures = 0;

// ── routes ──────────────────────────────────────────────────────────────────
await fetch('http://localhost:7897/api/settings', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ saved_channels: [] }),
}).catch((e) => console.warn('settings reset failed:', String(e)));

// Fake channel VOD list — one Twitch stream row so App derives vodUrl (exercises
// the render-time slug/vodUrl wiring AND the DVR request body).
await page.route('**/api/channel/videos**', (route) => {
  void route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      videos: [{
        id: 'v987654321',
        platform: 'Twitch',
        title: 'Synthetic e2e VOD (growing archive)',
        duration: 14400,
        duration_string: '4:00:00',
        created_at: new Date(Date.now() - 60_000).toISOString(),
        views: 1,
        thumbnail_url: null,
        url: 'https://www.twitch.tv/videos/987654321',
        channel: 'monstercat',
        content_kind: 'stream',
      }],
      channel: 'monstercat',
      platforms: ['Twitch'],
      content: 'vods',
      days: 7,
    }),
  });
});
await page.route('**/api/channels/*/live**', (route) => {
  const m = /\/api\/channels\/([^/?]+)\/live/.exec(route.request().url());
  const cid = m ? m[1] : 'ch_unknown';
  void route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      channel_id: cid,
      fetched_at: Date.now(),
      live: [{
        platform: 'Twitch',
        is_live: true,
        title: 'Monstercat (synthetic e2e)',
        url: `${ORIGIN}/live/master.m3u8`,
        headers: {},
        type: 'hls',
      }],
    }),
  });
});
// Live session creation — crafted response carrying the DVR archive fields the
// (stale) shared backend cannot produce for a dummy URL.
await page.route('**/api/preview/live**', (route) => {
  void route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      session_id: 'e2e-live-001',
      master_url: '/api/preview/hls/e2e-live-001/master.m3u8',
      playback_url: '/api/preview/hls/e2e-live-001/master.m3u8',
      kind: 'hls',
      variant_heights: [144, 360, 480, 720, 1080, 1440],
      quality_labels: [],
      active_height: 0,
      extract_source: '',
      mux_ready: true,
      playlist_ready: true,
      segment_buffer_ready: true,
      trim_timeline: false,
      duration_sec: 14400,
      window_hls_mux_start: 0,
      window_hls_mux_end: 0,
      cached_progressive: false,
      is_live: true,
      growing_vod: false,
      archive_url: '/api/preview/hls/e2e-live-001/archive.m3u8',
      archive_duration: 10,
    }),
  });
});
await page.route('**/api/preview/session/e2e-live-001**', (route) => {
  void route.fulfill({ contentType: 'application/json', body: '{}' });
});
await page.route('**/api/preview/hls/*/master.m3u8*', (route) => {
  void route.fulfill({ contentType: 'application/vnd.apple.mpegurl', body: MASTER });
});
await page.route('**/live/media-*.m3u8*', (route) => {
  void route.fulfill({ contentType: 'application/vnd.apple.mpegurl', body: LIVE_MEDIA });
});
await page.route('**/resource?id=replay-playlist*', (route) => {
  void route.fulfill({ contentType: 'application/vnd.apple.mpegurl', body: snapshotPlaylist() });
});
await page.route('**/live/*.ts*', (route) => {
  const u = route.request().url();
  const body = /seg[1357]\.ts/.test(u) ? live2 : live1;
  void route.fulfill({ contentType: 'video/mp2t', body });
});

// ── drive the app ───────────────────────────────────────────────────────────
await page.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
await page.click('text=CHANNELS');
await page.waitForTimeout(600);
await page.fill('input[placeholder*="KICK / TWITCH / YOUTUBE"]', 'https://www.twitch.tv/monstercat');
await page.keyboard.press('Enter');
await page.waitForSelector('text=Add channel', { timeout: 5000 });
await page.click('text=Add channel');
await page.waitForSelector('text=monstercat', { timeout: 8000 });
await page.waitForSelector('div[role="button"]:has-text("● LIVE")', { timeout: 15000 });
await page.click('div[role="button"]:has-text("● LIVE")');

const popup = () => page.locator('div.group').filter({ has: page.locator('video') });
await page.waitForSelector('[data-live-transport]', { timeout: 20000 });
await page.waitForTimeout(600);

// ── assertions ──────────────────────────────────────────────────────────────
const popupText = async () => (await popup().textContent() || '').replace(/\s+/g, ' ');
let text = await popupText();
failures += check(text.includes('🔴 LIVE'), '1 popup opens in LIVE mode');
failures += check(!text.includes('Failed to start live stream') && !text.includes('No response'), '2 no session/playback error shown');
failures += check(await popup().locator('button[title="Open channel"]').count() === 1, '3 open-channel button rendered (twitch slug)');

const playedPast = await page.waitForFunction(() => {
  const v = Array.from(document.querySelectorAll('video')).find((el) => el.currentTime > 4);
  return v ? v.currentTime : 0;
}, null, { timeout: 20000 }).then((h) => h.jsonValue()).catch(() => 0);
failures += check(playedPast >= 4, `4 live playback advanced past 4s (currentTime=${playedPast.toFixed(2)})`);

// Quality ladder: 360-1080 only (144/1440 filtered), ORIGINAL indices, default 480.
await popup().locator('button[title="Video quality"]').click();
await page.waitForTimeout(300);
const quality = await page.evaluate(() => {
  const group = Array.from(document.querySelectorAll('div.group')).find((g) => g.querySelector('video'));
  if (!group) return [];
  return Array.from(group.querySelectorAll('[data-player-menu] div button')).map((b) => ({
    label: (b.textContent || '').trim(),
    active: b.className.includes('text-white'),
  }));
});
const labels = quality.map((q) => q.label);
failures += check(labels.length === 4, `5 quality menu shows 4 ladder entries (got ${labels.length}: ${labels.join(' | ') || 'none'})`);
failures += check(labels.every((l) => /^[0-9]+p/.test(l)), '6 all menu entries are height-labeled');
failures += check(labels.some((l) => l.startsWith('360p')) && labels.some((l) => l.startsWith('1080p')), '7 ladder spans 360p-1080p');
failures += check(!labels.some((l) => l.startsWith('144p') || l.startsWith('1440p')), '8 out-of-range levels filtered (no 144p/1440p)');
const active480 = quality.find((q) => q.label.startsWith('480p'));
failures += check(Boolean(active480?.active), '9 default level is 480p (highlighted)');
await page.mouse.click(10, 10); // close menu

// LIVE -> REPLAY: drag the rail back to ~4s (40% of a 10s max).
const rail = popup().locator('input[type="range"]');
const railBox = await rail.boundingBox();
await page.mouse.click(railBox.x + railBox.width * 0.4, railBox.y + railBox.height / 2);
await page.waitForTimeout(1200);
text = await popupText();
failures += check(text.includes('⏪ REPLAY'), '10 rail drag switches to REPLAY mode');
const ctAfterDrag = await page.evaluate(() => Array.from(document.querySelectorAll('video')).find((el) => el.currentTime > 0)?.currentTime ?? 0);
failures += check(Math.abs(ctAfterDrag - 4) < 2, `11 replay positioned at dragged time (currentTime=${ctAfterDrag.toFixed(2)})`);

// Seek within the snapshot: drag to ~80% (8s).
await page.mouse.click(railBox.x + railBox.width * 0.8, railBox.y + railBox.height / 2);
await page.waitForTimeout(800);
const ctAfterSeek = await page.evaluate(() => Array.from(document.querySelectorAll('video')).find((el) => el.currentTime > 0)?.currentTime ?? 0);
failures += check(Math.abs(ctAfterSeek - 8) < 2, `12 replay seek within snapshot (currentTime=${ctAfterSeek.toFixed(2)})`);

// Parked re-snapshot: after ~30s the popup re-snapshots and the rail grows 10 -> 14.
const railMaxBefore = await rail.evaluate((el) => el.max);
failures += check(parseFloat(railMaxBefore) >= 9.5, `13 replay rail max = snapshot duration ~10 (got ${railMaxBefore})`);
await page.waitForFunction(() => {
  const group = Array.from(document.querySelectorAll('div.group')).find((g) => g.querySelector('video'));
  const r = group?.querySelector('input[type="range"]');
  return r && parseFloat(r.max) > 13;
}, null, { timeout: 45000 }).then((h) => h.jsonValue()).catch(() => false);
const railMaxAfter = await rail.evaluate((el) => el.max);
failures += check(parseFloat(railMaxAfter) > 13, `14 parked re-snapshot grew the rail (max ${railMaxBefore} -> ${railMaxAfter})`);
failures += check(requested.snapshots.length >= 2, `15 re-snapshot issued a fresh cache-busted request (got ${requested.snapshots.length})`);

// REPLAY -> LIVE: the LIVE button snaps back to the live master.
const masterBefore = requested.master;
await popup().locator('button[title="Return to live"]').click();
await page.waitForTimeout(1500);
text = await popupText();
failures += check(text.includes('🔴 LIVE'), '16 LIVE button returns to LIVE mode');
failures += check(requested.master > masterBefore, `17 live master re-fetched after return (count ${requested.master} > ${masterBefore})`);

// Resize parity: east +120px, west clamp at LIVE_PANEL_MIN_W (320), nw clamp on-screen.
const handles = page.locator('div[data-live-popup] > div[data-panel-resize]');
const pickHandle = async (nearX, nearY) => {
  const boxes = await handles.evaluateAll((els) => els.map((el) => {
    const r = el.getBoundingClientRect();
    return { cx: r.x + r.width / 2, cy: r.y + r.height / 2 };
  }));
  return boxes.reduce((best, b) => {
    const d = (b.cx - nearX) ** 2 + (b.cy - nearY) ** 2;
    return d < best.d ? { ...b, d } : best;
  }, { cx: Infinity, cy: Infinity, d: Infinity });
};
const popupBox = async () => (await popup().boundingBox()) || { x: 0, y: 0, width: 0, height: 0 };
const boxBefore = await popupBox();

const eHandle = await pickHandle(boxBefore.x + boxBefore.width + 6, boxBefore.y + boxBefore.height / 2);
await page.mouse.move(eHandle.cx, eHandle.cy);
await page.mouse.down();
await page.mouse.move(eHandle.cx + 120, eHandle.cy, { steps: 6 });
await page.mouse.up();
await page.waitForTimeout(400);
const boxEast = await popupBox();
failures += check(Math.abs(boxEast.width - (boxBefore.width + 120)) < 6, `18 east resize grows width by delta (${boxBefore.width} -> ${boxEast.width.toFixed(0)})`);

const wHandle = await pickHandle(boxEast.x - 6, boxEast.y + boxEast.height / 2);
await page.mouse.move(wHandle.cx, wHandle.cy);
await page.mouse.down();
await page.mouse.move(wHandle.cx + 600, wHandle.cy, { steps: 6 }); // rightward = shrink the west edge
await page.mouse.up();
await page.waitForTimeout(400);
const boxWest = await popupBox();
failures += check(Math.abs(boxWest.width - 320) < 3, `19 west resize clamps at LIVE_PANEL_MIN_W (width=${boxWest.width.toFixed(0)})`);

const nwHandle = await pickHandle(boxWest.x - 3, boxWest.y - 3);
await page.mouse.move(nwHandle.cx, nwHandle.cy);
await page.mouse.down();
await page.mouse.move(nwHandle.cx - 400, nwHandle.cy - 400, { steps: 6 });
await page.mouse.up();
await page.waitForTimeout(400);
const boxNw = await popupBox();
failures += check(boxNw.x >= -1 && boxNw.y >= -1, `20a nw resize keeps popup on-screen (pos=${boxNw.x.toFixed(0)},${boxNw.y.toFixed(0)})`);
failures += check(boxNw.x + boxNw.width <= 1601 && boxNw.y + boxNw.height <= 901, `20b nw resize keeps popup within viewport (right=${(boxNw.x + boxNw.width).toFixed(0)},bottom=${(boxNw.y + boxNw.height).toFixed(0)})`);

// Close cleanly.
await popup().locator('.live-popup-close').click();
await page.waitForTimeout(400);
failures += check(await popup().count() === 0, '21 close button tears down the popup');

console.log(`\nRESULT: ${failures === 0 ? 'ALL PASS' : failures + ' FAILURES'}`);
console.log('master requests:', requested.master, '| snapshot requests:', requested.snapshots.length, '| ts requests:', requested.ts.length);
await browser.close();
process.exit(failures === 0 ? 0 : 1);
