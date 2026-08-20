/**
 * E2E tests for Frame mode — toggle, overlay grid, drag visibility, persistence.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts e2e/tests/frame-mode.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

const UI_URL = 'http://localhost:5173';

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
      localStorage.removeItem('vodrip.ui.frameMode');
    });
  });

  test('frame toggle enables overlay grid and persists to localStorage', async ({ page }) => {
    await page.goto(UI_URL);
    await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 15_000 });

    const toggle = page.locator('[data-frame-toggle] input[type="checkbox"]');
    await expect(toggle).toBeVisible();
    await expect(page.locator('[data-frame-overlay]')).toHaveCount(0);

    await toggle.check();

    const overlay = page.locator('[data-frame-overlay]');
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveCSS('opacity', '0');
    await expect(overlay).toHaveCSS('pointer-events', 'none');
    await expect(page.locator('[data-frame-cell="0"]')).toBeVisible();
    await expect(page.locator('[data-frame-cell="5"]')).toBeVisible();

    const stored = await page.evaluate(() => localStorage.getItem('vodrip.ui.frameMode'));
    expect(stored).toBe('1');
  });

  test('overlay becomes visible during an HTML5 drag', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('vodrip.ui.frameMode', '1');
    });

    await page.goto(UI_URL);
    const overlay = page.locator('[data-frame-overlay]');
    await expect(overlay).toBeVisible({ timeout: 15_000 });
    await expect(overlay).toHaveCSS('opacity', '0');

    await page.evaluate(() => {
      document.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true }));
    });

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
