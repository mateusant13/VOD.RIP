import { test, expect } from '@playwright/test';

const UI_URL = process.env.UI_URL || 'http://localhost:5173';

// USER-LEVEL latency: one real click on a preview button, with every
// /api/preview/* request/response timestamped, so a stall is attributed to the
// client or the server instead of guessed at. The timeline prints even on failure.
test('one preview click: where does the time go', async ({ page }) => {
  test.setTimeout(240000);
  const events: string[] = [];
  const errors: string[] = [];
  const t0 = { v: Date.now() };
  const stamp = (s: string) => events.push((Date.now() - t0.v) + 'ms ' + s);
  page.on('request', (r) => {
    const u = r.url();
    if (u.includes('/api/preview')) stamp('REQ ' + r.method() + ' ' + u.split('/api/')[1].slice(0, 68));
  });
  page.on('response', (r) => {
    const u = r.url();
    if (u.includes('/api/preview')) stamp('RES ' + r.status() + ' ' + u.split('/api/')[1].slice(0, 68));
  });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message.slice(0, 140)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 140)); });
  let result = 'no measurement';
  let failure: unknown = null;
  try {
    await page.addInitScript(() => {
      localStorage.setItem('vodrip.onboardingDone', '1');
      localStorage.setItem('vodrip.firstTime.cookieInstall', '1');
    });
    let loaded = false;
    for (let attempt = 0; attempt < 3 && !loaded; attempt++) {
      try {
        await page.goto(UI_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 30000 });
        loaded = true;
      } catch {
        await page.waitForTimeout(2000);
      }
    }
    if (!loaded) throw new Error('app shell never became reachable at ' + UI_URL);
    await page.getByRole('button', { name: /channels/i }).click();
    const input = page.getByPlaceholder(/kick \/ twitch \/ youtube name or url/i);
    if (await input.count()) {
      await input.fill('https://www.youtube.com/@titiltei');
      await input.press('Enter');
      const add = page.getByRole('button', { name: /add channel/i });
      if (await add.count()) await add.click({ timeout: 30000 }).catch(() => {});
      await page.waitForTimeout(1500);
    }
    const chan = page.getByRole('button', { name: /titiltei/i }).first();
    await expect(chan).toBeVisible({ timeout: 60000 });
    await chan.click({ force: true });
    const vtab = page.getByRole('button', { name: /^(videos|vods)$/i }).first();
    await expect(vtab).toBeVisible({ timeout: 90000 });
    await vtab.click();
    const btn = page.getByRole('button', { name: /^preview$/i }).first();
    await expect(btn).toBeVisible({ timeout: 120000 });
    t0.v = Date.now();
    events.length = 0;
    stamp('CLICK preview');
    await btn.click({ force: true });
    const player = page.locator('[role="application"][aria-label*="explore player"]').first();
    const tPlayer = Date.now();
    await expect(player).toBeVisible({ timeout: 180000 });
    stamp('PLAYER_VISIBLE (+' + (Date.now() - tPlayer) + 'ms)');
    const video = player.locator('video').first();
    await expect(video).toBeAttached({ timeout: 60000 });
    let ready = -1;
    for (let i = 0; i < 240; i++) {
      const s = await video.evaluate((v) => ({ rs: v.readyState, t: v.currentTime })).catch(() => null);
      if (s && (s.rs >= 2 || s.t > 0)) { ready = Date.now() - t0.v; stamp('VIDEO_READY rs=' + s.rs + ' t=' + s.t.toFixed(2)); break; }
      await page.waitForTimeout(250);
    }
    result = 'player_visible=' + (tPlayer - t0.v) + 'ms video_ready=' + ready + 'ms';
    expect(ready, 'click->video ready was ' + ready + 'ms').toBeGreaterThanOrEqual(0);
  } catch (e) {
    result = 'FAILED before completion: ' + String(e).split('\n')[0].slice(0, 160);
    failure = e;
  } finally {
    console.log('--- TIMELINE (' + events.length + ' api events) ---');
    for (const e of events.slice(0, 45)) console.log(e);
    const last = events[events.length - 1];
    if (events.length > 45 && last) console.log('... last event: ' + last);
    if (errors.length) console.log('--- CLIENT ERRORS --- ' + JSON.stringify(errors.slice(0, 5)));
    console.log('RESULT ' + result);
  }
  if (failure) throw failure;
});
