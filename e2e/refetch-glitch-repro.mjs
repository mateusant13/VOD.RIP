// Repro for Bug 6: preview panel goes square after channel refresh.
// Run: node e2e/refetch-glitch-repro.mjs
import { chromium } from 'playwright-core';

const BASE = 'http://localhost:5174';
const OUT = 'e2e/.repro-shots';

async function measurePreview(page) {
  return page.evaluate(() => {
    const els = [...document.querySelectorAll('div')];
    const videoHost = els.find((d) => /aspect-ratio/.test(d.style.cssText) && d.querySelector('video, iframe'));
    if (!videoHost) return null;
    let panel = videoHost.parentElement;
    while (panel && !(panel.style && panel.style.width && panel.style.width.includes('px'))) panel = panel.parentElement;
    if (!panel) return null;
    const p = panel.getBoundingClientRect();
    const v = videoHost.getBoundingClientRect();
    return {
      panel: { x: Math.round(p.x), y: Math.round(p.y), w: Math.round(p.width), h: Math.round(p.height) },
      videoHost: { w: Math.round(v.width), h: Math.round(v.height) },
      aspectCss: videoHost.style.aspectRatio,
    };
  });
}

async function dragPreviewHandle(page, dx) {
  const handleBox = await page.evaluate(() => {
    const els = [...document.querySelectorAll('div')];
    const hosts = els.filter((d) => /aspect-ratio/.test(d.style.cssText) && d.querySelector('video'));
    if (!hosts.length) return null;
    let panel = hosts[0].parentElement;
    while (panel && !(panel.style && panel.style.width && panel.style.width.includes('px'))) panel = panel.parentElement;
    if (!panel) return null;
    const pbox = panel.getBoundingClientRect();
    const handles = els.filter((d) => {
      const cls = d.className ? d.className.toString() : '';
      return cls.includes('cursor-ew-resize');
    });
    for (const h of handles) {
      const r = h.getBoundingClientRect();
      if (Math.abs(r.x - (pbox.x + pbox.width)) < 12 && r.width <= 20 && r.height > 100) {
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }
    }
    return null;
  });
  if (!handleBox) throw new Error('resize handle not found');
  await page.mouse.move(handleBox.x, handleBox.y);
  await page.mouse.down();
  await page.mouse.move(handleBox.x + dx, handleBox.y, { steps: 12 });
  await page.mouse.up();
}

async function clickRefresh(page) {
  // Refresh buttons live on the CHANNELS tab and/or the URL-aside channel list.
  const found = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')];
    const b = btns.find((x) => x.querySelector('svg.lucide-refresh-cw'));
    if (b) { b.click(); return true; }
    return false;
  });
  if (found) return;
  await page.evaluate(() => {
    const tabs = [...document.querySelectorAll('button')];
    const t = tabs.find((x) => (x.textContent || '').trim() === 'CHANNELS');
    if (t) t.click();
  });
  await page.waitForTimeout(1200);
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')];
    const b = btns.find((x) => x.querySelector('svg.lucide-refresh-cw'));
    if (b) b.click();
  });
}

async function main() {
  const browser = await chromium.launch({ headless: false, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const page = await browser.newPage({ viewport: { width: 1920, height: 949 } });
  page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200)); });
  page.on('pageerror', (e) => console.log('[pageerror]', String(e).slice(0, 200)));

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  const state = await page.evaluate(() => ({
    url: [...document.querySelectorAll('input')].map((i) => i.value).filter(Boolean)[0] || null,
    hasWatch: [...document.querySelectorAll('button')].some((b) => (b.textContent || '').trim() === 'Watch preview'),
    previewOpen: !!document.querySelector('video'),
  }));
  console.log('state:', JSON.stringify(state));

  if (!state.previewOpen) {
    if (!state.url) {
      await page.locator('input[placeholder*="PASTE VOD"]').fill('https://www.youtube.com/watch?v=jNQXAC9IVRw');
      await page.evaluate(() => {
        const b = [...document.querySelectorAll('button')].find((x) => (x.textContent || '').trim() === 'Extract Info');
        if (b) b.click();
      });
      await page.waitForFunction(() => [...document.querySelectorAll('button')].some((b) => (b.textContent || '').trim() === 'Watch preview'), { timeout: 20000 });
    }
    await page.evaluate(() => {
      const w = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').trim() === 'Watch preview');
      if (w) w.click();
    });
    await page.waitForFunction(() => !!document.querySelector('video'), { timeout: 30000 });
    await page.waitForTimeout(4000);
  }

  const before = await measurePreview(page);
  console.log('BEFORE drag:', JSON.stringify(before));
  if (!before) throw new Error('preview panel not measurable before drag');

  await dragPreviewHandle(page, 260);
  await page.waitForTimeout(800);
  const afterDrag = await measurePreview(page);
  console.log('AFTER drag:', JSON.stringify(afterDrag));

  await page.screenshot({ path: `${OUT}-before-refresh.png` });

  await clickRefresh(page);
  await page.waitForTimeout(6000);
  await page.screenshot({ path: `${OUT}-after-refresh.png` });
  const afterRefresh = await measurePreview(page);
  console.log('AFTER refresh:', JSON.stringify(afterRefresh));

  const ratio = (m) => (m ? (m.panel.h / m.panel.w).toFixed(3) : 'n/a');
  console.log('h/w before drag :', ratio(before));
  console.log('h/w after drag  :', ratio(afterDrag));
  console.log('h/w after refresh:', ratio(afterRefresh));

  await browser.close();
}

main().catch((e) => { console.error('REPRO FAILED:', e); process.exit(1); });
