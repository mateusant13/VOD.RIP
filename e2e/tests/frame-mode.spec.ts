/**
 * E2E tests for Frame mode — toggle, overlay grid, drag visibility, persistence.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts e2e/tests/frame-mode.spec.ts
 */
import { test, expect, type Locator, type Page } from '@playwright/test';

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

/**
 * Idle guide (no drag in flight): VISIBLE but faint, and click-through.
 *
 * This used to assert `opacity: 0`, which is the behaviour the bug report
 * was about — a frame grid you cannot see gives the user nothing to aim at,
 * so dragging "never works". The guide now shows at low opacity whenever
 * frame mode is on and brightens to 1 during a drag.
 */
async function expectIdleGuide(overlay: Locator) {
  const opacity = await overlay.evaluate((el) => parseFloat(el.style.opacity));
  expect(opacity).toBeGreaterThan(0);
  expect(opacity).toBeLessThan(1);
  // Click-through when idle so the base channel cards stay grabbable.
  await expect(overlay).toHaveCSS('pointer-events', 'none');
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
    // The guide is faintly visible while idle, so there is a target to aim
    // at before any drag starts, and it stays click-through.
    await expectIdleGuide(overlay);
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
    // Faint but present while idle, and click-through.
    await expectIdleGuide(overlay);

    await page.evaluate(() => {
      document.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true }));
    });

    // Guide brightens + becomes drop-capable mid-drag.
    await expect(overlay).toHaveCSS('opacity', '1');
    await expect(overlay).toHaveCSS('pointer-events', 'auto');

    await page.evaluate(() => {
      document.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true }));
    });

    // Back to the idle guide — still visible, still click-through.
    await expectIdleGuide(overlay);
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
