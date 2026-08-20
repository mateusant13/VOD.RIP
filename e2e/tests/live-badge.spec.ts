/**
 * E2E tests for the LIVE badge on channel rows — poll-driven visibility and popup open.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts e2e/tests/live-badge.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

const UI_URL = 'http://localhost:5173';
const CHANNEL_ID = 'e2e-chan-live-1';

const MOCK_CHANNEL = {
  id: CHANNEL_ID,
  displayName: 'TestStreamer',
  kickSlug: 'teststreamer',
  twitchSlug: '',
  youtubeSlug: '',
  vodVideos: [],
  clipVideos: [],
  updatedAt: new Date().toISOString(),
};

const LIVE_STATUS = {
  channel_id: CHANNEL_ID,
  live: [
    {
      platform: 'kick',
      is_live: true,
      title: 'Late night stream',
      url: 'https://kick.com/teststreamer',
      headers: {},
      type: 'hls',
    },
  ],
};

const OFFLINE_STATUS = {
  channel_id: CHANNEL_ID,
  live: [],
};

const MOCK_LIVE_SESSION = {
  session_id: 'e2e-live-sess-1',
  master_url: '/api/preview/hls/e2e-live-sess-1/master.m3u8',
  kind: 'hls',
};

async function mockCoreRoutes(page: Page, liveStatus: typeof LIVE_STATUS | typeof OFFLINE_STATUS) {
  await page.route(`**/api/channels/${CHANNEL_ID}/live`, (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(liveStatus),
    });
  });

  await page.route('**/api/settings', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        download_folder: '/tmp',
        download_folder_confirmed: true,
        download_threads: 4,
        quality: '1080p',
        saved_channels: [MOCK_CHANNEL],
      }),
    });
  });
}

async function seedChannel(page: Page) {
  await page.addInitScript(({ channel }) => {
    localStorage.setItem('vodrip_saved_channels', JSON.stringify([channel]));
    localStorage.removeItem('vodrip_channel_live_status');
  }, { channel: MOCK_CHANNEL });
}

async function openChannelsTab(page: Page) {
  await page.goto(UI_URL);
  await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 15_000 });
  await page.getByRole('button', { name: /CHANNELS|CANAIS|CANALES/i }).click();
  await expect(page.locator('[data-channel-row]')).toBeVisible({ timeout: 15_000 });
}

test.describe('Live badge', () => {
  test('shows LIVE badge when channel poll reports live', async ({ page }) => {
    await seedChannel(page);
    await mockCoreRoutes(page, LIVE_STATUS);
    await openChannelsTab(page);

    const badge = page.locator('[data-channel-row]').getByRole('button', { name: /Live|Ao vivo|En vivo/i });
    await expect(badge).toBeVisible({ timeout: 15_000 });
    await expect(badge).toContainText('LIVE');
  });

  test('clicking LIVE badge opens the live player popup', async ({ page }) => {
    await seedChannel(page);
    await mockCoreRoutes(page, LIVE_STATUS);

    await page.route('**/api/preview/live', (route) => {
      if (route.request().method() !== 'POST') {
        void route.continue();
        return;
      }
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_LIVE_SESSION),
      });
    });

    await openChannelsTab(page);

    const badge = page.locator('[data-channel-row]').getByRole('button', { name: /Live|Ao vivo|En vivo/i });
    await expect(badge).toBeVisible({ timeout: 15_000 });
    await badge.click();

    await expect(page.locator('[data-live-popup]')).toBeVisible({ timeout: 15_000 });
  });

  test('hides LIVE badge when channel poll reports offline', async ({ page }) => {
    await seedChannel(page);
    await mockCoreRoutes(page, OFFLINE_STATUS);
    await openChannelsTab(page);

    const badge = page.locator('[data-channel-row]').getByRole('button', { name: /Live|Ao vivo|En vivo/i });
    await expect(badge).toBeHidden({ timeout: 15_000 });
  });
});
