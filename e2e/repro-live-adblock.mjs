// E2E: Twitch livestream ad blocking (vaft port) — the app's live preview
// popup must strip 'stitched' ad segments from the HLS playlist before
// hls.js parses it. Fully offline: the live status poll and every m3u8/ts
// request are route-intercepted; only POST /api/preview/live hits the real
// backend (create_live_session does not validate the URL).
//
// Flow: add channel -> fake live status -> ● LIVE row -> popup -> synthetic
// playlist [live1, stitched-ad1, live2, live1, live2] -> assert the ad
// segment is never requested, the strip counter bumps, and playback jumps
// past the ad position.
//
// Run: node e2e/repro-live-adblock.mjs   (requires dev server on :5173)
import { chromium } from 'playwright';
import { readFileSync } from 'node:fs';

const ADTEST = 'C:/tmp/adtest';
const live1 = readFileSync(`${ADTEST}/live1.ts`);
const live2 = readFileSync(`${ADTEST}/live2.ts`);

const MASTER = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
http://localhost:5173/live/media.m3u8
`;

// Real Twitch media playlists interleave the ad segment between live ones,
// with a low-latency prefetch hint pointing at the next live segment.
const MEDIA = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:2.0,
http://localhost:5173/live/live1.ts
#EXTINF:2.0,
http://localhost:5173/live/stitched-ad1.ts?dnt=1&sig=abc
#EXT-X-TWITCH-PREFETCH:http://localhost:5173/live/prefetch-live2.ts
#EXTINF:2.0,
http://localhost:5173/live/live2.ts
#EXTINF:2.0,
http://localhost:5173/live/live1.ts
#EXTINF:2.0,
http://localhost:5173/live/live2.ts
#EXT-X-ENDLIST
`;

const browser = await chromium.launch({
  executablePath: 'C:/Users/Administrador/AppData/Local/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-win64/chrome-headless-shell.exe',
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await ctx.newPage();

const requested = { ts: [], m3u8: [], stitched: 0, prefetch: 0 };
page.on('request', (r) => {
  const u = r.url();
  if (u.includes('.m3u8')) { console.log('REQ-m3u8:', u.slice(0, 140)); }
  if (u.includes('stitched')) requested.stitched++;
  if (u.includes('prefetch-live2')) requested.prefetch++;
  if (u.includes('.ts')) requested.ts.push(u);
  if (u.includes('.m3u8')) requested.m3u8.push(u);
});

const check = (ok, label) => { console.log(`${label}: ${ok ? 'PASS' : 'FAIL'}`); return ok ? 0 : 1; };
let failures = 0;

// ── routes (installed before adding the channel) ────────────────────────────
// Backend persists saved_channels in settings — clear it so a previous run's
// channel doesn't trip the duplicate guard in the add-channel card.
await fetch('http://localhost:7897/api/settings', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ saved_channels: [] }),
}).catch((e) => console.warn('settings reset failed:', String(e)));

await page.route('**/api/channel/videos**', (route) => {
  void route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ videos: [], channel: 'monstercat', platforms: ['Twitch'], content: 'vods', days: 7 }),
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
        url: 'http://localhost:5173/live/master.m3u8',
        headers: {},
        type: 'hls',
      }],
    }),
  });
});
await page.route('**/*.m3u8*', (route) => {
  const u = route.request().url();
  const body = u.includes('/live/media') ? MEDIA : MASTER;
  void route.fulfill({ contentType: 'application/vnd.apple.mpegurl', body });
});
await page.route('**/live/*.ts*', (route) => {
  const u = route.request().url();
  const body = u.includes('live2') ? live2 : live1;
  void route.fulfill({ contentType: 'video/mp2t', body });
});

// ── drive the app ────────────────────────────────────────────────────────────
await page.goto('http://localhost:5173/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
await page.click('text=CHANNELS');
await page.waitForTimeout(600);
await page.fill('input[placeholder*="KICK / TWITCH / YOUTUBE"]', 'https://www.twitch.tv/monstercat');
await page.keyboard.press('Enter');
await page.waitForSelector('text=Add channel', { timeout: 5000 });
await page.click('text=Add channel');
await page.waitForSelector('text=monstercat', { timeout: 8000 });

// ● LIVE row appears once the (fake) live poll lands and the empty VOD list renders
await page.waitForSelector('div[role="button"]:has-text("● LIVE")', { timeout: 15000 });
await page.click('div[role="button"]:has-text("● LIVE")');

// ── assertions ───────────────────────────────────────────────────────────────
await page.waitForFunction(() => (window.__vodripAdSegmentsStripped ?? 0) >= 1, null, { timeout: 15000 })
  .catch(() => {});
const stripped = await page.evaluate(() => window.__vodripAdSegmentsStripped ?? 0);
failures += check(stripped >= 1, `1 ad strip counter bumped (got ${stripped})`);

const playedPast = await page.waitForFunction(() => {
  const v = Array.from(document.querySelectorAll('video')).find((el) => el.currentTime > 4);
  return v ? v.currentTime : 0;
}, null, { timeout: 20000 }).then((h) => h.jsonValue()).catch(() => 0);
failures += check(playedPast >= 4, `2 playback advanced past the ad position (currentTime=${playedPast.toFixed(2)})`);

failures += check(requested.stitched === 0, `3 ad (stitched) segment never requested (got ${requested.stitched})`);
failures += check(requested.prefetch === 0, `4 EXT-X-TWITCH-PREFETCH line removed (got ${requested.prefetch})`);
const tsNames = requested.ts.map((u) => u.split('/').pop().split('?')[0]);
failures += check(tsNames.includes('live1.ts') && tsNames.includes('live2.ts'), `5 live segments played (${[...new Set(tsNames)].join(', ')})`);

const popupOpen = await page.locator('div.group:has(video)').count();
failures += check(popupOpen > 0, '6 live popup still open');
const popupText = await page.evaluate(() => {
  const groups = Array.from(document.querySelectorAll('div.group'));
  return groups.map((g) => ({ cls: g.className.slice(0, 60), hasVideo: !!g.querySelector('video'), text: (g.textContent || '').replace(/\s+/g, ' ').slice(0, 120) })).filter((x) => x.hasVideo || x.text);
});
console.log('GROUPS:', JSON.stringify(popupText));
const liveRowCount = await page.locator('div[role="button"]:has-text("● LIVE")').count();
console.log('LIVE-ROW-count:', liveRowCount);
const errorText = (popupText || []).map((g) => g.text).join(' ');
failures += check(!errorText.includes('Failed to start live stream') && !errorText.includes('No response'), '7 no session/playback error shown');

console.log(`\nRESULT: ${failures === 0 ? 'ALL PASS' : failures + ' FAILURES'}`);
console.log('m3u8 requests:', requested.m3u8.length, '| ts requests:', requested.ts.length);
console.log('LIVE-TS:', JSON.stringify(requested.ts.filter(u=>u.includes('/live/'))));
await browser.close();
process.exit(failures === 0 ? 0 : 1);
