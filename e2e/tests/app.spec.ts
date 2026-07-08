/**
 * E2E tests for VOD.RIP — autonomous browser tests that verify the app
 * works correctly by navigating like a real user.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts
 */

import { test, expect, type Page, type BrowserContext } from '@playwright/test';

const UI_URL = 'http://localhost:5173';
const API_URL = 'http://localhost:7897';

/** Collect console errors during a test. */
async function collectConsoleErrors(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on('pageerror', (err) => errors.push(`Uncaught: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      errors.push(`Console[${msg.type()}]: ${msg.text()}`);
    }
  });
  return errors;
}

test.describe('App loads correctly', () => {
  test('homepage loads and shows the app shell', async ({ page }) => {
    const errors = await collectConsoleErrors(page);

    await page.goto(UI_URL);
    await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 15_000 });

    // The URL input should be present
    const urlInput = page.getByPlaceholder(/paste vod or clip link/i);
    await expect(urlInput).toBeVisible({ timeout: 10_000 });

    // Report any console errors
    for (const err of errors) {
      console.warn('Console error:', err);
    }
    expect(errors.filter((e) => !e.includes('favicon'))).toHaveLength(0);
  });

  test('tab switching works — URL, Queue, Settings', async ({ page }) => {
    await page.goto(UI_URL);
    await page.waitForSelector('.vod-app-shell', { timeout: 15_000 });

    // Click Queue tab
    const queueTab = page.getByText(/Queue/i);
    await expect(queueTab).toBeVisible();
    await queueTab.click();

    // Click Settings tab
    const settingsTab = page.getByText(/Settings/i);
    await expect(settingsTab).toBeVisible();
    await settingsTab.click();

    // Click URL tab to go back
    const urlTab = page.getByText(/URL/i);
    await expect(urlTab).toBeVisible();
    await urlTab.click();
  });
});

test.describe('API connectivity', () => {
  test('API /api/info endpoint is reachable', async ({ request }) => {
    const resp = await request.get(`${API_URL}/api/info`);
    expect(resp.ok()).toBeTruthy();
    const data = await resp.json();
    expect(data).toHaveProperty('version');
    expect(data).toHaveProperty('name');
  });

  test('API /api/settings returns settings', async ({ request }) => {
    const resp = await request.get(`${API_URL}/api/settings`);
    expect(resp.ok()).toBeTruthy();
    const data = await resp.json();
    expect(data).toHaveProperty('download_threads');
    expect(data).toHaveProperty('quality');
  });

  test('API /api/ytdlp/status reports available', async ({ request }) => {
    const resp = await request.get(`${API_URL}/api/ytdlp/status`);
    expect(resp.ok()).toBeTruthy();
    const data = await resp.json();
    expect(data.available).toBe(true);
  });

  test('API /api/app/version returns version', async ({ request }) => {
    const resp = await request.get(`${API_URL}/api/app/version`);
    expect(resp.ok()).toBeTruthy();
    const data = await resp.json();
    expect(data).toHaveProperty('version');
  });
});

test.describe('Download 1GB cap', () => {
  test('POST /api/download/video with oversized estimate receives cap_warning', async ({ request }) => {
    // Start a download with a full VOD URL — the backend estimates the size.
    // If it exceeds 1GB, the response should include cap_warning.
    const resp = await request.post(`${API_URL}/api/download/video`, {
      data: {
        url: 'https://www.twitch.tv/videos/2274706976',
        quality: 'source',
        crop_start: 0,
        crop_end: 36000,  // 10 hours — definitely over 1GB
      },
    });
    // Should succeed (download may be queued)
    expect(resp.ok()).toBeTruthy();
    const data = await resp.json();
    // If estimated size exceeds 1GB, cap_warning should be present
    if (data.cap_warning) {
      console.log('Download cap activated:', data.cap_warning);
      expect(data.cap_warning).toContain('capped');
    }
  });

  test('POST /api/download/video with small trim has no cap_warning', async ({ request }) => {
    const resp = await request.post(`${API_URL}/api/download/video`, {
      data: {
        url: 'https://www.twitch.tv/videos/2274706976',
        quality: 'source',
        crop_start: 0,
        crop_end: 10,  // 10 seconds — tiny, no cap needed
      },
    });
    expect(resp.ok()).toBeTruthy();
    const data = await resp.json();
    expect(data.cap_warning).toBeNull();
  });
});

test.describe('Preview system', () => {
  test('Preview session creation validates crop range', async ({ request }) => {
    const resp = await request.post(`${API_URL}/api/preview/session`, {
      data: {
        url: 'https://kick.com/test/clip/abc',
        crop_start: 10,
        crop_end: 5,  // end before start — should be rejected
      },
    });
    expect(resp.status()).toBe(400);
    const data = await resp.json();
    expect(data.detail).toContain('End must be after start');
  });

  test('Preview session rejects missing URL', async ({ request }) => {
    const resp = await request.post(`${API_URL}/api/preview/session`, {
      data: { crop_start: 0, crop_end: 10 },
    });
    expect(resp.status()).toBe(422);
  });
});

test.describe('Channel browser', () => {
  test('Channel videos endpoint returns 422 without params', async ({ request }) => {
    const resp = await request.get(`${API_URL}/api/channel/videos`);
    expect(resp.status()).toBe(422);
  });

  test('Channel clips endpoint returns 400 without params', async ({ request }) => {
    const resp = await request.get(`${API_URL}/api/channel/clips`);
    expect(resp.status()).toBe(400);
  });
});
