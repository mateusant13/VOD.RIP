/**
 * E2E: frame-mode drag-to-snap with a REAL mounted explore popup.
 *
 * Regression for the frame-snap chain proven live on 2026-09-04:
 *   popup body pointerdown → ChannelExplorePopup.onPopupDrag → document
 *   'explore-frame-arm' {id} → FrameOverlay arms grid + geometry hover →
 *   frameCellContents[index] = popupId → frameSnapRect → popup pins
 *   (centered, aspect-fit, maxW/maxH clamped) inside the cell.
 *
 * Also covers the sibling contract: dragging the body out of the cell
 * releases it (onUnsnap clears the cell), and dragging back re-pins.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts e2e/tests/frame-popup-snap.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const UI_URL = process.env.UI_URL || 'http://localhost:5173';

const FAKE_CHANNEL = {
  id: 'ch_e2e_frame',
  displayName: 'e2eframe',
  kickSlug: 'e2eframe',
  twitchSlug: '',
  youtubeSlug: '',
  updatedAt: new Date().toISOString(),
  vodVideos: [
    {
      id: 'kick-e2e-1',
      platform: 'Kick',
      title: 'E2E snap target VOD',
      duration: 300,
      duration_string: '5:00',
      created_at: new Date(Date.now() - 3_600_000).toISOString(),
      views: 1000,
      thumbnail_url: null,
      url: 'https://kick.com/e2eframe/videos/kick-e2e-1',
      channel: 'e2eframe',
      content_kind: 'vod',
    },
  ],
  clipVideos: [],
  vodPlatformsFetched: { Kick: true },
};

/** Settings mock that seeds one Kick channel with one VOD. */
async function mockSettingsWithChannel(page: Page) {
  await page.route('**/api/settings', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        download_folder: '/tmp',
        download_folder_confirmed: true,
        download_threads: 4,
        quality: '1080p',
        saved_channels: [FAKE_CHANNEL],
        channel_kick_enabled: true,
        channel_twitch_enabled: true,
        channel_youtube_enabled: true,
      }),
    }),
  );
}

/** Progressive MP4 preview session so the popup never needs real YouTube/Twitch media. */
async function mockPreviewSession(page: Page) {
  await page.route('**/api/preview/session', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = JSON.parse(route.request().postData() || '{}') as { url?: string };
    const sid = 'e2e-snap-session';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: sid,
        master_url: `/api/preview/hls/${sid}/master.m3u8`,
        playback_url: `/preview-e2e/${sid}/stream.mp4`,
        kind: 'progressive',
        duration_sec: 300,
        variant_heights: [720],
        active_height: 720,
      }),
    });
  });
  // Real 1s MP4 fixture (160x90 h264/aac) — a bare ftyp box fails to decode
  // in headless Chromium (MediaError code 4), which surfaces the popup's
  // error banner and shifts the pinned layout.
  await page.route('**/preview-e2e/**', (route) => {
    const file = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../fixtures/sample.mp4');
    route.fulfill({ status: 200, contentType: 'video/mp4', path: file });
  });
}

test.describe('Frame mode popup snap', () => {
  test.beforeEach(async ({ page }) => {
    await mockSettingsWithChannel(page);
    await mockPreviewSession(page);
    await page.addInitScript(() => {
      localStorage.setItem('vodrip.ui.frameMode', '1');
      localStorage.setItem('vodrip.onboardingDone', '1');
      localStorage.setItem('vodrip.firstTime.cookieInstall', '1');
    });
  });

  /** Find a grab point on the popup body that the drag guard accepts. */
  async function findGrabPoint(page: Page) {
    const grab = await page.evaluate(() => {
      const p = document.querySelector<HTMLElement>('.explore-frame-popup');
      if (!p) return null;
      const r = p.getBoundingClientRect();
      for (const frac of [0.5, 0.4, 0.45, 0.55, 0.35]) {
        const x = r.x + r.width / 2;
        const y = r.y + r.height * frac;
        const el = document.elementFromPoint(x, y);
        if (el && !el.closest('button, input, select, textarea, a, [role="slider"], [data-player-menu], [data-preview-chat-panel]')) {
          return { x, y };
        }
      }
      return null;
    });
    expect(grab).not.toBeNull();
    return grab!;
  }

  /** Trusted-mouse drag of the popup body into a cell; asserts the arm + hover. */
  async function dragPopupBodyTo(page: Page, cellIndex: number) {
    const target = page.locator(`[data-frame-cell="${cellIndex}"]`);
    const box = await target.boundingBox();
    if (!box) throw new Error(`cell ${cellIndex} has no box`);

    const grab = await findGrabPoint(page);
    await page.mouse.move(grab.x, grab.y);
    await page.mouse.down();
    // Grid must be armed mid-drag (pointer-arm, not HTML5 drag).
    const overlay = page.locator('[data-frame-overlay]');
    await expect(overlay).toHaveCSS('pointer-events', 'auto');
    await expect(overlay).toHaveCSS('opacity', '1');
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 6 });
    await expect(page.locator(`[data-frame-cell="${cellIndex}"]`)).toHaveCSS(
      'border-color',
      'rgb(255, 255, 255)',
    );
    await page.mouse.up();
  }

  test('dragging a real explore popup body snaps it into frame cell 0', async ({ page }) => {
    await page.goto(UI_URL);
    await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 60_000 });

    // Open the CHANNELS panel (locale-tolerant: CHANNELS/CANAIS/…).
    await page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find((x) =>
        /^(channels|canais)$/i.test((x.textContent || '').trim()),
      );
      b?.click();
    });
    await expect(page.locator('[data-channel-row]').first()).toBeVisible({ timeout: 10_000 });

    // Select the seeded channel → VOD list renders with a Preview button.
    await page.locator('[data-channel-row] div[role="button"]').first().click();
    await expect(page.locator('button[title="Preview VOD"]')).toBeVisible({ timeout: 10_000 });
    await page.locator('button[title="Preview VOD"]').first().click();

    const popup = page.locator('.explore-frame-popup');
    await expect(popup).toBeVisible({ timeout: 10_000 });
    await expect(popup).toHaveAttribute('role', 'application');
    const before = await popup.boundingBox();
    expect(before).toBeTruthy();

    await dragPopupBodyTo(page, 0);

    // Pin: popup rect contained in cell 0, centered in it, clamped by the
    // frameSnapRect layout effect (max-width/max-height inline styles).
    const cell = await page.locator('[data-frame-cell="0"]').boundingBox();
    const after = await popup.boundingBox();
    expect(cell).toBeTruthy();
    expect(after).toBeTruthy();
    const c = cell!;
    const a = after!;
    expect(a.x).toBeGreaterThanOrEqual(c.x - 2);
    expect(a.y).toBeGreaterThanOrEqual(c.y - 2);
    expect(a.x + a.width).toBeLessThanOrEqual(c.x + c.width + 2);
    expect(a.y + a.height).toBeLessThanOrEqual(c.y + c.height + 2);
    const centerDelta = async () => {
      const a = (await popup.boundingBox())!;
      return Math.max(
        Math.abs(a.x + a.width / 2 - (c.x + c.width / 2)),
        Math.abs(a.y + a.height / 2 - (c.y + c.height / 2)),
      );
    };
    await expect.poll(centerDelta, { timeout: 5_000 }).toBeLessThan(12);
    const maxW = await popup.evaluate((el) => el.style.maxWidth);
    const maxH = await popup.evaluate((el) => el.style.maxHeight);
    expect(parseFloat(maxW)).toBeGreaterThan(0);
    expect(parseFloat(maxW)).toBeLessThanOrEqual(c.width - 12);
    expect(parseFloat(maxH)).toBeGreaterThan(0);
    expect(parseFloat(maxH)).toBeLessThanOrEqual(c.height - 12);

    // Overlay returned to idle after release.
    await expect(page.locator('[data-frame-overlay]')).toHaveCSS('pointer-events', 'none');
  });

  test('dragging the pinned popup body out of the cell releases it, dragging back re-pins', async ({ page }) => {
    await page.goto(UI_URL);
    await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 60_000 });

    // Open the CHANNELS panel (locale-tolerant: CHANNELS/CANAIS/…).
    await page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find((x) =>
        /^(channels|canais)$/i.test((x.textContent || '').trim()),
      );
      b?.click();
    });
    await expect(page.locator('[data-channel-row]').first()).toBeVisible({ timeout: 10_000 });
    await page.locator('[data-channel-row] div[role="button"]').first().click();
    await expect(page.locator('button[title="Preview VOD"]')).toBeVisible({ timeout: 10_000 });
    await page.locator('button[title="Preview VOD"]').first().click();
    const popup = page.locator('.explore-frame-popup');
    await expect(popup).toBeVisible({ timeout: 10_000 });

    // Pin into cell 0 first (same gesture as the snap test).
    await dragPopupBodyTo(page, 0);
    const cell = await page.locator('[data-frame-cell="0"]').boundingBox();
    expect(cell).toBeTruthy();
    const c = cell!;

    // Unsnap: pointerdown on the pinned body releases it (onUnsnap), and
    // releasing in the row gap (outside every cell) leaves it floating.
    const grab = await findGrabPoint(page);
    await page.mouse.move(grab.x, grab.y);
    await page.mouse.down();
    await expect(page.locator('[data-frame-overlay]')).toHaveCSS('pointer-events', 'auto');
    await page.mouse.move(c.x + c.width + 216, c.y + c.height + 4, { steps: 6 });
    await page.mouse.up();
    await expect
      .poll(async () => (await popup.boundingBox())!.x, { timeout: 5_000 })
      .toBeGreaterThan(c.x + c.width);

    // Re-snap: drag the floating body back into cell 0 — it must re-pin
    // centered (the pin layout effect re-centers when chrome height changes,
    // e.g. after the video loads or an error banner appears).
    await dragPopupBodyTo(page, 0);
    const centerDelta = async () => {
      const a = (await popup.boundingBox())!;
      return Math.max(
        Math.abs(a.x + a.width / 2 - (c.x + c.width / 2)),
        Math.abs(a.y + a.height / 2 - (c.y + c.height / 2)),
      );
    };
    await expect.poll(centerDelta, { timeout: 5_000 }).toBeLessThan(12);
    const after = (await popup.boundingBox())!;
    expect(after.x).toBeGreaterThanOrEqual(c.x - 2);
    expect(after.y).toBeGreaterThanOrEqual(c.y - 2);
    expect(after.x + after.width).toBeLessThanOrEqual(c.x + c.width + 2);
    expect(after.y + after.height).toBeLessThanOrEqual(c.y + c.height + 2);
  });
});
