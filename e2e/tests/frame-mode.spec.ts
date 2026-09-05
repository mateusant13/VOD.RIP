/**
 * E2E tests for Frame mode — toggle, overlay grid, drag visibility, persistence.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts e2e/tests/frame-mode.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

// The playwright config sets UI_URL to the webServer's port; fall back to the
// conventional dev port so a lone-file run still works.
const UI_URL = process.env.UI_URL || 'http://localhost:5173';

/** Minimal settings mock so the app shell loads without depending on backend state. */
async function mockSettingsRoute(page: Page) {
  await page.route('**/api/settings', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        download_folder: '/tmp',
        download_folder_confirmed: true,
        download_threads: 4,
        quality: '1080p',
        saved_channels: [],
      }),
    });
  });
}

test.describe('Frame mode', () => {
  test.beforeEach(async ({ page }) => {
    await mockSettingsRoute(page);
    await page.addInitScript(() => {
      // Suppress first-run overlays that intercept the frame toggle:
      // FirstRunWizard (z-9999) and CookieInstallOffer (z-21000) both render
      // full-screen `fixed inset-0` modals that swallow clicks on anything
      // beneath them, including the bottom-right frame checkbox. The wizard
      // is gated on `vodrip.onboardingDone`; the cookie offer on the unseen
      // `vodrip.firstTime.cookieInstall` tutorial flag. Seeding both keeps
      // the frame-mode surface reachable.
      localStorage.setItem('vodrip.onboardingDone', '1');
      localStorage.setItem('vodrip.firstTime.cookieInstall', '1');
      localStorage.removeItem('vodrip.ui.frameMode');
    });
  });

  test('frame toggle enables overlay grid and persists to localStorage', async ({ page }) => {
    await page.goto(UI_URL);
    await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 60_000 });

    const toggle = page.locator('[data-frame-toggle] input[type="checkbox"]');
    await expect(toggle).toBeVisible();
    await expect(page.locator('[data-frame-overlay]')).toHaveCount(0);

    await toggle.check();

    const overlay = page.locator('[data-frame-overlay]');
    await expect(overlay).toBeVisible();
    // Tiling guide is hidden while idle and reveals only mid-drag.
    await expect(overlay).toHaveCSS('opacity', '0');
    await expect(overlay).toHaveCSS('pointer-events', 'none');
    await expect(page.locator('[data-frame-cell="0"]')).toBeVisible();
    await expect(page.locator('[data-frame-cell="5"]')).toBeVisible();

    const stored = await page.evaluate(() => localStorage.getItem('vodrip.ui.frameMode'));
    expect(stored).toBe('1');
  });
  test('frame mode grid appears mid-drag and is click-through when idle', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('vodrip.ui.frameMode', '1');
    });

    await page.goto(UI_URL);
    const overlay = page.locator('[data-frame-overlay]');
    await expect(overlay).toBeVisible({ timeout: 15_000 });
    // Tiling guide is hidden while idle so the base channel grid is unblocked.
    await expect(overlay).toHaveCSS('opacity', '0');
    // Click-through when idle so the base channel cards stay grabbable.
    await expect(overlay).toHaveCSS('pointer-events', 'none');

    await page.evaluate(() => {
      document.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true }));
    });

    // Guide appears + becomes drop-capable mid-drag.
    await expect(overlay).toHaveCSS('opacity', '1');
    await expect(overlay).toHaveCSS('pointer-events', 'auto');

    await page.evaluate(() => {
      document.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true }));
    });

    await expect(overlay).toHaveCSS('opacity', '0');
    await expect(overlay).toHaveCSS('pointer-events', 'none');
  });
  test('restores frame mode from localStorage on reload', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('vodrip.ui.frameMode', '1');
    });

    await page.goto(UI_URL);
    await expect(page.locator('[data-frame-overlay]')).toBeVisible({ timeout: 15_000 });

    const toggle = page.locator('[data-frame-toggle] input[type="checkbox"]');
    await expect(toggle).toBeChecked();

    await toggle.uncheck();
    await expect(page.locator('[data-frame-overlay]')).toHaveCount(0);

    const stored = await page.evaluate(() => localStorage.getItem('vodrip.ui.frameMode'));
    expect(stored).toBe('0');
  });
});
