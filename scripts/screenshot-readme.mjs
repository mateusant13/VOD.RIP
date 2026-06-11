#!/usr/bin/env node
/**
 * VOD.RIP README Screenshot Generator
 *
 * Starts the dev environment (Vite + Python API), drives the app into
 * showcase states, and captures 5 marketing screenshots:
 *   screenshots/readme/hero.png       — URL tab with loaded VOD info (core action)
 *   screenshots/readme/channel-open.png — Expanded channel VOD browser
 *   screenshots/readme/preview.png    — In-app video preview player
 *   screenshots/readme/trim.png       — Trim/crop controls focused
 *   screenshots/readme/queue.png      — Download queue with progress
 *
 * Usage:  node scripts/screenshot-readme.mjs
 */

import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

// ─── Paths ──────────────────────────────────────────────────────────────────

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const SCREENSHOTS_DIR = path.join(ROOT, 'screenshots', 'readme');
const API_PORT = process.env.PORT || '7897';
const API_URL = `http://127.0.0.1:${API_PORT}`;
const UI_URL = 'http://localhost:5173';

// ─── Dimensions ─────────────────────────────────────────────────────────────

const WINDOW_W = 1600;
const WINDOW_H = 1000;

// ─── Mock Data ──────────────────────────────────────────────────────────────

const MOCK_SETTINGS = {
  download_folder: 'C:\\Users\\Streamer\\Downloads',
  download_threads: 4,
  max_cache_mb: 200,
  throttle_kib: 0,
  ffmpeg_path: '',
  temp_folder: '',
  oauth: '',
  quality: 'source',
  panel_layout: {
    previewPanelWidth: 640,
    urlAside: { w: 288, h: 384 },
    main: { w: 640, h: 448 },
  },
  window_geometry: null,
  saved_channels: null,
};

const MOCK_VIDEO_INFO = {
  id: 'a1b2c3d4e5f6',
  title: 'Late Night Gaming — Highlights & Funny Moments',
  duration: 8733,
  duration_string: '2:25:33',
  uploader: 'xQc',
  thumbnail:
    'data:image/svg+xml,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">'
      + '<rect width="320" height="180" fill="#1a1a2e"/>'
      + '<text x="160" y="95" font-family="monospace" font-size="18" fill="#53fc18" text-anchor="middle" dominant-baseline="middle" font-weight="bold">VOD.RIP</text>'
      + '<text x="160" y="120" font-family="monospace" font-size="10" fill="#666" text-anchor="middle" dominant-baseline="middle">PREVIEW</text>'
      + '</svg>'
    ),
  webpage_url: 'https://kick.com/xqc/videos/a1b2c3d4',
  extractor: 'kick',
  is_live: false,
  qualities: ['1080p60', '1080p', '720p60', '720p', '480p', '360p'],
  platform: 'Kick',
  created_at: '2025-06-10T20:00:00Z',
};

const MOCK_PREVIEW_SESSION = {
  session_id: 'mock_session_001',
  master_url: '/api/preview/hls/mock_session_001/master.m3u8',
  playback_url: '/api/preview/hls/mock_session_001/master.m3u8',
  kind: 'hls',
  variant_heights: [240, 360, 480, 720, 1080],
  quality_labels: ['360p', '480p', '720p', '1080p', '1080p60'],
  active_height: 480,
};

const MOCK_DOWNLOADS = {
  active: [
    {
      download_id: 'dl_mock_001',
      url: 'https://kick.com/xqc/videos/a1b2c3d4',
      type: 'video',
      platform: 'Kick',
      status: 'Downloading...',
      progress: 67,
      output_file: 'C:\\Users\\Streamer\\Downloads\\xQc - Late Night Gaming Highlights_kick_a1b2c3d4.mp4',
      error: null,
      started_at: '2025-06-11T12:05:00Z',
      title: 'Late Night Gaming — Highlights & Funny Moments',
      channel: 'xQc',
    },
    {
      download_id: 'dl_mock_002',
      url: 'https://twitch.tv/forsen/clips/GorgeousSmoothLobsterPogChamp',
      type: 'clip',
      platform: 'Twitch',
      status: 'Starting...',
      progress: 3,
      output_file: 'C:\\Users\\Streamer\\Downloads\\forsen - funny clip (clip).mp4',
      error: null,
      started_at: '2025-06-11T12:10:00Z',
      title: 'forsen funny clip',
      channel: 'forsen',
    },
  ],
  history: [
    {
      download_id: 'dl_mock_003',
      url: 'https://kick.com/mogul_moves/videos/67890',
      type: 'video',
      platform: 'Kick',
      status: 'Completed',
      progress: 100,
      output_file: 'C:\\Users\\Streamer\\Downloads\\Ludwig_Ahgren_kick_67890.mp4',
      error: null,
      started_at: '2025-06-11T11:00:00Z',
      title: 'Ludwig Ahgren full stream VOD',
      channel: 'mogul_moves',
    },
  ],
};

const MOCK_LUDWIG_INFO = {
  id: 'ludwig_vod_2025',
  title: 'Ludwig Ahgren — $100,000 Speedrun Challenge',
  duration: 12452,
  duration_string: '3:27:32',
  uploader: 'Ludwig',
  thumbnail:
    'data:image/svg+xml,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">'
      + '<rect width="320" height="180" fill="#1a1a2e"/>'
      + '<text x="160" y="95" font-family="monospace" font-size="18" fill="#53fc18" text-anchor="middle" dominant-baseline="middle" font-weight="bold">VOD.RIP</text>'
      + '<text x="160" y="120" font-family="monospace" font-size="10" fill="#666" text-anchor="middle" dominant-baseline="middle">PREVIEW</text>'
      + '</svg>'
    ),
  webpage_url: 'https://www.twitch.tv/ludwig/videos/ludwig_vod_2025',
  extractor: 'twitch',
  is_live: false,
  qualities: ['1080p60', '1080p', '720p60', '720p', '480p', '360p'],
  platform: 'Twitch',
  created_at: '2026-06-09T18:00:00Z',
};

const VOD_TEMPLATES = [
  { title: 'Late Night Gaming #42', dur: 8932, durStr: '2:28:52', views: 124532, daysAgo: 1, thumb: null },
  { title: 'Reacting to the craziest internet moments', dur: 6104, durStr: '1:41:44', views: 98201, daysAgo: 2, thumb: null },
  { title: 'Just Chatting with the boys', dur: 5123, durStr: '1:25:23', views: 76110, daysAgo: 3, thumb: null },
  { title: 'Speedrunning challenge — WR attempt', dur: 7450, durStr: '2:04:10', views: 201443, daysAgo: 4, thumb: null },
  { title: 'Minecraft with viewers', dur: 10233, durStr: '2:50:33', views: 88776, daysAgo: 6, thumb: null },
  { title: 'New game release — first impressions', dur: 4802, durStr: '1:20:02', views: 54331, daysAgo: 7, thumb: null },
  { title: 'I read the most hateful comments', dur: 6721, durStr: '1:52:01', views: 212098, daysAgo: 9, thumb: null },
];

const CLIP_TEMPLATES = [
  { title: 'This was actually insane', dur: 42, views: 45231, daysAgo: 0 },
  { title: 'Chat went absolutely crazy LUL', dur: 27, views: 32100, daysAgo: 1 },
  { title: 'Perfect timing LMAO', dur: 35, views: 28451, daysAgo: 2 },
  { title: 'He really did that on stream', dur: 18, views: 19832, daysAgo: 3 },
  { title: 'The moment everyone was waiting for', dur: 53, views: 67211, daysAgo: 4 },
  { title: 'UNEXPECTED plot twist', dur: 14, views: 22098, daysAgo: 5 },
  { title: 'Can we talk about this clip?', dur: 31, views: 12443, daysAgo: 6 },
];

function makeVodVideo(t, platform, idx) {
  const date = new Date(Date.now() - t.daysAgo * 86400000);
  const id = `${platform}_vod_${idx}_${Date.now()}`;
  const channel = 'xQc';
  const slug = 'xqc';
  return {
    id, platform,
    title: t.title,
    duration: t.dur,
    duration_string: t.durStr,
    created_at: date.toISOString(),
    views: t.views,
    thumbnail_url: t.thumb || `data:image/svg+xml,${encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90" viewBox="0 0 160 90">'
      + '<rect width="160" height="90" fill="#1a1a2e"/>'
      + '<text x="80" y="48" font-family="monospace" font-size="11" fill="#53fc18" text-anchor="middle" dominant-baseline="middle" font-weight="bold">VOD</text>'
      + '</svg>'
    )}`,
    url: platform === 'Kick'
      ? `https://kick.com/${slug}/videos/${id}`
      : `https://www.twitch.tv/videos/${id}`,
    channel, content_kind: 'vod',
  };
}

function makeClipVideo(t, platform, idx) {
  const date = new Date(Date.now() - t.daysAgo * 86400000);
  const id = `${platform}_clip_${idx}_${Date.now()}`;
  const channel = 'xQc';
  const slug = 'xqc';
  return {
    id, platform,
    title: t.title,
    duration: t.dur,
    duration_string: `${t.dur}s`,
    created_at: date.toISOString(),
    views: t.views,
    thumbnail_url: `data:image/svg+xml,${encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90" viewBox="0 0 160 90">'
      + '<rect width="160" height="90" fill="#1a1a2e"/>'
      + '<text x="80" y="48" font-family="monospace" font-size="11" fill="#9146FF" text-anchor="middle" dominant-baseline="middle" font-weight="bold">CLIP</text>'
      + '</svg>'
    )}`,
    url: platform === 'Kick'
      ? `https://kick.com/${slug}/clips/${id}`
      : `https://clips.twitch.tv/${id}`,
    channel, content_kind: 'clip',
  };
}

const CHANNELS_DATA = [
  {
    id: 'ch_xqc', displayName: 'xQc', kickSlug: 'xqc', twitchSlug: 'xqc',
    vodVideos: VOD_TEMPLATES.map((t, i) => makeVodVideo(t, 'Kick', i)),
    clipVideos: CLIP_TEMPLATES.map((t, i) => makeClipVideo(t, 'Kick', i)),
    vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false,
  },
  {
    id: 'ch_forsen', displayName: 'forsen', kickSlug: 'forsen', twitchSlug: 'forsen',
    vodVideos: VOD_TEMPLATES.slice(0, 4).map((t, i) => makeVodVideo({ ...t, title: `forsen ${t.title}` }, 'Twitch', i)),
    clipVideos: CLIP_TEMPLATES.slice(0, 4).map((t, i) => makeClipVideo(t, 'Twitch', i)),
    vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false,
  },
  {
    id: 'ch_ludwig', displayName: 'Ludwig', kickSlug: 'mogul_moves', twitchSlug: 'ludwig',
    vodVideos: VOD_TEMPLATES.slice(0, 3).map((t, i) => makeVodVideo(t, 'Kick', i)),
    clipVideos: [], vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false,
  },
  {
    id: 'ch_asmongold', displayName: 'Asmongold', kickSlug: 'asmongold', twitchSlug: 'asmongold',
    vodVideos: VOD_TEMPLATES.slice(1, 5).map((t, i) => makeVodVideo({ ...t, title: `Asmon ${t.title}` }, 'Twitch', i)),
    clipVideos: CLIP_TEMPLATES.slice(0, 3).map((t, i) => makeClipVideo(t, 'Twitch', i)),
    vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false,
  },
];

// ─── Helpers ────────────────────────────────────────────────────────────────

function log(msg) {
  const ts = new Date().toLocaleTimeString();
  console.log(`[${ts}] ${msg}`);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForServer(url, label, timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.get(url, (res) => { res.resume(); resolve(); });
        req.on('error', reject);
        req.setTimeout(3000, () => { req.destroy(); reject(new Error('timeout')); });
      });
      log(`✓ ${label} ready at ${url}`);
      return true;
    } catch {
      await sleep(500);
    }
  }
  log(`⚠ ${label} did not become ready within ${timeoutMs / 1000}s`);
  return false;
}

// ─── Server Management ──────────────────────────────────────────────────────

const children = [];

function shutdown(code = 0) {
  log('Shutting down...');
  for (const child of children) {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { shell: true, stdio: 'ignore' });
      } else {
        child.kill('SIGTERM');
      }
    } catch { /* ignore */ }
  }
}

process.on('SIGINT', () => { shutdown(0); process.exit(0); });
process.on('SIGTERM', () => { shutdown(0); process.exit(0); });

async function startServers() {
  log('Starting Python API...');
  const api = spawn('python', ['backend/run.py'], {
    cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], shell: true,
    env: { ...process.env, PORT: API_PORT, KICK_RELOAD: '' },
  });
  api.stdout.on('data', (d) => process.stdout.write(`[api]  ${d}`));
  api.stderr.on('data', (d) => process.stderr.write(`[api]  ${d}`));
  children.push(api);

  await waitForServer(`${API_URL}/api/info`, 'API');

  log('Starting Vite dev server...');
  const vite = spawn('npx.cmd', ['vite'], {
    cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], shell: true,
    env: { ...process.env, PORT: API_PORT },
  });
  vite.stdout.on('data', (d) => process.stdout.write(`[vite] ${d}`));
  vite.stderr.on('data', (d) => process.stderr.write(`[vite] ${d}`));
  children.push(vite);

  await waitForServer(UI_URL, 'UI');
}

// ─── Screenshots ────────────────────────────────────────────────────────────

async function setupMockApi(page) {
  await page.route('**/api/settings', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SETTINGS) });
  });

  await page.route('**/api/info/video*', async (route) => {
    const qs = new URL(route.request().url()).searchParams;
    const id = qs.get('id') || '';
    const body = id.toLowerCase().includes('ludwig') ? MOCK_LUDWIG_INFO : MOCK_VIDEO_INFO;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });

  await page.route('**/api/info/clip*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_VIDEO_INFO) });
  });

  await page.route('**/api/preview/session', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PREVIEW_SESSION) });
    } else {
      await route.fulfill({ status: 404 });
    }
  });

  await page.route('**/api/preview/hls/**', async (route) => {
    const url = route.request().url();
    if (url.endsWith('.m3u8')) {
      await route.fulfill({
        status: 200, contentType: 'application/vnd.apple.mpegurl',
        body: '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\nplaylist.m3u8\n',
      });
    } else if (url.endsWith('playlist.m3u8')) {
      await route.fulfill({
        status: 200, contentType: 'application/vnd.apple.mpegurl',
        body: '#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-VERSION:3\n#EXTINF:10,\nsegment.ts\n#EXT-X-ENDLIST\n',
      });
    } else if (url.endsWith('.ts') || url.endsWith('.mp4') || url.includes('segment') || url.includes('resource')) {
      await route.fulfill({ status: 200, contentType: 'video/mp4', body: '' });
    } else {
      await route.fulfill({ status: 200, body: '' });
    }
  });

  await page.route('**/api/preview/session/*/quality', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PREVIEW_SESSION) });
    } else {
      await route.fulfill({ status: 404 });
    }
  });

  async function handleChannelVideos(route) {
    const qs = new URL(route.request().url()).searchParams;
    const kickSlug = qs.get('kick_slug') || '';
    const twitchLogin = qs.get('twitch_login') || '';
    const rawUrl = qs.get('url') || '';
    const ch = CHANNELS_DATA.find(
      (c) =>
        (kickSlug && c.kickSlug.toLowerCase() === kickSlug.toLowerCase()) ||
        (twitchLogin && c.twitchSlug.toLowerCase() === twitchLogin.toLowerCase()) ||
        (rawUrl && (c.kickSlug === rawUrl.toLowerCase().split('/').pop() ||
                    c.twitchSlug === rawUrl.toLowerCase().split('/').pop()))
    );
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        videos: ch?.vodVideos ?? [], channel: ch?.displayName ?? 'xqc',
        platforms: ['Kick', 'Twitch'], content: 'vods', days: 14, per_platform_errors: {},
      }),
    });
  }

  async function handleChannelClips(route) {
    const qs = new URL(route.request().url()).searchParams;
    const kickSlug = qs.get('kick_slug') || '';
    const twitchLogin = qs.get('twitch_login') || '';
    const rawUrl = qs.get('url') || '';
    const ch = CHANNELS_DATA.find(
      (c) =>
        (kickSlug && c.kickSlug.toLowerCase() === kickSlug.toLowerCase()) ||
        (twitchLogin && c.twitchSlug.toLowerCase() === twitchLogin.toLowerCase()) ||
        (rawUrl && (c.kickSlug === rawUrl.toLowerCase().split('/').pop() ||
                    c.twitchSlug === rawUrl.toLowerCase().split('/').pop()))
    );
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        clips: ch?.clipVideos ?? [], channel: ch?.displayName ?? 'xqc',
        platforms: ['Kick', 'Twitch'], content: 'clips', per_platform_errors: {},
      }),
    });
  }

  await page.route('**/api/channel/videos*', handleChannelVideos);
  await page.route('**/api/channel/clips*', handleChannelClips);

  await page.route('**/api/download/video', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ download_id: 'dl_mock_new', status: 'started' }) });
    } else { await route.fulfill({ status: 404 }); }
  });

  await page.route('**/api/download/clip', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ download_id: 'dl_mock_clip', status: 'started' }) });
    } else { await route.fulfill({ status: 404 }); }
  });

  await page.route('**/api/downloads', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_DOWNLOADS) });
  });

  await page.route('**/api/app/version', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ version: '1.0.5' }) });
  });

  await page.route('**/api/info', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ version: '1.0.5', name: 'VOD.RIP' }) });
  });

  await page.route('**/api/update/check*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ current: '1.0.5', update: null }) });
  });
}

async function injectLocalStorage(page) {
  await page.addInitScript((data) => {
    try {
      localStorage.setItem('vodrip_saved_channels', JSON.stringify(data.channels));
      localStorage.setItem('vodrip_panel_layout', JSON.stringify(data.panelLayout));
    } catch { /* private browsing or quota */ }
  }, {
    channels: CHANNELS_DATA,
    panelLayout: { previewPanelWidth: 640, urlAside: { w: 288, h: 384 }, main: { w: 640, h: 448 } },
  });
}

async function screenshotHero(browser) {
  log('--- hero.png ---');
  // Use a tighter viewport so the app fills most of the image with less empty space
  const heroContext = await browser.newContext({
    viewport: { width: 1300, height: 780 },
    deviceScaleFactor: 2,
    locale: 'en-US',
    colorScheme: 'dark',
  });
  const page = await heroContext.newPage();
  await setupMockApi(page);
  await injectLocalStorage(page);

  await page.goto(UI_URL, { waitUntil: 'networkidle' });
  await sleep(1500);

  // Stay on default URL tab, paste a VOD URL
  const urlInput = page.locator('input[placeholder*="PASTE VOD"]');
  await urlInput.waitFor({ state: 'visible', timeout: 5000 });
  await urlInput.fill('https://kick.com/xqc/videos/a1b2c3d4e5f6');
  await sleep(200);

  // Click Extract Info
  const extractBtn = page.locator('button', { hasText: 'Extract Info' });
  await extractBtn.click();
  await sleep(2500);

  // Wait for VOD info to render (title, duration, qualities, download button)
  await page.waitForSelector('text=Late Night Gaming', { timeout: 5000 }).catch(() => {});
  await sleep(1500);

  // Take a full-page screenshot — tighter viewport means less empty space
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, 'hero.png'), fullPage: false });
  log('✓ hero.png saved');
  await heroContext.close();
}

async function screenshotChannelOpen(page) {
  log('--- channel-open.png ---');
  await page.goto(UI_URL, { waitUntil: 'networkidle' });
  await sleep(1500);

  const channelsTab = page.locator('button', { hasText: 'Channels' }).first();
  await channelsTab.waitFor({ state: 'visible', timeout: 5000 });
  await channelsTab.click();
  await sleep(800);

  let rowCount = await page.locator('[data-channel-row]').count();
  log(`${rowCount} channel rows visible`);
  if (rowCount < 4) { await sleep(2000); rowCount = await page.locator('[data-channel-row]').count(); }

  const firstChannelBtn = page.locator('[data-channel-row]').first().getByText('xQc', { exact: true });
  await firstChannelBtn.waitFor({ state: 'visible', timeout: 3000 });
  await firstChannelBtn.click();
  log('Clicked first channel name');
  await sleep(1000);

  const selectedCount = await page.locator('[data-channel-row].border-white').count().catch(() => 0);
  log(`Selected (border-white) rows: ${selectedCount}`);

  if (selectedCount === 0) {
    log('Retrying click...');
    await page.locator('[data-channel-row]').first().locator('button').nth(1).click();
    await sleep(1000);
  }

  await sleep(3000);

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, 'channel-open.png'), fullPage: false });
  log('✓ channel-open.png saved');
}

async function screenshotPreview(page) {
  log('--- preview.png ---');
  await page.goto(UI_URL, { waitUntil: 'networkidle' });
  await sleep(1500);

  // Type Ludwig VOD URL in the URL tab
  const urlInput = page.locator('input[placeholder*="PASTE VOD"]');
  await urlInput.waitFor({ state: 'visible', timeout: 5000 });
  await urlInput.fill('https://www.twitch.tv/ludwig/videos/ludwig_vod_2025');
  await sleep(200);

  // Click Extract Info
  const extractBtn = page.locator('button', { hasText: 'Extract Info' });
  await extractBtn.click();
  await sleep(2000);

  // Wait for Ludwig VOD info to render
  await page.waitForSelector('text=Ludwig Ahgren', { timeout: 5000 }).catch(() => {});
  await sleep(1500);

  // Click Preview to open the in-app video player
  const previewBtn = page.locator('button', { hasText: /Preview/ }).first();
  await previewBtn.waitFor({ state: 'visible', timeout: 3000 });
  await previewBtn.click();
  log('Clicked Preview button');

  await sleep(3000);

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, 'preview.png'), fullPage: false });
  log('✓ preview.png saved');
}

async function screenshotTrim(page) {
  log('--- trim.png ---');
  // Reuse the preview flow: navigate, load VOD, open preview, then focus on trim
  await page.goto(UI_URL, { waitUntil: 'networkidle' });
  await sleep(1500);

  const urlInput = page.locator('input[placeholder*="PASTE VOD"]');
  await urlInput.waitFor({ state: 'visible', timeout: 5000 });
  await urlInput.fill('https://www.twitch.tv/ludwig/videos/ludwig_vod_2025');
  await sleep(200);

  const extractBtn = page.locator('button', { hasText: 'Extract Info' });
  await extractBtn.click();
  await sleep(2000);

  await page.waitForSelector('text=Ludwig Ahgren', { timeout: 5000 }).catch(() => {});
  await sleep(1500);

  const previewBtn = page.locator('button', { hasText: /Preview/ }).first();
  await previewBtn.waitFor({ state: 'visible', timeout: 3000 });
  await previewBtn.click();
  log('Opened preview');

  await sleep(3000);

  // Screenshot captures both preview player and trim controls panel
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, 'trim.png'), fullPage: false });
  log('✓ trim.png saved');
}

async function screenshotQueue(page) {
  log('--- queue.png ---');
  await page.goto(UI_URL, { waitUntil: 'networkidle' });
  await sleep(1500);

  const queueTab = page.locator('button', { hasText: 'Queue' }).first();
  await queueTab.waitFor({ state: 'visible', timeout: 3000 });
  await queueTab.click();
  await sleep(1500);

  await page.waitForSelector('text=Active Downloads', { timeout: 5000 }).catch(() => {});
  await sleep(500);

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, 'queue.png'), fullPage: false });
  log('✓ queue.png saved');
}

async function ensureScreenshotsDir() {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
}

// ─── Contact Sheet ──────────────────────────────────────────────────────────

async function generateContactSheet() {
  const files = ['hero.png', 'channel-open.png', 'preview.png', 'trim.png', 'queue.png'];
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VOD.RIP Screenshot Review</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #111; color: #eee; font-family: system-ui; padding: 2rem; }
  h1 { font-size: 1.5rem; margin-bottom: 0.5rem; color: #53fc18; }
  p { color: #888; margin-bottom: 2rem; font-size: 0.875rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 1.5rem; }
  .card { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; overflow: hidden; }
  .card img { width: 100%; height: auto; display: block; }
  .card .label { padding: 0.75rem; font-weight: 600; font-size: 0.875rem; }
</style>
</head>
<body>
<h1>VOD.RIP — README Screenshots</h1>
<p>Review all screenshots in one place.</p>
<div class="grid">
${files.map((f) => `<div class="card"><div class="label">${f}</div><img src="${f}" alt="${f}" loading="lazy"></div>`).join('\n')}
</div>
</body>
</html>`;

  const htmlPath = path.join(SCREENSHOTS_DIR, 'contact-sheet.html');
  fs.writeFileSync(htmlPath, html, 'utf-8');
  log(`Contact sheet: ${htmlPath}`);
}

// ─── Main ───────────────────────────────────────────────────────────────────

async function main() {
  console.log('╔══════════════════════════════════════════╗');
  console.log('║   VOD.RIP README Screenshot Generator    ║');
  console.log('╚══════════════════════════════════════════╝\n');

  await ensureScreenshotsDir();
  await startServers();
  log('Both servers are up.\n');

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });

  try {
    const context = await browser.newContext({
      viewport: { width: WINDOW_W, height: WINDOW_H },
      deviceScaleFactor: 2,
      locale: 'en-US',
      colorScheme: 'dark',
    });

    const page = await context.newPage();
    await setupMockApi(page);
    await injectLocalStorage(page);

    await screenshotHero(browser);
    await screenshotChannelOpen(page);
    await screenshotPreview(page);
    await screenshotTrim(page);
    await screenshotQueue(page);

    await generateContactSheet();

    log('\n✅ All screenshots saved to:');
    log(`   ${SCREENSHOTS_DIR}/`);
    log('   ├── hero.png');
    log('   ├── channel-open.png');
    log('   ├── preview.png');
    log('   ├── trim.png');
    log('   ├── queue.png');
    log('   └── contact-sheet.html');

  } catch (err) {
    console.error('Screenshot generation failed:', err);
    throw err;
  } finally {
    await browser.close();
    shutdown();
  }
}

main().catch((err) => {
  console.error(err);
  shutdown(1);
  process.exit(1);
});
