/**
 * MSE-direct window-HLS playback (opt-in via VITE_PREVIEW_MSE_DIRECT).
 *
 * Run with the MSE-direct build:
 *   VITE_PREVIEW_MSE_DIRECT=true npx vite build && npx playwright test e2e/tests/preview-mse-direct.spec.ts
 *
 * The test skips automatically when the flag is not compiled in (it probes
 * `window.__VITE_PREVIEW_MSE_DIRECT__` which the app exposes when enabled).
 */
import { test, expect, type Page } from '@playwright/test';
import http from 'node:http';

const UI_URL = 'http://localhost:4173'; // preview build server
const YT_URL = 'https://www.youtube.com/watch?v=4kyvGbRpV7M';

async function openYoutubePreview(page: Page, url: string) {
  await page.goto(UI_URL);
  const input = page.getByPlaceholder(/url/i).first();
  await input.fill(url);
  await page.getByRole('button', { name: /preview|load/i }).first().click();
  // Wait for the preview video element to appear.
  await page.waitForSelector('video', { timeout: 30_000 });
}

/** True when the dedicated :4173 preview build answers (main suite never starts it). */
async function previewServerUp(): Promise<boolean> {
  const { promise, resolve } = Promise.withResolvers<boolean>();
  const req = http.get(UI_URL, (res) => {
    res.resume();
    resolve(true);
  });
  req.setTimeout(2_000, () => { req.destroy(); resolve(false); });
  req.on('error', () => resolve(false));
  return promise;
}

test.describe('MSE-direct window-HLS', () => {
  test.beforeAll(async () => {
    // This spec targets a dedicated `vite preview` build on :4173 (see file
    // header). The main suite never starts it — skip cleanly instead of
    // failing with ERR_CONNECTION_REFUSED on goto().
    test.skip(!(await previewServerUp()), 'no preview build server on :4173 — run `npx vite preview --port 4173` with VITE_PREVIEW_MSE_DIRECT=true');
  });

  test('attaches a blob: MediaSource URL and plays', async ({ page }) => {
    await openYoutubePreview(page, YT_URL);

    const flagOn = await page.evaluate(
      () => (window as unknown as { __VITE_PREVIEW_MSE_DIRECT__?: boolean }).__VITE_PREVIEW_MSE_DIRECT__ === true,
    );
    test.skip(!flagOn, 'VITE_PREVIEW_MSE_DIRECT not compiled in — build with the flag to run this spec');

    // Give the MSE player time to attach + buffer.
    await page.waitForFunction(
      () => {
        const v = document.querySelector('video') as HTMLVideoElement | null;
        return !!v && typeof v.src === 'string' && v.src.startsWith('blob:');
      },
      { timeout: 30_000 },
    );

    const src = await page.evaluate(() => (document.querySelector('video') as HTMLVideoElement).src);
    expect(src.startsWith('blob:')).toBe(true);

    // Play and confirm readyState advances (MSE appended init + segments).
    await page.evaluate(() => (document.querySelector('video') as HTMLVideoElement).play().catch(() => {}));
    await page.waitForFunction(
      () => {
        const v = document.querySelector('video') as HTMLVideoElement | null;
        return !!v && v.readyState >= 2;
      },
      { timeout: 30_000 },
    );
  });

  test('seek via slider re-buffers through MSE without full reload', async ({ page }) => {
    await openYoutubePreview(page, YT_URL);
    const flagOn = await page.evaluate(
      () => (window as unknown as { __VITE_PREVIEW_MSE_DIRECT__?: boolean }).__VITE_PREVIEW_MSE_DIRECT__ === true,
    );
    test.skip(!flagOn, 'VITE_PREVIEW_MSE_DIRECT not compiled in');

    await page.waitForFunction(
      () => {
        const v = document.querySelector('video') as HTMLVideoElement | null;
        return !!v && v.src.startsWith('blob:') && v.readyState >= 2;
      },
      { timeout: 30_000 },
    );

    // Move the timeline slider to ~50%.
    const slider = page.locator('input[type="range"]').first();
    await slider.evaluate((el, v) => {
      const input = el as HTMLInputElement;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      setter?.call(input, String(v));
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }, 50);

    // After seek the video should still be a blob: src (no hls.js reload) and re-buffer.
    await page.waitForFunction(
      () => {
        const v = document.querySelector('video') as HTMLVideoElement | null;
        return !!v && v.src.startsWith('blob:') && v.readyState >= 1;
      },
      { timeout: 30_000 },
    );
    const src = await page.evaluate(() => (document.querySelector('video') as HTMLVideoElement).src);
    expect(src.startsWith('blob:')).toBe(true);
  });
});
