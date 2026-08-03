// E2E: live-popup quality policy — twitch/kick live keep levels up to source
// (no 1080 cap), youtube live caps at 1080 (or 360 when anonymous). Drives the
// real LivePlayerPopup through the real app UI; the live-status poll and the
// POST /api/preview/live response are intercepted to control platform +
// anonymous per scenario (the backend flags are covered by unit tests).
//
// Run: node e2e/repro-quality-policy-live.mjs   (own-stack vite on :5273,
// backend :8101; channel add uses the real API)
import { chromium } from 'playwright';

const VITE = process.env.QUALITY_VITE || 'http://127.0.0.1:5273';
const TWITCH_VOD = 'https://www.twitch.tv/videos/2835635556';

// Master ladder: 144p..2160p — proves the twitch/kick path KEEPS >1080p source
// levels while the youtube path caps at 1080.
const MASTER = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=256x144
${VITE}/live/media-144.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
${VITE}/live/media-360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=854x480
${VITE}/live/media-480.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
${VITE}/live/media-720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080
${VITE}/live/media-1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=2560x1440
${VITE}/live/media-1440.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=12000000,RESOLUTION=3840x2160
${VITE}/live/media-2160.m3u8
`;

const segs = (n) => Array.from({ length: n }, (_, i) =>
  `#EXTINF:2.0,\n${VITE}/live/seg${i}.ts\n`).join('');

// Live media playlist: NO #EXT-X-ENDLIST (live-capable preview), 5x2s = 10s.
const LIVE_MEDIA = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
${segs(5)}`;

const TS_BYTES = Buffer.concat([
  Buffer.from([0x47, 0x40, 0x00, 0x10]), // TS sync
  Buffer.alloc(188 * 8 - 4, 7),          // padding
]);

async function runScenario(browser, label, { platform, anonymous, expectItems, extraTitle }) {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 300)));

  // Route interception: live status poll + every m3u8/ts + the live session POST.
  // Channel content is intercepted too — the real gql refresh is slow/flaky and
  // the ● LIVE row only renders after the channel refresh settles.
  await page.route('**/api/channel/videos**', (route) => {
    const url = route.request().url();
    const isStreams = url.includes('content=streams');
    void route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        videos: isStreams ? [] : [{
          id: 'v2835635556', platform: 'Twitch', title: 'synthetic row',
          duration: 3600, duration_string: '1:00:00',
          created_at: new Date(Date.now() - 60000).toISOString(),
          views: 1, thumbnail_url: null,
          url: 'https://www.twitch.tv/videos/2835635556',
          channel: 'summit1g', content_kind: 'stream',
        }],
        channel: 'summit1g', platforms: ['Twitch'], content: 'vods', days: 0,
      }),
    });
  });
  await page.route('**/api/channel/clips**', (route) => {
    void route.fulfill({ contentType: 'application/json', body: JSON.stringify({ videos: [] }) });
  });
  await page.route('**/api/channels/*/live**', (route) => {
    void route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        channel_id: 'ch_qa',
        fetched_at: Date.now(),
        live: [{
          platform, is_live: true,
          title: `${extraTitle || platform} (synthetic)`,
          url: `${VITE}/live/master.m3u8`,
          headers: {}, type: 'hls',
        }],
      }),
    });
  });
  await page.route('**/api/preview/live', (route) => {
    void route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: `qa_${platform}_${anonymous ? 'anon' : 'auth'}`,
        master_url: `${VITE}/live/master.m3u8`,
        playback_url: `${VITE}/live/master.m3u8`,
        kind: 'hls', is_live: true, anonymous,
        platform, archive_duration: 0, variant_heights: [144, 360, 480, 720, 1080, 1440, 2160],
        active_height: 360, growing_vod: false,
      }),
    });
  });
  await page.route('**/live/master.m3u8', (r) => r.fulfill({ contentType: 'application/vnd.apple.mpegurl', body: MASTER }));
  await page.route('**/live/media-*.m3u8', (r) => r.fulfill({ contentType: 'application/vnd.apple.mpegurl', body: LIVE_MEDIA }));
  await page.route('**/live/seg*.ts', (r) => r.fulfill({ contentType: 'video/mp2t', body: TS_BYTES }));

  await page.goto(VITE, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);

  // Add the channel through the real extract flow (real backend API).
  await page.fill('input[placeholder*="PASTE VOD"]', TWITCH_VOD);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(6000);
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('button')]
      .find((b) => (b.innerText || '').toUpperCase().includes('ADD CHANNEL'));
    if (btn) btn.click();
  });
  await page.waitForTimeout(2000);
  await page.click('text=CHANNELS');

  // Select the channel row so the detail view renders.
  await page.waitForSelector('[data-channel-row]', { timeout: 15000 });
  await page.evaluate(() => {
    const el = document.querySelector('[data-channel-row] div[role="button"]');
    if (el) el.click();
  });

  // Wait for the ● LIVE row and open the live popup.
  let clicked = false;
  for (let i = 0; i < 30 && !clicked; i++) {
    clicked = await page.evaluate(() => {
      const el = [...document.querySelectorAll('div[role="button"]')]
        .find((e) => /● LIVE|LIVE/.test(e.textContent || '') && /border-red/.test(e.className));
      if (el) { el.click(); return true; }
      return false;
    });
    if (!clicked) await page.waitForTimeout(2000);
  }
  if (!clicked) throw new Error(`scenario ${label}: live row never appeared`);

  // Wait for MANIFEST_PARSED -> quality menu to populate.
  let items = [];
  for (let i = 0; i < 30 && !items.length; i++) {
    await page.waitForTimeout(1500);
    items = await page.evaluate(() => {
      const gear = [...document.querySelectorAll('[data-player-menu] button[title="Video quality"]')][0];
      if (!gear) return [];
      gear.click();
      const menus = [...document.querySelectorAll('[data-player-menu]')].map((m) => {
        const pop = m.querySelector(':scope > div');
        return pop ? [...pop.querySelectorAll('button')].map((b) => b.innerText.trim()).filter(Boolean) : [];
      }).flat();
      return menus;
    });
  }
  await page.evaluate(() => {
    const gear = [...document.querySelectorAll('[data-player-menu] button[title="Video quality"]')][0];
    if (gear) gear.click();
  });

  console.log(`${label}: menu=${JSON.stringify(items)}`);

  const pass = JSON.stringify(items) === JSON.stringify(expectItems);
  if (!pass) {
    console.log(`  EXPECTED ${JSON.stringify(expectItems)}`);
  }
  if (errors.length) console.log(`  pageerrors: ${errors.join(' | ')}`);
  await context.close();
  return { label, items, pass, errors };
}

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  const results = [];
  try {
    results.push(await runScenario(browser, 'twitch-live', {
      platform: 'Twitch', anonymous: false,
      expectItems: ['360p', '480p', '720p', '1080p', '1440p', '2160p'],
      extraTitle: 'summit1g',
    }));
    results.push(await runScenario(browser, 'youtube-live-anonymous', {
      platform: 'YouTube', anonymous: true,
      expectItems: ['360p'],
    }));
    results.push(await runScenario(browser, 'youtube-live-cookies', {
      platform: 'YouTube', anonymous: false,
      expectItems: ['360p', '720p', '1080p'],
    }));
    results.push(await runScenario(browser, 'kick-live', {
      platform: 'Kick', anonymous: false,
      expectItems: ['360p', '480p', '720p', '1080p', '1440p', '2160p'],
    }));
  } finally {
    await browser.close();
  }
  const failed = results.filter((r) => !r.pass || r.errors.length);
  console.log('\n=== quality-policy live menu results ===');
  for (const r of results) console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.label}: ${JSON.stringify(r.items)}`);
  process.exitCode = failed.length ? 1 : 0;
})();
