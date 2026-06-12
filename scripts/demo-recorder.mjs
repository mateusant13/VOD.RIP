#!/usr/bin/env node
/**
 * VOD.RIP Demo Recorder — Manual Interaction Mode
 *
 * Opens the app in a maximized browser window with channels pre-loaded
 * and mock API data, starts Playwright video recording, and waits for
 * YOU to interact naturally. Close the browser tab/window when done.
 *
 * The recording stops when the page is closed, then encodes to:
 *   assets/readme/demo.mp4
 *   assets/readme/demo.gif
 *
 * Usage:  node scripts/demo-recorder.mjs
 *
 * Prerequisites:  API on port 7897, Vite on port 5173 (npm run dev)
 */

import { chromium } from 'playwright';
import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// ─── Paths ──────────────────────────────────────────────────────────────────

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const ASSETS_DIR = path.join(ROOT, 'assets', 'readme');
const VID_DIR = path.join(ASSETS_DIR, '_vid');
const API_URL = 'http://127.0.0.1:7897';
const UI_URL = 'http://localhost:5173';

const FFMPEG_DIR = path.join(
  'C:\\Users\\Administrador\\AppData\\Local\\Microsoft\\WinGet\\Packages',
  'Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe',
  'ffmpeg-8.1.1-full_build', 'bin'
);
const FFMPEG = path.join(FFMPEG_DIR, 'ffmpeg.exe');

// ─── Mock Data ──────────────────────────────────────────────────────────────

const MOCK_INFO = {
  id: 'a1b2c3d4e5f6', title: 'Late Night Gaming — Highlights & Funny Moments',
  duration: 8733, duration_string: '2:25:33', uploader: 'xQc',
  thumbnail: 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#1a1a2e"/>'
    + '<text x="160" y="95" font-family="monospace" font-size="18" fill="#53fc18" text-anchor="middle" font-weight="bold">VOD.RIP</text></svg>'),
  webpage_url: 'https://kick.com/xqc/videos/a1b2c3d4', extractor: 'kick', is_live: false,
  qualities: ['1080p60', '1080p', '720p60', '720p', '460p', '360p'],
  platform: 'Kick', created_at: '2025-06-10T20:00:00Z',
};

const MOCK_SESSION = {
  session_id: 'mock_ses_001', master_url: '/api/preview/hls/mock_ses_001/master.m3u8',
  playback_url: '/api/preview/hls/mock_ses_001/master.m3u8', kind: 'hls',
  variant_heights: [240, 360, 480, 720, 1080],
  quality_labels: ['360p', '480p', '720p', '1080p', '1080p60'], active_height: 480,
};

function svgThumb(w, h, label, color) {
  return `data:image/svg+xml,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="${w}" height="${h}" fill="#1a1a2e"/>`
    + `<text x="${w/2}" y="${h/2}" font-family="monospace" font-size="${Math.round(w*.07)}" fill="${color}" text-anchor="middle" dominant-baseline="middle" font-weight="bold">${label}</text></svg>`
  )}`;
}

const VOD_T = [
  { title: 'Late Night Gaming #42', dur: 8932, durStr: '2:28:52', views: 124532, daysAgo: 1 },
  { title: 'Reacting to the craziest moments', dur: 6104, durStr: '1:41:44', views: 98201, daysAgo: 2 },
  { title: 'Just Chatting with the boys', dur: 5123, durStr: '1:25:23', views: 76110, daysAgo: 3 },
  { title: 'Speedrunning challenge WR attempt', dur: 7450, durStr: '2:04:10', views: 201443, daysAgo: 4 },
  { title: 'Minecraft with viewers', dur: 10233, durStr: '2:50:33', views: 88776, daysAgo: 6 },
];
const CLIP_T = [
  { title: 'This was actually insane', dur: 42, views: 45231, daysAgo: 0 },
  { title: 'Chat went absolutely crazy', dur: 27, views: 32100, daysAgo: 1 },
];

function mkVod(t, p, i, ch) {
  const d = new Date(Date.now() - t.daysAgo * 86400000);
  return { id: `${p}_vod_${i}`, platform: p, title: t.title, duration: t.dur,
    duration_string: t.durStr, created_at: d.toISOString(), views: t.views,
    thumbnail_url: svgThumb(160, 90, 'VOD', '#53fc18'),
    url: p === 'Kick' ? `https://kick.com/${ch}/videos/${p}_vod_${i}` : `https://twitch.tv/videos/${p}_vod_${i}`,
    channel: ch, content_kind: 'vod' };
}
function mkClip(t, p, i, ch) {
  const d = new Date(Date.now() - t.daysAgo * 86400000);
  return { id: `${p}_clip_${i}`, platform: p, title: t.title, duration: t.dur,
    duration_string: `${t.dur}s`, created_at: d.toISOString(), views: t.views,
    thumbnail_url: svgThumb(160, 90, 'CLIP', '#9146FF'),
    url: p === 'Kick' ? `https://kick.com/${ch}/clips/${p}_clip_${i}` : `https://clips.twitch.tv/${p}_clip_${i}`,
    channel: ch, content_kind: 'clip' };
}

const CHANNELS = [
  { id: 'ch_xqc', displayName: 'xQc', kickSlug: 'xqc', twitchSlug: 'xqc',
    vodVideos: VOD_T.map((t,i) => mkVod(t,'Kick',i,'xqc')),
    clipVideos: CLIP_T.map((t,i) => mkClip(t,'Kick',i,'xqc')),
    vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false },
  { id: 'ch_forsen', displayName: 'forsen', kickSlug: 'forsen', twitchSlug: 'forsen',
    vodVideos: VOD_T.slice(0,4).map((t,i) => mkVod({...t, title: `forsen ${t.title}`},'Twitch',i,'forsen')),
    clipVideos: CLIP_T.map((t,i) => mkClip(t,'Twitch',i,'forsen')),
    vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false },
  { id: 'ch_ludwig', displayName: 'Ludwig', kickSlug: 'mogul_moves', twitchSlug: 'ludwig',
    vodVideos: VOD_T.slice(1,4).map((t,i) => mkVod(t,'Kick',i,'ludwig')),
    clipVideos: [], vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false },
  { id: 'ch_asmongold', displayName: 'Asmongold', kickSlug: 'asmongold', twitchSlug: 'asmongold',
    vodVideos: VOD_T.slice(0,3).map((t,i) => mkVod({...t, title: `Asmon ${t.title}`},'Twitch',i,'asmongold')),
    clipVideos: CLIP_T.map((t,i) => mkClip(t,'Twitch',i,'asmongold')),
    vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false },
  { id: 'ch_hasanabi', displayName: 'HasanAbi', kickSlug: 'hasanabi', twitchSlug: 'hasanabi',
    vodVideos: VOD_T.slice(2,5).map((t,i) => mkVod({...t, title: `Hasan ${t.title}`},'Kick',i,'hasanabi')),
    clipVideos: [], vodErrors: {}, clipErrors: {}, updatedAt: new Date().toISOString(), loading: false },
];

// ─── Helpers ────────────────────────────────────────────────────────────────

function log(msg) { console.log(`[${new Date().toLocaleTimeString()}] ${msg}`); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─── Mock API Setup ─────────────────────────────────────────────────────────

async function setupMockApi(page) {
  await page.route('**/api/settings', r => r.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ download_folder: '...', download_threads: 4, max_cache_mb: 200, quality: 'source',
      panel_layout: { previewPanelWidth: 640, urlAside: { w: 288, h: 384 }, main: { w: 640, h: 448 } } }) }));
  await page.route('**/api/info/video*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_INFO) }));
  await page.route('**/api/info/clip*', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_INFO) }));

  await page.route('**/api/preview/session', async (r) => {
    if (r.request().method() === 'POST')
      await r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SESSION) });
    else await r.fulfill({ status: 404 });
  });

  await page.route('**/api/preview/hls/**', async (r) => {
    const u = r.request().url();
    if (u.endsWith('.m3u8'))
      await r.fulfill({ status: 200, contentType: 'application/vnd.apple.mpegurl',
        body: '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\np.m3u8\n' });
    else await r.fulfill({ status: 200, contentType: 'video/mp4', body: '' });
  });

  await page.route('**/api/preview/session/*/quality', r => r.fulfill({ status: 200, body: JSON.stringify(MOCK_SESSION) }));

  const handleVideos = async (r) => {
    const qs = new URL(r.request().url()).searchParams;
    const ks = qs.get('kick_slug')||'', tl = qs.get('twitch_login')||'', ru = qs.get('url')||'';
    const sl = ru.toLowerCase().split('/').pop()||'';
    const ch = CHANNELS.find(c => c.kickSlug===ks||c.twitchSlug===tl||c.kickSlug===sl||c.twitchSlug===sl);
    const ct = qs.get('content')||'vods';
    await r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ videos: ch?.vodVideos??[], channel: ch?.displayName??'xqc',
        platforms: ['Kick','Twitch'], content: ct, days: 14, per_platform_errors: {} }) });
  };
  const handleClips = async (r) => {
    const qs = new URL(r.request().url()).searchParams;
    const ks = qs.get('kick_slug')||'', tl = qs.get('twitch_login')||'', ru = qs.get('url')||'';
    const sl = ru.toLowerCase().split('/').pop()||'';
    const ch = CHANNELS.find(c => c.kickSlug===ks||c.twitchSlug===tl||c.kickSlug===sl||c.twitchSlug===sl);
    await r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ clips: ch?.clipVideos??[], channel: ch?.displayName??'xqc',
        platforms: ['Kick','Twitch'], content: 'clips', per_platform_errors: {} }) });
  };

  await page.route('**/api/channel/videos*', handleVideos);
  await page.route('**/api/channel/clips*', handleClips);
  await page.route('**/api/downloads', r => r.fulfill({ status: 200, contentType: 'application/json', body: '{"active":[],"history":[]}' }));
  await page.route('**/api/app/version', r => r.fulfill({ status: 200, body: '{"version":"1.0.14"}' }));
  await page.route('**/api/info', r => r.fulfill({ status: 200, body: '{"version":"1.0.14","name":"VOD.RIP 🪦"}' }));
  // NOTE: no catch-all. Playwright's last-registered-matching-route wins,
  // and a broad `**/api/**` would shadow all the specific mocks above.
  // Unmocked paths will fall through to the real network, which is fine —
  // the dev API is what the recorder expects the app to talk to.
}

// ─── Main ───────────────────────────────────────────────────────────────────

async function main() {
  console.log('╔══════════════════════════════════════════╗');
  console.log('║   VOD.RIP Demo Recorder (Manual Mode)   ║');
  console.log('╚══════════════════════════════════════════╝\n');

  fs.mkdirSync(ASSETS_DIR, { recursive: true });
  fs.mkdirSync(VID_DIR, { recursive: true });

  // Wait for API
  log('Waiting for API...');
  for (let i = 0; i < 40; i++) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.get(`${API_URL}/api/info`, r => { r.resume(); resolve(); });
        req.on('error', reject);
        req.setTimeout(2000, () => { req.destroy(); reject(new Error('timeout')); });
      });
      break;
    } catch { if (i === 39) { log('API not running. Start: cd backend && python run.py'); process.exit(1); } await sleep(1000); }
  }
  log('API ready.');

  log('Launching browser (fullscreen)...');
  const browser = await chromium.launch({
    headless: false,
    args: ['--no-sandbox', '--start-fullscreen', '--window-position=0,0'],
  });

  // No viewport constraint — window will fill the screen naturally.
  // deviceScaleFactor is omitted because Playwright rejects it when viewport is null.
  const context = await browser.newContext({
    viewport: null,
    locale: 'en-US', colorScheme: 'dark',
    recordVideo: { dir: VID_DIR, size: { width: 1920, height: 1080 } },
  });
  const page = await context.newPage();

  // Mock API + localStorage seed
  await setupMockApi(page);

  await page.addInitScript((data) => {
    try {
      localStorage.setItem('vodrip_saved_channels', JSON.stringify(data.channels));
      localStorage.setItem('vodrip_panel_layout', JSON.stringify({
        previewPanelWidth: 640, urlAside: { w: 288, h: 384 }, main: { w: 640, h: 448 },
      }));
    } catch {}
  }, { channels: CHANNELS });

  // Smooth synthetic cursor overlay.
  // - Hides the OS cursor so the recorder only sees our element.
  // - Reads the real mouse position from the page-level mousemove stream.
  // - Renders the cursor at a position that's lerped toward the target on each
  //   animation frame, so fast hand motion looks smooth in the captured video.
  // - The cursor is a pure visual element with pointer-events: none, so it
  //   never interferes with the app's own mouse handling.
  await page.addInitScript(() => {
    const STYLE_ID = '__demo_cursor_style';
    const styleEl = document.createElement('style');
    styleEl.id = STYLE_ID;
    styleEl.textContent = '*, *::before, *::after { cursor: none !important; }';
    const injectStyle = () => {
      if (!document.getElementById(STYLE_ID)) document.head.appendChild(styleEl);
    };
    if (document.head) injectStyle();
    else document.addEventListener('DOMContentLoaded', injectStyle);

    const initCursor = () => {
      // Standard white arrow pointer (24x24) drawn as inline SVG so it always
      // renders crisp at any zoom and doesn't depend on host fonts. Black
      // outline gives it the same contrast as a real OS cursor on light/dark UI.
      const SVG = `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
          <path d="M3 2 L3 19 L7.5 14.5 L10 21 L13 20 L10.5 13.5 L18 13 Z"
                fill="#ffffff" stroke="#000000" stroke-width="1.2"
                stroke-linejoin="round" />
        </svg>
      `.trim();
      const el = document.createElement('div');
      el.id = '__demo_cursor';
      el.style.cssText = [
        'position:fixed', 'top:0', 'left:0', 'width:24px', 'height:24px',
        'z-index:2147483647', 'pointer-events:none',
        'transform:translate3d(-2px,-2px,0)',
        'background:transparent', 'will-change:transform',
      ].join(';');
      el.innerHTML = SVG;
      document.body.appendChild(el);

      // Cursor tracks the real position with a small fixed time lag (~50ms).
      // That gives a consistent, smooth-looking motion without the unbounded
      // drift that a pure lerp exhibits during fast drags — the cursor never
      // falls arbitrarily far behind the real one, so resizes and drags stay
      // locked to it. The ring buffer of recent positions is read on every
      // animation frame; the rendered position is the interpolated point at
      // `now - LAG_MS`, which keeps the visual smooth and predictable.
      const LAG_MS = 50;
      const samples = [];          // [{t, x, y}] oldest → newest
      const MAX_SAMPLES = 64;
      const pushSample = (t, x, y) => {
        const last = samples[samples.length - 1];
        if (last && t - last.t < 1) return;     // dedupe sub-ms events
        samples.push({ t, x, y });
        if (samples.length > MAX_SAMPLES) samples.shift();
      };
      const sampleAt = (targetT) => {
        if (samples.length === 0) return null;
        if (samples.length === 1) return samples[0];
        if (targetT <= samples[0].t) return samples[0];
        const last = samples[samples.length - 1];
        if (targetT >= last.t) return last;
        // Linear search is fine: ring buffer is small and stays roughly sorted.
        for (let i = samples.length - 1; i > 0; i--) {
          const a = samples[i - 1], b = samples[i];
          if (targetT >= a.t && targetT <= b.t) {
            const f = (targetT - a.t) / (b.t - a.t);
            return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
          }
        }
        return last;
      };

      const record = (e) => {
        pushSample(performance.now(), e.clientX, e.clientY);
      };
      // Listen to BOTH pointer and mouse events so this works whether the app
      // uses pointer events (most modern UIs) or mouse events. pointerrawupdate
      // is Chrome-only and fires at the input rate, avoiding rAF coalescing.
      window.addEventListener('pointermove', record, { passive: true });
      window.addEventListener('pointerrawupdate', record, { passive: true });
      window.addEventListener('mousemove', record, { passive: true });

      const tick = () => {
        const s = sampleAt(performance.now() - LAG_MS);
        if (s) el.style.transform = `translate3d(${s.x - 2}px, ${s.y - 2}px, 0)`;
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initCursor);
    } else {
      initCursor();
    }
  });

  await page.goto(UI_URL, { waitUntil: 'networkidle' });
  await sleep(1000);

  // Click Channels tab to show the pre-loaded channels
  try {
    const channelsTab = page.locator('button', { hasText: 'Channels' }).first();
    await channelsTab.waitFor({ state: 'visible', timeout: 5000 });
    await channelsTab.click();
    log('Channels tab opened. You can now interact with the app.');
  } catch {
    log('Could not click Channels tab. The app is open for you to interact with.');
  }

  // Go fullscreen via the F11 shortcut — reliable across platforms and
  // doesn't require a propagating user-gesture token.
  try {
    await page.keyboard.press('F11');
    log('Fullscreen engaged (F11).');
  } catch (err) {
    log(`Fullscreen toggle skipped: ${err.message}`);
  }
  console.log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log('  ✅ RECORDING — close the browser tab when done');
  console.log('  🖱  Your real mouse is recorded with smoothing');
  console.log('  📹  Video will be saved to assets/readme/demo.mp4');
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n');
  // ── Wait for the user to close the tab/browser, then finalize ──
  // Multiple signals cover all the ways a user can stop the recording:
  //   - closing the tab           → page 'close'
  //   - closing the whole window  → context 'close'
  //   - killing the browser process → browser 'disconnected'
  //   - pressing Ctrl-C in the terminal → SIGINT
  // Whichever fires first triggers finalize(); the guard ensures encoding runs once.
  let finalized = false;
  const triggerFinalize = (reason) => {
    if (finalized) return;
    finalized = true;
    log(`Recording stopped (${reason}). Finalizing...`);
    finalize(page, context, browser, VID_DIR, ASSETS_DIR, FFMPEG, log).catch((err) => {
      console.error('Finalize failed:', err);
      process.exitCode = 1;
    });
  };

  const onSignal = (sig) => { triggerFinalize(`signal ${sig}`); };
  process.once('SIGINT', onSignal);
  process.once('SIGTERM', onSignal);

  page.once('close', () => triggerFinalize('tab closed'));
  context.once('close', () => triggerFinalize('context closed'));
  browser.once('disconnected', () => triggerFinalize('browser disconnected'));

  // Block here until any of the signals above fires.
  await new Promise(() => {});
}

async function finalize(page, context, browser, VID_DIR, ASSETS_DIR, FFMPEG, log) {
  // Get video path BEFORE closing the context
  let videoPath = null;
  try { videoPath = page?.video()?.path(); } catch {}

  // Close context to finalize recording
  try { await context?.close(); } catch {}
  try { await browser?.close(); } catch {}
  await sleep(2000);

  // Find the video file
  let webmFile = videoPath;
  if (!webmFile || !fs.existsSync(webmFile)) {
    const files = fs.readdirSync(VID_DIR).filter(f => f.endsWith('.webm'));
    if (files.length > 0) webmFile = path.join(VID_DIR, files[0]);
  }
  if (!webmFile || !fs.existsSync(webmFile)) {
    log('⚠ No video file found. Files in VID_DIR:');
    log(fs.readdirSync(VID_DIR).join(', '));
    process.exit(1);
  }

  log(`Raw recording: ${webmFile}`);

  // ── Encode MP4 ──
  log('Encoding MP4...');
  execSync(
    `"${FFMPEG}" -y -i "${webmFile}" -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p `
    + `-vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" "${path.join(ASSETS_DIR, 'demo.mp4')}"`,
    { stdio: 'inherit', timeout: 180000 }
  );
  log('✓ demo.mp4');

  // ── Encode GIF ──
  log('Encoding GIF...');
  execSync(
    `"${FFMPEG}" -y -i "${path.join(ASSETS_DIR, 'demo.mp4')}" `
    + `-vf "fps=10,scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=bayer" `
    + `-loop 0 "${path.join(ASSETS_DIR, 'demo.gif')}"`,
    { stdio: 'inherit', timeout: 180000 }
  );
  log('✓ demo.gif');

  // Cleanup
  try { fs.rmSync(VID_DIR, { recursive: true }); } catch {}

  log('\n✅ Done!');
  log(`   ${path.join(ASSETS_DIR, 'demo.mp4')}`);
  log(`   ${path.join(ASSETS_DIR, 'demo.gif')}`);
}

main().catch(err => { console.error(err); process.exit(1); });
