/**
 * Stress: 5 channel explore mini-previews + 1 main preview, concurrent seeks.
 * Phase A — start at 0, play 30s. Phase B — jump to 75% on every player.
 */
import { test, expect, type Page } from '@playwright/test';

const UI_URL = 'http://localhost:5173';
const MAIN_URL = 'https://www.youtube.com/watch?v=4kyvGbRpV7M';

type PlayerProbe = {
  label: string;
  kind: 'video' | 'slider';
  index: number;
  duration: number;
  currentTime: number;
  ready: boolean;
  playing: boolean;
  error: string | null;
};

async function setReactRange(page: Page, locator: ReturnType<Page['locator']>, value: number) {
  await locator.evaluate((el, v) => {
    const input = el as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, String(v));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

async function probePlayers(page: Page): Promise<PlayerProbe[]> {
  return page.evaluate(() => {
    const out: PlayerProbe[] = [];
    const videos = Array.from(document.querySelectorAll('video'));
    videos.forEach((v, i) => {
      out.push({
        label: `video#${i}`,
        kind: 'video',
        index: i,
        duration: Number.isFinite(v.duration) ? v.duration : 0,
        currentTime: v.currentTime,
        ready: v.readyState >= 2,
        playing: !v.paused && !v.ended,
        error: v.error ? String(v.error.code) : null,
      });
    });
    const popups = Array.from(
      document.querySelectorAll('[role="application"][aria-label*="explore player"]'),
    );
    popups.forEach((popup, i) => {
      const slider = popup.querySelector('input[type="range"]') as HTMLInputElement | null;
      if (!slider) return;
      const max = parseFloat(slider.max || '0');
      const val = parseFloat(slider.value || '0');
      const iframe = popup.querySelector('iframe');
      out.push({
        label: `popup#${i}${iframe ? '-yt' : '-proxy'}`,
        kind: 'slider',
        index: i,
        duration: max,
        currentTime: val,
        ready: !slider.disabled && max > 0,
        playing: !slider.disabled,
        error: null,
      });
    });
    return out;
  });
}

async function seekAllTo(page: Page, ratio: number) {
  await page.evaluate((r) => {
    for (const v of document.querySelectorAll('video')) {
      if (v.duration > 0) {
        v.currentTime = Math.max(0, v.duration * r);
        void v.play().catch(() => {});
      }
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    for (const popup of document.querySelectorAll('[role="application"][aria-label*="explore player"]')) {
      const slider = popup.querySelector('input[type="range"]') as HTMLInputElement | null;
      if (!slider || slider.disabled) continue;
      const max = parseFloat(slider.max || '0');
      if (max <= 0) continue;
      const t = max * r;
      setter?.call(slider, String(t));
      slider.dispatchEvent(new Event('input', { bubbles: true }));
      slider.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }, ratio);
}

test.describe('Preview stress — 5 mini + main', () => {
  test.setTimeout(360_000);

  test('concurrent seeks: 0 → 30s play → 75%', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(`console: ${msg.text()}`);
    });

    await page.goto(UI_URL);
    await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 20_000 });

    // Channel mini-previews first (before main preview panel steals focus/clicks)
    await page.getByRole('button', { name: /channels/i }).click();
    let tripleChannel = page.getByRole('button', { name: /titiltei.*titiltei.*titiltei/i });
    if ((await tripleChannel.count()) === 0) {
      const addInput = page.getByPlaceholder(/kick \/ twitch \/ youtube name or url/i);
      await addInput.fill('https://www.youtube.com/@titiltei');
      await addInput.press('Enter');
      await page.getByRole('button', { name: /add channel/i }).click({ timeout: 30_000 });
      tripleChannel = page.getByRole('button', { name: /titiltei/i });
    }
    await tripleChannel.first().click({ force: true, timeout: 30_000 });
    await expect(page.getByRole('button', { name: /videos/i })).toBeVisible({ timeout: 30_000 });

    const previewBtns = page.getByRole('button', { name: /^preview$/i });
    await expect(previewBtns.first()).toBeVisible({ timeout: 60_000 });
    const count = await previewBtns.count();
    expect(count).toBeGreaterThanOrEqual(5);

    for (let i = 0; i < 5; i++) {
      await previewBtns.nth(i).click({ force: true });
      await page.waitForTimeout(500);
    }

    await expect(page.locator('[role="application"][aria-label*="explore player"]')).toHaveCount(5, {
      timeout: 60_000,
    });

    // Main preview last — tab bar only (not explore-popup "URL" carry buttons)
    await page.locator('.vod-app-shell').getByRole('button', { name: /^url$/i }).click();
    const urlInput = page.getByPlaceholder(/paste vod or clip link|vod or clip link/i);
    await urlInput.fill(MAIN_URL);
    await page.getByRole('button', { name: /extract info/i }).click();
    await expect(page.getByRole('button', { name: /watch preview/i })).toBeEnabled({ timeout: 120_000 });
    await page.getByRole('button', { name: /watch preview/i }).click();

    // Wait until main video + popups are ready
    await expect.poll(async () => {
      const probes = await probePlayers(page);
      const ready = probes.filter((p) => p.ready && p.duration > 0);
      return ready.length;
    }, { timeout: 120_000, intervals: [1000, 2000, 3000] }).toBeGreaterThanOrEqual(6);

    const before = await probePlayers(page);
    console.log('STRESS ready players:', JSON.stringify(before, null, 2));

    // Phase A: start at 0
    await seekAllTo(page, 0);
    await page.waitForTimeout(1500);

    // Play 30 seconds concurrently
    await page.waitForTimeout(30_000);

    const mid = await probePlayers(page);
    console.log('STRESS after 30s:', JSON.stringify(mid, null, 2));

    // Phase B: jump to 75%
    await seekAllTo(page, 0.75);
    await page.waitForTimeout(8000);

    const after = await probePlayers(page);
    console.log('STRESS after 75% seek:', JSON.stringify(after, null, 2));

    const fatalConsole = consoleErrors.filter(
      (e) => !e.includes('favicon') && !e.includes('ResizeObserver'),
    );

    // Every player must have advanced and still be healthy
    for (const p of after) {
      expect(p.duration, `${p.label} duration`).toBeGreaterThan(0);
      expect(p.error, `${p.label} error`).toBeNull();
      if (p.kind === 'video') {
        expect(p.currentTime, `${p.label} at 75%`).toBeGreaterThan(p.duration * 0.5);
      } else {
        expect(p.currentTime, `${p.label} at 75%`).toBeGreaterThan(p.duration * 0.5);
      }
    }

    const playingCount = after.filter((p) => p.playing || p.currentTime > 0).length;
    expect(playingCount).toBeGreaterThanOrEqual(6);
    expect(fatalConsole).toEqual([]);
  });
});
